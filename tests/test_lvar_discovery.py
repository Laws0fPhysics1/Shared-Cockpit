"""Reliable L:var discovery with the dry-run sim: scheduled re-lists catch vars an
aircraft's WASM creates late, a capped list (MobiFlight's 1000) is backed up by names
learned from the co-pilot, an aircraft profile and a persistent name cache; the
ignore patterns still apply to re-listed names; re-lists cause no extra traffic.

Run:  python tests/test_lvar_discovery.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMPD = tempfile.mkdtemp(prefix="sc_lvdisc_")
PROFILES = os.path.join(TMPD, "profiles")
os.makedirs(PROFILES)
with open(os.path.join(PROFILES, "trainer.json"), "w", encoding="utf-8") as f:
    json.dump({"name": "Test Trainer", "match": ["dry run TRAINER"],          # case-insensitive
               "lvars": ["PROFILE_ONLY_VAR"]}, f)
with open(os.path.join(PROFILES, "other.json"), "w", encoding="utf-8") as f:
    json.dump({"name": "Some Airliner", "match": ["CRJ"], "lvars": ["OTHER_PROFILE_VAR"]}, f)
CACHE = os.path.join(TMPD, "lvar_cache.json")
with open(CACHE, "w", encoding="utf-8") as f:
    json.dump({"DRYRUN": ["CACHED_ONLY_VAR"], "OTHER_PLANE": ["NOT_THIS_AIRCRAFT"]}, f)
os.environ["SC_PROFILE_DIR"] = PROFILES      # read at import of shared_cockpit.profiles
os.environ["SC_LVAR_CACHE"] = CACHE
os.environ.pop("SC_BRIDGE_IN_DRY", None)

from shared_cockpit import flavor  # noqa: E402

flavor.select("dry")
from shared_cockpit import cockpit  # noqa: E402
from shared_cockpit.engine import Engine  # noqa: E402

# re-list every second for 45 s after the aircraft is built (default: 0,5,15,30,60 s)
cockpit.RELIST_AFTER = tuple(float(i) for i in range(0, 46))
cockpit.RELIST_EXTRA_S = 0.6
T_START = time.time()


def wait(cond, t=10.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


def events(eng, direction=None, kind="lvar", name=None, since=0.0):
    return [e for e in eng.cp.slog.recent(600)
            if e[0] >= since and e[2] == kind and (direction is None or e[1] == direction)
            and (name is None or e[3] == name)]


def watched(eng, n):
    return n in eng.cp.lv_name_set


logs = []
A = Engine(lambda m: logs.append("[A] " + m), "Host", bridge_port=50681)
B = Engine(lambda m: logs.append("[B] " + m), "Joiner", bridge_port=50682)
try:
    wait(lambda: A.ready and B.ready, what="fake sims ready")
    A.host(50981)
    time.sleep(0.2)
    B.join("127.0.0.1", 50981)
    wait(lambda: B.link and A.link and B.pf == "host", what="connected")
    wait(lambda: A.cp.lv_names and B.cp.lv_names, what="L:var lists")
    for eng in (A, B):
        assert set(eng.cp.lv_names) == eng.cp.lv_name_set, "lv_names / lv_name_set disagree"
        assert len(eng.cp.lv_names) == len(eng.cp.lv_name_set), "duplicate names watched"
    print(f"OK initial list: {len(A.cp.lv_names)} L:vars watched on A")

    # --- d. aircraft profile
    for eng in (A, B):
        assert eng.cp.profile and eng.cp.profile.get("name") == "Test Trainer", eng.cp.profile
        assert watched(eng, "PROFILE_ONLY_VAR"), "profile var not watched"
        assert not watched(eng, "OTHER_PROFILE_VAR"), "non-matching profile applied"
    assert any(e[1] == "info" and e[2] == "profile" and "aircraft profile: Test Trainer" in e[5]
               for e in A.cp.slog.recent(600)), "profile log line"
    print("OK profile matched by title (case-insensitive); its L:var is watched")

    # --- e(1). names cached by an earlier session are watched on build
    assert A.cp.cache_path == CACHE, (A.cp.cache_path, CACHE)
    assert watched(A, "CACHED_ONLY_VAR"), "cached name not registered on build"
    assert not watched(A, "NOT_THIS_AIRCRAFT"), "another aircraft's cached names registered"
    print("OK cached names for this aircraft are watched on build (other aircraft's are not)")

    # --- b. re-lists of an unchanged sim cause no traffic and no duplicates
    wait(lambda: "FAKE_SYS_CLOCK" in A.cp.lv_ignored and "FAKE_SYS_CLOCK" in B.cp.lv_ignored, t=15,
         what="noise filter")
    n_before = len(A.cp.lv_names)
    t0 = time.time()
    time.sleep(2.6)                             # >= 2 scheduled re-lists
    statics = ["XMLVAR_OVHD_BAT1", "XMLVAR_OVHD_APU_MASTER", "XMLVAR_OVHD_SEATBELTS",
               "A320_FCU_ALT_SEL", "PROFILE_ONLY_VAR", "CACHED_ONLY_VAR"]
    spurious = [e for e in events(A, "out", since=t0) + events(B, "out", since=t0) if e[3] in statics]
    assert not spurious, spurious
    assert not events(B, "in", since=t0) and not events(A, "in", since=t0), "lvar traffic on re-list"
    assert len(A.cp.lv_names) == n_before == len(A.cp.lv_name_set), "re-list changed the watch list"
    print("OK re-lists of an unchanged cockpit send nothing and watch nothing new")

    # --- a. an L:var created late by the aircraft gets picked up by a re-list
    assert not watched(A, "LATE_SWITCH")
    A.sc.add_lvar("LATE_SWITCH")
    wait(lambda: watched(A, "LATE_SWITCH"), t=4, what="late var watched on A")
    assert "LATE_SWITCH" in A.cp.lv_names
    time.sleep(0.5)                             # first reading = baseline (not a change)
    assert not events(A, "out", name="LATE_SWITCH"), "baseline reading was sent"
    A.sc.set_var("L:LATE_SWITCH", 1)
    wait(lambda: B.sc.get("L:LATE_SWITCH") == 1, what="late var A->B")
    print("OK L:var created late on A is found by a re-list and syncs to B")

    # --- f. ignore patterns still apply to re-listed names
    A.sc.add_lvar("LATE_BLINK_LIGHT")
    A.sc.add_lvar("LATE_PROBE")                 # a normal name added at the same time
    wait(lambda: watched(A, "LATE_PROBE"), t=4, what="re-list after LATE_PROBE")
    time.sleep(1.2)
    assert not watched(A, "LATE_BLINK_LIGHT"), "ignored name watched after re-list"
    assert "XMLVAR_BLINK_STATE" not in A.cp.lv_name_set
    print("OK ignore patterns apply to re-listed names (LATE_BLINK_LIGHT not watched)")

    # --- c. capped list + names learned from the co-pilot
    cap = len(A.sc.lvar_names())
    A.sc.lvar_list_cap = cap                    # A's "MobiFlight" list is now full
    old_cap = cockpit.MF_LIST_CAP
    assert old_cap == 1000, old_cap
    cockpit.MF_LIST_CAP = cap
    t_cap = time.time()
    A.sc.add_lvar("CAP_X")
    B.sc.add_lvar("CAP_X")
    assert "CAP_X" not in A.sc.lvar_names() and "CAP_X" in B.sc.lvar_names()
    wait(lambda: watched(B, "CAP_X"), t=4, what="CAP_X watched on B via re-list")
    wait(lambda: any(e[1] == "info" and "full" in e[5] for e in events(A, since=t_cap)), t=4,
         what="'list full' info logged")
    cockpit.MF_LIST_CAP = old_cap
    time.sleep(0.6)                             # several re-lists on A: still not in its list
    assert not watched(A, "CAP_X"), "CAP_X watched on A though it is beyond A's list cap"
    time.sleep(0.4)                             # B has a baseline reading of CAP_X
    B.sc.set_var("L:CAP_X", 7)
    wait(lambda: A.sc.get("L:CAP_X") == 7, what="B->A CAP_X applied")
    wait(lambda: watched(A, "CAP_X"), t=3, what="CAP_X learned from co-pilot on A")
    peer_note = any("peer" in (e[5] or "").lower() or "co-pilot" in (e[5] or "").lower()
                    for e in A.cp.slog.recent(600) if e[2] == "lvar" and e[1] == "info")
    time.sleep(0.5)
    A.sc.set_var("L:CAP_X", 9)
    wait(lambda: B.sc.get("L:CAP_X") == 9, what="A->B CAP_X after peer-learn")
    print("OK capped list: CAP_X learned from co-pilot's message, then syncs both ways"
          + ("" if peer_note else " (note: no 'peer' info log line seen)"))
finally:
    A.shutdown()
    B.shutdown()

# --- e(2). names seen changing are saved to the cache (on close) and loaded next time
with open(CACHE, encoding="utf-8") as f:
    cache = json.load(f)
assert "OTHER_PLANE" in cache, "cache lost other aircraft's names"
names = set(cache.get("DRYRUN", []))
for n in ("LATE_SWITCH", "CAP_X"):
    assert n in names, (n, cache)
print(f"OK cache saved {len(names)} names for DRYRUN incl. LATE_SWITCH, CAP_X")

C = Engine(lambda m: logs.append("[C] " + m), "Fresh", bridge_port=50683)
try:
    wait(lambda: C.ready and C.cp.lv_names, what="fresh engine built")
    wait(lambda: watched(C, "LATE_SWITCH") and watched(C, "CAP_X"), t=3, what="cache loaded on build")
    print("OK a fresh session watches the cached names from the start")
finally:
    C.shutdown()

print(f"\nL:VAR DISCOVERY TESTS PASSED ({time.time() - T_START:.0f} s)")
shutil.rmtree(TMPD, ignore_errors=True)
