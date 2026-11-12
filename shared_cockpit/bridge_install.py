"""In-sim bridge for Shared Cockpit (Community folder packages).

Two packages go into the sim's Community folder:

* shared-cockpit-bridge: overrides html_ui/Pages/VCockpit/Core/VCockpit.js with
  the user's OWN copy of that file plus our hook (bridge_hook.js) appended. The
  hook reports every cockpit interaction (H: events: MCDU keys, buttons) to the
  app over a localhost WebSocket and replays the co-pilot's presses into the
  local instruments. Generated at install time from the installed sim's file,
  so Asobo's code is never redistributed and always matches the sim version.
* mobiflight-event-module: MobiFlight's WASM module (MIT licence), used to list
  the aircraft's L:vars and run calculator code. Skipped if already installed.

Stdlib only. Flavor keys "2020" / "2024" as in flavor.py.
"""
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile

BRIDGE_NAME = "shared-cockpit-bridge"
BRIDGE_VERSION = "1.1.0"   # bump whenever bridge_hook.js changes: installed copies then show "update"
DEFAULT_PORT = 50556
MOBIFLIGHT_NAME = "mobiflight-event-module"
MOBIFLIGHT_URL = ("https://github.com/MobiFlight/MobiFlight-WASM-Module/releases/download/"
                  "1.0.1/mobiflight-event-module.1.0.1.zip")
VCOCKPIT_REL = ("html_ui", "Pages", "VCockpit", "Core", "VCockpit.js")
CORE_PACKAGE = "asobo-vcockpits-core"
MARKER = "\n// ---- Shared Cockpit bridge ----\n"
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_FILE = os.path.join(HERE, "bridge_hook.js")


# ------------------------------------------------------------------ locating the sim
def _usercfg_candidates(key):
    ad = os.environ.get("APPDATA", "")
    la = os.environ.get("LOCALAPPDATA", "")
    if key == "2020":
        return [os.path.join(ad, "Microsoft Flight Simulator", "UserCfg.opt"),
                os.path.join(la, "Packages", "Microsoft.FlightSimulator_8wekyb3d8bbwe",
                             "LocalCache", "UserCfg.opt")]
    if key == "2024":
        return [os.path.join(ad, "Microsoft Flight Simulator 2024", "UserCfg.opt"),
                os.path.join(la, "Packages", "Microsoft.Limitless_8wekyb3d8bbwe",
                             "LocalCache", "UserCfg.opt")]
    return []


def find_usercfg(key):
    for p in _usercfg_candidates(key):
        if os.path.isfile(p):
            return p
    return None


def find_packages_path(key):
    """InstalledPackagesPath from the sim's UserCfg.opt, or None."""
    cfg = find_usercfg(key)
    if not cfg:
        return None
    try:
        with open(cfg, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*InstalledPackagesPath\s+"?([^"\r\n]+?)"?\s*$', line)
                if m:
                    p = m.group(1).strip()
                    return p if os.path.isdir(p) else None
    except OSError:
        return None
    return None


def find_community(key):
    pk = find_packages_path(key)
    if not pk:
        return None
    c = os.path.join(pk, "Community")
    return c if os.path.isdir(c) else None


def find_vcockpit_js(key):
    """The sim's original VCockpit.js (loose file), or None when the sim packs it
    (MSFS 2024 ships its core UI inside archives, so there is no file to hook)."""
    pk = find_packages_path(key)
    if not pk:
        return None
    official = [d for d in ("Official", "Official2020", "Official2024")
                if os.path.isdir(os.path.join(pk, d))]
    for o in official:
        base = os.path.join(pk, o)
        for store in ("Steam", "OneStore"):
            p = os.path.join(base, store, CORE_PACKAGE, *VCOCKPIT_REL)
            if os.path.isfile(p):
                return p
    return None


# ------------------------------------------------------------------ building the package
def hook_source(port=DEFAULT_PORT):
    with open(HOOK_FILE, encoding="utf-8") as f:
        return f.read().replace("__SC_PORT__", str(int(port)))


def build_override(original_text, port=DEFAULT_PORT):
    """Original VCockpit.js + our hook appended (any earlier hook is stripped first)."""
    base = original_text.split(MARKER, 1)[0]
    return base.rstrip("\n") + "\n" + MARKER + hook_source(port)


def _filetime(ts):
    """Unix seconds -> Windows FILETIME (100 ns since 1601), as MSFS layouts use."""
    return int(ts * 10_000_000) + 116444736000000000


def build_layout(pkg_dir):
    """layout.json content for every file in the package (except layout/manifest)."""
    entries = []
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, pkg_dir).replace(os.sep, "/")
            if rel in ("layout.json", "manifest.json"):
                continue
            st = os.stat(full)
            entries.append({"path": rel, "size": st.st_size, "date": _filetime(st.st_mtime)})
    entries.sort(key=lambda e: e["path"])
    return {"content": entries}


MANIFEST = {
    "dependencies": [],
    "content_type": "MISC",
    "title": "Shared Cockpit Bridge",
    "manufacturer": "",
    "creator": "Shared Cockpit",
    "package_version": BRIDGE_VERSION,
    "minimum_game_version": "1.0.0",
    "release_notes": {},
}


