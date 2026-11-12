"""Dry-run simulator: stands in for SimConnect so two people can test the
connection (port forwarding, sync, control handoff) without MSFS installed.

It keeps simvars in a dict by name. When not frozen, the airplane flies a
standard-rate circle near Seattle. Freeze events, data writes and switch
events all act on the dict exactly like the real sim would, and the GUI's
dry-run panel reads/writes the same dict.
"""
import math
import struct
import threading
import time

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

    def __init__(self, *_a, **_k):
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
        }
        self.defs = {}       # define_id -> [(name, dtype)]
        self.reqs = {}       # req_id -> [define_id, period, flags, interval, counter, last_payload]
        self.queue = []
        self.events = _event_map()
        self.t0 = self.last = time.perf_counter()
        self.next_frame = self.t0
        self.next_second = self.t0

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

    def send_event(self, name, value=0):
        if value & 0x80000000:
            value -= 1 << 32
        with self.lock:
            if name in FREEZE_VARS:
                self.state[FREEZE_VARS[name]] = 1.0 if value else 0.0
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

    def subscribe_system(self, *_a):
        pass

    def poll(self):
        now = time.perf_counter()
        if now >= self.next_frame:
            dt = min(now - self.last, 0.2)
            self.last = now
            self.next_frame = now + FRAME
            self._physics(dt)
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
            if s.get("IS LATITUDE LONGITUDE FREEZE ON", 0.0) >= 0.5:
                return
            t = time.perf_counter() - self.t0
            hdg = (s["PLANE HEADING DEGREES TRUE"] + 3.0 * dt) % 360.0
            v = s["VELOCITY BODY Z"]
            lat = s["PLANE LATITUDE"]
            s["PLANE HEADING DEGREES TRUE"] = hdg
            s["PLANE LATITUDE"] = lat + v * math.cos(math.radians(hdg)) * dt / FT_PER_DEG_LAT
            s["PLANE LONGITUDE"] += (v * math.sin(math.radians(hdg)) * dt /
                                     (FT_PER_DEG_LAT * math.cos(math.radians(lat))))
            s["PLANE ALTITUDE"] = 3000.0 + 150.0 * math.sin(t / 15.0)
            s["PLANE BANK DEGREES"] = -25.0
            s["ROTATION VELOCITY BODY Y"] = math.radians(3.0)
            s["AILERON POSITION"] = -0.1
