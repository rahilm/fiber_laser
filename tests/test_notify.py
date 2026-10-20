"""Notifications (mlaser.notify): back ends against local fakes (HTTP for ntfy and the webhook, a
small SMTP server), rate limits, quiet hours, non-blocking emit, the secret store, and the event
wiring (controller events, the queue runner, maintenance)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import stat
import threading
import time
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mlaser.machine import events as EV
from mlaser.notify import EventWiring, Notifier, SecretStore, make, signature
from mlaser.notify.backends import SIGNATURE_HEADER


# ============================================================================== fakes
class _NoLookupHTTPServer(ThreadingHTTPServer):
    """No ``socket.getfqdn`` in ``server_bind`` (a 35 s reverse lookup on macOS CI)."""

    def server_bind(self) -> None:
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class FakeHTTP:
    """Records every POST; answers ``code``."""

    def __init__(self, code: int = 200) -> None:
        self.code = code
        self.requests: list[dict] = []
        self.got = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(outer.code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                outer.got.set()

            def log_message(self, *a):
                pass

        self.httpd = _NoLookupHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSMTP:
    """A minimal SMTP server: EHLO / MAIL / RCPT / DATA / QUIT."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.mails: list[dict] = []
        self.got = threading.Event()
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(30)
        f = conn.makefile("rb")

        def say(s: str) -> None:
            conn.sendall(s.encode() + b"\r\n")

        say("220 fake ESMTP")
        mail: dict = {"rcpt": []}
        while True:
            line = f.readline()
            if not line:
                break
            cmd = line.decode().strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                conn.sendall(b"250-fake\r\n250 OK\r\n")
            elif up.startswith("MAIL FROM"):
                mail["from"] = cmd[10:].strip()
                say("250 OK")
            elif up.startswith("RCPT TO"):
                mail["rcpt"].append(cmd[8:].strip())
                say("250 OK")
            elif up == "DATA":
                say("354 go")
                data = b""
                while True:
                    ln = f.readline()
                    if ln in (b".\r\n", b""):
                        break
                    data += ln
                mail["data"] = data
                self.mails.append(mail)
                mail = {"rcpt": []}
                say("250 queued")
                self.got.set()
            elif up == "QUIT":
                say("221 bye")
                break
            else:
                say("250 OK")
        conn.close()

    def close(self) -> None:
        self.sock.close()


class DictSecrets:
    def __init__(self, **kw: str) -> None:
        self.d = dict(kw)

    def get(self, name: str) -> str:
        return self.d.get(name, "")


@pytest.fixture()
def http():
    s = FakeHTTP()
    yield s
    s.close()


def _notifier(settings: dict, secrets=None, **kw) -> Notifier:
    base = {"enabled": True, "min_interval_s": 0.0, "max_per_hour": 0.0}
    base.update(settings)
    return Notifier(base, secrets or DictSecrets(), **kw)


# ============================================================================== back ends
def test_ntfy_headers_body_and_token(http):
    n = _notifier({"ntfy_enabled": True, "ntfy_server": http.url, "ntfy_topic": "shop floor"},
                  DictSecrets(**{"notify.ntfy_token": "tk_123"}))
    try:
        assert n.emit(make("alarm", "Alarm 12 ≥ limit", "Follower alarm: check the nozzle", key="a12"))
        assert n.flush(30)
    finally:
        n.close()
    (req,) = http.requests
    h = {k.lower(): v for k, v in req["headers"].items()}
    assert req["path"] == "/shop%20floor"
    assert h["priority"] == "5" and h["authorization"] == "Bearer tk_123"
    assert h["tags"] == "rotating_light"
    assert h["title"].startswith("=?UTF-8?B?")               # non-ASCII title: RFC 2047
    assert base64.b64decode(h["title"][10:-2]).decode() == "Alarm 12 ≥ limit"
    assert req["body"].decode() == "Follower alarm: check the nozzle"
    assert n.results[-1].ok and n.counts["sent"] == 1


