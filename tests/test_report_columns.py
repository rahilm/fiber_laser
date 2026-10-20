"""The Report workspace's tables fit their width (docs/dev/RESPONSIVE.md rule 1): narrower than
their columns (Segoe UI on Windows made "Per day" 2-10 px too wide at 1280 × 800 and 919 × 814)
the least important columns hide instead of a horizontal scroll bar; the key column stays, and
every column comes back when there is room."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTableWidget  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n=8):
    for _ in range(n):
        app.processEvents()


def test_report_tables_hide_columns_instead_of_scrolling(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1280, 800)
        w.show()
        w.shell.host.activate("report")
        _pump(app)
        tabs = w.findChild(QTableWidget, "report_days").parentWidget().parentWidget()
        tabs.setMaximumWidth(420)             # less than any table's columns need
        _pump(app)
        bad = []
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            _pump(app)
            t = tabs.currentWidget()
            assert isinstance(t, QTableWidget) and t.isVisible()
            if t.horizontalScrollBar().isVisible():
                bad.append(f"{t.objectName()}: scrolls by {t.horizontalScrollBar().maximum()} px")
            hh = t.horizontalHeader()
            if all(hh.isSectionHidden(c) for c in range(t.columnCount())):
                bad.append(f"{t.objectName()}: every column hidden")
        assert not bad, bad
        # wide again: every column back
        tabs.setMaximumWidth(16777215)
        w.resize(2400, 1000)
        _pump(app)
        for i in range(tabs.count()):
            tabs.setCurrentIndex(i)
            _pump(app)
            t = tabs.currentWidget()
            hidden = [c for c in range(t.columnCount()) if t.horizontalHeader().isSectionHidden(c)]
            assert not hidden, (t.objectName(), hidden)
    finally:
        dispose(w)
