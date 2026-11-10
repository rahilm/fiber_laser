"""The part list: every part of the drawing as a row, with find / filter, two-way selection with
the canvas and *Assign technology…* for the selected parts (UX review 2026-11 finding 12).

It is a dock of its own (``parts``), tabbed with the Layer parameters and Properties docks in
Design and with Properties in Process; the dock policy folds it with the others in a narrow
window (docs/dev/RESPONSIVE.md).  The panel is a list (rows scroll); its controls are one
filter field and one row of buttons that wraps.

* :class:`PartListModel` is a ``QAbstractTableModel`` over ``document.parts``.  An edit
  updates it incrementally: a row whose part is the same object and not among the parts the
  edit changed (``DocumentService.changed``) is not looked at again, the others are compared
  value by value and only rows that differ are signalled (``dataChanged``); parts added at the
  end are inserted.  Sorting and filtering are done here on plain Python rows (the view order
  is a list of part indices), so selection sync maps rows to parts without a proxy model.
* The size column needs every part's bounding box: :class:`PartBoxes` caches it by part uid,
  validated by the identity of the part's segment objects (edits replace segment lists and
  segment objects, never mutate them in place: ``ops.document.transform_parts``,
  ``cad.edit``), so an edit re-measures only the parts it changed.  The Properties panel uses
  the same cache (``window.part_list.part_bbox``).
* Filtering never changes the document: it only hides rows.  The selection of hidden rows is
  kept (the canvas shows it).
* *Assign technology…* works on the existing per-layer mechanism (a technology record is
  assigned to a layer; parts follow their layer): the selected parts move to a layer that
  already follows the record, or to a free layer that is then assigned the record, in one
  undo step (:func:`plan_assignment`, :func:`assign_technology`).  When the selection is
  exactly the parts of one layer, that layer is assigned the record and nothing moves.
"""
from __future__ import annotations

import operator
import time
from dataclasses import dataclass, field
from itertools import chain
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import (QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QLabel, QLineEdit, QPushButton, QToolButton,
                               QTreeView, QVBoxLayout, QWidget)

from mlaser.services import units as U

from . import style
from .i18n import Translator
from .responsive import ColumnFitter, CurrentPageStack, flow

# columns
NUM, NAME, LAYER, TECH, SIZE, CONTOURS, ORDER, STATE = range(8)
N_COLS = 8
#: right-aligned, tabular figures
NUMERIC = frozenset({NUM, LAYER, SIZE, CONTOURS, ORDER})
#: ColumnFitter priorities: the name stretches and stays; then the number, the layer, ...
#: (a part with a warning also carries the warning icon beside its name, which never hides)
PRIORITIES = {NUM: 5, NAME: None, LAYER: 4, TECH: 3, STATE: 2.5, SIZE: 2, ORDER: 1, CONTOURS: 0}
#: the role that carries the part's index in the document
PART_ROLE = Qt.UserRole + 1
#: selection from the list reaches the canvas after this (ms): one canvas update per burst
#: of keyboard moves (holding an arrow key over 2,700 rows)
TO_CANVAS_MS = 30

_ROW_FLAGS = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
_NO_FLAGS = Qt.NoItemFlags

# warnings (bit flags)
W_OPEN = 1
W_NO_SETTINGS = 2