def _sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_bridge(community, original_path, port=DEFAULT_PORT):
    """Write/refresh Community/shared-cockpit-bridge from the sim's original file."""
    with open(original_path, encoding="utf-8", errors="replace") as f:
        original = f.read()
    pkg = os.path.join(community, BRIDGE_NAME)
    target = os.path.join(pkg, *VCOCKPIT_REL)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_override(original, port))
    with open(os.path.join(pkg, "bridge.json"), "w", encoding="utf-8") as f:
        json.dump({"source_sha256": _sha256_file(original_path), "port": int(port),
                   "version": BRIDGE_VERSION, "source": original_path}, f, indent=2)
    with open(os.path.join(pkg, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, indent=2)
    with open(os.path.join(pkg, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(build_layout(pkg), f, indent=2)
    return pkg


def _download(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "SharedCockpit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def install_mobiflight(community, log=print, fetch=None):
    """Install MobiFlight's WASM module unless a copy is already there. Returns True if present."""
    dest = os.path.join(community, MOBIFLIGHT_NAME)
    if os.path.isdir(dest):
        return True
    log("Downloading MobiFlight WASM module (MIT licence, ~0.5 MB)...")
    data = (fetch or _download)(MOBIFLIGHT_URL)
    tmp = tempfile.mkdtemp(prefix="sc_mf_")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():   # refuse odd paths
                if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                    raise RuntimeError(f"unsafe path in MobiFlight zip: {n}")
            z.extractall(tmp)
        src = os.path.join(tmp, MOBIFLIGHT_NAME)
        if not os.path.isfile(os.path.join(src, "manifest.json")):
            raise RuntimeError("unexpected MobiFlight zip layout")
        shutil.copytree(src, dest)
        log("MobiFlight WASM module installed.")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ public API
def status(key):
    """Never raises. Describes what's installed and what's missing."""
    st = {"sim": key, "sim_found": False, "community": None, "vcockpit": None,
          "capture_supported": False, "bridge_installed": False, "bridge_current": False,
          "mobiflight": False, "conflicts": [], "restart_needed": False, "message": ""}
    try:
        st["sim_found"] = find_usercfg(key) is not None
        com = find_community(key)
        st["community"] = com
        vc = find_vcockpit_js(key)
        st["vcockpit"] = vc
        st["capture_supported"] = vc is not None
        if not com:
            st["message"] = f"MSFS {key} Community folder not found."
            return st
        pkg = os.path.join(com, BRIDGE_NAME)
        st["bridge_installed"] = os.path.isfile(os.path.join(pkg, *VCOCKPIT_REL))
        st["mobiflight"] = os.path.isdir(os.path.join(com, MOBIFLIGHT_NAME))
        if st["bridge_installed"] and vc:
            try:
                with open(os.path.join(pkg, "bridge.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                st["bridge_current"] = (meta.get("source_sha256") == _sha256_file(vc)
                                        and meta.get("version") == BRIDGE_VERSION)
            except (OSError, ValueError):
                st["bridge_current"] = False
        for name in sorted(os.listdir(com)):
            if name == BRIDGE_NAME:
                continue
            if os.path.isfile(os.path.join(com, name, *VCOCKPIT_REL)):
                st["conflicts"].append(name)
        if not vc:
            st["message"] = (f"Button/FMS capture isn't available for MSFS {key}: the sim packs its "
                             "cockpit core files, so there's nothing to hook. Switch/L:var sync still works.")
        elif not st["bridge_installed"]:
            st["message"] = "In-sim bridge not installed."
        elif not st["bridge_current"]:
            st["message"] = ("The bridge needs a refresh (new Shared Cockpit version or MSFS update): "
                             "click Install / repair, then restart MSFS.")
        elif st["conflicts"]:
            st["message"] = ("Other add-ons also replace the cockpit core file (" + ", ".join(st["conflicts"]) +
                             "). Only one can win; remove them from Community while using Shared Cockpit.")
        else:
            st["message"] = "In-sim bridge installed."
    except Exception as e:  # status must never break the caller
        st["message"] = f"Couldn't check the in-sim bridge: {e}"
    return st


def install(key, port=DEFAULT_PORT, log=print, progress=None, fetch=None):
    """Install/refresh the bridge (+ MobiFlight) for one sim. Returns status(key) + 'changed'."""
    com = find_community(key)
    if not com:
        log(f"MSFS {key}: Community folder not found - skipping the in-sim bridge.")
        st = status(key)
        st["changed"] = False
        return st
    before = status(key)
    vc = find_vcockpit_js(key)
    changed = False
    if vc:
        write_bridge(com, vc, port)
        changed = not (before["bridge_installed"] and before["bridge_current"])
        log(f"MSFS {key}: in-sim bridge written to {os.path.join(com, BRIDGE_NAME)}")
    else:
        log(f"MSFS {key}: no loose cockpit core file - button/FMS capture not available for this sim.")
    try:
        had = os.path.isdir(os.path.join(com, MOBIFLIGHT_NAME))
        install_mobiflight(com, log=log, fetch=fetch)
        changed = changed or not had
    except Exception as e:
        log(f"MSFS {key}: couldn't install the MobiFlight module ({e}). L:var listing will be unavailable.")
    st = status(key)
    st["changed"] = changed
    st["restart_needed"] = changed
    if changed:
        log(f"MSFS {key}: restart MSFS (if it's running) to load the in-sim bridge.")
    return st


def uninstall(key, log=print):
    """Remove only our own package folder (MobiFlight is left alone: other tools use it)."""
    com = find_community(key)
    if not com:
        return False
    pkg = os.path.join(com, BRIDGE_NAME)
    if os.path.isdir(pkg):
        shutil.rmtree(pkg, ignore_errors=True)
        log(f"Removed {pkg}")
        return True
    return False
