"""One home per setting (SETTINGS_MAP.md, WORK_ITEMS X-1): walk the whole application (every
workspace and dock, and every dialog a command opens) and check the value bindings of
:mod:`mlaser.ui.homes`:

* a value id has editors on **at most one surface** (a surface: a dock, a workspace page, or a
  dialog class);
* every read-only mirror names a value that has a home;
* every value of an *implemented* home has an editor somewhere;
* the surfaces declared as fully bound have no unbound input.

Tracks add their bindings (``bind_editor`` / ``bind_mirror`` / ``bind_transient``) and mark their
homes implemented; the test then holds them to it.
"""
from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QDialog, QDockWidget, QWidget

from mlaser import ops

#: under xdist the module's tests stay together on one worker: the walk (every workspace, dock
#: and dialog: 10-19 s on CI) is built once, not once per test on four workers
pytestmark = pytest.mark.xdist_group("one_home")

#: surfaces whose every input must be bound (editor / mirror / transient)
BOUND_SURFACES = {"dock:plugin.ext_process.properties"}


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def surface_of(w: QWidget) -> str:
    p = w
    while p is not None:
        if isinstance(p, QDockWidget):
            return f"dock:{p.objectName()}"
        if type(p).__name__ == "WorkspacePage":
            return f"workspace:{p.objectName()}"
        if isinstance(p, QDialog):
            return f"dialog:{type(p).__name__}"
        if p.parentWidget() is None:
            return f"window:{type(p).__name__}"
        p = p.parentWidget()
    return "?"


def _collect(root: QWidget, out: dict) -> None:
    from mlaser.ui import homes

    for w in [root] + root.findChildren(QWidget):
        vid = w.property(homes.PROP_VALUE)
        if vid is None:
            continue
        out.setdefault(w.property(homes.PROP_ROLE), {}).setdefault(vid, set()).add(surface_of(w))


def _unbound(root: QWidget, surface: str) -> list[str]:
    from mlaser.ui import homes

    bad = []
    for w in root.findChildren(QWidget):
        if surface_of(w) != surface or not homes.is_input(w) or w.property(homes.PROP_VALUE) is not None:
            continue
        if isinstance(w.parentWidget(), QWidget) and w.parentWidget().property(homes.PROP_VALUE) is not None:
            continue                       # a sub-widget of a bound one
        bad.append(f"{type(w).__name__} {w.objectName() or w.accessibleName()}")
    return bad


@pytest.fixture(scope="module")
def walk(app, tmp_path_factory):
    os.environ["MLASER_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMenu, QMessageBox

    from wiring import real_window

    found: dict = {}
    unbound: dict = {}
    dialogs: list = []

    def capture(self, *a):
        dialogs.append(self)
        _collect(self, found)
        s = f"dialog:{type(self).__name__}"
        if s in BOUND_SURFACES:
            unbound.setdefault(s, []).extend(_unbound(self, s))
        return 0

    saved = {}
    patches = {
        (QDialog, "exec"): capture, (QDialog, "open"): lambda self, *a: capture(self),
        (QMessageBox, "exec"): lambda self, *a: 0, (QMenu, "exec"): lambda self, *a: None,
        (QFileDialog, "getOpenFileName"): staticmethod(lambda *a, **k: ("", "")),
        (QFileDialog, "getOpenFileNames"): staticmethod(lambda *a, **k: ([], "")),
        (QFileDialog, "getSaveFileName"): staticmethod(lambda *a, **k: ("", "")),
        (QFileDialog, "getExistingDirectory"): staticmethod(lambda *a, **k: ""),
    }
    for n in ("information", "warning", "critical", "question"):
        patches[(QMessageBox, n)] = staticmethod(lambda *a, **k: QMessageBox.No)
    for n in ("getInt", "getDouble", "getText", "getItem"):
        patches[(QInputDialog, n)] = staticmethod(lambda *a, **k: (None, False))
    for (cls, name), fn in patches.items():
        saved[(cls, name)] = cls.__dict__.get(name)
        setattr(cls, name, fn)
    try:
        from mlaser.controller import nettest
        nettest_saved = nettest.network_test
        nettest.network_test = lambda *a, **k: []
    except ImportError:
        nettest = None
    w = real_window(exec_dialogs=True)
    try:
        w.set_document(ops.demo_document())
        w.resize(1280, 800)
        w.show()
        w.canvas.select_parts([0])
        for ws in ["design", "process", "nest", "run", "report", "machine"] + list(w.workspaces):
            w.show_workspace(ws)
            app.processEvents()
        _collect(w, found)
        for s in BOUND_SURFACES:
            if s.startswith("dock:"):
                unbound.setdefault(s, []).extend(_unbound(w, s))
        for cmd, act in sorted(w.ribbon.actions.items()):
            if cmd.startswith(("app.exit", "shell.mode_", "hw.connect", "hw.reconnect", "file.")):
                continue
            try:
                act.trigger()
            except Exception:  # noqa: BLE001 - coverage is another test's job
                pass
            app.processEvents()
        yield found, unbound
    finally:
        w._test_no_dialogs = True
        w.close()
        for (cls, name), fn in saved.items():
            if fn is None:
                try:
                    delattr(cls, name)
                except AttributeError:
                    pass
            else:
                setattr(cls, name, fn)
        if nettest is not None:
            nettest.network_test = nettest_saved
        for d in dialogs:
            d.deleteLater()


def test_each_value_has_at_most_one_editing_surface(walk):
    found, _ = walk
    dup = {vid: sorted(s) for vid, s in found.get("editor", {}).items() if len(s) > 1}
    assert not dup, f"values edited in more than one place: {dup}"


def test_every_mirror_and_editor_has_a_home(walk):
    from mlaser.ui.homes import home_of

    found, _ = walk
    for role in ("editor", "mirror"):
        orphans = sorted(vid for vid in found.get(role, {}) if home_of(vid) is None)
        assert not orphans, f"{role}s without a home: {orphans}"


def test_implemented_homes_have_their_editors(walk):
    from mlaser.services import inheritance as I
    from mlaser.services import technology as T
    from mlaser.ui.techform import SKIP

    found, _ = walk
    editors = found.get("editor", {})
    gp = [k for k in T.layer_registry() if k not in SKIP]
    required = [f"recipe.{k}" for k in gp] + [f"layer.{k}" for k in gp]
    required += [f"craft.{v[6:]}" for v in I.CRAFT_IDS if not v.endswith("at_corner")]
    required += ["recipe.craft.lead_in.length", "recipe.craft.kerf_width"]
    missing = [vid for vid in required if vid not in editors]
    assert not missing, f"implemented homes without an editor: {missing[:20]} ({len(missing)})"


def test_bound_surfaces_have_no_unbound_inputs(walk):
    _, unbound = walk
    bad = {s: v for s, v in unbound.items() if v}
    assert not bad, f"inputs without a value binding: {bad}"


def test_edit_in_focuses_the_home(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    from mlaser.ui.homes import edit_in
    from wiring import real_window

    w = real_window()
    try:
        w.set_document(ops.demo_document())
        w.resize(1280, 800)
        w.show()
        w.show_workspace("design")
        w.canvas.select_parts([0])
        app.processEvents()
        ed = edit_in(w, "craft.lead_in.length")
        assert ed is not None and ed.property("mlaser_value") == "craft.lead_in.length"
        assert ed.isVisible() or not ed.isHidden()
        ed = edit_in(w, "recipe.GP.SlowStartSpeed")       # a secondary field: revealed
        assert ed is not None and not ed.isHidden()
    finally:
        w._test_no_dialogs = True
        w.close()
