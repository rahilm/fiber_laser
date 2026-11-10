"""A plugin cannot grant itself machine authority, network hosts or another plugin's settings
through the handles its context gives it, and cannot touch the heartbeat without the grant
(review 2026-11-09, arch.md A13; docs/dev/PLUGINS.md §6, docs/SAFETY.md §3a).

The first test is the reviewer's probe (``arch/dead/perm/selfgrant.py``) as a test: a manifest
plugin that declares ``machine = true``, is enabled but has no authority, writes the grant keys
through ``ctx.config`` and ``ctx.services.config``.  Before the fix both writes went through and
the plugin then held the controller view and the policy allowed it to arm."""
from __future__ import annotations

import pytest

from mlaser.machine.demo import DemoController
from mlaser.services import plugins as P
from mlaser.services.commands import CommandRegistry
from mlaser.services.config import ConfigService, Setting


class Host:
    def __init__(self, tmp_path):
        self.commands = CommandRegistry()
        self.hooks = P.Hooks()
        self.config = ConfigService(tmp_path / "config.json")
        self.docs = None
        self.jobs = None
        self.ctl = DemoController()
        self.tr_ = None
        self.logs: list[str] = []

    def log(self, m):
        self.logs.append(m)


def manifest(pid="x", machine=True, network=("cad.example.com",)):
    return {"plugin": {"id": pid, "name": pid.upper()},
            "permissions": {"network": list(network), "machine": machine, "secrets": False}}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    h = Host(tmp_path)
    P.install_policy(h.ctl, h.config)
    h.config.set(P.ALLOWLIST_KEY, {"x": "x:plugin", "y": "y:plugin"})   # enabled, no authority
    return h


def _no_grant(host, ctx):
    assert not ctx.allows_host("evil.example")
    with pytest.raises(PermissionError, match="machine authority"):
        ctx.controller
    assert host.ctl.plugin_policy_check("plugin:x", "arm", "t")            # refused
    assert host.config.get(P.AUTHORITY_KEY) == []
    assert host.config.get(P.NETWORK_KEY) == {}


def test_reviewer_probe_self_grant_is_refused(host):
    ctx = P.PluginContext(host, "x", "entry-point", manifest())
    _no_grant(host, ctx)
    with pytest.raises(PermissionError):
        ctx.config.set(P.NETWORK_KEY, {"x": ["evil.example"]})
    with pytest.raises(PermissionError):
        ctx.services.config.set(P.AUTHORITY_KEY, ["x"])
    _no_grant(host, ctx)


@pytest.mark.parametrize("key", [P.ALLOWLIST_KEY, P.AUTHORITY_KEY, P.ACCEPTED_KEY, P.NETWORK_KEY,
                                 "safety.max_buffer_ms", "safety.heartbeat_output", "app.monitor_enabled",
                                 "plugin.y.server", "plugin.x", "plugin.xy.k", "plugins.x.k"])
def test_every_write_route_outside_the_own_namespace_is_refused(host, key):
    ctx = P.PluginContext(host, "x", "entry-point", manifest())
    before = dict(host.config.values)
    for cfg in (ctx.config, ctx.services.config):
        with pytest.raises(P.ProtectedSettingError):
            cfg.set(key, ["x"])
        with pytest.raises(P.ProtectedSettingError):
            cfg.reset(key)
        with pytest.raises(P.ProtectedSettingError):
            cfg.update({"plugin.x.ok": 1, key: ["x"]})                      # nothing of it written
        with pytest.raises(P.ProtectedSettingError):
            cfg.register([Setting(key, "json", [])])
    assert host.config.values == before


