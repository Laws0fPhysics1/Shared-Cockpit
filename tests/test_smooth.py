"""Smoothness features, measured with the dry-run sim over localhost.

Covers: UDP motion path, adaptive jitter buffer, interpolation accuracy under
packet loss + jitter, seamless control handoff over a laggy link, fuel copy on
handoff, weather mismatch warning, ground-height blending.

Run:  python tests/test_smooth.py
"""
import bisect
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import flavor, net  # noqa: E402

flavor.select("dry")
from shared_cockpit.engine import Engine  # noqa: E402

FT = 364566.0


def wait(cond, t=8.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


def dist_ft(lat1, lon1, lat2, lon2):
    return math.hypot((lat2 - lat1) * FT, (lon2 - lon1) * FT * math.cos(math.radians(lat1)))


def truth_at(hist, t):
    """Interpolate a FakeSim history [(t, lat, lon, alt, frozen)] at time t."""
    ts = [h[0] for h in hist]
    i = bisect.bisect_left(ts, t)
    if i <= 0 or i >= len(hist):
        return None
    a, b = hist[i - 1], hist[i]
    f = (t - a[0]) / max(b[0] - a[0], 1e-9)
    return tuple(a[k] + (b[k] - a[k]) * f for k in (1, 2, 3))


def record_writes(eng):
    log = []
    orig = eng.motion.write

    def w(vals):
        log.append((eng.last_rt, list(vals)))
        orig(vals)
    eng.motion.write = w
    return log


def errors(log, hist, t_from):
    errs = []
    for rt, v in log:
        if rt is None or rt < t_from:
            continue
        tr = truth_at(list(hist), rt)
        if tr:
            errs.append(dist_ft(tr[0], tr[1], v[0], v[1]))
    return errs


logs = []
A = Engine(lambda m: logs.append("[A] " + m), "Host")
B = Engine(lambda m: logs.append("[B] " + m), "Joiner")
wait(lambda: A.ready and B.ready, what="fake sims ready")
A.host(50997)
time.sleep(0.2)
B.join("127.0.0.1", 50997)
wait(lambda: B.link and B.pf == "host" and B.sc.frozen(), what="connected")

# 1. UDP path
wait(lambda: A._udp_tx_ok and B._udp_tx_ok, what="UDP both ways")
print("OK motion over UDP both ways")

# 2. adaptive buffer on a clean link (it shrinks gently from its 120 ms start)
time.sleep(7.0)
assert B.buf < 0.06, B.buf
print(f"OK clean link: shows co-pilot {B.buf * 1000:.0f} ms behind (was a fixed 100 ms + network delay)")

# 3. accuracy, clean then impaired
wlog = record_writes(B)
t0 = net.now()
time.sleep(3.0)
e = errors(wlog, A.sc.history, t0)
print(f"OK clean link: follower within {max(e):.2f} ft of true path (mean {sum(e) / len(e):.2f})")
assert max(e) < 2.0

net.NETSIM = {"delay": 0.05, "jitter": 0.03, "loss": 0.15}
time.sleep(2.0)                                   # let the buffer adapt
t0 = net.now()
time.sleep(5.0)
e = errors(wlog, A.sc.history, t0)
# speed along the displayed path, per unit of displayed time: constant if motion is smooth
sp = [dist_ft(a[1][0], a[1][1], b[1][0], b[1][1]) / (b[0] - a[0]) for a, b in zip(wlog, wlog[1:])
      if a[0] and a[0] >= t0 and b[0] - a[0] > 0.005]
dev = max(abs(v - 202.6) for v in sp) / 202.6
print(f"OK 15% loss + 50+-30 ms: follower within {max(e):.2f} ft (mean {sum(e) / len(e):.2f}), "
      f"buffer {B.buf * 1000:.0f} ms, displayed speed steady within {dev * 100:.1f}%")
assert max(e) < 3.0 and dev < 0.05, (max(e), dev)

# 4. seamless handoff over a laggy link (no loss so the check is deterministic)
net.NETSIM = {"delay": 0.08, "jitter": 0.01}
time.sleep(2.5)
A.sc.set_var("FUEL TANK LEFT MAIN QUANTITY", 10.0)   # host burned fuel
time.sleep(2.5)                                       # env exchange (every 2 s)
th = net.now()
B.take_controls()
wait(lambda: A.sc.frozen() and not B.sc.frozen(), what="handoff")
time.sleep(4.0)


def speed_range(hist, t_from, t_to):
    """Min/max ground speed (ft/s) between consecutive frames: a jump shows as a spike,
    a pause as ~0. True speed is 202.6 ft/s."""
    h = [x for x in hist if t_from <= x[0] <= t_to]
    sp = [dist_ft(a[1], a[2], b[1], b[2]) / (b[0] - a[0]) for a, b in zip(h, h[1:]) if b[0] - a[0] > 0.01]
    return min(sp), max(sp)


a_lo, a_hi = speed_range(list(A.sc.history), th - 0.5, th + 5)
b_lo, b_hi = speed_range(list(B.sc.history), th - 0.5, th + 5)
print(f"OK handoff over 80 ms lag: giver speed {a_lo:.0f}-{a_hi:.0f} ft/s, taker {b_lo:.0f}-{b_hi:.0f} ft/s "
      f"(true 203; a jump would spike past 1000)")
assert 140 < a_lo and a_hi < 270 and 140 < b_lo and b_hi < 270, (a_lo, a_hi, b_lo, b_hi)
assert abs(B.sc.get("FUEL TANK LEFT MAIN QUANTITY") - 10.0) < 0.1, B.sc.get("FUEL TANK LEFT MAIN QUANTITY")
print("OK fuel copied to the new pilot flying")
A.take_controls()
wait(lambda: B.sc.frozen() and not A.sc.frozen(), what="handoff back")
net.NETSIM = {}

# 5. weather mismatch warning
B.sc.set_var("AMBIENT WIND DIRECTION", 270.0)
B.sc.set_var("AMBIENT WIND VELOCITY", 20.0)
wait(lambda: any("Weather/sim differs" in m and "wind" in m for m in logs), what="weather warning")
assert "differs" in B.status["env"]
print(f"OK weather mismatch warned ({B.status['env']})")

# 6. ground blending: host flies 20 ft above its runway; joiner's runway is 30 ft higher
A.sc.alt_base, A.sc.alt_amp = 20.0, 0.0
B.sc.set_var("GROUND ALTITUDE", 30.0)
time.sleep(1.5)
assert abs(B.sc.get("PLANE ALTITUDE") - 50.0) < 1.0, B.sc.get("PLANE ALTITUDE")
A.sc.alt_base = 500.0
time.sleep(1.5)
assert abs(B.sc.get("PLANE ALTITUDE") - 500.0) < 1.0, B.sc.get("PLANE ALTITUDE")
print("OK near the ground the joiner keeps 20 ft over ITS runway (50 ft MSL); at 500 ft it uses MSL")

A.shutdown(); B.shutdown()
print("\nSMOOTHNESS TESTS PASSED")
