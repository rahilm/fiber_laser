"""The automation API: a local REST + WebSocket server, read-only except for queue intake.

Off by default; the operator or engineer enables it (Run > Automation API, docs/user/automation.md).
Every resource is under ``/api/v1`` (the OpenAPI description is :mod:`.openapi`):

==========  ===========================  ==================================================
method      path                         what
==========  ===========================  ==================================================
GET         ``/api/v1/health``           ``{"ok": true}`` (no token, no data)
GET         ``/api/v1/openapi.json``     the OpenAPI 3.1 description (no token)
GET         ``/api/v1/status``           the last published machine / job / queue status
GET         ``/api/v1/queue``            queue entries and the jobs waiting for approval
GET         ``/api/v1/events``           events after ``?after=<seq>`` (long poll ``&wait=<s>``)
GET         ``/api/v1/ws``               WebSocket: the same events, pushed (read-only)
POST        ``/api/v1/queue``            submit a job: it waits for the operator's approval
==========  ===========================  ==================================================

**No remote control.**  There is no endpoint that starts, stops, holds, resumes, arms, moves,
homes or approves anything, and the server cannot reach the machine: it holds no controller
(the import-linter contract forbids ``mlaser.api`` to import the controller, the devices, the
card protocol or the streamer), only the status dict and the events the application publishes
and one callable that drops a submission into the intake inbox (:mod:`mlaser.services.intake`).
A submitted job becomes a queue entry only when the operator approves it at the machine, and
it still runs only after "Sheet loaded: start" and the normal Start path.  Paths that sound like
control (``/start``, ``/stop``, ``/arm`` ...) answer ``403`` with the reason, so an integrator
learns it at once.  A remote **Stop** is deliberately not offered either: stopping belongs to
the e-stop chain and the operator at the machine (docs/dev/API.md "Why no remote stop").

Hardening (the same as the LAN monitor, :mod:`mlaser.web.server`, whose connection caps,
lifetimes and rate limit it reuses): localhost by default; every request needs the token
(``Authorization: Bearer <token>``, compared in constant time; the WebSocket also accepts
``?token=`` because browsers cannot set headers on it); socket timeouts, connection lifetime,
connection caps per server and per client, a token-bucket rate limit, a request body cap
(``Content-Length`` required, no chunked bodies), at most :attr:`max_ws` event streams with
their own lifetime and ping, and no work on the GUI thread (requests read what was published).
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import select
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from mlaser.web.server import MonitorServer, _Handler, _Server

from . import websocket as WS

log = logging.getLogger("mlaser.api")
_QUERY_TOKEN = re.compile(r"([?&](?:token|t)=)[^&\s\"']*")

PREFIX = "/api/v1"
DEFAULT_PORT = 8766
MAX_BODY = 1 * 1024 * 1024 + 4096        # a JSON submission without an upload
MAX_UPLOAD = 20 * 1024 * 1024            # an uploaded drawing (decoded)
EVENT_BUFFER = 1000                      # events kept for /events and late WebSocket readers
MAX_WS = 4                               # event streams at once
WS_LIFETIME_S = 3600.0                   # an event stream is closed after an hour (clients reconnect)
WS_PING_S = 15.0
LONG_POLL_MAX_S = 20.0
#: words that look like machine control: answered 403 with the reason (never implemented)
CONTROL_WORDS = ("start", "stop", "hold", "pause", "resume", "continue", "arm", "disarm", "move", "jog",
                 "home", "frame", "dry_run", "fire", "laser", "reset", "approve", "estop", "e-stop", "abort",
                 "run", "cancel", "skip", "outputs", "gas")

Submit = Callable[[dict, str], dict]          # (request, client address) -> pending job (public dict)
Upload = Callable[[str, bytes], str]          # (file name, content) -> stored path


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status, self.code, self.extra = status, code, dict(extra or {})


class _ApiHandler(_Handler):
    server_version = "mlaser-api/1"
    monitor: "AutomationServer"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - http.server API
        # never the token: an error line can quote the request line with ?token=
        log.debug("%s %s", self.address_string(), _QUERY_TOKEN.sub(r"\1<token>", fmt % args))

    # ------------------------------------------------------------------ responses
    def _json(self, code: int, obj: Any, extra: Optional[dict] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _error(self, e: ApiError) -> None:
        self.monitor.refused += 1 if e.status in (401, 403, 405) else 0
        hdr = {"WWW-Authenticate": 'Bearer realm="mlaser"'} if e.status == 401 else None
        self._json(e.status, {"error": {"code": e.code, "message": str(e), **e.extra}}, hdr)

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
              extra: Optional[dict] = None) -> None:
        extra = dict(extra or {})
        if code == 405:
            extra.setdefault("Allow", "GET, HEAD, POST" if self._path() == PREFIX + "/queue" else "GET, HEAD")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ------------------------------------------------------------------ helpers
    def _path(self) -> str:
        return urlsplit(self.path).path.rstrip("/") or "/"

    def _query(self) -> dict:
        return parse_qs(urlsplit(self.path).query)

    def _token_ok(self, allow_query: bool = False) -> bool:
        tok = self.monitor.token.encode()
        auth = self.headers.get("Authorization", "")
        if auth[:7].lower() == "bearer ":
            given = auth[7:].strip().encode()
            if given and hmac.compare_digest(given, tok):
                return True
        if allow_query:
            q = (self._query().get("token") or [""])[0].encode()
            if q and hmac.compare_digest(q, tok):
                return True
        return False

    def _auth(self, allow_query: bool = False) -> None:
        if not self._token_ok(allow_query):
            raise ApiError(401, "unauthorized", "a valid token is required (Authorization: Bearer <token>)")

    def _control_refusal(self, path: str) -> Optional[ApiError]:
        words = [w.lower() for w in path.split("/") if w]
        if any(w in CONTROL_WORDS for w in words):
            return ApiError(403, "no_remote_control",
                            "the automation API cannot control the machine: no remote start, stop, hold, arm, "
                            "move or approval. Start and stop at the machine; the e-stop chain is hardware.")
        return None

    # ------------------------------------------------------------------ verbs
    def _refuse_method(self) -> None:          # PUT, PATCH, DELETE, OPTIONS, TRACE, CONNECT
        try:
            path = self._path()
            e = self._control_refusal(path)
            if e is not None:
                raise e
            raise ApiError(405, "method_not_allowed", "read-only API: only GET, and POST /api/v1/queue")
        except ApiError as err:
            self.monitor.refused += 1
            self.close_connection = True
            self._error(err)

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = _refuse_method

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        try:
            self._get()
        except ApiError as e:
            self._error(e)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        try:
            self._post()
        except ApiError as e:
            self.close_connection = True       # an unread body must not be taken for a request
            self._error(e)

    def _get(self) -> None:
        m = self.monitor
        path = self._path()
        if path == PREFIX + "/health":
            self._json(200, {"ok": True, "api": "mlaser-automation/1"})
            return
        if path == PREFIX + "/openapi.json":
            from .openapi import openapi
            self._json(200, openapi(m.allow_queue))
            return
        if not path.startswith(PREFIX + "/"):
            raise ApiError(404, "not_found", "unknown resource (see /api/v1/openapi.json)")
        e = self._control_refusal(path)
        if e is not None:
            raise e
        if path == PREFIX + "/ws":
            self._auth(allow_query=True)
            self._websocket()
            return
        self._auth()
        m.requests += 1
        if path == PREFIX + "/status":
            self._json(200, m.status())
        elif path == PREFIX + "/queue":
            st = m.status()
            self._json(200, {"entries": st.get("queue", []), "pending_approval": st.get("pending_approval", []),
                             "queue_intake": m.allow_queue, "published": st.get("published")})
        elif path == PREFIX + "/events":
            q = self._query()
            try:
                after = int((q.get("after") or ["0"])[0])
                limit = max(1, min(500, int((q.get("limit") or ["100"])[0])))
                wait = max(0.0, min(LONG_POLL_MAX_S, float((q.get("wait") or ["0"])[0])))
            except ValueError:
                raise ApiError(400, "bad_request", "after, limit and wait must be numbers") from None
            evs, nxt, lost = m.events_after(after, limit, wait)
            self._json(200, {"events": evs, "next": nxt, "lost": lost})
        else:
            raise ApiError(404, "not_found", "unknown resource (see /api/v1/openapi.json)")

    def _post(self) -> None:
        m = self.monitor
        path = self._path()
        e = self._control_refusal(path)
        if e is not None:
            raise e
        if path != PREFIX + "/queue":
            if not path.startswith(PREFIX + "/"):
                raise ApiError(404, "not_found", "unknown resource (see /api/v1/openapi.json)")
            raise ApiError(405, "method_not_allowed", "the only write is POST /api/v1/queue (submit a job "
                           "for approval)")
        self._auth()
        if not m.allow_queue or m.submit is None:
            raise ApiError(403, "queue_intake_off", "queue intake is switched off in the API settings")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required", "send the body with Content-Length (no chunked encoding)")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "length_required", "Content-Length is required") from None
        cap = m.max_body
        if n < 0 or n > cap:
            raise ApiError(413, "too_large", f"the body is limited to {cap} bytes")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type", "send application/json")
        raw = self.rfile.read(n) if n else b""
        if len(raw) < n:
            raise ApiError(400, "bad_request", "the body ended early")
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_json", "the body is not valid JSON") from None
        if not isinstance(req, dict):
            raise ApiError(400, "bad_request", "the body must be a JSON object")
        m.requests += 1
        result = m.handle_submission(req, str(self.client_address[0]))
        self._json(202, result, {"Location": PREFIX + "/queue"})

    # ------------------------------------------------------------------ websocket
    def _websocket(self) -> None:
        m = self.monitor
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or \
                "upgrade" not in (self.headers.get("Connection") or "").lower():
            raise ApiError(426, "upgrade_required", "connect with a WebSocket client", {})
        key = self.headers.get("Sec-WebSocket-Key")
        if not WS.valid_key(key) or self.headers.get("Sec-WebSocket-Version") != "13":
            raise ApiError(400, "bad_handshake", "bad Sec-WebSocket-Key or version")
        sock = self.connection
        if not m._ws_enter(sock):
            raise ApiError(503, "too_many_streams", f"at most {m.max_ws} event streams at once")
        try:
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", WS.accept_key(str(key)))
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            m.requests += 1
            self._ws_loop(sock)
        finally:
            m._ws_leave(sock)

    def _ws_send(self, opcode: int, payload: bytes) -> None:
        self.wfile.write(WS.encode(opcode, payload))
        self.wfile.flush()

    def _ws_loop(self, sock: Any) -> None:
        m = self.monitor
        t0 = time.monotonic()
        seq = m.last_seq()
        self._ws_send(WS.OP_TEXT, json.dumps({"type": "hello", "seq": seq, "status": m.status()},
                                             ensure_ascii=False, default=str).encode("utf-8"))
        last_ping = last_pong = time.monotonic()
        try:
            while m.running and not m._closing.is_set():
                now = time.monotonic()
                if now - t0 > m.ws_lifetime_s:
                    self._ws_close(WS.CLOSE_TRY_AGAIN, "lifetime reached: reconnect")
                    return
                evs, nxt, lost = m.events_after(seq, 100, min(0.5, m.ws_ping_s / 2))
                if lost:
                    self._ws_send(WS.OP_TEXT, json.dumps({"type": "lost", "events": lost}).encode())
                for ev in evs:
                    self._ws_send(WS.OP_TEXT, json.dumps(ev, ensure_ascii=False, default=str).encode("utf-8"))
                seq = nxt
                while True:                            # client frames (control only)
                    r, _, _ = select.select([sock], [], [], 0)
                    if not r:
                        break
                    op, data = WS.read_frame(self.rfile)
                    if op == WS.OP_CLOSE:
                        self._ws_close(WS.CLOSE_NORMAL, "")
                        return
                    if op == WS.OP_PING:
                        self._ws_send(WS.OP_PONG, data)
                    elif op == WS.OP_PONG:
                        last_pong = time.monotonic()
                    # text / binary frames: read-only stream, ignored (nothing is acted on)
                now = time.monotonic()
                if now - last_ping > m.ws_ping_s:
                    if last_pong < last_ping:          # no answer within a whole ping interval
                        self._ws_close(WS.CLOSE_POLICY, "no pong")
                        return
                    self._ws_send(WS.OP_PING, b"mlaser")
                    last_ping = now
        except WS.ProtocolError as exc:
            try:
                self._ws_close(exc.code, str(exc))
            except OSError:
                pass
        except (OSError, EOFError, ValueError):
            return

    def _ws_close(self, code: int, reason: str) -> None:
        try:
            self._ws_send(WS.OP_CLOSE, WS.close_payload(code, reason))
        except OSError:
            pass


class AutomationServer(MonitorServer):
    """``start()`` serves the API in a daemon thread; the application publishes
    :meth:`publish_status` and :meth:`publish_event`, and gives ``submit`` / ``upload`` (the
    intake store) when queue intake is on.  ``port=0`` binds a free port (``address``)."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str = "", *,
                 submit: Optional[Submit] = None, upload: Optional[Upload] = None, allow_queue: bool = False,
                 max_upload: int = MAX_UPLOAD, max_ws: int = MAX_WS, ws_lifetime_s: float = WS_LIFETIME_S,
                 ws_ping_s: float = WS_PING_S, event_buffer: int = EVENT_BUFFER, **limits: Any) -> None:
        if not token or len(token) < 24:
            raise ValueError("the automation API needs a token of at least 24 characters")
        super().__init__(host, port, token, **limits)
        self.submit = submit
        self.upload = upload
        self.allow_queue = bool(allow_queue)
        self.max_upload = int(max_upload)
        self.max_ws = max(1, int(max_ws))
        self.ws_lifetime_s = float(ws_lifetime_s)
        self.ws_ping_s = float(ws_ping_s)
        self.submissions = 0
        self._events: deque = deque(maxlen=max(10, int(event_buffer)))
        self._seq = 0
        self._cond = threading.Condition()
        self._ws: set[int] = set()
        self._closing = threading.Event()
        self._status: dict = {"state": "offline", "published": 0.0}

    # ------------------------------------------------------------------ publishing (app)
    @property
    def max_body(self) -> int:
        # base64 grows by 4/3; the JSON fields around it are small
        return max(MAX_BODY, self.max_upload * 4 // 3 + 64 * 1024) if self.upload is not None else MAX_BODY

    def publish_status(self, status: dict) -> None:
        snap = dict(status)
        snap["published"] = time.time()
        with self._lock:
            self._status = snap

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def publish_event(self, kind: str, data: Optional[dict] = None) -> int:
        """Append one event (any thread; never blocks on a client).  Returns its sequence
        number."""
        with self._cond:
            self._seq += 1
            ev = {"seq": self._seq, "t": time.time(), "type": str(kind), **(data or {})}
            self._events.append(ev)
            self._cond.notify_all()
            return self._seq

    def last_seq(self) -> int:
        with self._cond:
            return self._seq

    def events_after(self, after: int, limit: int = 100, wait: float = 0.0) -> tuple[list, int, int]:
        """Events with ``seq > after`` (at most ``limit``), waiting up to ``wait`` s for one.
        Returns ``(events, next after, events lost)`` (lost: dropped from the buffer before
        this reader got them)."""
        deadline = time.monotonic() + max(0.0, wait)
        with self._cond:
            while self._seq <= after and wait > 0 and not self._closing.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            evs = [e for e in self._events if e["seq"] > after][:limit]
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            lost = max(0, oldest - after - 1) if after < self._seq else 0
            # no news: stay at ``after`` (or at the last event of this run, for a reader that
            # remembers a sequence number of an earlier run)
            nxt = evs[-1]["seq"] if evs else min(after, self._seq)
            return evs, nxt, lost

    # ------------------------------------------------------------------ intake
    def handle_submission(self, req: dict, client: str) -> dict:
        """POST /queue: optional upload (``filename`` + ``content_base64``), then the intake
        submit.  Raises ApiError with the refusal."""
        if self.submit is None:
            raise ApiError(403, "queue_intake_off", "queue intake is switched off")
        req = dict(req)
        content = req.pop("content_base64", None)
        filename = req.pop("filename", None)
        if content is not None:
            if self.upload is None:
                raise ApiError(403, "upload_off", "uploads are switched off: give a path in a job folder")
            if not isinstance(content, str) or not isinstance(filename, str):
                raise ApiError(400, "bad_request", "filename and content_base64 must be strings")
            try:
                data = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(400, "bad_base64", "content_base64 is not valid base64") from None
            if len(data) > self.max_upload:
                raise ApiError(413, "too_large", f"uploads are limited to {self.max_upload} bytes")
            if "path" in req:
                raise ApiError(400, "bad_request", "give either path or filename + content_base64")
            req["path"] = self._call_upload(filename, data)
        elif filename is not None:
            raise ApiError(400, "bad_request", "filename without content_base64")
        result = self._call_submit(req, client)
        self.submissions += 1
        self.publish_event("intake_submitted", {"job": result})
        return result

    def _call_upload(self, filename: str, data: bytes) -> str:
        assert self.upload is not None
        try:
            return self.upload(filename, data)
        except ValueError as exc:                # IntakeError
            raise ApiError(400 if getattr(exc, "code", "") != "too_large" else 413,
                           getattr(exc, "code", "invalid"), str(exc)) from None

    def _call_submit(self, req: dict, client: str) -> dict:
        assert self.submit is not None
        try:
            return self.submit(req, client)
        except ValueError as exc:                # IntakeError (code: full, forbidden_path, ...)
            code = getattr(exc, "code", "invalid")
            status = {"full": 429, "forbidden_path": 403, "not_found": 404, "too_large": 413}.get(code, 400)
            raise ApiError(status, code, str(exc)) from None

    # ------------------------------------------------------------------ websocket bookkeeping
    def _ws_enter(self, sock: Any) -> bool:
        with self._conn_lock:
            if len(self._ws) >= self.max_ws:
                return False
            self._ws.add(id(sock))
            return True

    def _ws_leave(self, sock: Any) -> None:
        with self._conn_lock:
            self._ws.discard(id(sock))

    @property
    def streams(self) -> int:
        with self._conn_lock:
            return len(self._ws)

    def _reap(self) -> None:
        """Connection lifetimes; an event stream has its own (``ws_lifetime_s``, enforced by its
        thread, plus a margin here)."""
        now = time.monotonic()
        with self._conn_lock:
            old = [sock for sock, _ip, t0 in self._conns.values()
                   if now - t0 > (self.ws_lifetime_s + 30.0 if id(sock) in self._ws else self.max_connection_s)]
        for sock in old:
            self.reaped += 1
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "AutomationServer":
        if self._httpd is not None:
            return self
        self._closing.clear()
        handler = type("ApiHandler", (_ApiHandler,), {"monitor": self, "timeout": self.timeout_s})
        server_cls = type("ApiHTTPServer", (_Server,), {"monitor": self})
        httpd = server_cls((self.host, self.port), handler)
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="mlaser-automation-api", daemon=True)
        self._thread.start()
        log.warning("automation API serving on %s:%d (read-only; queue intake %s)", *self.address,
                    "on, operator approval required" if self.allow_queue else "off")
        return self

    def stop(self) -> None:
        self._closing.set()
        with self._cond:
            self._cond.notify_all()
        super().stop()

    def url(self, host: Optional[str] = None) -> str:
        h, p = self.address
        h = host or h
        if ":" in h:
            h = f"[{h}]"
        return f"http://{h}:{p}{PREFIX}/"


__all__ = ["AutomationServer", "ApiError", "PREFIX", "DEFAULT_PORT", "CONTROL_WORDS", "MAX_UPLOAD", "MAX_WS"]
