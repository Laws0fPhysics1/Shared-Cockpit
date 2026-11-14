"""Full-cockpit sync: everything beyond flying the plane and the basic switch list.

- Button presses (H: events), captured by the in-sim bridge (a hook in the
  cockpit's VCockpit.js that talks to us over a local WebSocket) and replayed
  on the other PC by the same hook. Covers MCDU/FMS keys and any glass-cockpit
  button. The replay never goes back through the sim's event system, so it is
  never re-reported (no echo).
- L:vars (aircraft-specific switches/knobs, e.g. most of the A320neo
  overhead): values read/written directly over SimConnect by name. Names come
  from several sources and are added incrementally (never resetting what is
  already watched): MobiFlight's in-sim list (re-listed a few times after an
  aircraft loads, since aircraft create their L:vars late, and capped at 1000
  names), the aircraft profile (profiles/*.json), a per-aircraft cache of names
  that actually synced before (lvar_cache.json), and names the co-pilot sends.
  Every change is mirrored, except values that keep changing on their own
  (timers, animations, ping-pong), which are detected and ignored for the session.
- Match co-pilot: pull the co-pilot's whole aircraft state (switches, L:vars,
  input events, fuel/payload, engines) and apply it here.
- Copy co-pilot's flight: they save a .FLT (position, systems, weather preset,
  time) and send it; this sim loads it, then runs Match co-pilot.
- Pre-flight checklist and the sync log / inspector.

The engine calls the on_* hooks from its sim thread.
"""
import base64
import glob
import json
import os
import re
import tempfile
import time
from collections import deque

from . import flavor, net, profiles, syncdefs as sd
from .simconnect import Block, PERIOD_NEVER, PERIOD_SECOND, PERIOD_SIM_FRAME, FLAG_CHANGED
from .synclog import SyncLog

BRIDGE_PORT = int(os.environ.get("SC_BRIDGE_PORT", "50556"))
LV_BLOCK = 100               # L:vars per data definition
LV_MAX = 4000
RELIST_AFTER = (0.0, 5.0, 15.0, 30.0, 60.0)   # s after a build: ask for the L:var list (again)
RELIST_EXTRA_S = 30.0        # the last list still found new names: one more this much later
RELIST_EXTRA_MAX = 4
MF_LIST_TIMEOUT_S = 30.0     # a MobiFlight list that never came back no longer blocks re-lists
MF_LIST_CAP = 1000           # MobiFlight lists at most this many names
LV_CACHE_MAX = 3000          # cached names per aircraft
LV_CACHE_SAVE_S = 30.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LV_ID0 = 50000               # define/request ids for L:var reads (per generation)
LV_WRITE_ID0 = 70000         # define ids for single L:var writes
IE_GET_ID0 = 90000           # request ids for GetInputEvent snapshots (Match co-pilot)
IE_INIT_ID0 = 95000          # request ids for the initial input-event values
K_GROUP = 77                 # notification group for relayed key events
K_ECHO_S = 1.0               # a K event we fired ourselves is ignored when the sim reports it
H_QUIET_S = 0.4              # K events right after a button press are its effects: both sims make them
CLICK_S = 0.6                # a K event is relayed only if the pilot touched a control this recently
# Knob-turn button events: on Airbus knobs they only make the FMS re-read a value the
# knob's model code already changed, so the VALUE is synced instead (slot sync).
AP_KNOB_H = re.compile(r"_AP_(INC|DEC)_|^AP_KNOB_(Up|Down)$")
FMS_PAIR = re.compile(r"^(.*?(?:CDU|MCDU|FMC|FMS)_?)([12])(_.*)$")
H_DEDUPE = 0.06              # s: every cockpit screen reports the same press once
NOISE_WINDOW, NOISE_SECONDS = 10, 8   # changed in 8 of the last 10 s => "changes on its own"
# Aircraft-profile control classes (profiles.py): momentary presses and stepped knobs.
PRESS_GAP_S = 0.05           # a queued press of the same var waits this long after the release
KNOB_VERIFY_S = 0.4          # after a direct write: did the value stick?
KNOB_STEP_S = 0.1            # min time between step pulses
KNOB_PULSE_S = 0.08          # step var held at up/down this long (a few sim frames), then back to rest
KNOB_STEP_WAIT_S = 0.3       # value didn't move after a pulse: pulse again anyway
KNOB_TIMEOUT_S = 40.0
KNOB_MAX_STEPS = 600
KNOB_MAX_REVERSALS = 4       # overshoots back and forth: the target can't be hit exactly
KNOB_TOL = 1e-6
TMP =os.path.join(tempfile.gettempdir(), "SharedCockpit")


def _load_ignore():
    pats = list(sd.LVAR_IGNORE)
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lvar_ignore.txt")
    try:
        with open(p, encoding="utf-8") as f:
            pats += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        pass
    return [re.compile(x, re.I) for x in pats]


