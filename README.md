# Shared Cockpit

Fly one airplane with a friend in **Microsoft Flight Simulator 2024 or 2020**. One of you
hosts (port-forwards a TCP port); the other types in the host's IP and port. Whoever has the
controls flies. The other seat's airplane follows them exactly, and the cockpit syncs both ways:
switches, radios, autopilot, the overhead panel, and every button press, including FMS/MCDU keys.
Works with the Steam and Microsoft Store / Xbox app versions.

## Install (the easy way)
1. Download **[install.bat](install.bat)** (on GitHub: open it → *Download raw file*).
2. Double-click it. If Windows SmartScreen warns, click *More info → Run anyway*.

It installs Python if needed, downloads the app to `%LOCALAPPDATA%\SharedCockpit`,
detects MSFS 2020/2024, fetches SimConnect from Microsoft's SDK server (only the one DLL,
~60–90 MB, nothing installed in Windows), installs the **in-sim bridge** into MSFS's Community
folder (see below), and adds Desktop + Start Menu shortcuts. Restart MSFS after the first install.
Run it again any time to repair. To remove everything, run `install.bat /uninstall`.

The app checks GitHub for updates on launch and offers to install them.

## Fly together
1. Both: in MSFS set **Multiplayer: Off** (world map). Shared Cockpit syncs the plane itself;
   with MSFS multiplayer on you'd each see the other's plane sitting inside your own.
2. Both: load into a flight with the **same aircraft** (different liveries are fine).
3. Both: open **Shared Cockpit**, pick your sim in the *Simulator* dropdown (auto-detected) and
   check the **Pre-flight** tab: same aircraft, same weather, crash damage off, bridge connected.
4. **Host:** choose *Host*, port `50555`, click **Start hosting**, then **Show my public IP** and
   send your friend that IP and port.
5. **Friend:** choose *Join a host*, type the IP and port, click **Connect**.
6. **Line up the two cockpits** (Flight tab, *Cockpit* box). The friend clicks:
   - **Copy co-pilot's flight**: loads the host's saved flight (position, aircraft systems, fuel,
     weather preset, time). The best start: do it right after connecting.
   - **Match co-pilot's aircraft**: copies the host's switches, overhead, fuel and engine state
     (cold & dark vs running) without reloading. Engines use the sim's auto-start, which some
     aircraft ignore; then start them by hand.
7. The host starts with the controls. Use **Take controls** / **Give controls** to swap.

### The in-sim bridge (every button, FMS keys, overhead)
MSFS doesn't let an outside app see cockpit button presses, so the installer adds two small
add-ons to MSFS's Community folder (manage them on the **Pre-flight** tab):
- **Shared Cockpit Bridge.** A hook in the cockpit screens' code (`VCockpit.js`, generated from
  your own copy of the sim's file) reports every button press (H: events) to the app, and
  replays your co-pilot's presses. Pressing **A** on your MCDU types **A** on theirs.
- **MobiFlight event module** (MIT licence, from MobiFlight). Lists each aircraft's internal
  cockpit variables (L:vars, e.g. most of the A320neo overhead) so the app can sync them all.
  Values that change on their own (timers, animations) are detected and left alone.

FMS sync works by replaying key presses (like YourControls does), so both FMSes must start
the same: use **Copy co-pilot's flight**, then program the FMS together.

**Restart MSFS after installing or updating the bridge**: the sim only loads add-ons at startup.
The app refreshes its own bridge when a new version needs it, and the Pre-flight tab says so.

### How the cockpit is synced: causes, not just results
- **Button presses and knob turns are replayed as what they are.** Glass-cockpit buttons and FMS
  keys travel as button events (via the bridge); autopilot knobs and buttons that work through
  the sim's key commands (`AP_ALT_VAR_INC`, `HEADING_BUG_DEC`, `AP_MASTER`, ...) are watched and
  fired on the other sim. Turning the altitude knob up 10 notches turns it up 10 notches there.
- **Switch positions are copied as values** (lights, overhead, radios). Knob/key "input events"
  never are: their value isn't a position, and copying it re-fires the action.
- **No echoes.** A value or command that just arrived from your co-pilot is never sent back,
  even if the sim reports an older reading a frame later.
- **Autopilot selectors are synced by slot** (1 = selected, 3 = what Airbus FCUs display) and
  written the way the knob's own model code does it, so the altitude/heading/speed/VS bugs move
  on both screens. Values follow a turning knob after a 0.3 s pause.
- **Key commands are relayed only right after one of you touched a control.** Commands an
  aircraft's own code fires by itself (an A320 FMS does this a lot) stay local: both sims make them.
