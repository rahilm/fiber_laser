"""The window state and the recent files (UI evaluation F13): splitter positions and current tabs
survive a restart (also those of a workspace built later); a DXF / SVG opened from File > Open
is a recent file, in mlaser and in the OS's recent documents."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.ui

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QSplitter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DXF = os.path.join(ROOT, "tests", "data", "dxf", "mix.dxf")


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


def test_splitters_and_tabs_round_trip(app, quiet, config):
    from wiring import dispose
    w = _window(app, config)
    ws = w.extension("ext_process").workspace
    w.show_workspace("ext_process.process")
    app.processEvents()
    split = ws.findChild(QSplitter)
    assert split is not None and split.count() == 2
    total = sum(split.sizes())
    split.setSizes([total // 5, total - total // 5])
    want_sizes = split.sizes()
    ws.tabs.setCurrentIndex(ws.tabs.count() - 1)
    want_tab = ws.tabs.currentIndex()
    assert want_tab > 0
    dispose(w)
    from mlaser.ui.shell import view_state
    saved = config().get(view_state.KEY, {})
    assert saved and any("process_workspace" in k for k in saved), saved

    w2 = _window(app, config)
    try:
        ext = w2.extension("ext_process")
        assert not ext.workspace_built                    # waits for the page ...
        ws2 = ext.workspace                               # ... and is applied when it is built
        assert ws2.tabs.currentIndex() == want_tab
        assert ws2.findChild(QSplitter).sizes() == want_sizes
    finally:
        dispose(w2)


def test_view_state_keys_and_mismatch(app):
    from PySide6.QtWidgets import QTabWidget, QWidget

    from mlaser.ui.shell import view_state
    root = QWidget()
    root.setObjectName("root")
    named = QWidget(root)
    named.setObjectName("page")
    a, b = QSplitter(named), QSplitter(named)
    for s in (a, b):
        s.addWidget(QWidget())
        s.addWidget(QWidget())
    tabs = QTabWidget(named)
    tabs.setObjectName("tabs")
    for i in range(3):
        tabs.addTab(QWidget(), str(i))
    assert view_state.key_of(a, root) == "page/QSplitter[0]"
    assert view_state.key_of(b, root) == "page/QSplitter[1]"
    assert view_state.key_of(tabs, root) == "page/tabs"
    tabs.setCurrentIndex(2)
    st = view_state.collect(root, {"gone/QSplitter[0]": {"tab": 1}})
    assert st["page/tabs"] == {"tab": 2} and "gone/QSplitter[0]" in st      # kept for later
    tabs.setCurrentIndex(0)
    st["page/tabs"] = {"tab": 7}                           # no such tab: ignored
    st["page/QSplitter[0]"] = {"split": "!!"}              # garbage: ignored
    view_state.restore(root, st)
    assert tabs.currentIndex() == 0
    root.deleteLater()


def test_dxf_from_file_open_is_a_recent_file(app, quiet, config, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from mlaser.ui import native
    from wiring import dispose
    told = []
    monkeypatch.setattr(native, "os_add_recent_document", lambda p: told.append(p) or True)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (DXF, "")))
    w = _window(app, config)
    try:
        assert w.shell.native.recent.paths == []
        w.file_open()
        app.processEvents()
        assert w.shell.native.recent.paths[0] == os.path.abspath(DXF)
        assert told == [os.path.abspath(DXF)]
        w.load_path(DXF)                                   # again: still one entry, the OS told once
        assert w.shell.native.recent.paths.count(os.path.abspath(DXF)) == 1 and len(told) == 1
    finally:
        dispose(w)
