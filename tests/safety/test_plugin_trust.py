"""Plugin trust model (review 2, code_health P1-1 / P1-8 / P2-2; docs/dev/PLUGINS.md §6): entry-point
plugins are listed but load only when the service engineer enabled them (a persisted allowlist of
name and target), after the safety UI exists, never in the core's command namespaces, and
``ctx.controller`` goes through the controller's ``plugin_policy`` hook when it has one."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from mlaser.services import plugins as P

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/wiring.py

PLUGIN = '''
SEEN = {}

def register(ctx):
    w = ctx.window
    SEEN["safety_ui"] = getattr(w, "safety_ui", None) is not None
    SEEN["controller"] = ctx.controller
    ctx.register_command("{name}.hello", lambda: None, label="Hello")
'''

EVIL = '''
def register(ctx):
    ctx.register_command("run.stop", lambda: None, label="not the real stop")
'''


class _EP:
    def __init__(self, name, value):
        self.name, self.value, self.dist = name, value, None


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fake_eps(tmp_path, monkeypatch):
    """Install plugin modules in tmp_path and announce them as ``mlaser.plugins`` entry points."""
    import importlib.metadata as md
    eps: list = []
    monkeypatch.syspath_prepend(str(tmp_path))

    def add(name, source):
        mod = f"r2plug_{name}"
        (tmp_path / f"{mod}.py").write_text(textwrap.dedent(source.replace("{name}", name)))
        sys.modules.pop(mod, None)
        eps.append(_EP(name, mod))
        return mod

    real = md.entry_points
    monkeypatch.setattr(md, "entry_points", lambda **kw: list(eps) if kw.get("group") == P.ENTRY_POINT_GROUP
                        else real(**kw))
    yield add
    for e in eps:
        sys.modules.pop(e.value, None)


def _window(monkeypatch, tmp_path, controller=None):
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    w = MainWindow(controller or DemoController(), extensions=EXTENSIONS, entry_points=True)
    w._test_no_dialogs = True
    return w


def _status(w):
    return {i.name: s for i, s in w.plugin_catalog}


def test_discovered_plugins_are_listed_not_loaded(app, fake_eps, monkeypatch, tmp_path):
    mod = fake_eps("hello", PLUGIN)
    w = _window(monkeypatch, tmp_path)
    try:
        assert _status(w) == {"hello": "disabled"}
        assert "hello" not in w.extensions and mod not in sys.modules       # never imported
        assert w.commands.get("hello.hello") is None
    finally:
        w.close()


def test_enabled_plugin_loads_after_the_safety_ui(app, fake_eps, monkeypatch, tmp_path):
    from mlaser.services.config import ConfigService
    mod = fake_eps("hello", PLUGIN)
    cfg = ConfigService()
    cfg.set(P.ALLOWLIST_KEY, {"hello": mod})
    cfg.save()
    w = _window(monkeypatch, tmp_path)
    try:
        assert _status(w) == {"hello": "loaded"}
        seen = sys.modules[mod].SEEN
        assert seen["safety_ui"] is True                        # the safety UI existed already
        assert seen["controller"] is w.ctl                      # no plugin_policy: the controller
        assert w.commands.get("hello.hello") is not None
        assert w.plugin_contexts["hello"].origin == "entry-point"
    finally:
        w.close()


def test_changed_target_needs_enabling_again(app, fake_eps, monkeypatch, tmp_path):
    from mlaser.services.config import ConfigService
    mod = fake_eps("hello", PLUGIN)
    cfg = ConfigService()
    cfg.set(P.ALLOWLIST_KEY, {"hello": "someone_else:plugin"})
    cfg.save()
    w = _window(monkeypatch, tmp_path)
    try:
        assert _status(w) == {"hello": "changed"} and mod not in sys.modules
    finally:
        w.close()


def test_plugin_cannot_claim_the_stop_command(app, fake_eps, monkeypatch, tmp_path):
    """P2-2: a plugin pre-claiming run.stop used to make MainWindow() fail (DuplicateCommandError);
    now it is refused, rolled back, and Stop stays the core's."""
    from mlaser.services.config import ConfigService
    mod = fake_eps("evil", EVIL)
    cfg = ConfigService()
    cfg.set(P.ALLOWLIST_KEY, {"evil": mod})
    cfg.save()
    w = _window(monkeypatch, tmp_path)
    try:
        assert _status(w) == {"evil": "failed"}
        spec = w.commands.get("run.stop")
        assert spec is not None and spec.owner == "shell"
        stopped = []
        monkeypatch.setattr(w.ctl, "stop", lambda *a, **k: stopped.append(1))
        w.commands.run("run.stop")
        assert stopped == [1]
    finally:
        w.close()


def test_reserved_namespaces_only_for_entry_points():
    class Host:
        def __init__(self):
            from mlaser.services.commands import CommandRegistry
            self.commands = CommandRegistry()
            self.hooks = P.Hooks()

    h = Host()
    ctx = P.PluginContext(h, "third", "entry-point")
    for cid in ("run.start", "shell.palette", "workspace.run"):
        with pytest.raises(P.ReservedCommandError):
            ctx.register_command(cid, lambda: None)
    ctx.register_command("third.ok", lambda: None)
    P.PluginContext(h, "builtin_one").register_command("run.custom", lambda: None)   # built in: allowed


def test_allowlist_rules():
    ep = P.PluginInfo("x", "pkg:plugin", "entry-point")
    assert not P.allowed(ep, {}) and not P.allowed(ep, None) and not P.allowed(ep, {"x": "other:plugin"})
    assert P.allowed(ep, {"x": "pkg:plugin"})
    assert P.plugin_status(ep, {"x": "other"}) == "changed" and P.plugin_status(ep, {}) == "disabled"
    assert P.allowed(P.PluginInfo("b", "mlaser.ui.ext_cam"), {})              # built-ins always load


def test_plugin_policy_hook_is_used_when_the_controller_has_it():
    class View:
        pass

    class Ctl:
        def __init__(self):
            self.calls = []

        def plugin_policy(self, plugin, origin):
            self.calls.append((plugin, origin))
            return View()

    class Host:
        ctl = Ctl()

    ctx = P.PluginContext(Host(), "third", "entry-point")
    assert isinstance(ctx.controller, View) and Host.ctl.calls == [("third", "entry-point")]
    assert P.controller_for(object, "x", "builtin") is object                   # no hook: the controller


def test_plugins_dialog_saves_the_allowlist(app, fake_eps, monkeypatch, tmp_path):
    from PySide6.QtCore import Qt

    from mlaser.services.config import ConfigService
    from mlaser.ui.ext_options import PluginsDialog
    mod = fake_eps("hello", PLUGIN)
    w = _window(monkeypatch, tmp_path)
    try:
        assert w.commands.get("app.plugins").min_mode == "service"
        d = PluginsDialog(w, w.plugin_catalog, {})
        assert d.table.rowCount() == 1 and d.checked() == {}
        d.table.item(0, 0).setCheckState(Qt.Checked)
        assert d.checked() == {"hello": mod} and d.new_ones() == ["hello"]
        w.extension("ext_options").save_allowlist(d.checked())
        assert ConfigService().get(P.ALLOWLIST_KEY) == {"hello": mod}            # persisted
        ro = PluginsDialog(w, w.plugin_catalog, {}, editable=False)
        assert not (ro.table.item(0, 0).flags() & Qt.ItemIsEnabled)
        d.deleteLater()
        ro.deleteLater()
    finally:
        w.close()
