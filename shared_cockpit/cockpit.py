"""Full-cockpit sync: everything beyond flying the plane and the basic switch list.

- Button presses (H: events), captured by the in-sim bridge (a hook in the
  cockpit's VCockpit.js that talks to us over a local WebSocket) and replayed
  on the other PC by the same hook. Covers MCDU/FMS keys and any glass-cockpit
  button. The replay never goes back through the sim's event system, so it is
  never re-reported (no echo).
- L:vars (aircraft-specific switches/knobs, e.g. most of the A320neo
  overhead): names listed by MobiFlight's in-sim module, values read/written
  directly over SimConnect. Every change is mirrored, except values that keep
  changing on their own (timers, animations, ping-pong), which are detected
  and ignored for the session.
- Match co-pilot: pull the co-pilot's whole aircraft state (switches, L:vars,
  input events, fuel/payload, engines) and apply it here.
- Copy co-pilot's flight: they save a .FLT (position, systems, weather preset,
  time) and send it; this sim loads it, then runs Match co-pilot.
- Pre-flight checklist and the sync log / inspector.

The engine calls the on_* hooks from its sim thread.
"""
import base64
import glob
import os
import re
import tempfile
import time
from collections import deque

from . import flavor, net, syncdefs as sd
from .simconnect import Block, PERIOD_NEVER, PERIOD_SECOND, PERIOD_SIM_FRAME, FLAG_CHANGED
from .synclog import SyncLog

