"""Shared Cockpit window (tkinter)."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import messagebox, ttk

from . import flavor, sdkfetch, updater
from .version import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, "settings.json")
DEFAULTS = {"name": "Pilot", "mode": "host", "ip": "", "port": 50555, "sim": "",
            "sdk_prompted": []}

DRY_SWITCHES = [  # (label, simvar)
    ("Gear down", "GEAR HANDLE POSITION"),
    ("Landing light", "LIGHT LANDING"),
    ("Beacon", "LIGHT BEACON"),
    ("Nav lights", "LIGHT NAV"),
    ("Parking brake", "BRAKE PARKING POSITION"),
    ("Autopilot", "AUTOPILOT MASTER"),
]
DRY_COM = ["118.000", "121.500", "122.800", "124.350", "127.100"]


def load_settings():
    try:
        with open(SETTINGS) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(s):
    try:
        with open(SETTINGS, "w") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


def restart_app():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py")], cwd=ROOT, creationflags=flags)


class App:
    def __init__(self):
        self.s = load_settings()
        if self.s["sim"] not in flavor.FLAVORS:
            found = flavor.detect_installed()
            self.s["sim"] = found[0] if found else "dry"
            save_settings(self.s)
        flavor.select(self.s["sim"])

        from .engine import Engine  # after flavor.select
        self.logq = queue.Queue()
        self.root = tk.Tk()
        self.root.title(f"Shared Cockpit {__version__} - {flavor.SIM}")
        self.root.geometry("680x700")
        self.root.minsize(580, 560)
        self.engine = Engine(self.log, self.s["name"])
        self._dry_vars = {}
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(150, self._refresh)
        self.root.after(600, self._startup_checks)

    # ------------------------------------------------------------ layout
    def _build(self):
        pad = {"padx": 8, "pady": 3}
        simf = ttk.LabelFrame(self.root, text="Simulator")
        simf.pack(fill="x", **pad)
        self.v_sim = tk.StringVar(value=flavor.LABELS[flavor.KEY])
        cb = ttk.Combobox(simf, textvariable=self.v_sim, state="readonly", width=22,
                          values=[flavor.LABELS[k] for k in ("2024", "2020", "dry")])
        cb.pack(side="left", padx=8, pady=6)
        cb.bind("<<ComboboxSelected>>", self._sim_changed)
        self.b_sdk = ttk.Button(simf, text="Get SimConnect from Microsoft", command=self._fetch_sdk)
        self.l_sdk = ttk.Label(simf, text="")
        self.l_sdk.pack(side="left", padx=8)
        self.pb = ttk.Progressbar(simf, length=140, mode="determinate")

        top = ttk.LabelFrame(self.root, text="Connection")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Your name").grid(row=0, column=0, sticky="w", **pad)
        self.v_name = tk.StringVar(value=self.s["name"])
        ttk.Entry(top, textvariable=self.v_name, width=18).grid(row=0, column=1, sticky="w", **pad)
        self.v_mode = tk.StringVar(value=self.s["mode"])
        ttk.Radiobutton(top, text="Host (I port-forward)", value="host", variable=self.v_mode,
                        command=self._mode_changed).grid(row=1, column=0, sticky="w", **pad)
        ttk.Radiobutton(top, text="Join a host", value="join", variable=self.v_mode,
                        command=self._mode_changed).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(top, text="Host IP").grid(row=2, column=0, sticky="w", **pad)
        self.v_ip = tk.StringVar(value=self.s["ip"])
        self.e_ip = ttk.Entry(top, textvariable=self.v_ip, width=22)
        self.e_ip.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(top, text="Port").grid(row=2, column=2, sticky="e", **pad)
        self.v_port = tk.StringVar(value=str(self.s["port"]))
        ttk.Entry(top, textvariable=self.v_port, width=8).grid(row=2, column=3, sticky="w", **pad)
        bar = ttk.Frame(top)
        bar.grid(row=3, column=0, columnspan=4, sticky="w", **pad)
        self.b_go = ttk.Button(bar, text="Start hosting", command=self._go)
        self.b_go.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Disconnect", command=self.engine.stop_net).pack(side="left", padx=6)
        ttk.Button(bar, text="Show my public IP", command=self._public_ip).pack(side="left", padx=6)

        st = ttk.LabelFrame(self.root, text="Status")
        st.pack(fill="x", **pad)
        self.lbl = {}
        for i, (k, t) in enumerate([("sim", "Simulator"), ("net", "Network"), ("rtt", "Ping"),
                                    ("motion", "Motion link"), ("env", "Weather / sim rate"),
                                    ("aircraft", "Your aircraft"), ("peer_aircraft", "Co-pilot aircraft"),
                                    ("ie", "Cockpit sync")]):
            ttk.Label(st, text=t + ":").grid(row=i, column=0, sticky="w", padx=8)
            self.lbl[k] = ttk.Label(st, text="")
            self.lbl[k].grid(row=i, column=1, sticky="w", padx=8)

        ctl = ttk.LabelFrame(self.root, text="Controls")
        ctl.pack(fill="x", **pad)
        self.l_pf = tk.Label(ctl, text="", font=("Segoe UI", 13, "bold"))
        self.l_pf.pack(side="left", padx=10, pady=6)
        ttk.Button(ctl, text="Give controls", command=self.engine.give_controls).pack(side="right", padx=6)
        ttk.Button(ctl, text="Take controls", command=self.engine.take_controls).pack(side="right", padx=6)

        if flavor.is_dry():
            self._build_dry(pad)

        lf = ttk.LabelFrame(self.root, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.t_log = tk.Text(lf, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.t_log.yview)
        self.t_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.t_log.pack(fill="both", expand=True)
        self._mode_changed()
        self._update_sdk_row()

    def _build_dry(self, pad):
        df = ttk.LabelFrame(self.root, text="Dry run - pretend cockpit (changes sync to your co-pilot)")
        df.pack(fill="x", **pad)
        self.l_pos = ttk.Label(df, text="", font=("Consolas", 9))
        self.l_pos.grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        for i, (label, var) in enumerate(DRY_SWITCHES):
            v = tk.IntVar()
            ttk.Checkbutton(df, text=label, variable=v,
                            command=lambda var=var, v=v: self._dry_set(var, v.get())
                            ).grid(row=1 + i // 3, column=i % 3, sticky="w", padx=8)
            self._dry_vars[var] = v
        ttk.Label(df, text="COM1").grid(row=3, column=0, sticky="w", padx=8, pady=2)
        self.v_com = tk.StringVar()
        cb = ttk.Combobox(df, textvariable=self.v_com, values=DRY_COM, state="readonly", width=9)
        cb.grid(row=3, column=1, sticky="w", padx=8, pady=2)
        cb.bind("<<ComboboxSelected>>",
                lambda e: self._dry_set("COM ACTIVE FREQUENCY:1", round(float(self.v_com.get()) * 1e6)))

    def _dry_sim(self):
        sc = self.engine.sc
        return sc if sc is not None and hasattr(sc, "set_var") else None

    def _dry_set(self, var, value):
        sc = self._dry_sim()
        if sc:
            sc.set_var(var, value)

    def _mode_changed(self):
        host = self.v_mode.get() == "host"
        self.e_ip.configure(state="disabled" if host else "normal")
        self.b_go.configure(text="Start hosting" if host else "Connect")

    # ------------------------------------------------------------ simulator / SDK
    def _sim_changed(self, _e=None):
        key = {v: k for k, v in flavor.LABELS.items()}[self.v_sim.get()]
        if key == flavor.KEY:
            return
        if self.engine.link and not messagebox.askyesno(
                "Switch simulator", "This disconnects your co-pilot and restarts the app. Continue?"):
            self.v_sim.set(flavor.LABELS[flavor.KEY])
            return
        self.s["sim"] = key
        save_settings(self.s)
        self.restart()

    def _dll_state(self):
        """(path or None, has_input_events)"""
        from .simconnect import dll_candidates
        for p in dll_candidates():
            if os.path.exists(p):
                return p, sdkfetch.has_input_event_exports(p)
        return None, False

    def _update_sdk_row(self):
        self.b_sdk.pack_forget()
        if flavor.is_dry():
            self.l_sdk.configure(text="No game needed - a pretend airplane is simulated.")
            return
        path, ie = self._dll_state()
        if path and ie:
            self.l_sdk.configure(text="SimConnect: full cockpit sync ready")
        else:
            self.b_sdk.pack(side="left", padx=4, before=self.l_sdk)
            self.l_sdk.configure(text="SimConnect: basic sync only" if path else "SimConnect: missing")

    def _fetch_sdk(self):
        self.b_sdk.configure(state="disabled")
        self.pb.pack(side="left", padx=4)

        def prog(done, total):
            self.root.after(0, lambda: self.pb.configure(maximum=total, value=done))

        def go():
            try:
                sdkfetch.fetch(ROOT, log=self.log, progress=prog)
                ok = True
            except Exception as e:  # network, COM, expand... show it, keep going
                self.log(f"Couldn't fetch SimConnect: {e}. You can install the {flavor.SIM} SDK "
                         "manually (Developer Mode > Help > SDK); it'll be found automatically.")
                ok = False
            self.root.after(0, lambda: self._fetch_done(ok))
        threading.Thread(target=go, daemon=True).start()

    def _fetch_done(self, ok):
        self.pb.pack_forget()
        self.b_sdk.configure(state="normal")
        self._update_sdk_row()
        if ok:
            if self.engine.link:
                self.log("SimConnect installed. Restart the app (after your flight) to use it.")
            else:
                self.log("SimConnect installed - restarting to load it...")
                self.root.after(800, self.restart)

    # ------------------------------------------------------------ startup checks
    def _startup_checks(self):
        if not flavor.is_dry():
            path, ie = self._dll_state()
            if not ie and flavor.KEY not in self.s.get("sdk_prompted", []):
                self.s.setdefault("sdk_prompted", []).append(flavor.KEY)
                save_settings(self.s)
                if messagebox.askyesno(
                        "Full cockpit sync",
                        f"Download SimConnect for {flavor.SIM} from Microsoft's SDK server?\n\n"
                        "One-time download (about 60-90 MB). It enables syncing of most cockpit "
                        "switches. Nothing gets installed in Windows."):
                    self._fetch_sdk()
        threading.Thread(target=self._check_update, daemon=True).start()

    def _check_update(self):
        if not updater.configured():
            return
        try:
            new = updater.update_available()
        except Exception as e:
            self.log(f"Update check skipped ({e.__class__.__name__}).")
            return
        if new:
            self.root.after(0, lambda: self._offer_update(new))

    def _offer_update(self, new):
        if updater.is_dev_checkout(ROOT):
            self.log(f"Version {new} is on GitHub (this is a git checkout - use git pull).")
            return
        if not messagebox.askyesno("Update available",
                                   f"Shared Cockpit {new} is available (you have {__version__}).\n\n"
                                   "Install it now? The app restarts; your settings are kept."):
            self.log(f"Update {new} skipped.")
            return

        def go():
            try:
                updater.apply_update(ROOT, log=self.log)
                self.root.after(0, self.restart)
            except Exception as e:
                self.log(f"Update failed: {e}")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ actions
    def _go(self):
        try:
            port = int(self.v_port.get())
            assert 1 <= port <= 65535
        except (ValueError, AssertionError):
            self.log("Port must be a number 1-65535.")
            return
        self.engine.name = self.v_name.get().strip() or "Pilot"
        self.s.update(name=self.engine.name, mode=self.v_mode.get(), ip=self.v_ip.get().strip(), port=port)
        save_settings(self.s)
        if self.v_mode.get() == "host":
            try:
                self.engine.host(port)
            except OSError as e:
                self.log(f"Could not open port {port}: {e}")
        else:
            ip = self.v_ip.get().strip()
            if not ip:
                self.log("Enter the host's IP address.")
                return
            self.engine.join(ip, port)

    def _public_ip(self):
        def go():
            try:
                with urllib.request.urlopen("https://api.ipify.org", timeout=6) as r:
                    ip = r.read().decode().strip()
                self.log(f"Your public IP is {ip} - give your friend this and port {self.v_port.get()}.")
            except OSError as e:
                self.log(f"Couldn't look up public IP: {e}")
        threading.Thread(target=go, daemon=True).start()

    # ------------------------------------------------------------ log / refresh
    def log(self, msg):
        self.logq.put(time.strftime("%H:%M:%S ") + msg)

    def _refresh(self):
        try:
            while True:
                line = self.logq.get_nowait()
                self.t_log.configure(state="normal")
                self.t_log.insert("end", line + "\n")
                self.t_log.see("end")
                self.t_log.configure(state="disabled")
        except queue.Empty:
            pass
        st = self.engine.status
        for k, w in self.lbl.items():
            w.configure(text=st.get(k, ""))
        if self.engine.link:
            me = self.engine.i_have_controls()
            self.l_pf.configure(text="YOU HAVE CONTROLS" if me else "CO-PILOT HAS CONTROLS",
                                fg="#1a7f37" if me else "#b35900")
        else:
            self.l_pf.configure(text="Not connected (you fly normally)", fg="#555555")
        sc = self._dry_sim()
        if sc:
            self.l_pos.configure(text="Pretend plane: %.5f, %.5f  alt %5.0f ft  hdg %03.0f  %s" % (
                sc.get("PLANE LATITUDE"), sc.get("PLANE LONGITUDE"), sc.get("PLANE ALTITUDE"),
                sc.get("PLANE HEADING DEGREES TRUE"),
                "(FROZEN - following co-pilot)" if sc.frozen() else "(flying itself in circles)"))
            for var, v in self._dry_vars.items():
                want = 1 if sc.get(var) >= 0.5 else 0
                if v.get() != want:
                    v.set(want)
            com = "%.3f" % (sc.get("COM ACTIVE FREQUENCY:1") / 1e6)
            if self.v_com.get() != com:
                self.v_com.set(com)
        self.root.after(150, self._refresh)

    def restart(self):
        self.engine.shutdown()
        restart_app()
        self.root.after(200, self.root.destroy)

    def quit(self):
        self.engine.shutdown()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.log(f"Shared Cockpit {__version__}. " + (
            "Dry run: no game needed. Host or join to test the connection." if flavor.is_dry() else
            f"Start {flavor.SIM}, load the SAME aircraft as your co-pilot, then Host or Join."))
        self.root.mainloop()


def main():
    App().run()
