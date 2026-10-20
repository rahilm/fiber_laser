"""The Run screen's STOP, HOLD, START and Arm are never hidden by docks (review 2, features.md
§6; docs/SAFETY.md §10): at every supported window size, after visiting every workspace, with
every dock the application has shown (the classic Nest dock that *Import file...* in the Nest
workspace used to open, which then squeezed the Run screen until STOP, START and Arm were cut
off at 1440 × 900), the buttons lie entirely inside the visible part of the page, and the run
bar's buttons inside the window."""
from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton, QScrollArea, QWidget

SIZES = [(1280, 720), (1280, 800), (1366, 768), (1440, 900), (1600, 900), (1920, 1080)]
KEYS = ("stop", "hold", "start", "arm")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    w.show()
    yield w
    w.close()


def pump(app, s=0.15):
    t = time.monotonic() + s
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def _scroll_view(widget):
    area = widget.parentWidget()
    while area is not None and not isinstance(area, QScrollArea):
        area = area.parentWidget()
    return area.viewport() if area is not None else widget.window()


def _visible_in(b, view) -> bool:
    if not b.isVisibleTo(view.window()):
        return False
    r = b.rect().translated(b.mapTo(view, b.rect().topLeft()))
    return view.rect().contains(r)


def _open_every_dock(win, tmp_path):
    """Everything that leaves docks behind: the classic Nest dock through the Nest workspace's
    import (the reported case), plus every other dock of the window."""
    from mlaser.io.dxf_import import import_dxf  # noqa: F401 - the importer exists
    from mlaser.model import Contour, Document, Line, Part
    from mlaser.ui import docops  # noqa: F401
    sq = Contour([Line((0, 0), (40, 0)), Line((40, 0), (40, 40)), Line((40, 40), (0, 40)), Line((0, 40), (0, 0))])
    doc = Document(parts=[Part([sq])])
    path = tmp_path / "part.chf"
    from mlaser.formats.chf import save_chf
    save_chf(doc, str(path))
    win.commands.run("workspace.nest")
    nest = win.extension("ext_nest")[0]
    nest.import_part(str(path))
    return nest


def _check(win, app, size, label):
    win.resize(*size)
    pump(app, 0.25)
    ws = win.extension("run")
    scr = ws.screen
    scr.refresh()
    pump(app, 0.05)
    view = _scroll_view(scr)
    for key in KEYS:
        b = scr.buttons[key]
        assert _visible_in(b, view), (label, size, key, b.geometry(), view.size())
    bar = win.findChild(QWidget, "run_bar")
    for key in KEYS:
        b = bar.findChild(QPushButton, f"run_{key}")
        assert b is not None and _visible_in(b, win), (label, size, "run bar", key)


@pytest.mark.parametrize("size", SIZES)
def test_run_controls_visible_at_every_size(win, app, tmp_path, size):
    win.resize(*size)
    _open_every_dock(win, tmp_path)
    pump(app, 0.1)
    # every workspace, then Run: the run bar's targets are always in the window
    for ws_id in win.shell.host.visible_ids():
        win.commands.run(f"workspace.{ws_id}")
        pump(app, 0.05)
        bar = win.findChild(QWidget, "run_bar")
        for key in KEYS:
            b = bar.findChild(QPushButton, f"run_{key}")
            assert _visible_in(b, win), (ws_id, size, key)
    win.commands.run("workspace.run")
    _check(win, app, size, "after every workspace")


def test_nest_import_does_not_leak_the_classic_dock(win, app, tmp_path):
    nest = _open_every_dock(win, tmp_path)
    assert nest.dock.isHidden()                      # the Nest workspace shows the project itself
    win.commands.run("workspace.design")
    nest.show()                                      # the classic command opens it in Design
    pump(app, 0.05)
    assert nest.dock.isVisible()
    win.commands.run("workspace.run")
    pump(app, 0.05)
    assert nest.dock.isHidden()                      # never on the Run screen
    _check(win, app, (1440, 900), "nest dock")
    win.commands.run("workspace.design")
    pump(app, 0.05)
    assert nest.dock.isVisible()                     # back where the operator left it


def test_dock_shown_while_on_run_is_hidden_again(win, app):
    win.resize(1440, 900)
    win.commands.run("workspace.run")
    pump(app, 0.1)
    for d in win.shell.host.docks():
        d.show()                                     # a plugin or the View menu shows panels
    pump(app, 0.1)
    docked = [d.objectName() for d in win.shell.host.docks() if d.isVisible() and not d.isFloating()]
    assert docked == [], docked
    _check(win, app, (1440, 900), "all docks shown on Run")
    # a dock a plugin adds while Run is active
    from PySide6.QtWidgets import QLabel
    d = win.host_add_dock(QLabel("plugin panel"), "Plugin panel", "right", "plugin.test.panel")
    d.show()
    pump(app, 0.1)
    assert d.isHidden()
    _check(win, app, (1440, 900), "plugin dock")
