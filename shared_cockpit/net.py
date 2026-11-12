"""TCP link between the two pilots: one port, newline-delimited JSON messages."""
import json
import socket
import threading
import time


class Link:
    """One live connection. on_message(dict) and on_close(reason) run on the reader thread."""

    def __init__(self, sock, on_message, on_close):
        self.sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(None)
        self.on_message, self.on_close = on_message, on_close
        self._wlock = threading.Lock()
        self.alive = True
        self.bytes_in = self.bytes_out = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, msg):
        if not self.alive:
            return
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            with self._wlock:
                self.sock.sendall(data)
            self.bytes_out += len(data)
        except OSError as e:
            self.close(f"send failed: {e}")

    def _reader(self):
        buf = b""
        try:
            while self.alive:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.bytes_in += len(chunk)
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        self.on_message(msg)
            self.close("peer disconnected")
        except OSError as e:
            self.close(f"connection lost: {e}")

    def close(self, reason="closed"):
        if not self.alive:
            return
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.on_close(reason)


class Host:
    """Listens on a port and accepts exactly one co-pilot at a time."""

    def __init__(self, port, on_connect, log):
        self.port, self.on_connect, self.log = port, on_connect, log
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("0.0.0.0", port))
        self.srv.listen(2)
        self.busy = False
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                sock, addr = self.srv.accept()
            except OSError:
                return
            if self.busy:
                self.log(f"Rejected extra connection from {addr[0]} (already have a co-pilot)")
                sock.close()
                continue
            self.log(f"Co-pilot connected from {addr[0]}:{addr[1]}")
            self.on_connect(sock)

    def stop(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass


def connect(ip, port, timeout=8.0):
    s = socket.create_connection((ip, port), timeout=timeout)
    return s


def now():
    return time.perf_counter()
