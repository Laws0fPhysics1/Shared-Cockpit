"""In-sim experiment tool: read, watch, write and pulse L:vars over SimConnect
(no MobiFlight needed), to check how an aircraft treats its own variables
before writing a profile class for them. Prints plain lines to paste back.

Names: "FOO" or "L:FOO" is an L:var; a name with a space ("LIGHT LANDING") or
"A:NAME" is a normal sim variable (unit "number"). Numeric variables only.

Usage (MSFS running, in a flight; from the app folder):
  python tools/probe.py [--sim 2020|2024] read NAME [NAME ...]
  python tools/probe.py watch NAME [NAME ...] [--secs 10]     every change, per sim frame
  python tools/probe.py write NAME VALUE                      write, then read back after 0.5 s
  python tools/probe.py press NAME [VALUE] [--rest 0] [--ms 100]
                                    VALUE (default 1) for --ms, then --rest; prints what the sim showed
  python tools/probe.py knob VALUE_VAR STEP_VAR [--up 1 --down -1 --rest 0] [--ms 100]
                                    one up pulse, one down pulse (measures the step), then a direct
                                    write of value + one step: did it stick or get reverted?
  e.g. python tools/probe.py knob ASCRJ_FCP_ALT_INFO ASCRJ_FCP_ALT_CHANGE
Add --dry to run against the built-in pretend CRJ (fakesim crj_mode) instead of MSFS.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared_cockpit import flavor  # noqa: E402
from shared_cockpit.simconnect import (Block, FLAG_CHANGED, PERIOD_NEVER, PERIOD_ONCE,  # noqa: E402
                                       PERIOD_SIM_FRAME, SimConnect, SimConnectError)

DEF0 = 900


def var(name):
    n = name.strip()
    if n[:2].upper() == "L:":
        return "L:" + n[2:].strip()
    if n[:2].upper() == "A:":
        return n[2:].strip()
    return n if " " in n else "L:" + n


def fmt(v):
    return f"{v:g}" if isinstance(v, float) else str(v)


def fmt_d(d):
    return ("+" if d > 0 else "") + fmt(d)


class Probe:
    def __init__(self, sc):
        self.sc = sc
        self.next_id = DEF0
        self.blocks = {}           # tuple(names) -> Block (one definition per name set)
        self.data = {}             # request id -> latest values

    def block(self, names):
        key = tuple(names)
        if key not in self.blocks:
            self.blocks[key] = Block(self.sc, self.next_id, [(n, "number") for n in names])
            self.next_id += 1
        return self.blocks[key]

    def pump(self, secs=0.0, on_data=None):
        end = time.perf_counter() + secs
        while True:
            for m in self.sc.poll():
                if m[0] == "data":
                    for key, blk in self.blocks.items():
                        if blk.define_id == m[1]:
                            vals = blk.unpack(m[2])
                            self.data[m[1]] = vals
                            if on_data:
                                on_data(key, vals)
                elif m[0] == "exception":
                    print(f"  sim exception {m[1]} (packet {m[2]}, index {m[3]})")
                elif m[0] == "quit":
                    raise SystemExit("MSFS closed")
            if time.perf_counter() >= end:
                return
            time.sleep(0.002)

    def read(self, names):
        blk = self.block(names)
        self.data.pop(blk.define_id, None)
        self.sc.request(blk.define_id, blk.define_id, PERIOD_ONCE)
        end = time.perf_counter() + 3.0
        while blk.define_id not in self.data and time.perf_counter() < end:
            self.pump(0.02)
        vals = self.data.get(blk.define_id)
        if vals is None:
            raise SystemExit("no answer from the sim (is a flight loaded?)")
        return vals

    def write(self, name, value):
        self.block([name]).write([float(value)])

    def watch(self, names, secs, quiet=False):
        """Per-frame readings for `secs`. Returns [(t, values)] of every change."""
        blk = self.block(names)
        seen = []
        t0 = time.perf_counter()

        def on(key, vals):
            if key == tuple(names) and (not seen or vals != seen[-1][1]):
                seen.append((time.perf_counter() - t0, vals))
                if not quiet:
                    print(f"  {seen[-1][0]:7.3f}s  " + "  ".join(f"{n}={fmt(v)}" for n, v in zip(names, vals)))
        self.sc.request(blk.define_id, blk.define_id, PERIOD_SIM_FRAME, FLAG_CHANGED, interval=0)
        self.pump(secs, on)
        self.sc.request(blk.define_id, blk.define_id, PERIOD_NEVER)
        return seen

    def press(self, name, value, rest, ms, watch_s=1.0):
        """value for ms, then rest; returns the per-frame readings seen meanwhile."""
        blk = self.block([name])
        seen = []
        t0 = time.perf_counter()

        def on(key, vals):
            if key == (name,) and (not seen or vals[0] != seen[-1][1]):
                seen.append((time.perf_counter() - t0, vals[0]))
        self.sc.request(blk.define_id, blk.define_id, PERIOD_SIM_FRAME, FLAG_CHANGED, interval=0)
        self.write(name, value)
        self.pump(ms / 1000.0, on)
        self.write(name, rest)
        self.pump(watch_s, on)
        self.sc.request(blk.define_id, blk.define_id, PERIOD_NEVER)
        return seen


def settle(p, name, secs=1.0):
    """Read `name` until it has stopped changing for 0.3 s (at most `secs` more)."""
    last, since = p.read([name])[0], time.perf_counter()
    end = since + secs
    while time.perf_counter() < end:
        p.pump(0.05)
        v = p.read([name])[0]
        if v != last:
            last, since = v, time.perf_counter()
        elif time.perf_counter() - since >= 0.3:
            break
    return last


def cmd_knob(p, a):
    val, step = var(a.args[0]), var(a.args[1])
    v0 = settle(p, val, 0.5)
    print(f"{val} = {fmt(v0)}, {step} = {fmt(p.read([step])[0])}")
    p.press(step, a.up, a.rest, a.ms)
    v_up = settle(p, val)
    d_up = v_up - v0
    print(f"up pulse ({step}={fmt(a.up)} for {a.ms} ms): {fmt(v0)} -> {fmt(v_up)}")
    p.press(step, a.down, a.rest, a.ms)
    v_dn = settle(p, val)
    d_dn = v_dn - v_up
    print(f"down pulse ({step}={fmt(a.down)} for {a.ms} ms): {fmt(v_up)} -> {fmt(v_dn)}")
    target = v_dn + (d_up if d_up else 1.0)
    p.write(val, target)
    p.pump(0.6)
    v_w = settle(p, val, 0.5)
    if abs(v_w - target) < 1e-6:
        res = "direct write stuck"
        p.write(val, v_dn)                          # put it back
        p.pump(0.3)
    elif abs(v_w - v_dn) < 1e-6:
        res = "direct write reverted"
    else:
        res = f"direct write of {fmt(target)} ended at {fmt(v_w)}"
    print(f"direct write {val} = {fmt(target)}: read back {fmt(v_w)}")
    print(f"RESULT {val}: {res}; up pulse {fmt_d(d_up)}, down pulse {fmt_d(d_dn)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Shared Cockpit in-sim L:var probe",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--sim", choices=("2020", "2024"), default="2024")
    ap.add_argument("--dry", action="store_true", help="use the built-in pretend CRJ instead of MSFS")
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--rest", type=float, default=0.0)
    ap.add_argument("--ms", type=int, default=100)
    ap.add_argument("--up", type=float, default=1.0)
    ap.add_argument("--down", type=float, default=-1.0)
    ap.add_argument("command", choices=("read", "watch", "write", "press", "knob"))
    ap.add_argument("args", nargs="+")
    a = ap.parse_args(argv)

    if a.dry:
        flavor.select("dry")
        from shared_cockpit.fakesim import FakeSim
        sc = FakeSim(crj_mode=True)
    else:
        flavor.select(a.sim)
        try:
            sc = SimConnect("Shared Cockpit probe")
            sc.open()
        except (SimConnectError, OSError) as ex:
            raise SystemExit(f"Can't connect to {flavor.SIM}: {ex}")
    print(f"probe: {flavor.SIM} via {sc.dll_path}")
    p = Probe(sc)
    try:
        p.pump(0.2)
        if a.command == "read":
            names = [var(n) for n in a.args]
            for n, v in zip(names, p.read(names)):
                print(f"{n} = {fmt(v)}")
        elif a.command == "watch":
            names = [var(n) for n in a.args]
            print(f"watching {len(names)} for {a.secs:g} s (every sim frame, changes only)")
            seen = p.watch(names, a.secs)
            print(f"RESULT {len(seen)} distinct readings")
        elif a.command == "write":
            if len(a.args) != 2:
                ap.error("write NAME VALUE")
            n = var(a.args[0])
            before = p.read([n])[0]
            p.write(n, float(a.args[1]))
            p.pump(0.5)
            after = p.read([n])[0]
            stuck = abs(after - float(a.args[1])) < 1e-6
            print(f"RESULT {n}: {fmt(before)} -> wrote {a.args[1]} -> read {fmt(after)} "
                  f"({'stuck' if stuck else 'NOT stuck'})")
        elif a.command == "press":
            n = var(a.args[0])
            value = float(a.args[1]) if len(a.args) > 1 else 1.0
            before = p.read([n])[0]
            seen = p.press(n, value, a.rest, a.ms)
            for t, v in seen:
                print(f"  {t:7.3f}s  {n}={fmt(v)}")
            after = p.read([n])[0]
            print(f"RESULT {n}: {fmt(before)} -> {fmt(value)} for {a.ms} ms -> {fmt(a.rest)}; "
                  f"sim showed {' '.join(fmt(v) for _, v in seen) or 'nothing'}; now {fmt(after)}")
        elif a.command == "knob":
            if len(a.args) != 2:
                ap.error("knob VALUE_VAR STEP_VAR")
            cmd_knob(p, a)
    finally:
        sc.close()


if __name__ == "__main__":
    main()