def _swatch(color: QColor, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(color)
    return QIcon(pm)


# --------------------------------------------------------------------------- bounding boxes
class PartBoxes:
    """``part.bbox()`` cached by uid while the part's segment objects are the same.  A part that
    was moved since (:meth:`shifted`, from a :class:`~mlaser.services.document.ChangedParts`
    with a ``shift``) gets its cached box moved along instead of measured again: two
    bounding-box passes over every part made a select-all nudge slow (perf review finding 6).
    The moved box equals a new measurement up to rounding in the last bits."""

    def __init__(self) -> None:
        self.cache: dict[int, tuple[tuple, tuple]] = {}   # uid -> (segments, bbox)
        self.moved: dict[int, tuple[float, float]] = {}   # uid -> how far it moved since its box
        self.hits = self.misses = self.shifts = 0

    def get(self, p) -> tuple:
        cs = p.contours
        segs = tuple(cs[0].segments) if len(cs) == 1 else tuple(chain.from_iterable(c.segments for c in cs))
        hit = self.cache.get(p.uid)
        if hit is not None and len(hit[0]) == len(segs) and all(map(operator.is_, hit[0], segs)):
            self.hits += 1
            return hit[1]
        moved = self.moved.pop(p.uid, None)
        if moved is not None and hit is not None and len(hit[0]) == len(segs):
            self.shifts += 1
            x0, y0, x1, y1 = hit[1]
            dx, dy = moved
            bb = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
            self.cache[p.uid] = (segs, bb)
            return bb
        self.misses += 1
        bb = p.bbox()
        self.cache[p.uid] = (segs, bb)
        return bb

    def shifted(self, uid: int, shift: tuple[float, float]) -> bool:
        """The part ``uid`` was moved by ``shift``: True when its cached box can move along."""
        if uid not in self.cache:
            return False
        m = self.moved.get(uid)
        self.moved[uid] = shift if m is None else (m[0] + shift[0], m[1] + shift[1])
        return True

    def prune(self, live: set) -> None:
        if len(self.cache) > 2 * max(len(live), 64):
            self.cache = {u: v for u, v in self.cache.items() if u in live}
            self.moved = {u: v for u, v in self.moved.items() if u in live}


# --------------------------------------------------------------------------- filter
@dataclass
class PartFilter:
    """What the list shows.  ``text``: words that must all match (part number, name, layer,
    technology or warning text, case-insensitive); ``#12`` is part 12 exactly; ``>50`` / ``<50``
    the larger side of the part's box in display units.  ``layer``: one layer or None.
    ``warnings``: only parts with a warning."""
    text: str = ""
    layer: Optional[int] = None
    warnings: bool = False
    _terms: list = field(default_factory=list, repr=False)

    def set_text(self, text: str) -> None:
        self.text = text
        terms = []
        for word in text.lower().split():
            if word[:1] in "<>" and len(word) > 1:
                try:
                    terms.append((word[0], float(word[1:].replace(",", "."))))
                    continue
                except ValueError:
                    pass
            if word[:1] == "#" and word[1:].isdigit():
                terms.append(("#", int(word[1:])))
                continue
            terms.append(("", word))
        self._terms = terms

    @property
    def active(self) -> bool:
        return bool(self._terms) or self.layer is not None or self.warnings


# --------------------------------------------------------------------------- model
class PartListModel(QAbstractTableModel):
    """One row per part (see the module docstring)."""

    def __init__(self, tr: Translator, boxes: PartBoxes, parent=None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.boxes = boxes
        self.parts: list = []                 # document.parts at the last update
        self.rows: list[tuple] = []           # per part: (name, layer, contours, w, h, open)
        self.layer_tech: dict[int, str] = {}  # layer -> technology label ("" when none)
        self.no_settings: set[int] = set()    # layers without cut settings (placeholders)
        self.order: dict[int, int] = {}       # part index -> cut order (1-based)
        self.view: list[int] = []             # part index of each row
        self.sort_col, self.sort_desc = NUM, False
        self.filter = PartFilter()
        self.order_fn: Optional[Callable[[], list]] = None   # [(part index, contour index)] in cut order
        self._swatches: dict[int, QIcon] = {}
        self._num_font: Optional[QFont] = None
        self._warn_icon: Optional[QIcon] = None
        self._texts: dict[str, str] = {}
        self.last_update_ms = 0.0
        self.retranslate()

    # ---------------------------------------------------------------- texts
    def retranslate(self) -> None:
        t = self.tr_.tr
        size = U.current().header(t("partlist.col.size"), "length")
        self._texts = {
            "part": t("np_Part"), "open": t("partlist.warn.open"), "no_settings": t("partlist.warn.no_settings"),
            "headers": [t("partlist.col.num"), t("partlist.col.name"), t("partlist.col.layer"),
                        t("partlist.col.tech"), size, t("partlist.col.contours"), t("partlist.col.order"),
                        t("partlist.col.state")],
            "no_tech": t("partlist.tech.none"),
        }
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, N_COLS - 1))
        self.headerDataChanged.emit(Qt.Horizontal, 0, N_COLS - 1)

    def name(self, i: int) -> str:
        return self.rows[i][0] or self.tr_.format("partlist.part_n", n=i + 1)

    def warnings(self, i: int) -> int:
        _name, layer, _nc, _w, _h, is_open = self.rows[i]
        return (W_OPEN if is_open else 0) | (W_NO_SETTINGS if layer in self.no_settings else 0)

    def warning_text(self, i: int) -> str:
        w = self.warnings(i)
        out = []
        if w & W_OPEN:
            out.append(self._texts["open"])
        if w & W_NO_SETTINGS:
            out.append(self._texts["no_settings"])
        return " · ".join(out)

    def tech(self, layer: int) -> str:
        return self.layer_tech.get(layer, "")

    def size_text(self, i: int) -> str:
        u = U.current()
        dec = 2 if u.unit("length") == "mm" else u.decimals("length")
        _n, _l, _c, w, h, _o = self.rows[i]
        return f"{u.number(w, 'length', dec)}×{u.number(h, 'length', dec)}"  # i18n: ok (a size)

    def summary(self, i: int) -> str:
        """The row as a screen reader says it."""
        _name, layer, nc, _w, _h, _o = self.rows[i]
        return self.tr_.format("partlist.a11y.row", n=i + 1, name=self.name(i), layer=layer,
                               tech=self.tech(layer) or self._texts["no_tech"], size=self.size_text(i),
                               contours=nc, state=self.warning_text(i) or self.tr_.tr("partlist.a11y.ok"))

    # ---------------------------------------------------------------- Qt model API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.view)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else N_COLS

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802 - Qt API
        if orientation != Qt.Horizontal or not 0 <= section < N_COLS:
            return None
        if role == Qt.DisplayRole:
            return self._texts["headers"][section]
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignRight if section in NUMERIC else Qt.AlignLeft) | Qt.AlignVCenter)
        return None

    def flags(self, index):
        # called for every cell a selection covers (21,600 calls for select-all of 2,700 parts):
        # a constant, not three enum ORs per call (0.5 s)
        return _ROW_FLAGS if index.isValid() else _NO_FLAGS

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.view):
            return None
        i = self.view[index.row()]
        col = index.column()
        name, layer, nc, _w, _h, _o = self.rows[i]
        if role == Qt.DisplayRole:
            if col == NUM:
                return str(i + 1)
            if col == NAME:
                return self.name(i)
            if col == LAYER:
                return str(layer)
            if col == TECH:
                return self.tech(layer)
            if col == SIZE:
                return self.size_text(i)
            if col == CONTOURS:
                return str(nc)
            if col == ORDER:
                o = self.order.get(i)
                return str(o) if o is not None else ""
            if col == STATE:
                return self.warning_text(i)
            return None
        if role == PART_ROLE:
            return i
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignRight if col in NUMERIC else Qt.AlignLeft) | Qt.AlignVCenter)
        if role == Qt.FontRole and col in NUMERIC:
            return self.num_font()
        if role == Qt.DecorationRole:
            if col == LAYER:
                return self.swatch(layer)
            if col in (NAME, STATE) and self.warnings(i):
                return self.warn_icon()
            return None
        if role == Qt.ToolTipRole:
            if col == STATE:
                return self.warning_text(i) or None
            if col == NAME and self.warnings(i):
                return f"{self.name(i)}\n{self.warning_text(i)}"  # i18n: ok (two translated texts, one per line)
            if col in (NAME, TECH):
                return self.data(index, Qt.DisplayRole) or None
            return None
        if role in (Qt.AccessibleTextRole, Qt.AccessibleDescriptionRole) and col in (NUM, NAME):
            return self.summary(i)
        return None

    def num_font(self) -> QFont:
        if self._num_font is None:
            f = QFont()
            try:
                f.setFeature(QFont.Tag("tnum"), 1)     # tabular figures: digits line up
            except (AttributeError, TypeError):        # Qt < 6.7
                pass
            self._num_font = f
        return self._num_font

    def swatch(self, layer: int) -> QIcon:
        ic = self._swatches.get(layer)
        if ic is None:
            from .canvas import layer_color
            ic = self._swatches[layer] = _swatch(layer_color(layer))
        return ic

    def warn_icon(self) -> QIcon:
        if self._warn_icon is None:
            from .icons import icon
            self._warn_icon = icon("alert-triangle", style.color("warning").name())
        return self._warn_icon

    def theme_changed(self) -> None:
        self._warn_icon = None
        if self.rowCount():
            self.dataChanged.emit(self.index(0, STATE), self.index(self.rowCount() - 1, STATE))

    # drag: the panel supplies the data (MainWindow._drag_mime: the selected parts)
    mime_fn: Optional[Callable[[], Any]] = None

    def mimeTypes(self) -> list:  # noqa: N802 - Qt API
        from .clipboard import MIME_DXF, MIME_PARTS, MIME_SVG
        return [MIME_PARTS, MIME_SVG, *MIME_DXF, "text/plain", "text/uri-list"]

    def mimeData(self, indexes):  # noqa: N802 - Qt API
        fn = self.mime_fn
        md = fn() if fn is not None else None
        return md if md is not None else super().mimeData(indexes)

    def supportedDragActions(self):  # noqa: N802 - Qt API
        return Qt.CopyAction

    def sort(self, column: int, order=Qt.AscendingOrder) -> None:
        self.sort_col, self.sort_desc = int(column), order == Qt.DescendingOrder
        self._relayout()

    # ---------------------------------------------------------------- view order
    def _accepts(self, i: int) -> bool:
        f = self.filter
        name, layer, _nc, w, h, _o = self.rows[i]
        if f.layer is not None and layer != f.layer:
            return False
        if f.warnings and not self.warnings(i):
            return False
        if not f._terms:
            return True
        hay = None
        for kind, v in f._terms:
            if kind == "#":
                if i + 1 != v:
                    return False
            elif kind in ("<", ">"):
                side = U.current().to_display(max(w, h), "length")
                if (kind == ">" and not side > v) or (kind == "<" and not side < v):
                    return False
            else:
                if hay is None:
                    hay = " ".join((str(i + 1), self.name(i), str(layer), self.tech(layer),
                                    self.warning_text(i))).lower()
                if v not in hay:
                    return False
        return True

    def _sort_key(self, col: int) -> Callable[[int], Any]:
        rows = self.rows
        if col == NAME:
            return lambda i: (self.name(i).lower(), i)
        if col == LAYER:
            return lambda i: (rows[i][1], i)
        if col == TECH:
            return lambda i: (self.tech(rows[i][1]).lower(), rows[i][1], i)
        if col == SIZE:
            return lambda i: (rows[i][3] * rows[i][4], i)
        if col == CONTOURS:
            return lambda i: (rows[i][2], i)
        if col == ORDER:
            big = len(rows) + 1
            return lambda i: (self.order.get(i, big), i)
        if col == STATE:
            return lambda i: (-self.warnings(i), i)
        return lambda i: i

    def compute_view(self) -> list[int]:
        idx: Iterable[int] = range(len(self.rows))
        if self.filter.active:
            idx = [i for i in idx if self._accepts(i)]
        if self.sort_col != NUM:
            out = sorted(idx, key=self._sort_key(self.sort_col), reverse=self.sort_desc)
        else:
            out = list(idx)
            if self.sort_desc:
                out.reverse()
        return out

    def _relayout(self) -> None:
        """Filter or sort changed: the rows are laid out again (a reset; the panel re-applies
        the selection from the document)."""
        self.beginResetModel()
        self.view = self.compute_view()
        self.endResetModel()

    def set_filter(self, text: Optional[str] = None, layer: Any = ..., warnings: Optional[bool] = None) -> None:
        if text is not None:
            self.filter.set_text(text)
        if layer is not ...:
            self.filter.layer = layer
        if warnings is not None:
            self.filter.warnings = bool(warnings)
        self._relayout()

    # ---------------------------------------------------------------- document
    def _row_of(self, p, i: int) -> tuple:
        x0, y0, x1, y1 = self.boxes.get(p)
        return (p.name or "", int(p.craft.layer), len(p.contours), x1 - x0, y1 - y0,
                any(not c.closed for c in p.contours))

    def update(self, doc, changed: Optional[set] = None) -> None:
        """Bring the rows up to date with ``doc`` (``changed``: the uids an edit may have
        changed, None = look at every part)."""
        t0 = time.perf_counter()
        parts = list(doc.parts) if doc is not None else []
        old_parts, old_rows = self.parts, self.rows
        new_rows: list[tuple] = []
        dirty: list[int] = []
        live = set()
        shift = getattr(changed, "shift", None)      # a move (DocumentService.ChangedParts)
        if changed is None:
            self.boxes.moved.clear()                 # not known what changed: measure again
        for i, p in enumerate(parts):
            live.add(p.uid)
            same = i < len(old_parts) and old_parts[i] is p
            if changed is not None and p.uid in changed:
                if shift is not None and self.boxes.shifted(p.uid, shift):
                    if same:                 # moved: name, layer, contours and size stay
                        new_rows.append(old_rows[i])
                        continue
                else:
                    self.boxes.moved.pop(p.uid, None)
            if changed is not None and same and p.uid not in changed:
                new_rows.append(old_rows[i])
                continue
            row = self._row_of(p, i)
            new_rows.append(row)
            if i >= len(old_rows) or old_rows[i] != row or old_parts[i] is not p:
                dirty.append(i)
        self.boxes.prune(live)
        # per layer: technology label and missing cut settings; the cut order
        from .workspaces.process import technology_name
        layers = {r[1] for r in new_rows}
        tech, no_settings = {}, set()
        for li in layers:
            lp = doc.layers.get(li) if doc is not None else None
            tech[li] = technology_name(doc, li) if doc is not None else ""
            if lp is None or (lp.extra or {}).get("placeholder"):
                no_settings.add(li)
        order = self._order(doc, len(parts))
        aux = tech != self.layer_tech or no_settings != self.no_settings
        order_changed = order != self.order
        self.parts, self.layer_tech, self.no_settings, self.order = parts, tech, no_settings, order
        old_view = self.view
        self.rows = new_rows
        new_view = self.compute_view()
        n_old = len(old_view)
        if new_view == old_view:
            self._emit_changed(dirty, aux, order_changed)
        elif len(new_view) > n_old and new_view[:n_old] == old_view:
            self.view = old_view
            self._emit_changed(dirty, aux, order_changed)
            self.beginInsertRows(QModelIndex(), n_old, len(new_view) - 1)
            self.view = new_view
            self.endInsertRows()
        else:
            self.beginResetModel()
            self.view = new_view
            self.endResetModel()
        self.last_update_ms = (time.perf_counter() - t0) * 1000

    def _emit_changed(self, dirty: list[int], aux: bool, order_changed: bool) -> None:
        n = len(self.view)
        if not n:
            return
        if aux:
            self.dataChanged.emit(self.index(0, 0), self.index(n - 1, N_COLS - 1))
            return
        if order_changed:
            self.dataChanged.emit(self.index(0, ORDER), self.index(n - 1, ORDER))
        if not dirty:
            return
        pos = {i: r for r, i in enumerate(self.view)} if (self.sort_col != NUM or self.sort_desc or
                                                         self.filter.active) else None
        rows = sorted(r for r in ((pos.get(i) if pos is not None else (i if i < n else None)) for i in dirty)
                      if r is not None)
        # contiguous runs, one signal each
        start = prev = None
        for r in rows:
            if start is None:
                start = prev = r
            elif r == prev + 1:
                prev = r
            else:
                self.dataChanged.emit(self.index(start, 0), self.index(prev, N_COLS - 1))
                start = prev = r
        if start is not None:
            self.dataChanged.emit(self.index(start, 0), self.index(prev, N_COLS - 1))

    def _order(self, doc, n: int) -> dict[int, int]:
        """Part index -> its place in the cutting order (the order of its first contour)."""
        seq = None
        if self.order_fn is not None:
            try:
                seq = self.order_fn()
            except Exception:  # noqa: BLE001 - the order column is information only
                seq = None
        if not seq:
            return {i: i + 1 for i in range(n)}
        out: dict[int, int] = {}
        for pi, _ci in seq:
            if pi not in out and 0 <= pi < n:
                out[pi] = len(out) + 1
        return out


