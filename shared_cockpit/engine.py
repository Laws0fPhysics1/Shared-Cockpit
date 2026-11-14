"""Shared-cockpit sync engine.

Threads:
  sim thread   - owns SimConnect (all SimConnect calls happen here)
  net readers  - TCP/UDP readers push received messages into self.inbox
  GUI thread   - pushes commands into self.inbox, reads self.status

Model: the pilot flying (PF) runs the real flight model. The other seat's
aircraft is frozen and placed every frame from PF snapshots. Snapshots are
rendered slightly in the past (an adaptive jitter buffer) and interpolated
along a curve built from the PF's real velocity (cubic Hermite), so network
jitter and lost packets don't show.
"""
import math
import queue
import secrets
import socket
import struct
import threading
import time
from collections import deque

from . import flavor, net, profiles, syncdefs as sd
from .simconnect import (Block, SimConnect, SimConnectError, PERIOD_NEVER, PERIOD_ONCE,
                         PERIOD_SIM_FRAME, PERIOD_SECOND, FLAG_CHANGED, DT_STRING256,
                         DT_FLOAT64)

PROTO = 4               # 4: aircraft-profile presses ("lp") and knobs ("kn")
SEND_HZ = 40.0          # PF motion packets per second
BUF_START, BUF_MIN, BUF_MAX = 0.12, 0.035, 0.60   # total playback delay behind the PF (s)
BUF_SLEW = 0.08         # max change of the shown delay per second (8% playback speed)
MAX_EXTRAP = 0.30       # max prediction past the newest snapshot (s)
APPLY_HZ = 60.0         # fallback placement rate when own sim frames aren't arriving
HANDOFF_RAMP = 2.0      # s to ease from "live" to buffered playback after giving controls
CATCHUP = 1.2           # s the taker spends fast-forwarding to "live" before taking over
GROUND_TOP, GROUND_FULL = 100.0, 40.0   # ft AGL: blend to local-terrain altitude below these
FT_PER_DEG = 364566.0   # feet per degree of latitude

EV_AIRCRAFT_LOADED, EV_SIM_START, EV_FLIGHT_LOADED = 1, 2, 3
DEF_INFO, REQ_INFO, REQ_ENUM_IE = 1, 1, 2


def _wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def _lerp_angle(a, b, f):
    return a + _wrap180(b - a) * f


def _hermite(p0, m0, p1, m1, h, f):
    f2, f3 = f * f, f * f * f
    return ((2 * f3 - 3 * f2 + 1) * p0 + (f3 - 2 * f2 + f) * h * m0 +
            (-2 * f3 + 3 * f2) * p1 + (f3 - f2) * h * m1)


class Snap:
    __slots__ = ("ts", "m", "c", "v", "g")

    def __init__(self, ts, m, c, v, g):
        self.ts, self.m, self.c, self.v, self.g = ts, m, c, v, g

    def rates(self):
        """d(lat)/dt, d(lon)/dt in deg/s and d(alt)/dt in ft/s, or None."""
        if not self.v:
            return None
        ve, vu, vn = self.v
        coslat = max(math.cos(math.radians(self.m[sd.M_LAT])), 1e-6)
        return vn / FT_PER_DEG, ve / (FT_PER_DEG * coslat), vu


