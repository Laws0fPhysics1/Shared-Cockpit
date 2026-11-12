"""Updater test with a fake GitHub archive (no network).

Run:  python tests/test_updater.py
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import updater  # noqa: E402

# version compare
updater.REPO = "someone/shared-cockpit"
updater.latest_version = lambda timeout=6: "1.2.0"
assert updater.update_available() == "1.2.0"
updater.latest_version = lambda timeout=6: updater.__version__
assert updater.update_available() is None
print("OK version check")

# fake install dir with user data that must survive
inst = tempfile.mkdtemp()
os.makedirs(os.path.join(inst, "shared_cockpit"))
os.makedirs(os.path.join(inst, "sdk_dll", "2024"))
for rel, txt in (("run.py", "old"), ("shared_cockpit/engine.py", "old"),
                 ("settings.json", '{"name": "Me"}'), ("sdk_dll/2024/SimConnect.dll", "dll")):
    with open(os.path.join(inst, rel), "w") as f:
        f.write(txt)

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("shared-cockpit-main/run.py", "new")
    z.writestr("shared-cockpit-main/shared_cockpit/engine.py", "new")
    z.writestr("shared-cockpit-main/shared_cockpit/newfile.py", "new")
    z.writestr("shared-cockpit-main/settings.json", "{}")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


updater.urllib.request.urlopen = lambda *a, **k: Resp(buf.getvalue())
updater.apply_update(inst, log=lambda m: None)


def rd(rel):
    with open(os.path.join(inst, rel)) as f:
        return f.read()


assert rd("run.py") == "new" and rd("shared_cockpit/engine.py") == "new"
assert rd("shared_cockpit/newfile.py") == "new"
assert rd("settings.json") == '{"name": "Me"}', "settings must be kept"
assert rd("sdk_dll/2024/SimConnect.dll") == "dll", "DLLs must be kept"
shutil.rmtree(inst)
print("OK update applied; settings + SimConnect kept")
print("\nUPDATER TESTS PASSED")
