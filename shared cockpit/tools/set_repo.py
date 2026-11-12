"""Point the installer and auto-updater at your GitHub repo.

Usage:  python tools/set_repo.py yourname/shared-cockpit
"""
import os
import re
import sys

if len(sys.argv) != 2 or not re.fullmatch(r"[\w.-]+/[\w.-]+", sys.argv[1]):
    sys.exit(__doc__)
repo = sys.argv[1]
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for rel, pat, rep in (("install.bat", r'set "REPO=[^"]*"', f'set "REPO={repo}"'),
                      ("shared_cockpit/version.py", r'REPO = "[^"]*"', f'REPO = "{repo}"')):
    p = os.path.join(root, rel)
    with open(p, "rb") as f:
        s = f.read().decode()
    s2, n = re.subn(pat, rep, s)
    if n != 1:
        sys.exit(f"pattern not found in {rel}")
    with open(p, "wb") as f:
        f.write(s2.encode())
    print(f"{rel}: REPO = {repo}")
