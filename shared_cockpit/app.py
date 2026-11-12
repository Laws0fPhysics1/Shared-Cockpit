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
            "sdk_prompted": [], "mirror_fms": True}

DRY_SWITCHES = [  # (label, simvar)
    ("Gear down", "GEAR HANDLE POSITION"),
    ("Landing light", "LIGHT LANDING"),
    ("Beacon", "LIGHT BEACON"),
    ("Nav lights", "LIGHT NAV"),
    ("Parking brake", "BRAKE PARKING POSITION"),
    ("Autopilot", "AUTOPILOT MASTER"),
    ("APU master (L:var)", "L:XMLVAR_OVHD_APU_MASTER"),
    ("Seatbelts (L:var)", "L:XMLVAR_OVHD_SEATBELTS"),
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
        self.root.geometry("720x700")
        self.root.minsize(580, 560)
        self.engine = Engine(self.log, self.s["name"])
        self.engine.cp.mirror_fms = bool(self.s.get("mirror_fms", True))
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

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, **pad)
        fly = ttk.Frame(self.nb)
        self.nb.add(fly, text="Flight")
        pre = ttk.Frame(self.nb)
        self.nb.add(pre, text="Pre-flight")
        if flavor.is_dry():
            dry = ttk.Frame(self.nb)
            self.nb.add(dry, text="Dry run")
        self.tab_insp = ttk.Frame(self.nb)
        self.nb.add(self.tab_insp, text="Sync inspector")
        lf = ttk.Frame(self.nb)
        self.nb.add(lf, text="Log")

        # ---- Flight tab
        top = ttk.LabelFrame(fly, text="Connection")
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

        ctl = ttk.LabelFrame(fly, text="Controls")
        ctl.pack(fill="x", **pad)
        self.l_pf = tk.Label(ctl, text="", font=("Segoe UI", 13, "bold"))
        self.l_pf.pack(side="left", padx=10, pady=6)
        ttk.Button(ctl, text="Give controls", command=self.engine.give_controls).pack(side="right", padx=6)
        ttk.Button(ctl, text="Take controls", command=self.engine.take_controls).pack(side="right", padx=6)

        ck = ttk.LabelFrame(fly, text="Cockpit")
        ck.pack(fill="x", **pad)
        ttk.Button(ck, text="Match co-pilot's aircraft", command=self.engine.match_copilot
                   ).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(ck, text="Copies their switches, overhead, fuel and engines (cold & dark vs running).",
                  foreground="#555").grid(row=0, column=1, sticky="w")
        ttk.Button(ck, text="Copy co-pilot's flight", command=self._copy_flight
                   ).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(ck, text="Loads their saved flight: position, systems, weather preset, time.",
                  foreground="#555").grid(row=1, column=1, sticky="w")
        self.v_mirror = tk.IntVar(value=1 if self.s.get("mirror_fms", True) else 0)
        ttk.Checkbutton(ck, text="Mirror captain and first officer FMS (a key on either goes to both)",
                        variable=self.v_mirror, command=self._mirror_changed
                        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))

        st = ttk.LabelFrame(fly, text="Status")
        st.pack(fill="x", **pad)
        self.lbl = {}
        for i, (k, t) in enumerate([("sim", "Simulator"), ("net", "Network"), ("rtt", "Ping"),
                                    ("motion", "Motion link"), ("env", "Weather / sim rate"),
                                    ("aircraft", "Your aircraft"), ("peer_aircraft", "Co-pilot aircraft"),
                                    ("ie", "Input events"), ("bridge", "Buttons / overhead")]):
            ttk.Label(st, text=t + ":").grid(row=i, column=0, sticky="w", padx=8)
            self.lbl[k] = ttk.Label(st, text="")
            self.lbl[k].grid(row=i, column=1, sticky="w", padx=8)

        # ---- Pre-flight tab
        self._build_checklist(pre, pad)
        if flavor.is_dry():
            self._build_dry(dry, pad)
        self._build_inspector(self.tab_insp, pad)

        # ---- Log tab
        self.t_log = tk.Text(lf, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.t_log.yview)
        self.t_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.t_log.pack(fill="both", expand=True)
        self._mode_changed()
        self._update_sdk_row()

    def _build_checklist(self, parent, pad):
        ttk.Label(parent, text="Before a shared flight - green is checked automatically, grey you check yourself.",
                  foreground="#555").pack(anchor="w", **pad)
        self.ck_frame = ttk.Frame(parent)
        self.ck_frame.pack(fill="x", **pad)
        self.ck_rows = []
        br = ttk.LabelFrame(parent, text="In-sim bridge (every button, FMS keys, overhead)")
        br.pack(fill="x", **pad)
        self.l_bridge = ttk.Label(br, text="checking...", wraplength=560, justify="left")
        self.l_bridge.pack(anchor="w", padx=8, pady=4)
        row = ttk.Frame(br)
        row.pack(anchor="w", padx=8, pady=(0, 6))
        self.b_bridge = ttk.Button(row, text="Install / repair bridge", command=self._install_bridge)
        self.b_bridge.pack(side="left")
        b_rm = ttk.Button(row, text="Remove bridge", command=self._remove_bridge)
        b_rm.pack(side="left", padx=8)
        if flavor.is_dry():
            self.b_bridge.configure(state="disabled")
            b_rm.configure(state="disabled")

    def _build_inspector(self, parent, pad):
        top = ttk.Frame(parent)
        top.pack(fill="x", **pad)
        self.l_counts = ttk.Label(top, text="", font=("Consolas", 9), justify="left")
        self.l_counts.pack(side="left", anchor="w")
        ttk.Button(top, text="Open log file", command=self._open_synclog).pack(side="right")
        cols = ("time", "dir", "kind", "name", "value", "note")
        self.tv = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        for c, w in zip(cols, (70, 45, 55, 210, 80, 220)):
            self.tv.heading(c, text=c)
            self.tv.column(c, width=w, stretch=(c in ("name", "note")))
        self.tv.tag_configure("fail", foreground="#b00020")
        self.tv.tag_configure("skip", foreground="#8a6d00")
        self.tv.tag_configure("in", foreground="#1a5fb4")
        sb = ttk.Scrollbar(parent, command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv.pack(fill="both", expand=True, **pad)
        self._insp_last = None

    def _build_dry(self, df, pad):
        ttk.Label(df, text="Pretend cockpit - changes sync to your co-pilot's pretend cockpit.",
                  foreground="#555").grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        self.l_pos = ttk.Label(df, text="", font=("Consolas", 9))
        self.l_pos.grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        for i, (label, var) in enumerate(DRY_SWITCHES):
            v = tk.IntVar()
            ttk.Checkbutton(df, text=label, variable=v,
                            command=lambda var=var, v=v: self._dry_set(var, v.get())
                            ).grid(row=2 + i // 3, column=i % 3, sticky="w", padx=8)
            self._dry_vars[var] = v
        r = 2 + (len(DRY_SWITCHES) + 2) // 3
        ttk.Label(df, text="COM1").grid(row=r, column=0, sticky="w", padx=8, pady=2)
        self.v_com = tk.StringVar()
        cb = ttk.Combobox(df, textvariable=self.v_com, values=DRY_COM, state="readonly", width=9)
        cb.grid(row=r, column=1, sticky="w", padx=8, pady=2)
        cb.bind("<<ComboboxSelected>>",
                lambda e: self._dry_set("COM ACTIVE FREQUENCY:1", round(float(self.v_com.get()) * 1e6)))
        fms = ttk.LabelFrame(df, text="Pretend FMS (key presses sync like the real MCDU)")
        fms.grid(row=r + 1, column=0, columnspan=4, sticky="we", padx=8, pady=6)
        self.l_scratch = tk.Label(fms, text="", font=("Consolas", 14, "bold"), width=28, anchor="w",
                                  bg="#111", fg="#7CFC00")
        self.l_scratch.grid(row=0, column=0, columnspan=8, sticky="w", padx=6, pady=4)
        for i, k in enumerate(list("ABCDEKLS") + list("0123") + ["CLR"]):
            ttk.Button(fms, text=k, width=4, command=lambda k=k: self._dry_key(k)
                       ).grid(row=1 + i // 8, column=i % 8, padx=1, pady=1)

    def _dry_key(self, k):
        sc = self._dry_sim()
        if not sc:
            return
        name = f"A320_Neo_CDU_1_BTN_{k}"
        sc.press_h(name)                                   # this cockpit
        self.engine.inbox.put(("ws", {"t": "h", "n": name}, "dry"))   # as if the bridge saw it

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
            self._bridge_check(offer=True)
        else:
            self._bridge_check()
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

    # ------------------------------------------------------------ cockpit / bridge
    def _copy_flight(self):
        if not self.engine.link:
            self.log("Connect to your co-pilot first.")
            return
        if messagebox.askyesno("Copy co-pilot's flight",
                               "Your sim will load your co-pilot's flight (position, aircraft systems, "
                               "fuel, weather preset, time) and show a loading screen.\n\n"
                               "Their FMS isn't part of a saved flight: program both FMSes together "
                               "afterwards (key presses sync).\n\nLoad it now?"):
            self.engine.copy_copilot_flight()

    def _mirror_changed(self):
        on = bool(self.v_mirror.get())
        self.engine.cp.mirror_fms = on
        self.s["mirror_fms"] = on
        save_settings(self.s)

    def _bridge_check(self, offer=False):
        if flavor.is_dry():
            self.l_bridge.configure(text="Not needed for a dry run.")
            return

        def go():
            try:
                from . import bridge_install
                st = bridge_install.status(flavor.KEY)
            except Exception as e:
                st = {"message": f"Couldn't check the bridge: {e}"}
            self.root.after(0, lambda: self._bridge_show(st, offer))
        threading.Thread(target=go, daemon=True).start()

    def _bridge_show(self, st, offer=False):
        self._bridge_status = st
        lines = [st.get("message") or ""]
        if st.get("bridge_installed") and st.get("bridge_current") is False:
            lines.append("MSFS updated its cockpit files: click Install / repair to refresh the bridge.")
        if st.get("conflicts"):
            lines.append("Conflicts: " + ", ".join(st["conflicts"]))
        self.l_bridge.configure(text="\n".join(x for x in lines if x))
        need = st.get("sim_found") and (not st.get("bridge_installed") and st.get("capture_supported")
                                        or st.get("bridge_current") is False or not st.get("mobiflight"))
        if st.get("bridge_installed") and st.get("bridge_current") is False and not getattr(self, "_auto_repaired", False):
            self._auto_repaired = True           # our own add-on: refresh it without asking
            self.log("Updating the in-sim bridge to this version. Restart MSFS afterwards.")
            self._install_bridge()
            return
        key = f"bridge_prompted_{flavor.KEY}"
        if offer and need and not self.s.get(key):
            self.s[key] = True
            save_settings(self.s)
            if messagebox.askyesno(
                    "In-sim bridge",
                    "Install the in-sim bridge into MSFS's Community folder?\n\n"
                    "It lets every cockpit button and FMS key press sync, plus the overhead panel. "
                    "It adds two add-ons (Shared Cockpit Bridge, MobiFlight event module, MIT licence). "
                    "Restart MSFS afterwards.\n\nYou can remove it any time on the Pre-flight tab."):
                self._install_bridge()

    def _install_bridge(self):
        self.b_bridge.configure(state="disabled")

        def go():
            try:
                from . import bridge_install
                st = bridge_install.install(flavor.KEY, log=self.log)
                if st.get("changed") or st.get("restart_needed"):
                    self.log("In-sim bridge installed. Restart MSFS so it loads it.")
            except Exception as e:
                self.log(f"Bridge install failed: {e}")
            self.root.after(0, lambda: (self.b_bridge.configure(state="normal"), self._bridge_check()))
        threading.Thread(target=go, daemon=True).start()

    def _remove_bridge(self):
        if not messagebox.askyesno("Remove bridge", "Remove the Shared Cockpit Bridge add-on from MSFS's "
                                   "Community folder? (MobiFlight's module is left in place.)"):
            return
        try:
            from . import bridge_install
            bridge_install.uninstall(flavor.KEY, log=self.log)
        except Exception as e:
            self.log(f"Couldn't remove the bridge: {e}")
        self._bridge_check()

    def _open_synclog(self):
        p = self.engine.cp.slog.path
        if p and os.path.exists(p):
            os.startfile(p)

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
            self.l_scratch.configure(text=f"CPT {sc.scratchpad or '_':<8} FO {sc.scratchpad2 or '_'}")
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 7 == 0:
            self._refresh_checklist()
        if self._tick_n % 3 == 0 and self.nb.select() == str(self.tab_insp):
            self._refresh_inspector()
        self.root.after(150, self._refresh)

    def _refresh_checklist(self):
        items = self.engine.cp.checklist()
        while len(self.ck_rows) < len(items):
            r = len(self.ck_rows)
            mark = tk.Label(self.ck_frame, text="", font=("Segoe UI", 11, "bold"), width=2)
            mark.grid(row=r, column=0, sticky="w")
            name = ttk.Label(self.ck_frame, text="")
            name.grid(row=r, column=1, sticky="w", padx=4)
            det = ttk.Label(self.ck_frame, text="", foreground="#555", wraplength=380)
            det.grid(row=r, column=2, sticky="w", padx=8)
            self.ck_rows.append((mark, name, det))
        for (mark, name, det), (label, ok, detail) in zip(self.ck_rows, items):
            mark.configure(text={True: "\u2714", False: "\u2716", None: "\u25CB"}[ok],
                           fg={True: "#1a7f37", False: "#b00020", None: "#777"}[ok])
            name.configure(text=label)
            det.configure(text=detail)

    def _refresh_inspector(self):
        slog = self.engine.cp.slog
        ev = slog.recent(200)
        sig = (len(ev), ev[-1][0] if ev else 0)
        if sig != self._insp_last:
            self._insp_last = sig
            self.tv.delete(*self.tv.get_children())
            for t, d, k, n, v, note in reversed(ev):
                self.tv.insert("", "end", values=(time.strftime("%H:%M:%S", time.localtime(t)), d, k, n, v, note),
                               tags=(d,))
        lines = []
        for kind, c in slog.summary().items():
            lines.append(f"{kind:7s} sent {c['out']:5d}  received {c['in']:5d}  applied {c['apply']:5d}  "
                         f"failed {c['fail']:4d}  skipped {c['skip']:4d}")
        self.l_counts.configure(text="\n".join(lines) or "No sync traffic yet.")

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
