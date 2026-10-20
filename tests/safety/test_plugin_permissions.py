"""Plugin API 2 permissions, enforced where the application can (docs/dev/PLUGIN_STANDARD.md §5,
docs/SAFETY.md §3a): ctx.http hosts and the https rule, redirects, the size cap and retries;
ctx.controller needs ``machine`` and the authority grant; ctx.secrets is namespaced and its file
is 0600; the document, orders and queue need their permissions; a manifest outside the api /
mlaser ranges is refused before import; a legacy plugin gets no API-2 services."""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/plugin_fixtures.py

from plugin_fixtures import RepoServer  # noqa: E402

from mlaser.services import plugins as P  # noqa: E402
from mlaser.services.config import ConfigService  # noqa: E402
from mlaser.services.plugin_http import HostNotAllowedError, HttpClient, InsecureURLError  # noqa: E402


def manifest(pid="t", **perms):
    base = {"network": [], "files": False, "document": False, "orders": False, "queue": False,
            "machine": False, "secrets": False}
    base.update(perms)
    return {"plugin": {"id": pid, "name": pid, "version": "1.0", "entry": f"m_{pid}:plugin", "api": ">=2,<3",
                       "mlaser": ">=0.1.0rc1"}, "permissions": base}


class Host:
    def __init__(self, tmp):
        from mlaser.services.commands import CommandRegistry
        from mlaser.services.document import DocumentService
        self.commands = CommandRegistry()
        self.hooks = P.Hooks()
        self.docs = DocumentService()
        self.config = ConfigService(Path(tmp) / "c.json")
        self.ctl = object()
        self.tr_ = None
        self.logs: list[str] = []
        self.posted: list = []

    def log(self, msg):
        self.logs.append(msg)

    def post_to_gui(self, fn):
        self.posted.append(fn)


@pytest.fixture
def repo():
    r = RepoServer()
    yield r
    r.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    return Host(tmp_path)


# ----------------------------------------------------------------------------- http
def test_http_host_enforcement(host, repo):
    repo.routes["/ok"] = b'{"a": 1}'
    ctx = P.PluginContext(host, "t", "entry-point", manifest("t", network=["127.0.0.1"]))
    r = ctx.http.get(repo.url("/ok"), params={"q": "x"})
    assert r.status == 200 and r.json() == {"a": 1} and r.headers["content-length"] == "8"
    assert repo.hits[-1] == "/ok?q=x"
    port = repo.httpd.server_address[1]
    with pytest.raises(HostNotAllowedError):
        ctx.http.get(f"http://localhost:{port}/ok")                 # not in permissions.network
    none = P.PluginContext(host, "n", "entry-point", manifest("n"))
    with pytest.raises(PermissionError):
        none.http.get(repo.url("/ok"))
    with pytest.raises(InsecureURLError):                            # https only off this computer
        P.PluginContext(host, "w", "entry-point", manifest("w", network=["example.com"])).http.get(
            "http://example.com/")
    with pytest.raises(InsecureURLError):
        ctx.http.get("ftp://127.0.0.1/x")
    with pytest.raises(InsecureURLError):
        ctx.http.get(f"http://user:pw@127.0.0.1:{port}/ok")
    # a built-in may use any host (still the https rule)
    builtin = P.PluginContext(host, "ext_x", "builtin")
    assert builtin.http.get(f"http://localhost:{port}/ok").status == 200