# --------------------------------------------------------------------------- view
#: keys the list and its filter field use themselves: the window's own shortcuts for them (the
#: arrow keys nudge the selection, Esc deselects, Page Up / Down step through the parts) must
#: not fire while one of these has the focus.  The safety layer's Esc / Space while a job runs
#: is an application event filter and comes first (docs/SAFETY.md).
_OWN_KEYS = frozenset({Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_PageUp, Qt.Key_PageDown,
                       Qt.Key_Home, Qt.Key_End, Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter})


class _PartView(QTreeView):
    """The rows; Enter shows the selected parts on the canvas, Esc goes back to the filter."""
    activatedSelection = Signal()
    escaped = Signal()

    def event(self, ev) -> bool:
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.ShortcutOverride and ev.key() in _OWN_KEYS:
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev) -> None:  # noqa: N802 - Qt API
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not ev.modifiers():
            self.activatedSelection.emit()
            return
        if ev.key() == Qt.Key_Escape and not ev.modifiers():
            self.escaped.emit()
            return
        super().keyPressEvent(ev)

    def startDrag(self, actions) -> None:  # noqa: N802 - Qt API
        # copy only: a "move" accepted by the target must not remove rows from the list
        super().startDrag(Qt.CopyAction)


class _Fitter(ColumnFitter):
    """Columns hide before the name gets narrower than about a dozen characters (the default
    lets the key column give way to half its width first: "Fla…" beside a layer column)."""

    _seen: dict

    def need(self, c: int) -> int:
        h = self._header()
        if not hasattr(self, "_seen"):
            self._seen = {}
        if h.isSectionHidden(c) and c in self._seen:
            # a hidden section has no size hint (0): fit() subtracts what the column needed
            # while it was shown, measured just before it hid it
            return self._seen[c]
        if c == self.stretch:
            n = max(h.minimumSectionSize(),
                    min(self.view.sizeHintForColumn(c), self.view.fontMetrics().averageCharWidth() * 12))
        else:
            n = super().need(c)
        self._seen[c] = n
        return n


