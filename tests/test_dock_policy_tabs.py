"""The dock policy sizes a tab group through the tab on top (mlaser/ui/shell/dock_policy.py):
a tab behind keeps a stale geometry, and when that stale width already equalled the target the
shown tab was never resized (live resizing on macOS: the Properties dock stayed 63 px narrower
than its group's target)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _pump(app, n=10):
    for _ in range(n):
        app.processEvents()


def test_tab_group_is_sized_through_the_tab_on_top(app, monkeypatch, tmp_path):
    from wiring import dispose, real_window
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    w = real_window()
    try:
        w.resize(1512, 875)
        w.show()
        w.shell.host.activate("design")
        _pump(app)
        pol = w.shell.dock_policy
        groups = [g for g in ([d, *w.tabifiedDockWidgets(d)] for d in pol.side_docks()) if len(g) > 1]
        if not groups:
            pytest.skip("no tabbed side docks in this layout")
        order = [d for d in pol.side_docks() if d in groups[0]]
        first, second = order[0], order[1]      # the policy meets ``first`` first: put it behind
        second.raise_()
        _pump(app)
        pol.apply()
        _pump(app)
        target = pol.targets()
        want = next(v for d, v in target.items() if d in (first, second))
        # the shown tab narrowed (as a window shrink does); the hidden one keeps its width
        pol.enabled = False
        w.resizeDocks([second], [max(second.minimumSizeHint().width() + 10, want - 60)], Qt.Horizontal)
        _pump(app)
        assert second.width() < want - 20
        pol.enabled = True
        pol.apply()
        _pump(app)
        assert abs(second.width() - want) <= 2, (second.width(), want)
    finally:
        dispose(w)
