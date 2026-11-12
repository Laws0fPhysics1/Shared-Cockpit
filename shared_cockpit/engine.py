"""Shared-cockpit sync engine.

Threads:
  sim thread   - owns SimConnect (all SimConnect calls happen here)
  net reader   - pushes received messages into self.inbox
  GUI thread   - pushes commands into self.inbox, reads self.status

Model: the pilot flying (PF) runs the real flight model. The other seat's
aircraft is frozen (lat/lon, altitude, attitude) and positioned every frame
from PF snapshots, rendered ~DELAY seconds in the past and interpolated, so
network jitter doesn't cause stutter.
"""
import math
import queue
import socket
import struct
import threading
import time
from collections import deque

from . import flavor, net, syncdefs as sd
from .simconnect import (Block, SimConnect, SimConnectError, PERIOD_NEVER, PERIOD_ONCE,
                         PERIOD_SIM_FRAME, PERIOD_SECOND, FLAG_CHANGED, DT_STRING256,
                         DT_FLOAT64)

PROTO = 1
DELAY = 0.10          # interpolation delay (s); raise if motion stutters on bad links
MAX_EXTRAP = 0.25     # max extrapolation past newest snapshot (s)
SEND_HZ = 40.0        # PF motion packets per second
APPLY_HZ = 60.0       # fallback placement rate when own sim frames aren't arriving

EV_AIRCRAFT_LOADED, EV_SIM_START, EV_FLIGHT_LOADED = 1, 2, 3
DEF_INFO, REQ_INFO, REQ_ENUM_IE = 1, 1, 2


def _lerp_angle(a, b, f):
    d = (b - a + 180.0) % 360.0 - 180.0
    return a + d * f


def _wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


