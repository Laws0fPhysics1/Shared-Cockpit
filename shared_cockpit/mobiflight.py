"""Client for the MobiFlight WASM module (MIT, github.com/MobiFlight/MobiFlight-WASM-Module).

The module runs inside MSFS. We use it for two things an outside SimConnect app
can't do on its own:
  - list every L: variable of the loaded aircraft   (MF.LVars.List)
  - run calculator code / RPN, e.g. "(>H:...)"      (MF.SimVars.Set.<rpn>)

Protocol (checked against src/Sources/Code/Module.cpp):
  client data areas "<Client>.Command" and "<Client>.Response", 1024-byte
  zero-terminated strings. The default client is "MobiFlight". We ping on the
  default areas, register our own client (MF.Clients.Add.<Name> answered with
  "MF.Clients.Add.<Name>.Finished"), then use "<Name>.Command/.Response".
  Quirks handled here:
    * the module watches Command with ON_SET + CHANGED, so sending the same
      string twice in a row is silently dropped -> a dummy goes in between;
    * the first SetClientData after start may be ignored -> dummy first, and
      pings are retried until a Pong arrives;
    * responses overwrite one area (one write per L:var name while listing),
      so Response is requested ON_SET without the CHANGED flag (every write is
      delivered, even repeated "MF.Pong").
    * the module lists at most 1000 L:vars.

Pure logic, no threads: the engine calls on_client_data() for every
("client_data", req_id, raw) it polls, and tick() regularly.
"""
import time

from .simconnect import CDP_ON_SET

MSG_SIZE = 1024
DEFAULT = "MobiFlight"
DUMMY = "MF.DummyCmd"
PING_RETRY = 5.0
OWN_PING_RETRY = 2.0
LIST_TIMEOUT = 15.0


