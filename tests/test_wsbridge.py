"""WebSocket bridge server test with a minimal stdlib client (masked frames).

Run:  python tests/test_wsbridge.py
"""
import base64
import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared_cockpit.wsbridge import BridgeServer  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(cond, t=3.0, what=""):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timeout: " + what)


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                        "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.s.recv(4096)
        head, self.buf = data.split(b"\r\n\r\n", 1)
        assert b" 101 " in head.split(b"\r\n")[0], head
        import hashlib
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                                             .encode()).digest())
        assert want in head, "bad Sec-WebSocket-Accept"

    def frame(self, op, payload, fin=True):
        mask = os.urandom(4)
        n = len(payload)
        b0 = (0x80 if fin else 0) | op
        if n < 126:
            hdr = struct.pack("!BB", b0, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", b0, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", b0, 0x80 | 127, n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send_json(self, obj):
        self.frame(0x1, json.dumps(obj).encode())

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b0, b1 = self._need(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._need(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._need(8))[0]
        assert not (b1 & 0x80), "server frames must not be masked"
        return b0 & 0x0F, self._need(n)


got, changes = [], []
port = free_port()
srv = BridgeServer(port, on_message=lambda m, cid: got.append((m, cid)),
                   log=lambda m: None, on_change=lambda: changes.append(1))

# busy port -> OSError from the constructor
try:
    BridgeServer(port, log=lambda m: None)
    raise AssertionError("second server on a busy port should fail")
except OSError:
    pass
print("OK busy port raises OSError")

c = Client(port)
c.send_json({"t": "hello", "panel": "A320_Neo_CDU"})
c.send_json({"t": "h", "n": "A320_Neo_CDU_1_BTN_A"})
wait(lambda: len(got) >= 2, what="messages")
assert got[0][0]["t"] == "hello" and got[1][0] == {"t": "h", "n": "A320_Neo_CDU_1_BTN_A"}
cid = got[1][1]
wait(lambda: srv.clients() == [{"id": cid, "panel": "A320_Neo_CDU"}], what="client listed")
print("OK handshake, hello, H-event message, clients()")

# non-JSON is ignored, connection stays up
c.frame(0x1, b"not json")
c.send_json({"t": "h", "n": "AFTER_JUNK"})
wait(lambda: got[-1][0].get("n") == "AFTER_JUNK", what="after junk")

# fragmented message: text frame (FIN=0) + continuation (FIN=1), with a ping in between
part = json.dumps({"t": "h", "n": "FRAGMENTED_" + "X" * 300}).encode()
c.frame(0x1, part[:100], fin=False)
c.frame(0x9, b"hi")
c.frame(0x0, part[100:], fin=True)
op, payload = c.recv()
assert op == 0xA and payload == b"hi", (op, payload)
wait(lambda: got[-1][0].get("n", "").startswith("FRAGMENTED_"), what="fragmented")
assert len(got[-1][0]["n"]) == len("FRAGMENTED_") + 300
print("OK fragmented frames reassembled, ping answered with pong mid-message")

# broadcast to two clients (second uses a 16-bit length)
c2 = Client(port)
c2.send_json({"t": "hello", "panel": "A320_Neo_PFD"})
wait(lambda: len(srv.clients()) == 2, what="two clients")
srv.broadcast({"t": "h", "n": "A320_Neo_CDU_1_BTN_B", "pad": "Y" * 200})
for cl in (c, c2):
    op, payload = cl.recv()
    assert op == 0x1 and json.loads(payload)["n"] == "A320_Neo_CDU_1_BTN_B"
print("OK broadcast reaches every panel")

# client close -> dropped; dead socket dropped on broadcast
c2.frame(0x8, struct.pack("!H", 1000))
op, _ = c2.recv()
assert op == 0x8
wait(lambda: len(srv.clients()) == 1, what="c2 dropped")
c.s.close()
time.sleep(0.1)
srv.broadcast({"t": "x"})
wait(lambda: srv.clients() == [], what="dead client dropped")
assert len(changes) >= 4
print("OK close handshake, dead clients dropped, on_change fired")

srv.close()
time.sleep(0.05)
c3 = socket.socket()
c3.settimeout(1)
try:
    c3.connect(("127.0.0.1", port))
    raise AssertionError("server should be closed")
except OSError:
    pass
print("OK close() stops listening")
print("\nWSBRIDGE TESTS PASSED")
