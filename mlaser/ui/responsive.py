"""Responsive layout helpers (docs/dev/RESPONSIVE.md).

Panels lay themselves out from the space they actually get (the window size, the width of the
dock the user dragged, the font size and the screen's scale), never from a list of screen sizes:
every threshold here is computed from the content ("the widest label plus its field no longer
fits", "two columns of the widest section no longer fit").

* :class:`FlowLayout`: items left to right, wrapping to the next row; ``uniform=True`` makes it
  an auto-fit grid (as many equal columns as the widest item allows), ``justify=True`` stretches
  each row to the full width.  It reports ``heightForWidth``, so the parent gives it the rows it
  needs.
* :class:`ResponsiveForm`: label beside the field while the widest label plus the widest field
  minimum fit, the label above the field otherwise.
* :class:`CollapsibleSection`: a titled section whose body folds away (a header button with an
  arrow; keyboard and screen-reader accessible).
* :class:`ColumnsPanel`: sections in 1, 2 or 3 columns, as many as the widest section allows;
  when the panel is shorter than its content it first turns compact and then folds the
  collapsible sections, lowest priority first (a section the user opened stays open), and
  unfolds them again when the room comes back: a control panel never needs a scroll bar.
* :func:`set_compact` / :class:`CompactWhenNarrow`: smaller paddings and icon-only tool buttons
  (their text goes to the tooltip) when the comfortable layout does not fit the width.
* :func:`columns_for`: the number of columns of a given minimum width that fit.
* :func:`mark_list`: a scroll area that holds a *list* (may scroll vertically); forms and
  control panels must not (tests/test_responsive_layout.py).
* :class:`FitArea`: the last-resort wrapper of a page: its minimum is the page's own minimum
  (so the window never shrinks below what the page needs), capped by the screen so a page that
  cannot fit a small screen scrolls instead of pushing the window off it.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QLabel, QLayout, QLayoutItem, QScrollArea, QSizePolicy,
                               QStackedWidget, QStyle, QStyleOptionButton, QToolButton, QVBoxLayout, QWidget, QWidgetItem)

#: property names used by the helpers and read by the layout harness
STATE_PROP = "responsive_state"
LIST_PROP = "responsive_scroll"
#: touch targets (px, logical): the primary run / safety controls, every other touch control
from .style import SP1, SP2, TARGET_RUN as TARGET, TARGET_TOUCH as TARGET_MIN  # noqa: E402 - one source (style.py)


class _KeepMin(QObject):
    """See :func:`keep_min_size`."""

    def __init__(self, w: QWidget, size: QSize):
        super().__init__(w)
        self.size = QSize(size)
        self._apply(w)

    def _apply(self, w: QWidget) -> None:
        m = w.minimumSize()
        want = m.expandedTo(self.size)
        if want != m:
            w.setMinimumSize(want)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() in (QEvent.Polish, QEvent.StyleChange, QEvent.FontChange, QEvent.Show):
            self._apply(obj)
        return False


def keep_min_size(w: QWidget, size: QSize) -> QWidget:
    """Give ``w`` at least ``size`` and keep it: a style sheet that sets ``min-height`` (and resets
    the widget's minimum to 0 when its rule stops matching, e.g. after a user-mode switch) cannot
    shrink a safety-relevant touch target (STOP, Hold, Start, Arm)."""
    old = w.findChild(_KeepMin, "", Qt.FindDirectChildrenOnly)
    if old is not None:
        w.removeEventFilter(old)
        old.deleteLater()
    f = _KeepMin(w, size)
    w.installEventFilter(f)
    return w


def columns_for(width: int, min_column: int, spacing: int = 0, max_columns: int = 3) -> int:
    """How many columns of at least ``min_column`` px fit ``width`` (1..``max_columns``)."""
    if min_column <= 0:
        return max_columns
    n = (width + spacing) // (min_column + spacing)
    return int(max(1, min(max_columns, n)))


def mark_list(area: QWidget) -> QWidget:
    """``area`` (a QScrollArea) shows a list: vertical scrolling is expected there."""
    area.setProperty(LIST_PROP, "list")
    return area


def _spacing(widget: Optional[QWidget], horizontal: bool = True) -> int:
    style = widget.style() if widget is not None else None
    if style is None:
        return 6
    pm = QStyle.PM_LayoutHorizontalSpacing if horizontal else QStyle.PM_LayoutVerticalSpacing
    s = style.pixelMetric(pm, None, widget)
    return s if s >= 0 else 6


# --------------------------------------------------------------------------- flow layout
class FlowLayout(QLayout):
    """Items left to right, wrapping to the next row (see the module docstring)."""

    def __init__(self, parent: Optional[QWidget] = None, uniform: bool = False, justify: bool = True,
                 hspacing: int = -1, vspacing: int = -1, max_columns: int = 0):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.uniform = uniform
        self.justify = justify
        self.max_columns = max_columns
        self._h = hspacing
        self._v = vspacing
        self._width = 0              # the width last laid out at (its rows set the minimum)

    # --- QLayout API
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def addWidget(self, w: QWidget) -> None:  # noqa: N802 - Qt API
        self.addChildWidget(w)
        # sizes and geometry in widget rectangles, not the style's "layout item" rectangles:
        # the macOS style's buttons, combos and check boxes have layout item margins (the
        # bezel's shadow), and rows sized from the smaller item rects left them shorter than
        # their minimum (28 of 32 px) and spilling out of the flow's box (native-check, macOS)
        w.setAttribute(Qt.WA_LayoutUsesWidgetRect, True)
        self.addItem(QWidgetItem(w))
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int) -> Optional[QLayoutItem]:  # noqa: N802 - Qt API
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int) -> Optional[QLayoutItem]:  # noqa: N802 - Qt API
        if 0 <= i < len(self._items):
            it = self._items.pop(i)
            self.invalidate()
            return it
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._do(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt API
        super().setGeometry(rect)
        if rect.width() != self._width:
            before = self._rows_h(self._width)
            self._width = rect.width()
            if self._rows_h(self._width) != before:
                # more (or fewer) rows at this width: the minimum height changes; tell the
                # parents (a splitter does not ask for height-for-width)
                QTimer.singleShot(0, self._changed)
        self._do(rect, apply=True)

    def _changed(self) -> None:
        try:
            self.invalidate()
            w = self.parentWidget()
            if w is not None:
                w.updateGeometry()
        except RuntimeError:          # deleted meanwhile
            pass

    def _rows_h(self, width: int) -> int:
        return self._do(QRect(0, 0, width, 0), apply=False) if width > 0 else 0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        """One row (what the items want when there is room)."""
        items = self._visible()
        m = self.contentsMargins()
        if not items:
            return QSize(m.left() + m.right(), m.top() + m.bottom())
        hs = self.hspacing()
        if self.uniform:
            cw = max(it.sizeHint().width() for it in items)
            n = len(items) if not self.max_columns else min(len(items), self.max_columns)
            w = n * cw + (n - 1) * hs
        else:
            w = sum(it.sizeHint().width() for it in items) + (len(items) - 1) * hs
        h = max(it.sizeHint().height() for it in items)
        if self.max_columns and len(items) > self.max_columns:
            rows = -(-len(items) // self.max_columns)
            h = rows * h + (rows - 1) * self.vspacing()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        """One item per row: the widest item's minimum."""
        items = self._visible()
        m = self.contentsMargins()
        w = max((self._min_w(it) for it in items), default=0)
        h = max((it.minimumSize().height() for it in items), default=0) + m.top() + m.bottom()
        # at the width it has, the rows it needs (a parent that does not ask for height for
        # width, such as a splitter, still gives the room: no row drawn over the next widget)
        h = max(h, self._rows_h(self._width))
        return QSize(w + m.left() + m.right(), h)

    # --- helpers
    def hspacing(self) -> int:
        return self._h if self._h >= 0 else (self.spacing() if self.spacing() >= 0 else _spacing(self.parentWidget()))

    def vspacing(self) -> int:
        return self._v if self._v >= 0 else (self.spacing() if self.spacing() >= 0 else
                                              _spacing(self.parentWidget(), False))

    def _visible(self) -> list[QLayoutItem]:
        return [it for it in self._items if not it.isEmpty()]

    @staticmethod
    def _min_w(it: QLayoutItem) -> int:
        """A button's text must stay readable: its minimum is its size hint."""
        w = it.widget()
        if w is not None and w.sizePolicy().horizontalPolicy() in (QSizePolicy.Fixed, QSizePolicy.Minimum,
                                                                  QSizePolicy.MinimumExpanding):
            return it.sizeHint().width()
        return max(it.minimumSize().width(), it.sizeHint().width() if w is not None and _is_button(w) else 0)

    @staticmethod
    def _item_h(it: QLayoutItem, width: int) -> int:
        if it.hasHeightForWidth():
            return max(it.heightForWidth(width), it.minimumSize().height())
        return it.sizeHint().height()

    def columns(self, width: int) -> int:
        """Uniform mode: the columns that fit ``width``."""
        items = self._visible()
        if not items:
            return 1
        cw = max(self._min_w(it) for it in items)
        n = columns_for(width, cw, self.hspacing(), max_columns=self.max_columns or len(items))
        return min(n, len(items))

    def _do(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        items = self._visible()
        if not items:
            return m.top() + m.bottom()
        hs, vs = self.hspacing(), self.vspacing()
        width = max(r.width(), 1)
        rows: list[list[tuple[QLayoutItem, int]]] = []
        if self.uniform:
            n = self.columns(width)
            cw = max((width - (n - 1) * hs) // n, 1)
            for i in range(0, len(items), n):
                rows.append([(it, cw) for it in items[i:i + n]])
        else:
            row: list[tuple[QLayoutItem, int]] = []
            used = 0
            for it in items:
                w = min(max(it.sizeHint().width(), self._min_w(it)), width)
                need = w if not row else used + hs + w
                if row and need > width:
                    rows.append(row)
                    row, used = [], 0
                    need = w
                row.append((it, w))
                used = need
            if row:
                rows.append(row)
        y = r.y()
        for row in rows:
            h = max(self._item_h(it, w) for it, w in row)
            if apply:
                widths = [w for _, w in row]
                if self.justify and not self.uniform:
                    extra = width - (sum(widths) + (len(row) - 1) * hs)
                    grow = [i for i, (it, _) in enumerate(row)
                            if it.widget() is None or it.widget().sizePolicy().horizontalPolicy()
                            not in (QSizePolicy.Fixed, QSizePolicy.Maximum)]
                    if extra > 0 and grow:
                        each, rest = divmod(extra, len(grow))
                        for k, i in enumerate(grow):
                            widths[i] += each + (1 if k < rest else 0)
                x = r.x()
                for (it, _), w in zip(row, widths):
                    ih = h if it.widget() is None or it.widget().sizePolicy().verticalPolicy() != QSizePolicy.Fixed \
                        else self._item_h(it, w)
                    it.setGeometry(QRect(QPoint(x, y), QSize(w, ih)))
                    x += w + hs
            y += h + vs
        return y - vs - r.y() + m.top() + m.bottom()


def _is_button(w: QWidget) -> bool:
    from PySide6.QtWidgets import QAbstractButton
    return isinstance(w, QAbstractButton)


def flow(widgets: Iterable[QWidget], uniform: bool = False, justify: bool = True, margins: int = 0,
         max_columns: int = 0) -> QWidget:
    """A widget holding ``widgets`` in a :class:`FlowLayout`."""
    box = QWidget()
    lay = FlowLayout(box, uniform=uniform, justify=justify, max_columns=max_columns)
    lay.setContentsMargins(margins, margins, margins, margins)
    for w in widgets:
        lay.addWidget(w)
    return box


# --------------------------------------------------------------------------- responsive form
def _hidden(w: QWidget) -> bool:
    """Hidden on purpose (``hide()`` / ``setVisible(False)``), not merely not shown yet: a
    widget created without a parent reports ``isHidden()`` until it is shown with one."""
    return w.isHidden() and w.testAttribute(Qt.WA_WState_ExplicitShowHide)


class _FormGrid(QGridLayout):
    """A ResponsiveForm's grid.  It reports the form's height for a width: a parent layout asks
    a child widget's *layout* for its height for width (QWidgetItem), never the widget itself."""

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return isinstance(self.parentWidget(), ResponsiveForm)

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        form = self.parentWidget()
        return form.heightForWidth(width) if isinstance(form, ResponsiveForm) else -1

    def minimumHeightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self.heightForWidth(width)


class ResponsiveForm(QWidget):
    """Rows of (label, field): the label beside the field when the widest label plus the widest
    field minimum fit the width, above it otherwise.  Its minimum width is the stacked one, so a
    narrow dock never forces it to scroll."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._grid = _FormGrid(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        # the minimum is the stacked one (minimumSizeHint), not the grid's side-by-side one
        self._grid.setSizeConstraint(QLayout.SetNoConstraint)
        self._rows: list[tuple[Optional[QWidget], QWidget]] = []
        self._beside = True
        self._placing = False
        self.setProperty(STATE_PROP, "beside")

    def layout_grid(self) -> QGridLayout:
        return self._grid

    def addRow(self, label, field: Optional[QWidget] = None) -> Optional[QWidget]:  # noqa: N802 - like QFormLayout
        """A row: ``label`` (text or widget) and ``field``; ``addRow(widget)`` or a None label:
        a row spanning both columns (a heading, a diagram, a row of buttons)."""
        if field is None and isinstance(label, QWidget):
            label, field = None, label
        if isinstance(label, str):
            label = QLabel(label) if label else None
        if isinstance(label, QLabel) and label.buddy() is None:
            label.setBuddy(field)
        assert field is not None
        self._rows.append((label, field))
        field.installEventFilter(self)       # shown / hidden directly: the grid follows
        self._place()
        return label

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() in (QEvent.ShowToParent, QEvent.HideToParent) and not self._placing:
            for lab, f in self._rows:
                if f is obj:
                    if lab is not None and _hidden(lab) != _hidden(f):
                        lab.setVisible(not _hidden(f))
                    self._place()
                    break
        return False

    def rows(self) -> list[tuple[Optional[QWidget], QWidget]]:
        return list(self._rows)

    def row_widgets(self, field: QWidget) -> tuple[Optional[QWidget], QWidget]:
        for lab, f in self._rows:
            if f is field or lab is field:
                return lab, f
        raise KeyError(field)

    def set_row_visible(self, field: QWidget, on: bool) -> None:
        """Show or hide the row of ``field`` (its label too); a hidden row takes no room."""
        lab, f = self.row_widgets(field)
        if _hidden(f) == (not on):
            return
        for w in (lab, f):
            if w is not None:
                w.setVisible(on)
        self._place()

    def is_row_visible(self, field: QWidget) -> bool:
        return not _hidden(self.row_widgets(field)[1])

    def beside(self) -> bool:
        return self._beside

    def _label_w(self) -> int:
        return max((lab.sizeHint().width() for lab, f in self._rows
                    if lab is not None and not _hidden(lab) and not _hidden(f)), default=0)

    def _field_min(self) -> int:
        return max((max(f.minimumSizeHint().width(), f.minimumWidth()) for lab, f in self._rows
                    if not _hidden(f) and lab is not None), default=0)

    def _span_min(self) -> int:
        return max((max(f.minimumSizeHint().width(), f.minimumWidth()) for lab, f in self._rows
                    if not _hidden(f) and lab is None), default=0)

    def needed_beside(self) -> int:
        m = self._grid.contentsMargins()
        hs = self._grid.horizontalSpacing()
        hs = hs if hs >= 0 else _spacing(self)
        return self._label_w() + hs + self._field_min() + m.left() + m.right()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        m = self._grid.contentsMargins()
        w = max(self._label_w(), self._field_min(), self._span_min()) + m.left() + m.right()
        return QSize(w, super().minimumSizeHint().height())

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        """The height at ``width`` in the arrangement that width gets (beside or above), not in
        the current one: the form switches only when it is resized, and a parent that sized it
        from the current arrangement left it short once it stacked (a spin box 22 of 24 px
        high in Run Control's DO section on the X11 and Windows runners)."""
        return self._height(width, width >= self.needed_beside())

    def _row_gap(self, upper: list, lower: list) -> int:
        """The grid's spacing between two rows: its own, or (-1) the style's spacing for the
        controls involved, as QGridLayout asks it (under macOS 10 px between a label and the
        field below it, 6 px elsewhere)."""
        vs = self._grid.verticalSpacing()
        if vs >= 0:
            return vs

        def types(ws):
            t = QSizePolicy.ControlType.DefaultType
            for w in ws:
                t |= w.sizePolicy().controlType()
            return t
        gap = self.style().combinedLayoutSpacing(types(upper), types(lower), Qt.Vertical, None, self)
        return gap if gap >= 0 else _spacing(self, False)

    def _height(self, width: int, beside: bool) -> int:
        g = self._grid
        hs = g.horizontalSpacing()
        hs = hs if hs >= 0 else _spacing(self)
        m = g.contentsMargins()
        inner = max(width - m.left() - m.right(), 1)

        def h(x: Optional[QWidget], w: int) -> int:
            if x is None or _hidden(x):
                return 0
            hfw = x.heightForWidth(w) if x.hasHeightForWidth() else -1
            return max(hfw, x.sizeHint().height(), x.minimumSizeHint().height(), x.minimumHeight())
        field_w = max(inner - self._label_w() - hs, 1) if beside else inner
        rows: list[tuple[int, list]] = []         # (height, the widgets in that grid row)
        for lab, f in self._rows:
            if _hidden(f):
                continue
            if lab is None:
                rows.append((h(f, inner), [f]))
            elif beside:
                rows.append((max(h(lab, self._label_w()), h(f, field_w)), [lab, f]))
            else:
                rows.append((h(lab, inner), [lab]))
                rows.append((h(f, inner), [f]))
        total = sum(r[0] for r in rows)
        total += sum(self._row_gap(rows[i][1], rows[i + 1][1]) for i in range(len(rows) - 1))
        return total + m.top() + m.bottom()

    def _place(self) -> None:
        self._placing = True
        try:
            self._place_rows()
        finally:
            self._placing = False

    def _place_rows(self) -> None:
        g = self._grid
        while g.count():
            g.takeAt(0)
        r = 0
        for lab, f in self._rows:
            if _hidden(f):
                continue             # a hidden row takes no grid row (no spacing either)
            if lab is None:
                g.addWidget(f, r, 0, 1, 2 if self._beside else 1)
                r += 1
            elif self._beside:
                g.addWidget(lab, r, 0, Qt.AlignVCenter | Qt.AlignLeft)
                g.addWidget(f, r, 1)
                r += 1
            else:
                g.addWidget(lab, r, 0)
                g.addWidget(f, r + 1, 0)
                r += 2
        g.setColumnStretch(0, 0 if self._beside else 1)
        g.setColumnStretch(1, 1 if self._beside else 0)
        self.updateGeometry()

    def set_beside(self, on: bool) -> None:
        if on != self._beside:
            self._beside = on
            self.setProperty(STATE_PROP, "beside" if on else "above")
            self._place()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        self.set_beside(ev.size().width() >= self.needed_beside())
        super().resizeEvent(ev)


# --------------------------------------------------------------------------- wrapping check box
class WrapCheckBox(QCheckBox):
    """A check box whose text wraps onto more lines when it is narrower than the text (a long
    option in a narrow form: "Stay down on short moves" made the technology form scroll
    sideways at 220 px with the macOS fonts).  It still carries its own text (DESIGN.md §3);
    its minimum width is the indicator plus the longest word, its height follows its width."""

    def _parts(self, width: int) -> tuple[QRect, QRect]:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.rect = QRect(0, 0, width, max(self.height(), 1))
        ind = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, opt, self)
        gap = self.style().pixelMetric(QStyle.PM_CheckBoxLabelSpacing, opt, self)
        text = QRect(ind.right() + 1 + gap, 0, max(1, width - ind.right() - 1 - gap), opt.rect.height())
        return ind, text

    def _text_h(self, width: int) -> int:
        _ind, text = self._parts(width)
        r = self.fontMetrics().boundingRect(QRect(0, 0, text.width(), 1 << 16), int(Qt.TextWordWrap),
                                            self.text().replace("&&", "\0").replace("&", "").replace("\0", "&"))
        return r.height()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        full = super().minimumSizeHint()
        words = self.text().replace("&", "").split() or [""]
        longest = max(self.fontMetrics().horizontalAdvance(w) for w in words)
        ind, text = self._parts(full.width())
        w = min(full.width(), text.x() + longest + 2)
        # one line high: the wrapped height comes through height for width, so a check box
        # that has room is exactly as tall as a plain one
        return QSize(w, full.height())

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def _one_line(self, width: int) -> bool:
        return self.fontMetrics().horizontalAdvance(self.text().replace("&", "")) <= self._parts(width)[1].width()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        one = super().sizeHint().height()
        if self._one_line(width):
            return one
        pad = max(0, one - self.fontMetrics().height())
        return max(one, self._text_h(width) + pad)

    def hitButton(self, pos) -> bool:  # noqa: N802 - Qt API
        return self.rect().contains(pos)

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt API
        from PySide6.QtGui import QPainter
        if self._one_line(self.width()):
            super().paintEvent(ev)           # one line: exactly the style's own check box
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        ind, text = self._parts(self.width())
        p = QPainter(self)
        o = QStyleOptionButton(opt)
        o.rect = QRect(ind.x(), (self.height() - ind.height()) // 2 if self.height() < 2 * ind.height() else ind.y(),
                       ind.width(), ind.height())
        self.style().drawPrimitive(QStyle.PE_IndicatorCheckBox, o, p, self)
        t = QStyleOptionButton(opt)
        t.rect = text
        self.style().drawItemText(p, text, int(Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap), self.palette(),
                                  self.isEnabled(), self.text().replace("&", ""), QPalette.WindowText)
        if self.hasFocus():
            from PySide6.QtWidgets import QStyleOptionFocusRect
            fr = QStyleOptionFocusRect()
            fr.initFrom(self)
            fr.rect = text
            self.style().drawPrimitive(QStyle.PE_FrameFocusRect, fr, p, self)
        p.end()


# --------------------------------------------------------------------------- collapsible section
class ElidedToolButton(QToolButton):
    """A tool button whose text is cut with "…" to the width it gets (the full text in the
    tooltip and the accessible name): a long title never widens its panel."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ""
        self._tip_extra = ""
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full = text
        super().setText(text)
        self.setAccessibleName(text)
        self._fit()
        self.updateGeometry()

    def full_text(self) -> str:
        return self._full

    def hint_for(self, text: str) -> QSize:
        """The size the button needs to show ``text`` (as ``QToolButton.sizeHint`` computes it,
        without its cache, which a font change after the text does not clear)."""
        from PySide6.QtWidgets import QStyle, QStyleOptionToolButton
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(text) if text else 0
        h = fm.height()
        style = self.toolButtonStyle()
        if style != Qt.ToolButtonTextOnly and (self.arrowType() != Qt.NoArrow or not self.icon().isNull()):
            ic = self.iconSize()
            if style == Qt.ToolButtonTextBesideIcon or not text:
                w += ic.width() + (4 if text else 0)
                h = max(h, ic.height())
            elif style == Qt.ToolButtonTextUnderIcon:
                w = max(w, ic.width())
                h += ic.height() + 4
        opt.rect = QRect(0, 0, w, h)
        return self.style().sizeFromContents(QStyle.CT_ToolButton, opt, QSize(w, h), self)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.hint_for(QToolButton.text(self))

    def full_width(self) -> int:
        """The width the whole text needs (the size hint is the shown, possibly cut, text's)."""
        return self.hint_for(self._full).width()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.hint_for("M…" if self._full else "")

    def _fit(self) -> None:
        if self.width() > 0 and self.hint_for(self._full).width() > self.width():
            room = max(0, self.width() - self.hint_for("").width())
            text = self.fontMetrics().elidedText(self._full, Qt.ElideRight, room)
            while text and self.hint_for(text).width() > self.width() and room > 0:
                room -= 2
                text = self.fontMetrics().elidedText(self._full, Qt.ElideRight, room)
        else:
            text = self._full
        if text != QToolButton.text(self):
            QToolButton.setText(self, text)
        cut = text != self._full
        tip = self._full if cut else ""
        if self._tip_extra:
            tip = (tip + "\n" if tip else "") + self._tip_extra
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit()
            self.updateGeometry()

    def set_tip_extra(self, text: str) -> None:
        self._tip_extra = text
        self._fit()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self._fit()

    muted = False        # drawn in the palette's placeholder colour (a summary, not a title)

    def paintEvent(self, ev) -> None:  # noqa: N802 - Qt API
        if not self.muted:
            super().paintEvent(ev)
            return
        from PySide6.QtWidgets import QStyle, QStyleOptionToolButton, QStylePainter
        p = QStylePainter(self)
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        c = opt.palette.color(QPalette.PlaceholderText)
        for role in (QPalette.ButtonText, QPalette.WindowText, QPalette.Text):
            opt.palette.setColor(role, c)
        p.drawComplexControl(QStyle.CC_ToolButton, opt)


class _HeaderRow(QWidget):
    """The header of a section with ``fit_title``: the title (cut with "…" when narrow) and,
    while the section is folded, its summary in the rest of the row (cut too, or hidden)."""

    def __init__(self, title: ElidedToolButton, summary: ElidedToolButton):
        super().__init__()
        self.title_btn, self.summary_btn = title, summary
        title.setParent(self)
        summary.setParent(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _gap(self) -> int:
        return 4

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.title_btn.full_width()
        if self.property("folded") is True and self.summary_btn.full_text():
            w += self._gap() + self.summary_btn.full_width()
        return QSize(w, max(self.title_btn.sizeHint().height(), self.summary_btn.sizeHint().height()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(self.title_btn.minimumSizeHint().width(), self.sizeHint().height())

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.place()

    def place(self) -> None:
        W, H = self.width(), self.height()
        tw = min(self.title_btn.full_width(), W)
        self.title_btn.setGeometry(0, 0, tw, H)
        rest = W - tw - self._gap()
        show = bool(self.summary_btn.full_text()) and rest >= self.summary_btn.minimumSizeHint().width() \
            and self.property("folded") is True
        self.summary_btn.setVisible(show)
        if show:
            self.summary_btn.setGeometry(tw + self._gap(), 0, rest, H)


class CollapsibleSection(QWidget):
    """A titled section whose body folds away.  ``collapsible=False`` makes a plain titled
    section (the header is a label).  ``priority``: :class:`ColumnsPanel` folds the lowest
    first.  ``fit_title``: the title is cut with "…" when the panel is narrow (full text in its
    tooltip) and a folded section shows its :meth:`set_summary` line after the title."""
    toggled = Signal(bool)
    _choices = 0          # order of the user's choices (the newest one stays open)

    def __init__(self, title: str = "", body: Optional[QWidget] = None, collapsible: bool = True,
                 priority: int = 0, parent: Optional[QWidget] = None, fit_title: bool = False):
        super().__init__(parent)
        self.collapsible = collapsible
        self.priority = priority
        self.fit_title = fit_title and collapsible
        self.user_choice: Optional[bool] = None     # the user opened / closed it (None: automatic)
        self.choice_order = 0
        self._title = ""
        self._summary = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP1)
        # the header: a tool button with an arrow (keyboard and screen readers can open and
        # close it), a plain bold label when the section does not fold
        self.header: QWidget
        self.summary_btn: Optional[ElidedToolButton] = None
        if collapsible:
            hb = ElidedToolButton() if self.fit_title else QToolButton()
            hb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            hb.setAutoRaise(True)
            hb.setCheckable(True)
            hb.setChecked(True)
            hb.setArrowType(Qt.DownArrow)
            hb.clicked.connect(self._user_toggle)
            self.header = hb
        else:
            self.header = QLabel()
        self.header.setObjectName("section_header")
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        f = self.header.font()
        f.setBold(True)
        self.header.setFont(f)
        self.header_row: QWidget = self.header
        if self.fit_title:
            sb = ElidedToolButton()
            sb.setObjectName("section_summary")
            sb.setAutoRaise(True)
            sb.setToolButtonStyle(Qt.ToolButtonTextOnly)
            sb.setFocusPolicy(Qt.NoFocus)            # the title button is the keyboard target
            sb.muted = True
            sb.clicked.connect(lambda: self._user_toggle(True))
            self.summary_btn = sb
            self.header_row = _HeaderRow(self.header, sb)   # type: ignore[arg-type]
            self.header_row.setProperty("folded", False)
        v.addWidget(self.header_row)
        self.frame = QFrame()
        self.frame.setObjectName("section_body")
        self.frame.setFrameShape(QFrame.StyledPanel)
        fl = QVBoxLayout(self.frame)
        fl.setContentsMargins(SP1 + SP1 // 2, SP1 + SP1 // 2, SP1 + SP1 // 2, SP1 + SP1 // 2)   # 6 px: the narrowest dock still fits
        v.addWidget(self.frame)
        self.body = body
        if body is not None:
            fl.addWidget(body)
        self.setTitle(title)

    def setTitle(self, text: str) -> None:  # noqa: N802 - like QGroupBox
        self._title = text
        self.header.setText(text)
        self.header.setAccessibleName(text)
        self._tip()

    def title(self) -> str:
        return self._title

    def set_summary(self, text: str) -> None:
        """One line of what the folded section holds ("line 3 mm · outside 0.2 mm"): shown
        after the title while the section is folded (``fit_title`` sections)."""
        self._summary = text
        if self.summary_btn is not None:
            self.summary_btn.setText(text)
            self.header.setAccessibleDescription(text)
            self.header_row.updateGeometry()
            self.header_row.place()  # type: ignore[attr-defined]
        self._tip()

    def summary(self) -> str:
        return self._summary

    def _tip(self) -> None:
        if not self.collapsible:
            return
        extra = ("" if self.expanded() else
                 (self._summary + "\n" if self._summary else "") + "(collapsed: click to show)")
        if isinstance(self.header, ElidedToolButton):
            self.header.set_tip_extra(extra)
        else:
            self.header.setToolTip(self.title() + (" " + extra if extra else ""))

    def expanded(self) -> bool:
        return not self.frame.isHidden()

    def set_expanded(self, on: bool) -> None:
        if on == self.expanded():
            return
        self.frame.setVisible(on)
        if isinstance(self.header, QToolButton):
            self.header.setChecked(on)
            self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        if self.fit_title:
            self.header_row.setProperty("folded", not on)
            self.header_row.place()  # type: ignore[attr-defined]
        self._tip()
        self.updateGeometry()
        self.toggled.emit(on)

    def open_by_user(self) -> None:
        """Open it as if the user had (an "Edit in …" link): it stays open."""
        if not self.expanded() or self.user_choice is not True:
            self._user_toggle(True)

    def _user_toggle(self, on: bool) -> None:
        self.user_choice = on
        CollapsibleSection._choices += 1
        self.choice_order = CollapsibleSection._choices
        self.set_expanded(on)
        p = self.parentWidget()
        while p is not None and not isinstance(p, ColumnsPanel):
            p = p.parentWidget()
        if p is not None:
            p.section_chosen(self)

    def collapsed_height(self) -> int:
        return self.header_row.sizeHint().height()

    def height_for(self, width: int, expanded: Optional[bool] = None) -> int:
        """The section's height at ``width`` (expanded or not, default: as it is)."""
        exp = self.expanded() if expanded is None else expanded
        if not exp:
            return self.collapsed_height()
        lay = self.layout()
        sp = lay.spacing()
        fl = self.frame.layout()
        m = fl.contentsMargins()
        fw = self.frame.frameWidth()
        inner = width - m.left() - m.right() - 2 * fw
        body = 0
        if self.body is not None:
            body = self.body.heightForWidth(inner) if self.body.hasHeightForWidth() else -1
            if body < 0:
                body = self.body.sizeHint().height()
            body = max(body, self.body.minimumSizeHint().height())
        return self.collapsed_height() + sp + body + m.top() + m.bottom() + 2 * fw

    def min_width(self) -> int:
        """The section's minimum width when expanded (the body's minimum plus the frame)."""
        fl = self.frame.layout()
        m = fl.contentsMargins()
        body = 0
        if self.body is not None:
            body = max(self.body.minimumSizeHint().width(), self.body.minimumWidth())
        return max(body + m.left() + m.right() + 2 * self.frame.frameWidth(),
                   self.header_row.minimumSizeHint().width())


# --------------------------------------------------------------------------- compact mode
def set_compact(root: QWidget, on: bool, ratio: float = 0.5) -> None:
    """Compact mode of ``root``'s subtree: the layouts' margins and spacings scaled by ``ratio``
    (restored when ``on`` is False) and tool buttons marked ``compactable`` icon-only (their
    text stays in the tooltip and the accessible name)."""
    if bool(root.property("compact")) == on:
        return
    root.setProperty("compact", on)
    for w in [root, *root.findChildren(QWidget)]:
        lay = w.layout()
        if lay is not None:
            saved = lay.property("comfortable") if hasattr(lay, "property") else None
            if on:
                m = lay.contentsMargins()
                saved = (m.left(), m.top(), m.right(), m.bottom(), lay.spacing())
                lay.setProperty("comfortable", list(saved))
                lay.setContentsMargins(*(int(x * ratio) for x in saved[:4]))
                if saved[4] > 0:
                    lay.setSpacing(max(2, int(saved[4] * ratio)))
            elif saved:
                lay.setContentsMargins(*saved[:4])
                if saved[4] > 0:
                    lay.setSpacing(saved[4])
        if isinstance(w, QToolButton) and w.property("compactable"):
            if on:
                w.setProperty("comfortable_style", int(w.toolButtonStyle().value))
                if not w.toolTip():
                    w.setToolTip(w.text())
                w.setToolButtonStyle(Qt.ToolButtonIconOnly)
            elif w.property("comfortable_style") is not None:
                w.setToolButtonStyle(Qt.ToolButtonStyle(int(w.property("comfortable_style"))))
    # the layouts' cached sizes (a box layout's height for width, a parent's cached item sizes)
    # are stale until their next layout pass, and a panel measures its sections right after
    # this: drop the caches now, innermost first, so the next measure sees the new spacing
    # (Run Control was laid out with the compact heights once comfortable again: the jog pad
    # 2 px and the DO form 3-5 px short on the X11, Windows and macOS runners)
    for w in reversed([root, *root.findChildren(QWidget)]):
        lay = w.layout()
        if lay is not None:          # only containers: their sizes follow their layouts'
            lay.invalidate()
            w.updateGeometry()


class CompactWhenNarrow(QObject):
    """Turns ``widget`` compact (:func:`set_compact`) while its width is below what its
    comfortable layout needs, and comfortable again when that fits."""

    def __init__(self, widget: QWidget, on_change: Optional[Callable[[bool], None]] = None):
        super().__init__(widget)
        self.w = widget
        self.on_change = on_change
        self._comfortable = 0
        widget.installEventFilter(self)

    def comfortable_width(self) -> int:
        was = bool(self.w.property("compact"))
        if was:
            set_compact(self.w, False)
        need = self.w.sizeHint().width()
        if was:
            set_compact(self.w, True)
        return need

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if obj is self.w and ev.type() == QEvent.Resize:
            on = ev.size().width() < self.comfortable_width()
            if on != bool(self.w.property("compact")):
                set_compact(self.w, on)
                if self.on_change is not None:
                    self.on_change(on)
        return False


# --------------------------------------------------------------------------- columns panel
class ColumnsPanel(QWidget):
    """Sections in 1..``max_columns`` columns, folded to fit the height (module docstring).

    Sections keep their order, top to bottom and then left to right.  The layout is a function
    of the panel's size, the content and the user's open/closed choices only, so a panel shrunk
    and grown again looks as before."""
    layoutChanged = Signal()

    def __init__(self, max_columns: int = 3, parent: Optional[QWidget] = None, compact: bool = True,
                 grow: bool = False, scroll_overflow: bool = False):
        super().__init__(parent)
        self.max_columns = max_columns
        #: ``scroll_overflow``: the panel sits in a scroll area; what does not fit once every
        #: automatic section is folded (sections the user opened, or every section while
        #: :attr:`fold` is off) raises its minimum height, so that area scrolls instead of the
        #: newest section the user opened being folded again
        self.scroll_overflow = scroll_overflow
        #: ``fold`` off: no automatic folding (a searchable list of every field, say)
        self.fold = True
        #: ``refill``: after folding, reopen the folded sections that still fit
        self.refill = scroll_overflow
        #: the width a column needs before a second one is used (default: the narrowest
        #: workable one, :meth:`_col_min`); a form passes the width its labels beside the
        #: fields need, so two cramped columns never replace one comfortable one
        self.column_min: Optional[Callable[[], int]] = None
        #: ``grow``: one column while the height allows, more (asking the parent for the width
        #: through ``sizeHint``) before anything folds; else as many columns as the width allows
        self.grow = grow
        self._hint_cols = 1
        self.allow_compact = compact
        self.sections: list[CollapsibleSection] = []
        self.pinned: list[QWidget] = []     # widgets on top, always shown in full (e.g. STOP)
        self._busy = False
        self._comfortable_hint: Optional[QSize] = None
        self._comfortable_min_w = 0
        self._last: Optional[tuple] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.relayout)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def add_pinned(self, w: QWidget) -> QWidget:
        w.setParent(self)
        self.pinned.append(w)
        w.installEventFilter(self)
        self.schedule()
        return w

    def add_section(self, section: CollapsibleSection) -> CollapsibleSection:
        section.setParent(self)
        self.sections.append(section)
        section.installEventFilter(self)
        self.schedule()
        return section

    def spacing(self) -> int:
        return _spacing(self, False)

    def margins(self) -> int:
        return 2 if self.property("compact") else 4

    def schedule(self) -> None:
        self._last = None
        if not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() == QEvent.LayoutRequest and not self._busy:
            self.schedule()
        return False

    def event(self, ev) -> bool:
        if ev.type() in (QEvent.LayoutRequest, QEvent.FontChange, QEvent.StyleChange) and not self._busy:
            self.schedule()
        return super().event(ev)

    def section_chosen(self, s: CollapsibleSection) -> None:
        """The user opened or closed ``s``: lay out again (other automatic sections fold to make
        room for one the user opened)."""
        self._last = None
        self.relayout()

    # --- sizes
    def _count_min(self) -> int:
        """The column width the column count is computed from."""
        lo = self._col_min()
        return max(lo, self.column_min()) if self.column_min is not None else lo

    def _exp_min(self, expanded: dict) -> int:
        """The column width the column count is computed from, for this expansion: a folded
        section needs only its header's width, so a panel whose sections are all folded sets
        their headers side by side (Process's technology form: six folded headers stacked
        one per row kept the page taller than a 1280 x 800 window with the Windows fonts)."""
        ws = [s.min_width() if expanded.get(id(s), True) else s.header.minimumSizeHint().width()
              for s in self.sections if not s.isHidden()]
        ws += [max(p.minimumSizeHint().width(), p.minimumWidth()) for p in self.pinned if not p.isHidden()]
        lo = max(ws, default=0)
        if self.column_min is not None and any(expanded.get(id(s), True) for s in self.sections if not s.isHidden()):
            lo = max(lo, self.column_min())
        return lo

    def _col_min(self) -> int:
        ws = [s.min_width() for s in self.sections if not s.isHidden()]
        ws += [max(p.minimumSizeHint().width(), p.minimumWidth()) for p in self.pinned if not p.isHidden()]
        return max(ws, default=0)

    @staticmethod
    def _widget_h(p: QWidget, width: int) -> int:
        """A pinned widget's height at ``width``: its preferred height, never less than its
        minimum (an explicit minimum such as a touch target's counts, as in a layout)."""
        ph = p.heightForWidth(width) if p.hasHeightForWidth() else -1
        return max(ph if ph >= 0 else p.sizeHint().height(), p.minimumSizeHint().height(), p.minimumHeight())

    def _pinned_h(self, width: int) -> int:
        return sum(self._widget_h(p, width) + self.spacing() for p in self.pinned if not p.isHidden())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        s = self._min_now()
        if bool(self.property("compact")) and self._comfortable_min_w:
            # compact saves height, not width: a minimum width that shrank with the compact
            # paddings let the dock narrow, which made the panel comfortable again, which
            # widened it... (at the window's minimum the canvas alternated by 2 px)
            s.setWidth(max(s.width(), self._comfortable_min_w))
        return s

    def _min_now(self) -> QSize:
        m = 2 * self.margins()
        w = self._col_min()
        if self.scroll_overflow and self.width() > 0:
            # in a scroll area: the least height at the width it has (headers side by side)
            return QSize(w + m, self.folded_height(self.width()))
        h = self._pinned_h(w)
        for s in self.sections:
            if s.isHidden():
                continue
            h += (s.collapsed_height() if s.collapsible else s.height_for(w, True)) + self.spacing()
        return QSize(w + m, h + m)

    #: the comfortable column: this much wider than the narrowest workable one (when the
    #: sections' own preferred widths ask for that much), so rows of buttons need fewer lines
    COMFORT = 1.6

    def comfortable_width(self) -> int:
        lo = self._col_min()
        want = max((s.sizeHint().width() for s in self.sections if not s.isHidden()), default=lo)
        return max(lo, self._count_min(), min(want, int(lo * self.COMFORT)))

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        if bool(self.property("compact")) and self._comfortable_hint is not None:
            # the size it wants is the comfortable one: compact is what it does when it gets
            # less.  Measured compact, the hint was smaller, a dock sized from it stayed narrow,
            # and the panel stayed compact after the window grew again (live resize ended with
            # Run Control 23 px narrower and compact, X11 and macOS)
            return self._comfortable_hint
        return self._hint_now()

    def _hint_now(self) -> QSize:
        m = 2 * self.margins()
        w = self.comfortable_width()
        if self.grow and self._hint_cols > 1:
            n = self._hint_cols
            return QSize(n * w + (n - 1) * self.spacing() + m, max(self.height(), self.minimumSizeHint().height()))
        h = self._pinned_h(w) + sum(s.height_for(w, True) + self.spacing() for s in self.sections if not s.isHidden())
        return QSize(w + m, h + m)

    # --- layout
    def _plan(self, W: int, H: int, expanded: dict, n: int = 0) -> tuple[int, list[list[CollapsibleSection]], int]:
        """(columns, sections per column, tallest column height) for this expansion (``n``
        columns, default: as many as the width allows)."""
        sp = self.spacing()
        secs = [s for s in self.sections if not s.isHidden()]
        n = n or columns_for(W, self._exp_min(expanded), sp, self.max_columns)
        n = max(1, min(n, len(secs))) if secs else 1
        cw = (W - (n - 1) * sp) // n
        top = self._pinned_h(W)
        heights = [s.height_for(cw, expanded[id(s)]) for s in secs]
        if n == 1:
            return 1, [secs], top + sum(h + sp for h in heights)
        # balanced split in order: the smallest maximum column height (n <= 3: try all cuts)
        best: Optional[tuple[int, list[list[CollapsibleSection]]]] = None
        idx = range(1, len(secs))
        cuts_list: list[tuple[int, ...]] = [()]
        for _ in range(n - 1):
            cuts_list = [c + (i,) for c in cuts_list for i in idx if not c or i > c[-1]]
        for cuts in cuts_list:
            bounds = [0, *cuts, len(secs)]
            cols = [secs[bounds[i]:bounds[i + 1]] for i in range(n)]
            hs = [sum(heights[secs.index(s)] + sp for s in col) for col in cols]
            m = max(hs)
            if best is None or m < best[0]:
                best = (m, cols)
        assert best is not None
        return n, best[1], top + best[0]

    def relayout(self) -> None:
        if self._busy:
            return
        W = self.width() - 2 * self.margins()
        H = self.height() - 2 * self.margins()
        key = (W, H, tuple((id(s), s.user_choice, s.isHidden()) for s in self.sections), self.font().key(),
               self.fold)
        if key == self._last:
            return
        self._busy = True
        try:
            self._relayout(W, H)
        finally:
            self._busy = False
        self._last = key
        self.layoutChanged.emit()

    def _fits(self, W: int, H: int, exp: dict) -> bool:
        return self._plan(W, H, exp, self._cols(W, H, exp))[2] <= H + self.spacing()

    def _cols(self, W: int, H: int, exp: dict) -> int:
        """Columns to use: all the width allows, or (``grow``) the fewest that fit the height."""
        most = columns_for(W, self._exp_min(exp), self.spacing(), self.max_columns)
        if not self.grow:
            return most
        for n in range(1, most + 1):
            if self._plan(W, H, exp, n)[2] <= H + self.spacing():
                return n
        return most

    def _wanted_columns(self, H: int, exp: dict) -> int:
        """``grow``: the columns the height needs with every section open (the width asked of
        the parent in ``sizeHint``)."""
        cw = self.comfortable_width()
        for n in range(1, self.max_columns + 1):
            W = n * cw + (n - 1) * self.spacing()
            if self._plan(W, H, exp, n)[2] <= H + self.spacing():
                return n
        return self.max_columns

    def _relayout(self, W: int, H: int) -> None:
        secs = [s for s in self.sections if not s.isHidden()]
        exp = {id(s): (s.user_choice if s.user_choice is not None else True) if s.collapsible else True
               for s in secs}
        if self.grow:
            want = self._wanted_columns(H, dict(exp))
            if want != self._hint_cols:
                self._hint_cols = want
                self.updateGeometry()
        compact = False
        if self.allow_compact:
            if bool(self.property("compact")):
                set_compact(self, False)
                W, H = self.width() - 2 * self.margins(), self.height() - 2 * self.margins()
            # comfortable: what sizeHint reports while compact (the same value it reports now,
            # so no geometry update: an extra relayout pass here moved the canvas under a test's
            # mouse clicks)
            self._comfortable_hint = self._hint_now()
            self._comfortable_min_w = self._min_now().width()
            if not self._fits(W, H, exp):
                set_compact(self, True)
                compact = True
                W, H = self.width() - 2 * self.margins(), self.height() - 2 * self.margins()
        # fold automatic sections, lowest priority (then the last) first; then, if the user's
        # own choices still do not fit, the sections the user opened, oldest choice first (the
        # newest one stays open in a panel that scrolls its overflow)
        order = sorted([s for s in secs if s.collapsible], key=lambda s: (s.priority, -secs.index(s)))
        chosen = sorted([x for x in order if x.user_choice], key=lambda s: s.choice_order)
        if self.scroll_overflow and chosen:
            chosen = chosen[:-1]
        if self.fold:
            for s in [x for x in order if x.user_choice is None] + chosen:
                if self._fits(W, H, exp):
                    break
                exp[id(s)] = False
            if self.refill:
                # the room left: open again the folded automatic sections that fit, highest
                # priority first (a small section need not stay folded for a big one)
                for s in reversed(order):
                    if s.user_choice is None and not exp[id(s)]:
                        exp[id(s)] = True
                        if not self._fits(W, H, exp):
                            exp[id(s)] = False
        for s in secs:
            s.set_expanded(exp[id(s)])
        n, cols, _ = self._plan(W, H, exp, self._cols(W, H, exp))
        if self.scroll_overflow:
            self._overflow(W, H, secs, exp)
        self._place(W, n, cols)
        folded = [s.title() for s in secs if not exp[id(s)]]
        self.setProperty(STATE_PROP, f"{n} col{'s' if n > 1 else ''}" + (" compact" if compact else "")
                         + (f" folded: {', '.join(folded)}" if folded else ""))

    def _overflow(self, W: int, H: int, secs: list, exp: dict) -> None:
        """``scroll_overflow``: the height the panel needs with every automatic section folded
        (independent of the height it has, so the scroll area's resize cannot oscillate)."""
        least = {id(s): exp[id(s)] if (s.user_choice or not self.fold or not s.collapsible) else False
                 for s in secs}
        need = self._plan(W, H, least, self._cols(W, H, least))[2] - self.spacing() + 2 * self.margins()
        need = max(need, 0)
        if need > self.minimumSizeHint().height():
            if self.minimumHeight() != need:
                self.setMinimumHeight(need)
        elif self.minimumHeight():
            self.setMinimumHeight(0)

    def folded_height(self, width: int) -> int:
        """The least height at ``width``: every collapsible section folded, in as many columns
        as the width allows (a wide panel's headers sit side by side)."""
        secs = [s for s in self.sections if not s.isHidden()]
        exp = {id(s): not s.collapsible for s in secs}
        W = max(1, width - 2 * self.margins())
        n = columns_for(W, self._exp_min(exp), self.spacing(), self.max_columns)
        return self._plan(W, 1 << 20, exp, n)[2] - self.spacing() + 2 * self.margins()

    def needed_height(self, width: int) -> int:
        """The height every visible section needs open at ``width`` (a list that may scroll)."""
        secs = [s for s in self.sections if not s.isHidden()]
        exp = {id(s): s.user_choice is not False or not s.collapsible for s in secs}
        W = width - 2 * self.margins()
        return self._plan(W, 1 << 20, exp, self._cols(W, 1 << 20, exp))[2] - self.spacing() + 2 * self.margins()

    def _place(self, W: int, n: int, cols: list[list[CollapsibleSection]]) -> None:
        sp = self.spacing()
        m = self.margins()
        y = m
        for p in self.pinned:
            if p.isHidden():
                continue
            ph = self._widget_h(p, W)
            p.setGeometry(m, y, W, ph)
            y += ph + sp
        cw = (W - (n - 1) * sp) // max(n, 1)
        for c, col in enumerate(cols):
            x = m + c * (cw + sp)
            yy = y
            for s in col:
                h = s.height_for(cw)
                s.setGeometry(x, yy, cw, h)
                yy += h + sp

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.relayout()
        self.schedule()          # once more when the children's layouts have settled

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self._last = None
        self.relayout()
        self.schedule()


# --------------------------------------------------------------------------- table columns
class ColumnFitter(QObject):
    """A table or tree that never needs a horizontal scroll bar for its key column (rule R2):
    while the columns' content widths do not fit the view, the columns of lowest ``priority``
    hide (the key column, priority ``None``, never does; a stretched column gives way down to
    its minimum first); they come back when the view grows.  ``priorities``: column -> priority
    (higher stays longer); columns not listed have priority 0."""

    def __init__(self, view, priorities: Optional[dict] = None, stretch: Optional[int] = None):
        super().__init__(view)
        self.view = view
        self.priorities = dict(priorities or {})
        self.stretch = stretch
        self.auto_hidden: set[int] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fit)
        view.viewport().installEventFilter(self)
        m = view.model()
        if m is not None:
            for sig in (m.rowsInserted, m.rowsRemoved, m.modelReset, m.dataChanged, m.layoutChanged,
                        m.columnsInserted):
                sig.connect(lambda *_a: self.schedule())
        header = self._header()
        if stretch is not None:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(stretch, header.ResizeMode.Stretch)
        self.schedule()

    def _header(self):
        return self.view.horizontalHeader() if hasattr(self.view, "horizontalHeader") else self.view.header()

    def schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if ev.type() in (QEvent.Resize, QEvent.Show):
            self.schedule()
        return False

    def need(self, c: int) -> int:
        h = self._header()
        if c == self.stretch:
            return max(h.minimumSectionSize(), h.sectionSizeHint(c) // 2)
        if h.sectionResizeMode(c) in (h.ResizeMode.Interactive, h.ResizeMode.Fixed):
            return h.sectionSize(c) if not h.isSectionHidden(c) else h.defaultSectionSize()
        return max(h.sectionSizeHint(c), self.view.sizeHintForColumn(c))

    def fit(self) -> None:
        h = self._header()
        n = h.count()
        avail = self.view.viewport().width()
        if avail <= 0:
            return
        for c in list(self.auto_hidden):
            h.setSectionHidden(c, False)
        self.auto_hidden.clear()
        shown = [c for c in range(n) if not h.isSectionHidden(c)]
        need = sum(self.need(c) for c in shown)
        order = sorted([c for c in shown if self.priorities.get(c, 0) is not None and c != self.stretch],
                       key=lambda c: (self.priorities.get(c, 0), -c))
        for c in order:
            if need <= avail:
                break
            h.setSectionHidden(c, True)
            self.auto_hidden.add(c)
            need -= self.need(c)
        # what fits shows no scroll bar (not even the one Qt flashes while a stretched column
        # is rounded); what cannot fit scrolls
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if need <= avail else Qt.ScrollBarAsNeeded)
        self.view.setProperty(STATE_PROP, f"hidden columns: {sorted(self.auto_hidden)}" if self.auto_hidden else "all columns")


# --------------------------------------------------------------------------- page stack
class CurrentPageStack(QStackedWidget):
    """A stacked widget whose minimum and preferred sizes are the *current* page's (a plain
    QStackedWidget takes the largest of all its pages, so one tall page would set the window's
    minimum for all the others)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _i: self.updateGeometry())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.currentWidget()
        return w.minimumSizeHint().expandedTo(w.minimumSize()) if w is not None else QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.currentWidget()
        return w.sizeHint() if w is not None else QSize(0, 0)


# --------------------------------------------------------------------------- page wrapper
class FitArea(QScrollArea):
    """A page's wrapper (see the module docstring).  The page gets the viewport's size, never
    less than what it needs: its minimum width and, at that width, its minimum height (wrapped
    text and wrapping rows need more height in a narrow window).  That need is also this area's
    minimum, so the window never shrinks below it and the area never scrolls; it is capped by
    the screen, so a page that cannot fit a small screen at all scrolls instead of growing the
    window off it."""

    #: share of the screen's available size a page may claim as its minimum
    SCREEN_SHARE = 0.85

    def __init__(self, widget: QWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("shell_ws_scroll")
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(False)
        self.setWidget(widget)
        self._need = QSize(0, 0)
        self._hint = QSize(0, 0)
        self._sync_queued = False
        widget.installEventFilter(self)

    def _cap(self, size: QSize) -> QSize:
        """At most what the screen leaves for the page: its available size less the window's
        own chrome around this area (top bar, docks, run bar, frame), and at most
        :data:`SCREEN_SHARE` of the screen."""
        scr = self.screen()
        if scr is None:
            return size
        avail = scr.availableSize()
        cap = QSize(int(avail.width() * self.SCREEN_SHARE), int(avail.height() * self.SCREEN_SHARE))
        win = self.window()
        if win is not None and win is not self and win.isVisible() and self.isVisible():
            chrome = win.frameGeometry().size() - self.size()
            cap = cap.boundedTo(QSize(max(avail.width() - chrome.width(), 1), max(avail.height() - chrome.height(), 1)))
        return size.boundedTo(cap)

    def need(self, width: int) -> QSize:
        """The page's minimum size at ``width``."""
        w = self.widget()
        if w is None:
            return QSize(0, 0)
        m = w.minimumSizeHint().expandedTo(w.minimumSize())
        width = max(width, m.width())
        h = m.height()
        lay = w.layout()
        if lay is not None and lay.hasHeightForWidth():
            h = max(h, lay.minimumHeightForWidth(width))
        return QSize(m.width(), h)

    def sync(self) -> None:
        w = self.widget()
        if w is None:
            return
        # the whole area (scroll bars or not): a page that fits shows none
        avail = self.contentsRect().size()
        need = self.need(avail.width())
        hint = self.minimumSizeHint()
        if need != self._need or hint != self._hint:
            self._need, self._hint = need, hint
            self.updateGeometry()
        fits_w, fits_h = need.width() <= avail.width(), need.height() <= avail.height()
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff if fits_w else Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff if fits_h else Qt.ScrollBarAsNeeded)
        vp = self.viewport().size()
        size = QSize(max(vp.width(), need.width()), max(vp.height(), need.height()))
        if w.size() != size:
            w.resize(size)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        if obj is self.widget() and ev.type() == QEvent.LayoutRequest and not self._sync_queued:
            # with this area as the timer's context: Qt drops the call if the area is deleted
            # first (a window disposed of with a layout request queued), instead of calling
            # ``sync`` on a deleted wrapper
            self._sync_queued = True
            QTimer.singleShot(0, self, self._queued_sync)
        return False

    def _queued_sync(self) -> None:
        self._sync_queued = False
        self.sync()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self.sync()

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.sync()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        if self.widget() is None:
            return super().minimumSizeHint()
        need = self.need(self.viewport().width() if self.isVisible() else 0)
        return self._cap(need)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = self.widget()
        return w.sizeHint() if w is not None else super().sizeHint()


__all__ = ["WrapCheckBox", "TARGET", "TARGET_MIN", "keep_min_size", "ColumnFitter", "CurrentPageStack", "FlowLayout", "ResponsiveForm", "CollapsibleSection", "ColumnsPanel", "FitArea",
           "CompactWhenNarrow", "set_compact", "columns_for", "mark_list", "flow", "STATE_PROP", "LIST_PROP"]