class PartListPanel(QWidget):
    """The Parts dock's content: filter row, the part rows (or an empty state) and a status
    line.  Signals: ``selectionToCanvas(list[int])`` (part indices selected in the list),
    ``zoomRequested(list[int])`` (show these parts on the canvas), ``assignRequested()``."""
    selectionToCanvas = Signal(list)
    zoomRequested = Signal(list)
    assignRequested = Signal()

    def __init__(self, tr: Translator, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("part_list_panel")
        self.tr_ = tr
        self.document = None
        self.boxes = PartBoxes()
        self.model = PartListModel(tr, self.boxes, self)
        self._sync = False
        self._selected: list[bool] = []
        lay = QVBoxLayout(self)
        style.margins(lay, "dock")
        lay.setSpacing(style.SP2)

        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("part_filter")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _t: self._filter_timer.start())
        self.filter_edit.installEventFilter(self)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_filter)
        lay.addWidget(self.filter_edit)

        self.layer_combo = QComboBox()
        self.layer_combo.setObjectName("part_filter_layer")
        self.layer_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.layer_combo.currentIndexChanged.connect(lambda _i: self.model.set_filter(
            layer=self.layer_combo.currentData()) if not self._sync else None)
        self.layer_combo.currentIndexChanged.connect(lambda _i: self._after_relayout())
        self.warn_btn = QToolButton()
        self.warn_btn.setObjectName("part_filter_warnings")
        self.warn_btn.setCheckable(True)
        self.warn_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        from .icons import icon
        self.warn_btn.setIcon(icon("alert-triangle"))
        self.warn_btn.toggled.connect(lambda on: (self.model.set_filter(warnings=on), self._after_relayout()))
        self.show_btn = QToolButton()
        self.show_btn.setObjectName("part_show_on_canvas")
        self.show_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.show_btn.setIcon(icon("focus-centered"))
        self.show_btn.clicked.connect(self.zoom_to_selection)
        self.assign_btn = QPushButton()
        self.assign_btn.setObjectName("part_assign_technology")
        self.assign_btn.clicked.connect(self.assignRequested.emit)
        self.controls = flow([self.layer_combo, self.warn_btn, self.show_btn, self.assign_btn], justify=False)
        lay.addWidget(self.controls)

        self.view = _PartView()
        self.view.setObjectName("part_list")
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setTextElideMode(Qt.ElideRight)
        self.view.setSortingEnabled(True)
        self.view.header().setSortIndicator(NUM, Qt.AscendingOrder)
        self.view.header().setSectionsMovable(False)
        self.view.setIconSize(QSize(12, 12))
        # drag the selected parts out: to the canvas (paste there), another mlaser window, a
        # drawing program or a file manager (a .dxf file); MainWindow sets the data source
        self.view.setDragEnabled(True)
        self.view.setDragDropMode(QAbstractItemView.DragOnly)
        self.view.setDefaultDropAction(Qt.CopyAction)
        self.view.selectionModel().selectionChanged.connect(self._view_selection_changed)
        self.view.doubleClicked.connect(lambda _i: self.zoom_to_selection())
        self.view.activatedSelection.connect(self.zoom_to_selection)
        self.model.modelReset.connect(self._after_relayout)
        self.model.rowsInserted.connect(lambda *_a: self._reselect())
        hdr = self.view.header()
        for c in range(N_COLS):
            if c != NAME:
                hdr.setSectionResizeMode(c, hdr.ResizeMode.ResizeToContents)
        # content widths from the rows on screen only: a column is not measured over 2,700 rows
        # on every edit (the default looks at 1,000)
        hdr.setResizeContentsPrecision(0)
        self.fitter = _Fitter(self.view, PRIORITIES, stretch=NAME)

        self.empty = QWidget()
        self.empty.setObjectName("part_list_empty")
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(style.SP2, style.SP3, style.SP2, style.SP3)
        self.empty_text = QLabel()
        self.empty_text.setObjectName("part_list_empty_text")
        self.empty_text.setWordWrap(True)
        self.empty_text.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.clear_btn = QPushButton()
        self.clear_btn.setObjectName("part_filter_clear")
        self.clear_btn.clicked.connect(self.clear_filter)
        ev.addWidget(self.empty_text)
        ev.addWidget(self.clear_btn, 0, Qt.AlignHCenter)
        ev.addStretch(1)
        self.stack = CurrentPageStack()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.empty)
        lay.addWidget(self.stack, 1)

        self.status = QLabel()
        self.status.setObjectName("part_list_status")
        style.set_font(self.status, "caption")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self._to_canvas = QTimer(self)
        self._to_canvas.setSingleShot(True)
        self._to_canvas.setInterval(TO_CANVAS_MS)
        self._to_canvas.timeout.connect(self._emit_selection)
        self.view.escaped.connect(self.focus_filter)      # Esc in the list: back to the filter

        tr.bind(self, "partlist.title", lambda _t: self.retranslate())
        from .units_qt import on_units_changed
        on_units_changed(self, lambda _u: (self.model.retranslate(), self.refresh()), now=False)
        self._update_state()

    # ---------------------------------------------------------------- size
    def _min_height(self, hint: QSize) -> int:
        lay = self.layout()
        return max(hint.height(), lay.minimumHeightForWidth(max(self.width(), hint.width())))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # the filter controls wrap (FlowLayout), so the least height is the layout's at the
        # width the panel has.  A dock area takes minimumSizeHint, never a height for a width:
        # at the window's minimum height the last control of the wrapped row was cut
        s = super().minimumSizeHint()
        self._min_h = self._min_height(s)
        return QSize(s.width(), self._min_h)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        if ev.size().width() != ev.oldSize().width() and \
                self._min_height(super().minimumSizeHint()) != getattr(self, "_min_h", -1):
            self.updateGeometry()           # the controls wrap differently at this width

    # ---------------------------------------------------------------- texts
    def retranslate(self) -> None:
        t = self.tr_.tr
        from .shell.keymap import native_text
        self.filter_edit.setPlaceholderText(native_text(t("partlist.filter.placeholder")))
        self.filter_edit.setAccessibleName(t("partlist.filter.name"))
        self.filter_edit.setToolTip(native_text(t("partlist.filter.tip")))
        self.layer_combo.setAccessibleName(t("partlist.filter.layer"))
        self.layer_combo.setToolTip(t("partlist.filter.layer"))
        self.warn_btn.setText(t("partlist.filter.warnings"))
        self.warn_btn.setAccessibleName(t("partlist.filter.warnings"))
        self.warn_btn.setToolTip(t("partlist.filter.warnings_tip"))
        self.show_btn.setText(t("partlist.show"))
        self.show_btn.setAccessibleName(t("partlist.show"))
        style.set_tooltip(self.show_btn, t("partlist.show_tip"))
        self.assign_btn.setText(t("partlist.assign_btn"))
        self.assign_btn.setAccessibleName(t("partlist.assign_btn"))
        style.set_tooltip(self.assign_btn, t("partlist.assign_tip"))
        self.clear_btn.setText(t("partlist.filter.clear"))
        self.view.setAccessibleName(t("partlist.title"))
        self.view.setAccessibleDescription(t("partlist.a11y.desc"))
        self.status.setAccessibleName(t("partlist.status"))
        self.model.retranslate()
        self._fill_layers()
        self._update_state()

    # ---------------------------------------------------------------- compatibility
    def part_bbox(self, p) -> tuple:
        """``p.bbox()``, cached (see :class:`PartBoxes`); the Properties panel uses it too."""
        return self.boxes.get(p)

    @property
    def bbox_hits(self) -> int:
        return self.boxes.hits

    @property
    def bbox_misses(self) -> int:
        return self.boxes.misses

    @property
    def bbox_moved(self) -> int:
        return self.boxes.shifts

    @property
    def mime_for_selection(self):
        return self.model.mime_fn

    @mime_for_selection.setter
    def mime_for_selection(self, fn) -> None:
        self.model.mime_fn = fn

    # ---------------------------------------------------------------- document
    def set_document(self, doc, changed=None) -> None:
        """Show ``doc``; ``changed`` as for ``CanvasView.rebuild`` (the uids of the parts an
        edit may have changed: a row whose part is the same object and not in it is skipped)."""
        if doc is not self.document:
            changed = None
        self.document = doc
        self.refresh(changed)

    def refresh(self, changed=None) -> None:
        self._sync = True
        try:
            self.model.update(self.document, changed)
            self._fill_layers()
            self._apply_selection(self._flags())
        finally:
            self._sync = False
        self._update_state()

    def _flags(self) -> list[bool]:
        return [p.selected for p in self.document.parts] if self.document is not None else []

    def _fill_layers(self) -> None:
        layers = sorted({r[1] for r in self.model.rows})
        have = [self.layer_combo.itemData(i) for i in range(self.layer_combo.count())]
        want = [None, *layers]
        if have == want:
            if self.layer_combo.count():
                self.layer_combo.setItemText(0, self.tr_.tr("partlist.filter.all_layers"))
            return
        cur = self.layer_combo.currentData()
        self._sync = True
        try:
            self.layer_combo.clear()
            self.layer_combo.addItem(self.tr_.tr("partlist.filter.all_layers"), None)
            for li in layers:
                self.layer_combo.addItem(self.model.swatch(li), self.tr_.format("partlist.layer_n", n=li), li)
            i = self.layer_combo.findData(cur) if cur is not None else 0
            self.layer_combo.setCurrentIndex(max(i, 0))
        finally:
            self._sync = False
        if cur is not None and self.layer_combo.currentData() is None:
            self.model.set_filter(layer=None)      # the layer is gone: show all again

    # ---------------------------------------------------------------- filter
    def _apply_filter(self) -> None:
        self.model.set_filter(text=self.filter_edit.text())

    def clear_filter(self) -> None:
        self._sync = True
        try:
            self.filter_edit.clear()
            self.layer_combo.setCurrentIndex(0)
            self.warn_btn.setChecked(False)
        finally:
            self._sync = False
        self._filter_timer.stop()
        self.model.set_filter(text="", layer=None, warnings=False)

    def focus_filter(self) -> None:
        self.filter_edit.setFocus(Qt.ShortcutFocusReason)
        self.filter_edit.selectAll()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        from PySide6.QtCore import QEvent
        if obj is self.filter_edit and ev.type() == QEvent.ShortcutOverride and ev.key() in _OWN_KEYS:
            ev.accept()                  # the field's keys, not the window's (see _OWN_KEYS)
            return True
        if obj is self.filter_edit and ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Escape and self.filter_edit.text():
                self.filter_edit.clear()
                return True
            if ev.key() in (Qt.Key_Down, Qt.Key_Return, Qt.Key_Enter) and self.model.rowCount():
                if self._filter_timer.isActive():
                    self._filter_timer.stop()
                    self._apply_filter()
                self.view.setFocus(Qt.TabFocusReason)
                if not self.view.currentIndex().isValid() and self.model.rowCount():
                    self.view.setCurrentIndex(self.model.index(0, 0))
                return True
        return super().eventFilter(obj, ev)

    def _after_relayout(self) -> None:
        if self._sync:
            return
        self._reselect()
        self._update_state()
        from . import a11y
        if self.model.filter.active:
            a11y.announce(self.view, self.status.text())

    def _reselect(self) -> None:
        if self.document is None:
            return
        self._sync = True
        try:
            self._apply_selection(self._flags())
        finally:
            self._sync = False

    # ---------------------------------------------------------------- selection
    def sync_selection(self) -> None:
        """The canvas selection changed: select the same rows and scroll the first into view."""
        if self.document is None or self._to_canvas.isActive():
            return
        flags = self._flags()
        if flags == self._selected:
            return
        self._sync = True
        try:
            self._apply_selection(flags)
        finally:
            self._sync = False
        rows = self.view.selectionModel().selectedRows()
        if rows:
            vis = self.view.viewport().rect()
            if not any(vis.intersects(self.view.visualRect(r)) for r in rows[:200]):
                self.view.scrollTo(min(rows, key=lambda ix: ix.row()), QAbstractItemView.PositionAtCenter)
        self._update_state()

    def _apply_selection(self, flags: list) -> None:
        """Select the rows whose part's flag is set, contiguous rows as one range, all in one
        ``QItemSelectionModel.select`` call (per-row selection cost grew with the selection:
        select-all of 10,000 parts took 222 s, review 2 §1.4, P1-8)."""
        self._selected = list(flags)
        model = self.model
        view = model.view
        n = len(flags)
        last = N_COLS - 1
        sel = QItemSelection()
        start = None
        for r, i in enumerate(view):
            on = i < n and flags[i]
            if on:
                if start is None:
                    start = r
            elif start is not None:
                sel.select(model.index(start, 0), model.index(r - 1, last))
                start = None
        if start is not None:
            sel.select(model.index(start, 0), model.index(len(view) - 1, last))
        self.view.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)

    def selected_parts(self) -> list[int]:
        """Part indices of the selected rows (visible rows only)."""
        view = self.model.view
        out = []
        for rng in self.view.selectionModel().selection():
            out.extend(view[r] for r in range(rng.top(), rng.bottom() + 1) if r < len(view))
        return sorted(set(out))

    def _view_selection_changed(self, *_a) -> None:
        if self._sync:
            return
        self._to_canvas.start()

    def _emit_selection(self) -> None:
        """The rows the user selected, plus the selected parts the filter hides (filtering never
        changes the selection of what it hides)."""
        if self.document is None:
            return
        shown = set(self.model.view)
        hidden_sel = [i for i, on in enumerate(self._flags()) if on and i not in shown] if self.model.filter.active else []
        idx = sorted(set(self.selected_parts()) | set(hidden_sel))
        self._selected = [False] * len(self.document.parts)
        for i in idx:
            if i < len(self._selected):
                self._selected[i] = True
        self._sync = True
        try:
            self.selectionToCanvas.emit(idx)
        finally:
            self._sync = False
        self._update_state()

    def flush(self) -> None:
        """Send a pending list selection to the canvas now (tests, before a command)."""
        if self._to_canvas.isActive():
            self._to_canvas.stop()
            self._emit_selection()

    def zoom_to_selection(self) -> None:
        self.flush()
        idx = self.selected_parts()
        if idx:
            self.zoomRequested.emit(idx)

    # ---------------------------------------------------------------- state
    def _update_state(self) -> None:
        total = len(self.model.rows)
        shown = len(self.model.view)
        nsel = sum(1 for f in self._selected if f)
        if total == 0:
            self.empty_text.setText(self.tr_.tr("partlist.empty"))
            self.clear_btn.hide()
            self.stack.setCurrentWidget(self.empty)
        elif shown == 0:
            self.empty_text.setText(self.tr_.tr("partlist.no_match"))
            self.clear_btn.show()
            self.stack.setCurrentWidget(self.empty)
        else:
            self.stack.setCurrentWidget(self.view)
        if total and shown != total:
            text = self.tr_.plural("partlist.count_filtered", total, shown=shown, selected=nsel)
        else:
            text = self.tr_.plural("partlist.count", total, selected=nsel)
        self.status.setText(text)
        nwarn = None
        if total:
            nwarn = sum(1 for i in range(total) if self.model.warnings(i))
        self.warn_btn.setEnabled(bool(nwarn) or self.warn_btn.isChecked())
        reason = self.tr_.tr("partlist.need_selection")
        style.set_enabled(self.show_btn, nsel > 0, reason)
        style.set_enabled(self.assign_btn, nsel > 0, reason)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        """About 40 characters wide (number, name, layer, warning): the dock it shares with
        Properties is no wider for it; the user can widen it, and the columns follow."""
        hint = super().sizeHint()
        want = max(self.fontMetrics().averageCharWidth() * 40, self.controls.minimumSizeHint().width())
        return QSize(want + 2 * style.SP2, hint.height())


