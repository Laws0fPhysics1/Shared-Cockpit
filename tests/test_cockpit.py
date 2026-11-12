"""Full-cockpit sync with the dry-run sim: L:vars (incl. the self-changing filter),
button presses (FMS keys), Match co-pilot, Copy co-pilot's flight, checklist.
Also: H events from a real WebSocket bridge client reach the co-pilot's bridge.

Run:  python tests/test_cockpit.py
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit import flavor  # noqa: E402

flavor.select("dry")
os.environ["SC_BRIDGE_IN_DRY"] = "1"          # run the real WebSocket bridge too
from shared_cockpit.engine import Engine  # noqa: E402


def wait(cond, t=10.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timeout: " + what)


class WsClient:
    """Minimal WebSocket client standing in for one cockpit screen's JS hook."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.s.recv(1024)
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        assert acc in resp, resp
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        hdr = bytes([0x81]) + (bytes([0x80 | len(data)]) if len(data) < 126 else
                               bytes([0x80 | 126]) + struct.pack(">H", len(data)))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout=3.0):
        self.s.settimeout(timeout)
        while True:
            if len(self.buf) >= 2:
                op, ln = self.buf[0] & 0x0F, self.buf[1] & 0x7F
                off = 2
                if ln == 126:
                    ln, off = struct.unpack(">H", self.buf[2:4])[0], 4
                if len(self.buf) >= off + ln:
                    payload, self.buf = self.buf[off:off + ln], self.buf[off + ln:]
                    if op == 1:
                        return json.loads(payload)
                    continue
            self.buf += self.s.recv(4096)

    def close(self):
        self.s.close()


logs = []
A = Engine(lambda m: logs.append("[A] " + m), "Host", bridge_port=50661)
B = Engine(lambda m: logs.append("[B] " + m), "Joiner", bridge_port=50662)
wait(lambda: A.ready and B.ready, what="fake sims ready")
A.host(50996)
time.sleep(0.2)
B.join("127.0.0.1", 50996)
wait(lambda: B.link and B.pf == "host" and B.sc.frozen(), what="connected")
wait(lambda: A.cp.lv_names and B.cp.lv_names, what="L:var lists")
assert "XMLVAR_BLINK_STATE" not in A.cp.lv_names, "name filter"
print(f"OK {len(A.cp.lv_names)} pretend L:vars listed (blink var filtered by name)")

# --- L:var sync both ways
A.sc.set_var("L:XMLVAR_OVHD_APU_MASTER", 1)
wait(lambda: B.sc.get("L:XMLVAR_OVHD_APU_MASTER") == 1, what="A->B L:var")
B.sc.set_var("L:XMLVAR_OVHD_SEATBELTS", 1)
B.sc.set_var("L:A320_FCU_ALT_SEL", 24000)
wait(lambda: A.sc.get("L:XMLVAR_OVHD_SEATBELTS") == 1 and A.sc.get("L:A320_FCU_ALT_SEL") == 24000,
     what="B->A L:vars")
print("OK overhead-style L:vars sync both ways")

# --- the self-changing L:var gets ignored, then nothing ping-pongs
wait(lambda: "FAKE_SYS_CLOCK" in A.cp.lv_ignored and "FAKE_SYS_CLOCK" in B.cp.lv_ignored, t=15,
     what="noise filter")
n0 = sum(1 for e in A.cp.slog.recent(600) if e[2] == "lvar" and e[3] == "FAKE_SYS_CLOCK" and e[1] == "out")
time.sleep(1.5)
n1 = sum(1 for e in A.cp.slog.recent(600) if e[2] == "lvar" and e[3] == "FAKE_SYS_CLOCK" and e[1] == "out")
assert n1 == n0, "clock still being sent"
print("OK self-changing L:var detected and ignored on both sides (no ping-pong)")

# --- FMS key presses (dry run pretend MCDU) via the engine's bridge path
for k in "KSEA":
    A.inbox.put(("ws", {"t": "h", "n": f"A320_Neo_CDU_1_BTN_{k}"}, "dry"))
    A.sc.press_h(f"A320_Neo_CDU_1_BTN_{k}")
    time.sleep(0.1)
wait(lambda: B.sc.scratchpad == "KSEA", what="FMS keys")
A.inbox.put(("ws", {"t": "h", "n": "A320_Neo_CDU_1_BTN_CLR"}, "dry"))
wait(lambda: B.sc.scratchpad == "KSE", what="CLR key")
print("OK FMS keys typed on host appear on co-pilot's pretend MCDU (KSEA, CLR)")