class Engine:
    def __init__(self, log, name="Pilot"):
        self.log = log
        self.name = name
        self.inbox = queue.Queue()
        self.running = True

        self.sc = None
        self.ready = False
        self.gen = 0                 # data-definition generation (rebuilt per aircraft)
        self.title = ""
        self.model = ""
        self.engines = 1

        self.role = None             # "host" | "join"
        self.link = None
        self.hostsrv = None
        self.peer = {}
        self.pf = None               # "host" | "join": who has the controls
        self.ctl_epoch = 0
        self._frozen = False
        self._last_freeze = 0.0

        self.snaps = deque(maxlen=120)
        self._offsets = deque()
        self._offset = None
        self._last_send = 0.0
        self._last_apply = 0.0
        self._last_frame = 0.0
        self._frz_state = None
        self.ie_type, self.ie_double = {}, None
        self._latest_ctrl = None
        self._last_ping = 0.0
        self.rtt = None

        self.sw_prev = {}            # last local reading per switch
        self.sw_known = {}           # last agreed value per switch
        self.sw_items = {s[0]: s for s in sd.SWITCHES}
        self.sw_set_blocks = {}

        self.ie_by_hash, self.ie_by_name, self.ie_applied = {}, {}, {}
        self.exc_count = 0

        self.status = {"sim": "Not connected", "net": "Idle", "pf": "", "aircraft": "",
                       "peer_aircraft": "", "rtt": "", "ie": ""}
        threading.Thread(target=self._sim_loop, daemon=True).start()

    # ================================================================ public (GUI thread)
    def host(self, port):
        self.stop_net()
        self.role = "host"
        self.hostsrv = net.Host(port, self._on_sock, self.log)
        self.status["net"] = f"Hosting on port {port}, waiting for co-pilot..."
        self.log(f"Hosting on TCP port {port}. Your friend joins with your public IP and this port.")
        self.inbox.put(("cmd", "set_pf", "host"))

    def join(self, ip, port):
        self.stop_net()
        self.role = "join"
        self.status["net"] = f"Connecting to {ip}:{port}..."

        def go():
            try:
                s = net.connect(ip, port)
            except (OSError, socket.timeout) as e:
                self.status["net"] = "Connect failed"
                self.log(f"Could not connect to {ip}:{port}: {e}")
                return
            self.log(f"Connected to {ip}:{port}")
            self._on_sock(s)
        threading.Thread(target=go, daemon=True).start()

    def stop_net(self):
        if self.link:
            self.link.close("you disconnected")
        if self.hostsrv:
            self.hostsrv.stop()
            self.hostsrv = None
        self.role = None
        self.status["net"] = "Idle"

    def take_controls(self):
        self.inbox.put(("cmd", "take"))

    def give_controls(self):
        self.inbox.put(("cmd", "give"))

    def shutdown(self):
        self.running = False
        self.stop_net()

    def i_have_controls(self):
        return self.link is None or self.pf == self.role

    # ================================================================ network glue
    def _on_sock(self, sock):
        if self.hostsrv:
            self.hostsrv.busy = True
        self.link = net.Link(sock, lambda m: self.inbox.put(("net", m)), self._on_close)
        self.status["net"] = "Connected"
        self._send({"t": "hello", "v": PROTO, "name": self.name, "title": self.title,
                    "model": self.model, "sim": flavor.SIM,
                    "role": self.role, "ie": bool(self.sc and self.sc.has_input_events)})

    def _on_close(self, reason):
        self.log(f"Link closed: {reason}")
        self.link = None
        if self.hostsrv:
            self.hostsrv.busy = False
            self.status["net"] = "Co-pilot left. Waiting for co-pilot..."
        else:
            self.status["net"] = "Disconnected"
        self.peer = {}
        self.inbox.put(("cmd", "disconnected"))

    def _send(self, msg):
        if self.link:
            self.link.send(msg)

    # ================================================================ sim thread
    def _sim_loop(self):
        while self.running:
            if self.sc is None:
                if not self._try_connect():
                    self._drain_inbox_offline()
                    time.sleep(2.0)
                    continue
            try:
                for m in self.sc.poll():
                    self._on_sim(m)
                while True:
                    try:
                        item = self.inbox.get_nowait()
                    except queue.Empty:
                        break
                    self._on_inbox(item)
                self._tick()
            except SimConnectError as e:
                self.log(f"SimConnect error: {e}")
                self._drop_sim()
            except OSError as e:
                self.log(f"SimConnect DLL error: {e}")
                self._drop_sim()
            except Exception as e:  # never let the sim thread die silently
                import traceback
                self.log(f"Internal error: {e!r} " + f"{traceback.format_exc(limit=3)}")
                self._drop_sim()
                time.sleep(1.0)
            time.sleep(0.002)
        self._drop_sim()

    def _drain_inbox_offline(self):
        # Keep network/GUI state machine alive while MSFS isn't connected.
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return
            self._on_inbox(item)

    def _try_connect(self):
        try:
            if flavor.is_dry():
                from .fakesim import FakeSim
                sc = FakeSim()
            else:
                sc = SimConnect(f"Shared Cockpit ({flavor.SIM})")
            sc.open()
        except (SimConnectError, OSError) as e:
            self.status["sim"] = "Waiting for MSFS..."
            if not getattr(self, "_warned_sim", False):
                self.log(f"MSFS not reachable yet ({e}). Retrying every 2 s.")
                self._warned_sim = True
            return False
        self._warned_sim = False
        self.sc = sc
        dry = flavor.is_dry()
        self.status["sim"] = "Dry run simulator running" if dry else f"Connected to {flavor.SIM}"
        self.status["ie"] = ("basic switches (dry run)" if dry else
                             "input events: available" if sc.has_input_events
                             else "input events: off - click 'Get SimConnect from Microsoft'")
        self.log(f"Connected to MSFS via {sc.dll_path}")
        sc.add_def(DEF_INFO, "TITLE", None, DT_STRING256)
        sc.add_def(DEF_INFO, "NUMBER OF ENGINES", "number", DT_FLOAT64)
        sc.add_def(DEF_INFO, "ATC MODEL", None, DT_STRING256)
        sc.subscribe_system(EV_AIRCRAFT_LOADED, "AircraftLoaded")
        sc.subscribe_system(EV_SIM_START, "SimStart")
        sc.subscribe_system(EV_FLIGHT_LOADED, "FlightLoaded")
        sc.request(REQ_INFO, DEF_INFO, PERIOD_ONCE)
        self._frozen = None  # unknown; force re-apply
        return True

    def _drop_sim(self):
        if self.sc:
            self.sc.close()
        self.sc = None
        self.ready = False
        self.status["sim"] = "Lost MSFS connection"

    # ---------------------------------------------------------------- data defs
    def _build(self):
        """(Re)create data definitions for the current aircraft."""
        sc = self.sc
        if self.ready:  # stop previous generation's requests
            base = self.gen * 100
            for r in (base + 1, base + 2, base + 3, base + 4):
                try:
                    sc.request(r, r, PERIOD_NEVER)
                except SimConnectError:
                    pass
        self.gen += 1
        base = self.gen * 100
        self.REQ_MOTION, self.REQ_CTRL, self.REQ_SW, self.REQ_FRZ = base + 1, base + 2, base + 3, base + 4
        self.motion = Block(sc, base + 1, sd.MOTION)
        self.ctrl = Block(sc, base + 2, sd.controls(self.engines))
        self.sw = Block(sc, base + 3, [(s[1], s[2]) for s in sd.SWITCHES])
        self.frz = Block(sc, base + 4, [("IS LATITUDE LONGITUDE FREEZE ON", "bool"),
                                        ("IS ALTITUDE FREEZE ON", "bool"),
                                        ("IS ATTITUDE FREEZE ON", "bool")])
        self._frz_state = None
        self.sw_set_blocks = {}
        did = base + 10
        for s in sd.SWITCHES:
            if s[3] == "set":
                self.sw_set_blocks[s[0]] = Block(sc, did, [(s[1], s[2])])
                did += 1
        sc.request(self.REQ_MOTION, base + 1, PERIOD_SIM_FRAME)
        sc.request(self.REQ_CTRL, base + 2, PERIOD_SIM_FRAME)
        sc.request(self.REQ_SW, base + 3, PERIOD_SIM_FRAME, FLAG_CHANGED, interval=2)
        sc.request(self.REQ_FRZ, base + 4, PERIOD_SECOND)
        self.sw_prev = {}
        self._latest_ctrl = None
        self.ready = True
        if sc.has_input_events:
            try:
                self.ie_by_hash, self.ie_by_name, self.ie_type = {}, {}, {}
                sc.subscribe_all_input_events()
                sc.enumerate_input_events(REQ_ENUM_IE)
            except (SimConnectError, OSError) as e:
                self.log(f"Input events unavailable: {e}")
                sc.has_input_events = False
        self._frozen = None

    # ---------------------------------------------------------------- sim messages
    def _on_sim(self, m):
        kind = m[0]
        if kind == "data":
            req, raw = m[1], m[2]
            if req == REQ_INFO:
                self.title = raw[:256].split(b"\0", 1)[0].decode("utf-8", "replace")
                self.engines = int(struct.unpack_from("<d", raw, 256)[0] + 0.5)
                # ATC MODEL identifies the aircraft type independent of livery
                # (in MSFS 2020 TITLE includes the livery name).
                self.model = raw[264:520].split(b"\0", 1)[0].decode("utf-8", "replace")
                self.status["aircraft"] = self.title
                self.log(f"Aircraft: {self.title} ({self.engines} engine(s))")
                self._build()
                self._send({"t": "title", "title": self.title, "model": self.model})
                self._check_titles()
            elif not self.ready:
                return
            elif req == self.REQ_MOTION:
                self._on_motion(self.motion.unpack(raw))
            elif req == self.REQ_CTRL:
                self._latest_ctrl = self.ctrl.unpack(raw)
            elif req == self.REQ_SW:
                self._on_switches(self.sw.unpack(raw))
            elif req == self.REQ_FRZ:
                self._frz_state = self.frz.unpack(raw)
        elif kind == "event":
            if m[1] in (EV_AIRCRAFT_LOADED, EV_FLIGHT_LOADED, EV_SIM_START):
                self.sc.request(REQ_INFO, DEF_INFO, PERIOD_ONCE)
        elif kind == "exception":
            self.exc_count += 1
            if self.exc_count <= 15:
                self.log(f"Sim rejected a request (exception {m[1]}, packet {m[2]}, index {m[3]})"
                         " - usually an event/variable this aircraft doesn't support.")
        elif kind == "quit":
            self.log("MSFS closed.")
            self._drop_sim()
        elif kind == "input_list":
            for name, h, et in m[1]:
                self.ie_by_hash[h] = name
                self.ie_by_name[name] = h
                self.ie_type[h] = et
            # Type codes differ between docs and reality (DOUBLE=0 or 1); nearly all
            # input events are numeric, so the most common code is the double one.
            codes = list(self.ie_type.values())
            self.ie_double = max(set(codes), key=codes.count) if codes else None
            self.status["ie"] = f"input events: {len(self.ie_by_name)} mapped"
        elif kind == "input_event":
            et = self.ie_type.get(m[1])
            if et is None or et == self.ie_double:
                self._on_local_input_event(m[1], m[2])

    def _check_titles(self):
        pt = self.peer.get("title")
        self.status["peer_aircraft"] = pt or ""
        if not (pt and self.title) or pt == self.title:
            return
        pm = self.peer.get("model")
        if pm and self.model and pm == self.model:
            self.log(f"Same aircraft type ({self.model}), different livery - that's fine.")
        else:
            self.log(f"WARNING: aircraft differ! You: '{self.title}'  Co-pilot: '{pt}'. "
                     "Load the exact same aircraft/livery for switches to line up.")

    # ---------------------------------------------------------------- motion
    def _on_motion(self, vals):
        t = net.now()
        if self._is_slave():
            # Our own sim frame ticked: place the frozen aircraft now, in step
            # with the sim's frame loop (less jitter than a free-running timer).
            self._last_frame = t
            self._last_apply = t
            self._apply_motion()
            return
        if not (self.link and self.pf == self.role):
            return
        if t - self._last_send < 1.0 / SEND_HZ:
            return
        self._last_send = t
        m = [round(v, 9) if i < 2 else round(v, 5) for i, v in enumerate(vals)]
        c = [round(v, 4) for v in self._latest_ctrl] if self._latest_ctrl else None
        self._send({"t": "mo", "ts": t, "m": m, "c": c})

    def _on_remote_motion(self, msg):
        t_local = net.now()
        sample = t_local - msg["ts"]
        self._offsets.append((t_local, sample))
        while self._offsets and t_local - self._offsets[0][0] > 3.0:
            self._offsets.popleft()
        self._offset = min(s for _, s in self._offsets)
        if self.snaps and msg["ts"] <= self.snaps[-1][0]:
            return
        self.snaps.append((msg["ts"], msg["m"], msg.get("c")))

    def _sample(self, rt):
        """Interpolated motion vector at sender-time rt."""
        s = self.snaps
        if rt <= s[0][0]:
            return list(s[0][1]), s[0][2]
        if len(s) == 1:
            return list(s[-1][1]), s[-1][2]
        if rt >= s[-1][0]:
            a, b = s[-2], s[-1]
            f = min(rt - b[0], MAX_EXTRAP) / max(b[0] - a[0], 1e-3)
            f = 1.0 + f
        else:
            i = len(s) - 1
            while s[i - 1][0] > rt:
                i -= 1
            a, b = s[i - 1], s[i]
            f = (rt - a[0]) / max(b[0] - a[0], 1e-6)
        out = []
        for k, (x, y) in enumerate(zip(a[1], b[1])):
            if k == sd.M_LON or k in sd.ANGLE_IDX:
                v = _lerp_angle(x, y, f)
                if k == sd.M_HDG:
                    v %= 360.0
                elif k != sd.M_PITCH:
                    v = _wrap180(v)
                out.append(v)
            else:
                out.append(x + (y - x) * f)
        return out, b[2]

    def _apply_motion(self):
        if not self.snaps or self._offset is None:
            return
        rt = net.now() - self._offset - DELAY
        vals, ctrl = self._sample(rt)
        self.motion.write(vals)
        if ctrl and len(ctrl) == len(self.ctrl.vars):
            self.ctrl.write(ctrl)
        while len(self.snaps) > 2 and self.snaps[1][0] < rt - 1.0:
            self.snaps.popleft()

    def _set_freeze(self, on):
        if not (self.sc and self.ready):
            return
        v = 1 if on else 0
        for ev in ("FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET"):
            self.sc.send_event(ev, v)
        self._frozen = on
        self._last_freeze = net.now()

    # ---------------------------------------------------------------- switches
    def _on_switches(self, vals):
        out = {}
        for s, v in zip(sd.SWITCHES, vals):
            key, unit = s[0], s[2]
            tol = sd.tolerance(unit)
            prev = self.sw_prev.get(key)
            self.sw_prev[key] = v
            if prev is None:
                self.sw_known.setdefault(key, v)
                continue
            if abs(v - prev) <= tol:
                continue                        # didn't change locally
            known = self.sw_known.get(key)
            if known is not None and abs(v - known) <= tol:
                continue                        # change was us applying the peer's value
            self.sw_known[key] = v
            out[key] = v
        if out:
            self._send({"t": "sw", "s": out})

    def _apply_switches(self, values, full=False):
        if not (self.sc and self.ready):
            return
        both_ie = self.sc.has_input_events and self.peer.get("ie")
        for key, v in values.items():
            s = self.sw_items.get(key)
            if not s:
                continue
            _, var, unit, kind, arg, scale = s
            self.sw_known[key] = v
            cur = self.sw_prev.get(key)
            if cur is not None and abs(cur - v) <= sd.tolerance(unit):
                continue
            try:
                if kind == "set":
                    self.sw_set_blocks[key].write([v])
                elif kind == "event":
                    self.sc.send_event(arg, round(v * scale))
                elif kind == "onoff":
                    self.sc.send_event(arg[0] if v >= 0.5 else arg[1])
                elif kind == "toggle":
                    if both_ie:
                        continue
                    if cur is None or (cur >= 0.5) != (v >= 0.5):
                        self.sc.send_event(arg)
            except SimConnectError as e:
                self.log(f"Could not apply {key}: {e}")

    # ---------------------------------------------------------------- input events
    def _on_local_input_event(self, h, v):
        if not (self.link and self.peer.get("ie")):
            return
        name = self.ie_by_hash.get(h)
        if not name or any(k in name.upper() for k in sd.INPUT_EVENT_SKIP):
            return
        a = self.ie_applied.get(name)
        if a is not None and abs(a - v) < 1e-6:
            del self.ie_applied[name]          # echo of the peer's change
            return
        self._send({"t": "ie", "n": name, "v": v})

    def _apply_input_event(self, name, v):
        if not (self.sc and self.sc.has_input_events):
            return
        h = self.ie_by_name.get(name)
        if h is None:
            return
        self.ie_applied[name] = v
        try:
            self.sc.set_input_event(h, v)
        except SimConnectError as e:
            self.log(f"Input event {name} failed: {e}")

    # ---------------------------------------------------------------- control ownership
    def _set_pf(self, pf, announce=False):
        self.pf = pf
        self.snaps.clear()
        self._offsets.clear()
        self._offset = None
        if announce:
            self._send({"t": "ctl", "e": self.ctl_epoch, "pf": pf})
        me = self.i_have_controls()
        self.status["pf"] = "YOU have the controls" if me else "CO-PILOT has the controls"
        if self.link:
            self.log("You have the controls." if me else "Co-pilot has the controls - hands off, "
                     "your aircraft now follows theirs.")
        self._set_freeze(not me)

    # ---------------------------------------------------------------- inbox
    def _on_inbox(self, item):
        if item[0] == "cmd":
            c = item[1]
            if c == "set_pf":
                self._set_pf(item[2])
            elif c == "take":
                if self.link and self.pf != self.role:
                    self.ctl_epoch += 1
                    self._set_pf(self.role, announce=True)
            elif c == "give":
                if self.link and self.pf == self.role:
                    self.ctl_epoch += 1
                    self._set_pf("join" if self.role == "host" else "host", announce=True)
            elif c == "disconnected":
                self._set_pf(self.role or "host")
            return
        msg = item[1]
        t = msg.get("t")
        if t == "mo":
            if self.pf != self.role:
                self._on_remote_motion(msg)
        elif t == "sw":
            self._apply_switches(msg["s"])
        elif t == "swfull":
            self.log(f"Received full cockpit state ({len(msg['s'])} items).")
            self._apply_switches(msg["s"], full=True)
        elif t == "ie":
            self._apply_input_event(msg["n"], msg["v"])
        elif t == "ctl":
            e = msg["e"]
            if e > self.ctl_epoch or (e == self.ctl_epoch and self.role == "join"):
                self.ctl_epoch = e
                self._set_pf(msg["pf"])
        elif t == "hello":
            self.peer = msg
            self.log(f"Co-pilot '{msg.get('name')}' says hello (aircraft: {msg.get('title') or '?'}).")
            if msg.get("sim") and msg["sim"] != flavor.SIM:
                self.log(f"Note: co-pilot is on {msg['sim']}, you're on {flavor.SIM}. Cross-version "
                         "flying is untested; aircraft names won't match and some switches may not sync.")
            if msg.get("v") != PROTO:
                self.log("WARNING: co-pilot runs a different Shared Cockpit version.")
            self._check_titles()
            if self.role == "host":
                # Host is authoritative on connect: who flies + full switch state.
                self._send({"t": "ctl", "e": self.ctl_epoch, "pf": self.pf or "host"})
                self._send({"t": "swfull", "s": dict(self.sw_prev)})
        elif t == "title":
            self.peer["title"] = msg["title"]
            self.peer["model"] = msg.get("model", "")
            self._check_titles()
            if self.role == "host":
                self._send({"t": "swfull", "s": dict(self.sw_prev)})
        elif t == "ping":
            self._send({"t": "pong", "x": msg["x"]})
        elif t == "pong":
            self.rtt = (net.now() - msg["x"]) * 1000.0
            self.status["rtt"] = f"{self.rtt:.0f} ms"

    # ---------------------------------------------------------------- periodic
    def _is_slave(self):
        return self.link is not None and self.pf is not None and self.pf != self.role

    def _tick(self):
        t = net.now()
        if self.link and t - self._last_ping > 2.0:
            self._last_ping = t
            self._send({"t": "ping", "x": t})
        if not self.ready:
            return
        slave = self._is_slave()
        fs = self._frz_state
        if fs is not None:
            # Sim reports actual freeze flags: re-assert if they drifted.
            drift = any((v >= 0.5) != slave for v in fs)
        else:
            drift = slave and t - self._last_freeze > 3.0
        if self._frozen is None or (self._frozen and not slave) or                 (drift and t - self._last_freeze > 1.0):
            self._set_freeze(slave)
            self._frz_state = None
        # Fallback placement if our own sim frames aren't arriving (e.g. paused).
        if slave and t - self._last_frame > 0.1 and t - self._last_apply >= 1.0 / APPLY_HZ:
            self._last_apply = t
            self._apply_motion()