- **Aircraft whose autopilot panel is a cockpit screen** (Airbus FCU, Boeing MCP): generic autopilot
  mode switches aren't pushed, the aircraft's own logic runs on both sims. AP on/off still syncs.
- **Switches an aircraft flips by itself** (e.g. A320 automatic probe heat) are left alone.
- **Captain and first officer FMS are mirrored** (Flight tab checkbox, on by default): a key on
  either FMS goes to both FMS units on both PCs.
In MSFS 2024 the cockpit UI files are packed, so button/FMS capture is **2020-only for now**;
2024 still gets the overhead (L:vars) through MobiFlight. Another add-on that also replaces
`VCockpit.js` (e.g. YourControls) conflicts; the Pre-flight tab warns about it.

### Test without the game: Dry run
Pick **Dry run (no sim)** in the Simulator dropdown. A pretend airplane flies circles on the
host's side; when the friend joins, their pretend airplane freezes and follows. Flip the
pretend switches (gear, lights, autopilot, COM1, overhead L:vars) and type on the pretend
FMS keypad, and watch them change on the other PC. Use it to check port forwarding before a flight.

To try a bad connection, start the app with `SC_NETSIM` set, for example in a command prompt:
`set SC_NETSIM=delay=0.08,jitter=0.03,loss=0.05` then `python run.py`.

### Port forwarding (host only)
On your router, forward port `50555` for **both TCP and UDP** to your PC's local IP
(`ipconfig` → IPv4 Address). TCP alone works too. Motion then falls back to TCP, which is a
little less smooth on a bad connection; the *Motion link* line in the app shows which one is in use.
The first time you host, Windows Firewall asks: allow **Private and Public**. If it didn't
ask, run this once in an admin PowerShell:
```
New-NetFirewallRule -DisplayName "Shared Cockpit" -Direction Inbound -Protocol TCP -LocalPort 50555 -Action Allow
New-NetFirewallRule -DisplayName "Shared Cockpit UDP" -Direction Inbound -Protocol UDP -LocalPort 50555 -Action Allow
```

## How smooth sync works
- **UDP for motion.** Position updates go over UDP (no waiting on lost packets). Switches and
  handoffs go over TCP (guaranteed delivery). If UDP is blocked, it falls back to TCP automatically.
- **Clock sync.** Ping/pong measures the clock difference between the PCs (the same method NTP uses), so
  every packet's real age is known.
- **Adaptive buffer.** The other seat shows the pilot flying just far enough in the past to cover
  the network's delay spread: about 45 ms on a good link, more on a jittery one. It adjusts on its own.
- **Curved interpolation.** The path between packets follows the plane's real velocity (cubic
  Hermite), so turns stay round and lost packets are covered by prediction.
- **Seamless handoff.** *Take controls* fast-forwards your view to "live" over 1.2 s, then takes
  over exactly on a sim frame. The other pilot's plane keeps gliding until your packets arrive,
  then eases back to buffered playback. Fuel and payload are copied to whoever takes over.
- **Height above ground near the ground.** Below 100 ft the follower keeps the same height above
  *its own* runway, so different scenery elevations can't sink or float the wheels.
- **Weather check.** The app warns when your wind, pressure, temperature, visibility, rain/snow or
  sim rate differ, because those make the two flight models diverge after a handoff.

## What syncs
| | Direction |
|---|---|
| Position, attitude, velocities (100 ms interpolation buffer) | pilot flying → other seat |
| Yoke/rudder/brakes, throttle/mixture/prop levers | pilot flying → other seat |
| Gear, flaps, spoilers, parking brake, trims | both ways |
| Exterior/panel/cabin lights, battery, alternator, avionics, pitot heat | both ways |
| COM1/2, NAV1/2 (active + standby), OBS, transponder, baro | both ways |
| Autopilot modes and HDG/ALT/VS/SPD selectors | both ways |
| Every cockpit button press, incl. FMS/MCDU keys (in-sim bridge, MSFS 2020) | both ways |
| Aircraft-specific switches/knobs, e.g. the A320neo overhead (L:vars via MobiFlight) | both ways |
| Cockpit "input events" (B: vars, needs the SDK SimConnect.dll) | both ways |
| Whole aircraft state on demand: **Match co-pilot's aircraft** | on click |
| Saved flight incl. weather preset and time: **Copy co-pilot's flight** | on click |

## Tips / known limits
- **Turn off crash damage** on the non-flying side (the frozen plane is teleported every frame).
- Non-flying pilot: hands off the yoke/throttle.
- **Weather can't be forced.** MSFS removed every weather-setting function from SimConnect,
  so no app can set it. Both pick Live Weather (or the same preset; *Copy co-pilot's flight*
  brings a preset along). The Pre-flight tab compares the two.