# --- the same press reported by several cockpit screens is sent once
before = sum(1 for e in A.cp.slog.recent(600) if e[2] == "button" and e[1] == "out")
for _ in range(3):
    A.inbox.put(("ws", {"t": "h", "n": "A320_Neo_CDU_1_BTN_X"}, "dry"))
time.sleep(0.3)
after = sum(1 for e in A.cp.slog.recent(600) if e[2] == "button" and e[1] == "out")
assert after - before == 1, after - before
print("OK one press reported by 3 screens is sent once")

# --- real WebSocket bridge: a screen on A presses a key; B's screen gets the replay
wa, wb = WsClient(50661), WsClient(50662)
wa.send({"t": "hello", "panel": "CDU", "v": 1})
wb.send({"t": "hello", "panel": "CDU", "v": 1})
wait(lambda: A.cp.bridge_screens() == 1 and B.cp.bridge_screens() == 1, what="ws hello")
wa.send({"t": "h", "n": "A320_Neo_CDU_1_BTN_EXEC", "a": [], "g": None})
got = {wb.recv()["n"], wb.recv()["n"]}            # the press + its FMS mirror (on by default)
assert got == {"A320_Neo_CDU_1_BTN_EXEC", "A320_Neo_CDU_2_BTN_EXEC"}, got
wa.close(); wb.close()
print("OK cockpit-screen hook on host -> WebSocket -> network -> co-pilot's screen hook")

# --- Match co-pilot: B goes cold & dark (engines off, fuel differs: neither syncs live)
B.sc.set_var("GENERAL ENG COMBUSTION:1", 0)
B.sc.set_var("FUEL TANK RIGHT MAIN QUANTITY", 3)
A.sc.set_var("LIGHT BEACON", 1)
A.sc.set_var("L:XMLVAR_OVHD_BAT1", 1)
time.sleep(1.0)
B.match_copilot()
try:
    wait(lambda: B.sc.get("GENERAL ENG COMBUSTION:1") == 1
         and B.sc.get("LIGHT BEACON") == A.sc.get("LIGHT BEACON") == 1
         and B.sc.get("L:XMLVAR_OVHD_BAT1") == A.sc.get("L:XMLVAR_OVHD_BAT1") == 1
         and abs(B.sc.get("FUEL TANK RIGHT MAIN QUANTITY") - A.sc.get("FUEL TANK RIGHT MAIN QUANTITY")) < 0.1,
         what="match co-pilot")
except AssertionError:
    for v in ("GENERAL ENG COMBUSTION:1", "LIGHT BEACON", "L:XMLVAR_OVHD_BAT1", "FUEL TANK RIGHT MAIN QUANTITY"):
        print("  ", v, "A", A.sc.get(v), "B", B.sc.get(v))
    for m in logs[-12:]:
        print(m)
    raise
print("OK Match co-pilot: engines started, switches, overhead L:vars and fuel copied")

# --- Copy co-pilot's flight: B loads A's saved flight, then auto-matches
A.sc.set_var("SIMULATION RATE", 1.0)
A.sc.set_var("L:XMLVAR_OVHD_APU_MASTER", 0)
time.sleep(0.5)
B.sc.set_var("AMBIENT TEMPERATURE", 30.0)   # B's world differs
B.copy_copilot_flight()
wait(lambda: B.sc.loaded_flights == 1, what="flight loaded")
assert abs(B.sc.get("AMBIENT TEMPERATURE") - A.sc.get("AMBIENT TEMPERATURE")) < 0.01
wait(lambda: any("Matched co-pilot's aircraft" in m for m in logs if m.startswith("[B]")), t=15,
     what="auto match after load")
print("OK Copy co-pilot's flight: .FLT sent + loaded (weather/time came along), then auto-matched")

# --- checklist
time.sleep(1.5)
items = dict((k, ok) for k, ok, _ in B.cp.checklist())
print("   checklist:", B.cp.checklist())
assert items["Same aircraft"] is True and items["Crash damage off"] is False
B.sc.set_var("REALISM CRASH DETECTION", 0)
wait(lambda: dict((k, ok) for k, ok, _ in B.cp.checklist())["Crash damage off"] is True, what="crash check")
print("OK pre-flight checklist: aircraft match, crash damage check")
assert B.cp.slog.path and os.path.exists(B.cp.slog.path)
print(f"OK sync log written ({os.path.basename(B.cp.slog.path)})")

A.shutdown(); B.shutdown()
print("\nCOCKPIT TESTS PASSED")
