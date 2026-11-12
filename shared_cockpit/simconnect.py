"""Minimal ctypes binding to SimConnect.dll (MSFS 2020/2024).

Only the calls this tool needs. SimConnect.h is #pragma pack(1), so every
received struct is parsed with explicit byte offsets.
"""
import ctypes
import os
import struct
from ctypes import (POINTER, byref, c_char_p, c_double, c_long, c_uint32,
                    c_uint64, c_void_p, wintypes)

# SIMCONNECT_DATATYPE
DT_INT32 = 1
DT_FLOAT64 = 4
DT_STRING256 = 9

# SIMCONNECT_PERIOD
PERIOD_NEVER = 0
PERIOD_ONCE = 1
PERIOD_VISUAL_FRAME = 2
PERIOD_SIM_FRAME = 3
PERIOD_SECOND = 4

# SIMCONNECT_DATA_REQUEST_FLAG
FLAG_CHANGED = 0x1

USER_OBJECT = 0
UNUSED = 0xFFFFFFFF
GROUP_PRIORITY_HIGHEST = 1
EVENT_FLAG_GROUPID_IS_PRIORITY = 0x10

# SIMCONNECT_RECV_ID
RECV_NULL = 0
RECV_EXCEPTION = 1
RECV_OPEN = 2
RECV_QUIT = 3
RECV_EVENT = 4
RECV_SIMOBJECT_DATA = 8
RECV_CLIENT_DATA = 16
RECV_ENUMERATE_INPUT_EVENTS = 34
RECV_GET_INPUT_EVENT = 35
RECV_SUBSCRIBE_INPUT_EVENT = 36

# SIMCONNECT_INPUT_EVENT_TYPE
INPUT_EVENT_TYPE_DOUBLE = 0
INPUT_EVENT_TYPE_STRING = 1

E_FAIL = -2147467259  # 0x80004005 as signed

# SIMCONNECT_CLIENT_DATA_PERIOD (client data areas, e.g. a WASM module's)
CDP_NEVER = 0
CDP_ONCE = 1
CDP_VISUAL_FRAME = 2
CDP_ON_SET = 3
CDP_SECOND = 4
# SIMCONNECT_CLIENT_DATA_REQUEST_FLAG
CD_FLAG_CHANGED = 0x1
CD_FLAG_TAGGED = 0x2


class SimConnectError(RuntimeError):
    pass


def dll_candidates():
    """Where to look for SimConnect.dll for the selected sim, best first."""
    from . import flavor
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    c = [os.path.join(root, "sdk_dll", flavor.KEY, "SimConnect.dll")]
    sdk_dirs = list(flavor.SDK_DEFAULT_DIRS)
    if flavor.SDK_ENV and os.environ.get(flavor.SDK_ENV):
        sdk_dirs.insert(0, os.environ[flavor.SDK_ENV])
    c += [os.path.join(d, "SimConnect SDK", "lib", "SimConnect.dll") for d in sdk_dirs]
    c += [os.path.join(root, "sdk_dll", "SimConnect.dll"), os.path.join(root, "SimConnect.dll")]
    return c


def _find_dll():
    for p in dll_candidates():
        if os.path.exists(p):
            return p
    raise SimConnectError("SimConnect.dll not found - click 'Get SimConnect' in the app")


