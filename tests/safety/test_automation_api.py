"""The automation API cannot control the machine (docs/dev/API.md, docs/SAFETY.md).

* auth: every resource but /health and /openapi.json needs the token (Bearer; the WebSocket also
  ?token=), compared in constant time;
* read-only: every method on every path, and every control-sounding path, is refused; the only
  write is POST /queue, which drops a submission into the intake inbox;
* approval: a submitted job is pending (not in the queue, the runner cannot run it) until the
  operator approves it at the machine; approving only queues it;
* **no endpoint can start motion**: the whole battery runs against the real application wiring
  with a spy controller, and not one motion, arming or output method is called;
* hardening: the LAN monitor's limits (connection caps, rate limit) plus body caps, chunked
  bodies refused, at most N event streams, the token never in the log;
* the package cannot import the controller (also an import-linter contract).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from mlaser.api import AutomationServer
from mlaser.api import websocket as WS

TOKEN = "t" * 32
DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
GEAR = os.path.join(DATA, "dxf", "gear.dxf")

#: every MachineAPI member that moves, fires, arms, switches an output or changes the safety state
MACHINE_ACTIONS = ("arm", "disarm", "start", "start_job", "dry_run", "frame", "pause", "resume", "resume_saved",
                   "stop", "step_backward", "step_forward", "move_to", "move_by", "return_zero", "home",
                   "startup_homing", "jog_start", "jog_step", "jog_stop", "set_origin", "go_origin_point",
                   "set_output", "set_gas_type", "set_laser_power", "laser_press", "laser_release", "laser_off",
                   "laser_test", "reset_fault", "operator_action", "safe_halt", "clear_alarms", "heartbeat_hold",
                   "connect", "disconnect", "configure_safety", "raise_fault")
PATHS = ("/", "/api", "/api/v1", "/api/v1/status", "/api/v1/queue", "/api/v1/events", "/api/v1/ws",
         "/api/v1/health", "/api/v1/openapi.json", "/api/v1/queue/abc", "/api/v1/queue/abc/approve",
         "/api/v1/start", "/api/v1/stop", "/api/v1/hold", "/api/v1/resume", "/api/v1/arm", "/api/v1/disarm",
         "/api/v1/jog", "/api/v1/move", "/api/v1/home", "/api/v1/frame", "/api/v1/laser", "/api/v1/reset",
         "/api/v1/approve", "/api/v1/estop", "/api/v1/machine/start", "/api/v1/outputs/gas", "/status.json")
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


# ------------------------------------------------------------------------------ helpers
def http(srv, method, path, body=None, token=TOKEN, ctype="application/json", headers=None):
    h, p = srv.address
    req = urllib.request.Request(f"http://{h}:{p}{path}", method=method, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", ctype)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as f:
            data = f.read()
            return f.status, (json.loads(data) if data and f.headers.get_content_type() == "application/json" else data)
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except ValueError:
            return e.code, data


def raw(srv, data: bytes, timeout=10.0) -> bytes:
    s = socket.create_connection(srv.address, timeout=timeout)
    try:
        s.sendall(data)
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


def ws_connect(srv, token=TOKEN, query=True):
    s = socket.create_connection(srv.address, timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    path = f"/api/v1/ws?token={token}" if query else "/api/v1/ws"
    hdr = "" if query else f"Authorization: Bearer {token}\r\n"
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{hdr}\r\n").encode())
    f = s.makefile("rb")
    status = f.readline()
    headers = {}
    while True:
        ln = f.readline()
        if ln in (b"\r\n", b""):
            break
        k, _, v = ln.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    return s, f, status, headers, key


def ws_read(f):
    op, data = WS.read_frame(f, 1 << 22, require_mask=False)
    return op, (json.loads(data) if op == WS.OP_TEXT else data)


@pytest.fixture()
def server():
    made = []

    def make(**kw):
        calls = []
        kw.setdefault("submit", lambda req, client: (calls.append((req, client)),
                                                     {"id": "x1", "status": "pending_approval"})[1])
        kw.setdefault("allow_queue", True)
        kw.setdefault("rate_per_s", 0.0)                # the rate limit has its own test
        srv = AutomationServer("127.0.0.1", 0, TOKEN, **kw).start()
        srv.calls = calls
        made.append(srv)
        return srv
    yield make
    for s in made:
        s.stop()


# ------------------------------------------------------------------------------ auth
def test_every_resource_needs_the_token(server):
    srv = server()
    assert http(srv, "GET", "/api/v1/health", token="")[0] == 200
    code, spec = http(srv, "GET", "/api/v1/openapi.json", token="")
    assert code == 200 and spec["openapi"].startswith("3.1") and "/queue" in spec["paths"]
    for path in ("/api/v1/status", "/api/v1/queue", "/api/v1/events"):
        for tok in ("", "wrong", TOKEN[:-1], TOKEN + "x"):
            code, body = http(srv, "GET", path, token=tok)
            assert code == 401 and body["error"]["code"] == "unauthorized", (path, tok)
        assert http(srv, "GET", path)[0] == 200
    # REST does not take the token from the URL (it would end up in proxy logs)
    assert http(srv, "GET", f"/api/v1/status?token={TOKEN}", token="")[0] == 401
    code, _ = http(srv, "POST", "/api/v1/queue", json.dumps({"path": "x.dxf"}).encode(), token="")
    assert code == 401 and srv.calls == []
    # WebSocket: query or header token, nothing without
    s, f, status, _h, _k = ws_connect(srv, token="nope")
    assert b"401" in status
    s.close()
    with pytest.raises(ValueError):
        AutomationServer("127.0.0.1", 0, "short")


def test_the_token_never_reaches_the_log(server, caplog):
    srv = server()
    caplog.set_level(logging.DEBUG, logger="mlaser")
    s, f, status, _h, _k = ws_connect(srv)
    assert b"101" in status
    s.close()
    raw(srv, f"GARBAGE /api/v1/ws?token={TOKEN} HTTP/9\r\n\r\n".encode(), timeout=2)
    http(srv, "GET", f"/api/v1/status?token={TOKEN}", token="")
    time.sleep(0.2)
    assert TOKEN not in caplog.text


# ------------------------------------------------------------------------------ read-only
def test_read_only_every_method_and_path(server):
    srv = server()
    for path in PATHS:
        for method in METHODS:
            if (method, path) in (("POST", "/api/v1/queue"),):
                continue
            body = b"{}" if method in ("POST", "PUT", "PATCH") else None
            code, resp = http(srv, method, path, body)
            if method in ("GET", "HEAD") and path in ("/api/v1/status", "/api/v1/queue", "/api/v1/events",
                                                      "/api/v1/health", "/api/v1/openapi.json"):
                assert code == 200, (method, path)
            else:
                assert code in (400, 403, 404, 405, 426), (method, path, code)
    assert srv.calls == []                                   # nothing reached the intake


@pytest.mark.parametrize("word", ["start", "stop", "hold", "resume", "arm", "disarm", "move", "jog", "home",
                                  "frame", "laser", "reset", "approve", "estop"])
def test_control_paths_say_why(server, word):
    srv = server()
    for method in ("GET", "POST", "PUT"):
        code, body = http(srv, method, f"/api/v1/{word}", b"{}" if method != "GET" else None)
        assert code == 403 and body["error"]["code"] == "no_remote_control", (method, word)


def test_queue_submission_validation(server, tmp_path):
    srv = server(allow_queue=False, submit=None)
    code, body = http(srv, "POST", "/api/v1/queue", b'{"path": "x.dxf"}')
    assert code == 403 and body["error"]["code"] == "queue_intake_off"
    srv = server()
    assert http(srv, "POST", "/api/v1/queue", b'{"path": "x.dxf"}', ctype="text/plain")[0] == 415
    assert http(srv, "POST", "/api/v1/queue", b"{nope")[0] == 400
    assert http(srv, "POST", "/api/v1/queue", b"[1, 2]")[0] == 400
    assert http(srv, "POST", "/api/v1/queue", json.dumps({"filename": "a.dxf", "content_base64": "AAAA"}).encode())[0] == 403
    assert srv.calls == []
    code, body = http(srv, "POST", "/api/v1/queue", b'{"path": "x.dxf", "quantity": 2}')
    assert code == 202 and body["status"] == "pending_approval"
    assert srv.calls == [({"path": "x.dxf", "quantity": 2}, "127.0.0.1")]
    # bodies: Content-Length required, chunked refused, capped before reading
    r = raw(srv, (f"POST /api/v1/queue HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                  "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{}{}{\r\n0\r\n\r\n").encode())
    assert r.startswith(b"HTTP/1.1 411")
    r = raw(srv, (f"POST /api/v1/queue HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
                  "Content-Type: application/json\r\nContent-Length: 999999999\r\n\r\n{").encode())
    assert r.startswith(b"HTTP/1.1 413")
    assert len(srv.calls) == 1


def test_upload_limits(server):
    stored = []
    srv = server(upload=lambda name, data: stored.append((name, data)) or f"/x/{name}", max_upload=1000)
    ok = json.dumps({"filename": "a.dxf", "content_base64": base64.b64encode(b"0\nEOF\n").decode()}).encode()
    assert http(srv, "POST", "/api/v1/queue", ok)[0] == 202 and stored == [("a.dxf", b"0\nEOF\n")]
    big = json.dumps({"filename": "a.dxf", "content_base64": base64.b64encode(b"x" * 1001).decode()}).encode()
    assert http(srv, "POST", "/api/v1/queue", big)[0] == 413
    bad = json.dumps({"filename": "a.dxf", "content_base64": "%%%"}).encode()
    assert http(srv, "POST", "/api/v1/queue", bad)[0] == 400
    both = json.dumps({"filename": "a.dxf", "content_base64": "AAAA", "path": "/etc/x.dxf"}).encode()
    assert http(srv, "POST", "/api/v1/queue", both)[0] == 400
    assert len(stored) == 1


# ------------------------------------------------------------------------------ events
def test_events_poll_and_websocket(server):
    srv = server(ws_ping_s=0.3)
    srv.publish_status({"state": "ready"})
    srv.publish_event("job_started", {"mode": "process", "source": "real"})
    code, body = http(srv, "GET", "/api/v1/events?after=0")
    assert code == 200 and [e["type"] for e in body["events"]] == ["job_started"] and body["next"] == 1
    t0 = time.monotonic()
    code, body = http(srv, "GET", "/api/v1/events?after=1&wait=0.3")
    assert code == 200 and body["events"] == [] and body["next"] == 1 and time.monotonic() - t0 >= 0.25
    s, f, status, headers, key = ws_connect(srv, query=False)
    assert b"101" in status and headers["sec-websocket-accept"] == WS.accept_key(key)
    op, hello = ws_read(f)
    assert hello["type"] == "hello" and hello["status"]["state"] == "ready" and hello["seq"] == 1
    srv.publish_event("alarm", {"code": "5"})
    op, ev = ws_read(f)
    assert op == WS.OP_TEXT and ev["type"] == "alarm" and ev["seq"] == 2
    # a client data frame is ignored (read-only); the server pings, we answer
    s.sendall(WS.encode(WS.OP_TEXT, b'{"cmd": "start"}', mask=True))
    op, data = ws_read(f)
    assert op == WS.OP_PING
    s.sendall(WS.encode(WS.OP_PONG, data, mask=True))
    srv.publish_event("state", {"new": "armed"})
    op, ev = ws_read(f)
    while op == WS.OP_PING:
        s.sendall(WS.encode(WS.OP_PONG, ev, mask=True))
        op, ev = ws_read(f)
    assert ev["type"] == "state"
    s.sendall(WS.encode(WS.OP_CLOSE, WS.close_payload(1000), mask=True))
    op, _ = ws_read(f)
    assert op == WS.OP_CLOSE
    s.close()
    # an unmasked client frame closes the stream
    s, f, status, _h, _k = ws_connect(srv)
    ws_read(f)
    s.sendall(WS.encode(WS.OP_TEXT, b"x"))
    op, data = ws_read(f)
    assert op == WS.OP_CLOSE and data[:2] == (WS.CLOSE_PROTOCOL).to_bytes(2, "big")
    s.close()


def test_event_buffer_reports_lost_events(server):
    srv = server(event_buffer=10)
    for i in range(25):
        srv.publish_event("tick", {"i": i})
    code, body = http(srv, "GET", "/api/v1/events?after=0&limit=500")
    assert body["lost"] == 15 and [e["seq"] for e in body["events"]] == list(range(16, 26))


# ------------------------------------------------------------------------------ hardening
def test_stream_cap_and_connection_limits(server):
    srv = server(max_ws=1, max_per_client=2)
    s1, f1, st1, _h, _k = ws_connect(srv)
    assert b"101" in st1
    s2, f2, st2, _h, _k = ws_connect(srv)
    assert b"503" in st2
    s2.close()
    held = socket.create_connection(srv.address, timeout=5)           # s1 + this one = 2
    time.sleep(0.2)
    r = raw(srv, b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert r.startswith(b"HTTP/1.1 503")
    held.close()
    s1.close()


def test_a_refused_connection_gets_its_503_without_a_reset(server):
    """macOS CI (3.13): ``ECONNRESET`` instead of the 503.  The accept loop answered and closed
    the socket with the request still unread, and a close with unread bytes sends a TCP reset,
    which on macOS discards the 503 the client has not read yet.  The server now reads the
    request, sends the 503 and a FIN, and closes after the client's EOF: a client that is still
    sending after the refusal is not reset (Linux shows the reset as EPIPE on the next send)."""
    srv = server(max_per_client=1)
    held = socket.create_connection(srv.address, timeout=5)
    time.sleep(0.2)
    s = socket.create_connection(srv.address, timeout=5)
    try:
        s.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(0.6)                                  # refused by now (accept loop: 0.25 s)
        s.sendall(b"\r\n")
        time.sleep(0.1)
        s.sendall(b"\r\n")                               # a reset makes this raise
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        assert out.startswith(b"HTTP/1.1 503") and b"Retry-After" in out
    finally:
        s.close()
        held.close()
    assert srv.rejected >= 1


def test_rate_limit(server):
    srv = server(rate_per_s=0.5, rate_burst=3)
    codes = [http(srv, "GET", "/api/v1/status")[0] for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_the_api_package_cannot_reach_the_machine():
    code = ("import sys, mlaser.api, mlaser.api.server, mlaser.api.openapi, mlaser.notify\n"
            "bad = sorted(m for m in sys.modules if m.startswith(('mlaser.controller', 'mlaser.devices', "
            "'mlaser.protocol', 'mlaser.nc.stream', 'mlaser.machine.api', 'mlaser.machine.safety', 'PySide6')))\n"
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "[]", out.stderr
    srv = AutomationServer("127.0.0.1", 0, TOKEN)
    held = {type(v).__module__ for v in vars(srv).values()}
    assert not any(m.startswith(("mlaser.controller", "mlaser.machine", "mlaser.ui")) for m in held)


# ------------------------------------------------------------------------------ the real wiring
@pytest.fixture()
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def spy_controller():
    from mlaser.ui.facade import DemoController

    class Spy(DemoController):
        record = False
        calls: list = []

    for name in MACHINE_ACTIONS:
        orig = getattr(DemoController, name, None)
        if orig is None:
            continue

        def wrap(self, *a, _orig=orig, _name=name, **k):
            if type(self).record:
                type(self).calls.append(_name)
            return _orig(self, *a, **k)
        setattr(Spy, name, wrap)
    return Spy()


def pump(app, seconds=0.2):
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)


def test_no_endpoint_can_start_motion_and_jobs_need_approval(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    ctl = spy_controller()
    w = real_window(controller=ctl)
    try:
        ctl.connect()
        assert ctl.arm()                       # even armed and ready, nothing remote can start it
        plug = w.extension("ext_automation")
        run = w.extension("run")
        plug.settings["queue_intake"] = True
        assert plug.set_enabled(True, confirm=False, port=0)
        srv = plug.server
        plug.server.token = TOKEN              # the test's token (the plugin generated its own)
        srv.rate_per_s = 0.0                   # the battery is ~200 requests (rate limit: own test)
        plug.publish_status()
        pump(app, 1.0)                         # the GUI's own start-up questions after connect (homing)
        type(ctl).record = True
        type(ctl).calls.clear()
        # --- the battery: every method on every path, a WebSocket session, a long poll
        for path in PATHS:
            for method in METHODS:
                if (method, path) == ("POST", "/api/v1/queue"):
                    continue
                http(srv, method, path, b"{}" if method in ("POST", "PUT", "PATCH") else None)
        s, f, status, _h, _k = ws_connect(srv)
        assert b"101" in status
        ws_read(f)
        s.sendall(WS.encode(WS.OP_TEXT, b'{"type": "start"}', mask=True))
        http(srv, "GET", "/api/v1/events?after=0&wait=0.2")
        # --- a submission (uploaded drawing) and one by path outside the job folders
        with open(GEAR, "rb") as fh:
            up = {"filename": "gear.dxf", "content_base64": base64.b64encode(fh.read()).decode(), "quantity": 2,
                  "name": "gear", "note": "from the ERP", "reference": "SO-4711"}
        code, job = http(srv, "POST", "/api/v1/queue", json.dumps(up).encode())
        assert code == 202 and job["status"] == "pending_approval" and job["source"] == "api"
        code, body = http(srv, "POST", "/api/v1/queue", json.dumps({"path": GEAR}).encode())
        assert code == 403 and body["error"]["code"] == "forbidden_path"
        code, body = http(srv, "POST", "/api/v1/queue", json.dumps({"path": GEAR, "layers": {}}).encode())
        assert code == 400 and body["error"]["code"] == "unknown_field"
        run.tick()                              # the Run screen picks the inbox up
        pump(app)
        assert [j.id for j in run.intake.pending] == [job["id"]]
        assert len(run.queue) == 0 and run.queue.next_entry() is None
        assert run.runner.state == "idle" and run.runner.begin() is False
        plug.publish_status()
        code, q = http(srv, "GET", "/api/v1/queue")
        assert q["entries"] == [] and [p["id"] for p in q["pending_approval"]] == [job["id"]]
        assert "path" not in q["pending_approval"][0]
        assert http(srv, "GET", "/api/v1/status")[1]["remote_control"] is False
        # nothing above reached the machine
        assert type(ctl).calls == [], type(ctl).calls
        # --- the operator approves at the machine: a queue entry, still not started
        w.commands.run("workspace.run")
        panel = run.screen.queue_panel.approval
        panel.refresh()
        assert panel.isVisibleTo(run.screen.queue_panel) and panel.table.rowCount() == 1
        entry = panel.approve(job["id"])
        assert entry is not None and entry.status == "waiting" and entry.extra["intake"]["approved_by"]
        pump(app)
        assert run.intake.pending == [] and [e.id for e in run.queue] == [entry.id]
        assert run.runner.state == "idle"       # queue mode is the operator's choice
        assert type(ctl).calls == [], type(ctl).calls
        code, body = http(srv, "GET", "/api/v1/events?after=0&limit=500")
        kinds = [e["type"] for e in body["events"]]
        assert "intake_submitted" in kinds and "intake_approved" in kinds and "queue_changed" in kinds
        s.close()
    finally:
        type(ctl).record = False
        w.close()


def test_rejecting_from_the_run_screen(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        run = w.extension("run")
        job = run.intake.submit({"path": GEAR}, "cli", "anna")
        run.tick()
        panel = run.screen.queue_panel.approval
        assert panel.reject(job.id) and run.intake.pending == [] and len(run.queue) == 0
    finally:
        w.close()


def test_api_off_by_default_and_on_localhost(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        plug = w.extension("ext_automation")
        assert plug.server is None and plug.settings["enabled"] is False
        assert plug.settings["host"] == "127.0.0.1" and plug.settings["queue_intake"] is False
        assert plug.set_enabled(True, confirm=False, port=0)
        assert plug.server.address[0] == "127.0.0.1" and plug.server.submit is None
        assert len(plug.settings["token"]) >= 24
        w.commands.run("api.toggle", False) if hasattr(w.commands, "run") else plug.set_enabled(False)
        assert plug.server is None
    finally:
        w.close()
