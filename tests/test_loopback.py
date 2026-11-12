"""Loopback test: host + joiner engines over real TCP with a fake SimConnect.

Run:  python tests/test_loopback.py
"""
import os
import queue
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import engine as eng_mod  # noqa: E402
from shared_cockpit import syncdefs as sd  # noqa: E402


class FakeSC:
    has_input_events = False
    dll_path = "fake"

    def __init__(self, *_a, **_k):
        self.q = queue.Queue()
        self.events = []      # (name, value)
        self.writes = []      # (define_id, values)
        self.defs = {}

    def open(self):
        title = b"Cessna 172".ljust(256, b"\0")
        self.q.put(("data", eng_mod.REQ_INFO, title + struct.pack("<d", 1.0)))

    def close(self):
        pass

    def add_def(self, did, name, unit, dtype=4):
        self.defs.setdefault(did, []).append(name)

    def request(self, *a, **k):
        pass

    def subscribe_system(self, *a):
        pass

    def set_data(self, did, payload):
        n = len(payload) // 8
        self.writes.append((did, struct.unpack("<%dd" % n, payload)))

    def send_event(self, name, value=0):
        self.events.append((name, value))

    def poll(self):
        while True:
            try:
                yield self.q.get_nowait()
            except queue.Empty:
                return

    # helpers for the test
    def push(self, req, values):
        self.q.put(("data", req, struct.pack("<%dd" % len(values), *values)))


eng_mod.SimConnect = FakeSC
logs = []


def mk(tag):
    return eng_mod.Engine(lambda m: logs.append(f"[{tag}] {m}"), tag)


def wait(cond, t=3.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timeout: " + what)


def freeze_state(sc):
    ev = [v for n, v in sc.events if n == "FREEZE_LATITUDE_LONGITUDE_SET"]
    return ev[-1] if ev else None


def sw_vals(over=None):
    v = []
    for s in sd.SWITCHES:
        v.append({"Hz": 118_000_000.0, "Bco16": 0x1200}.get(s[2], 0.0))
    for k, x in (over or {}).items():
        v[[s[0] for s in sd.SWITCHES].index(k)] = x
    return v


PORT = 50999
A, B = mk("host"), mk("join")
wait(lambda: A.ready and B.ready, what="engines ready")
sa, sb = A.sc, B.sc

# baseline switch readings on both sides
sa.push(A.REQ_SW, sw_vals())
sb.push(B.REQ_SW, sw_vals())

A.host(PORT)
time.sleep(0.2)
B.join("127.0.0.1", PORT)
wait(lambda: B.pf == "host" and A.link and B.link, what="handshake / ctl")
wait(lambda: freeze_state(sb) == 1, what="joiner frozen")
assert freeze_state(sa) in (None, 0), "host must not be frozen"
print("OK handshake: host flies, joiner frozen")

# --- motion: host flies east at constant rate, 60 Hz
t0 = time.time()
lon0 = -122.0
while time.time() - t0 < 1.0:
    dt = time.time() - t0
    vals = [47.0, lon0 + dt * 0.001, 1000 + dt * 10, 2.0, 0.0, 359.5 + dt * 1.0] + [0.0] * 6
    sa.push(A.REQ_MOTION, vals)
    sa.push(A.REQ_CTRL, [0.1] * len(A.ctrl.vars))
    time.sleep(1 / 60)
time.sleep(0.2)
mw = [v for d, v in sb.writes if d == B.motion.define_id]
assert len(mw) > 40, f"too few motion writes on joiner: {len(mw)}"
last = mw[-1]
assert abs(last[0] - 47.0) < 1e-6
assert abs(last[1] - (lon0 + 0.001)) < 0.0003, last[1]
assert 0 <= last[5] < 360, f"heading wrap broken: {last[5]}"
hdgs = [w[5] for w in mw]
jumps = max(abs(((b - a + 180) % 360) - 180) for a, b in zip(hdgs, hdgs[1:]))
assert jumps < 0.2, f"heading jumped {jumps}"
cw = [v for d, v in sb.writes if d == B.ctrl.define_id]
assert cw and abs(cw[-1][0] - 0.1) < 1e-6
print(f"OK motion: {len(mw)} smooth writes, heading wrapped through 360 cleanly, controls mirrored")

# --- placement follows the joiner's own sim frames
n0 = len([1 for d, _ in sb.writes if d == B.motion.define_id])
for _ in range(5):
    sb.push(B.REQ_MOTION, [0.0] * 12)
time.sleep(0.05)
n1 = len([1 for d, _ in sb.writes if d == B.motion.define_id])
assert n1 - n0 >= 5, f"frame-synced placement missing ({n1 - n0})"
print("OK placement driven by joiner's own sim frames")

# --- sim drops the freeze -> engine re-asserts it
n_fr = sum(1 for n, v in sb.events if n == "FREEZE_ALTITUDE_SET" and v == 1)
time.sleep(1.1)
sb.push(B.REQ_FRZ, [1.0, 0.0, 1.0])
wait(lambda: sum(1 for n, v in sb.events if n == "FREEZE_ALTITUDE_SET" and v == 1) > n_fr,
     what="re-freeze after drift")
print("OK freeze drift re-asserted")

# --- switch: host lowers gear + sets COM1
sa.push(A.REQ_SW, sw_vals({"gear": 1.0, "com1": 121_500_000.0, "ap_vs_s1": -500.0}))
wait(lambda: ("GEAR_SET", 1) in sb.events and ("COM_RADIO_SET_HZ", 121_500_000) in sb.events
     and ("AP_VS_VAR_SET_ENGLISH", -500) in sb.events, what="switch mirror to joiner")
# joiner sim now reports the applied values -> must NOT echo back
n_ev_a = len(sa.events)
sb.push(B.REQ_SW, sw_vals({"gear": 1.0, "com1": 121_500_000.0, "ap_vs_s1": -500.0}))
time.sleep(0.3)
assert not [e for e in sa.events[n_ev_a:] if e[0] != "FREEZE_LATITUDE_LONGITUDE_SET"
            and not e[0].startswith("FREEZE")], f"echo! {sa.events[n_ev_a:]}"
print("OK switches: host->joiner mirrored, no echo")

# --- joiner flips landing light -> host gets it
sb.push(B.REQ_SW, sw_vals({"gear": 1.0, "com1": 121_500_000.0, "ap_vs_s1": -500.0, "lt_landing": 1.0}))
wait(lambda: ("LANDING_LIGHTS_SET", 1) in sa.events, what="joiner->host light")
print("OK switches: joiner->host mirrored")

# --- control handoff
B.take_controls()
wait(lambda: A.pf == "join" and B.pf == "join", what="handoff")
wait(lambda: freeze_state(sa) == 1 and freeze_state(sb) == 0, what="freeze swap")
print("OK handoff: joiner flies, host frozen")
A.take_controls()
wait(lambda: A.pf == "host" and B.pf == "host" and freeze_state(sb) == 1, what="handoff back")
print("OK handoff back")

# --- disconnect unfreezes everyone
B.stop_net()
wait(lambda: freeze_state(sb) == 0 and A.link is None, what="disconnect unfreeze")
print("OK disconnect: joiner unfrozen, host waiting")

# --- rejoin works
B.join("127.0.0.1", PORT)
wait(lambda: B.link and B.pf == "host" and freeze_state(sb) == 1, what="rejoin")
print("OK rejoin")
A.shutdown(); B.shutdown()
print("\nALL TESTS PASSED")
