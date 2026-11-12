"""In-sim bridge installer, against a fake MSFS packages tree (no downloads, no real sim).

Run:  python tests/test_bridge_install.py
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import bridge_install as bi  # noqa: E402

SAMPLE = '''class VCockpitPanel extends HTMLElement {}
Coherent.on("OnInteractionEvent", function (_target, _args) {
    var panel = window.document.getElementById("panel");
});
'''

# 1. build_override: original kept, hook appended once, port substituted
o = bi.build_override(SAMPLE, 51234)
assert o.startswith(SAMPLE.rstrip("\n")), "original must be kept verbatim"
assert bi.MARKER in o and "ws://127.0.0.1:51234" in o and "__SC_PORT__" not in o
o2 = bi.build_override(o, 50556)                       # regenerate on an already-hooked file
assert o2.count(bi.MARKER) == 1 and "ws://127.0.0.1:50556" in o2
print("OK build_override (original verbatim, single hook, port set)")

# 2. fake sim tree
tmp = tempfile.mkdtemp(prefix="sc_bi_")
pk = os.path.join(tmp, "Packages")
core = os.path.join(pk, "Official", "Steam", bi.CORE_PACKAGE, *bi.VCOCKPIT_REL)
os.makedirs(os.path.dirname(core))
with open(core, "w", encoding="utf-8") as f:
    f.write(SAMPLE)
com = os.path.join(pk, "Community")
os.makedirs(com)
cfg = os.path.join(tmp, "UserCfg.opt")
with open(cfg, "w", encoding="utf-8") as f:
    f.write(f'Version 1\nInstalledPackagesPath "{pk}"\n')
bi.find_usercfg = lambda key: cfg if key == "2020" else None

assert bi.find_packages_path("2020") == pk
assert bi.find_community("2020") == com
assert bi.find_vcockpit_js("2020") == core
st = bi.status("2020")
assert st["sim_found"] and st["capture_supported"] and not st["bridge_installed"], st
assert bi.status("2024")["community"] is None                  # missing sim: no exception
print("OK status on a fake tree (nothing installed; missing sim handled)")

# 3. install with MobiFlight download mocked
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("mobiflight-event-module/manifest.json", "{}")
    z.writestr("mobiflight-event-module/layout.json", '{"content": []}')
    z.writestr("mobiflight-event-module/modules/MobiFlightWasmModule.wasm", b"\0asm")
fetched = []
st = bi.install("2020", port=50556, log=lambda m: None, fetch=lambda url: fetched.append(url) or buf.getvalue())
assert st["bridge_installed"] and st["bridge_current"] and st["mobiflight"] and st["changed"], st
assert fetched == [bi.MOBIFLIGHT_URL]
pkg = os.path.join(com, bi.BRIDGE_NAME)
with open(os.path.join(pkg, *bi.VCOCKPIT_REL), encoding="utf-8") as f:
    assert f.read() == bi.build_override(SAMPLE, 50556)
lay = json.load(open(os.path.join(pkg, "layout.json")))
paths = {e["path"]: e for e in lay["content"]}
assert "html_ui/Pages/VCockpit/Core/VCockpit.js" in paths and "bridge.json" in paths
assert "layout.json" not in paths and "manifest.json" not in paths
e = paths["html_ui/Pages/VCockpit/Core/VCockpit.js"]
assert e["size"] == os.path.getsize(os.path.join(pkg, *bi.VCOCKPIT_REL))
assert e["date"] > 116444736000000000                   # FILETIME, after 1970
man = json.load(open(os.path.join(pkg, "manifest.json")))
assert man["content_type"] == "MISC" and man["title"] == "Shared Cockpit Bridge"
print("OK install: bridge + layout.json (FILETIME dates) + manifest + MobiFlight")

# 4. reinstall is a no-op for MobiFlight; a sim update makes the bridge stale
st = bi.install("2020", log=lambda m: None, fetch=lambda url: (_ for _ in ()).throw(AssertionError("no re-download")))
assert not st["changed"], st
with open(core, "a", encoding="utf-8") as f:
    f.write("// sim update\n")
st = bi.status("2020")
assert st["bridge_installed"] and not st["bridge_current"] and "update" in st["message"], st
st = bi.install("2020", log=lambda m: None, fetch=lambda url: b"")
assert st["bridge_current"] and st["changed"], st
print("OK stale detection after a sim update, and reinstall regenerates")

# 5. conflicts (another add-on overriding VCockpit.js) and an existing MobiFlight kept
other = os.path.join(com, "YourControls", *bi.VCOCKPIT_REL)
os.makedirs(os.path.dirname(other))
open(other, "w").close()
st = bi.status("2020")
assert st["conflicts"] == ["YourControls"] and "YourControls" in st["message"], st
print("OK conflict detected:", st["conflicts"])

# 6. uninstall removes only our folder
assert bi.uninstall("2020", log=lambda m: None)
assert not os.path.exists(pkg)
assert os.path.isdir(os.path.join(com, bi.MOBIFLIGHT_NAME)) and os.path.isdir(os.path.join(com, "YourControls"))
print("OK uninstall removes only shared-cockpit-bridge")

# 7. packed-UI sim (like MSFS 2024): no loose VCockpit.js -> capture unsupported, still no error
os.remove(core)
st = bi.status("2020")
assert not st["capture_supported"] and "isn't available" in st["message"], st
st = bi.install("2020", log=lambda m: None, fetch=lambda url: b"")
assert not st["bridge_installed"]
print("OK sim without a loose cockpit file: capture reported unavailable, no failure")

# 8. hook JS is valid JavaScript (node --check), when node is available
node = shutil.which("node")
if node:
    js = os.path.join(tmp, "check.js")
    with open(js, "w", encoding="utf-8") as f:
        f.write(bi.build_override(SAMPLE, 50556))
    r = subprocess.run([node, "--check", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    print("OK node --check: original + hook parses")
else:
    print("SKIP node --check (node not installed)")

shutil.rmtree(tmp, ignore_errors=True)
print("\nBRIDGE INSTALL TESTS PASSED")
