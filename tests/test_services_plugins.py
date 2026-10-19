"""CommandRegistry, hooks and the plugin API (mlaser.services, Qt-free), plus the real wiring of
the application: MainWindow with EXTENSIONS loaded through PluginContext."""

from __future__ import annotations

import os
import sys
import types

import pytest

from mlaser.services.commands import CommandRegistry, DuplicateCommandError, UnknownCommandError
from mlaser.services.config import ConfigService, Setting
from mlaser.services.document import DocumentService
from mlaser.services.plugins import (
    DuplicateProviderError,
    Hooks,
    PluginContext,
    PluginInfo,
    discover,
    register_plugin,
)


def test_registry_refuses_duplicates_and_reports_failures():
    reg = CommandRegistry()
    calls = []
    reg.register("a.b", lambda: calls.append(1), "core")
    with pytest.raises(DuplicateCommandError):
        reg.register("a.b", lambda: None, "plugin")
    assert reg.duplicates == [("a.b", "core", "plugin")]
    reg.run("a.b")
    assert calls == [1]
    errs = []
    reg.on_error.append(lambda c, e: errs.append(c))
    reg.register("bad", lambda: 1 / 0, "x")
    assert reg.run("bad") is None and "ZeroDivisionError" in reg.failures["bad"] and errs == ["bad"]
    with pytest.raises(UnknownCommandError):
        reg.run("nope")
    reg.register("t", lambda on: calls.append(on), "x", checkable=True)
    reg.run("t", True)
    assert calls[-1] is True
    assert set(reg.handlers()) == {"a.b", "bad", "t"}
    assert reg.unregister_owner("x") == ["bad", "t"]


def test_hooks_and_providers():
    h = Hooks()
    seen = []
    off = h.add("mode_changed", seen.append, "p")
    h.add("mode_changed", lambda m: 1 / 0, "q")
    h.emit("mode_changed", "pan")
    assert seen == ["pan"] and h.errors and h.errors[0][1] == "q"
    off()
    h.emit("mode_changed", "select")
    assert seen == ["pan"]
    with pytest.raises(KeyError):
        h.add("no_such_hook", print)
    h.provide("nudge", print, "p")
    with pytest.raises(DuplicateProviderError):
        h.provide("nudge", print, "q")
    h.remove_owner("p")
    assert h.provider("nudge") is None


class FakeHost:
    """The Host protocol without Qt."""

    def __init__(self):
        self.commands = CommandRegistry()
        self.hooks = Hooks()
        self.docs = DocumentService()
        self.config = ConfigService(load=False)
        self.jobs = None
        self.ctl = None
        self.tr_ = None
        self.logs = []
        self.docks = []

    def log(self, msg):
        self.logs.append(msg)

    def host_add_dock(self, widget, title, area, key):
        self.docks.append((title, area, key))
        return widget

    def host_add_ribbon_tab(self, title, commands):
        return (title, commands)

    def host_add_workspace(self, name, widget, title):
        return widget


def _plugin_module(name, register):
    mod = types.ModuleType(name)
    mod.register = register
    sys.modules[name] = mod
    return name


def test_plugin_context_edit_settings_and_rollback():
    host = FakeHost()

    def reg(ctx):
        from mlaser.model import Part

        ctx.register_command("hello.add", lambda: ctx.edit(lambda d: d.parts.append(Part(name="h")), "add"))
        s = ctx.settings_schema([Setting("count", "int", 3, min=1, max=10)])
        assert s["count"] == 3
        s["count"] = 5
        with pytest.raises(ValueError):
            s["count"] = 50
        ctx.on_controller_changed(lambda c: None)
        ctx.add_dock(object(), "Hello")
        return "ok"

    ctx, res = register_plugin(host, PluginInfo("hello", _plugin_module("_mlaser_test_hello", reg)))
    assert res == "ok" and isinstance(ctx, PluginContext)
    host.commands.run("hello.add")
    assert [p.name for p in host.docs.document.parts] == ["h"]
    host.docs.undo()
    assert host.docs.document.parts == []
    assert host.config.get("plugin.hello.count") == 5
    assert host.hooks.count("controller_changed") == 1 and host.docks[0][0] == "Hello"

    # a plugin that collides with an existing command is rolled back and reported
    def bad(ctx):
        ctx.register_command("bad.one", print)
        ctx.on_mode_changed(print)
        ctx.register_command("hello.add", print)

    ctx2, err = register_plugin(host, PluginInfo("bad", _plugin_module("_mlaser_test_bad", bad)))
    assert ctx2 is None and isinstance(err, DuplicateCommandError)
    assert "bad.one" not in host.commands and host.hooks.count("mode_changed") == 0
    assert any("duplicate command" in m for m in host.logs)
    assert host.commands.duplicates[-1] == ("hello.add", "hello", "bad")

    # an import error is logged, not swallowed
    ctx3, err3 = register_plugin(host, PluginInfo("missing", "_mlaser_no_such_module"))
    assert ctx3 is None and any("import failed" in m for m in host.logs)


def test_entry_point_discovery(monkeypatch):
    class EP:
        def __init__(self, name, value):
            self.name, self.value = name, value

    import importlib.metadata as md

    monkeypatch.setattr(
        md,
        "entry_points",
        lambda group: [EP("zeta", "pkg:z"), EP("ext_cam", "x:y")] if group == "mlaser.plugins" else [],
    )
    infos = discover(["mlaser.ui.ext_cam"])
    assert [(i.name, i.origin) for i in infos] == [("ext_cam", "builtin"), ("zeta", "entry-point")]


def test_example_plugin_loads():
    """examples/mlaser_hello is the plugin of docs/dev/PLUGINS.md."""
    root = os.path.join(os.path.dirname(__file__), "..", "examples", "mlaser_hello")
    sys.path.insert(0, os.path.abspath(root))
    try:
        host = FakeHost()
        ctx, ext = register_plugin(host, PluginInfo("hello", "mlaser_hello:plugin"))
        assert ctx is not None, host.logs
        assert "hello.grid" in host.commands
        host.commands.run("hello.grid")
        assert len(host.docs.document.parts) == ext.settings["rows"] * ext.settings["cols"]
        host.docs.undo()
        assert host.docs.document.parts == []
    finally:
        sys.path.remove(os.path.abspath(root))


# ----------------------------------------------------------------------------- real wiring (Qt)
@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.ui
def test_real_wiring_no_monkeypatching_no_duplicates(app):
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    from wiring import real_window

    w = real_window()
    try:
        assert sorted(w.extensions) == sorted(EXTENSIONS)
        assert w.commands.duplicates == []
        # nothing replaced the window's methods (set_controller, set_mode, nudge)
        for name in ("set_controller", "set_mode", "nudge"):
            assert getattr(type(w), name) is getattr(MainWindow, name)
            assert name not in vars(w)
        # each command has exactly one owner; the formerly overridden core commands
        own = {c: w.commands.owner(c) for c in ("machine.home", "sort.nearest", "sort.in_out", "tool.array")}
        assert own == {
            "machine.home": "core",
            "sort.nearest": "core",
            "sort.in_out": "core",
            "tool.array": "ext_cam",
        }
        # controller hooks: every plugin re-attaches through the hook
        assert w.hooks.count("controller_changed") >= 3
        # the nudge provider of ext_draw is used by the window's arrow keys
        assert w.hooks.provider("nudge") is not None
    finally:
        w.close()