class CockpitSync:
    def __init__(self, engine, bridge_port=None):
        self.e = engine
        self.slog = SyncLog()
        self.port = bridge_port or BRIDGE_PORT
        self.bridge = None
        self.panels = {}
        self.mf = None
        self._mf_sc = None
        self.REQ_STATE = None
        self.state_blk = None
        self.state = None
        # L:vars
        self.lv_names = []
        self.lv_name_set = set()
        self.lv_blocks = []          # (req_id, Block, names)
        self.lv_gen = 0
        self.lv_prev, self.lv_known, self.lv_write = {}, {}, {}
        self.lv_recent = {}
        self.lv_secs = {}            # name -> deque of whole seconds it changed in
        self.lv_ignored = set()
        self.lv_out = {}
        self._lv_flush = 0.0
        self._lv_did = LV_WRITE_ID0
        self._ignore = _load_ignore()
        self.profile = None
        self.lv_cls = {}             # watched name -> class ("state"/"press"/"knob"/"step")
        self.lv_fast = set()         # names in the every-frame blocks
        self.kn_out = {}             # knob value var -> value to send
        self._kn_flush = 0.0
        self._press_down = set()     # press vars currently away from rest (already sent)
        self._press_quiet = {}       # press var -> time until which our readings aren't sent
        self._press_q = {}           # press var -> deque of values to press (received)
        self._press_act = {}         # press var -> {"release": t, "rest": r} or {"next": t}
        self._knob = {}              # knob value var -> driver state (a received target)
        self._knob_mode = {}         # knob value var -> "write" | "step" (per aircraft build)
        self.sw_off = set()          # generic switch keys turned off by the profile
        self._relist_due = []        # net.now() times at which to ask for the L:var list
        self._relist_extra = 0
        self._mf_listing = None      # time a MobiFlight list was requested (in flight)
        self._lv_listed = False      # a list result arrived for this build
        self._mf_full_noted = False
        # cache of L:var names that actually synced, per aircraft
        self.cache_path = os.environ.get("SC_LVAR_CACHE") or os.path.join(ROOT, "lvar_cache.json")
        self._cache = None           # {aircraft key: [names]} (loaded lazily)
        self._cache_key = "?"
        self._cache_set = set()
        self._cache_dirty = False
        self._cache_saved = 0.0
        self._cache_drop = set()     # (key, name) found to be noise: removed from the file too
        # H events
        self._h_recent = {}
        self.instruments = set()     # cockpit instruments the bridge reported (element names)
        self.mirror_fms = True       # a key on either FMS also goes to the other one
        # K events (autopilot knobs/buttons): relayed as causes
        self.k_by_id = {}
        self._k_sc = None
        self._k_sent = {}            # (name, data) -> deque of times we fired it
        self._h_quiet_until = 0.0
        self._k_secs = {}
        self._k_noisy = set()
        # initial input-event values (so a knob/key "action" is recognised on first use)
        self._ie_init = {}
        # snapshots / flight transfer
        self._pending_state = None
        self._pending_flt = None
        self._match_at = None
        self._load_wait = None
        self._last_status = 0.0
        self._start_bridge()

    # ------------------------------------------------------------------ helpers
    def log(self, msg):
        self.e.log(msg)

    def _start_bridge(self):
        if flavor.is_dry() and os.environ.get("SC_BRIDGE_IN_DRY") != "1":
            return
        try:
            from .wsbridge import BridgeServer
            self.bridge = BridgeServer(self.port,
                                       on_message=lambda m, cid: self.e.inbox.put(("ws", m, cid)),
                                       log=lambda m: self.slog.add("info", "bridge", note=m))
        except ImportError:
            self.bridge = None
        except OSError as ex:
            self.bridge = None
            self.log(f"In-sim bridge port {self.port} is busy ({ex}); button sync is off. "
                     "Is another Shared Cockpit running?")

    def bridge_screens(self):
        try:
            return len(self.bridge.clients()) if self.bridge else 0
        except Exception:
            return 0

    def close(self):
        self._save_cache()
        try:
            if self.bridge:
                self.bridge.close()
        except Exception:
            pass
        self.slog.close()

    # ------------------------------------------------------------------ build / sim
    def on_build(self, base):
        """Engine rebuilt its data definitions (new aircraft or reconnect)."""
        e, sc = self.e, self.e.sc
        if self.REQ_STATE is not None:
            try:
                sc.request(self.REQ_STATE, self.REQ_STATE, PERIOD_NEVER)
            except Exception:
                pass
        self.REQ_STATE = base + 7
        self.instruments = set()     # new aircraft: its screens report again
        self.state_blk = Block(sc, base + 7, sd.state_vars(e.engines))
        self.state = None
        sc.request(self.REQ_STATE, base + 7, PERIOD_SECOND)
        if self._mf_sc is not sc:          # new SimConnect connection: definitions are gone
            self.lv_write = {}
            self._lv_did = LV_WRITE_ID0
        self._lv_reset()
        title, model = getattr(e, "title", "") or "", getattr(e, "model", "") or ""
        try:
            self.profile = profiles.find_profile(title, model)
        except Exception as ex:
            self.profile = None
            self.slog.add("fail", "profile", note=str(ex))
        self.sw_off = {s[0] for s in sd.SWITCHES if profiles.switch_off(self.profile, s[0])}
        self._knob, self._knob_mode = {}, {}
        if self.profile:
            self.slog.add("info", "profile", note=f"aircraft profile: {profiles.summary(self.profile)}")
            names = list(self.profile.get("lvars") or [])
            for kn in profiles.knobs(self.profile):
                names += [kn["value"], kn["step"]]
            self._add_lvars(names, "profile")
        self._save_cache()                  # the previous aircraft's names
        self._cache_key = model or title or "?"
        cached = self._cache_names(self._cache_key)
        self._cache_set = set(cached)
        if cached:
            self._add_lvars(cached, "cache")
        t0 = net.now()
        self._relist_due = sorted(t0 + float(x) for x in RELIST_AFTER)
        self._relist_extra = 0
        self._mf_listing = None
        self._lv_listed = False
        self._mf_full_noted = False
        if not hasattr(sc, "lvar_names") and not flavor.is_dry():
            if self.mf is None or self._mf_sc is not sc:
                try:
                    from .mobiflight import MobiFlightClient
                    self.mf = MobiFlightClient(sc, log=lambda m: self.slog.add("info", "mf", note=m))
                    self.mf.start()
                except Exception as ex:
                    self.mf = None
                    self.slog.add("fail", "mf", note=f"MobiFlight client unavailable: {ex}")
        self._mf_sc = sc
        self._watch_k_events(sc)
        if self._load_wait:                 # a co-pilot flight just loaded: match its state
            self._load_wait = None
            self._match_at = net.now() + 6.0

    def _lv_reset(self):
        sc = self.e.sc
        for rid, blk, _ in self.lv_blocks:
            try:
                sc.request(rid, blk.define_id, PERIOD_NEVER)
            except Exception:
                pass
        self.lv_blocks, self.lv_names, self.lv_name_set = [], [], set()
        self.lv_prev, self.lv_known, self.lv_secs, self.lv_out = {}, {}, {}, {}
        self.lv_ignored = set()
        self.lv_cls, self.lv_fast, self.kn_out = {}, set(), {}
        self._press_down, self._press_quiet, self._press_q, self._press_act = set(), {}, {}, {}
        self._knob = {}
        self.lv_gen += 1                     # new aircraft/connection: a fresh id range

    def _lv_classify(self, n):
        """(class, rule/knob) from the aircraft profile; a knob's step var is "step"
        (watched, so the driver knows its value, but never sent or applied)."""
        if not self.profile:
            return "state", None
        try:
            cls, rule = profiles.classify(self.profile, n)
        except Exception:
            return "state", None
        if cls == "ignore" and profiles.knob_step(self.profile, n):
            return "step", rule
        return cls, rule

    def _lv_class(self, n):
        return self._lv_classify(n)[0]

    def _set_lvars(self, names):
        """A full L:var list (MobiFlight or the dry-run sim): add what's new."""
        return self._add_lvars(names, "list")

    def _add_lvars(self, names, why):
        """Start watching the names not watched yet. Never touches what's already
        watched, so it can run any number of times. Returns how many were added."""
        sc = self.e.sc
        if sc is None:
            return 0
        new, skipped, seen = [], 0, set()
        for n in names or []:
            if not isinstance(n, str):
                continue
            n = n.strip()
            if n[:2].upper() == "L:":
                n = n[2:].strip()
            if not n or n in self.lv_name_set or n in seen:
                continue
            seen.add(n)
            cls = self._lv_class(n)
            if cls == "ignore":
                skipped += 1
                continue
            # the profile named it a press/knob on purpose: the generic name filter doesn't apply
            if cls == "state" and any(p.search(n) for p in self._ignore):
                skipped += 1
                continue
            new.append((n, cls))
        room = max(0, LV_MAX - len(self.lv_names))
        if len(new) > room:
            skipped += len(new) - room
            new = new[:room]
        first = why == "list" and not self._lv_listed
        if why == "list":
            self._lv_listed = True
        base = LV_ID0 + (self.lv_gen % 10) * 1000
        k = 0
        # presses, knob values and knob step vars are read every sim frame (a key is
        # down for ~100 ms); everything else every 4th frame when it changed
        fast = [(n, c) for n, c in new if c in ("press", "knob", "step")]
        normal = [(n, c) for n, c in new if c not in ("press", "knob", "step")]
        for group, interval in ((fast, 0), (normal, 3)):
            for i in range(0, len(group), LV_BLOCK):
                chunk = [n for n, _ in group[i:i + LV_BLOCK]]
                did = base + len(self.lv_blocks)
                try:
                    blk = Block(sc, did, [("L:" + n, "number") for n in chunk])
                    sc.request(did, did, PERIOD_SIM_FRAME, FLAG_CHANGED, interval=interval)
                except Exception as ex:
                    self.slog.add("fail", "lvar", note=f"watching {len(chunk)} L:vars failed: {ex}")
                    skipped += len(chunk)
                    continue
                self.lv_blocks.append((did, blk, chunk))
                self.lv_names.extend(chunk)
                self.lv_name_set.update(chunk)
                for n, c in group[i:i + LV_BLOCK]:
                    self.lv_cls[n] = c
                    if interval == 0:
                        self.lv_fast.add(n)
                k += len(chunk)
        if k > 0 or first:
            total = len(self.lv_names)
            self.slog.add("info", "lvar", note=f"{total} L:vars watched (+{k} from {why}), {skipped} skipped")
            self.log(f"Cockpit variables: syncing {total} L:vars (+{k} from {why})"
                     + (f", {skipped} skipped as display/animation" if skipped else "") + ".")
        return k

    # ---- re-listing
    def _relist(self, t):
        """Ask for the L:var list at the scheduled times after a build."""
        if not self._relist_due or t < self._relist_due[0]:
            return
        sc = self.e.sc
        if hasattr(sc, "lvar_names"):                # dry-run simulator lists its own L:vars
            self._relist_due.pop(0)
            try:
                names = list(sc.lvar_names())
            except Exception as ex:
                self.slog.add("fail", "lvar", note=f"list failed: {ex}")
                return
            self._on_list(names, t)
            return
        mf = self.mf
        if mf is None or not getattr(mf, "ready", False) or not self.e.ready:
            return                                   # stays due until MobiFlight is ready
        if self._mf_listing is not None:
            if t - self._mf_listing < MF_LIST_TIMEOUT_S:
                return                               # one list at a time
            self._mf_listing = None
        self._relist_due.pop(0)
        self._mf_listing = t
        gen = self.lv_gen
        mf.list_lvars(lambda names: self._on_mf_list(names, gen))

    def _on_mf_list(self, names, gen=None):
        self._mf_listing = None
        if gen is not None and gen != self.lv_gen:
            return                                   # a list made for the previous aircraft
        self._on_list(list(names or []), net.now())

    def _on_list(self, names, t):
        if len(names) >= MF_LIST_CAP and not self._mf_full_noted:
            self._mf_full_noted = True
            self.slog.add("info", "lvar", note=f"MobiFlight list is full ({MF_LIST_CAP}): this aircraft may "
                                               "have more L:vars; restart MSFS before changing aircraft. Names "
                                               "learned from the co-pilot, the profile and the cache still sync.")
            self.log(f"MobiFlight listed its maximum of {MF_LIST_CAP} L:vars, so some of this aircraft's may be "
                     "missing (L:vars of aircraft flown earlier stay listed). Restart MSFS before changing "
                     "aircraft. Names from the co-pilot, the aircraft profile and the cache still sync.")
        added = self._add_lvars(names, "list")
        if not self._relist_due and added > 0 and self._relist_extra < RELIST_EXTRA_MAX:
            self._relist_extra += 1
            self._relist_due.append(t + RELIST_EXTRA_S)

    # ---- name cache (names that actually synced, per aircraft)
    def _load_cache_file(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(d, dict):
            return {}
        return {k: [n for n in v if isinstance(n, str)][:LV_CACHE_MAX]
                for k, v in d.items() if isinstance(k, str) and isinstance(v, list)}

    def _cache_names(self, key):
        try:
            if self._cache is None:
                self._cache = self._load_cache_file()
            return list(self._cache.get(key) or [])
        except Exception:
            return []

    def _cache_note(self, n):
        if not isinstance(n, str) or n in self._cache_set:
            return
        try:
            if self._cache is None:
                self._cache = self._load_cache_file()
            lst = self._cache.setdefault(self._cache_key, [])
            if len(lst) >= LV_CACHE_MAX:
                return
            lst.append(n)
            self._cache_set.add(n)
            self._cache_dirty = True
        except Exception:
            pass

    def _cache_forget(self, n):
        """A name that turned out to be noise doesn't belong in the cache."""
        try:
            lst = (self._cache or {}).get(self._cache_key)
            if lst and n in lst:
                lst.remove(n)
                self._cache_set.discard(n)
                self._cache_dirty = True
                self._cache_drop.add((self._cache_key, n))
        except Exception:
            pass

    def _save_cache(self):
        if not self._cache_dirty or self._cache is None:
            return
        self._cache_dirty = False
        try:
            merged = self._load_cache_file()         # another instance may have written too
            for k, names in self._cache.items():
                cur = merged.setdefault(k, [])
                have = set(cur)
                cur.extend(n for n in names if n not in have)
                cur[:] = [n for n in cur if (k, n) not in self._cache_drop]
                del cur[LV_CACHE_MAX:]
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=1, sort_keys=True)
            os.replace(tmp, self.cache_path)
        except Exception as ex:
            try:
                self.slog.add("fail", "lvar", note=f"saving the L:var cache failed: {ex}")
            except Exception:
                pass

    def on_sim(self, m):
        """Sim messages the engine doesn't handle. Returns True if consumed."""
        kind = m[0]
        if kind == "client_data":
            return bool(self.mf and self.mf.on_client_data(m[1], m[2]))
        if kind == "input_value":
            p = self._pending_state
            if p and m[1] in p["ie_req"]:
                p["ie"][p["ie_req"][m[1]]] = m[2]
            elif m[1] in self._ie_init:
                self.e.ie_last.setdefault(self._ie_init.pop(m[1]), m[2])
            return True
        return False

    def on_data(self, req, raw):
        if req == self.REQ_STATE and self.state_blk:
            self.state = self.state_blk.unpack(raw)
            return True
        for rid, blk, names in self.lv_blocks:
            if rid == req:
                self._on_lvars(names, blk.unpack(raw))
                return True
        return False

    # ------------------------------------------------------------------ K events
    def _watch_k_events(self, sc):
        if self._k_sc is sc or not hasattr(sc, "watch_event"):
            return
        self._k_sc = sc
        self.k_by_id = {}
        ok = 0
        for name in sd.K_RELAY:
            try:
                self.k_by_id[sc.watch_event(K_GROUP, name)] = name
                ok += 1
            except Exception as ex:
                self.slog.add("fail", "k", name, note=str(ex))
        try:
            sc.set_group_priority(K_GROUP)
        except Exception as ex:
            self.slog.add("fail", "k", note=f"group priority: {ex}")
        self.slog.add("info", "k", note=f"watching {ok} autopilot key events")

    def note_sent(self, name, data=0):
        """The engine fired K:<name> itself: don't relay the sim's report of it."""
        if name in sd.K_RELAY:
            self._k_sent.setdefault((name, int(data) & 0xFFFFFFFF), deque()).append(net.now())

    def _is_own_k(self, name, data):
        dq = self._k_sent.get((name, data))
        now = net.now()
        while dq and now - dq[0] > K_ECHO_S:
            dq.popleft()
        if dq:
            dq.popleft()
            return True
        return False

    def on_k(self, eid, data):
        """A watched key event fired in our sim. Returns True if it was one of ours."""
        name = self.k_by_id.get(eid)
        if not name:
            return False
        data = int(data) & 0xFFFFFFFF
        if self._is_own_k(name, data):
            return True                         # we fired it (co-pilot's replay): no echo
        if not profiles.k_relay(self.profile):
            return True                         # this aircraft's own code drives its autopilot
        now = net.now()
        if now < self._h_quiet_until:
            # Fired by a cockpit instrument reacting to a button press; the same press
            # is replayed on the other sim, whose instrument fires it there too.
            self.slog.add("skip", "k", name, data, "effect of a button press (both sims make it)")
            return True
        if name in self._k_noisy:
            return True
        sc = self.e.sc
        if (getattr(sc, "has_input_events", False) and self.e.ie_by_name
                and now - self.e.last_click > CLICK_S):
            # Nobody touched a control: the aircraft's own code fired it (an A320 FMS
            # fires AP_PANEL_SPEED_HOLD on its own). Both sims do that themselves.
            self.slog.add("skip", "k", name, data, "fired by the aircraft's own logic")
            return True
        dq = self._k_secs.setdefault(name, deque())
        sec = int(now)
        if not dq or dq[-1] != sec:
            dq.append(sec)
        while dq and dq[0] <= sec - 10:
            dq.popleft()
        if len(dq) >= 8:
            self._k_noisy.add(name)
            self.slog.add("skip", "k", name, data, "fires on its own - not relayed this session")
            return True
        if self.e.link:
            self.e._send({"t": "k", "n": name, "d": data})
            self.slog.add("out", "k", name, data)
        return True

    def _replay_k(self, msg):
        name, data = msg.get("n"), int(msg.get("d") or 0) & 0xFFFFFFFF
        self.slog.add("in", "k", name, data)
        if not profiles.k_relay(self.profile):
            self.slog.add("skip", "k", name, data, "key events off for this aircraft (profile)")
            return
        if name not in sd.K_RELAY or not (self.e.sc and self.e.ready):
            self.slog.add("skip", "k", name, data)
            return
        try:
            self.note_sent(name, data)
            self.e.sc.send_event(name, data)
            self.slog.add("apply", "k", name, data)
        except Exception as ex:
            self.slog.add("fail", "k", name, data, str(ex))

    def request_ie_snapshot(self):
        """Read every input event's current value once, so the first press of an
        action-type event (value doesn't change) is recognised as an action."""
        sc, e = self.e.sc, self.e
        if sc is None or not getattr(sc, "has_get_input_event", False):
            return
        self._ie_init = {}
        for i, (name, h) in enumerate(sorted(e.ie_by_name.items())):
            rid = IE_INIT_ID0 + i
            try:
                sc.get_input_event(rid, h)
                self._ie_init[rid] = name
            except Exception:
                break

    # ------------------------------------------------------------------ L:vars
    def _noisy(self, n, v, sec):
        """Count a change; True (and ignored for the session) if it changes on its own."""
        dq = self.lv_secs.setdefault(n, deque())
        if not dq or dq[-1] != sec:
            dq.append(sec)
        while dq and dq[0] <= sec - NOISE_WINDOW:
            dq.popleft()
        if len(dq) >= NOISE_SECONDS:
            self.lv_ignored.add(n)
            self.lv_out.pop(n, None)
            self._cache_forget(n)
            self.slog.add("skip", "lvar", n, v, "changes on its own - ignored this session")
            return True
        return False

    def _on_lvars(self, names, vals):
        t = net.now()
        sec = int(t)
        for n, v in zip(names, vals):
            cls = self.lv_cls.get(n, "state")
            if cls != "state":
                self._on_lvar_special(n, v, cls, t, sec)
                continue
            prev = self.lv_prev.get(n)
            self.lv_prev[n] = v
            if prev is None:
                self.lv_known.setdefault(n, v)
                continue
            if abs(v - prev) < 1e-6 or n in self.lv_ignored:
                continue
            if self._noisy(n, v, sec):
                continue
            known = self.lv_known.get(n)
            if known is not None and abs(v - known) < 1e-6:
                continue                      # our own apply of the co-pilot's value
            rec = self.lv_recent.get(n)
            if rec and any(abs(v - rv) < 1e-6 and t - rt < sd.ECHO_S for rt, rv in rec):
                continue                      # a late reading of a value the co-pilot sent
            self.lv_known[n] = v
            if self.e.link:
                self.lv_out[n] = v
                self._cache_note(n)

    def _on_lvar_special(self, n, v, cls, t, sec):
        """A reading of a profile-classified L:var (press, knob value, knob step)."""
        prev = self.lv_prev.get(n)
        self.lv_prev[n] = v
        if cls == "press":
            rule = self._lv_classify(n)[1] or {}
            rest = float(rule.get("rest", 0.0) or 0.0)
            away = abs(v - rest) > 1e-6
            if prev is None:
                if away:
                    self._press_down.add(n)          # already down: not a new press
                return
            if abs(v - prev) < 1e-6 or n in self.lv_ignored:
                return
            if n in self._press_act or t < self._press_quiet.get(n, 0.0):
                # our replay of the co-pilot's press (or its late readings)
                if away:
                    self._press_down.add(n)
                else:
                    self._press_down.discard(n)
                return
            if self._noisy(n, v, sec):
                return
            if not away:
                self._press_down.discard(n)          # released: nothing to send
                return
            if n in self._press_down:
                return                               # still held, moved between values
            self._press_down.add(n)
            if self.e.link:
                self.e._send({"t": "lp", "n": n, "v": v, "r": rest,
                              "ms": int(rule.get("hold_ms", 100) or 100)})
                self.slog.add("out", "press", n, v)
                self._cache_note(n)
            return
        if cls == "knob":
            if prev is None:
                self.lv_known.setdefault(n, v)
                return
            if abs(v - prev) < 1e-6 or n in self._knob:
                return                               # unchanged, or we're driving it
            known = self.lv_known.get(n)
            if known is not None and abs(v - known) < 1e-6:
                return
            rec = self.lv_recent.get(n)
            if rec and any(abs(v - rv) < 1e-6 and t - rt < sd.ECHO_S for rt, rv in rec):
                return                               # a late reading of a co-pilot target
            self.lv_known[n] = v
            if self.e.link:
                self.kn_out[n] = v
                self._cache_note(n)
        # "step" (a knob's step var): read only, never sent

    def _write_lvar(self, n, v, source="lvar"):
        sc = self.e.sc
        if not (sc and self.e.ready):
            return False
        try:
            blk = self.lv_write.get(n)
            if blk is None:
                blk = self.lv_write[n] = Block(sc, self._lv_did, [("L:" + n, "number")])
                self._lv_did += 1
            blk.write([v])
            return True
        except Exception as ex:
            self.slog.add("fail", source, n, v, str(ex))
            return False

    def _apply_lvars(self, values, source="lvar"):
        sc = self.e.sc
        if not (sc and self.e.ready):
            return 0
        done = 0
        now = net.now()
        for n, v in values.items():
            cls, rule = self._lv_classify(n)
            if cls in ("ignore", "step"):
                self.slog.add("skip", source, n, v, "ignored by aircraft profile")
                continue
            if cls == "press":
                rest = float((rule or {}).get("rest", 0.0) or 0.0)
                if source == "match":
                    self.slog.add("skip", source, n, v, "momentary press: not part of the state")
                elif abs(v - rest) > 1e-6:
                    self._press_in(n, v, rule)       # a press sent as a value (co-pilot without profile)
                    done += 1
                continue
            if cls == "knob":
                self._knob_target(n, v, rule, source)
                done += 1
                continue
            self.lv_known[n] = v
            rec = self.lv_recent.setdefault(n, deque(maxlen=12))
            rec.append((now, v))
            if n in self.lv_ignored:
                self.slog.add("skip", source, n, v, "ignored here (changes on its own)")
                continue
            cur = self.lv_prev.get(n)
            if cur is not None and abs(cur - v) < 1e-6:
                continue
            if self._write_lvar(n, v, source):
                done += 1
                self.slog.add("apply", source, n, v)
        return done

    # ---- momentary presses (received)
    def _press_in(self, n, v, rule, msg=None):
        rule = rule or {}
        msg = msg or {}
        rest = rule.get("rest", msg.get("r", 0.0))
        hold = rule.get("hold_ms", msg.get("ms", 100))
        rest = float(rest) if isinstance(rest, (int, float)) else 0.0
        hold = float(hold) if isinstance(hold, (int, float)) and hold > 0 else 100.0
        self._press_q.setdefault(n, deque()).append((v, rest, min(hold, 5000.0) / 1000.0))
        self._press_run(n, net.now())

    def _press_run(self, n, t):
        act = self._press_act.get(n)
        if act is not None:
            if "release" in act:
                if t < act["release"]:
                    return
                self._write_lvar(n, act["rest"], "press")
                act = self._press_act[n] = {"next": t + PRESS_GAP_S}
            if t < act["next"]:
                return
            del self._press_act[n]
        q = self._press_q.get(n)
        if not q:
            self._press_q.pop(n, None)
            return
        v, rest, hold = q.popleft()
        if self._write_lvar(n, v, "press"):
            self.slog.add("apply", "press", n, v)
            self._press_act[n] = {"release": t + hold, "rest": rest}
            self._press_quiet[n] = t + hold + sd.ECHO_S
            self._press_down.add(n)

    def _on_press_msg(self, msg):
        n, v = msg.get("n"), msg.get("v")
        if not isinstance(n, str) or not isinstance(v, (int, float)) or isinstance(v, bool):
            return
        self.slog.add("in", "press", n, v)
        cls, rule = self._lv_classify(n)
        if cls in ("ignore", "step"):
            self.slog.add("skip", "press", n, v, "ignored by aircraft profile")
            return
        self._learn_peer_lvars({n: v})
        self._cache_note(n)
        if not (self.e.sc and self.e.ready):
            self.slog.add("skip", "press", n, v, "sim not ready")
            return
        self._press_in(n, v, rule if cls == "press" else None, msg)

    # ---- knobs (received targets)
    @staticmethod
    def _knob_diff(cur, target, kn):
        d = target - cur
        w = (kn or {}).get("wrap")
        if w:
            d = (d + w / 2.0) % w - w / 2.0
        return d

    def _on_knob_msg(self, msg):
        n, v = msg.get("n"), msg.get("v")
        if not isinstance(n, str) or not isinstance(v, (int, float)) or isinstance(v, bool):
            return
        self.slog.add("in", "knob", n, v)
        cls, _ = self._lv_classify(n)
        if cls in ("ignore", "step"):
            self.slog.add("skip", "knob", n, v, "ignored by aircraft profile")
            return
        self._learn_peer_lvars({n: v})
        self._cache_note(n)
        self._apply_lvars({n: v}, source="knob")   # knob => driver; no profile here => plain value

    def _knob_target(self, n, v, kn, source="knob"):
        t = net.now()
        v = float(v)
        self.lv_known[n] = v
        self.lv_recent.setdefault(n, deque(maxlen=12)).append((t, v))
        d = self._knob.get(n)
        cur = self.lv_prev.get(n)
        if d is None and cur is not None and abs(self._knob_diff(cur, v, kn)) <= KNOB_TOL:
            return
        mode = self._knob_mode.get(n, "write")
        if d is None:
            d = self._knob[n] = {"pulse_end": None, "last_step": -1e9, "v_at_pulse": None}
        d.update(target=v, t0=t, steps=0, rev=0, dir=0, source=source)
        if mode == "write":
            self._write_lvar(n, v, "knob")
            d["phase"] = "verify"
            d["verify_at"] = t + KNOB_VERIFY_S
        else:
            d["phase"] = "step"

    def _knob_done(self, n, d, how):
        self._knob.pop(n, None)
        self.lv_known[n] = d["target"]
        self.lv_recent.setdefault(n, deque(maxlen=12)).append((net.now(), d["target"]))
        self.slog.add("apply", "knob", n, d["target"], how)

    def _knob_fail(self, n, d, why):
        self._knob.pop(n, None)
        cur = self.lv_prev.get(n)
        if cur is not None:
            self.lv_known[n] = cur
        self.slog.add("fail", "knob", n, d["target"], why)

    def _knob_tick(self, n, d, t):
        cls, kn = self._lv_classify(n)
        if cls != "knob" or not kn:
            self._knob.pop(n, None)
            return
        if d["pulse_end"] is not None:
            if t < d["pulse_end"]:
                return
            self._write_lvar(kn["step"], kn["rest"], "knob")
            d["pulse_end"] = None
        target = d["target"]
        cur = self.lv_prev.get(n)
        if d["phase"] == "verify":
            if t < d["verify_at"]:
                return
            if cur is not None and abs(self._knob_diff(cur, target, kn)) <= KNOB_TOL:
                self._knob_done(n, d, "direct write")
                return
            self._knob_mode[n] = "step"
            self.slog.add("info", "knob", n, note=f"knob {n}: direct write ignored, stepping")
            d["phase"] = "step"
            d["t0"] = t
        if cur is not None and abs(self._knob_diff(cur, target, kn)) <= KNOB_TOL:
            self._knob_done(n, d, f"{d['steps']} steps")
            return
        if t - d["t0"] > KNOB_TIMEOUT_S or d["steps"] >= KNOB_MAX_STEPS:
            self._knob_fail(n, d, f"gave up after {d['steps']} steps ({cur} -> {target})")
            return
        if cur is None or t - d["last_step"] < KNOB_STEP_S:
            return
        if (d["v_at_pulse"] is not None and abs(cur - d["v_at_pulse"]) < 1e-9
                and t - d["last_step"] < KNOB_STEP_WAIT_S):
            return                                   # wait for the last step to show
        diff = self._knob_diff(cur, target, kn)
        direction = 1 if diff > 0 else -1
        if d["dir"] and direction != d["dir"]:
            d["rev"] += 1
            if d["rev"] >= KNOB_MAX_REVERSALS:
                self._knob_fail(n, d, f"can't reach {target} exactly with steps (at {cur})")
                return
        d["dir"] = direction
        if self._write_lvar(kn["step"], kn["up"] if direction > 0 else kn["down"], "knob"):
            d["pulse_end"] = t + KNOB_PULSE_S
        d["last_step"] = t
        d["v_at_pulse"] = cur
        d["steps"] += 1

    # ------------------------------------------------------------------ bridge (WebSocket)
    def on_ws(self, msg, cid):
        t = msg.get("t")
        if t in ("hello", "inst"):
            if t == "hello":
                self.panels[cid] = msg.get("panel") or "?"
                self.slog.add("info", "bridge", self.panels[cid], note="cockpit screen connected")
            before = self.screen_ap
            self.instruments.update(x for x in (msg.get("inst") or []) if isinstance(x, str))
            if self.screen_ap and not before:
                self.slog.add("info", "bridge", note="autopilot panel is a cockpit screen: "
                                                     "generic AP mode sync off, its own logic runs on both sims")
            return
        if t != "h":
            return
        n = msg.get("n")
        if not n or not isinstance(n, str):
            return
        now = net.now()
        if now - self._h_recent.get(n, -1.0) < H_DEDUPE:
            return                              # same press reported by another screen
        self._h_recent[n] = now
        self._h_quiet_until = now + H_QUIET_S
        self.e.last_click = now
        if AP_KNOB_H.search(n):
            self.slog.add("skip", "button", n, note="knob turn: its value is synced instead")
            return
        twin = self._fms_twin(n)
        if twin and self.bridge_screens():
            self.bridge.broadcast({"t": "h", "n": twin, "a": msg.get("a") or [], "i": None})
            self.slog.add("apply", "button", twin, note="mirrored to the other FMS")
        elif twin and hasattr(self.e.sc, "press_h"):             # dry-run pretend cockpit
            self.e.sc.press_h(twin)
            self.slog.add("apply", "button", twin, note="mirrored to the other FMS (dry run)")
        if self.e.link:
            self.e._send({"t": "h", "n": n, "a": msg.get("a") or [], "i": msg.get("i")})
            self.slog.add("out", "button", n)
        else:
            self.slog.add("skip", "button", n, note="not connected")

    @property
    def screen_ap(self):
        return any(k in t for t in self.instruments for k in sd.SCREEN_AP_TAGS)

    def _fms_twin(self, n):
        if not self.mirror_fms:
            return None
        m = FMS_PAIR.match(n)
        return f"{m.group(1)}{'2' if m.group(2) == '1' else '1'}{m.group(3)}" if m else None

    def _replay_h(self, msg):
        n = msg.get("n")
        twin = self._fms_twin(n)
        if twin:
            self._replay_h_one(dict(msg, n=twin, i=None), note="mirrored to the other FMS")
        self._replay_h_one(msg)

    def _replay_h_one(self, msg, note=""):
        n = msg.get("n")
        self.slog.add("in", "button", n, note=note)
        self._h_quiet_until = net.now() + H_QUIET_S
        if self.bridge_screens():
            self.bridge.broadcast({"t": "h", "n": n, "a": msg.get("a") or [], "i": msg.get("i")})
            self.slog.add("apply", "button", n)
        elif self.mf and getattr(self.mf, "ready", False):
            self.mf.execute(f"(>H:{n})")
            self.slog.add("apply", "button", n, note="via MobiFlight (bridge not connected)")
        elif hasattr(self.e.sc, "press_h"):          # dry-run pretend cockpit
            self.e.sc.press_h(n)
            self.slog.add("apply", "button", n, note="dry run")
        else:
            self.slog.add("fail", "button", n, note="in-sim bridge not connected")

    # ------------------------------------------------------------------ net messages
    def on_net(self, msg):
        t = msg.get("t")
        if t == "h":
            self._replay_h(msg)
        elif t == "k":
            self._replay_k(msg)
        elif t == "lv":
            vals = msg.get("v") or {}
            for n, v in vals.items():
                self.slog.add("in", "lvar", n, v)
                if self._lv_class(n) not in ("ignore", "step"):
                    self._cache_note(n)
            self._learn_peer_lvars(vals)
            self._apply_lvars(vals)
        elif t == "lp":
            self._on_press_msg(msg)
        elif t == "kn":
            self._on_knob_msg(msg)
        elif t == "state_req":
            self._start_state_snapshot()
        elif t == "state":
            self._apply_state(msg)
        elif t == "flt_req":
            self._save_flight()
        elif t == "flt":
            self._load_flight(msg)
        elif t == "flt_err":
            self.log(f"Co-pilot couldn't send their flight: {msg.get('m')}")
        else:
            return False
        return True

    def _learn_peer_lvars(self, vals):
        """Watch L:vars the co-pilot's sim knows about (its list may have had them)."""
        new = [n for n in vals if isinstance(n, str) and n not in self.lv_name_set]
        if new:
            self._add_lvars(new, "peer")

    def on_disconnect(self):
        self.lv_out = {}
        self.kn_out = {}
        self._pending_state = None
        self._pending_flt = None

    # ------------------------------------------------------------------ commands
    def request_match(self):
        if not self.e.link:
            self.log("Connect to your co-pilot first.")
            return
        self.e._send({"t": "state_req"})
        self.slog.add("out", "match", note="asked co-pilot for their aircraft state")
        self.log("Asking co-pilot for their aircraft state...")

    def request_flight(self):
        if not self.e.link:
            self.log("Connect to your co-pilot first.")
            return
        self.e._send({"t": "flt_req"})
        self.log("Asking co-pilot to send their flight (position, systems, weather preset, time)...")

    # ------------------------------------------------------------------ Match co-pilot
    def _start_state_snapshot(self):
        e, sc = self.e, self.e.sc
        p = {"deadline": net.now() + 1.2, "ie": {}, "ie_req": {}}
        if sc is not None and getattr(sc, "has_get_input_event", False):
            for i, (name, h) in enumerate(sorted(e.ie_by_name.items())):
                rid = IE_GET_ID0 + i
                try:
                    sc.get_input_event(rid, h)
                    p["ie_req"][rid] = name
                except Exception:
                    break
        if not p["ie_req"]:
            p["deadline"] = 0.0
        self._pending_state = p

    def _send_state(self, p):
        e = self.e
        n_env = len(sd.ENV)
        eng = list(self.state[:-1]) if self.state else []
        msg = {"t": "state", "title": e.title, "model": e.model,
               "sw": {k: v for k, v in e.sw_prev.items() if k not in self.sw_off},
               # only plain values and knob values (targets for the co-pilot's knob driver)
               "lv": {n: v for n, v in self.lv_prev.items()
                      if n not in self.lv_ignored and self.lv_cls.get(n, "state") in ("state", "knob")},
               "ie": {n: v for n, v in p["ie"].items()
                      if not any(k in n.upper() for k in sd.INPUT_EVENT_SKIP)
                      and not profiles.input_event_off(self.profile, n)},
               "eng": eng,
               "load": [round(x, 3) for x in e._env[n_env:]] if e._env else []}
        e._send(msg)
        self.slog.add("out", "match", note=f"sent state: {len(msg['sw'])} switches, {len(msg['lv'])} L:vars, "
                                           f"{len(msg['ie'])} input events")

    def _apply_state(self, msg):
        e = self.e
        if not (e.sc and e.ready):
            self.log("Can't match yet: the sim isn't connected.")
            return
        if msg.get("model") and e.model and msg["model"] != e.model:
            self.log("Note: your aircraft differs from the co-pilot's; matching what lines up.")
        self.slog.add("in", "match", note="co-pilot's aircraft state")
        e._apply_switches(msg.get("sw") or {}, full=True)
        self._learn_peer_lvars(msg.get("lv") or {})
        n_lv = self._apply_lvars(msg.get("lv") or {}, source="match")
        n_ie = 0
        for name, v in (msg.get("ie") or {}).items():
            if name in e.ie_by_name:
                e._apply_input_event(name, v)
                n_ie += 1
        load = msg.get("load") or []
        if load:
            saved = e.peer_env
            e.peer_env = {"l": load}
            try:
                e._apply_peer_load()
            finally:
                e.peer_env = saved
        eng_msg = ""
        remote_on = any(x >= 0.5 for x in msg.get("eng") or [])
        local_on = any(x >= 0.5 for x in (self.state[:-1] if self.state else []))
        try:
            if remote_on and not local_on:
                e.sc.send_event("ENGINE_AUTO_START")
                eng_msg = " Starting engines (auto-start; some aircraft ignore it - then start them by hand)."
            elif local_on and not remote_on and msg.get("eng"):
                e.sc.send_event("ENGINE_AUTO_SHUTDOWN")
                eng_msg = " Shutting engines down to match."
        except Exception as ex:
            eng_msg = f" Engine start/stop failed: {ex}"
        self.log(f"Matched co-pilot's aircraft: switches, {n_lv} cockpit variables, {n_ie} input events, "
                 f"fuel/payload.{eng_msg}")

    # ------------------------------------------------------------------ Copy co-pilot's flight
    def _save_flight(self):
        sc = self.e.sc
        if sc is None or not hasattr(sc, "flight_save"):
            self.e._send({"t": "flt_err", "m": "their sim isn't connected"})
            return
        out = os.path.join(TMP, "out")
        os.makedirs(out, exist_ok=True)
        for f in glob.glob(os.path.join(out, "*")):
            try:
                os.remove(f)
            except OSError:
                pass
        base = os.path.join(out, "cockpit_flight")
        try:
            sc.flight_save(base, "Shared Cockpit", "Flight sent to co-pilot")
        except Exception as ex:
            self.e._send({"t": "flt_err", "m": f"saving failed: {ex}"})
            return
        self._pending_flt = {"t0": net.now(), "base": base, "sizes": None, "stable": 0.0}
        self.log("Co-pilot asked for your flight: saving it...")

    def _check_saved_flight(self, t):
        p = self._pending_flt
        files = [f for f in glob.glob(p["base"] + ".*") if os.path.isfile(f)]
        has_flt = any(f.lower().endswith(".flt") for f in files)
        sizes = tuple(sorted((os.path.basename(f), os.path.getsize(f)) for f in files))
        if has_flt and sizes == p["sizes"]:
            if t - p["stable"] >= 1.0:
                payload = {}
                for f in files:
                    with open(f, "rb") as fh:
                        payload[os.path.basename(f)] = base64.b64encode(fh.read()).decode()
                self.e._send({"t": "flt", "files": payload})
                self._pending_flt = None
                self.log(f"Sent your flight to the co-pilot ({sum(s for _, s in sizes) // 1024} KB).")
                self.slog.add("out", "flight", note=", ".join(payload))
                return
        else:
            p["sizes"], p["stable"] = sizes, t
        if t - p["t0"] > 20.0:
            self._pending_flt = None
            self.e._send({"t": "flt_err", "m": "the sim didn't save the flight"})
            self.log("Saving the flight for the co-pilot timed out.")

    def _load_flight(self, msg):
        sc = self.e.sc
        files = msg.get("files") or {}
        if sc is None or not hasattr(sc, "flight_load"):
            self.log("Got the co-pilot's flight, but your sim isn't connected.")
            return
        inn = os.path.join(TMP, "in")
        os.makedirs(inn, exist_ok=True)
        for f in glob.glob(os.path.join(inn, "*")):
            try:
                os.remove(f)
            except OSError:
                pass
        flt = None
        for name, b64 in files.items():
            name = os.path.basename(name)
            if not re.fullmatch(r"[\w.\- ]+", name):
                continue
            path = os.path.join(inn, name)
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(b64))
            if name.lower().endswith(".flt"):
                flt = path
        if not flt:
            self.log("The co-pilot's flight had no .FLT file.")
            return
        self.slog.add("in", "flight", note=", ".join(files))
        self.log("Loading the co-pilot's flight (you'll see a loading screen). Their aircraft state is "
                 "matched once it's loaded.")
        self._load_wait = net.now()
        sc.flight_load(os.path.splitext(flt)[0])

    # ------------------------------------------------------------------ periodic
    def tick(self, t):
        e = self.e
        if self.mf:
            try:
                self.mf.tick(t)
            except Exception as ex:
                self.slog.add("fail", "mf", note=str(ex))
        self._relist(t)
        if self._cache_dirty and t - self._cache_saved >= LV_CACHE_SAVE_S:
            self._cache_saved = t
            self._save_cache()
        if self.lv_out and t - self._lv_flush >= 0.05:
            self._lv_flush = t
            if e.link:
                e._send({"t": "lv", "v": self.lv_out})
                for n, v in self.lv_out.items():
                    self.slog.add("out", "lvar", n, v)
            self.lv_out = {}
        if self.kn_out and t - self._kn_flush >= 0.05:
            self._kn_flush = t
            if e.link:
                for n, v in self.kn_out.items():
                    e._send({"t": "kn", "n": n, "v": v})
                    self.slog.add("out", "knob", n, v)
            self.kn_out = {}
        if self._press_act or self._press_q:
            for n in list(set(self._press_act) | set(self._press_q)):
                self._press_run(n, t)
        if self._knob:
            for n, d in list(self._knob.items()):
                self._knob_tick(n, d, t)
        p = self._pending_state
        if p and t >= p["deadline"]:
            self._pending_state = None
            self._send_state(p)
        if self._pending_flt:
            self._check_saved_flight(t)
        if self._match_at and t >= self._match_at:
            self._match_at = None
            self.request_match()
        if self._load_wait and t - self._load_wait > 120:
            self._load_wait = None
        if t - self._last_status > 1.0:
            self._last_status = t
            self.e.status["bridge"] = self.status_text()

    # ------------------------------------------------------------------ status / checklist
    def status_text(self):
        if flavor.is_dry():
            return f"dry run: {len(self.lv_names)} pretend cockpit variables"
        parts = []
        n = self.bridge_screens()
        parts.append(f"{n} cockpit screens hooked" if n else "buttons: bridge not connected")
        if self.mf is not None:
            parts.append("MobiFlight ready" if getattr(self.mf, "ready", False) else "MobiFlight not found")
        if self.lv_names:
            parts.append(f"{len(self.lv_names)} L:vars synced" +
                         (f", {len(self.lv_ignored)} ignored" if self.lv_ignored else ""))
        return " | ".join(parts)

    def checklist(self):
        """[(label, ok True/False/None (manual), detail)]"""
        e = self.e
        connected = e.link is not None
        items = []
        pt = e.peer.get("title") if connected else None
        if not connected or not pt or not e.title:
            items.append(("Same aircraft", None, "connect to compare"))
        else:
            same = pt == e.title or (e.peer.get("model") and e.peer.get("model") == e.model)
            items.append(("Same aircraft", bool(same), "" if same else f"co-pilot: {pt}"))
        if connected and e.peer_env and e._env:
            iss = getattr(e, "env_issues", [])
            items.append(("Same weather and sim rate", not iss,
                          "; ".join(iss) if iss else "Live Weather or the same preset on both"))
        else:
            items.append(("Same weather and sim rate", None, "both Live Weather, or the same preset"))
        crash = self.state[-1] if self.state else None
        items.append(("Crash damage off", None if crash is None else crash < 0.5,
                      "Assistance > Failure and damage > Crash damage: Off"))
        items.append(("MSFS multiplayer off", None, "World map > Multiplayer: Off (we sync the plane ourselves)"))
        if not flavor.is_dry():
            n = self.bridge_screens()
            items.append(("In-sim bridge (buttons, FMS)", n > 0,
                          f"{n} cockpit screens hooked" if n else "install it below, then restart MSFS"))
            items.append(("MobiFlight module (overhead L:vars)", bool(self.mf and getattr(self.mf, "ready", False)),
                          "installed with the bridge"))
        if connected:
            same_v = e.peer.get("v") == e.PROTO
            items.append(("Co-pilot on the same app version", same_v, "" if same_v else "both run install.bat to update"))
            items.append(("Fast motion link (UDP)", bool(e._udp_tx_ok), "" if e._udp_tx_ok else
                          "host: forward UDP 50555 too (TCP works, a bit less smooth)"))
        return items
