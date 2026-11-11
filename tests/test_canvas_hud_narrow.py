"""The canvas bar stays one row in a narrow view: where the words of *Fit drawing* and *Fit
table* would wrap the row, the two buttons show their icons alone (their names stay in the
tooltip and for assistive tech).  With the macOS font the row wrapped at half screen and the bar
covered 14 % of the view (tests/test_half_screen.py, AREA_SHARE 12 %)."""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _settle(app, sec: float = 0.4) -> None:
    t = time.monotonic() + sec
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)


def test_fit_buttons_fold_to_icons_rather_than_wrap_the_row(app):
    from mlaser.ops import demo_document
    from wiring import dispose, real_window
    w = real_window()
    w.show()
    try:
        w.set_document(demo_document())
        hud = w.canvas.hud
        w.resize(1512, 875)
        _settle(app)
        hud.place()
        one_row = hud.height()
        assert hud.fit_drawing.toolButtonStyle() == Qt.ToolButtonTextOnly        # room: words
        assert not hud.fit_drawing.icon().isNull()
        w.resize(683, 875)
        _settle(app)
        # wider words (another platform's font): they no longer fit on one row at half screen
        texts = hud.fit_drawing.text(), hud.fit_table.text()
        hud.fit_drawing.setText(texts[0] + " " + texts[0])
        hud.fit_table.setText(texts[1] + " " + texts[1])
        hud.place()
        try:
            v = w.canvas
            assert hud.fit_drawing.toolButtonStyle() == Qt.ToolButtonIconOnly
            assert hud.fit_table.toolButtonStyle() == Qt.ToolButtonIconOnly
            assert hud.fit_drawing.isVisible() and hud.fit_drawing.toolTip()
            assert hud.fit_drawing.accessibleName() == texts[0]
            assert hud.height() == one_row, (hud.geometry(), one_row)
            assert hud.width() * hud.height() <= hud.AREA_SHARE * v.width() * v.height() + 1
        finally:
            hud.fit_drawing.setText(texts[0])
            hud.fit_table.setText(texts[1])
        w.resize(1512, 875)
        _settle(app)
        hud.place()
        assert hud.fit_drawing.toolButtonStyle() == Qt.ToolButtonTextOnly        # words again
    finally:
        dispose(w)



def test_the_bar_holds_its_widest_control_in_any_view(app):
    """Where even the icon-only row does not fit the view beside the ruler (Windows' metrics at
    the window's minimum width: *Fit table* was cut by 4 px), the bar takes the width its widest
    control needs and moves over the ruler; no control is ever narrower than it needs or cut.
    Simulated with a style whose tool buttons are wider than the room beside the ruler (and
    narrower than the view)."""
    from PySide6.QtWidgets import QProxyStyle, QStyle, QToolButton, QWidget

    from mlaser.ops import demo_document
    from wiring import dispose, real_window

    class _Wide(QProxyStyle):
        extra = 0

        def sizeFromContents(self, ct, opt, size, widget=None):  # noqa: N802 - Qt API
            s = super().sizeFromContents(ct, opt, size, widget)
            if ct == QStyle.CT_ToolButton:
                s.setWidth(s.width() + self.extra)
            return s

    w = real_window()
    w.show()
    wide = _Wide("Fusion")
    hud = w.canvas.hud
    try:
        w.set_document(demo_document())
        w.shell.host.activate("process")
        w.resize(527, 648)
        _settle(app)
        v = w.canvas
        inset = v.ruler_px() + 4 if v.options.show_rulers else 8
        room = v.width() - inset - 8                     # beside the ruler
        b0 = hud.fit_table
        b0.setToolButtonStyle(Qt.ToolButtonIconOnly)
        lm, fm = hud.layout().contentsMargins(), hud.contentsMargins()
        pad = lm.left() + lm.right() + fm.left() + fm.right()
        # each tool button alone needs more than the room beside the ruler, less than the view
        wide.extra = room - b0.sizeHint().width() - pad + 6
        for b in hud.findChildren(QToolButton):
            b.setStyle(wide)
        hud.place()
        assert pad + b0.sizeHint().width() > room and pad + b0.sizeHint().width() <= v.width() - 2
        assert v.rect().contains(hud.geometry()), (v.size(), hud.geometry())
        for c in hud.findChildren(QWidget):
            if not c.isVisibleTo(hud) or c.parentWidget() is not hud:
                continue
            assert hud.rect().contains(c.geometry()), (c.objectName(), c.geometry(), hud.size())
            assert c.width() >= c.minimumSizeHint().width(), (c.objectName(), c.width())
    finally:
        for b in hud.findChildren(QToolButton):
            b.setStyle(None)
        dispose(w)
