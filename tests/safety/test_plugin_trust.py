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
        # the Safety-core plugin view once it is merged; the controller until then
        try:
            import mlaser.controller.plugin_view  # noqa: F401
        except ImportError:
            assert seen["controller"] is w.ctl
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


def test_third_party_plugins_get_the_plugin_view(monkeypatch):
    """Safety-core contract: ``ctx.controller`` of a third-party plugin is
    ``mlaser.controller.plugin_view.plugin_view(ctl, name)``; built-ins keep the controller."""
    import types

    class Host:
        ctl = object()

    calls = []
    fake = types.ModuleType("mlaser.controller.plugin_view")
    fake.plugin_view = lambda ctl, name: calls.append((ctl, name)) or ("view", name)
    monkeypatch.setitem(sys.modules, "mlaser.controller.plugin_view", fake)
    assert P.PluginContext(Host(), "third", "entry-point").controller == ("view", "third")
    assert calls == [(Host.ctl, "third")]
    assert P.PluginContext(Host(), "ext_card").controller is Host.ctl          # built in
    monkeypatch.setitem(sys.modules, "mlaser.controller.plugin_view", None)    # not merged yet
    assert P.PluginContext(Host(), "third", "entry-point").controller is Host.ctl


def test_plugin_policy_needs_machine_authority(tmp_path):
    from mlaser.services.config import ConfigService
    cfg = ConfigService(tmp_path / "c.json")
    pol = P.plugin_policy(cfg)
    assert pol("operator", "arm", "") is None                                 # not a plugin: not judged
    why = pol("plugin:hello", "arm", "test")
    assert why and "machine authority" in why
    cfg.set(P.AUTHORITY_KEY, ["hello"])
    assert pol("plugin:hello", "arm", "") is not None                         # granted but not enabled
    cfg.set(P.ALLOWLIST_KEY, {"hello": "mod"})
    assert pol("plugin:hello", "arm", "") is None and pol("plugin:other", "arm", "") is not None

    class Ctl:
        policy = None

        def set_plugin_policy(self, fn):
            self.policy = fn

    c = Ctl()
    assert P.install_policy(c, cfg) and c.policy("plugin:hello", "reset_fault", "") is None
    assert P.install_policy(object(), cfg) is False                           # feature check


def test_replace_document_refused_while_locked():
    class Docs:
        replaced = []

        def replace(self, *a):
            self.replaced.append(a)

    class Host:
        docs = Docs()
        locked = True

        def document_locked(self, what):
            return self.locked

    h = Host()
    ctx = P.PluginContext(h, "third", "entry-point")
    with pytest.raises(P.DocumentLockedError):
        ctx.replace_document(object())
    assert Docs.replaced == []
    h.locked = False
    ctx.replace_document(object())
    assert len(Docs.replaced) == 1


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
        d.table.item(0, 1).setCheckState(Qt.Checked)                             # machine authority
        assert d.authority() == ["hello"]
        w.extension("ext_options").save_allowlist(d.checked(), d.authority())
        assert ConfigService().get(P.ALLOWLIST_KEY) == {"hello": mod}            # persisted
        assert ConfigService().get(P.AUTHORITY_KEY) == ["hello"]
        ro = PluginsDialog(w, w.plugin_catalog, {}, editable=False)
        assert not (ro.table.item(0, 0).flags() & Qt.ItemIsEnabled)
        d.deleteLater()
        ro.deleteLater()
    finally:
        w.close()