# --------------------------------------------------------------------------- canvas
def zoom_canvas_to(canvas, rect: QRectF) -> None:
    """Show ``rect`` (scene units) on the canvas: zoomed to it, with a margin, never closer
    than 20 mm across (one small hole fills no screen)."""
    w, h = max(rect.width(), 20.0), max(rect.height(), 20.0)
    c = rect.center()
    r = QRectF(c.x() - w * 0.6, c.y() - h * 0.6, w * 1.2, h * 1.2)
    canvas.zoom(1.0)                 # the user's zoom from now on: a resize keeps it
    canvas.fitInView(r, Qt.KeepAspectRatio)


def parts_rect(panel: PartListPanel, doc, indices: Iterable[int]) -> Optional[QRectF]:
    boxes = [panel.part_bbox(doc.parts[i]) for i in indices if 0 <= i < len(doc.parts) and doc.parts[i].contours]
    if not boxes:
        return None
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    return QRectF(x0, y0, x1 - x0, y1 - y0)


# --------------------------------------------------------------------------- assign technology
@dataclass
class Assignment:
    """What *Assign technology…* will do: ``layer`` the selected parts go to; ``assign``: the
    layer is assigned the record (a new or the selection's own layer) or already follows it;
    ``move``: the parts change layer."""
    layer: int
    assign: bool
    move: bool