def test_webhook_json_and_signature(http):
    n = _notifier({"webhook_enabled": True, "webhook_url": http.url + "/hook", "machine_name": "Laser 1"},
                  DictSecrets(**{"notify.webhook_secret": "s3cret"}))
    try:
        n.emit(make("sheet_done", "Sheet done", "flange: sheet 1/3 cut", key="e1:1", entry="e1"))
        assert n.flush(30)
    finally:
        n.close()
    (req,) = http.requests
    h = {k.lower(): v for k, v in req["headers"].items()}
    body = json.loads(req["body"])
    assert body["event"] == "sheet_done" and body["machine"] == "Laser 1" and body["data"]["entry"] == "e1"
    want = "sha256=" + hmac.new(b"s3cret", req["body"], hashlib.sha256).hexdigest()
    assert h[SIGNATURE_HEADER.lower()] == want == signature(req["body"], "s3cret")
    assert h["x-mlaser-event"] == "sheet_done"


def test_email_through_a_local_smtp_server(monkeypatch):
    # macOS CI: smtplib's EHLO name and make_msgid's domain came from socket.getfqdn(), a reverse
    # DNS lookup outside every timeout (tens of seconds there); the sender must not call it
    fqdn_calls: list = []
    real_getfqdn = socket.getfqdn
    monkeypatch.setattr(socket, "getfqdn", lambda *a: fqdn_calls.append(a) or real_getfqdn(*a))
    srv = FakeSMTP()
    n = _notifier({"email_enabled": True, "smtp_host": "127.0.0.1", "smtp_port": srv.port, "smtp_security": 2,
                   "email_from": "laser@shop.example", "email_to": "a@shop.example, b@shop.example"})
    try:
        n.emit(make("maintenance_due", "Maintenance due", "Lens cleaning (due)"))
        assert n.flush(60)
        assert srv.got.wait(10)
    finally:
        n.close()
        srv.close()
    (mail,) = srv.mails
    assert mail["rcpt"] == ["<a@shop.example>", "<b@shop.example>"]
    msg = message_from_bytes(mail["data"])
    assert msg["Subject"] == "Maintenance due" and msg["X-Mlaser-Event"] == "maintenance_due"
    assert "Lens cleaning" in msg.get_payload()
    assert n.results[-1].ok, n.results[-1]
    assert not fqdn_calls, "socket.getfqdn() (a reverse DNS lookup) on the send path"
    assert msg["Message-ID"].endswith("@shop.example>")


def test_failure_is_recorded_not_raised():
    srv = FakeHTTP(code=500)
    n = _notifier({"webhook_enabled": True, "webhook_url": srv.url, "ntfy_enabled": True,
                   "ntfy_server": "ftp://nope", "ntfy_topic": "x"})
    try:
        assert n.emit(make("job_finished", "Job finished", "x"))
        assert n.flush(30)
    finally:
        n.close()
        srv.close()
    res = {r.channel: r for r in n.results}
    assert not res["webhook"].ok and "500" in res["webhook"].detail
    assert not res["ntfy"].ok and "URL" in res["ntfy"].detail
    assert n.counts["failed"] == 2


def test_channel_choice_per_event():
    sent: list = []
    senders = {c: (lambda n, cfg, s, t, c=c: sent.append((n.kind, c)) or "ok") for c in ("ntfy", "email", "webhook")}
    n = _notifier({"ntfy_enabled": True, "email_enabled": True, "webhook_enabled": False,
                   "channels_alarm": "ntfy, webhook", "channels_sheet_done": "email"}, senders=senders)
    try:
        n.emit(make("alarm", "a", "b"))
        n.emit(make("sheet_done", "a", "b"))
        n.emit(make("job_finished", "a", "b"))       # all enabled channels
        assert n.flush(30)
    finally:
        n.close()
    assert sorted(sent) == sorted([("alarm", "ntfy"), ("sheet_done", "email"), ("job_finished", "ntfy"),
                                   ("job_finished", "email")])


