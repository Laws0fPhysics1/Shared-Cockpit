"""Dry-run simulator: stands in for SimConnect so two people can test the
connection (port forwarding, sync, control handoff) without MSFS installed.

It keeps simvars in a dict by name. When not frozen, the airplane flies a
standard-rate circle near Seattle. Freeze events, data writes and switch
events all act on the dict exactly like the real sim would, and the GUI's
dry-run panel reads/writes the same dict.

crj_mode (env SC_FAKE_CRJ=1, or FakeSim(crj_mode=True)) pretends to be an
Aerosoft CRJ whose WASM owns parts of the cockpit (see _crj_frame): knob value
L:vars that only move on turn pulses, MCDU key L:vars that type into a pretend
scratchpad, and LIGHT LANDING forced from the aircraft's own switch L:var.
"""
import json
import math
import os
import struct
import threading
import time
from collections import deque

from . import syncdefs as sd
from .simconnect import (DT_FLOAT64, DT_STRING256, FLAG_CHANGED, PERIOD_NEVER, PERIOD_ONCE,
                         PERIOD_SECOND, PERIOD_SIM_FRAME, PERIOD_VISUAL_FRAME)

FRAME = 1.0 / 30.0
FT_PER_DEG_LAT = 364000.0
FREEZE_VARS = {
    "FREEZE_LATITUDE_LONGITUDE_SET": "IS LATITUDE LONGITUDE FREEZE ON",
    "FREEZE_ALTITUDE_SET": "IS ALTITUDE FREEZE ON",
    "FREEZE_ATTITUDE_SET": "IS ATTITUDE FREEZE ON",
}

# --- crj_mode: pretend Aerosoft CRJ -------------------------------------------
CRJ_TITLE, CRJ_MODEL = "Aerosoft CRJ 550 Dry Run", "CRJ5"
# value var -> (step var, animation var, change per pulse, wrap or None, accepts direct writes)
CRJ_KNOBS = {
    "L:ASCRJ_FCP_ALT_INFO": ("L:ASCRJ_FCP_ALT_CHANGE", "L:ASCRJ_FCP_ALT_SEL", 100.0, None, False),
    "L:ASCRJ_FCP_HDG_INFO": ("L:ASCRJ_FCP_HDG_CHANGE", "L:ASCRJ_FCP_HDG_SEL", 1.0, 360.0, True),
}
CRJ_KEYS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(i) for i in range(10)] + ["CLR"]
CRJ_MCDU = {f"L:ASCRJ_MCDU1_{k}": k for k in CRJ_KEYS}
CRJ_PRESS_MS = 100           # how long the CRJ holds a key / knob-turn L:var non-zero


def _event_map():
    m = {}
    for key, var, unit, kind, arg, scale in sd.SWITCHES:
        if kind == "event":
            m[arg] = ("set", var, scale)
        elif kind == "onoff":
            m[arg[0]] = ("const", var, 1.0)
            m[arg[1]] = ("const", var, 0.0)
        elif kind == "toggle":
            m[arg] = ("toggle", var, None)
    return m


