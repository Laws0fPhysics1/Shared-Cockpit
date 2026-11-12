"""Post-install steps run by install.bat (console, no GUI):
detect the installed sim(s), fetch SimConnect for each, create shortcuts."""
import os
import subprocess
import sys

from . import flavor, sdkfetch
from .simconnect import dll_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "Shared Cockpit"


def _shortcut_paths():
    paths = []
    for env, sub in (("USERPROFILE", "Desktop"),
                     ("APPDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            # OneDrive-redirected desktops: ask the shell for the real path.
            if sub == "Desktop":
                try:
                    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                        "[Environment]::GetFolderPath('Desktop')"],
                                       capture_output=True, text=True, timeout=20)
                    if r.stdout.strip():
                        paths.append(os.path.join(r.stdout.strip(), NAME + ".lnk"))
                        continue
                except (OSError, subprocess.SubprocessError):
                    pass
            paths.append(os.path.join(base, sub, NAME + ".lnk"))
    return paths


def make_shortcuts():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    run = os.path.join(ROOT, "run.py")
    def q(s):  # PowerShell single-quoted literal
        return "'" + s.replace("'", "''") + "'"
    for lnk in _shortcut_paths():
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
              f"$s.TargetPath={q(pyw)};$s.Arguments={q(chr(34) + run + chr(34))};"
              f"$s.WorkingDirectory={q(ROOT)};$s.Description='Shared cockpit for MSFS';$s.Save()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True)
        print(("  shortcut: " + lnk) if r.returncode == 0 else ("  shortcut failed: " + r.stderr[-200:]))


def remove_shortcuts():
    for lnk in _shortcut_paths():
        try:
            os.remove(lnk)
            print("  removed " + lnk)
        except OSError:
            pass


def setup():
    from .app import load_settings, save_settings
    found = flavor.detect_installed()
    print("Simulators found: " + (", ".join(flavor.LABELS[k] for k in found) or
                                  "none (dry-run mode available for testing)"))
    s = load_settings()
    if s.get("sim") not in flavor.FLAVORS:
        s["sim"] = found[0] if found else "dry"
    for key in found:
        flavor.select(key)
        have = next((p for p in dll_candidates() if os.path.exists(p)), None)
        if have and sdkfetch.has_input_event_exports(have):
            print(f"{flavor.SIM}: SimConnect already present.")
            continue
        last = [0]

        def prog(done, total):
            pct = int(done * 100 / max(total, 1))
            if pct >= last[0] + 10 or done == total:
                last[0] = pct
                print(f"  {pct:3d}%  ({done / 1e6:.0f} of {total / 1e6:.0f} MB)", flush=True)
        try:
            sdkfetch.fetch(ROOT, log=lambda m: print("  " + m, flush=True), progress=prog)
            s.setdefault("sdk_prompted", []).append(key)
        except Exception as e:
            print(f"  Couldn't fetch SimConnect for {flavor.SIM} ({e}). The app will offer it again.")
    save_settings(s)
    print("Creating shortcuts...")
    make_shortcuts()