BRIDGE_PORT = int(os.environ.get("SC_BRIDGE_PORT", "50556"))
LV_BLOCK = 100               # L:vars per data definition
LV_MAX = 2000
LV_ID0 = 50000               # define/request ids for L:var reads (per generation)
LV_WRITE_ID0 = 70000         # define ids for single L:var writes
IE_GET_ID0 = 90000           # request ids for GetInputEvent snapshots
H_DEDUPE = 0.06              # s: every cockpit screen reports the same press once
NOISE_WINDOW, NOISE_SECONDS = 10, 8   # changed in 8 of the last 10 s => "changes on its own"
TMP = os.path.join(tempfile.gettempdir(), "SharedCockpit")


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
        self._want_list = False
        self.REQ_STATE = None
        self.state_blk = None
        self.state = None
        # L:vars
        self.lv_names = []
        self.lv_blocks = []          # (req_id, Block, names)
        self.lv_gen = 0
        self.lv_prev, self.lv_known, self.lv_write = {}, {}, {}
        self.lv_secs = {}            # name -> deque of whole seconds it changed in
        self.lv_ignored = set()
        self.lv_out = {}
        self._lv_flush = 0.0
        self._lv_did = LV_WRITE_ID0
        self._ignore = _load_ignore()
        # H events
        self._h_recent = {}
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
        self.state_blk = Block(sc, base + 7, sd.state_vars(e.engines))
        self.state = None
        sc.request(self.REQ_STATE, base + 7, PERIOD_SECOND)
        if self._mf_sc is not sc:          # new SimConnect connection: definitions are gone
            self.lv_write = {}
            self._lv_did = LV_WRITE_ID0
        self._lv_reset()
        if hasattr(sc, "lvar_names"):       # dry-run simulator lists its own L:vars
            self._set_lvars(sc.lvar_names())
        elif not flavor.is_dry():
            if self.mf is None or self._mf_sc is not sc:
                try:
                    from .mobiflight import MobiFlightClient
                    self.mf = MobiFlightClient(sc, log=lambda m: self.slog.add("info", "mf", note=m))
                    self.mf.start()
                except Exception as ex:
                    self.mf = None
                    self.slog.add("fail", "mf", note=f"MobiFlight client unavailable: {ex}")
            self._want_list = True
        self._mf_sc = sc
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
        self.lv_blocks, self.lv_names = [], []
        self.lv_prev, self.lv_known, self.lv_secs, self.lv_out = {}, {}, {}, {}
        self.lv_ignored = set()

    def _set_lvars(self, names):
        sc = self.e.sc
        if sc is None:
            return
        clean = []
        for n in names:
            n = n.strip()
            if n[:2].upper() == "L:":
                n = n[2:]
            if not n or any(p.search(n) for p in self._ignore):
                continue
            clean.append(n)
        clean = sorted(set(clean))[:LV_MAX]
        skipped = len(names) - len(clean)
        self._lv_reset()
        self.lv_gen += 1
        base = LV_ID0 + (self.lv_gen % 10) * 1000
        for i in range(0, len(clean), LV_BLOCK):
            chunk = clean[i:i + LV_BLOCK]
            did = base + i // LV_BLOCK
            blk = Block(sc, did, [("L:" + n, "number") for n in chunk])
            sc.request(did, did, PERIOD_SIM_FRAME, FLAG_CHANGED, interval=3)
            self.lv_blocks.append((did, blk, chunk))
        self.lv_names = clean
        self.log(f"Cockpit variables: syncing {len(clean)} L:vars"
                 + (f" ({skipped} skipped as display/animation)" if skipped else "") + ".")
        self.slog.add("info", "lvar", note=f"{len(clean)} L:vars, {skipped} skipped")

    def on_sim(self, m):
        """Sim messages the engine doesn't handle. Returns True if consumed."""
        kind = m[0]
        if kind == "client_data":
            return bool(self.mf and self.mf.on_client_data(m[1], m[2]))
        if kind == "input_value":
            p = self._pending_state
            if p and m[1] in p["ie_req"]:
                p["ie"][p["ie_req"][m[1]]] = m[2]
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

    # ------------------------------------------------------------------ L:vars
    def _on_lvars(self, names, vals):
        t = net.now()
        sec = int(t)
        for n, v in zip(names, vals):
            prev = self.lv_prev.get(n)
            self.lv_prev[n] = v
            if prev is None:
                self.lv_known.setdefault(n, v)
                continue
            if abs(v - prev) < 1e-6 or n in self.lv_ignored:
                continue
            dq = self.lv_secs.setdefault(n, deque())
            if not dq or dq[-1] != sec:
                dq.append(sec)
            while dq and dq[0] <= sec - NOISE_WINDOW:
                dq.popleft()
            if len(dq) >= NOISE_SECONDS:
                self.lv_ignored.add(n)
                self.lv_out.pop(n, None)
                self.slog.add("skip", "lvar", n, v, "changes on its own - ignored this session")
                continue
            known = self.lv_known.get(n)
            if known is not None and abs(v - known) < 1e-6:
                continue                      # our own apply of the co-pilot's value
            self.lv_known[n] = v
            if self.e.link:
                self.lv_out[n] = v

    def _apply_lvars(self, values, source="lvar"):
        sc = self.e.sc
        if not (sc and self.e.ready):
            return 0
        done = 0
        for n, v in values.items():
            self.lv_known[n] = v
            if n in self.lv_ignored:
                self.slog.add("skip", source, n, v, "ignored here (changes on its own)")
                continue
            cur = self.lv_prev.get(n)
            if cur is not None and abs(cur - v) < 1e-6:
                continue
            try:
                blk = self.lv_write.get(n)
                if blk is None:
                    blk = self.lv_write[n] = Block(sc, self._lv_did, [("L:" + n, "number")])
                    self._lv_did += 1
                blk.write([v])
                done += 1
                self.slog.add("apply", source, n, v)
            except Exception as ex:
                self.slog.add("fail", source, n, v, str(ex))
        return done

    # ------------------------------------------------------------------ bridge (WebSocket)
    def on_ws(self, msg, cid):
        t = msg.get("t")
        if t == "hello":
            self.panels[cid] = msg.get("panel") or "?"
            self.slog.add("info", "bridge", self.panels[cid], note="cockpit screen connected")
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
        if self.e.link:
            self.e._send({"t": "h", "n": n, "a": msg.get("a") or [], "g": msg.get("g")})
            self.slog.add("out", "button", n)
        else:
            self.slog.add("skip", "button", n, note="not connected")

    def _replay_h(self, msg):
        n = msg.get("n")
        self.slog.add("in", "button", n)
        if self.bridge_screens():
            self.bridge.broadcast({"t": "h", "n": n, "a": msg.get("a") or [], "g": msg.get("g")})
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
        elif t == "lv":
            vals = msg.get("v") or {}
            for n, v in vals.items():
                self.slog.add("in", "lvar", n, v)
            self._apply_lvars(vals)
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

    def on_disconnect(self):
        self.lv_out = {}
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
               "sw": dict(e.sw_prev),
               "lv": {n: v for n, v in self.lv_prev.items() if n not in self.lv_ignored},
               "ie": {n: v for n, v in p["ie"].items()
                      if not any(k in n.upper() for k in sd.INPUT_EVENT_SKIP)},
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
            if self._want_list and getattr(self.mf, "ready", False) and e.ready:
                self._want_list = False
                self.mf.list_lvars(self._set_lvars)
        if self.lv_out and t - self._lv_flush >= 0.05:
            self._lv_flush = t
            if e.link:
                e._send({"t": "lv", "v": self.lv_out})
                for n, v in self.lv_out.items():
                    self.slog.add("out", "lvar", n, v)
            self.lv_out = {}
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