class FakeSim:
    has_input_events = False
    dll_path = "built-in dry-run simulator"

    def __init__(self, *_a, crj_mode=None, **_k):
        self.lock = threading.Lock()
        self.state = {
            "TITLE": "Dry Run Trainer", "ATC MODEL": "DRYRUN", "NUMBER OF ENGINES": 1.0,
            "PLANE LATITUDE": 47.45, "PLANE LONGITUDE": -122.31, "PLANE ALTITUDE": 3000.0,
            "PLANE PITCH DEGREES": -2.0, "PLANE BANK DEGREES": -25.0,
            "PLANE HEADING DEGREES TRUE": 0.0, "VELOCITY BODY Z": 202.6,
            "GENERAL ENG THROTTLE LEVER POSITION:1": 75.0,
            "GENERAL ENG MIXTURE LEVER POSITION:1": 100.0,
            "GENERAL ENG PROPELLER LEVER POSITION:1": 100.0,
            "COM ACTIVE FREQUENCY:1": 118_000_000.0, "COM STANDBY FREQUENCY:1": 121_500_000.0,
            "COM ACTIVE FREQUENCY:2": 122_800_000.0, "COM STANDBY FREQUENCY:2": 119_100_000.0,
            "NAV ACTIVE FREQUENCY:1": 110_500_000.0, "NAV STANDBY FREQUENCY:1": 113_000_000.0,
            "NAV ACTIVE FREQUENCY:2": 110_500_000.0, "NAV STANDBY FREQUENCY:2": 113_000_000.0,
            "TRANSPONDER CODE:1": float(0x1200), "KOHLSMAN SETTING MB:1": 1013.25,
            "GEAR HANDLE POSITION": 1.0, "ELECTRICAL MASTER BATTERY": 1.0,
            "GROUND ALTITUDE": 0.0, "SEA LEVEL PRESSURE": 1013.25, "SIMULATION RATE": 1.0,
            "AMBIENT WIND DIRECTION": 0.0, "AMBIENT WIND VELOCITY": 0.0,
            "FUEL TANK LEFT MAIN QUANTITY": 26.0, "FUEL TANK RIGHT MAIN QUANTITY": 26.0,
            "PAYLOAD STATION COUNT": 2.0,
            "PAYLOAD STATION WEIGHT:1": 170.0, "PAYLOAD STATION WEIGHT:2": 170.0,
            "GENERAL ENG COMBUSTION:1": 1.0, "REALISM CRASH DETECTION": 1.0,
            "AMBIENT TEMPERATURE": 15.0, "AMBIENT VISIBILITY": 20000.0, "AMBIENT PRECIP STATE": 2.0,
            # pretend aircraft-specific cockpit variables (like the A320neo overhead)
            "L:XMLVAR_OVHD_BAT1": 1.0, "L:XMLVAR_OVHD_APU_MASTER": 0.0,
            "L:XMLVAR_OVHD_SEATBELTS": 0.0, "L:A320_FCU_ALT_SEL": 10000.0,
            "L:FAKE_SYS_CLOCK": 0.0,          # changes by itself every frame (must be ignored)
            "L:XMLVAR_BLINK_STATE": 0.0,      # filtered by name
        }
        self.events_by_name = {}
        self._eids, self.watched = {}, {}     # K events the client watches -> id
        self.scratchpad = ""                   # pretend captain MCDU scratchpad (H-event keys)
        self.scratchpad2 = ""                  # pretend first officer MCDU
        self.loaded_flights = 0
        self.lvar_list_cap = None              # int: lvar_names() lists only the first N (MobiFlight caps at 1000)
        self.alt_base, self.alt_amp = 3000.0, 150.0   # tests can fly it low
        self.history = deque(maxlen=4000)              # (t, lat, lon, alt, frozen) per frame
        self.defs = {}       # define_id -> [(name, dtype)]
        self.reqs = {}       # req_id -> [define_id, period, flags, interval, counter, last_payload]
        self.queue = []
        self.events = _event_map()
        self.t0 = self.last = time.perf_counter()
        self.next_frame = self.t0
        self.next_second = self.t0
        # crj_mode (pretend Aerosoft CRJ WASM)
        self.crj_mode = (os.environ.get("SC_FAKE_CRJ") == "1") if crj_mode is None else bool(crj_mode)
        self._crj_ready = False
        self.crj_scratchpad = ""               # pretend CRJ MCDU1 scratchpad (typed by ASCRJ_MCDU1_* keys)
        self.crj_typed = []                    # every key the pretend MCDU accepted, in order
        self.crj_pulses = {}                   # step var -> turn pulses the pretend WASM acted on
        self.crj_reverts = {}                  # value var -> direct writes it reverted
        self._crj_val = {}                     # knob value var -> the WASM's own value
        self._crj_prev = {}                    # var -> value at the previous frame boundary
        self._crj_release = []                 # [(perf time, var, rest)] pilot clicks to release
        self._crj_turns = []                   # [(step var, sign)] queued pilot knob clicks
        self._crj_turn_at = 0.0
        if self.crj_mode:
            self._crj_install()

    # -- SimConnect-compatible surface ---------------------------------
    def open(self):
        pass

    def close(self):
        pass

    def add_def(self, define_id, name, unit, dtype=DT_FLOAT64):
        self.defs.setdefault(define_id, []).append((name, dtype))

    def request(self, req_id, define_id, period, flags=0, interval=0):
        if period == PERIOD_NEVER:
            self.reqs.pop(req_id, None)
            return
        self.reqs[req_id] = [define_id, period, flags, interval, 0, None]
        if period == PERIOD_ONCE:
            self._emit(req_id)
            self.reqs.pop(req_id, None)

    def set_data(self, define_id, payload):
        names = [n for n, _ in self.defs.get(define_id, [])]
        vals = struct.unpack("<%dd" % len(names), payload[:8 * len(names)])
        with self.lock:
            for n, v in zip(names, vals):
                self.state[n] = v
            s = self.state
            if "PLANE LATITUDE" in names and s.get("IS LATITUDE LONGITUDE FREEZE ON", 0.0) >= 0.5:
                # log where the frozen plane is actually put, at the moment it's put there
                self.history.append((time.perf_counter(), s["PLANE LATITUDE"], s["PLANE LONGITUDE"],
                                     s["PLANE ALTITUDE"], True))

    def send_event(self, name, value=0):
        value = int(value) & 0xFFFFFFFF
        if name in self.watched:                # the sim reports every watched K event
            self.queue.append(("event", self.watched[name], value))
        if value & 0x80000000:
            value -= 1 << 32
        with self.lock:
            rel = {"AP_ALT_VAR_INC": ("AUTOPILOT ALTITUDE LOCK VAR", 100),
                   "AP_ALT_VAR_DEC": ("AUTOPILOT ALTITUDE LOCK VAR", -100),
                   "HEADING_BUG_INC": ("AUTOPILOT HEADING LOCK DIR", 1),
                   "HEADING_BUG_DEC": ("AUTOPILOT HEADING LOCK DIR", -1)}
            if name in rel:
                var, d = rel[name]
                for slot in (":1", ":3"):            # selected + displayed slots move together
                    self.state[var + slot] = self.state.get(var + slot, 0.0) + d
                return
            if name == "AP_MASTER":
                self.state["AUTOPILOT MASTER"] = 0.0 if self.state.get("AUTOPILOT MASTER", 0.0) >= 0.5 else 1.0
                return
            if name in FREEZE_VARS:
                self.state[FREEZE_VARS[name]] = 1.0 if value else 0.0
                return
            if name in ("ENGINE_AUTO_START", "ENGINE_AUTO_SHUTDOWN"):
                self.state["GENERAL ENG COMBUSTION:1"] = 1.0 if name == "ENGINE_AUTO_START" else 0.0
                return
            m = self.events.get(name)
            if not m:
                return
            kind, var, x = m
            if kind == "set":
                self.state[var] = value / x
            elif kind == "const":
                self.state[var] = x
            else:
                self.state[var] = 0.0 if self.state.get(var, 0.0) >= 0.5 else 1.0

    def subscribe_system(self, event_id, name):
        self.events_by_name[name] = event_id

    def event_id(self, name):
        return self._eids.setdefault(name, 1000 + len(self._eids))

    def watch_event(self, group_id, name):
        eid = self.event_id(name)
        self.watched[name] = eid
        return eid

    def set_group_priority(self, *_a):
        pass

    def user_event(self, name, data=0):
        """A pilot turns a knob / pushes a button in this pretend cockpit."""
        self.send_event(name, data)

    # -- dry-run stand-ins for the in-sim bridge / MobiFlight / flight files --
    def lvar_names(self):
        with self.lock:
            names = [n[2:] for n in self.state if n.startswith("L:")]   # dict keeps insertion order
        cap = self.lvar_list_cap
        return names[:cap] if isinstance(cap, int) else names

    def add_lvar(self, name, value=0.0):
        """The aircraft's own code creates an L:var late (like a WASM module after load)."""
        with self.lock:
            self.state.setdefault("L:" + name, float(value))

    def press_h(self, name):
        """A pretend MCDU: CDU key events type into a scratchpad."""
        key = name.rsplit("_BTN_", 1)[-1] if "_CDU_" in name else ""
        attr = "scratchpad2" if "_CDU_2_" in name else "scratchpad"
        with self.lock:
            pad = getattr(self, attr)
            if key == "CLR":
                pad = pad[:-1]
            elif len(key) == 1:
                pad = (pad + key)[-16:]
            setattr(self, attr, pad)

    def flight_save(self, path, title="", description="", flags=0):
        with self.lock:
            data = {k: v for k, v in self.state.items() if isinstance(v, float)}
        with open(path + ".FLT", "w", encoding="utf-8") as f:
            f.write("[Main]\nTitle=" + title + "\n[SharedCockpitDryRun]\n" + json.dumps(data))

    def flight_load(self, path):
        with open(path + ".FLT", encoding="utf-8") as f:
            data = json.loads(f.read().split("[SharedCockpitDryRun]\n", 1)[1])
        with self.lock:
            for k, v in data.items():
                if not k.startswith("IS ") and not k.startswith("L:"):
                    self.state[k] = v           # like a real .FLT: no L:vars
            self.loaded_flights += 1
        eid = self.events_by_name.get("FlightLoaded")
        if eid is not None:
            self.queue.append(("event", eid, 0))

    def poll(self):
        now = time.perf_counter()
        if now >= self.next_frame:
            dt = min(now - self.last, 0.2)
            self.last = now
            self.next_frame = now + FRAME
            self._physics(dt)
            if self.crj_mode:
                self._crj_frame(now)
            for rid, r in list(self.reqs.items()):
                if r[1] in (PERIOD_SIM_FRAME, PERIOD_VISUAL_FRAME):
                    r[4] += 1
                    if r[4] > r[3]:
                        r[4] = 0
                        self._emit(rid)
        if now >= self.next_second:
            self.next_second = now + 1.0
            for rid, r in list(self.reqs.items()):
                if r[1] == PERIOD_SECOND:
                    self._emit(rid)
        q, self.queue = self.queue, []
        for m in q:
            yield m

    # -- GUI helpers -----------------------------------------------------
    def get(self, name, default=0.0):
        with self.lock:
            return self.state.get(name, default)

    def set_var(self, name, value):
        with self.lock:
            self.state[name] = float(value)

    def frozen(self):
        return self.get("IS LATITUDE LONGITUDE FREEZE ON") >= 0.5

    # -- internals -------------------------------------------------------
    def _emit(self, rid):
        r = self.reqs.get(rid)
        if not r:
            return
        out = b""
        with self.lock:
            for name, dtype in self.defs.get(r[0], []):
                v = self.state.get(name, "" if dtype == DT_STRING256 else 0.0)
                if dtype == DT_STRING256:
                    out += str(v).encode()[:255].ljust(256, b"\0")
                else:
                    out += struct.pack("<d", float(v))
        if r[2] & FLAG_CHANGED:
            if out == r[5]:
                return
            r[5] = out
        self.queue.append(("data", rid, out))

    def _physics(self, dt):
        with self.lock:
            s = self.state
            now = time.perf_counter()
            s["L:FAKE_SYS_CLOCK"] = s.get("L:FAKE_SYS_CLOCK", 0.0) + 1.0   # a self-changing L:var
            frozen = s.get("IS LATITUDE LONGITUDE FREEZE ON", 0.0) >= 0.5
            if frozen:
                return                      # positions are logged when the engine writes them
            t = time.perf_counter() - self.t0
            hdg = (s["PLANE HEADING DEGREES TRUE"] + 3.0 * dt) % 360.0
            v = s["VELOCITY BODY Z"]
            lat = s["PLANE LATITUDE"]
            s["PLANE HEADING DEGREES TRUE"] = hdg
            s["PLANE LATITUDE"] = lat + v * math.cos(math.radians(hdg)) * dt / FT_PER_DEG_LAT
            s["PLANE LONGITUDE"] += (v * math.sin(math.radians(hdg)) * dt /
                                     (FT_PER_DEG_LAT * math.cos(math.radians(lat))))
            s["PLANE ALTITUDE"] = self.alt_base + self.alt_amp * math.sin(t / 15.0)
            s["VELOCITY WORLD X"] = v * math.sin(math.radians(hdg))
            s["VELOCITY WORLD Z"] = v * math.cos(math.radians(hdg))
            s["VELOCITY WORLD Y"] = self.alt_amp / 15.0 * math.cos(t / 15.0)
            self.history.append((now, s["PLANE LATITUDE"], s["PLANE LONGITUDE"],
                                 s["PLANE ALTITUDE"], False))
            s["PLANE BANK DEGREES"] = -25.0
            s["ROTATION VELOCITY BODY Y"] = math.radians(3.0)
            s["AILERON POSITION"] = -0.1

    # -- crj_mode: pretend Aerosoft CRJ --------------------------------------
    def _crj_install(self):
        with self.lock:
            s = self.state
            s["TITLE"], s["ATC MODEL"], s["NUMBER OF ENGINES"] = CRJ_TITLE, CRJ_MODEL, 2.0
            s.setdefault("L:ASCRJ_FCP_ALT_INFO", 10000.0)
            s.setdefault("L:ASCRJ_FCP_HDG_INFO", 90.0)
            for v, (step, anim, *_r) in CRJ_KNOBS.items():
                s.setdefault(step, 0.0)
                s.setdefault(anim, 0.0)
                self._crj_val[v] = s[v]
                self.crj_pulses[step] = 0
                self.crj_reverts[v] = 0
            for n in CRJ_MCDU:
                s.setdefault(n, 0.0)
            for n in ("L:ASCRJ_OVHD_LDG_LT", "L:ASCRJ_PARK_BRAKE", "L:ASCRJ_ELEVTRIM", "L:ASCRJ_FCP_AP_ENG"):
                s.setdefault(n, 0.0)
            s["LIGHT LANDING"] = s["L:ASCRJ_OVHD_LDG_LT"]
            self._crj_prev = {n: s.get(n, 0.0) for n in list(CRJ_MCDU) + [k[0] for k in CRJ_KNOBS.values()]}
        self._crj_ready = True

    def _crj_frame(self, now):
        """One frame of the pretend CRJ WASM, run from the frame loop under the lock.
        It samples its L:vars only here, at frame boundaries, like the real thing: a
        pulse written and cleared between two frames is never seen."""
        if not self._crj_ready:
            self._crj_install()
        with self.lock:
            s = self.state
            # the pilot's own clicks (crj_press / crj_turn) are released after their hold time
            keep = []
            for t, var, rest in self._crj_release:
                if now >= t:
                    s[var] = rest
                else:
                    keep.append((t, var, rest))
            self._crj_release = keep
            if self._crj_turns and now >= self._crj_turn_at:
                step, sign = self._crj_turns.pop(0)
                s[step] = float(sign)
                self._crj_release.append((now + CRJ_PRESS_MS / 1000.0, step, 0.0))
                self._crj_turn_at = now + CRJ_PRESS_MS / 1000.0 + 0.04
            # knobs: the WASM owns the value; it moves once per turn pulse (rest -> non-rest edge)
            for v, (step, anim, inc, wrap, direct) in CRJ_KNOBS.items():
                own = self._crj_val[v]
                cur = s.get(v, own)
                if abs(cur - own) > 1e-9:
                    if direct:
                        own = cur % wrap if wrap else cur
                    else:
                        self.crj_reverts[v] = self.crj_reverts.get(v, 0) + 1
                st = s.get(step, 0.0)
                if st != 0.0 and self._crj_prev.get(step, 0.0) == 0.0:
                    d = inc if st > 0 else -inc
                    own = (own + d) % wrap if wrap else own + d
                    s[anim] = (s.get(anim, 0.0) + (1.0 if st > 0 else -1.0)) % 30.0   # knob animation frame
                    self.crj_pulses[step] = self.crj_pulses.get(step, 0) + 1
                self._crj_prev[step] = st
                self._crj_val[v] = own
                s[v] = own
            # MCDU keys: a key L:var going 0 -> non-zero types once
            for n, key in CRJ_MCDU.items():
                val = s.get(n, 0.0)
                if val != 0.0 and self._crj_prev.get(n, 0.0) == 0.0:
                    self.crj_typed.append(key)
                    pad = self.crj_scratchpad
                    self.crj_scratchpad = pad[:-1] if key == "CLR" else (pad + key)[-24:]
                self._crj_prev[n] = val
            # the CRJ owns its lights: LIGHT LANDING follows its own switch L:var
            s["LIGHT LANDING"] = s.get("L:ASCRJ_OVHD_LDG_LT", 0.0)

    def crj_press(self, name, value=1.0, ms=CRJ_PRESS_MS, rest=0.0):
        """The pilot clicks a CRJ button: its L:var is `value` for `ms`, then `rest`."""
        if not name.startswith("L:"):
            name = "L:" + name
        with self.lock:
            self.state[name] = float(value)
            self._crj_release.append((time.perf_counter() + ms / 1000.0, name, float(rest)))

    def crj_turn(self, value_var, clicks):
        """The pilot turns a CRJ knob `clicks` detents (negative = down), one pulse per ~140 ms."""
        if not value_var.startswith("L:"):
            value_var = "L:" + value_var
        step = CRJ_KNOBS[value_var][0]
        with self.lock:
            self._crj_turns.extend([(step, 1 if clicks > 0 else -1)] * abs(int(clicks)))

    def crj_value(self, value_var):
        """The knob value the pretend CRJ shows: its own value, not a direct write it
        is about to revert at the next frame."""
        if not value_var.startswith("L:"):
            value_var = "L:" + value_var
        with self.lock:
            return self._crj_val.get(value_var, self.state.get(value_var, 0.0))

    def crj_busy(self):
        """Pilot clicks still queued or held."""
        with self.lock:
            return bool(self._crj_turns or self._crj_release)