def test_reads_are_copies_and_internals_are_hidden(host):
    host.config.set(P.AUTHORITY_KEY, ["z"])
    ctx = P.PluginContext(host, "x", "entry-point", manifest())
    ctx.config.get(P.AUTHORITY_KEY).append("x")                             # mutating a read value
    ctx.config.get(P.ALLOWLIST_KEY)["y"] = "other"
    ctx.config.setting(P.NETWORK_KEY).default["x"] = ["evil.example"]       # the schema default
    assert host.config.get(P.AUTHORITY_KEY) == ["z"]
    assert host.config.get(P.ALLOWLIST_KEY)["y"] == "y:plugin"
    assert host.config.setting(P.NETWORK_KEY).default == {}
    assert not ctx.allows_host("evil.example")
    for name in ("values", "schema", "mapping", "namespace", "load", "save", "flush", "import_dat",
                 "set_profile", "machine", "_listeners", "path"):
        with pytest.raises(AttributeError):
            getattr(ctx.config, name)
    with pytest.raises(AttributeError):
        ctx.config._cfg = None                                              # read-only view
    assert ctx.config.is_set(P.AUTHORITY_KEY) and P.NETWORK_KEY in [s.key for s in ctx.config.section("plugins")]


def test_own_settings_still_work(host):
    ctx = P.PluginContext(host, "x", "entry-point", manifest())
    s = ctx.settings_schema([Setting("server", "str", "https://cad.example.com")])
    s["server"] = "https://other.example.com"
    assert host.config.get("plugin.x.server") == "https://other.example.com"
    ctx.config.set("plugin.x.server", "a")
    assert ctx.services.config.get("plugin.x.server") == "a"
    ctx.config.reset("plugin.x.server")
    assert s["server"] == "https://cad.example.com"
    # a built-in plugin keeps the store itself (the Plugin manager and Options write through it)
    assert P.PluginContext(host, "ext_options", "builtin").config is host.config


def test_legacy_plugin_needs_the_grant_for_the_controller(host):
    """Default deny: a plugin without a manifest gets no controller view without authority
    (the view's start, laser and output members would otherwise work while the operator armed)."""
    legacy = P.PluginContext(host, "y", "entry-point")
    with pytest.raises(PermissionError, match="machine authority"):
        legacy.controller
    with pytest.raises(PermissionError):
        legacy.config.set(P.AUTHORITY_KEY, ["y"])
    host.config.set(P.AUTHORITY_KEY, ["y"])                                 # the operator grants it
    assert repr(legacy.controller) == "<controller view for plugin:y>"


def test_controller_changed_hook_gets_the_view_never_the_controller(host):
    got = []
    ctx = P.PluginContext(host, "x", "entry-point", manifest())
    ctx.on_controller_changed(got.append)
    ctx.on("controller_changed", got.append)
    host.hooks.emit("controller_changed", host.ctl)
    assert got == [None, None]                                               # no authority
    host.config.set(P.AUTHORITY_KEY, ["x"])
    host.hooks.emit("controller_changed", host.ctl)
    assert [repr(v) for v in got[2:]] == ["<controller view for plugin:x>"] * 2
    builtin = []
    P.PluginContext(host, "ext_card").on_controller_changed(builtin.append)
    host.hooks.emit("controller_changed", host.ctl)
    assert builtin == [host.ctl]


def test_plugin_heartbeat_hold_needs_the_policy(host):
    """docs/dev/PLUGINS.md §6: the policy is asked before a plugin holds (or releases) the
    heartbeat.  Before the fix the view forwarded heartbeat_hold unchecked."""
    calls = []
    host.ctl.heartbeat_hold = lambda hold: calls.append(hold)
    host.config.set(P.AUTHORITY_KEY, ["x"])                                 # the view, but ...
    host.config.set(P.ALLOWLIST_KEY, {})                                    # ... not enabled
    view = P.PluginContext(host, "x", "entry-point", manifest()).controller
    view.heartbeat_hold(True)
    view.heartbeat_hold(False)                                              # never ends a test hold
    assert calls == []
    assert host.ctl.plugin_policy_check("plugin:x", "heartbeat_hold", "")
    host.config.set(P.ALLOWLIST_KEY, {"x": "x:plugin"})                     # enabled and granted
    view.heartbeat_hold(True)
    view.heartbeat_hold(False)
    assert calls == [True, False]
    # the operator's own path is not judged
    assert host.ctl.plugin_policy_check("operator", "heartbeat_hold", "") is None


def test_no_plugin_policy_refuses_the_heartbeat_hold(host):
    from mlaser.controller.plugin_view import plugin_view
    calls = []
    ctl = DemoController()
    ctl.heartbeat_hold = lambda hold: calls.append(hold)
    plugin_view(ctl, "x").heartbeat_hold(False)
    assert calls == []
