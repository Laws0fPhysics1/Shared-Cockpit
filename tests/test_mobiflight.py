"""MobiFlight client test against a fake of the MobiFlight WASM module.

The fake copies the real module's behaviour (Module.cpp): Command areas are
watched ON_SET + CHANGED (an identical consecutive command is dropped), the
first command written to a new area is lost, responses go to "<Client>.Response",
L:var names are written one per response.

Run:  python tests/test_mobiflight.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit.mobiflight import MSG_SIZE, MobiFlightClient  # noqa: E402
from shared_cockpit.simconnect import CD_FLAG_CHANGED, CDP_ON_SET  # noqa: E402

LVARS = ["XMLVAR_Baro1_Mode", "A320_Neo_MFD_NAV_MODE", "XMLVAR_Momentary_PUSH_OVHD_ANTIICE_ENG1",
         "A32NX_OVHD_ELEC_BAT_1_PB_IS_AUTO", "XMLVAR_LTS_Test"]


class FakeMF:
    def __init__(self, installed=True):
        self.installed = installed
        self.names = {}            # area id -> name (client's mapping)
        self.requests = {}         # area name -> (req_id, flags)
        self.defs = {}
        self.clients = {"MobiFlight"}
        self.last_cmd = {}         # area name -> last processed command
        self.seen_area = set()
        self.out = []              # pending ("client_data", req, raw)
        self.executed = []
        self.commands = []         # every processed command (after quirks)

    # -- SimConnect surface used by MobiFlightClient
    def map_client_data(self, name, area_id):
        self.names[area_id] = name

    def add_to_client_data_def(self, def_id, offset, size):
        self.defs[def_id] = (offset, size)

    def request_client_data(self, area_id, req_id, def_id, period, flags=0):
        assert period == CDP_ON_SET and not (flags & CD_FLAG_CHANGED), "responses need ON_SET w/o CHANGED"
        self.requests[self.names[area_id]] = req_id

    def set_client_data(self, area_id, def_id, payload):
        assert len(payload) == MSG_SIZE and self.defs[def_id] == (0, MSG_SIZE)
        name = self.names[area_id]
        if not self.installed or not name.endswith(".Command"):
            return
        client = name[:-len(".Command")]
        if client not in self.clients:
            return                                   # area doesn't exist (yet)
        if name not in self.seen_area:               # first write after start is lost
            self.seen_area.add(name)
            return
        cmd = payload.split(b"\0", 1)[0].decode()
        if self.last_cmd.get(name) == cmd:           # CHANGED flag: identical -> dropped
            return
        self.last_cmd[name] = cmd
        self.commands.append((client, cmd))
        self._process(client, cmd)

    def poll(self):
        out, self.out = self.out, []
        yield from out

    # -- module behaviour
    def _respond(self, client, text):
        req = self.requests.get(client + ".Response")
        if req is not None:
            raw = text.encode() + b"\0" * (MSG_SIZE - len(text))
            self.out.append(("client_data", req, raw))

    def _process(self, client, cmd):
        if cmd == "MF.Ping":
            self._respond(client, "MF.Pong")
        elif cmd == "MF.LVars.List":
            self._respond(client, "MF.LVars.List.Start")
            for n in sorted(LVARS):
                self._respond(client, n)
            self._respond(client, "MF.LVars.List.End")
        elif cmd == "MF.Version.Get":
            self._respond(client, "MF.Version.1.0.1")
        elif cmd.startswith("MF.SimVars.Set."):
            self.executed.append(cmd[len("MF.SimVars.Set."):])
        elif cmd.startswith("MF.Clients.Add."):
            new = cmd[len("MF.Clients.Add."):]
            self.clients.add(new)
            self._respond(client, f"MF.Clients.Add.{new}.Finished")


def pump(mf, cl, now, steps=20):
    for _ in range(steps):
        msgs = list(mf.poll())
        for kind, req, raw in msgs:
            assert kind == "client_data"
            assert cl.on_client_data(req, raw), "response not recognised"
        cl.tick(now)
        if not msgs:
            break


logs = []
mf = FakeMF()
cl = MobiFlightClient(mf, log=logs.append, id_base=9000)
assert not cl.on_client_data(1234, b"x"), "foreign request ids must be ignored"
# the default client's own L:var list arriving at module start must be ignored
mf.requests["MobiFlight.Response"] = 9000
cl.start()
mf._respond("MobiFlight", "XMLVAR_stray_from_default_list")
now = 100.0
pump(mf, cl, now)
# first Ping was preceded by a dummy (eaten as "first write"), so it got through at once
assert cl.available, "module should answer the first ping"
assert cl.ready, f"own client should be registered: {mf.commands}"
assert ("SharedCockpit", "MF.Ping") in mf.commands
assert cl.version == "1.0.1", cl.version
print(f"OK handshake: module found, own client 'SharedCockpit' ready, version {cl.version}")

got = []
cl.list_lvars(got.append)
pump(mf, cl, now)
assert got == [sorted(LVARS)], got
assert "XMLVAR_stray_from_default_list" not in got[0]
print(f"OK L:var listing ({len(got[0])} names, stray default-client names ignored)")

cl.execute("(>H:A320_Neo_CDU_1_BTN_A)")
cl.execute("(>H:A320_Neo_CDU_1_BTN_A)")      # identical twice in a row must still run twice
cl.execute("1 (>L:XMLVAR_LTS_Test)")
pump(mf, cl, now)
assert mf.executed == ["(>H:A320_Neo_CDU_1_BTN_A)", "(>H:A320_Neo_CDU_1_BTN_A)",
                       "1 (>L:XMLVAR_LTS_Test)"], mf.executed
print("OK execute: RPN delivered, identical consecutive commands both run")

# commands queued before ready are flushed once ready
mf2 = FakeMF()
cl2 = MobiFlightClient(mf2, log=logs.append, id_base=9100)
cl2.execute("(>H:EARLY)")
got2 = []
cl2.list_lvars(got2.append)
cl2.start()
pump(mf2, cl2, 0.0)
assert mf2.executed == ["(>H:EARLY)"] and got2 == [sorted(LVARS)], (mf2.executed, got2)
print("OK commands and listing requested before ready are run once ready")

# module not installed -> no answer, ping retried every 5 s; installing later recovers
mf3 = FakeMF(installed=False)
clock = {"t": 0.0}
cl3 = MobiFlightClient(mf3, log=logs.append, id_base=9200, clock=lambda: clock["t"])
cl3.start()
pump(mf3, cl3, 0.0)
assert not cl3.available and not cl3.ready
mf3.installed = True
cl3.tick(3.0)                       # < 5 s: no retry yet
assert not cl3.available
cl3.tick(5.1)                       # retry (first write after "start" is eaten by the module)
pump(mf3, cl3, 5.1)
cl3.tick(10.2)                      # second retry gets through
pump(mf3, cl3, 10.2)
assert cl3.available and cl3.ready, mf3.commands
print("OK no module -> available False; pings retried every 5 s until it answers")

# lost own-client Pong is retried
mf4 = FakeMF()
cl4 = MobiFlightClient(mf4, log=logs.append, id_base=9300, clock=lambda: 0.0)
orig = mf4._respond
drop = {"n": 1}


def flaky(client, text):
    if client == "SharedCockpit" and text == "MF.Pong" and drop["n"]:
        drop["n"] -= 1
        return
    orig(client, text)


mf4._respond = flaky
cl4.start()
pump(mf4, cl4, 0.0)
assert cl4.available and not cl4.ready
cl4.tick(2.5)
pump(mf4, cl4, 2.5)
assert cl4.ready
print("OK lost Pong on our own client is retried")
print("\nMOBIFLIGHT TESTS PASSED")