def test_http_redirects_size_cap_and_retries(repo):
    port = repo.httpd.server_address[1]
    seen_auth = []
    repo.routes["/final"] = lambda h: (seen_auth.append(h.headers.get("Authorization")) or 200, {}, b"done")
    repo.routes["/same"] = (302, {"Location": "/final"}, b"")
    repo.routes["/other"] = (302, {"Location": f"http://localhost:{port}/final"}, b"")
    allow_both = HttpClient(lambda h: h in ("127.0.0.1", "localhost"), sleep=lambda s: None)
    only_ip = HttpClient(lambda h: h == "127.0.0.1", sleep=lambda s: None)
    assert only_ip.get(repo.url("/same"), auth=("u", "s")).text == "done"
    assert seen_auth[-1] and seen_auth[-1].startswith("Basic ")
    with pytest.raises(HostNotAllowedError):
        only_ip.get(repo.url("/other"))
    allow_both.get(repo.url("/other"), auth=("u", "s"))
    assert seen_auth[-1] is None                                      # credentials not sent to another host
    repo.routes["/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(ConnectionError):
        only_ip.get(repo.url("/loop"))
    # size cap: Content-Length and while reading
    repo.routes["/big"] = b"x" * 5000
    with pytest.raises(ValueError):
        only_ip.get(repo.url("/big"), max_bytes=1000)
    # retries honour Retry-After and stop at the budget
    calls = []
    sleeps = []

    def flaky(h):
        calls.append(1)
        return (503, {"Retry-After": "2"}, b"busy") if len(calls) < 3 else (200, {}, b"ok")
    repo.routes["/flaky"] = flaky
    c = HttpClient(lambda h: True, sleep=sleeps.append)
    assert c.get(repo.url("/flaky")).text == "ok" and sleeps == [2.0, 2.0]
    repo.routes["/slow"] = (429, {"Retry-After": "60"}, b"later")
    sleeps.clear()
    r = c.get(repo.url("/slow"))
    assert r.status == 429 and sleeps == []                           # 60 s > the 30 s budget: no wait
    repo.routes["/down"] = (502, {}, b"")
    sleeps.clear()
    assert c.get(repo.url("/down")).status == 502 and sleeps == [1.0, 2.0, 4.0]
    # POST json
    got = {}
    repo.routes["/post"] = lambda h: (got.setdefault("body", h.body) and 201, {}, b"{}")
    assert c.post(repo.url("/post"), json={"a": 1}).status == 201 and json.loads(got["body"]) == {"a": 1}


def test_http_never_on_a_machine_thread(repo):
    repo.routes["/ok"] = b"ok"
    c = HttpClient(lambda h: True)
    err = []

    def run():
        try:
            c.get(repo.url("/ok"))
        except RuntimeError as exc:
            err.append(exc)
    t = threading.Thread(target=run, name="mlaser-stream-watchdog")
    t.start()
    t.join(5)
    assert err and "machine thread" in str(err[0])


# ----------------------------------------------------------------------------- controller
def test_controller_needs_machine_and_the_authority_grant(host, monkeypatch):
    import types
    fake = types.ModuleType("mlaser.controller.plugin_view")
    fake.plugin_view = lambda ctl, name: ("view", name)
    monkeypatch.setitem(sys.modules, "mlaser.controller.plugin_view", fake)
    no_machine = P.PluginContext(host, "a", "entry-point", manifest("a"))
    with pytest.raises(PermissionError):
        no_machine.controller
    declared = P.PluginContext(host, "b", "entry-point", manifest("b", machine=True))
    with pytest.raises(PermissionError) as e:
        declared.controller                                           # declared but not granted
    assert "machine authority" in str(e.value)
    host.config.set(P.AUTHORITY_KEY, ["b"])
    assert declared.controller == ("view", "b")                       # still the plugin view
    assert P.PluginContext(host, "ext_card", "builtin").controller is host.ctl
    assert P.PluginContext(host, "legacy", "entry-point").controller == ("view", "legacy")   # API 1 rule


# ----------------------------------------------------------------------------- secrets
def test_secrets_namespaced_and_private(host, caplog):
    a = P.PluginContext(host, "a", "entry-point", manifest("a", secrets=True))
    b = P.PluginContext(host, "b", "entry-point", manifest("b", secrets=True))
    with caplog.at_level(logging.DEBUG):
        a.secrets.set("token", "s3cret-value")
    assert a.secrets.get("token") == "s3cret-value" and b.secrets.get("token") is None
    path = a.data_dir / "secrets.json"
    assert path.is_file() and json.loads(path.read_text()) == {"plugin.a.token": "s3cret-value"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "s3cret-value" not in caplog.text
    a.secrets.delete("token")
    assert a.secrets.get("token") is None
    with pytest.raises(ValueError):
        a.secrets.get("../escape")
    with pytest.raises(PermissionError):
        P.PluginContext(host, "c", "entry-point", manifest("c")).secrets          # not declared
    # a keyring back end gets the namespaced name
    stored = {}

    class KR:
        def get_password(self, svc, name):
            return stored.get((svc, name))

        def set_password(self, svc, name, value):
            stored[(svc, name)] = value

        def delete_password(self, svc, name):
            stored.pop((svc, name), None)
    s = P.PluginSecrets("a", a.data_dir, keyring_module=KR())
    s.set("k", "v")
    assert stored == {("mlaser", "plugin.a.k"): "v"} and s.backend == "keyring"


# ----------------------------------------------------------------------------- other APIs
def test_document_orders_queue_need_their_permissions(host):
    ctx = P.PluginContext(host, "a", "entry-point", manifest("a"))
    with pytest.raises(PermissionError):
        ctx.edit(lambda d: None, "x")
    with pytest.raises(PermissionError):
        ctx.services.orders
    with pytest.raises(PermissionError):
        ctx.services.queue
    host.hooks.provide("orders", lambda: "BOOK", "orders")
    ok = P.PluginContext(host, "b", "entry-point", manifest("b", document=True, orders=True))
    ok.edit(lambda d: None, "x", parts=())
    assert ok.services.orders == "BOOK" and ok.services.config is host.config and ok.services.technology is None
    # min_mode default from the manifest
    m = manifest("c")
    m["plugin"]["min_mode"] = "service"
    spec = P.PluginContext(host, "c", "entry-point", m).register_command("c.x", lambda: None)
    assert spec.min_mode == "service"


def test_legacy_plugin_gets_no_api2_services(host):
    ctx = P.PluginContext(host, "old", "entry-point")
    assert ctx.manifest is None and ctx.permissions.legacy
    for attr in ("http", "secrets", "data_dir"):
        with pytest.raises(PermissionError):
            getattr(ctx, attr)
    ctx.edit(lambda d: None, "x", parts=())                         # API 1 behaviour stays


def test_incompatible_or_invalid_manifest_refused_before_import(host, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "m_x.py").write_text("raise SystemExit('must not be imported')\n")
    m = manifest("x")
    m["plugin"]["api"] = ">=3"
    ctx, err = P.register_plugin(host, P.PluginInfo("x", "m_x:plugin", "entry-point", manifest=m))
    assert ctx is None and "incompatible" in str(err) and "m_x" not in sys.modules
    ctx, err = P.register_plugin(host, P.PluginInfo("x", "m_x:plugin", "entry-point", manifest_error="bad toml"))
    assert ctx is None and "invalid manifest" in str(err) and "m_x" not in sys.modules


def test_run_in_background_delivers_on_the_gui_thread(host):
    ctx = P.PluginContext(host, "a", "entry-point", manifest("a"))
    got = []
    h = ctx.run_in_background(lambda: 41 + 1, on_done=got.append)
    assert h.wait(5)
    assert got == [] and len(host.posted) == 1                       # posted, not called on the worker
    host.posted.pop()()
    assert got == [42]
    errs = []
    h2 = ctx.run_in_background(lambda: 1 / 0, on_error=errs.append)
    h2.wait(5)
    host.posted.pop()()
    assert isinstance(errs[0], ZeroDivisionError)
    h3 = ctx.run_in_background(lambda: None)
    h3.cancel()
    assert h3.cancelled
    ctx.show_message("hello", "warning")
    assert host.logs[-1] == "a: WARNING: hello"