class SimConnect:
    def __init__(self, app_name="Shared Cockpit", dll_path=None):
        self.dll_path = dll_path or _find_dll()
        self.dll = ctypes.WinDLL(self.dll_path)
        self.h = wintypes.HANDLE()
        self.app_name = app_name
        self._event_ids = {}
        self._next_event = 1000
        self.has_input_events = hasattr(self.dll, "SimConnect_SubscribeInputEvent") and \
            hasattr(self.dll, "SimConnect_SetInputEvent")
        self._proto()

    # -- prototypes ----------------------------------------------------
    def _proto(self):
        d = self.dll
        H = wintypes.HANDLE
        D = wintypes.DWORD
        d.SimConnect_Open.argtypes = [POINTER(H), c_char_p, wintypes.HWND, D, H, D]
        d.SimConnect_Close.argtypes = [H]
        d.SimConnect_AddToDataDefinition.argtypes = [H, D, c_char_p, c_char_p, D, ctypes.c_float, D]
        d.SimConnect_RequestDataOnSimObject.argtypes = [H, D, D, D, D, D, D, D, D]
        d.SimConnect_SetDataOnSimObject.argtypes = [H, D, D, D, D, D, c_void_p]
        d.SimConnect_MapClientEventToSimEvent.argtypes = [H, D, c_char_p]
        d.SimConnect_TransmitClientEvent.argtypes = [H, D, D, D, D, D]
        d.SimConnect_SubscribeToSystemEvent.argtypes = [H, D, c_char_p]
        d.SimConnect_GetNextDispatch.argtypes = [H, POINTER(c_void_p), POINTER(D)]
        for f in ("SimConnect_Open", "SimConnect_Close", "SimConnect_AddToDataDefinition",
                  "SimConnect_RequestDataOnSimObject", "SimConnect_SetDataOnSimObject",
                  "SimConnect_MapClientEventToSimEvent", "SimConnect_TransmitClientEvent",
                  "SimConnect_SubscribeToSystemEvent", "SimConnect_GetNextDispatch"):
            getattr(d, f).restype = c_long
        if self.has_input_events:
            d.SimConnect_SubscribeInputEvent.argtypes = [H, c_uint64]
            d.SimConnect_SubscribeInputEvent.restype = c_long
            d.SimConnect_SetInputEvent.argtypes = [H, c_uint64, D, c_void_p]
            d.SimConnect_SetInputEvent.restype = c_long
            if hasattr(d, "SimConnect_EnumerateInputEvents"):
                d.SimConnect_EnumerateInputEvents.argtypes = [H, D]
                d.SimConnect_EnumerateInputEvents.restype = c_long
        # client data (all exported by the old bundled DLL too)
        d.SimConnect_MapClientDataNameToID.argtypes = [H, c_char_p, D]
        d.SimConnect_AddToClientDataDefinition.argtypes = [H, D, D, D, ctypes.c_float, D]
        d.SimConnect_RequestClientData.argtypes = [H, D, D, D, D, D, D, D, D]
        d.SimConnect_SetClientData.argtypes = [H, D, D, D, D, D, c_void_p]
        for f in ("SimConnect_MapClientDataNameToID", "SimConnect_AddToClientDataDefinition",
                  "SimConnect_RequestClientData", "SimConnect_SetClientData"):
            getattr(d, f).restype = c_long
        # flights
        d.SimConnect_FlightSave.argtypes = [H, c_char_p, c_char_p, c_char_p, D]
        d.SimConnect_FlightLoad.argtypes = [H, c_char_p]
        d.SimConnect_FlightSave.restype = d.SimConnect_FlightLoad.restype = c_long
        self.has_get_input_event = hasattr(d, "SimConnect_GetInputEvent")
        if self.has_get_input_event:
            d.SimConnect_GetInputEvent.argtypes = [H, D, c_uint64]
            d.SimConnect_GetInputEvent.restype = c_long

    def _ok(self, hr, what):
        if hr < 0:
            raise SimConnectError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")

    # -- connection ----------------------------------------------------
    def open(self):
        hr = self.dll.SimConnect_Open(byref(self.h), self.app_name.encode(), None, 0, None, 0)
        self._ok(hr, "SimConnect_Open (is MSFS running and in a flight or menu?)")

    def close(self):
        if self.h:
            try:
                self.dll.SimConnect_Close(self.h)
            except OSError:
                pass
            self.h = wintypes.HANDLE()

    # -- data ----------------------------------------------------------
    def add_def(self, define_id, name, unit, dtype=DT_FLOAT64):
        hr = self.dll.SimConnect_AddToDataDefinition(
            self.h, define_id, name.encode(), unit.encode() if unit else None, dtype, 0.0, UNUSED)
        self._ok(hr, f"AddToDataDefinition({name})")

    def request(self, req_id, define_id, period, flags=0, interval=0):
        hr = self.dll.SimConnect_RequestDataOnSimObject(
            self.h, req_id, define_id, USER_OBJECT, period, flags, 0, interval, 0)
        self._ok(hr, "RequestDataOnSimObject")

    def set_data(self, define_id, payload: bytes):
        buf = ctypes.create_string_buffer(payload, len(payload))
        hr = self.dll.SimConnect_SetDataOnSimObject(
            self.h, define_id, USER_OBJECT, 0, 0, len(payload), buf)
        self._ok(hr, "SetDataOnSimObject")

    # -- events --------------------------------------------------------
    def event_id(self, name):
        eid = self._event_ids.get(name)
        if eid is None:
            eid = self._next_event
            self._next_event += 1
            hr = self.dll.SimConnect_MapClientEventToSimEvent(self.h, eid, name.encode())
            self._ok(hr, f"MapClientEventToSimEvent({name})")
            self._event_ids[name] = eid
        return eid

    def send_event(self, name, value=0):
        eid = self.event_id(name)
        hr = self.dll.SimConnect_TransmitClientEvent(
            self.h, USER_OBJECT, eid, int(value) & 0xFFFFFFFF,
            GROUP_PRIORITY_HIGHEST, EVENT_FLAG_GROUPID_IS_PRIORITY)
        self._ok(hr, f"TransmitClientEvent({name})")

    def subscribe_system(self, event_id, name):
        hr = self.dll.SimConnect_SubscribeToSystemEvent(self.h, event_id, name.encode())
        self._ok(hr, f"SubscribeToSystemEvent({name})")

    # -- input events (B: vars, needs the MSFS SDK SimConnect.dll) -------
    def subscribe_all_input_events(self):
        hr = self.dll.SimConnect_SubscribeInputEvent(self.h, 0)
        self._ok(hr, "SubscribeInputEvent")

    def enumerate_input_events(self, req_id):
        hr = self.dll.SimConnect_EnumerateInputEvents(self.h, req_id)
        self._ok(hr, "EnumerateInputEvents")

    def set_input_event(self, hash_, value):
        v = c_double(value)
        hr = self.dll.SimConnect_SetInputEvent(self.h, hash_, 8, byref(v))
        self._ok(hr, "SetInputEvent")

    def get_input_event(self, req_id, hash_):
        """Ask for an input event's current value; answer arrives as ("input_value", ...)."""
        if not getattr(self, "has_get_input_event", False):
            raise SimConnectError("GetInputEvent needs the MSFS SDK SimConnect.dll")
        hr = self.dll.SimConnect_GetInputEvent(self.h, req_id, hash_)
        self._ok(hr, "GetInputEvent")

    # -- flights -------------------------------------------------------
    def flight_save(self, path, title="Shared Cockpit", description="", flags=0):
        hr = self.dll.SimConnect_FlightSave(self.h, path.encode("mbcs"), title.encode("mbcs"),
                                            description.encode("mbcs"), flags)
        self._ok(hr, "FlightSave")

    def flight_load(self, path):
        hr = self.dll.SimConnect_FlightLoad(self.h, path.encode("mbcs"))
        self._ok(hr, "FlightLoad")

    # -- client data areas (talking to a WASM module such as MobiFlight) --
    def map_client_data(self, name, area_id):
        hr = self.dll.SimConnect_MapClientDataNameToID(self.h, name.encode(), area_id)
        self._ok(hr, f"MapClientDataNameToID({name})")

    def add_to_client_data_def(self, def_id, offset, size_bytes):
        hr = self.dll.SimConnect_AddToClientDataDefinition(
            self.h, def_id, offset, size_bytes, 0.0, UNUSED)
        self._ok(hr, "AddToClientDataDefinition")

    def request_client_data(self, area_id, req_id, def_id, period, flags=0):
        hr = self.dll.SimConnect_RequestClientData(
            self.h, area_id, req_id, def_id, period, flags, 0, 0, 0)
        self._ok(hr, "RequestClientData")

    def set_client_data(self, area_id, def_id, payload: bytes):
        buf = ctypes.create_string_buffer(payload, len(payload))
        hr = self.dll.SimConnect_SetClientData(self.h, area_id, def_id, 0, 0, len(payload), buf)
        self._ok(hr, "SetClientData")

    # -- dispatch ------------------------------------------------------
    def poll(self):
        """Yield parsed messages until the queue is empty.

        Yields tuples: ("data", request_id, raw_bytes) | ("event", event_id, data)
        | ("exception", code, send_id, index) | ("quit",) | ("open",)
        | ("input_event", hash, value) | ("input_list", [(name, hash), ...])
        """
        p = c_void_p()
        n = wintypes.DWORD()
        while True:
            hr = self.dll.SimConnect_GetNextDispatch(self.h, byref(p), byref(n))
            if hr < 0 or not p.value:
                return
            raw = ctypes.string_at(p.value, n.value)
            size, ver, rid = struct.unpack_from("<III", raw, 0)
            if rid == RECV_SIMOBJECT_DATA:
                req = struct.unpack_from("<I", raw, 12)[0]
                yield ("data", req, raw[40:])
            elif rid == RECV_CLIENT_DATA:
                # same layout as SIMOBJECT_DATA: dwRequestID @12, data @40
                req = struct.unpack_from("<I", raw, 12)[0]
                yield ("client_data", req, raw[40:])
            elif rid == RECV_GET_INPUT_EVENT:
                # RequestID @12, eType @16, Value inline @20
                if len(raw) >= 28:
                    req, typ = struct.unpack_from("<II", raw, 12)
                    yield ("input_value", req, struct.unpack_from("<d", raw, 20)[0], typ)
            elif rid == RECV_EVENT:
                _grp, eid, data = struct.unpack_from("<III", raw, 12)
                yield ("event", eid, data)
            elif rid == RECV_EXCEPTION:
                code, send_id, index = struct.unpack_from("<III", raw, 12)
                yield ("exception", code, send_id, index)
            elif rid == RECV_QUIT:
                yield ("quit",)
            elif rid == RECV_OPEN:
                yield ("open",)
            elif rid == RECV_SUBSCRIBE_INPUT_EVENT and len(raw) >= 32:
                # Hash@12, eType@20, Value inline @24. The eType code is unreliable
                # (Hash=0 subscriptions report NONE), so the engine decides double-vs-
                # string from the enumerated descriptor instead.
                h, typ = struct.unpack_from("<QI", raw, 12)
                yield ("input_event", h, struct.unpack_from("<d", raw, 24)[0], typ)
            elif rid == RECV_ENUMERATE_INPUT_EVENTS:
                # SIMCONNECT_RECV_LIST_TEMPLATE: dwRequestID, dwArraySize, dwEntryNumber, dwOutOf
                _req, count, _entry, _outof = struct.unpack_from("<IIII", raw, 12)
                if not count:
                    continue
                # Descriptor = char Name[64], UINT64 Hash, DWORD eType (76 bytes), but
                # measure the stride in case a newer sim appends fields.
                stride = (len(raw) - 28) // count
                if stride < 76:
                    continue
                items, off = [], 28
                for _ in range(count):
                    name = raw[off:off + 64].split(b"\0", 1)[0].decode("latin-1")
                    h, et = struct.unpack_from("<QI", raw, off + 64)
                    items.append((name, h, et))
                    off += stride
                yield ("input_list", items)


class Block:
    """A data definition made of FLOAT64 simvars, packed/unpacked as a vector."""

    def __init__(self, sc, define_id, vars_):
        self.sc, self.define_id, self.vars = sc, define_id, list(vars_)
        self.fmt = "<%dd" % len(self.vars)
        self.size = struct.calcsize(self.fmt)
        for name, unit in self.vars:
            sc.add_def(define_id, name, unit)

    def unpack(self, raw):
        return list(struct.unpack_from(self.fmt, raw, 0))

    def write(self, values):
        self.sc.set_data(self.define_id, struct.pack(self.fmt, *values))
