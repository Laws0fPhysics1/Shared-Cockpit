"""Check GitHub for a newer version and update in place."""
import io
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile

from .version import BRANCH, REPO, __version__

UA = {"User-Agent": "SharedCockpit-updater"}
# Never overwritten by an update: user settings and downloaded SimConnect DLLs.
KEEP = {"settings.json", "sdk_dll", "SimConnect.dll", ".git"}


def configured():
    return bool(REPO) and not REPO.startswith("OWNER/")


def _vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_version(timeout=6):
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/shared_cockpit/version.py"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        txt = r.read().decode("utf-8", "replace")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    if not m:
        raise RuntimeError("version not found in remote file")
    return m.group(1)


def update_available():
    """Returns the newer version string, or None."""
    if not configured():
        return None
    v = latest_version()
    return v if _vt(v) > _vt(__version__) else None


def is_dev_checkout(app_root):
    return os.path.isdir(os.path.join(app_root, ".git"))


def apply_update(app_root, log=print):
    url = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
    log(f"Downloading {url} ...")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        data = r.read()
    tmp = tempfile.mkdtemp(prefix="sc_upd_")
    try:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        roots = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if len(roots) != 1 or not os.path.exists(os.path.join(tmp, roots[0], "run.py")):
            raise RuntimeError("unexpected archive layout")
        src = os.path.join(tmp, roots[0])
        for name in os.listdir(src):
            if name in KEEP:
                continue
            s, d = os.path.join(src, name), os.path.join(app_root, name)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        log("Update installed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
