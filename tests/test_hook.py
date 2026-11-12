"""Runs the real cockpit hook (bridge_hook.js) in Node with a pretend panel and
Coherent, and checks: targeted presses are reported with the instrument's element
name (GUIDs differ between PCs), only by the panel holding that instrument;
replays reach the matching instrument only; untargeted replays reach all.

Run:  python tests/test_hook.py      (skipped if node isn't installed)
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
node = shutil.which("node")
if not node:
    print("node not installed - skipped\nHOOK TESTS PASSED")
    sys.exit(0)

hook = open(os.path.join(ROOT, "shared_cockpit", "bridge_hook.js"), encoding="utf-8").read()
hook = hook.replace("__SC_PORT__", "50556")

harness = r"""
var sent = [], handlers = [], sockets = [];
function Instr(tag, guid) {
  this.tagName = tag.toUpperCase(); this.got = [];
  this.getAttribute = function (k) { return k === "Guid" ? guid : null; };
  this.onInteractionEvent = function (a) { this.got.push(a.join("|")); };
}
var cdu = new Instr("a320-neo-cdu-main-display", "111"), fcu = new Instr("a320-neo-fcu", "222");
var panel = { children: [cdu, fcu] };
global.window = { document: { getElementById: function () { return panel; } } };
global.globalPanelData = { sName: "MAIN PANEL" };
global.Coherent = { on: function (n, f) { if (n === "OnInteractionEvent") handlers.push(f); } };
global.WebSocket = function (url) { this.url = url; sockets.push(this); var s = this;
  this.send = function (d) { sent.push(JSON.parse(d)); };
  setTimeout(function () { s.onopen && s.onopen(); }, 0); };
global.setTimeout = setTimeout;
""" + hook + r"""
setTimeout(function () {
  var fire = function (t, a) { handlers.forEach(function (h) { h(t, a); }); };
  fire("222", ["A320_Neo_FCU_VS_INC"]);          // targeted at the FCU (this panel has it)
  fire("999", ["SOMETHING_ELSE"]);               // targeted at another panel's instrument
  fire(null, ["A320_Neo_CDU_1_BTN_A"]);          // broadcast
  var ws = sockets[0];
  ws.onmessage({ data: JSON.stringify({ t: "h", n: "A320_Neo_FCU_VS_DEC", a: [], i: "a320-neo-fcu" }) });
  ws.onmessage({ data: JSON.stringify({ t: "h", n: "A320_Neo_CDU_1_BTN_B", a: [], i: null }) });
  console.log(JSON.stringify({ sent: sent, cdu: cdu.got, fcu: fcu.got, handlers: handlers.length }));
  process.exit(0);
}, 10);
"""
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "h.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(harness)
    out = subprocess.run([node, p], capture_output=True, text=True, timeout=30)
if out.returncode != 0:
    print(out.stderr)
    raise SystemExit("node run failed")
import json  # noqa: E402
r = json.loads(out.stdout.strip().splitlines()[-1])
sent = [m for m in r["sent"] if m.get("t") == "h"]
assert r["handlers"] == 1, "hook must add exactly one extra listener"
assert {"t": "h", "n": "A320_Neo_FCU_VS_INC", "a": [], "i": "a320-neo-fcu"} in sent, sent
assert all(m["n"] != "SOMETHING_ELSE" for m in sent), "other panel's instrument reported here"
assert {"t": "h", "n": "A320_Neo_CDU_1_BTN_A", "a": [], "i": None} in sent, sent
assert any(m.get("t") == "hello" and m.get("v") == 3 and m.get("inst") == ["a320-neo-cdu-main-display", "a320-neo-fcu"] for m in r["sent"])
print("OK presses reported with the instrument's name, only by the panel that holds it")
assert r["fcu"] == ["A320_Neo_FCU_VS_DEC", "A320_Neo_CDU_1_BTN_B"], r["fcu"]
assert r["cdu"] == ["A320_Neo_CDU_1_BTN_B"], r["cdu"]
print("OK replays: targeted -> matching instrument only; untargeted -> every instrument")
print("\nHOOK TESTS PASSED")
