"""A tab bar never sets a window's minimum width to the width of all its tabs (native check,
macOS style: SH_TabBar_PreferNoArrows turned the bar's scroll arrows off, and the Report
workspace's four touch tabs made its minimum 730 px, wider than half a 1366 px screen)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QLabel, QProxyStyle, QStyle, QTabWidget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _NoArrows(QProxyStyle):
    """Fusion with the macOS style's preference for tab bars without scroll arrows."""

    def styleHint(self, hint, opt=None, widget=None, ret=None):  # noqa: N802 - Qt API
        if hint == QStyle.SH_TabBar_PreferNoArrows:
            return 1
        return super().styleHint(hint, opt, widget, ret)


@pytest.mark.parametrize("kind", ["workspace", "dock"])
def test_tabs_scroll_instead_of_widening_the_window(app, kind):
    from mlaser.ui import style
    proxy = _NoArrows("Fusion")
    plain = QTabWidget()
    ours = style.tabs(QTabWidget(), kind)
    for t in (plain, ours):            # a style change (the application's style) re-reads the hint
        t.setStyle(proxy)
        t.tabBar().setStyle(proxy)
    try:
        for t in (plain, ours):
            for title in ("Per day", "Jobs", "Per job", "Alarm history", "Another tab", "And one more"):
                t.addTab(QLabel("x"), title)
        all_tabs = sum(ours.tabBar().tabRect(i).width() for i in range(ours.count()))
        assert not plain.usesScrollButtons()                   # the style's own choice: no arrows
        assert ours.usesScrollButtons()
        assert ours.minimumSizeHint().width() < all_tabs / 2, (ours.minimumSizeHint(), all_tabs)
    finally:
        plain.deleteLater()
        ours.deleteLater()
        app.processEvents()