def _follows(lp, tech) -> bool:
    from mlaser.services import inheritance as I
    from mlaser.services import technology as T
    ref = T.layer_technology_ref(lp)
    if not ref or (lp.extra or {}).get("placeholder"):
        return False
    same = (ref.get("id") is not None and ref.get("id") == getattr(tech, "id", None)
            and (ref.get("version") or None) == (getattr(tech, "version", None) or None))
    return bool(same) and not I.layer_overrides(lp)


def plan_assignment(doc, selected: list, tech, max_layers: Optional[int] = None) -> Optional[Assignment]:
    """Where the ``selected`` parts go so that they are cut with ``tech`` (None: no layer free).

    1. the selection is exactly the parts of one layer: that layer is assigned ``tech``;
    2. a layer already follows ``tech`` (same record and version, nothing changed on the layer):
       the parts move there; a layer that parts use first;
    3. else the lowest layer no part uses (an undefined one first) is assigned ``tech`` and the
       parts move there."""
    from mlaser.formats.params import N_LAYERS
    limit = max_layers if max_layers is not None else N_LAYERS
    sel = set(id(p) for p in selected)
    sel_layers = {p.craft.layer for p in selected}
    used = {p.craft.layer for p in doc.parts}
    if len(sel_layers) == 1:
        li = next(iter(sel_layers))
        if all(id(p) in sel for p in doc.parts if p.craft.layer == li):
            return Assignment(li, assign=not _follows(doc.layers.get(li), tech), move=False)
    follows = [li for li, lp in sorted(doc.layers.items()) if _follows(lp, tech)]
    if follows:
        li = next((x for x in follows if x in used), follows[0])
        return Assignment(li, assign=False, move=True)
    free = [li for li in range(limit) if li not in used]
    undefined = [li for li in free if doc.layers.get(li) is None or (doc.layers[li].extra or {}).get("placeholder")]
    pick = (undefined or free or [None])[0]
    if pick is None:
        return None
    return Assignment(pick, assign=True, move=True)


