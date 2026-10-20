"""Layout audit of a live window (tests/test_responsive_layout.py; docs/dev/RESPONSIVE.md).

:func:`audit` walks every visible widget of a top-level window and returns the violations of the
responsive-layout rules:

* ``hscroll``: a visible horizontal scroll bar anywhere (a canvas view excepted: panning a
  drawing is not a layout problem);
* ``vscroll``: a visible vertical scroll bar on a *form or control panel*, i.e. a ``QScrollArea``
  that is not marked as a list (``responsive.mark_list``); item views, text views and areas
  marked as lists may scroll;
* ``clipped``: a widget smaller than its ``minimumSizeHint``; a button narrower than its
  ``sizeHint`` (its text is cut); a label whose text does not fit, unless it carries a tooltip
  (the elided text is readable there); a combo box narrower than its current text; a widget that
  its ancestors cut (partly outside the visible part of a parent that does not scroll).

Each violation is a :class:`Violation` with the widget's path (object names / classes up to the
window), so a report names the panel to fix.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractScrollArea, QAbstractSpinBox,
                               QComboBox, QDockWidget, QGraphicsView, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QStyle, QStyleOptionButton, QStyleOptionComboBox, QTabBar,
                               QTextEdit, QWidget)

TOL = 1      # px: rounding of fractional layouts


@dataclass(frozen=True)
class Violation:
    kind: str          # hscroll | vscroll | clipped
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind:8s} {self.path}  {self.detail}"


def widget_path(w: QWidget, stop: QWidget | None = None) -> str:
    parts = []
    x = w
    while x is not None and x is not stop:
        name = x.objectName()
        cls = type(x).__name__
        if name and not name.startswith("qt_"):
            parts.append(f"{cls}#{name}")
        elif isinstance(x, (QDockWidget,)) or x.parentWidget() is stop:
            parts.append(cls)
        x = x.parentWidget()
    # keep the leaf and the named ancestors: short but locatable
    out = list(reversed(parts))
    if len(out) > 6:
        out = out[:2] + ["…"] + out[-4:]
    leaf = type(w).__name__ + (f"#{w.objectName()}" if w.objectName() else "")
    text = _text_of(w)
    if out and out[-1] != leaf:
        out.append(leaf)
    return " > ".join(out) + (f" '{text[:40]}'" if text else "")


def _text_of(w: QWidget) -> str:
    if isinstance(w, (QAbstractButton, QLabel)):
        return w.text().replace("\n", " ")
    if isinstance(w, QComboBox):
        return w.currentText()
    return ""


def is_list_area(a: QAbstractScrollArea) -> bool:
    """May this area scroll vertically?  Item views, text views and areas marked as lists."""
    if isinstance(a, (QAbstractItemView, QPlainTextEdit, QTextEdit, QGraphicsView)):
        return True
    x: QWidget | None = a
    while x is not None:
        if x.property("responsive_scroll") == "list":
            return True
        if x is not a and isinstance(x, QAbstractScrollArea):
            break
        x = x.parentWidget()
    return False


def _scroll_ancestor(w: QWidget) -> QAbstractScrollArea | None:
    x = w.parentWidget()
    while x is not None:
        if isinstance(x, QAbstractScrollArea) and x.viewport().isAncestorOf(w):
            return x
        x = x.parentWidget()
    return None


def _clip_rect(w: QWidget, top: QWidget) -> QRect:
    """The part of ``w`` (in its own coordinates) its ancestors up to ``top`` let show, ignoring
    scroll-area viewports (scrolled content is not clipped, it is scrolled)."""
    r = w.rect()
    x, off = w, QPoint(0, 0)
    while x is not None and x is not top:
        p = x.parentWidget()
        if p is None:
            break
        off = off + x.pos()
        if isinstance(p.parentWidget(), QAbstractScrollArea) and p is p.parentWidget().viewport():
            return r            # inside a scrolled viewport: the scroll bars decide
        r = r.intersected(p.rect().translated(-off))
        x = p
    return r


def _push_button_need(b: QPushButton) -> tuple[int, int]:
    """(width the text and icon need, width the button's contents rect has).  A push button's
    ``sizeHint`` adds the style's minimum width for dialog buttons (80 px in several styles),
    which says nothing about whether its text fits."""
    opt = QStyleOptionButton()
    b.initStyleOption(opt)
    room = b.style().subElementRect(QStyle.SE_PushButtonContents, opt, b).width()
    need = b.fontMetrics().horizontalAdvance(b.text().replace("&&", "\0").replace("&", "").replace("\0", "&"))
    if not b.icon().isNull():
        need += b.iconSize().width() + 4
    if b.menu() is not None:
        need += b.style().pixelMetric(QStyle.PM_MenuButtonIndicator, opt, b)
    return need, room


def _combo_clipped(c: QComboBox) -> bool:
    if c.isEditable():
        return False
    opt = QStyleOptionComboBox()
    c.initStyleOption(opt) if hasattr(c, "initStyleOption") else None
    opt.rect = c.rect()
    field = c.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, c)
    need = c.fontMetrics().horizontalAdvance(c.currentText())
    if not c.itemIcon(c.currentIndex()).isNull():
        need += c.iconSize().width() + 4
    return need > field.width() + TOL and not c.toolTip()


def audit(top: QWidget, skip=()) -> list[Violation]:
    """Violations in the visible part of ``top`` (see the module docstring).  ``skip``: widgets
    whose subtrees are not checked (e.g. a floating overlay)."""
    out: list[Violation] = []
    seen_paths: set[tuple[str, str]] = set()

    def add(kind: str, w: QWidget, detail: str) -> None:
        p = widget_path(w, top)
        if (kind, p) in seen_paths:
            return
        seen_paths.add((kind, p))
        out.append(Violation(kind, p, detail))

    skipped = [s for s in skip if s is not None]
    for w in top.findChildren(QWidget):
        if not w.isVisible() or w.window() is not top:
            continue
        if any(s is w or s.isAncestorOf(w) for s in skipped):
            continue
        if w.width() <= 0 or w.height() <= 0:
            continue
        if isinstance(w, QAbstractScrollArea):
            if isinstance(w, QGraphicsView):
                continue
            if w.horizontalScrollBar().isVisible():
                add("hscroll", w, f"content {w.horizontalScrollBar().maximum()} px wider than {w.viewport().width()}")
            if w.verticalScrollBar().isVisible() and not is_list_area(w):
                add("vscroll", w, f"content {w.verticalScrollBar().maximum()} px taller than {w.viewport().height()}")
            continue
        if not isinstance(w, (QAbstractButton, QLabel, QComboBox, QAbstractSpinBox, QLineEdit, QTabBar)):
            continue
        if isinstance(w, QTabBar) or isinstance(w.parentWidget(), (QAbstractSpinBox, QTabBar, QComboBox)):
            continue     # parts of a compound widget: the compound is checked
        vis = _clip_rect(w, top)
        if vis.isEmpty():
            continue     # not on screen at all: a tabbed dock behind its sibling, a collapsed part
        sa = _scroll_ancestor(w)
        msh = w.minimumSizeHint()
        size = w.size()
        if w.minimumWidth() == 0 and msh.width() > size.width() + TOL and not isinstance(w, QLabel):
            add("clipped", w, f"width {size.width()} < minimum {msh.width()}")
            continue
        if w.minimumHeight() == 0 and msh.height() > size.height() + TOL and not isinstance(w, QLabel):
            add("clipped", w, f"height {size.height()} < minimum {msh.height()}")
            continue
        if isinstance(w, QPushButton):
            need, room = _push_button_need(w)
            if w.text() and need > room + TOL:
                add("clipped", w, f"text needs {need} px, has {room}")
                continue
        elif isinstance(w, QAbstractButton):
            sh = w.sizeHint()
            if sh.width() > size.width() + TOL and w.text():
                add("clipped", w, f"text needs {sh.width()} px, has {size.width()}")
                continue
        elif isinstance(w, QLabel):
            if w.pixmap() is not None and not w.pixmap().isNull():
                pass
            elif w.text() and w.wordWrap():
                need = w.heightForWidth(w.width())
                if need > size.height() + TOL and not w.toolTip():
                    add("clipped", w, f"wrapped text needs {need} px high, has {size.height()}")
                    continue
            elif w.text() and not w.wordWrap():
                sh = w.sizeHint()
                if sh.width() > size.width() + TOL and not w.toolTip():
                    add("clipped", w, f"text needs {sh.width()} px, has {size.width()}")
                    continue
        elif isinstance(w, QComboBox):
            if _combo_clipped(w):
                add("clipped", w, f"current text does not fit {size.width()} px")
                continue
        # cut by an ancestor that does not scroll
        if sa is None and (vis.width() < size.width() - TOL or vis.height() < size.height() - TOL):
            add("clipped", w, f"cut by its parent: {vis.width()}x{vis.height()} of {size.width()}x{size.height()}")
    return out


def summary(violations) -> dict[str, int]:
    out = {"hscroll": 0, "vscroll": 0, "clipped": 0}
    for v in violations:
        out[v.kind] = out.get(v.kind, 0) + 1
    return out
