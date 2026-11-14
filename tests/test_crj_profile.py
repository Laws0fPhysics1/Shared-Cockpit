"""Phase 2 aircraft profile with the dry-run sim pretending to be an Aerosoft CRJ
(fakesim crj_mode): knobs synced by stepping (ALT, direct writes reverted) or by
direct write (HDG), MCDU key presses typed exactly once, ignored L:vars never
watched/sent/applied, switches_off (LIGHT LANDING owned by the aircraft) neither
sent nor applied and no flip-flop, Match co-pilot carries no press/ignore vars,
K events not relayed (k_relay false), engine PROTO 4. A second run in a
subprocess with an empty profile dir checks the no-profile behaviour is unchanged.

Uses the real profile shared_cockpit/profiles/aerosoft_crj.json.
Run:  python tests/test_crj_profile.py
"""
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NOPROFILE = "--noprofile" in sys.argv
TMPD = tempfile.mkdtemp(prefix="sc_crj_")
os.environ["SC_FAKE_CRJ"] = "1"
os.environ.pop("SC_BRIDGE_IN_DRY", None)
if NOPROFILE:
    os.environ["SC_PROFILE_DIR"] = os.path.join(TMPD, "no_profiles")
    os.makedirs(os.environ["SC_PROFILE_DIR"])
else:
    os.environ.pop("SC_PROFILE_DIR", None)          # the real profiles/ directory
if not os.environ.get("SC_LVAR_CACHE"):
    os.environ["SC_LVAR_CACHE"] = os.path.join(TMPD, "lvar_cache.json")

from shared_cockpit import flavor  # noqa: E402

flavor.select("dry")
from shared_cockpit import engine as engine_mod, profiles  # noqa: E402
from shared_cockpit.engine import Engine  # noqa: E402

ALT, ALT_STEP, ALT_SEL = "ASCRJ_FCP_ALT_INFO", "ASCRJ_FCP_ALT_CHANGE", "ASCRJ_FCP_ALT_SEL"
HDG, HDG_STEP = "ASCRJ_FCP_HDG_INFO", "ASCRJ_FCP_HDG_CHANGE"
LDG = "ASCRJ_OVHD_LDG_LT"
T_START = time.time()