# ============================================================================== filters and limits
def _capture_senders(sent):
    return {"webhook": lambda n, cfg, s, t: sent.append(n) or "ok"}


def test_disabled_event_switch_and_global_switch():
    sent: list = []
    st = {"enabled": False, "webhook_enabled": True, "event_alarm": False, "min_interval_s": 0.0,
          "max_per_hour": 0.0}
    n = Notifier(st, DictSecrets(), senders=_capture_senders(sent))
    try:
        assert not n.emit(make("job_finished", "a", "b"))
        st["enabled"] = True
        assert not n.emit(make("alarm", "a", "b"))       # event switched off
        assert n.emit(make("job_finished", "a", "b"))
        assert n.emit(make("test", "t", "x"), force=True)
        assert n.flush(30)
    finally:
        n.close()
    assert [x.kind for x in sent] == ["job_finished", "test"] and n.counts["suppressed"] == 2


def test_rate_limit_per_key_and_token_bucket():
    now = [1000.0]
    sent: list = []
    st = {"enabled": True, "webhook_enabled": True, "min_interval_s": 60.0, "max_per_hour": 360.0}
    n = Notifier(st, DictSecrets(), senders=_capture_senders(sent), clock=lambda: now[0])
    try:
        assert n.emit(make("alarm", "a", "b", key="a1"))
        assert not n.emit(make("alarm", "a", "b", key="a1"))    # the same alarm within a minute
        assert n.emit(make("alarm", "a", "b", key="a2"))        # another one
        now[0] += 61
        assert n.emit(make("alarm", "a", "b", key="a1"))
        # token bucket: burst 10, refilled at 360/h = one every 10 s (the 61 s refilled it); the
        # alarm just sent took one
        ok = sum(n.emit(make("sheet_done", "s", "x", key=f"s{i}")) for i in range(20))
        assert ok == 9
        assert not n.emit(make("sheet_done", "s", "x", key="late"))
        now[0] += 10.5
        assert n.emit(make("sheet_done", "s", "x", key="later"))
        assert n.flush(30)
    finally:
        n.close()
    assert n.counts["limited"] == 1 + 11 + 1


def test_quiet_hours_keep_alarms():
    t0 = time.mktime((2026, 10, 20, 23, 30, 0, 0, 0, -1))
    sent: list = []
    st = {"enabled": True, "webhook_enabled": True, "min_interval_s": 0.0, "max_per_hour": 0.0,
          "quiet_start": "22:00", "quiet_end": "06:00", "quiet_alarms": True}
    n = Notifier(st, DictSecrets(), senders=_capture_senders(sent), clock=lambda: t0)
    try:
        assert not n.emit(make("sheet_done", "s", "x"))
        assert n.emit(make("alarm", "a", "x"))
        st["quiet_alarms"] = False
        assert not n.emit(make("alarm", "a", "y", key="other"))
        st["quiet_start"] = ""                          # off
        assert n.emit(make("sheet_done", "s", "x"))
        assert n.flush(30)
    finally:
        n.close()


def test_emit_never_blocks_on_a_slow_channel():
    release = threading.Event()
    started = threading.Event()

    def slow(n, cfg, s, t):
        started.set()
        release.wait(60)
        return "ok"

    n = _notifier({"webhook_enabled": True}, senders={"webhook": slow}, queue_size=4)
    try:
        n.emit(make("test", "t", "x"), force=True)
        assert started.wait(30)                         # the worker is stuck in the send
        t0 = time.perf_counter()
        results = [n.emit(make("test", "t", f"{i}"), force=True) for i in range(50)]
        dt = time.perf_counter() - t0
        assert dt < 2.0, dt                             # the caller never waits for the network
        assert results.count(True) == 4 and n.counts["dropped"] == 46
    finally:
        release.set()
        assert n.flush(60)
        n.close()
    assert n.counts["sent"] == 5


