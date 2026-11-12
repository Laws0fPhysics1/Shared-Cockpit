"""Sync log: what was sent, received and applied, for the inspector and for bug hunting.

Keeps the last N events in memory (the GUI's Sync inspector reads them),
counts them per kind/direction, and appends everything to
logs/sync-<date>-<time>.log next to the app (motion packets are not logged).
"""
import os
import threading
import time
from collections import Counter, deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SyncLog:
    def __init__(self, keep=600, to_file=True):
        self.events = deque(maxlen=keep)       # (time, dir, kind, name, value, note)
        self.counts = Counter()                # (kind, dir) -> n
        self.lock = threading.Lock()
        self.path = None
        self._f = None
        if to_file:
            try:
                d = os.path.join(ROOT, "logs")
                os.makedirs(d, exist_ok=True)
                self.path = os.path.join(d, time.strftime("sync-%Y%m%d-%H%M%S.log"))
                self._f = open(self.path, "a", encoding="utf-8", buffering=1)
                self._prune(d)
            except OSError:
                self._f = None

    @staticmethod
    def _prune(d, keep=20):
        try:
            logs = sorted(f for f in os.listdir(d) if f.startswith("sync-") and f.endswith(".log"))
            for f in logs[:-keep]:
                os.remove(os.path.join(d, f))
        except OSError:
            pass

    def add(self, direction, kind, name="", value="", note=""):
        """direction: 'out' (sent to co-pilot), 'in' (received), 'apply' (done locally),
        'fail', 'skip' or 'info'."""
        t = time.time()
        if isinstance(value, float):
            value = f"{value:.6g}"
        with self.lock:
            self.events.append((t, direction, kind, str(name), str(value), note))
            self.counts[(kind, direction)] += 1
        if self._f:
            try:
                self._f.write(f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} "
                              f"{direction:5s} {kind:6s} {name} = {value} {note}\n")
            except OSError:
                pass

    def recent(self, n=200):
        with self.lock:
            return list(self.events)[-n:]

    def summary(self):
        with self.lock:
            c = dict(self.counts)
        kinds = sorted({k for k, _ in c})
        return {k: {d: c.get((k, d), 0) for d in ("out", "in", "apply", "fail", "skip")} for k in kinds}

    def close(self):
        if self._f:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
