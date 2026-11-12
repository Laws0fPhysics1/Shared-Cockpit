"""Network transport between the two pilots.

TCP (reliable, ordered): handshake, switches, control handoff, pings.
UDP (fast, may drop): motion snapshots. If UDP can't get through (port not
forwarded), the engine falls back to sending motion over TCP.

Testing aid: set SC_NETSIM, e.g. "delay=0.08,jitter=0.03,loss=0.05", to make
the link behave like a bad internet connection (loss applies to UDP only;
TCP stays ordered).
"""
import heapq
import itertools
import json
import os
import random
import socket
import threading
import time


def now():
    return time.perf_counter()


# ------------------------------------------------------------------ impairment
def _parse_netsim(s):
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


NETSIM = _parse_netsim(os.environ.get("SC_NETSIM", ""))


class _Delayer:
    def __init__(self):
        self.heap, self.cv, self.seq = [], threading.Condition(), itertools.count()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, due, fn):
        with self.cv:
            heapq.heappush(self.heap, (due, next(self.seq), fn))
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                while not self.heap:
                    self.cv.wait()
                due, _, fn = self.heap[0]
                wait = due - now()
                if wait > 0:
                    self.cv.wait(wait)
                    continue
                heapq.heappop(self.heap)
            try:
                fn()
            except OSError:
                pass


_delayer = None
_delayer_lock = threading.Lock()


def _impaired(send, data, ordered_state=None):
    """Send now, or through the impairment simulator when SC_NETSIM is set."""
    global _delayer
    ns = NETSIM
    if not ns:
        send(data)
        return
    if ordered_state is None and random.random() < ns.get("loss", 0.0):
        return
    due = now() + ns.get("delay", 0.0) + random.uniform(0.0, ns.get("jitter", 0.0))
    if ordered_state is not None:  # TCP: keep order
        due = max(due, ordered_state.get("last", 0.0))
        ordered_state["last"] = due
    with _delayer_lock:
        if _delayer is None:
            _delayer = _Delayer()
    _delayer.submit(due, lambda: send(data))


def _encode(msg):
    return json.dumps(msg, separators=(",", ":")).encode()


# ------------------------------------------------------------------ TCP
class Link:
    """One live TCP connection. on_message(dict) and on_close(reason) run on the reader thread."""

    def __init__(self, sock, on_message, on_close):
        self.sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.settimeout(None)
        self.peer_ip = sock.getpeername()[0]
        self.on_message, self.on_close = on_message, on_close
        self._wlock = threading.Lock()
        self._order = {}
        self.alive = True
        self.bytes_in = self.bytes_out = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, msg):
        if self.alive:
            _impaired(self._raw_send, _encode(msg) + b"\n", self._order)

    def _raw_send(self, data):
        if not self.alive:
            return
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
    """Listens on a TCP port and accepts exactly one co-pilot at a time."""

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
    return socket.create_connection((ip, port), timeout=timeout)


# ------------------------------------------------------------------ UDP
class Udp:
    """UDP socket for motion. on_packet(msg, addr) runs on the reader thread."""

    def __init__(self, port, on_packet):
        self.on_packet = on_packet
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:  # Windows: don't kill recvfrom with ICMP "port unreachable" errors
            self.sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (AttributeError, OSError, ValueError):
            pass
        self.sock.bind(("0.0.0.0", port))
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, msg, addr):
        if self.alive:
            _impaired(lambda d: self.sock.sendto(d, addr), _encode(msg))

    def _reader(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(65535)
            except ConnectionResetError:
                continue
            except OSError:
                return
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.on_packet(msg, addr)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
