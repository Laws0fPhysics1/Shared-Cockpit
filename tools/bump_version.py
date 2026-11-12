"""Bump __version__ in shared_cockpit/version.py.

Usage:  python tools/bump_version.py [patch|minor|major]   (default: patch)
Prints the new version.
"""
import os
import re
import sys

part = (sys.argv[1] if len(sys.argv) > 1 else "patch").lower()
if part not in ("patch", "minor", "major"):
    sys.exit(__doc__)
p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared_cockpit", "version.py")
s = open(p, encoding="utf-8").read()
m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', s)
if not m:
    sys.exit("version not found")
ma, mi, pa = map(int, m.groups())
if part == "major":
    ma, mi, pa = ma + 1, 0, 0
elif part == "minor":
    mi, pa = mi + 1, 0
else:
    pa += 1
new = f"{ma}.{mi}.{pa}"
open(p, "w", encoding="utf-8").write(s[:m.start()] + f'__version__ = "{new}"' + s[m.end():])
print(new)