def assign_technology(win, tech, parts: Optional[list] = None) -> Optional[str]:
    """Give the selected parts (``parts``: these) the technology ``tech`` as one undo step on
    the per-layer mechanism (:func:`plan_assignment`).  Returns the message shown, None when
    nothing was done."""
    from mlaser.services import inheritance as I
    from . import a11y
    tr = win.tr_
    doc = win.docs.document
    sel = parts if parts is not None else [p for p in doc.parts if p.selected]
    if not sel or tech is None:
        return None
    from . import safety_ui
    if bool(getattr(win, "safety_ui", None)) and safety_ui.job_locked(win, tr.tr("partlist.assign_btn")):
        return None
    plan = plan_assignment(doc, sel, tech)
    if plan is None:
        msg = tr.tr("partlist.assign.no_layer")
        win.statusBar().showMessage(msg, 8000)
        a11y.announce(win.part_list.view, msg)
        return None
    store = _store(win)
    ids = {id(p) for p in sel}

    def fn(d) -> None:
        if plan.move:
            for p in d.parts:
                if id(p) in ids:
                    p.craft.layer = plan.layer
        if plan.assign:
            I.assign_recipe(d, store, tech, [plan.layer])
        else:
            I.materialise(d, store, layers=[plan.layer])
    win.docs.edit(fn, "partlist.assign_tech")
    name = tech.key.label()
    if plan.move:
        msg = tr.plural("partlist.assign.moved", len(sel), layer=plan.layer, tech=name)
    else:
        msg = tr.plural("partlist.assign.layer", len(sel), layer=plan.layer, tech=name)
    win.statusBar().showMessage(msg, 10000)
    log = getattr(getattr(win, "log_panel", None), "log", None)
    if log is not None:
        log(msg)
    a11y.announce(win.part_list.view, msg)
    return msg


