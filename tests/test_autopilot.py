"""Autopilot/FCU sync, reproducing what failed in a real A320neo flight (2026-11-12):
knob turns re-fired backwards (altitude wound down to -84000 ft), AP buttons fought.

Covers: K-event cause relay (knob INC/DEC, AP_MASTER) with echo guard, settle of
selector values, stale-reading echo window, input-event rules (autopilot/FMS names
skipped, repeated-value actions detected, self-changing events filtered).

Run:  python tests/test_autopilot.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import flavor  # noqa: E402

flavor.select("dry")
from shared_cockpit.engine import Engine  # noqa: E402

ALT, HDG, AP = "AUTOPILOT ALTITUDE LOCK VAR", "AUTOPILOT HEADING LOCK DIR", "AUTOPILOT MASTER"


def wait(cond, t=8.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


def count(eng, d, kind, name=None):
    return sum(1 for e in eng.cp.slog.recent(600) if e[1] == d and e[2] == kind and (name is None or e[3] == name))


logs = []
A = Engine(lambda m: logs.append("[A] " + m), "Host", bridge_port=50671)
B = Engine(lambda m: logs.append("[B] " + m), "Joiner", bridge_port=50672)
wait(lambda: A.ready and B.ready, what="fake sims ready")
A.host(50985)
time.sleep(0.2)
B.join("127.0.0.1", 50985)
wait(lambda: B.link and B.pf == "host" and B.sc.frozen(), what="connected")
wait(lambda: A.cp.k_by_id and B.cp.k_by_id, what="K events watched")
for e in (A, B):
    e.sc.set_var(ALT, 5000)
    e.sc.set_var(HDG, 90)
time.sleep(1.5)

# --- altitude knob: 10 notches UP on A -> exactly +1000 on both, same direction, no runaway
a_out0 = count(A, "out", "k")
t_turn = time.time()
for _ in range(10):
    A.sc.user_event("AP_ALT_VAR_INC")
    time.sleep(0.05)
wait(lambda: A.sc.get(ALT) == 6000 and B.sc.get(ALT) == 6000, what="alt +1000 on both")
time.sleep(2.0)                                        # anything left fighting would move it now
assert A.sc.get(ALT) == 6000 and B.sc.get(ALT) == 6000, (A.sc.get(ALT), B.sc.get(ALT))
assert count(A, "out", "k") - a_out0 == 10, count(A, "out", "k") - a_out0
assert count(B, "out", "k", "AP_ALT_VAR_INC") == 0, "B echoed the replayed knob turns back"
print("OK altitude knob up 10 notches on host -> +1000 ft on both, no echo, no runaway (was -84000)")

# --- heading knob DOWN on B (co-pilot side) -> same on A
for _ in range(5):
    B.sc.user_event("HEADING_BUG_DEC")
    time.sleep(0.05)
wait(lambda: A.sc.get(HDG) == 85 and B.sc.get(HDG) == 85, what="hdg -5 on both")
time.sleep(1.5)
assert A.sc.get(HDG) == 85 and B.sc.get(HDG) == 85
print("OK heading knob down 5 on co-pilot -> -5 on both")

# --- selector values are sent once settled, not while turning
settled = [e for e in A.cp.slog.recent(600)
           if e[2] == "switch" and e[3] == "ap_alt_sel" and e[1] == "out" and e[0] >= t_turn]
assert len(settled) <= 1 and all("settled" in e[5] for e in settled), settled
print(f"OK selector value synced only after the knob stopped ({len(settled)} settled message)")

# --- AP push on A: engages on both, exactly once, no on/off fight
A.sc.user_event("AP_MASTER")
wait(lambda: A.sc.get(AP) == 1 and B.sc.get(AP) == 1, what="AP engaged on both")
flips = []
end = time.time() + 2.5
while time.time() < end:
    flips.append((A.sc.get(AP), B.sc.get(AP)))
    time.sleep(0.05)
assert all(f == (1.0, 1.0) for f in flips), set(flips)
print("OK AP push on host engages both, stays engaged (no toggle fight)")
B.sc.user_event("AP_MASTER")
wait(lambda: A.sc.get(AP) == 0 and B.sc.get(AP) == 0, what="AP off on both")
time.sleep(1.5)
assert A.sc.get(AP) == 0 and B.sc.get(AP) == 0
print("OK AP push on co-pilot disengages both")

# --- stale reading: B applies 7000 from A, then B's sim briefly reports the older 6000
A.sc.set_var(ALT, 7000)
wait(lambda: B.sc.get(ALT) == 7000, t=5, what="alt 7000 to B")
B.sc.set_var(ALT, 6000)                         # late/stale reading on B
time.sleep(0.15)
B.sc.set_var(ALT, 7000)
time.sleep(2.0)
assert A.sc.get(ALT) == 7000, A.sc.get(ALT)
print("OK a stale reading on the co-pilot's side isn't sent back (echo window)")
# --- a K event fired right after a button press is that press's effect: not relayed
b_in0 = count(B, "in", "k", "AP_PANEL_VS_ON")
A.inbox.put(("ws", {"t": "h", "n": "A320_Neo_FCU_VS_ZERO"}, "dry"))   # host presses V/S
time.sleep(0.05)
A.sc.user_event("AP_PANEL_VS_ON")                 # host's FCU code reacts with a K event
time.sleep(1.0)
assert count(B, "in", "k", "AP_PANEL_VS_ON") == b_in0, "button-press effect was relayed (would double)"
print("OK a K event caused by a button press isn't relayed (the replayed press makes it there)")
A.shutdown(); B.shutdown()

# --- input-event rules (unit level)
C = Engine(lambda m: None, "C", bridge_port=50673)
wait(lambda: C.ready, what="C ready")
sent = []
C._send = lambda msg: sent.append(msg)
C.link = object()
C.peer = {"ie": True}
C.ie_by_hash = {1: "ELECTRICAL_Battery_1", 2: "AUTOPILOT_Altitude", 3: "FMC_A320_1_Keyboard_G",
                4: "LIGHTING_Landing_1", 5: "HANDLING_Gizmo", 6: "MB_ENSURE_Autobrake_Keys_Sync"}
C.ie_last = {"LIGHTING_Landing_1": 0.0}           # as read by the initial snapshot
C._on_local_input_event(1, 1.0)                   # switch position change -> sent
C._on_local_input_event(1, 0.0)                   # and back -> sent
C._on_local_input_event(2, 0.0)                   # autopilot knob -> never
C._on_local_input_event(3, 0.0)                   # FMS key -> never
C._on_local_input_event(6, 1.0)                   # self-updating helper -> never
C._on_local_input_event(4, 0.0)                   # same as snapshot: an action -> never
C._on_local_input_event(4, 1.0)
ies = lambda: [m for m in sent if m.get("t") == "ie"]
names = [m["n"] for m in ies()]
assert names == ["ELECTRICAL_Battery_1", "ELECTRICAL_Battery_1"], names
assert "LIGHTING_Landing_1" in C.ie_action
C.ie_recent["ELECTRICAL_Battery_1"] = [(time.perf_counter(), 1.0)]
C._on_local_input_event(1, 1.0)                   # value the co-pilot just sent -> echo, not sent
assert len(ies()) == 2, ies()
print("OK input events: switch positions mirrored; autopilot, FMS keys, helpers, actions and echoes not")
C.link = None
C.shutdown()
print("\nAUTOPILOT TESTS PASSED")