def test_emit_swallows_broken_settings():
    class Broken:
        def __getitem__(self, key):
            raise RuntimeError("config gone")

    n = Notifier(Broken(), DictSecrets(), start=False)
    assert n.emit(make("alarm", "a", "b")) is False


# ============================================================================== secrets
def test_secret_file_permissions_and_no_keyring(tmp_path):
    p = tmp_path / "cfg" / "secrets.json"
    s = SecretStore(p, use_keyring=False)
    assert s.backend == "file" and s.get("notify.smtp_password") == ""
    s.set("notify.smtp_password", "pa55")
    assert s.get("notify.smtp_password") == "pa55" and s.has("notify.smtp_password")
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        os.chmod(p, 0o644)                                # widened by someone: fixed on read
        assert SecretStore(p, use_keyring=False).get("notify.smtp_password") == "pa55"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    s.set("notify.smtp_password", "")
    assert json.loads(p.read_text()) == {}


class FakeKeyring:
    def __init__(self, fail: bool = False) -> None:
        self.store: dict = {}
        self.fail = fail

    def get_password(self, svc, name):
        if self.fail:
            raise RuntimeError("no keyring backend")
        return self.store.get((svc, name))

    def set_password(self, svc, name, value):
        if self.fail:
            raise RuntimeError("no keyring backend")
        self.store[(svc, name)] = value

    def delete_password(self, svc, name):
        self.store.pop((svc, name), None)


def test_secrets_go_to_the_keyring_when_available(tmp_path):
    kr = FakeKeyring()
    p = tmp_path / "secrets.json"
    s = SecretStore(p, keyring_module=kr)
    assert s.backend == "keyring"
    s.set("notify.ntfy_token", "tok")
    assert kr.store[("mlaser", "notify.ntfy_token")] == "tok" and s.get("notify.ntfy_token") == "tok"
    assert not p.exists()                                 # nothing in the file
    s.set("notify.ntfy_token", "")
    assert ("mlaser", "notify.ntfy_token") not in kr.store


def test_failing_keyring_falls_back_to_the_file(tmp_path):
    p = tmp_path / "secrets.json"
    s = SecretStore(p, keyring_module=FakeKeyring(fail=True))
    s.set("notify.webhook_secret", "w")
    assert s.backend == "file" and s.get("notify.webhook_secret") == "w"
    assert json.loads(p.read_text()) == {"notify.webhook_secret": "w"}


def test_settings_schema_holds_no_secret():
    from mlaser.ui.ext_notify import settings_schema
    keys = {s.key for s in settings_schema()}
    assert not any(w in k for k in keys for w in ("password", "token", "secret"))


# ============================================================================== wiring
class Capture:
    def __init__(self) -> None:
        self.got: list = []

    def emit(self, n, force=False):
        self.got.append(n)
        return True


def _wiring(source="real", include=False):
    cap = Capture()
    src = [source]
    w = EventWiring(cap, source=lambda mode: src[0], include_simulator=lambda: include)
    bus = EV.EventBus()
    w.attach_bus(bus)
    return w, cap, bus, src


def test_job_finished_from_the_event_bus():
    w, cap, bus, _ = _wiring()
    bus.publish(EV.JobStarted("process", "/jobs/flange.chf"))
    bus.publish(EV.JobFinished("process", False, "paused"))          # job continues
    bus.publish(EV.JobStarted("process", "resume", resume=True))
    bus.publish(EV.JobFinished("process", True, "done", duration_s=120))
    bus.publish(EV.JobStarted("frame", "", tracked=False))
    bus.publish(EV.JobFinished("frame", True, "done", tracked=False))
    (n,) = cap.got
    assert n.kind == "job_finished" and "flange.chf" in n.message and "2.0 min" in n.message
    bus.publish(EV.JobStarted("process", "b.chf"))
    bus.publish(EV.JobFinished("process", False, "stopped"))
    assert cap.got[-1].title == "Job stopped" and "stopped" in cap.got[-1].message