class MobiFlightClient:
    def __init__(self, sc, log=print, client_name="SharedCockpit", id_base=9000, clock=time.monotonic):
        self.sc, self.log, self.name, self.clock = sc, log, client_name, clock
        b = id_base
        self.A_CMD, self.A_RESP, self.A_OWN_CMD, self.A_OWN_RESP = b, b + 1, b + 2, b + 3
        self.DEF_STR = b
        self.R_RESP, self.R_OWN_RESP = b, b + 1
        self.available = False      # module answered (installed and running)
        self.ready = False          # our own client registered and answering
        self.version = None
        self._started = False
        self._registered = False    # own areas mapped
        self._last_sent = {}
        self._t_ping = self._t_add = self._t_own_ping = 0.0
        self._queue = []            # commands waiting for ready
        self._list_cbs = []         # callbacks waiting for a listing
        self._listing = None        # names collected so far, or None
        self._t_list = 0.0
        self._list_requested = False

    # ---------------------------------------------------------------- public
    def start(self):
        sc = self.sc
        sc.map_client_data(DEFAULT + ".Command", self.A_CMD)
        sc.map_client_data(DEFAULT + ".Response", self.A_RESP)
        sc.add_to_client_data_def(self.DEF_STR, 0, MSG_SIZE)
        sc.request_client_data(self.A_RESP, self.R_RESP, self.DEF_STR, CDP_ON_SET, 0)
        self._started = True
        self._send(self.A_CMD, DUMMY)
        self._ping_default()

    def execute(self, rpn):
        """Run calculator code in the sim, e.g. '(>H:A320_Neo_CDU_1_BTN_A)'."""
        cmd = "MF.SimVars.Set." + rpn
        if self.ready:
            self._send(self.A_OWN_CMD, cmd)
        else:
            self._queue.append(cmd)
            del self._queue[:-200]           # don't grow without bound if MobiFlight is missing

    def list_lvars(self, callback):
        """callback(list_of_names) once the module has listed every L:var (max 1000)."""
        self._list_cbs.append(callback)
        if self.ready and self._listing is None:
            self._request_list()
        else:
            self._list_requested = True

    def on_client_data(self, req_id, raw):
        if req_id not in (self.R_RESP, self.R_OWN_RESP):
            return False
        text = raw[:MSG_SIZE].split(b"\0", 1)[0].decode("utf-8", "replace")
        own = req_id == self.R_OWN_RESP
        self._on_response(text, own)
        return True

    def tick(self, now=None):
        if not self._started:
            return
        now = self.clock() if now is None else now
        if not self.available:
            if now - self._t_ping >= PING_RETRY:
                self._ping_default(now)
        elif not self._registered:
            if now - self._t_add >= PING_RETRY:
                self._add_client(now)
        elif not self.ready:
            if now - self._t_own_ping >= OWN_PING_RETRY:
                self._ping_own(now)
        if self._listing is not None and now - self._t_list > LIST_TIMEOUT:
            self.log(f"MobiFlight: L:var list incomplete ({len(self._listing)} names), using it anyway")
            self._finish_list()

    # ---------------------------------------------------------------- internals
    def _send(self, area, cmd):
        if self._last_sent.get(area) == cmd:
            self._set(area, DUMMY)             # identical consecutive commands are dropped
        self._set(area, cmd)

    def _set(self, area, cmd):
        data = cmd.encode("utf-8")[:MSG_SIZE - 1]
        self.sc.set_client_data(area, self.DEF_STR, data + b"\0" * (MSG_SIZE - len(data)))
        self._last_sent[area] = cmd

    def _ping_default(self, now=None):
        self._t_ping = self.clock() if now is None else now
        self._send(self.A_CMD, "MF.Ping")

    def _add_client(self, now=None):
        self._t_add = self.clock() if now is None else now
        self._send(self.A_CMD, "MF.Clients.Add." + self.name)

    def _ping_own(self, now=None):
        self._t_own_ping = self.clock() if now is None else now
        self._send(self.A_OWN_CMD, "MF.Ping")

    def _register_own(self):
        sc = self.sc
        sc.map_client_data(self.name + ".Command", self.A_OWN_CMD)
        sc.map_client_data(self.name + ".Response", self.A_OWN_RESP)
        sc.request_client_data(self.A_OWN_RESP, self.R_OWN_RESP, self.DEF_STR, CDP_ON_SET, 0)
        self._registered = True
        self._set(self.A_OWN_CMD, DUMMY)
        self._ping_own()

    def _request_list(self):
        self._listing = None
        self._list_requested = False
        self._t_list = self.clock()
        self._send(self.A_OWN_CMD, "MF.LVars.List")

    def _finish_list(self):
        names = sorted(set(self._listing or []))
        self._listing = None
        cbs, self._list_cbs = self._list_cbs, []
        for cb in cbs:
            try:
                cb(names)
            except Exception as e:
                self.log(f"MobiFlight: L:var list callback failed: {e!r}")

    def _on_response(self, text, own):
        if not text:
            return
        if text == "MF.Pong":
            if not own and not self.available:
                self.available = True
                self.log("MobiFlight module found in the sim.")
                self._add_client()
            elif own and not self.ready:
                self.ready = True
                self.log(f"MobiFlight: client '{self.name}' ready.")
                self._send(self.A_OWN_CMD, "MF.Version.Get")
                q, self._queue = self._queue, []
                for cmd in q:
                    self._send(self.A_OWN_CMD, cmd)
                if self._list_requested or self._list_cbs:
                    self._request_list()
            return
        if text == f"MF.Clients.Add.{self.name}.Finished":
            if not self._registered:
                self._register_own()
            return
        if text.startswith("MF.Version."):
            self.version = text[len("MF.Version."):]
            return
        if not own:
            return                               # default client's stray traffic (its own L:var list etc.)
        if text == "MF.LVars.List.Start":
            self._listing = []
            self._t_list = self.clock()
            return
        if text == "MF.LVars.List.End":
            if self._listing is not None:
                self._finish_list()
            return
        if self._listing is not None and not text.startswith("MF."):
            self._listing.append(text)
            self._t_list = self.clock()
