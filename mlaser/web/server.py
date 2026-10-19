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
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
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

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - http.server API
        log.debug("%s %s", self.address_string(), fmt % args)

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


class MonitorServer:
    """``start()`` serves on ``host:port`` in a daemon thread; ``publish(snapshot, svg)`` replaces
    what it shows; ``stop()`` closes the socket."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str = "",
                 refresh_s: float = 2.0) -> None:
        if not token or len(token) < 12:
            raise ValueError("the LAN monitor needs an access token of at least 12 characters")
        self.host, self.port, self.token = host, int(port), token
        self.refresh_s = refresh_s
        self.requests = 0
        self.refused = 0
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
        handler = type("MonitorHandler", (_Handler,), {"monitor": self})
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
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
        log.info("LAN monitor stopped")


__all__ = ["MonitorServer", "new_token", "local_addresses", "READ_METHODS", "DEFAULT_PORT"]