def test_simulator_jobs_are_skipped_by_default():
    w, cap, bus, src = _wiring("simulator")
    bus.publish(EV.JobStarted("process", "x"))
    bus.publish(EV.JobFinished("process", True, "done"))
    bus.publish(EV.AlarmRaised(alarms=("A1",), active=("A1",)))
    assert cap.got == []
    w2, cap2, bus2, _ = _wiring("practice", include=True)
    bus2.publish(EV.JobStarted("process", "x"))
    bus2.publish(EV.JobFinished("process", True, "done"))
    assert [n.kind for n in cap2.got] == ["job_finished"]


def test_alarm_once_per_code_and_fault_once():
    class A:
        def __init__(self, code, text):
            self.code, self.text_en = code, text

    w, cap, bus, _ = _wiring()
    a = A(17, "X servo alarm")
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))           # still the same alarm
    bus.publish(EV.AlarmCleared(alarms=(a,), active=()))
    bus.publish(EV.AlarmRaised(alarms=(a,), active=(a,)))           # raised again: new
    bus.publish(EV.StateChanged("running", "fault", fault="link lost"))
    bus.publish(EV.StateChanged("fault", "fault", fault="link lost"))
    bus.publish(EV.StateChanged("fault", "disarmed"))
    bus.publish(EV.StateChanged("running", "fault", fault="link lost"))
    kinds = [(n.kind, n.key) for n in cap.got]
    assert kinds == [("alarm", "alarm:17"), ("alarm", "alarm:17"), ("alarm", "fault:link lost"),
                     ("alarm", "fault:link lost")]
    assert "X servo alarm" in cap.got[0].message


def test_queue_sheet_done_and_waiting_for_the_next_sheet(tmp_path):
    from mlaser.services.queue import JobQueue, QueueRunner
    q = JobQueue(tmp_path / "queue.json")
    q.add(str(tmp_path / "a.chf"), 2, name="flange", material="S235", thickness_mm=3.0)
    w, cap, bus, _ = _wiring()
    r = QueueRunner(q, load=lambda e: True, start=lambda: True)
    r.attach(bus)
    w.attach_runner(r)
    assert r.begin() and r.confirm_sheet()
    bus.publish(EV.JobStarted("process", "a.chf"))
    assert r.state == "running"
    bus.publish(EV.JobFinished("process", True, "done", duration_s=30))
    assert r.state == "confirm"
    kinds = [n.kind for n in cap.got]
    assert kinds == ["job_finished", "sheet_done", "queue_waiting"] or \
        sorted(kinds) == sorted(["job_finished", "sheet_done", "queue_waiting"])
    wait = next(n for n in cap.got if n.kind == "queue_waiting")
    assert "flange" in wait.message and "2/2" in wait.message and "S235 3 mm" in wait.message
    # the operator leaving and re-entering queue mode is no "waiting" notification
    r.cancel()
    r.begin()
    assert [n.kind for n in cap.got].count("queue_waiting") == 1
    w.detach()
    assert w._sheet_done not in r.sheet_listeners


def test_maintenance_and_test_notifications():
    w, cap, bus, _ = _wiring()
    w.machine = lambda: "Laser 2"
    w.maintenance("lens", "due", "Maintain: lens (due)")
    assert cap.got[-1].kind == "maintenance_due" and cap.got[-1].title == "Laser 2: Maintenance due"
    assert w.test() and cap.got[-1].kind == "test"


def test_production_service_reports_due_maintenance(tmp_path):
    from mlaser.services.production import ProductionService

    class _S(dict):
        pass

    svc = ProductionService(_S(), str(tmp_path))
    got = []
    svc.maintenance_listeners.append(lambda k, s, m: got.append((k, s, m)))
    from mlaser.diag import maintain as M

    item = M.MaintainItem(key="custom lens", group="custom", custom=True)
    svc.maintain_plan.items = [item]
    svc.maintain_plan.check = lambda now, s: [(item, "due")]
    svc.check_maintenance(time.time())
    assert got and got[0][1] == "due"
