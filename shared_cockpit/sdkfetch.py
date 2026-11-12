"""Fetch the MSFS SDK's SimConnect.dll without installing the SDK.

The official Core installer is a zip holding a small .msi plus several .cab
archives. We:
  1. read the official sdk.json index to find the current Core installer zip,
  2. download only the .msi out of the zip (HTTP range requests),
  3. query its tables with Windows' built-in Windows Installer COM API to find
     which .cab holds "SimConnect SDK\\lib\\SimConnect.dll" and its key,
  4. download only that .cab and unpack the one file with Windows' expand.exe.
"""
import json
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zlib

from . import flavor

UA = {"User-Agent": "SharedCockpit/1.0"}
CHUNK = 1 << 20


class FetchError(RuntimeError):
    pass


def _get(url, rng=None, timeout=30):
    h = dict(UA)
    if rng:
        h["Range"] = "bytes=%d-%d" % rng
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


# ------------------------------------------------------------------ index
def core_zip_url():
    with _get(flavor.SDK_INDEX) as r:
        idx = json.loads(r.read().decode("utf-8-sig"))
    base = flavor.SDK_INDEX.rsplit("/", 1)[0] + "/"
    for gv in idx.get("game_versions", []):
        item = gv.get("downloads_menu", {}).get("SDK Installer (Core)")
        if item and item.get("value"):
            return base + item["value"]
    raise FetchError("No Core installer listed in the SDK index")


# ------------------------------------------------------------------ remote zip
class RemoteZip:
    def __init__(self, url):
        self.url = url
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA),
                                    timeout=30) as r:
            self.size = int(r.headers["Content-Length"])
        tail_len = min(self.size, 65536)
        with _get(url, (self.size - tail_len, self.size - 1)) as r:
            tail = r.read()
        i = tail.rfind(b"PK\x05\x06")
        if i < 0:
            raise FetchError("Not a zip (no end-of-central-directory)")
        n, cdsize, cdoff = struct.unpack_from("<HII", tail, i + 10)
        with _get(url, (cdoff, cdoff + cdsize - 1)) as r:
            cd = r.read()
        self.entries, p = {}, 0
        for _ in range(n):
            meth = struct.unpack_from("<H", cd, p + 10)[0]
            csz, usz = struct.unpack_from("<II", cd, p + 20)
            fl, el, cl = struct.unpack_from("<HHH", cd, p + 28)
            off = struct.unpack_from("<I", cd, p + 42)[0]
            name = cd[p + 46:p + 46 + fl].decode("cp437")
            self.entries[name.rsplit("/", 1)[-1].lower()] = (name, meth, csz, usz, off)
            p += 46 + fl + el + cl

    def find(self, suffix):
        for k, v in self.entries.items():
            if k.endswith(suffix.lower()):
                return k
        return None

    def extract(self, key, dest, progress=None):
        name, meth, csz, usz, off = self.entries[key.lower()]
        with _get(self.url, (off, off + 29)) as r:
            hdr = r.read()
        fl, el = struct.unpack_from("<HH", hdr, 26)
        start = off + 30 + fl + el
        dec = zlib.decompressobj(-15) if meth == 8 else None
        if meth not in (0, 8):
            raise FetchError(f"Unsupported zip method {meth}")
        done = 0
        with _get(self.url, (start, start + csz - 1), timeout=60) as r, open(dest, "wb") as f:
            while True:
                buf = r.read(CHUNK)
                if not buf:
                    break
                done += len(buf)
                f.write(dec.decompress(buf) if dec else buf)
                if progress:
                    progress(done, csz)
            if dec:
                f.write(dec.flush())
        if os.path.getsize(dest) != usz:
            raise FetchError(f"{key}: size mismatch after download")
        return csz


