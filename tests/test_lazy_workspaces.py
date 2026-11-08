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


#: seconds :func:`_reference_work` takes on the development host (Linux x86-64, Python 3.12)
REFERENCE_WORK_S = 0.015


def _reference_work() -> float:
    """Seconds a fixed mix of what building a window does (creating, laying out and deleting
    widgets, plain Python) takes now, best of three: the speed of this runner under its load."""
    from PySide6.QtWidgets import QFormLayout, QLabel, QLineEdit, QWidget

    def once() -> float:
        t = time.perf_counter()
        w = QWidget()
        form = QFormLayout(w)
        for i in range(150):
            form.addRow(QLabel(f"row {i}"), QLineEdit(str(i)))
        w.adjustSize()
        w.deleteLater()
        acc = 0.0
        for i in range(60_000):
            acc += (i % 97) * 0.5 - (i % 31) * 0.25
        assert acc
        dt = time.perf_counter() - t
        QApplication.processEvents()          # the deferred delete, outside the timing
        return dt
    return min(once() for _ in range(3))


def runner_budget(seconds: float) -> float:
    """``seconds`` (a bound with headroom on the development host) scaled by how much slower
    this runner is right now, never less (a shared Windows runner: 3.65 s for a ~0.6 s window).
    Building the Process page eagerly again (+1.1 s here, about 2.5x the window) stays far over
    the scaled bound on the same runner."""
    return seconds * max(1.0, _reference_work() / REFERENCE_WORK_S)


@pytest.mark.flaky(reruns=1)            # a timing bound on a shared runner (one rerun, like the other timing tests)
def test_window_construction_stays_fast(app, quiet, config):
    """The decisive check is structural: the window is built without the Process page.  The
    time bound has headroom (0.6 s measured with the lazy page, 1.5-2.0 s before, on a loaded
    64-core host) and is scaled by the runner's speed; the first window of the process pays the
    imports, the second is the one timed."""
    from wiring import dispose, real_window
    dispose(real_window(config=config()))
    cfg = config()
    t0 = time.perf_counter()
    w = real_window(config=cfg)
    dt = time.perf_counter() - t0
    try:
        ext = w.extension("ext_process")
        assert not ext.workspace_built and _process_workspaces(w) == []
        budget = runner_budget(1.5)
        assert dt < budget, f"MainWindow() took {dt:.2f} s (budget {budget:.2f} s on this runner)"
        # what the lazy page saves is real work: building it now costs a sizeable share of the
        # window (a page that became trivially cheap would make this test pointless)
        t1 = time.perf_counter()
        ext.page.ensure()
        assert ext.workspace_built and time.perf_counter() - t1 > 0.1 * dt
    finally:
        dispose(w)
