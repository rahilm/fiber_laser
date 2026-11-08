"""Cold start (UI evaluation F11): the Process workspace is built on first use, every command
still works from the menu before that, and the window's construction stays fast."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A fresh ConfigService on a private config directory (one per window: a window's plugins
    register their settings in it; a second one reads what the first saved)."""
    from mlaser.services.config import ConfigService
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "config"))
    return ConfigService


def _window(app, config):
    from wiring import real_window
    w = real_window(user_mode="engineer", config=config())
    w.resize(1280, 800)
    w.show()
    for _ in range(3):
        app.processEvents()
    return w


def _menu_action(w, cmd: str):
    from PySide6.QtWidgets import QMenu

    def walk(menu: QMenu):
        for a in menu.actions():
            if a.menu() is not None:
                yield from walk(a.menu())
            elif (a.data() or a.objectName()) == cmd:
                yield a
    for top in w.shell.menubar.actions():
        if top.menu() is not None:
            for a in walk(top.menu()):
                return a
    return None


def _process_workspaces(w) -> list:
    from mlaser.ui.workspaces.process import ProcessWorkspace
    return w.findChildren(ProcessWorkspace)


def test_process_workspace_is_built_on_first_use(app, quiet, config):
    from wiring import dispose
    w = _window(app, config)
    try:
        ext = w.extension("ext_process")
        assert w.shell.host.current == "design"
        assert not ext.workspace_built and _process_workspaces(w) == [], "built at start"
        # its commands are in the menus all the same, and the menu builds it on demand
        act = _menu_action(w, "process.library")
        assert act is not None, "process.library is not in a menu"
        act.trigger()
        app.processEvents()
        assert ext.workspace_built and len(_process_workspaces(w)) == 1
        assert w.shell.host.current.endswith("process") and ext.workspace.isVisible()
        assert ext.workspace.pages.currentIndex() == 1             # the Library page
    finally:
        dispose(w)


def test_a_workspace_command_builds_it_without_showing_it(app, quiet, config):
    from wiring import dispose
    w = _window(app, config)
    try:
        ext = w.extension("ext_process")
        w.set_document(__import__("mlaser.ui.docops", fromlist=["x"]).demo_document())
        assert not ext.workspace_built
        act = _menu_action(w, "cam.lead_check")          # a Cutting strategy tab command
        assert act is not None
        act.trigger()
        app.processEvents()
        assert ext.workspace_built and ext.strategy is not None
        # the layers and the card of what happened before it existed
        assert ext.workspace.layers.rowCount() == len({p.craft.layer for p in w.document.parts})
    finally:
        dispose(w)


def test_window_construction_stays_fast(app, quiet, config):
    """A bound with headroom (it measured 0.6 s with the lazy Process page and 1.5-2.0 s before,
    on a loaded 64-core CI host); the first window of the process pays the imports, the second
    is the one timed."""
    from wiring import dispose, real_window
    dispose(real_window(config=config()))
    cfg = config()
    t0 = time.perf_counter()
    w = real_window(config=cfg)
    dt = time.perf_counter() - t0
    try:
        assert not w.extension("ext_process").workspace_built
        assert dt < 1.5, f"MainWindow() took {dt:.2f} s"
    finally:
        dispose(w)
