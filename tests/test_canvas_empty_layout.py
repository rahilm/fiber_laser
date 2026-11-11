"""The canvas's empty state never covers the canvas bar (the review's wave-1 leftover: at a window
649 px high, a 14" MacBook with the Dock shown, the panel lay over *Toolpath*, the legend and
*Fit drawing*).  It is centred below the bar; a view too short for that makes the bar step
aside while there is no drawing, and the bar comes back with one.  Sizes are sample points,
from the supported floor to half a screen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(app):
    from wiring import dispose, real_window
    w = real_window()
    w.show()
    yield w
    dispose(w)


def _settle(win, app, size) -> None:
    win.resize(*size)
    sig, same = None, 0
    for _ in range(60):                       # until the canvas and its children are at rest
        app.processEvents()
        v = win.canvas
        now = (v.size().toTuple(), win.canvas_empty.geometry().getRect(), v.hud.geometry().getRect(),
               v.hud.isVisible(), win.canvas_empty.isVisible())
        same = same + 1 if now == sig else 0
        sig = now
        if same >= 3:
            break


def _in_view(w, view) -> QRect:
    return QRect(w.mapTo(view, QPoint(0, 0)), w.size())


@pytest.mark.parametrize("size", [(1280, 649), (1512, 649), (1280, 800), (683, 649), (683, 875), (756, 945)])
def test_empty_state_never_covers_the_canvas_bar(win, app, size):
    from mlaser.model import Document
    win.set_document(Document())
    win.show_workspace("design")
    _settle(win, app, size)
    v, e = win.canvas, win.canvas_empty
    assert e.isVisible(), size
    assert e.buttons["file.open"].isVisible() and e.buttons["sample"].isVisible()
    panel = _in_view(e, v)
    assert QRect(QPoint(0, 0), v.size()).contains(panel), (size, panel)
    for b in e.buttons.values():                               # every button whole
        if b.isVisible():
            assert e.rect().contains(b.geometry()), (size, b.objectName())
    if v.hud.isVisible():
        assert not panel.intersects(_in_view(v.hud, v)), (size, panel, v.hud.geometry())
    # a drawing: the panel goes, the bar is back
    win.open_sample()
    _settle(win, app, size)
    assert win.document.parts and not e.isVisible()
    assert v.hud.isVisible()
    win.set_document(Document())
    _settle(win, app, size)
    assert e.isVisible()


def test_tall_view_keeps_the_bar_and_the_whole_panel(win, app):
    """With room, nothing changes: the bar, and the panel with its title and explanation."""
    from mlaser.model import Document
    win.set_document(Document())
    win.show_workspace("design")
    _settle(win, app, (1512, 982))
    v, e = win.canvas, win.canvas_empty
    assert v.hud.isVisible() and e.isVisible() and e.title.isVisible() and e.text.isVisible()
    assert not _in_view(e, v).intersects(_in_view(v.hud, v))


def test_buttons_with_layout_item_margins_do_not_overlap(win, app):
    """The macOS style gives a push button "layout item" margins (its bezel's shadow): placed by
    those smaller rectangles, the panel's buttons overlapped by 8 px at a window 649 px high
    (native check, macOS style).  Simulated here with a style whose buttons have such margins."""
    from PySide6.QtWidgets import QProxyStyle, QStyle

    from mlaser.model import Document

    class _Bezel(QProxyStyle):
        def subElementRect(self, el, opt, widget=None):  # noqa: N802 - Qt API
            r = super().subElementRect(el, opt, widget)
            if el == QStyle.SE_PushButtonLayoutItem:
                return opt.rect.adjusted(6, 6, -6, -6)
            return r

    e = win.canvas_empty
    bezel = _Bezel("Fusion")
    for b in e.buttons.values():
        b.setStyle(bezel)
    try:
        win.set_document(Document())
        win.show_workspace("design")
        for size in ((1280, 649), (683, 649)):
            _settle(win, app, size)
            shown = [b for b in e.buttons.values() if b.isVisible()]
            assert len(shown) >= 2, size
            for b in shown:
                assert b.height() >= b.minimumHeight(), (size, b.objectName(), b.height())
                assert e.rect().contains(b.geometry()), (size, b.objectName())
            for a, b in zip(shown, shown[1:]):
                assert not a.geometry().intersects(b.geometry()), (size, a.geometry(), b.geometry())
    finally:
        for b in e.buttons.values():
            b.setStyle(None)
        _settle(win, app, (1280, 800))