def _store(win):
    ext = win.extension("ext_process") if hasattr(win, "extension") else None
    return getattr(ext, "store", None)


def pick_technology(win) -> Any:
    """The record the user picks (a dialog with the recipe picker); None when cancelled."""
    store = _store(win)
    if store is None:
        return None
    from .workspaces.recipe_picker import RecipePicker
    tr = win.tr_
    n = sum(1 for p in win.docs.document.parts if p.selected)
    dlg = style.dialog(win, tr.tr("partlist.assign.title"),
                       explanation=tr.plural("partlist.assign.explain", n))
    picker = RecipePicker(tr, store, dlg)
    cur = _common_tech(win)
    if cur is not None:
        picker.pick(cur.key)
    dlg.body.addWidget(picker)
    ok = dlg.ok_button()
    if ok is not None:
        ok.setText(tr.tr("partlist.assign.ok"))
        picker.changed.connect(lambda: ok.setEnabled(picker.picked() is not None))
        ok.setEnabled(picker.picked() is not None)
    if not dlg.exec():
        return None
    return picker.picked()


def _common_tech(win) -> Any:
    from mlaser.services import inheritance as I
    doc = win.docs.document
    layers = {p.craft.layer for p in doc.parts if p.selected}
    if len(layers) != 1:
        return None
    return I.recipe_of(doc, next(iter(layers)), _store(win))


# --------------------------------------------------------------------------- window wiring
def make_dock(win) -> PartListPanel:
    """The Parts dock (tabbed with the Layer parameters dock).  Called by MainWindow while it
    builds its docks; :func:`install` wires it once the commands and the canvas exist."""
    from PySide6.QtWidgets import QDockWidget
    panel = PartListPanel(win.tr_)
    dock = QDockWidget(win)
    dock.setObjectName("parts")
    dock.setWidget(panel)
    dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
    win.tr_.bind(dock, "partlist.title", dock.setWindowTitle)
    win.addDockWidget(Qt.LeftDockWidgetArea, dock)
    win.tabifyDockWidget(win.left_dock, dock)
    win.parts_dock = dock
    return panel


def install(win) -> None:
    """The commands of the part list (Find part: Ctrl/⌘+F, Show the part list, Assign
    technology…) and its wiring with the canvas.  Called by MainWindow after its own commands."""
    panel, dock = win.part_list, win.parts_dock
    panel.model.order_fn = lambda: win.canvas.contour_order()

    def zoom(idx: list) -> None:
        r = parts_rect(panel, win.docs.document, idx)
        if r is not None:
            zoom_canvas_to(win.canvas, r)
    panel.zoomRequested.connect(zoom)
    panel.assignRequested.connect(lambda: win.run("partlist.assign_tech"))

    def show_list() -> bool:
        """Show the Parts dock (in Design or Process); False elsewhere."""
        shell = getattr(win, "shell", None)
        cur = shell.host.current if shell is not None else "design"
        if cur not in ("design", "process"):
            win.statusBar().showMessage(win.tr_.tr("partlist.only_design_process"), 5000)
            return False
        dock.show()
        dock.raise_()
        return True

    def find() -> None:
        if show_list():
            panel.focus_filter()

    def assign() -> None:
        if win._operator_blocked():
            return
        panel.flush()
        if not any(p.selected for p in win.docs.document.parts):
            win.statusBar().showMessage(win.tr_.tr("partlist.need_selection"), 5000)
            return
        tech = getattr(win, "_test_assign_tech", None)
        if tech is None:
            if getattr(win, "_test_no_dialogs", False):
                return
            tech = pick_technology(win)
        if tech is not None:
            assign_technology(win, tech)

    win.commands.register("partlist.find", find, "core", "partlist.cmd.find", workspace="design")
    win.commands.register("partlist.show", lambda: show_list(), "core", "partlist.cmd.show", workspace="design")
    win.commands.register("partlist.assign_tech", assign, "core", "partlist.cmd.assign", workspace="process")


__all__ = ["PartListModel", "PartListPanel", "PartBoxes", "PartFilter", "Assignment", "plan_assignment",
           "assign_technology", "make_dock", "install", "zoom_canvas_to"]