- **ATC isn't synced.** No API exists for it; let one pilot handle ATC.
- **Something didn't sync?** The **Sync inspector** tab lists every event sent, received, applied,
  failed or skipped, and everything goes to `logs/sync-*.log` too. Variables that should never
  sync can be listed in `shared_cockpit/lvar_ignore.txt` (one regex per line).
- **Restart MSFS before switching aircraft.** MobiFlight lists at most 1000 cockpit variables, and
  variables from aircraft flown earlier stay in the list until the sim restarts. The app re-checks the
  list for about a minute after an aircraft loads, since some add-ons (e.g. the Aerosoft CRJ) create
  their variables late. It also watches variables named in an aircraft profile
  (`shared_cockpit/profiles/*.json`), ones the co-pilot sends, and ones that synced before
  (`lvar_cache.json`, kept across updates).
- Study-level add-ons (PMDG, Fenix, FBW, ...) keep state in their own systems; expect partial switch
  sync unless the aircraft has a profile.
- **Aircraft profiles** (`shared_cockpit/profiles/*.json`, picked by aircraft title) tell the app how an
  add-on works. Each L:var is a *state* (mirror the value), a *press* (momentary button, sent once
  per press) or *ignore* (animation/display). *Knobs* sync their real value: the other side writes it,
  or turns its own knob until it matches. A profile can also turn off generic switches the aircraft
  owns (`switches_off`), the autopilot key relay (`k_relay`), and input events (`input_events_off`).
  The **Aerosoft CRJ** profile ships with v1.3.0. Both pilots need v1.3.0 (protocol 4).
- **Checking a profile in the sim:** with the aircraft loaded (no co-pilot needed), run for example
  `python tools/probe.py --sim 2020 knob ASCRJ_FCP_ALT_INFO ASCRJ_FCP_ALT_CHANGE`. It tells you
  whether the aircraft accepts a direct write and how far one knob pulse moves the value. The other
  commands are `read`, `watch` (prints every change while you click), `write` and `press`.
- MSFS 2024 has a known slight jitter when an app places an aircraft every frame (YourControls has
  the same issue).

## For developers
- Run from source: `python run.py` (Python 3.10+, standard library only).
- Tests (no sim needed): `python tests/test_loopback.py`, `test_dryrun.py`, `test_smooth.py`
  (measures smoothness under simulated packet loss and lag), `test_updater.py`, `test_cockpit.py`
  (L:vars, FMS keys, Match, Copy flight, checklist), `test_wsbridge.py`, `test_mobiflight.py`,
  `test_bridge_install.py`, `test_lvar_discovery.py` (late L:vars, list cap, profiles, cache), `test_crj_profile.py` (pretend CRJ:
  presses, knobs, switches_off, Match; `SC_FAKE_CRJ=1` runs the dry-run sim as a CRJ).
- **Publishing: double-click `publish.bat`.** On the first run it asks for your GitHub repo
  (create an empty one first) and sets it in `install.bat` and the updater. Every run checks tests,
  offers a version bump (installed copies then get an update prompt), commits and pushes.
  `publish.bat /fast` skips the tests.
- No DLLs are committed: SimConnect is fetched from Microsoft at install time
  (`shared_cockpit/sdkfetch.py`), or found in an installed MSFS SDK.

| File | What it does |
|---|---|
| `install.bat` | one-file installer / updater |
| `publish.bat` | dev: test, version-bump, commit and push to GitHub |
| `shared_cockpit/engine.py` | sync logic: who flies, interpolation, switch mirroring |
| `shared_cockpit/cockpit.py` | button/FMS relay, L:var auto-sync, Match co-pilot, Copy flight, checklist |
| `shared_cockpit/wsbridge.py` / `bridge_hook.js` / `bridge_install.py` | in-sim bridge: local WebSocket, cockpit JS hook, Community installer |
| `shared_cockpit/mobiflight.py` | MobiFlight WASM module client (L:var list, calculator code) |
| `shared_cockpit/synclog.py` | sync log + inspector data |
| `shared_cockpit/syncdefs.py` | which variables sync, the place to add more |
| `shared_cockpit/simconnect.py` | ctypes SimConnect binding |
| `shared_cockpit/fakesim.py` | dry-run simulator |
| `shared_cockpit/sdkfetch.py` | pulls SimConnect.dll out of the official SDK installer |
| `shared_cockpit/updater.py` | GitHub update check |
| `shared_cockpit/flavor.py` | per-sim settings (2020 / 2024 / dry run) |