# ------------------------------------------------------------------ msi tables
_PS = r"""
$ErrorActionPreference = 'Stop'
$i = New-Object -ComObject WindowsInstaller.Installer
$db = $i.GetType().InvokeMember('OpenDatabase','InvokeMethod',$null,$i,@($args[0],0))
function Q($sql, $n) {
  $v = $db.GetType().InvokeMember('OpenView','InvokeMethod',$null,$db,@($sql))
  $v.GetType().InvokeMember('Execute','InvokeMethod',$null,$v,$null) | Out-Null
  $rows = @()
  while ($true) {
    $r = $v.GetType().InvokeMember('Fetch','InvokeMethod',$null,$v,$null)
    if ($r -eq $null) { break }
    $row = @()
    for ($k = 1; $k -le $n; $k++) { $row += $r.GetType().InvokeMember('StringData','GetProperty',$null,$r,$k) }
    $rows += ,$row
  }
  $v.GetType().InvokeMember('Close','InvokeMethod',$null,$v,$null) | Out-Null
  return ,$rows
}
$out = @{
  file = Q "SELECT ``File``,``Component_``,``FileName``,``Sequence`` FROM ``File``" 4
  comp = Q "SELECT ``Component``,``Directory_`` FROM ``Component``" 2
  dir  = Q "SELECT ``Directory``,``Directory_Parent``,``DefaultDir`` FROM ``Directory``" 3
  media = Q "SELECT ``DiskId``,``LastSequence``,``Cabinet`` FROM ``Media``" 3
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


def msi_tables(msi_path):
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(_PS)
        ps = f.name
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps,
                            msi_path], capture_output=True, text=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        os.unlink(ps)
    if r.returncode != 0:
        raise FetchError("Reading the installer index failed: " + (r.stderr or r.stdout)[-400:])
    return json.loads(r.stdout)


def _long(name):
    return name.split("|")[-1]


def locate_dll(t):
    """Return (file_key, cabinet_name) for SimConnect SDK\\lib\\SimConnect.dll."""
    dirs = {d[0]: (d[1], d[2]) for d in t["dir"]}

    def path(d):
        parts = []
        while d in dirs:
            parent, default = dirs[d]
            target = default.split(":")[0]
            nm = _long(target)
            if nm not in (".", "SourceDir", "TARGETDIR") and d != "TARGETDIR":
                parts.append(nm)
            if not parent or parent == d:
                break
            d = parent
        return "\\".join(reversed(parts)).lower()

    comp_dir = {c[0]: c[1] for c in t["comp"]}
    best = None
    for key, comp, fname, seq in t["file"]:
        if _long(fname).lower() != "simconnect.dll":
            continue
        p = path(comp_dir.get(comp, ""))
        score = ("simconnect sdk" in p) * 2 + p.endswith("lib")
        if best is None or score > best[0]:
            best = (score, key, int(seq), p)
    if not best:
        raise FetchError("SimConnect.dll not listed in the installer")
    _, key, seq, _p = best
    media = sorted((int(m[1]), m[2]) for m in t["media"] if m[2])
    for last, cab in media:
        if seq <= last:
            return key, cab.lstrip("#")
    raise FetchError("Could not map SimConnect.dll to a cabinet")


# ------------------------------------------------------------------ main entry
def has_input_event_exports(dll_path):
    try:
        d = open(dll_path, "rb").read()
    except OSError:
        return False
    return b"SimConnect_SubscribeInputEvent" in d


def fetch(app_root, log=print, progress=None):
    """Download the SDK SimConnect.dll into <app_root>/sdk_dll. Returns its path."""
    log(f"Looking up the current {flavor.SIM} SDK on sdk.flightsimulator.com ...")
    url = core_zip_url()
    rz = RemoteZip(url)
    msi = rz.find(".msi")
    if not msi:
        raise FetchError("No .msi in the SDK zip")
    work = tempfile.mkdtemp(prefix="sc_sdk_")
    try:
        msi_path = os.path.join(work, "core.msi")
        rz.extract(msi, msi_path)
        key, cab = locate_dll(msi_tables(msi_path))
        if cab.lower() not in rz.entries:
            raise FetchError(f"{cab} not found in the SDK zip")
        mb = rz.entries[cab.lower()][2] / 1e6
        log(f"SimConnect.dll is in {cab}; downloading {mb:.0f} MB ...")
        cab_path = os.path.join(work, cab)
        rz.extract(cab, cab_path, progress)
        out = os.path.join(work, "x")
        os.makedirs(out)
        r = subprocess.run(["expand", cab_path, f"-F:{key}", out], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        got = os.path.join(out, key)
        if not os.path.exists(got):
            raise FetchError("expand.exe could not unpack the DLL: " + (r.stdout + r.stderr)[-300:])
        if not has_input_event_exports(got):
            raise FetchError("Downloaded DLL lacks the input-event API (unexpected SDK layout)")
        dest_dir = os.path.join(app_root, "sdk_dll", flavor.KEY)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "SimConnect.dll")
        shutil.copyfile(got, dest)
        log(f"Installed {dest}")
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)
