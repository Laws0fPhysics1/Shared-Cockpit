"""Point the installer and auto-updater at your GitHub repo.

Usage:  python tools/set_repo.py owner/name
        python tools/set_repo.py https://github.com/owner/name.git
"""
import os
import re
import sys

if len(sys.argv) != 2:
    sys.exit(__doc__)
arg = sys.argv[1].strip().strip('"')
m = (re.fullmatch(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", arg)
     or re.fullmatch(r"git@github\.com:([\w.-]+)/([\w.-]+?)(?:\.git)?", arg)
     or re.fullmatch(r"([\w.-]+)/([\w.-]+?)(?:\.git)?", arg))
if not m:
    sys.exit(f"Not a GitHub repo: {arg}\n{__doc__}")
repo = f"{m.group(1)}/{m.group(2)}"
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for rel, pat, rep in (("install.bat", r'set "REPO=[^"]*"', f'set "REPO={repo}"'),
                      ("shared_cockpit/version.py", r'REPO = "[^"]*"', f'REPO = "{repo}"')):
    p = os.path.join(root, rel)
    with open(p, "rb") as f:
        s = f.read().decode()
    s2, n = re.subn(pat, rep, s)
    if n != 1:
        sys.exit(f"pattern not found in {rel}")
    if s2 != s:
        with open(p, "wb") as f:
            f.write(s2.encode())
        print(f"{rel}: REPO = {repo}")
