# Shared Cockpit

Fly one airplane with a friend in **Microsoft Flight Simulator 2024 or 2020**. One of you
hosts (port-forwards a TCP port); the other types in the host's IP and port. Whoever has the
controls flies. The other seat's airplane follows them exactly, and switches, radios and
autopilot sync both ways. Works with the Steam and Microsoft Store / Xbox app versions.

## Install (the easy way)
1. Download **[install.bat](install.bat)** (on GitHub: open it → *Download raw file*).
2. Double-click it. If Windows SmartScreen warns, click *More info → Run anyway*.

It installs Python if needed, downloads the app to `%LOCALAPPDATA%\SharedCockpit`,
detects MSFS 2020/2024, fetches SimConnect from Microsoft's SDK server (only the one DLL,
~60–90 MB, nothing installed in Windows), and adds Desktop + Start Menu shortcuts.
Run it again any time to repair. To remove everything, run `install.bat /uninstall`.

The app checks GitHub for updates on launch and offers to install them.

## Fly together
1. Both: start the sim and load into a flight with the **same aircraft** (different liveries are fine).
2. Both: open **Shared Cockpit** and pick your sim in the *Simulator* dropdown (auto-detected).
3. **Host:** choose *Host*, port `50555`, click **Start hosting**, then **Show my public IP** and
   send your friend that IP and port.
4. **Friend:** choose *Join a host*, type the IP and port, click **Connect**.
5. The host starts with the controls. Use **Take controls** / **Give controls** to swap.

### Test without the game: Dry run
Pick **Dry run (no sim)** in the Simulator dropdown. A pretend airplane flies circles on the
host's side; when the friend joins, their pretend airplane freezes and follows. Flip the
pretend switches (gear, lights, autopilot, COM1) and watch them change on the other PC. Use
it to check port forwarding before a flight.

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
- **Weather check.** The app warns when your wind, pressure or sim rate differ, because those make
  the two flight models diverge after a handoff.

## What syncs
| | Direction |
|---|---|
| Position, attitude, velocities (100 ms interpolation buffer) | pilot flying → other seat |
| Yoke/rudder/brakes, throttle/mixture/prop levers | pilot flying → other seat |
| Gear, flaps, spoilers, parking brake, trims | both ways |
| Exterior/panel/cabin lights, battery, alternator, avionics, pitot heat | both ways |
| COM1/2, NAV1/2 (active + standby), OBS, transponder, baro | both ways |
| Autopilot modes and HDG/ALT/VS/SPD selectors | both ways |
| Most other cockpit switches/knobs ("input events", experimental) | both ways |

## Tips / known limits
- **Turn off crash damage** on the non-flying side (the frozen plane is teleported every frame).
- Non-flying pilot: hands off the yoke/throttle.
- Weather and time aren't synced; both pick live weather and live time.
- Study-level add-ons (PMDG, Fenix, FBW, ...) keep state in their own systems; expect partial switch sync.
- MSFS 2024 has a known slight jitter when an app places an aircraft every frame (YourControls has
  the same issue).

## For developers
- Run from source: `python run.py` (Python 3.10+, standard library only).
- Tests (no sim needed): `python tests/test_loopback.py`, `test_dryrun.py`, `test_smooth.py`
  (measures smoothness under simulated packet loss and lag), `test_updater.py`.
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
| `shared_cockpit/syncdefs.py` | which variables sync, the place to add more |
| `shared_cockpit/simconnect.py` | ctypes SimConnect binding |
| `shared_cockpit/fakesim.py` | dry-run simulator |
| `shared_cockpit/sdkfetch.py` | pulls SimConnect.dll out of the official SDK installer |
| `shared_cockpit/updater.py` | GitHub update check |
| `shared_cockpit/flavor.py` | per-sim settings (2020 / 2024 / dry run) |
