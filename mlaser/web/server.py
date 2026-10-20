"""Read-only HTTP status server for the LAN monitor (standard library only).

Resources (every one needs the access token, ``?t=<token>``):

``GET /``             the phone-friendly page (polls ``status.json`` every 2 s)
``GET /status.json``  the last published snapshot (state, progress, ETA, alarms, queue)
``GET /sheet.svg``    the current sheet thumbnail
``GET /health``       ``ok`` (no token needed, no data: for a reachability check)

Every other path is 404, every other method (POST, PUT, PATCH, DELETE, ...) is 405, a missing or
wrong token is 403 (compared in constant time).  There is no endpoint that changes anything:
the server holds no reference to the controller, only the snapshot dict the application
publishes (:meth:`MonitorServer.publish`).  Responses carry ``Cache-Control: no-store``,
``Referrer-Policy: no-referrer`` (the token is in the URL), ``X-Content-Type-Options: nosniff``,
``X-Frame-Options: DENY`` and a Content-Security-Policy that allows only the page's own script.

The server binds to the configured interface address only (``127.0.0.1`` by default; set the
machine PC's LAN address to reach it from a phone).  It is off unless the operator enables it.

It runs in the safety process (the one that reopens the halt channel and writes the audit log),
so a LAN peer must not be able to exhaust its threads or file descriptors (code review P1-4):

* every connection has a socket **timeout** (``timeout_s``, 10 s: an idle or half-open request
  is closed) and a **lifetime** (``max_connection_s``, 30 s, enforced by the serving thread even
  for a client that trickles one byte at a time);
* at most ``max_connections`` (16) connections are served at once, at most
  ``max_per_client`` (4) from one address; a connection over a cap is answered ``503`` at
  once (no thread) and closed gracefully: the request bytes are read and discarded, the 503 is
  followed by a FIN, and the accept loop closes the socket on the client's EOF or after
  ``REFUSED_LINGER_S`` (only while the sockets held, these included, stay within
  ``max_connections``; beyond that it is closed at once).  Closing a socket with unread
  request bytes sends a TCP reset, and macOS discards a 503 that a reset overtakes (the
  client saw only ``ECONNRESET``);
* a **rate limit** per client address (token bucket, ``rate_per_s`` 5 requests/s, burst 20);
  over it, ``429`` with ``Retry-After``;
* a request does no work on the GUI thread: it serves the bytes the application published
  last (:meth:`MonitorServer.publish`), the page is rendered from that copy;
* the access log carries the path only (never the token in the query).
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .page import render_page

log = logging.getLogger("mlaser.web")

READ_METHODS = ("GET", "HEAD")
DEFAULT_PORT = 8765
TIMEOUT_S = 10.0                 # socket timeout of a connection (idle / half-open requests)
MAX_CONNECTION_S = 30.0          # lifetime of one connection (slow clients)
MAX_CONNECTIONS = 16             # connections served at once
MAX_PER_CLIENT = 4               # ... from one client address
RATE_PER_S = 5.0                 # requests per second and client address (token bucket)
RATE_BURST = 20.0
_MAX_CLIENTS_TRACKED = 1024      # rate-limit buckets kept (oldest dropped)
REFUSED_LINGER_S = 1.0           # a refused connection is closed after the client's EOF or this
_MAX_LINGERING = 64              # refused connections lingering at once, at most
_DRAIN_BYTES = 65536             # read from a refused connection per pass, at most
_TOKEN_RE = re.compile(r"([?&]t=)[^&\s\"']*")


def new_token() -> str:
    """A fresh random access token (128 bits, URL-safe)."""
    return secrets.token_urlsafe(16)


def local_addresses() -> list[str]:
    """IPv4 addresses of this PC (for the interface choice); loopback first."""
    out = ["127.0.0.1"]
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            a = str(info[4][0])
            if a not in out:
                out.append(a)
    except OSError:
        pass
    try:                                          # the address of the default route
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))           # TEST-NET-1: nothing is sent for UDP connect
            a = s.getsockname()[0]
            if a not in out:
                out.append(a)
        finally:
            s.close()
    except OSError:
        pass
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "mlaser-monitor/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # the owning MonitorServer (set on the class created per server)
    monitor: "MonitorServer"

    def log_request(self, code: Any = "-", size: Any = "-") -> None:  # noqa: D401 - http.server API
        # the path only: the request line carries the access token in the query
        path = urlsplit(getattr(self, "path", "") or "").path
        log.debug("%s %s %s %s", self.address_string(), getattr(self, "command", "-"), path, code)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - http.server API
        log.debug("%s %s", self.address_string(), _TOKEN_RE.sub(r"\1<token>", fmt % args))

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if not self.monitor._allow(self.client_address[0]):
            self.monitor.limited += 1
            self.close_connection = True
            self._send(429, b"too many requests\n", extra={"Retry-After": "1", "Connection": "close"})
            return False
        return True

    # ------------------------------------------------------------------ helpers
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
              extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if code == 405:
            self.send_header("Allow", ", ".join(READ_METHODS))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self, query: dict) -> bool:
        given = (query.get("t") or [""])[0]
        return bool(given) and hmac.compare_digest(given.encode(), self.monitor.token.encode())

    def _refuse_method(self) -> None:
        self.monitor.refused += 1
        self._send(405, b"read-only monitor: method not allowed\n")

    # every state-changing verb is refused
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _refuse_method

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        u = urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/health":
            self._send(200, b"ok\n")
            return
        if path not in ("/", "/status.json", "/sheet.svg"):
            self._send(404, b"not found\n")
            return
        q = parse_qs(u.query)
        if not self._authorised(q):
            self.monitor.refused += 1
            self._send(403, b"forbidden: open the monitor link with its access token\n")
            return
        self.monitor.requests += 1
        snap, svg = self.monitor.current()
        if path == "/status.json":
            body = json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/sheet.svg":
            self._send(200, svg.encode("utf-8"), "image/svg+xml; charset=utf-8",
                       {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        else:
            nonce = secrets.token_urlsafe(12)
            page = render_page(snap, self.monitor.token, nonce, self.monitor.refresh_s)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                   "frame-ancestors 'none'")
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy": csp})


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with the connection caps and lifetimes of the module doc."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    monitor: "MonitorServer"

    _lingering: Optional[list] = None        # [(sock, close-by)] refused connections

    def process_request(self, request: Any, client_address: Any) -> None:
        m = self.monitor
        ip = str(client_address[0])
        if not m._enter(ip, request):
            m.rejected += 1
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            m._leave(ip, request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.monitor._leave(str(client_address[0]), request)

    @staticmethod
    def _drain(sock: Any) -> bool:
        """Read what the client sent, without blocking; True at its EOF (or an error)."""
        n = 0
        while n < _DRAIN_BYTES:
            try:
                chunk = sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                return False
            except OSError:
                return True
            if not chunk:
                return True
            n += len(chunk)
        return False

    def _refuse(self, request: Any) -> None:
        """The 503 of a connection over a cap, sent from the accept loop (no thread, no
        blocking): drain the request, answer, FIN, and linger until the client closes."""
        try:
            request.setblocking(False)
            eof = self._drain(request)
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                            b"Connection: close\r\nRetry-After: 2\r\n\r\n")
            request.shutdown(socket.SHUT_WR)
        except OSError:
            eof = True
        if self._lingering is None:
            self._lingering = []
        # the sockets held stay within max_connections, the lingering ones included (a flood
        # of refused connections costs no file descriptors): no room, close at once
        room = self.monitor.connections + len(self._lingering) < min(self.monitor.max_connections,
                                                                       _MAX_LINGERING)
        if eof or not room:
            self._close_quietly(request)
            return
        self._lingering.append((request, time.monotonic() + REFUSED_LINGER_S))

    _linger_next = 0.0

    def _linger(self) -> None:
        """Close the refused connections whose client is done (or whose time is up).  At most
        every 50 ms and only the readable ones: the accept loop keeps up with a flood."""
        now = time.monotonic()
        if not self._lingering or now < self._linger_next:
            return
        self._linger_next = now + 0.05
        try:
            readable = set(select.select([sk for sk, _t in self._lingering], [], [], 0)[0])
        except (OSError, ValueError):
            readable = {sk for sk, _t in self._lingering}
        keep = []
        for sock, t_end in self._lingering:
            if (sock in readable and self._drain(sock)) or now >= t_end:
                self._close_quietly(sock)
            else:
                keep.append((sock, t_end))
        self._lingering = keep

    @staticmethod
    def _close_quietly(sock: Any) -> None:
        try:
            sock.close()
        except OSError:
            pass

    def service_actions(self) -> None:
        self.monitor._reap()
        self._linger()

    def server_close(self) -> None:
        super().server_close()
        for sock, _t in self._lingering or ():
            self._close_quietly(sock)
        self._lingering = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        # a client that went away (reset, reaped) is not an application error: no stderr dump
        log.debug("LAN monitor: connection from %s ended with an error", client_address[0], exc_info=True)


class MonitorServer:
    """``start()`` serves on ``host:port`` in a daemon thread; ``publish(snapshot, svg)`` replaces
    what it shows; ``stop()`` closes the socket.  ``port=0`` binds a free port (``address``)."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str = "",
                 refresh_s: float = 2.0, *, timeout_s: float = TIMEOUT_S,
                 max_connection_s: float = MAX_CONNECTION_S, max_connections: int = MAX_CONNECTIONS,
                 max_per_client: int = MAX_PER_CLIENT, rate_per_s: float = RATE_PER_S,
                 rate_burst: float = RATE_BURST) -> None:
        if not token or len(token) < 12:
            raise ValueError("the LAN monitor needs an access token of at least 12 characters")
        self.host, self.port, self.token = host, int(port), token
        self.refresh_s = refresh_s
        self.timeout_s = float(timeout_s)
        self.max_connection_s = float(max_connection_s)
        self.max_connections = max(1, int(max_connections))
        self.max_per_client = max(1, int(max_per_client))
        self.rate_per_s = float(rate_per_s)
        self.rate_burst = max(1.0, float(rate_burst))
        self.requests = 0
        self.refused = 0
        self.rejected = 0            # connections over a cap (503)
        self.limited = 0             # requests over the rate limit (429)
        self.reaped = 0              # connections closed at the end of their lifetime
        self._conn_lock = threading.Lock()
        self._conns: dict[int, tuple[Any, str, float]] = {}    # id(sock) -> (sock, ip, start)
        self._per_ip: dict[str, int] = {}
        self._buckets: dict[str, list[float]] = {}             # ip -> [tokens, last time]
        self._lock = threading.Lock()
        self._snapshot: dict = {"state": "OFFLINE", "published": 0.0}
        self._svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ data
    def publish(self, snapshot: dict, svg: Optional[str] = None) -> None:
        snap = dict(snapshot)
        snap["published"] = time.time()
        with self._lock:
            self._snapshot = snap
            if svg is not None:
                self._svg = svg

    def current(self) -> tuple[dict, str]:
        with self._lock:
            return self._snapshot, self._svg

    # ------------------------------------------------------------------ limits
    @property
    def connections(self) -> int:
        """Connections being served now."""
        with self._conn_lock:
            return len(self._conns)

    def _enter(self, ip: str, sock: Any) -> bool:
        with self._conn_lock:
            if len(self._conns) >= self.max_connections or self._per_ip.get(ip, 0) >= self.max_per_client:
                return False
            self._conns[id(sock)] = (sock, ip, time.monotonic())
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            return True

    def _leave(self, ip: str, sock: Any) -> None:
        with self._conn_lock:
            if self._conns.pop(id(sock), None) is None:
                return
            n = self._per_ip.get(ip, 0) - 1
            if n > 0:
                self._per_ip[ip] = n
            else:
                self._per_ip.pop(ip, None)

    def _reap(self) -> None:
        """Close connections older than ``max_connection_s`` (a slow client that keeps sending a
        byte now and then never hits the socket timeout)."""
        now = time.monotonic()
        with self._conn_lock:
            old = [sock for sock, _ip, t0 in self._conns.values() if now - t0 > self.max_connection_s]
        for sock in old:
            self.reaped += 1
            try:
                sock.shutdown(socket.SHUT_RDWR)   # the handler thread's read returns, it exits
            except OSError:
                pass

    def _allow(self, ip: str) -> bool:
        """Token bucket per client address."""
        if self.rate_per_s <= 0:
            return True
        now = time.monotonic()
        with self._conn_lock:
            b = self._buckets.get(ip)
            if b is None:
                if len(self._buckets) >= _MAX_CLIENTS_TRACKED:
                    oldest = min(self._buckets, key=lambda k: self._buckets[k][1])
                    del self._buckets[oldest]
                b = self._buckets[ip] = [self.rate_burst, now]
            b[0] = min(self.rate_burst, b[0] + (now - b[1]) * self.rate_per_s)
            b[1] = now
            if b[0] < 1.0:
                return False
            b[0] -= 1.0
            return True

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is not None:
            h, p = self._httpd.server_address[:2]
            return str(h), int(p)
        return self.host, self.port

    def url(self, host: Optional[str] = None) -> str:
        h, p = self.address
        h = host or h
        if ":" in h:
            h = f"[{h}]"
        return f"http://{h}:{p}/?t={self.token}"

    def start(self) -> "MonitorServer":
        if self._httpd is not None:
            return self
        # ``timeout``: StreamRequestHandler.setup puts it on every connection's socket
        handler = type("MonitorHandler", (_Handler,), {"monitor": self, "timeout": self.timeout_s})
        server_cls = type("MonitorHTTPServer", (_Server,), {"monitor": self})
        httpd = server_cls((self.host, self.port), handler)
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="mlaser-lan-monitor", daemon=True)
        self._thread.start()
        log.warning("LAN monitor serving read-only status on %s:%d", *self.address)
        return self

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._conn_lock:
            socks = [c[0] for c in self._conns.values()]
        for sock in socks:                    # open keep-alive connections end with the server
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        log.info("LAN monitor stopped")


__all__ = ["MonitorServer", "new_token", "local_addresses", "READ_METHODS", "DEFAULT_PORT",
           "TIMEOUT_S", "MAX_CONNECTION_S", "MAX_CONNECTIONS", "MAX_PER_CLIENT", "RATE_PER_S", "RATE_BURST"]
