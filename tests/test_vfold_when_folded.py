"""The docks under the page make room for a page that is short by a few pixels also when the
side docks are folded (Windows CI, 683 x 875: the Process Library, with the canvas hidden,
needed 426 px and got 401 because the preview and the machine-functions docks under it took
2 x 112 px; with the side docks folded nobody asked for the 25 px and the Library scrolled).

Here a 10 pt font at 683 px wide makes the Library a few pixels short at heights just below
the window's own height for it: at every height down to the window's minimum the page does not
scroll, the docks under it joined the side docks' tab group (folded, vfolded for the deficit),
and they come back under the page when the height is back, without folding to and fro."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402

from test_half_screen import app, settle, win  # noqa: E402,F401


def _under(w) -> list[str]:
    return [d.objectName() for d in w.shell.host.docks()
            if d.isVisible() and not d.isFloating() and w.dockWidgetArea(d) == Qt.BottomDockWidgetArea]


def test_library_short_with_the_side_docks_folded_moves_the_docks_under_it(win, app):  # noqa: F811 - the fixtures of test_half_screen
    from PySide6.QtWidgets import QAbstractScrollArea, QTabBar
    from mlaser.ops import demo_document
    font = app.font()
    big = app.font()
    big.setPointSizeF(10)
    app.setFont(big)
    try:
        win.set_document(demo_document())
        win.shell.host.activate("process")
        page = win.shell.host.pages["process"]
        win.resize(683, 1000)
        settle(app)
        if page.view_tabs.isVisible():
            page.view_tabs.setCurrentIndex(1)
            settle(app, 0.2)
        bar = next(b for b in win.findChildren(QTabBar) if b.objectName() == "process_pages")
        bar.setCurrentIndex(1)                            # the Library
        settle(app)
        pol = win.shell.dock_policy
        assert pol.folded is not None and pol.vfolded is None and _under(win), "the starting layout"
        under = _under(win)
        deficit_seen = False
        for h in range(1000, 760, -6):
            win.resize(683, h)
            settle(app, 0.2)
            scrolling = [(a.objectName(), a.viewport().height(), a.widget().minimumSizeHint().height())
                         for a in win.findChildren(QAbstractScrollArea)
                         if a.isVisible() and a.objectName() in ("techform_scroll", "shell_ws_scroll")
                         and a.verticalScrollBar().isVisible()]
            assert not scrolling, (h, win.size().toTuple(), scrolling, pol.folded is not None,
                                   pol.vfolded is not None, _under(win))
            if pol.folded is not None and pol.vfolded is not None and pol.vfolded.get("deficit"):
                deficit_seen = True
        # the case itself was met: short only by what the screen cap hid, side docks folded
        assert deficit_seen, "no height had the Library short with the side docks folded"
        win.resize(683, 1000)
        settle(app)
        assert pol.vfolded is None and _under(win) == under       # back under the page, as they were
    finally:
        app.setFont(font)
        settle(app, 0.1)