def wait(cond, t=10.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


def record(eng):
    """Keep a copy of every message the engine sends to its co-pilot."""
    eng.sent = []
    orig = eng._send

    def snd(msg, _o=orig, _e=eng):
        if _e.link:
            _e.sent.append((time.time(), json.loads(json.dumps(msg))))
        return _o(msg)
    eng._send = snd


def record_events(eng):
    """Keep every K event the engine fires into its (pretend) sim."""
    eng.fired = []
    orig = eng.sc.send_event

    def se(name, value=0, _o=orig, _e=eng):
        _e.fired.append((time.time(), name, value))
        return _o(name, value)
    eng.sc.send_event = se


def msgs(eng, t=None, since=0.0):
    return [m for ts, m in eng.sent if ts >= since and (t is None or m.get("t") == t)]


def lv_names_sent(eng, since=0.0):
    out = set()
    for m in msgs(eng, "lv", since):
        out.update((m.get("v") or {}).keys())
    return out


def sw_keys_sent(eng, since=0.0):
    out = set()
    for ts, m in eng.sent:
        if ts < since:
            continue
        if m.get("t") in ("sw", "swfull"):
            out.update((m.get("s") or {}).keys())
        elif m.get("t") == "state":
            out.update((m.get("sw") or {}).keys())
    return out


def slog(eng, direction=None, name=None, since=0.0, note=None):
    return [e for e in eng.cp.slog.recent(600)
            if e[0] >= since and (direction is None or e[1] == direction)
            and (name is None or e[3] == name) and (note is None or note in (e[5] or ""))]


def dump(*engs):
    for eng, tag in zip(engs, "AB"):
        print(f"--- {tag} log tail")
        for e in [e for e in eng.cp.slog.recent(600) if e[3] != "FAKE_SYS_CLOCK"][-40:]:
            print("  ", e)
    for m in logs[-15:]:
        print(m)


def start(bport_a, bport_b, port):
    a = Engine(lambda m: logs.append("[A] " + m), "Host", bridge_port=bport_a)
    b = Engine(lambda m: logs.append("[B] " + m), "Joiner", bridge_port=bport_b)
    record(a)
    record(b)
    wait(lambda: a.ready and b.ready, what="fake sims ready")
    record_events(a)
    record_events(b)
    a.host(port)
    time.sleep(0.2)
    b.join("127.0.0.1", port)
    try:
        wait(lambda: b.link and a.link and b.pf == "host", what="connected")
    except AssertionError:
        print("\n".join(logs[-20:]))
        a.shutdown()
        b.shutdown()
        raise
    wait(lambda: a.cp.lv_names and b.cp.lv_names, what="L:var lists")
    time.sleep(1.0)                                 # first readings are baselines
    return a, b


logs = []


# ======================================================================== no profile
def run_noprofile():
    A, B = start(50693, 50694, 50976)
    try:
        assert A.sc.get("TITLE").find("CRJ") >= 0
        assert A.cp.profile is None and B.cp.profile is None, (A.cp.profile, B.cp.profile)
        print("OK no-profile run: CRJ-titled fake sim, no profile matched")
        t0 = time.time()
        A.sc.set_var("L:ASCRJ_PARK_BRAKE", 1)
        A.sc.set_var("L:" + HDG, 120)
        wait(lambda: B.sc.get("L:ASCRJ_PARK_BRAKE") == 1 and B.sc.crj_value(HDG) == 120,
             what="state mirroring without profile")
        print("OK without a profile every L:var is value-mirrored as before (park brake, HDG)")
        A.sc.crj_turn(ALT, 2)
        wait(lambda: A.sc.crj_value(ALT) == 10200, what="A ALT turned")
        wait(lambda: ALT in lv_names_sent(A, t0), what="ALT value sent as a plain lv")
        A.sc.set_var("L:" + LDG, 1)
        # (either side may send it first: B's light follows the mirrored L:var too)
        wait(lambda: "lt_landing" in sw_keys_sent(A, t0) | sw_keys_sent(B, t0),
             what="lt_landing sent as a generic switch")
        time.sleep(0.5)
        assert not msgs(A, "lp") and not msgs(A, "kn") and not msgs(B, "lp") and not msgs(B, "kn"), \
            "lp/kn messages without a profile"
        print("OK without a profile: ALT sent as lv, lt_landing sent as a switch, no lp/kn messages")
    except Exception:
        dump(A, B)
        raise
    finally:
        A.shutdown()
        B.shutdown()
    print("\nNO-PROFILE CRJ TESTS PASSED")


if NOPROFILE:
    run_noprofile()
    sys.exit(0)

# ======================================================================== with the CRJ profile
assert engine_mod.PROTO == 4, engine_mod.PROTO
assert Engine.PROTO == 4, Engine.PROTO
print("OK engine PROTO = 4")

A, B = start(50691, 50692, 50975)
try:
    # --- profile matched + classification
    for eng in (A, B):
        p = eng.cp.profile
        assert p and p.get("name") == "Aerosoft CRJ", p and p.get("name")
        assert p["file"].replace("\\", "/").endswith("shared_cockpit/profiles/aerosoft_crj.json"), p["file"]
    P = A.cp.profile
    assert profiles.classify(P, "ASCRJ_MCDU1_K")[0] == "press"
    assert profiles.classify(P, ALT_SEL)[0] == "ignore"
    assert profiles.classify(P, ALT_STEP)[0] == "ignore"
    assert profiles.classify(P, "ASCRJ_PARK_BRAKE")[0] == "ignore"
    assert profiles.classify(P, ALT)[0] == "knob"
    assert profiles.classify(P, "L:" + LDG)[0] == "state"
    assert profiles.switch_off(P, "lt_landing") and profiles.switch_off(P, "ap_master")
    assert not profiles.switch_off(P, "gear") and not profiles.switch_off(P, "battery")
    note = [e for e in slog(A, "info") if e[2] == "profile" and "knob" in (e[5] or "")]
    assert note, "no profile summary note ('<name>: N rules, K knobs, switches off: ...')"
    print(f"OK CRJ profile matched on both; summary: {note[-1][5]}")

    # --- watch list: ignore vars never watched, the rest are
    for eng in (A, B):
        for n in (ALT_SEL, "ASCRJ_PARK_BRAKE", "ASCRJ_ELEVTRIM"):      # (knob step vars are watched)
            assert n not in eng.cp.lv_name_set, f"ignored {n} is watched"
        for n in (ALT, HDG, LDG, "ASCRJ_MCDU1_K"):
            assert n in eng.cp.lv_name_set, f"{n} not watched"
    print("OK ignored L:vars are not watched (knob step vars are, per contract); knob, press, state vars are")

    # --- MCDU presses: typed exactly once on each side
    t0 = time.time()
    for k in "KSEA":
        A.sc.crj_press("ASCRJ_MCDU1_" + k)
        time.sleep(0.3)
    try:
        wait(lambda: B.sc.crj_scratchpad == "KSEA", t=6, what="KSEA typed on B")
    except AssertionError:
        print("   A pad", repr(A.sc.crj_scratchpad), "B pad", repr(B.sc.crj_scratchpad), "lp", msgs(A, "lp"))
        raise
    time.sleep(1.5)
    assert A.sc.crj_scratchpad == "KSEA" and A.sc.crj_typed == list("KSEA"), (A.sc.crj_scratchpad, A.sc.crj_typed)
    assert B.sc.crj_scratchpad == "KSEA" and B.sc.crj_typed == list("KSEA"), (B.sc.crj_scratchpad, B.sc.crj_typed)
    lp = [m["n"] for m in msgs(A, "lp", t0)]
    assert lp == ["ASCRJ_MCDU1_" + k for k in "KSEA"], lp
    assert all(m.get("v") == 1 for m in msgs(A, "lp", t0)), msgs(A, "lp", t0)
    assert not msgs(B, "lp", t0), f"B echoed presses: {msgs(B, 'lp', t0)}"
    assert not any(n.startswith("ASCRJ_MCDU") for n in lv_names_sent(A, t0) | lv_names_sent(B, t0)), \
        "MCDU key sent as a plain lv"
    for k in "KSEA":
        assert B.sc.get("L:ASCRJ_MCDU1_" + k) == 0, f"B's key {k} left pressed"
    print("OK MCDU keys K,S,E,A: one 'lp' each, typed once on both sims, no echo, keys released")

    t0 = time.time()
    B.sc.crj_press("ASCRJ_MCDU1_CLR")
    wait(lambda: A.sc.crj_scratchpad == "KSE", t=4, what="CLR from B")
    time.sleep(1.0)
    assert A.sc.crj_scratchpad == B.sc.crj_scratchpad == "KSE", (A.sc.crj_scratchpad, B.sc.crj_scratchpad)
    assert not msgs(A, "lp", t0), msgs(A, "lp", t0)
    print("OK CLR pressed on B removes one letter on both (KSE), not echoed")

    # --- knob by stepping: ALT 10000 -> 12300 (B's CRJ reverts direct writes)
    t0 = time.time()
    rev0 = B.sc.crj_reverts["L:" + ALT]
    A.sc.crj_turn(ALT, 23)
    wait(lambda: A.sc.crj_value(ALT) == 12300, t=10, what="A spun to 12300")
    try:
        wait(lambda: B.sc.crj_value(ALT) == 12300, t=25, what="B stepped to 12300")
    except AssertionError:
        print("   B ALT", B.sc.crj_value(ALT), "pulses", B.sc.crj_pulses, "kn", msgs(A, "kn", t0)[-3:])
        dump(A, B)
        raise
    time.sleep(2.0)
    assert A.sc.crj_value(ALT) == 12300 and B.sc.crj_value(ALT) == 12300, \
        (A.sc.crj_value(ALT), B.sc.crj_value(ALT))
    assert msgs(A, "kn", t0) and all(m["n"] == ALT for m in msgs(A, "kn", t0)), msgs(A, "kn", t0)
    assert msgs(A, "kn", t0)[-1]["v"] == 12300, msgs(A, "kn", t0)[-1]
    assert not [m for m in msgs(B, "kn", t0) if m["n"] == ALT], f"B sent its stepping back: {msgs(B, 'kn', t0)}"
    assert B.sc.crj_reverts["L:" + ALT] > rev0, "B never tried a direct write"
    assert B.sc.crj_pulses["L:" + ALT_STEP] >= 23, B.sc.crj_pulses
    assert slog(B, "info", note="stepping"), "no 'direct write ignored, stepping' note on B"
    assert B.sc.get("L:" + ALT_STEP) == 0, "B's step var left non-rest"
    assert ALT not in lv_names_sent(A, t0) and ALT_SEL not in lv_names_sent(A, t0)
    print(f"OK ALT knob: A spun 10000->12300, B's direct write reverted, B stepped "
          f"({B.sc.crj_pulses['L:' + ALT_STEP]} pulses) to 12300, no echo")

    t0 = time.time()
    B.sc.crj_turn(ALT, -5)
    wait(lambda: B.sc.crj_value(ALT) == 11800, t=6, what="B spun down to 11800")
    try:
        wait(lambda: A.sc.crj_value(ALT) == 11800, t=20, what="A stepped to 11800")
    except AssertionError:
        print("   A ALT", A.sc.crj_value(ALT), "pulses", A.sc.crj_pulses)
        dump(A, B)
        raise
    time.sleep(2.0)
    assert A.sc.crj_value(ALT) == B.sc.crj_value(ALT) == 11800
    assert not [m for m in msgs(A, "kn", t0) if m["n"] == ALT], f"A sent its stepping back: {msgs(A, 'kn', t0)}"
    print("OK ALT knob the other way: B spun down to 11800, A stepped down, no echo")

    # --- knob by direct write: HDG (B's CRJ accepts writes) 350 -> 5 across north
    t0 = time.time()
    p0 = B.sc.crj_pulses["L:" + HDG_STEP]
    A.sc.set_var("L:" + HDG, 350)
    wait(lambda: B.sc.crj_value(HDG) == 350, t=6, what="HDG 350 on B")
    A.sc.crj_turn(HDG, 15)
    wait(lambda: A.sc.crj_value(HDG) == 5, t=6, what="A HDG turned to 5")
    wait(lambda: B.sc.crj_value(HDG) == 5, t=8, what="B HDG 5")
    time.sleep(1.5)
    assert A.sc.crj_value(HDG) == B.sc.crj_value(HDG) == 5, (A.sc.crj_value(HDG), B.sc.crj_value(HDG))
    assert B.sc.crj_pulses["L:" + HDG_STEP] == p0, "B stepped HDG though direct writes work"
    assert not [e for e in slog(B, "info", since=t0, note="stepping") if HDG in (e[5] or "")], \
        "B switched HDG to stepping"
    assert not [m for m in msgs(B, "kn", t0) if m["n"] == HDG], f"B echoed HDG: {msgs(B, 'kn', t0)}"
    print("OK HDG knob: direct writes stick on B (no step pulses), wraps 350 -> 5, no echo")

    # --- ignored vars: never sent, never applied (incl. a peer that sends them anyway)
    t0 = time.time()
    A.sc.set_var("L:ASCRJ_PARK_BRAKE", 1)
    A.sc.set_var("L:ASCRJ_ELEVTRIM", 0.3)
    time.sleep(1.5)
    assert B.sc.get("L:ASCRJ_PARK_BRAKE") == 0 and B.sc.get("L:ASCRJ_ELEVTRIM") == 0
    for eng in (A, B):
        for ts, m in eng.sent:
            names = []
            if m.get("t") == "lv":
                names = [(n, "state") for n in (m.get("v") or {})]
            elif m.get("t") == "lp":
                names = [(m["n"], "press")]
            elif m.get("t") == "kn":
                names = [(m["n"], "knob")]
            for n, want in names:
                got = profiles.classify(eng.cp.profile, n)[0]
                assert got == want, f"{n} ({got}) sent as '{m.get('t')}'"
    B.inbox.put(("net", {"t": "lv", "v": {"ASCRJ_PARK_BRAKE": 1.0, ALT_SEL: 7.0}}))
    wait(lambda: slog(B, "skip", "ASCRJ_PARK_BRAKE", t0, "ignored by aircraft profile"), t=3,
         what="skip note for a received ignored lv")
    time.sleep(0.5)
    assert B.sc.get("L:ASCRJ_PARK_BRAKE") == 0, "received ignored L:var applied"
    assert not slog(B, "apply", "ASCRJ_PARK_BRAKE", t0)
    print("OK ignored L:vars (park brake, trim, knob SEL/CHANGE) never sent; a received one is skipped")

    # --- switches_off: LIGHT LANDING is the CRJ's; its own switch L:var syncs instead
    t0 = time.time()
    A.sc.set_var("L:" + LDG, 1)
    wait(lambda: B.sc.get("L:" + LDG) == 1 and B.sc.get("LIGHT LANDING") == 1, t=5,
         what="landing light via the CRJ's L:var")
    flips = []
    end = time.time() + 2.5
    while time.time() < end:
        flips.append((A.sc.get("LIGHT LANDING"), B.sc.get("LIGHT LANDING")))
        time.sleep(0.02)
    assert all(x == (1.0, 1.0) for x in flips), f"LIGHT LANDING flip-flops: {sorted(set(flips))}"
    off = [k for k in sw_keys_sent(A) | sw_keys_sent(B)
           if any(fnmatch.fnmatch(k, pat) for pat in P["switches_off"])]
    assert not off, f"switches_off keys sent: {off}"
    assert not [f for f in A.fired + B.fired if f[1] == "LANDING_LIGHTS_SET"], "LANDING_LIGHTS_SET fired"
    B.inbox.put(("net", {"t": "sw", "s": {"lt_landing": 0.0, "ap_master": 1.0}}))
    B.inbox.put(("net", {"t": "swfull", "s": {"lt_landing": 0.0}}))
    time.sleep(0.6)
    assert not [e for e in slog(B, "apply", since=t0) if e[3] in ("lt_landing", "ap_master")], \
        "switches_off applied from a peer"
    assert not [f for f in B.fired if f[1] in ("LANDING_LIGHTS_SET", "AP_MASTER")], B.fired
    print("OK switches_off: lt_landing/ap_* never sent nor applied, LIGHT LANDING steady (no flip-flop)")

    # --- k_relay false: autopilot K events are not relayed
    t0 = time.time()
    A.sc.user_event("AP_ALT_VAR_INC")
    A.sc.user_event("HEADING_BUG_INC")
    time.sleep(0.8)
    assert not msgs(A, "k", t0), msgs(A, "k", t0)
    print("OK k_relay false: K events not relayed")

    # --- Match co-pilot: only state L:vars and knob values; no press/ignore, no switches_off
    t0 = time.time()
    B.match_copilot()
    wait(lambda: msgs(A, "state", t0), t=5, what="A sent its state")
    st = msgs(A, "state", t0)[-1]
    classes = {n: profiles.classify(P, n)[0] for n in st.get("lv") or {}}
    bad = {n: c for n, c in classes.items() if c not in ("state", "knob")}
    assert not bad, f"Match carries press/ignore vars: {bad}"
    assert LDG in classes and ALT in classes, sorted(classes)
    off = [k for k in st.get("sw") or {} if profiles.switch_off(P, k)]
    assert not off, f"Match carries switches_off keys: {off}"
    wait(lambda: any("Matched co-pilot's aircraft" in m for m in logs if m.startswith("[B]")), t=6,
         what="B matched")
    time.sleep(1.5)
    assert B.sc.crj_scratchpad == "KSE", f"Match typed keys: {B.sc.crj_scratchpad!r}"
    assert B.sc.crj_value(ALT) == A.sc.crj_value(ALT) == 11800
    assert B.sc.get("L:ASCRJ_PARK_BRAKE") == 0
    print(f"OK Match co-pilot: {len(classes)} L:vars, all state/knob; no switches_off keys; nothing typed")
except Exception:
    dump(A, B)
    raise
finally:
    A.shutdown()
    B.shutdown()

# ======================================================================== no-profile run
env = dict(os.environ)
env.pop("SC_PROFILE_DIR", None)
r = subprocess.run([sys.executable, os.path.abspath(__file__), "--noprofile"], env=env, cwd=ROOT,
                   capture_output=True, text=True, timeout=180)
print("\n".join("   " + ln for ln in r.stdout.strip().splitlines()))
if r.returncode != 0:
    print(r.stderr)
    raise AssertionError("no-profile run failed")

print(f"\nCRJ PROFILE TESTS PASSED ({time.time() - T_START:.0f} s)")
