"""Dry-run mode end to end: two engines with the built-in fake sim over localhost.

Run:  python tests/test_dryrun.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import flavor  # noqa: E402

flavor.select("dry")
from shared_cockpit.engine import Engine  # noqa: E402


def wait(cond, t=5.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


logs = []
A = Engine(lambda m: logs.append("[A] " + m), "Host")
B = Engine(lambda m: logs.append("[B] " + m), "Joiner")
wait(lambda: A.ready and B.ready, what="fake sims ready")
A.host(50998)
time.sleep(0.2)
B.join("127.0.0.1", 50998)
wait(lambda: B.link and B.pf == "host", what="connected")
wait(lambda: B.sc.frozen() and not A.sc.frozen(), what="joiner frozen")
print("OK connected; joiner's pretend plane frozen")

time.sleep(1.5)
d_lat = abs(A.sc.get("PLANE LATITUDE") - B.sc.get("PLANE LATITUDE"))
d_hdg = abs(A.sc.get("PLANE HEADING DEGREES TRUE") - B.sc.get("PLANE HEADING DEGREES TRUE"))
assert d_lat < 0.0005 and d_hdg < 2.0, (d_lat, d_hdg)
print(f"OK joiner follows host (lat diff {d_lat:.6f} deg, hdg diff {d_hdg:.2f} deg)")

B.sc.set_var("LIGHT LANDING", 1)
B.sc.set_var("COM ACTIVE FREQUENCY:1", 121_500_000)
wait(lambda: A.sc.get("LIGHT LANDING") == 1 and A.sc.get("COM ACTIVE FREQUENCY:1") == 121_500_000,
     what="joiner switches reach host")
A.sc.set_var("GEAR HANDLE POSITION", 0)
A.sc.set_var("AUTOPILOT MASTER", 1)
wait(lambda: B.sc.get("GEAR HANDLE POSITION") == 0 and B.sc.get("AUTOPILOT MASTER") == 1,
     what="host switches reach joiner")
print("OK switches both ways")

B.take_controls()
wait(lambda: A.sc.frozen() and not B.sc.frozen(), what="handoff")
time.sleep(1.0)
assert abs(A.sc.get("PLANE LATITUDE") - B.sc.get("PLANE LATITUDE")) < 0.0005
print("OK handoff: host now follows joiner")
B.stop_net()
wait(lambda: not A.sc.frozen() and not B.sc.frozen(), what="unfreeze on disconnect")
print("OK disconnect unfreezes both")
A.shutdown(); B.shutdown()
print("\nDRY RUN TESTS PASSED")
