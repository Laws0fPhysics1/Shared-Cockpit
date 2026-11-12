"""Local WebSocket server for the in-sim cockpit hook.

MSFS runs each cockpit panel (MCDU, PFD, ...) in its own Coherent GT browser
context. The Shared Cockpit JS hook in each panel connects here
(ws://127.0.0.1:PORT), reports button presses (H: events) as JSON and receives
presses from the co-pilot to replay. Stdlib only, RFC 6455 subset:
text frames (with fragmentation), ping/pong, close. Binds 127.0.0.1 only.
"""
import base64
import hashlib
import itertools
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MSG = 1 << 20


class _Client:
    def __init__(self, sock, cid):
        self.sock, self.id = sock, cid
        self.panel = ""
        self.lock = threading.Lock()
        self.alive = True

    def send_frame(self, opcode, payload=b""):
        n = len(payload)
        if n < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        with self.lock:
            self.sock.sendall(hdr + payload)


class BridgeServer:
    def __init__(self, port=50556, on_message=None, log=print, on_change=None):
        self.port = port
        self.on_message = on_message or (lambda msg, cid: None)
        self.on_change = on_change
        self.log = log
        self._clients = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.running = True
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE
                            if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else socket.SO_REUSEADDR, 1)
        try:
            self.srv.bind(("127.0.0.1", port))
            self.srv.listen(16)
        except OSError:
            self.srv.close()
            raise
        threading.Thread(target=self._accept, daemon=True).start()

    # ---------------------------------------------------------------- public
    def broadcast(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode()
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            try:
                c.send_frame(0x1, data)
            except OSError:
                self._drop(c)

    def clients(self):
        with self._lock:
            return [{"id": c.id, "panel": c.panel} for c in self._clients.values()]

    def close(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            try:
                c.send_frame(0x8, struct.pack("!H", 1001))
            except OSError:
                pass
            self._drop(c)

    # ---------------------------------------------------------------- internals
    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _drop(self, c):
        with self._lock:
            had = self._clients.pop(c.id, None) is not None
        c.alive = False
        try:
            c.sock.close()
        except OSError:
            pass
        if had:
            self._changed()

    def _accept(self):
        while self.running:
            try:
                sock, _addr = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _recv_exact(self, sock, n, buf):
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf[:n], buf[n:]

    def _handshake(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk or len(data) > 16384:
                return None, b""
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        key = None
        for line in head.decode("latin-1").split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "sec-websocket-key":
                key = v.strip()
        if not key:
            sock.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return None, b""
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        return True, rest

    def _serve(self, sock):
        c = None
        try:
            sock.settimeout(None)
            ok, buf = self._handshake(sock)
            if not ok:
                sock.close()
                return
            c = _Client(sock, next(self._ids))
            with self._lock:
                self._clients[c.id] = c
            self._changed()
            frag_op, frag = None, b""
            while self.running and c.alive:
                hdr, buf = self._recv_exact(sock, 2, buf)
                b0, b1 = hdr[0], hdr[1]
                fin, op = b0 & 0x80, b0 & 0x0F
                masked, n = b1 & 0x80, b1 & 0x7F
                if n == 126:
                    ext, buf = self._recv_exact(sock, 2, buf)
                    n = struct.unpack("!H", ext)[0]
                elif n == 127:
                    ext, buf = self._recv_exact(sock, 8, buf)
                    n = struct.unpack("!Q", ext)[0]
                if n > MAX_MSG:
                    break
                mask = b""
                if masked:
                    mask, buf = self._recv_exact(sock, 4, buf)
                payload, buf = self._recv_exact(sock, n, buf) if n else (b"", buf)
                if masked and n:
                    payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
                if op == 0x8:                                   # close
                    try:
                        c.send_frame(0x8, payload[:2])
                    except OSError:
                        pass
                    break
                if op == 0x9:                                   # ping
                    c.send_frame(0xA, payload)
                    continue
                if op == 0xA:                                   # pong
                    continue
                if op in (0x1, 0x2):
                    frag_op, frag = op, payload
                elif op == 0x0 and frag_op is not None:         # continuation
                    frag += payload
                    if len(frag) > MAX_MSG:
                        break
                else:
                    continue
                if not fin:
                    continue
                data, op_done, frag_op, frag = frag, frag_op, None, b""
                if op_done != 0x1:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("t") == "hello":
                    c.panel = str(msg.get("panel", ""))[:120]
                    self._changed()
                try:
                    self.on_message(msg, c.id)
                except Exception as e:
                    try:
                        self.log(f"Bridge message handler error: {e!r}")
                    except Exception:
                        pass
        except (OSError, ConnectionError, struct.error):
            pass
        except Exception as e:
            try:
                self.log(f"Bridge connection error: {e!r}")
            except Exception:
                pass
        finally:
            if c is not None:
                self._drop(c)
            else:
                try:
                    sock.close()
                except OSError:
                    pass
