"""Per-aircraft profiles: JSON files in shared_cockpit/profiles/ (or SC_PROFILE_DIR).

Schema 1 (unknown keys are ignored):
    {"name": "Aerosoft CRJ", "match": ["CRJ"], "lvars": ["ASCRJ_PARK_BRAKE", ...]}

A profile applies when any "match" string is a case-insensitive substring of the
aircraft's title or ATC model. Its "lvars" are watched even if MobiFlight never
lists them (L:vars are read by name, no listing needed).

Schema 2 adds how each control is synced:
    "lvar_classes": [{"re": regex, "class": "state"|"press"|"ignore",
                      "rest": 0, "hold_ms": 100}, ...]   first match wins (re.search
                                                         on the bare name, no "L:")
    "default_class": class of names matching no rule ("state")
    "knobs": [{"value": L:var, "step": L:var, "up": 1, "down": -1, "rest": 0,
               "wrap": 360 (optional)}]       value var = class "knob", step var = "ignore"
    "switches_off": fnmatch patterns over syncdefs SWITCHES keys (not synced)
    "k_relay": false => autopilot key events are not relayed
    "input_events_off": fnmatch patterns over input-event names (not synced)
"""
import fnmatch
import glob
import json
import os
import re

PROFILE_DIR = os.environ.get("SC_PROFILE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "profiles")

CLASSES = ("state", "press", "ignore")


def _num(x, default):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else default


def compile_profile(p):
    """Validate/compile the schema-2 parts of profile `p` in place (idempotent).
    Compiled data lives under keys starting with "_". Bad rules are skipped and
    listed in p["_errors"]."""
    if not isinstance(p, dict):
        return p
    if p.get("_compiled"):
        return p
    errors = []
    rules = []
    for i, r in enumerate(p.get("lvar_classes") or [] if isinstance(p.get("lvar_classes"), list) else []):
        if not isinstance(r, dict) or not isinstance(r.get("re"), str):
            errors.append(f"lvar_classes[{i}]: needs a 're' string")
            continue
        cls = r.get("class")
        if cls not in CLASSES:
            errors.append(f"lvar_classes[{i}]: unknown class {cls!r}")
            continue
        try:
            rx = re.compile(r["re"])
        except re.error as ex:
            errors.append(f"lvar_classes[{i}]: bad regex {r['re']!r}: {ex}")
            continue
        rule = dict(r)
        if cls == "press":
            rule["rest"] = _num(r.get("rest"), 0.0)
            hm = r.get("hold_ms")
            rule["hold_ms"] = int(hm) if isinstance(hm, (int, float)) and not isinstance(hm, bool) and hm > 0 else 100
        rules.append((rx, cls, rule))
    knob_value, knob_step = {}, {}
    for i, k in enumerate(p.get("knobs") or [] if isinstance(p.get("knobs"), list) else []):
        if not (isinstance(k, dict) and isinstance(k.get("value"), str) and isinstance(k.get("step"), str)
                and k["value"].strip() and k["step"].strip()):
            errors.append(f"knobs[{i}]: needs 'value' and 'step' L:var names")
            continue
        kn = dict(k)
        kn["value"] = _bare(k["value"])
        kn["step"] = _bare(k["step"])
        kn["up"] = _num(k.get("up"), 1.0)
        kn["down"] = _num(k.get("down"), -1.0)
        kn["rest"] = _num(k.get("rest"), 0.0)
        w = _num(k.get("wrap"), None)
        kn["wrap"] = w if w and w > 0 else None
        knob_value[kn["value"]] = kn
        knob_step[kn["step"]] = kn
    dc = p.get("default_class")
    p["_default_class"] = dc if dc in CLASSES else "state"
    p["_rules"] = rules
    p["_knob_value"] = knob_value
    p["_knob_step"] = knob_step
    p["_switches_off"] = [s.lower() for s in (p.get("switches_off") or []) if isinstance(s, str) and s.strip()] \
        if isinstance(p.get("switches_off"), list) else []
    p["_ie_off"] = [s.upper() for s in (p.get("input_events_off") or []) if isinstance(s, str) and s.strip()] \
        if isinstance(p.get("input_events_off"), list) else []
    p["_cls_cache"] = {}
    p["_errors"] = errors
    p["_compiled"] = True
    return p


def _bare(n):
    n = n.strip()
    return n[2:].strip() if n[:2].upper() == "L:" else n


def classify(profile, name):
    """(class, rule_or_knob_dict_or_None) for L:var `name` (with or without "L:").
    Classes: "state", "press", "ignore", "knob" (a knob's value var; the dict is the
    knob). A knob's step var is ("ignore", knob). No profile => ("state", None)."""
    if not profile:
        return "state", None
    if not profile.get("_compiled"):
        compile_profile(profile)
    cache = profile["_cls_cache"]
    hit = cache.get(name)
    if hit is not None:
        return hit
    n = _bare(name) if isinstance(name, str) else ""
    if n in profile["_knob_value"]:
        out = ("knob", profile["_knob_value"][n])
    elif n in profile["_knob_step"]:
        out = ("ignore", profile["_knob_step"][n])
    else:
        out = (profile["_default_class"], None)
        for rx, cls, rule in profile["_rules"]:
            if rx.search(n):
                out = (cls, rule)
                break
    if len(cache) < 20000:
        cache[name] = out
    return out


def knob_step(profile, name):
    """The knob whose step var is `name`, or None."""
    if not profile:
        return None
    if not profile.get("_compiled"):
        compile_profile(profile)
    return profile["_knob_step"].get(_bare(name))


def knobs(profile):
    if not profile:
        return []
    if not profile.get("_compiled"):
        compile_profile(profile)
    return list(profile["_knob_value"].values())


def switch_off(profile, key):
    """True if the generic switch `key` (syncdefs SWITCHES key) is off for this aircraft."""
    if not profile:
        return False
    if not profile.get("_compiled"):
        compile_profile(profile)
    k = (key or "").lower()
    return any(fnmatch.fnmatchcase(k, pat) for pat in profile["_switches_off"])


def input_event_off(profile, name):
    """True if input event `name` is not synced for this aircraft."""
    if not profile:
        return False
    if not profile.get("_compiled"):
        compile_profile(profile)
    n = (name or "").upper()
    return any(fnmatch.fnmatchcase(n, pat) for pat in profile["_ie_off"])


def k_relay(profile):
    """False if this aircraft's autopilot key events must not be relayed."""
    return not profile or profile.get("k_relay") is not False


def summary(profile):
    """One line: "<name>: N rules, K knobs, switches off: ..."."""
    if not profile:
        return ""
    if not profile.get("_compiled"):
        compile_profile(profile)
    off = ", ".join(profile.get("switches_off") or []) or "none"
    s = (f"{profile.get('name')}: {len(profile['_rules'])} rules, {len(profile['_knob_value'])} knobs, "
         f"switches off: {off}")
    if profile.get("k_relay") is False:
        s += ", key events not relayed"
    if profile["_ie_off"]:
        s += ", input events off: " + ", ".join(profile.get("input_events_off") or [])
    if profile["_errors"]:
        s += "; profile errors: " + "; ".join(profile["_errors"])
    return s


def load_profiles(path=None):
    """All valid profiles in `path` (default PROFILE_DIR), sorted by file name.
    Bad files are skipped. Each dict gets a "file" key and is compiled."""
    out = []
    for f in sorted(glob.glob(os.path.join(path or PROFILE_DIR, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                p = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        match = p.get("match")
        if isinstance(match, str):
            match = [match]
        if not isinstance(match, list):
            continue
        p["match"] = [m for m in match if isinstance(m, str) and m.strip()]
        lv = p.get("lvars")
        p["lvars"] = [n for n in lv if isinstance(n, str) and n.strip()] if isinstance(lv, list) else []
        p["name"] = p.get("name") if isinstance(p.get("name"), str) else os.path.splitext(os.path.basename(f))[0]
        p["file"] = f
        try:
            compile_profile(p)
        except Exception:
            continue
        out.append(p)
    return out


def find_profile(title, model, profiles=None):
    """First profile whose any match string is in the title or model (case-insensitive)."""
    if profiles is None:
        profiles = load_profiles()
    hay = [(title or "").lower(), (model or "").lower()]
    for p in profiles:
        for m in p.get("match") or []:
            m = m.strip().lower()
            if m and any(m in h for h in hay):
                return p
    return None
