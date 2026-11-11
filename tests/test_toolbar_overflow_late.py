"""A workspace tool bar whose extension button appears after the fit (a style that lays the
buttons out wider than their size hints, the layout pass after the queued check: Windows CI,
every width of the Design sweep) steps down to icons only / two rows until no action is hidden
behind the button (mlaser/ui/shell/workspace_host.py, docs/dev/RESPONSIVE.md)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QToolButton  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n=10):
    for _ in range(n):
        app.processEvents()


def test_an_extension_button_that_appears_late_is_stepped_down(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1280, 800)
        w.show()
        w.shell.host.activate("design")
        _pump(app)
        w.resize(w.minimumSizeHint().expandedTo(w.minimumSize()))
        _pump(app)
        page = w.shell.host.pages["design"]
        tb = page.toolbar
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
        assert ext is not None and not ext.isVisible()
        # the buttons laid out wider than the fit measured them, with no resize to follow
        for act in tb.actions():
            b = tb.widgetForAction(act)
            if isinstance(b, QToolButton) and not act.icon().isNull():
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.layout().invalidate()
        tb.layout().activate()
        _pump(app)
        assert not ext.isVisible(), (tb.width(), tb.sizeHint().width(), page.width())
    finally:
        dispose(w)