class Engine:
    PROTO = PROTO

    def __init__(self, log, name="Pilot", bridge_port=None):
        self.log = log
        self.name = name
        self.inbox = queue.Queue()
        self.running = True

        self.sc = None
        self.ready = False
        self.gen = 0                 # data-definition generation (rebuilt per aircraft)
        self.title = self.model = ""
        self.engines = 1
        self.payload_n = 0

        self.role = None             # "host" | "join"
        self.link = None
        self.hostsrv = None
        self.peer = {}
        self.pf = None               # "host" | "join": who has the controls
        self.ctl_epoch = 0
        self._frozen = False
        self._last_freeze = 0.0
        self._frz_state = None

        # UDP motion channel
        self.udp = None
        self.udp_peer = None         # where to send UDP
        self.udp_token = secrets.token_hex(6)   # peers must echo this to be accepted
        self.udp_token_peer = None
        self._udp_last_rx = 0.0
        self._udp_tx_ok = False
        self._last_uh = self._last_uok = 0.0
        self._conn_t = 0.0
        self._udp_hinted = False

        # timing / interpolation
        self.snaps = deque(maxlen=160)
        self._offsets = deque()      # (t_local, local_recv - remote_send): fallback clock estimate
        self._offset = None
        self._clk = deque()          # (t_local, rtt, clock difference) from ping/pong, NTP-style
        self._ages = deque()         # (t_local, age of each motion packet on arrival)
        self.buf = BUF_START
        self._buf_shown, self._buf_t = BUF_START, 0.0
        self.rtt = None
        self._ramp_t0 = None
        self._catchup_t0 = None
        self._latest_motion = None
        self.last_rt = None
        self._last_send = self._last_apply = self._last_frame = self._last_ping = 0.0

        self._latest_ctrl = self._latest_aux = self._env = None
        self.peer_env = None
        self._last_env_send = 0.0
        self._warned = {}
        self.env_issues = []
        self.load_blocks = {}

        self.sw_prev = {}            # last local reading per switch
        self.sw_known = {}           # last agreed value per switch
        self.sw_items = {s[0]: s for s in sd.SWITCHES}
        self.sw_set_blocks = {}

        self.ie_by_hash, self.ie_by_name, self.ie_applied = {}, {}, {}
        self.ie_last, self.ie_recent, self.ie_secs = {}, {}, {}
        self.ie_action, self.ie_noisy = set(), set()
        self.sw_recent, self.sw_pending = {}, {}
        self.sw_secs, self.sw_noisy = {}, set()
        self.last_click = 0.0                   # last local cockpit interaction (input event / button)
        self.ie_type, self.ie_double = {}, None
        self.exc_count = 0

        self.status = {"sim": "Not connected", "net": "Idle", "pf": "", "aircraft": "",
                       "peer_aircraft": "", "rtt": "", "ie": "", "motion": "", "env": "",
                       "bridge": ""}
        from .cockpit import CockpitSync
        self.cp = CockpitSync(self, bridge_port)      # buttons/FMS, L:vars, match, flight copy
        threading.Thread(target=self._sim_loop, daemon=True).start()

    # ================================================================ public (GUI thread)
    def host(self, port):
        self.stop_net()
        self.role = "host"
        self.hostsrv = net.Host(port, self._on_sock, self.log)
        try:
            self.udp = net.Udp(port, self._on_udp)
        except OSError as e:
            self.udp = None
            self.log(f"UDP port {port} unavailable ({e}); motion will use TCP.")
        self.status["net"] = f"Hosting on port {port}, waiting for co-pilot..."
        self.log(f"Hosting on port {port} (TCP + UDP). Your friend joins with your public IP and this port.")
        self.inbox.put(("cmd", "set_pf", "host"))

    def join(self, ip, port):
        self.stop_net()
        self.role = "join"
        self.status["net"] = f"Connecting to {ip}:{port}..."

        def go():
            try:
                s = net.connect(ip, port)
                addr = socket.gethostbyname(ip)
            except (OSError, socket.timeout) as e:
                self.status["net"] = "Connect failed"
                self.log(f"Could not connect to {ip}:{port}: {e}")
                return
            try:
                self.udp = net.Udp(0, self._on_udp)
                self.udp_peer = (addr, port)
            except OSError as e:
                self.log(f"UDP unavailable ({e}); motion will use TCP.")
            self.log(f"Connected to {ip}:{port}")
            self._on_sock(s)
        threading.Thread(target=go, daemon=True).start()

    def stop_net(self):
        if self.link:
            self.link.close("you disconnected")
        if self.hostsrv:
            self.hostsrv.stop()
            self.hostsrv = None
        if self.udp:
            self.udp.close()
            self.udp = None
        self.role = None
        self.status["net"] = "Idle"

    def take_controls(self):
        self.inbox.put(("cmd", "take"))

    def give_controls(self):
        self.inbox.put(("cmd", "give"))

    def match_copilot(self):
        self.inbox.put(("cmd", "match"))

    def copy_copilot_flight(self):
        self.inbox.put(("cmd", "flight"))

    def shutdown(self):
        self.running = False
        self.stop_net()
        self.cp.close()

    def i_have_controls(self):
        return self.link is None or self.pf == self.role

    # ================================================================ network glue
    def _on_sock(self, sock):
        if self.hostsrv:
            self.hostsrv.busy = True
        self.udp_token = secrets.token_hex(6)
        self.udp_token_peer = None
        self._udp_tx_ok = False
        self._udp_last_rx = 0.0
        self._udp_hinted = False
        self._conn_t = net.now()
        self.link = net.Link(sock, lambda m: self.inbox.put(("net", m)), self._on_close)
        self.status["net"] = "Connected"
        self._send({"t": "hello", "v": PROTO, "name": self.name, "title": self.title,
                    "model": self.model, "sim": flavor.SIM, "role": self.role,
                    "uk": self.udp_token, "ie": bool(self.sc and self.sc.has_input_events)})

    def _on_close(self, reason):
        self.log(f"Link closed: {reason}")
        self.link = None
        if self.hostsrv:
            self.hostsrv.busy = False
            self.udp_peer = None           # next joiner announces its own address
            self.status["net"] = "Co-pilot left. Waiting for co-pilot..."
        else:
            self.status["net"] = "Disconnected"
            if self.udp:
                self.udp.close()
                self.udp = None
        self._udp_tx_ok = False
        self.status["motion"] = ""
        self.peer = {}
        self.cp.on_disconnect()
        self.inbox.put(("cmd", "disconnected"))

    def _on_udp(self, msg, addr):
        self.inbox.put(("udp", msg, addr))

    def _send(self, msg):
        if self.link:
            self.link.send(msg)

    def _send_motion(self, msg):
        if self.udp and self.udp_peer and self._udp_tx_ok and self.udp_token_peer:
            msg["k"] = self.udp_token_peer
            self.udp.send(msg, self.udp_peer)
        else:
            self._send(msg)

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
                break
            self._on_inbox(item)
        self._net_tick(net.now())

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
        sc.add_def(DEF_INFO, "PAYLOAD STATION COUNT", "number", DT_FLOAT64)
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
            for r in range(base + 1, base + 7):
                try:
                    sc.request(r, r, PERIOD_NEVER)
                except SimConnectError:
                    pass
        self.gen += 1
        base = self.gen * 100
        (self.REQ_MOTION, self.REQ_CTRL, self.REQ_SW, self.REQ_FRZ, self.REQ_AUX,
         self.REQ_ENV) = range(base + 1, base + 7)
        self.motion = Block(sc, base + 1, sd.MOTION)
        self.ctrl = Block(sc, base + 2, sd.controls(self.engines))
        self.sw = Block(sc, base + 3, [(s[1], s[2]) for s in sd.SWITCHES])
        self.frz = Block(sc, base + 4, [("IS LATITUDE LONGITUDE FREEZE ON", "bool"),
                                        ("IS ALTITUDE FREEZE ON", "bool"),
                                        ("IS ATTITUDE FREEZE ON", "bool")])
        self.aux = Block(sc, base + 5, sd.AUX)
        self.load_vars = sd.load_vars(self.payload_n)
        self.env = Block(sc, base + 6, sd.ENV + self.load_vars)
        self.load_blocks = {}
        self._load_did = base + 60
        self._frz_state = self._latest_aux = self._env = None
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
        sc.request(self.REQ_AUX, base + 5, PERIOD_SIM_FRAME)
        sc.request(self.REQ_ENV, base + 6, PERIOD_SECOND)
        self.sw_prev = {}
        self._latest_ctrl = None
        self.ready = True
        self.cp.on_build(base)
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
        if kind in ("client_data", "input_value"):
            self.cp.on_sim(m)
            return
        if kind == "data":
            req, raw = m[1], m[2]
            if req == REQ_INFO:
                self.title = raw[:256].split(b"\0", 1)[0].decode("utf-8", "replace")
                self.engines = int(struct.unpack_from("<d", raw, 256)[0] + 0.5)
                # ATC MODEL identifies the aircraft type independent of livery
                # (in MSFS 2020 TITLE includes the livery name).
                self.model = raw[264:520].split(b"\0", 1)[0].decode("utf-8", "replace")
                self.payload_n = int(struct.unpack_from("<d", raw, 520)[0] + 0.5) \
                    if len(raw) >= 528 else 0
                self.status["aircraft"] = self.title
                self.log(f"Aircraft: {self.title} ({self.engines} engine(s))")
                self._build()
                self._send({"t": "title", "title": self.title, "model": self.model})
                self._check_titles()
            elif not self.ready:
                return
            elif req == self.REQ_MOTION:
                self._on_motion(self.motion.unpack(raw))
            elif req == self.REQ_AUX:
                self._latest_aux = self.aux.unpack(raw)
            elif req == self.REQ_CTRL:
                self._latest_ctrl = self.ctrl.unpack(raw)
            elif req == self.REQ_SW:
                self._on_switches(self.sw.unpack(raw))
            elif req == self.REQ_FRZ:
                self._frz_state = self.frz.unpack(raw)
            elif req == self.REQ_ENV:
                self._env = self.env.unpack(raw)
            else:
                self.cp.on_data(req, raw)
        elif kind == "event":
            if m[1] in (EV_AIRCRAFT_LOADED, EV_FLIGHT_LOADED, EV_SIM_START):
                self.sc.request(REQ_INFO, DEF_INFO, PERIOD_ONCE)
            else:
                self.cp.on_k(m[1], m[2])
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
            self.cp.request_ie_snapshot()
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

    # ---------------------------------------------------------------- clock / buffer
    def _add_offset_sample(self, t_local, sample):
        self._offsets.append((t_local, sample))
        while self._offsets and t_local - self._offsets[0][0] > 5.0:
            self._offsets.popleft()
        self._offset = min(s for _, s in self._offsets)

    def _clock_diff(self):
        """Our clock minus the co-pilot's clock (s), or None if unknown yet.

        Best: the ping/pong sample with the lowest round trip in the last 10 s
        (NTP midpoint method, accurate to ~half the round-trip asymmetry).
        Fallback before the first pong: fastest motion packet (assumes ~0 latency)."""
        if self._clk:
            return min(self._clk, key=lambda c: c[1])[2]
        return self._offset

    def _remote_now(self):
        d = self._clock_diff()
        return None if d is None else net.now() - d

    def _update_buffer(self, t_local, ts):
        """Adaptive playback delay: cover the 95th-percentile packet age + send spacing."""
        d = self._clock_diff()
        if d is None:
            return
        self._ages.append((t_local, t_local - d - ts))
        while self._ages and t_local - self._ages[0][0] > 5.0:
            self._ages.popleft()
        ages = sorted(a for _, a in self._ages)
        p95 = ages[min(len(ages) - 1, int(len(ages) * 0.95))]
        target = min(max(1.25 / SEND_HZ + p95 + 0.005, BUF_MIN), BUF_MAX)
        # grow fast when the link gets worse, shrink slowly when it improves
        self.buf += (target - self.buf) * (0.25 if target > self.buf else 0.01)
        self.status["motion"] = self._motion_status()

    def _motion_status(self):
        path = "UDP" if self._udp_tx_ok else "TCP"
        rx = "UDP" if net.now() - self._udp_last_rx < 2.5 else "TCP"
        return f"send {path} / receive {rx}, showing co-pilot {self.buf * 1000:.0f} ms behind"

    # ---------------------------------------------------------------- motion
    def _on_motion(self, vals):
        t = net.now()
        self._latest_motion, self._latest_motion_t = vals, t
        if self._is_slave():
            # Our own sim frame ticked: place the frozen aircraft now, in step
            # with the sim's frame loop (less jitter than a free-running timer).
            self._last_frame = self._last_apply = t
            if self._catchup_t0 is not None and t - self._catchup_t0 >= CATCHUP:
                # Take over exactly on a sim frame, so the sim integrates on from the
                # position we hand it rather than from a mid-frame write.
                self._finish_take()
                return
            self._apply_motion()
            return
        if not (self.link and self.pf == self.role):
            return
        if t - self._last_send < 1.0 / SEND_HZ:
            return
        self._last_send = t
        msg = {"t": "mo", "ts": t,
               "m": [round(v, 9) if i < 2 else round(v, 5) for i, v in enumerate(vals)],
               "c": [round(v, 4) for v in self._latest_ctrl] if self._latest_ctrl else None}
        a = self._latest_aux
        if a:
            msg["v"] = [round(a[sd.A_VE], 3), round(a[sd.A_VU], 3), round(a[sd.A_VN], 3)]
            msg["g"] = round(a[sd.A_GROUND], 2)
        self._send_motion(msg)

    def _on_remote_motion(self, msg):
        t_local = net.now()
        sample = t_local - msg["ts"]
        self._add_offset_sample(t_local, sample)
        self._update_buffer(t_local, msg["ts"])
        if self.snaps and msg["ts"] <= self.snaps[-1].ts:
            return                                   # late/duplicate UDP packet
        self.snaps.append(Snap(msg["ts"], msg["m"], msg.get("c"), msg.get("v"), msg.get("g")))

    def _sample(self, rt):
        """Motion vector, controls and remote ground elevation at sender-time rt."""
        s = self.snaps
        first, last = s[0], s[-1]
        if rt <= first.ts:
            return list(first.m), first.c, first.g
        if rt >= last.ts:                            # predict past the newest snapshot
            dt = min(rt - last.ts, MAX_EXTRAP)
            out = list(last.m)
            r = last.rates()
            prev = s[-2] if len(s) > 1 else None
            if r:
                out[sd.M_LAT] += r[0] * dt
                out[sd.M_LON] = _wrap180(out[sd.M_LON] + r[1] * dt)
                out[sd.M_ALT] += r[2] * dt
            elif prev:
                h = max(last.ts - prev.ts, 1e-3)
                for k in (sd.M_LAT, sd.M_ALT):
                    out[k] += (last.m[k] - prev.m[k]) / h * dt
                out[sd.M_LON] = _wrap180(out[sd.M_LON] + _wrap180(last.m[sd.M_LON] - prev.m[sd.M_LON]) / h * dt)
            if prev:
                h = max(last.ts - prev.ts, 1e-3)
                for k in sd.ANGLE_IDX:
                    out[k] = out[k] + _wrap180(last.m[k] - prev.m[k]) / h * dt
                out[sd.M_HDG] %= 360.0
                out[sd.M_BANK] = _wrap180(out[sd.M_BANK])
            return out, last.c, last.g
        i = len(s) - 1
        while s[i - 1].ts > rt:
            i -= 1
        a, b = s[i - 1], s[i]
        h = max(b.ts - a.ts, 1e-6)
        f = (rt - a.ts) / h
        out = []
        for k, (x, y) in enumerate(zip(a.m, b.m)):
            if k == sd.M_LON or k in sd.ANGLE_IDX:
                v = _lerp_angle(x, y, f)
                if k == sd.M_HDG:
                    v %= 360.0
                elif k != sd.M_PITCH:
                    v = _wrap180(v)
                out.append(v)
            else:
                out.append(x + (y - x) * f)
        ra, rb = a.rates(), b.rates()
        if ra and rb:                                # curved path through both snapshots
            out[sd.M_LAT] = _hermite(a.m[sd.M_LAT], ra[0], b.m[sd.M_LAT], rb[0], h, f)
            lon_b = a.m[sd.M_LON] + _wrap180(b.m[sd.M_LON] - a.m[sd.M_LON])
            out[sd.M_LON] = _wrap180(_hermite(a.m[sd.M_LON], ra[1], lon_b, rb[1], h, f))
            out[sd.M_ALT] = _hermite(a.m[sd.M_ALT], ra[2], b.m[sd.M_ALT], rb[2], h, f)
        g = a.g + (b.g - a.g) * f if a.g is not None and b.g is not None else b.g
        return out, b.c, g

    def _render_time(self):
        """Co-pilot-clock time to show: buffered, or easing to/from 'live' around a handoff."""
        # The shown delay follows the adaptive buffer at most BUF_SLEW s per s, so a
        # buffer change never shows as the plane visibly slowing down or speeding up.
        t = net.now()
        dt = min(t - self._buf_t, 0.1) if self._buf_t else 0.0
        self._buf_t = t
        step = BUF_SLEW * dt
        self._buf_shown += max(-step, min(step, self.buf - self._buf_shown))
        delay = self._buf_shown
        if self._catchup_t0 is not None:          # taking over: ease to live
            delay *= 1.0 - min((net.now() - self._catchup_t0) / CATCHUP, 1.0)
        elif self._ramp_t0 is not None:           # just gave controls: ease from live
            k = (net.now() - self._ramp_t0) / HANDOFF_RAMP
            if k >= 1.0:
                self._ramp_t0 = None
            else:
                delay *= k
        return self._remote_now() - delay

    def _apply_motion(self):
        if not self.snaps or self._clock_diff() is None:
            return
        rt = self._render_time()
        self.last_rt = rt
        vals, ctrl, g_remote = self._sample(rt)
        self._ground_blend(vals, g_remote)
        self.motion.write(vals)
        if ctrl and len(ctrl) == len(self.ctrl.vars):
            self.ctrl.write(ctrl)
        while len(self.snaps) > 3 and self.snaps[1].ts < rt - 1.0:
            self.snaps.popleft()

    def _ground_blend(self, vals, g_remote):
        """Near the ground, keep the remote height above ground on OUR terrain,
        so differing scenery elevations can't sink or float the wheels."""
        a = self._latest_aux
        if g_remote is None or not a:
            return
        agl = vals[sd.M_ALT] - g_remote
        if agl >= GROUND_TOP:
            return
        w = min(max((GROUND_TOP - agl) / (GROUND_TOP - GROUND_FULL), 0.0), 1.0)
        vals[sd.M_ALT] = vals[sd.M_ALT] * (1 - w) + (a[sd.A_GROUND] + agl) * w

    def _set_freeze(self, on):
        if not (self.sc and self.ready):
            return
        v = 1 if on else 0
        for ev in ("FREEZE_LATITUDE_LONGITUDE_SET", "FREEZE_ALTITUDE_SET", "FREEZE_ATTITUDE_SET"):
            self.sc.send_event(ev, v)
        self._frozen = on
        self._last_freeze = net.now()

    # ---------------------------------------------------------------- fuel / weather
    def _env_message(self):
        e = self._env
        n = len(sd.ENV)
        return {"t": "env", "w": [round(e[0], 1), round(e[1], 1)], "p": round(e[2], 2),
                "r": round(e[3], 3), "x": [round(e[sd.E_TEMP], 1), round(e[sd.E_VIS]),
                                           int(e[sd.E_PRECIP])],
                "l": [round(x, 3) for x in e[n:]]}

    def _check_env(self):
        e, p = self._env, self.peer_env
        if not (e and p):
            return
        issues = []
        wd, wv = e[0], e[1]
        pd, pv = p["w"]
        if abs(wv - pv) > 8 or (max(wv, pv) > 5 and abs(_wrap180(wd - pd)) > 30):
            issues.append(f"wind (you {wd:03.0f}/{wv:.0f} kt, co-pilot {pd:03.0f}/{pv:.0f} kt)")
        if abs(e[2] - p["p"]) > 2.0:
            issues.append(f"pressure (you {e[2]:.0f} hPa, co-pilot {p['p']:.0f} hPa)")
        if abs(e[3] - p["r"]) > 0.01:
            issues.append(f"sim rate (you {e[3]:g}x, co-pilot {p['r']:g}x)")
        if p.get("x"):
            pt, pvis, ppr = p["x"]
            if abs(e[sd.E_TEMP] - pt) > 3.0:
                issues.append(f"temperature (you {e[sd.E_TEMP]:.0f} C, co-pilot {pt:.0f} C)")
            vis, lo = e[sd.E_VIS], min(e[sd.E_VIS], pvis)
            if lo < 15000 and abs(vis - pvis) > 0.3 * max(vis, pvis, 1.0):
                issues.append(f"visibility (you {vis / 1000:.1f} km, co-pilot {pvis / 1000:.1f} km)")
            names = {2: "none", 4: "rain", 8: "snow"}
            if int(e[sd.E_PRECIP]) != ppr:
                issues.append(f"precipitation (you {names.get(int(e[sd.E_PRECIP]), '?')}, "
                              f"co-pilot {names.get(ppr, '?')})")
        self.env_issues = issues
        self.status["env"] = "matched" if not issues else "differs: " + ", ".join(i.split(" (")[0] for i in issues)
        t = net.now()
        for i in issues:
            key = i.split(" (")[0]
            if t - self._warned.get(key, -1e9) > 60:
                self._warned[key] = t
                self.log(f"Weather/sim differs from co-pilot: {i}. Pick the same weather preset "
                         "(or both Live Weather) and sim rate for smooth control handoffs. MSFS doesn't let apps set the weather.")

    def _apply_peer_load(self):
        """When taking the controls, copy the previous pilot's fuel & payload."""
        p, e = self.peer_env, self._env
        if not (p and e and self.ready) or len(p["l"]) != len(self.load_vars):
            return
        n = len(sd.ENV)
        changed = 0
        for i, ((var, unit), remote) in enumerate(zip(self.load_vars, p["l"])):
            local = e[n + i]
            if abs(remote - local) < 0.05 or (remote < 0.01 and local < 0.01):
                continue
            blk = self.load_blocks.get(var)
            if blk is None:
                blk = self.load_blocks[var] = Block(self.sc, self._load_did, [(var, unit)])
                self._load_did += 1
            blk.write([remote])
            changed += 1
        if changed:
            self.log(f"Copied fuel/payload from co-pilot ({changed} values).")

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
            if key in self.cp.sw_off:
                continue                        # the aircraft profile turned it off
            if key in self.sw_noisy or (key in sd.AP_MODE_KEYS and self.cp.screen_ap):
                continue
            known = self.sw_known.get(key)
            if known is not None and abs(v - known) <= tol:
                continue                        # change was us applying the peer's value
            t = net.now()
            rec = self.sw_recent.get(key)
            if rec and any(abs(v - rv) <= tol and t - rt < sd.ECHO_S for rt, rv in rec):
                # Back on a value the co-pilot sent: nothing of ours to send, and any
                # value still waiting to settle is stale now.
                self.sw_known[key] = v
                self.sw_pending.pop(key, None)
                continue
            dq = self.sw_secs.setdefault(key, deque())
            sec = int(t)
            if not dq or dq[-1] != sec:
                dq.append(sec)
            while dq and dq[0] <= sec - 10:
                dq.popleft()
            if len(dq) >= 8 and key not in sd.SETTLE_KEYS:
                # e.g. pitot heat on an aircraft that switches it automatically
                self.sw_noisy.add(key)
                self.cp.slog.add("skip", "switch", key, v, "changes on its own - not synced this session")
                continue
            self.sw_known[key] = v
            if key in sd.SETTLE_KEYS:
                self.sw_pending[key] = (v, t)   # sent once the knob stops moving
            else:
                out[key] = v
        if out:
            self._send({"t": "sw", "s": out})
            for k, v in out.items():
                self.cp.slog.add("out", "switch", k, v)

    def _sw_state(self):
        """Every switch reading, without the ones the aircraft profile turned off."""
        return {k: v for k, v in self.sw_prev.items() if k not in self.cp.sw_off}

    def _apply_switches(self, values, full=False):
        if not (self.sc and self.ready):
            return
        both_ie = self.sc.has_input_events and self.peer.get("ie")
        for key, v in values.items():
            s = self.sw_items.get(key)
            if not s:
                continue
            _, var, unit, kind, arg, scale = s
            if key in self.sw_noisy or (key in sd.AP_MODE_KEYS and self.cp.screen_ap):
                continue
            if key in self.cp.sw_off or profiles.switch_off(self.cp.profile, key):
                self.cp.slog.add("skip", "switch", key, v, "off for this aircraft (profile)")
                continue
            self.sw_known[key] = v
            self.sw_pending.pop(key, None)          # the co-pilot's newer value wins
            self.sw_recent.setdefault(key, deque(maxlen=12)).append((net.now(), v))
            cur = self.sw_prev.get(key)
            if cur is not None and abs(cur - v) <= sd.tolerance(unit):
                continue
            self.cp.slog.add("apply", "switch", key, v, "full state" if full else "")
            try:
                if kind == "set":
                    self.sw_set_blocks[key].write([v])
                elif kind == "event":
                    self.cp.note_sent(arg, round(v * scale))
                    self.sc.send_event(arg, round(v * scale))
                elif kind == "onoff":
                    self.cp.note_sent(arg[0] if v >= 0.5 else arg[1])
                    self.sc.send_event(arg[0] if v >= 0.5 else arg[1])
                elif kind == "slot":
                    self._apply_slot(var, arg, v)
                elif kind == "toggle":
                    if both_ie:
                        continue
                    if cur is None or (cur >= 0.5) != (v >= 0.5):
                        self.cp.note_sent(arg)
                        self.sc.send_event(arg)
            except SimConnectError as e:
                self.log(f"Could not apply {key}: {e}")
                self.cp.slog.add("fail", "switch", key, v, str(e))

    def _apply_slot(self, var, arg, v):
        """Autopilot selector in a given slot, written the way the knob's own model
        code does it: "slot value (>K:2:EVENT)" (needs MobiFlight's module)."""
        ev, slot = arg
        mf = self.cp.mf
        if hasattr(self.sc, "set_var"):              # dry-run simulator
            self.sc.set_var(var, v)
        elif mf is not None and getattr(mf, "ready", False):
            mf.execute(f"{slot} {round(v)} (>K:2:{ev})")
        elif slot == 1:                              # no MobiFlight: one-parameter fallback
            self.cp.note_sent(ev, round(v))
            self.sc.send_event(ev, round(v))
        else:
            self.cp.slog.add("skip", "switch", var, v, "needs the MobiFlight module")

    # ---------------------------------------------------------------- input events
    def _on_local_input_event(self, h, v):
        self.last_click = net.now()              # any cockpit control the pilot touched
        if not (self.link and self.peer.get("ie")):
            return
        name = self.ie_by_hash.get(h)
        if not name:
            return
        prev = self.ie_last.get(name)
        self.ie_last[name] = v
        if any(k in name.upper() for k in sd.INPUT_EVENT_SKIP):
            return
        if profiles.input_event_off(self.cp.profile, name):
            return
        if name in self.ie_action or name in self.ie_noisy:
            return
        if prev is not None and abs(v - prev) < 1e-9:
            # Fired again with the same value: an action (key press, knob turn), not a
            # switch position. Copying it would re-fire the action on the other sim.
            self.ie_action.add(name)
            self.cp.slog.add("skip", "input", name, v, "action event (not a position): not mirrored")
            return
        t = net.now()
        rec = self.ie_recent.get(name)
        if rec and any(abs(v - rv) < 1e-6 and t - rt < sd.ECHO_S for rt, rv in rec):
            return                              # echo / late reading of the co-pilot's value
        dq = self.ie_secs.setdefault(name, deque())
        sec = int(t)
        if not dq or dq[-1] != sec:
            dq.append(sec)
        while dq and dq[0] <= sec - 10:
            dq.popleft()
        if len(dq) >= 8:
            self.ie_noisy.add(name)
            self.cp.slog.add("skip", "input", name, v, "changes on its own - ignored this session")
            return
        self._send({"t": "ie", "n": name, "v": v})
        self.cp.slog.add("out", "input", name, v)

    def _apply_input_event(self, name, v):
        if not (self.sc and self.sc.has_input_events):
            return
        h = self.ie_by_name.get(name)
        if h is None:
            return
        if profiles.input_event_off(self.cp.profile, name):
            self.cp.slog.add("skip", "input", name, v, "off for this aircraft (profile)")
            return
        self.ie_recent.setdefault(name, deque(maxlen=12)).append((net.now(), v))
        self.ie_last[name] = v
        try:
            self.sc.set_input_event(h, v)
            self.cp.slog.add("apply", "input", name, v)
        except SimConnectError as e:
            self.log(f"Input event {name} failed: {e}")
            self.cp.slog.add("fail", "input", name, v, str(e))

    # ---------------------------------------------------------------- control ownership
    def _set_pf(self, pf, announce=False):
        was_slave = self._is_slave()
        was_pf = self.link is not None and self.pf == self.role
        self.pf = pf
        me = self.i_have_controls()
        if was_slave and me and self.ready and self.snaps and self._clock_diff() is not None:
            # Seamless takeover: start from where the plane is NOW on the other PC
            # (predicted forward past the network delay), not where we were showing it.
            vals, ctrl, g = self._sample(self._remote_now())
            self._ground_blend(vals, g)
            self.motion.write(vals)
            self._apply_peer_load()
        # Gave the controls away: start showing the new PF "live" (no lag jump-back),
        # then ease into normal buffered playback over HANDOFF_RAMP seconds.
        self._ramp_t0 = net.now() if (was_pf and not me) else None
        self._catchup_t0 = None
        self.snaps.clear()
        if self._ramp_t0 is not None and self._clock_diff() is not None and self._latest_motion:
            # Seed the follow buffer with our own last state (in the new PF's clock) so the
            # plane keeps gliding along its velocity until their first packets arrive.
            a = self._latest_aux
            self.snaps.append(Snap(
                self._latest_motion_t - self._clock_diff(), list(self._latest_motion),
                self._latest_ctrl,
                [a[sd.A_VE], a[sd.A_VU], a[sd.A_VN]] if a else None,
                a[sd.A_GROUND] if a else None))
        if announce:
            self._send({"t": "ctl", "e": self.ctl_epoch, "pf": pf})
        self.status["pf"] = "YOU have the controls" if me else "CO-PILOT has the controls"
        if self.link:
            self.log("You have the controls." if me else "Co-pilot has the controls - hands off, "
                     "your aircraft now follows theirs.")
        self._set_freeze(not me)

    def _begin_take(self):
        if not self.link or self.pf == self.role or self._catchup_t0 is not None:
            return
        if self._is_slave() and self.snaps and self._clock_diff() is not None and self.ready:
            self._catchup_t0 = net.now()          # fast-forward to "live", then take (in _tick)
        else:
            self._finish_take()

    def _finish_take(self):
        self._catchup_t0 = None
        self.ctl_epoch += 1
        self._set_pf(self.role, announce=True)

    # ---------------------------------------------------------------- inbox
    def _on_inbox(self, item):
        if item[0] == "cmd":
            c = item[1]
            if c == "set_pf":
                self._set_pf(item[2])
            elif c == "take":
                self._begin_take()
            elif c == "give":
                if self.link and self.pf == self.role:
                    self._send({"t": "give"})     # the co-pilot runs the smooth take path
                    self.log("Offering the controls to your co-pilot...")
            elif c == "disconnected":
                self._set_pf(self.role or "host")
            elif c == "match":
                self.cp.request_match()
            elif c == "flight":
                self.cp.request_flight()
            return
        if item[0] == "ws":
            self.cp.on_ws(item[1], item[2])
            return
        if item[0] == "udp":
            msg, addr = item[1], item[2]
            if not self.link or msg.get("k") != self.udp_token:
                return                                # stray or stale packet
            self._udp_last_rx = net.now()
            if self.role == "host":
                self.udp_peer = addr                  # reply to wherever the joiner's NAT maps
            if msg.get("t") == "mo" and self.pf != self.role:
                self._on_remote_motion(msg)
            return
        msg = item[1]
        t = msg.get("t")
        if self.cp.on_net(msg):
            return
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
        elif t == "give":
            self.log("Co-pilot is giving you the controls.")
            self._begin_take()
        elif t == "env":
            self.peer_env = msg
            self._check_env()
        elif t == "uok":
            was = self._udp_tx_ok
            self._udp_tx_ok = bool(msg.get("rx"))
            if self._udp_tx_ok != was:
                self.log("Motion now goes over UDP (fastest)." if self._udp_tx_ok else
                         "UDP stopped getting through; motion falls back to TCP.")
        elif t == "hello":
            self.peer = msg
            self.udp_token_peer = msg.get("uk")
            self.log(f"Co-pilot '{msg.get('name')}' says hello (aircraft: {msg.get('title') or '?'}).")
            if msg.get("sim") and msg["sim"] != flavor.SIM:
                self.log(f"Note: co-pilot is on {msg['sim']}, you're on {flavor.SIM}. Cross-version "
                         "flying is untested; aircraft names won't match and some switches may not sync.")
            if msg.get("v") != PROTO:
                self.log("WARNING: co-pilot runs a different Shared Cockpit version - both should update.")
            self._check_titles()
            if self.role == "host":
                # Host is authoritative on connect: who flies + full switch state.
                self._send({"t": "ctl", "e": self.ctl_epoch, "pf": self.pf or "host"})
                self._send({"t": "swfull", "s": self._sw_state()})
        elif t == "title":
            self.peer["title"] = msg["title"]
            self.peer["model"] = msg.get("model", "")
            self._check_titles()
            if self.role == "host":
                self._send({"t": "swfull", "s": self._sw_state()})
        elif t == "ping":
            self._send({"t": "pong", "x": msg["x"], "y": net.now()})
        elif t == "pong":
            t1 = net.now()
            self.rtt = (t1 - msg["x"]) * 1000.0
            self.status["rtt"] = f"{self.rtt:.0f} ms"
            if "y" in msg:   # NTP-style: midpoint of our send/receive vs their reply time
                self._clk.append((t1, t1 - msg["x"], (msg["x"] + t1) / 2.0 - msg["y"]))
                while self._clk and t1 - self._clk[0][0] > 10.0:
                    self._clk.popleft()

    # ---------------------------------------------------------------- periodic
    def _is_slave(self):
        return self.link is not None and self.pf is not None and self.pf != self.role

    def _net_tick(self, t):
        if not self.link:
            return
        if t - self._last_ping > 1.0:
            self._last_ping = t
            self._send({"t": "ping", "x": t})
        if self.udp and self.udp_peer and self.udp_token_peer and t - self._last_uh > 0.5:
            self._last_uh = t                 # keeps NAT mappings open + proves UDP works
            self.udp.send({"t": "uh", "k": self.udp_token_peer}, self.udp_peer)
        if t - self._last_uok > 1.0:
            self._last_uok = t
            self._send({"t": "uok", "rx": t - self._udp_last_rx < 2.5})
            self.status["motion"] = self._motion_status()
        if (not self._udp_hinted and t - self._conn_t > 8.0 and not self._udp_tx_ok
                and t - self._udp_last_rx > 8.0):
            self._udp_hinted = True
            port = self.udp_peer[1] if self.role == "join" and self.udp_peer else "your"
            self.log(f"UDP isn't getting through, so motion uses TCP (works, slightly less smooth). "
                     f"Host: also forward UDP port {port} on your router.")

    def _tick(self):
        t = net.now()
        self._net_tick(t)
        if not self.ready:
            return
        self.cp.tick(t)
        if self.sw_pending:
            for k in [k for k in self.sw_pending if k in self.cp.sw_off]:
                del self.sw_pending[k]
            done = {k: v for k, (v, t0) in self.sw_pending.items() if t - t0 >= sd.settle_s(k)}
            for k in done:
                del self.sw_pending[k]
            if done and self.link:
                self._send({"t": "sw", "s": done})
                for k, v in done.items():
                    self.cp.slog.add("out", "switch", k, v, "settled")
        if self.link and self._env and t - self._last_env_send > 2.0:
            self._last_env_send = t
            self._send(self._env_message())
            self._check_env()
        slave = self._is_slave()
        fs = self._frz_state
        if fs is not None:
            # Sim reports actual freeze flags: re-assert if they drifted.
            drift = any((v >= 0.5) != slave for v in fs)
        else:
            drift = slave and t - self._last_freeze > 3.0
        if (self._frozen is None or (self._frozen and not slave) or
                (drift and t - self._last_freeze > 1.0)):
            self._set_freeze(slave)
            self._frz_state = None
        if self._catchup_t0 is not None and t - self._catchup_t0 >= CATCHUP + 0.25:
            # No sim frame arrived to finish on (paused?) - finish anyway.
            if self._is_slave():
                self._finish_take()
            else:
                self._catchup_t0 = None
        # Fallback placement if our own sim frames aren't arriving (e.g. paused).
        if slave and t - self._last_frame > 0.1 and t - self._last_apply >= 1.0 / APPLY_HZ:
            self._last_apply = t
            self._apply_motion()
