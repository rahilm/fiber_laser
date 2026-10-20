"""A workspace's tool bar never hides actions behind its extension button (docs/dev/RESPONSIVE.md):
labels go first (icons only), and the page's minimum width is the icon-only tool bar's, so the
window's minimum follows.  Under windows11 (Segoe UI) the Design tool bar overflowed in every
window narrower than about 1180 px (native-check, 2026-10-20); a larger font shows the same
here."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QToolBar, QToolButton  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n=10):
    for _ in range(n):
        app.processEvents()


def _overflowing(win) -> list[str]:
    out = []
    for tb in win.findChildren(QToolBar, "shell_ws_toolbar"):
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
        if tb.isVisible() and ext is not None and ext.isVisible():
            out.append(tb.parentWidget().objectName())
    return out


def test_workspace_toolbars_do_not_overflow_at_the_window_minimum(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    f0 = app.font()
    try:
        f = app.font()
        f.setPointSizeF((f0.pointSizeF() if f0.pointSizeF() > 0 else 9) * 1.6)
        app.setFont(f)
        w.resize(1280, 800)
        w.show()
        bad = []
        for ws in w.shell.host.visible_ids():
            w.shell.host.activate(ws)
            _pump(app)
            for _ in range(4):            # the minimum may grow once the tool bar is measured
                w.resize(w.minimumSizeHint().expandedTo(w.minimumSize()))
                _pump(app)
            bad += [f"{ws} at {w.width()}x{w.height()}: {p}" for p in _overflowing(w)]
        assert not bad, bad
    finally:
        app.setFont(f0)
        dispose(w)
