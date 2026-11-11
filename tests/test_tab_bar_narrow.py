"""Report's tab bar never sets the window's minimum width to the width of all its tabs (native
check, macOS style: SH_TabBar_PreferNoArrows turned the bar's scroll arrows off, and the Report
workspace's four touch tabs made its minimum 730 px, wider than half a 1366 px screen)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QTabWidget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _NoArrows(QProxyStyle):
    """Fusion with the macOS style's preference for tab bars without scroll arrows."""

    def styleHint(self, hint, opt=None, widget=None, ret=None):  # noqa: N802 - Qt API
        if hint == QStyle.SH_TabBar_PreferNoArrows:
            return 1
        return super().styleHint(hint, opt, widget, ret)


def test_report_tabs_scroll_instead_of_widening_the_window(app):
    from wiring import dispose, real_window
    w = real_window()
    proxy = _NoArrows("Fusion")
    plain = QTabWidget()
    try:
        w.show_workspace("report")
        app.processEvents()
        tabs = w.findChild(QTabWidget, "report_tabs")
        assert tabs is not None
        for t in (plain, tabs):        # a style change (the application's style) re-reads the hint
            t.setStyle(proxy)
            t.tabBar().setStyle(proxy)
        assert not plain.usesScrollButtons()                   # the style's own choice: no arrows
        assert tabs.usesScrollButtons()
        all_tabs = sum(tabs.tabBar().tabRect(i).width() for i in range(tabs.count()))
        assert tabs.minimumSizeHint().width() < all_tabs / 2, (tabs.minimumSizeHint(), all_tabs)
    finally:
        plain.deleteLater()
        dispose(w)
