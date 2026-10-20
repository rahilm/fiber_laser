"""Side panels: layer parameters, part list, machine control panel, log/alarm dock."""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.model import LayerParams

from .canvas import LAYER_COLORS, layer_color
from ..machine.api import MachineAPI
from . import homes
from .i18n import Translator
from .params import GAS_KEYS
from .icons import command_icon
from .responsive import (TARGET_MIN, CollapsibleSection, ColumnFitter, ColumnsPanel, ResponsiveForm, flow)

PAD_ICON = 28       # px: the touch icon size (jog pad, run buttons)
JOG_KEYS = ("MC.IsStepMove", "MC.IsFastMode", "MC.StepLength")


def _swatch(color: QColor, size: int = 14) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(color)
    return QIcon(pm)


# --------------------------------------------------------------------------- layers
class LayerPanel(QWidget):
    """Layer list + colour buttons (lp0 "Layer Parameters", gp83 "Layer").

    Row of 11 colour buttons assigns the selected parts to a layer (inferred from the 11
    layer records in MainApp FUN_004473c0). The table edits the core cut parameters of the
    layers in use; "lp0" opens the full GP.* property grid.
    """
    assignLayer = Signal(int)
    editLayer = Signal(int)
    changed = Signal()

    COLS = [("gp83", None), ("pd125", "speed"), ("pd127", "power_pct"), ("pd128", "frequency_hz"),
            ("pd130", "gas"), ("pd131", "gas_pressure"), ("pd129", "cut_height")]

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.document = None
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        from .techform import FlowLayout
        roww = QWidget()
        row = FlowLayout(roww, 1)                # the swatches wrap in a narrow dock
        self.color_buttons = []
        for i in range(len(LAYER_COLORS)):
            b = QToolButton()
            b.setIcon(_swatch(layer_color(i), 16))
            b.setText(str(i))
            b.setToolTip(f"{tr.tr('gp83')} {i}")
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.clicked.connect(lambda _=False, i=i: self.assignLayer.emit(i))
            row.addWidget(b)
            self.color_buttons.append(b)
        v.addWidget(roww)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # no sideways scrolling (DESIGN.md R2): the layer column stretches and elides, the
        # others are shown in priority order (COLS order) while they fit; the header's context
        # menu shows a hidden one again; every value is also in the layer cell's tooltip
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setStretchLastSection(False)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setWordWrap(False)
        self.pinned: set[int] = set()            # columns the user asked to show
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(lambda r, c: c == 0 and self.editLayer.emit(self._row_layer(r)))
        v.addWidget(self.table)
        btns = QHBoxLayout()
        self.btn_params = QPushButton()
        tr.bind(self.btn_params, "lp0", self.btn_params.setText)
        self.btn_params.clicked.connect(self._edit_current)
        btns.addWidget(self.btn_params)
        btns.addStretch(1)
        v.addLayout(btns)
        tr.bind(self, "gp83", lambda _t: self._headers())

    def _headers(self):
        self.table.setHorizontalHeaderLabels([self.tr_.tr_param(k)[1] for k, _ in self.COLS])
        self.fit_columns()

    def fit_columns(self) -> None:
        """Show the value columns, in priority order, while they fit next to the layer column."""
        tb = self.table
        avail = tb.viewport().width() if tb.isVisible() else tb.width()
        fm = tb.fontMetrics()
        hh = tb.horizontalHeader()
        need0 = max([fm.horizontalAdvance(hh.model().headerData(0, Qt.Horizontal) or "") + 24] +
                    [fm.horizontalAdvance(tb.item(r, 0).text()) + 36 for r in range(tb.rowCount())
                     if tb.item(r, 0) is not None])
        used = need0
        for c in range(1, tb.columnCount()):
            w = max(fm.horizontalAdvance(str(hh.model().headerData(c, Qt.Horizontal) or "")) + 16,
                    tb.sizeHintForColumn(c) + 8)
            show = c in self.pinned or used + w <= avail
            tb.setColumnHidden(c, not show)
            if show:
                tb.setColumnWidth(c, w)
                used += w

    def resizeEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().resizeEvent(ev)
        self.fit_columns()

    def _header_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        hh = self.table.horizontalHeader()
        for c in range(1, self.table.columnCount()):
            a = m.addAction(str(hh.model().headerData(c, Qt.Horizontal)))
            a.setCheckable(True)
            a.setChecked(not self.table.isColumnHidden(c))
            a.toggled.connect(lambda on, c=c: (self.pinned.add(c) if on else self.pinned.discard(c),
                                               self.fit_columns()))
        m.popup(hh.mapToGlobal(pos))

    def _row_layer(self, r: int) -> int:
        it = self.table.item(r, 0)
        return int(it.data(Qt.UserRole)) if it else 1

    def _edit_current(self):
        r = self.table.currentRow()
        if r >= 0:
            self.editLayer.emit(self._row_layer(r))

    def set_document(self, doc) -> None:
        self.document = doc
        self.refresh()

    def refresh(self) -> None:
        doc = self.document
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        if doc is not None:
            used = sorted({p.craft.layer for p in doc.parts} | set(doc.layers))
            for li in used:
                lp = doc.layers.get(li)
                if lp is None:
                    # shown with defaults, but "undefined" for the pre-flight check until edited
                    lp = doc.layers[li] = LayerParams(index=li, color=layer_color(li).name(),
                                                      extra={"placeholder": True})
                r = self.table.rowCount()
                self.table.insertRow(r)
                head = QTableWidgetItem(_swatch(layer_color(li)), f"{self.tr_.tr('gp83')} {li}")
                head.setData(Qt.UserRole, li)
                head.setFlags(head.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, 0, head)
                tips = []
                for c, (_, attr) in enumerate(self.COLS[1:], start=1):
                    val = getattr(lp, attr)
                    txt = self.tr_.tr(GAS_KEYS[int(val) % len(GAS_KEYS)]) if attr == "gas" else f"{val:g}"
                    it = QTableWidgetItem(txt)
                    if attr == "gas":
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(r, c, it)
                    tips.append(f"{self.tr_.tr_param(self.COLS[c][0])[1]}: {txt}")
                head.setToolTip("\n".join(tips))
        self.table.blockSignals(False)
        self._headers()

    def _item_changed(self, it: QTableWidgetItem) -> None:
        if self.document is None or it.column() == 0:
            return
        li = self._row_layer(it.row())
        attr = self.COLS[it.column()][1]
        try:
            lp = self.document.layers[li]
            setattr(lp, attr, float(it.text()))
            lp.extra.pop("placeholder", None)        # the operator set it: the layer is defined
            self.changed.emit()
        except (ValueError, KeyError):
            pass


# --------------------------------------------------------------------------- parts
class PartListPanel(QTreeWidget):
    """Part list (np_Part / np_PartName): index, name, layer, contour count, size, closed.

    The size column needs every part's bounding box, which flattens every contour (the bulk of
    a refresh: ~250 ms of ~300 ms at 2,000 parts).  The boxes are cached by part ``uid`` and
    validated by the identity of the part's segment objects (edits replace segment lists and
    segment objects, never mutate them in place: ``ops.document.transform_parts``,
    ``cad.edit``), so an edit re-measures only the parts it changed.  The tree items are reused
    and only changed texts are set."""
    selectionToCanvas = Signal(list)

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.document = None
        self.setColumnCount(5)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._sync = False
        self._bbox_cache: dict[int, tuple[tuple, tuple]] = {}   # uid -> (segments, bbox)
        self._swatches: dict[int, QIcon] = {}
        self.bbox_hits = self.bbox_misses = 0
        self.itemSelectionChanged.connect(self._sel_changed)
        tr.bind(self, "np_Part", lambda _t: self._headers())
        ColumnFitter(self, {0: 1, 2: 3, 3: 2, 4: 0}, stretch=1)

    def _headers(self):
        t = self.tr_
        self.setHeaderLabels([t.tr("gp75"), t.tr("np_PartName"), t.tr("gp83"), t.tr("gp76"), t.tr("sf1").strip(" :：")])

    def set_document(self, doc) -> None:
        self.document = doc
        self.refresh()

    def part_bbox(self, p) -> tuple:
        """``p.bbox()``, cached by uid while the part's segment objects are the same."""
        segs = tuple(s for c in p.contours for s in c.segments)
        hit = self._bbox_cache.get(p.uid)
        if hit is not None and len(hit[0]) == len(segs) and all(a is b for a, b in zip(hit[0], segs)):
            self.bbox_hits += 1
            return hit[1]
        self.bbox_misses += 1
        bb = p.bbox()
        self._bbox_cache[p.uid] = (segs, bb)
        return bb

    def _swatch(self, layer: int) -> QIcon:
        ic = self._swatches.get(layer)
        if ic is None:
            ic = self._swatches[layer] = _swatch(layer_color(layer))
        return ic

    def refresh(self) -> None:
        self._sync = True
        doc = self.document
        parts = list(doc.parts) if doc is not None else []
        n_items = self.topLevelItemCount()
        if n_items > len(parts):
            for i in range(n_items - 1, len(parts) - 1, -1):
                self.takeTopLevelItem(i)
        part_word = self.tr_.tr("np_Part")
        red = QColor("#d00000")
        live = set()
        new_items = []
        for i, p in enumerate(parts):
            live.add(p.uid)
            x0, y0, x1, y1 = self.part_bbox(p)
            vals = (str(i + 1), p.name or f"{part_word}{i + 1}", str(p.craft.layer), str(len(p.contours)),
                    f"{x1 - x0:.2f}×{y1 - y0:.2f}")
            is_open = any(not c.closed for c in p.contours)
            if i < n_items:
                it = self.topLevelItem(i)
                for col, v in enumerate(vals):
                    if it.text(col) != v:
                        it.setText(col, v)
            else:
                it = QTreeWidgetItem(list(vals))
                it.setData(0, Qt.UserRole, i)
                new_items.append(it)
            if it.data(2, Qt.UserRole) != p.craft.layer:
                it.setIcon(2, self._swatch(p.craft.layer))
                it.setData(2, Qt.UserRole, p.craft.layer)
            if bool(it.data(1, Qt.UserRole)) != is_open:
                it.setData(1, Qt.UserRole, is_open)
                if is_open:
                    it.setForeground(1, red)
                else:
                    it.setData(1, Qt.ForegroundRole, None)
        if new_items:
            self.addTopLevelItems(new_items)
        self._apply_selection([p.selected for p in parts])
        if len(self._bbox_cache) > 2 * max(len(live), 64):
            self._bbox_cache = {u: v for u, v in self._bbox_cache.items() if u in live}
        self._sync = False

    def sync_selection(self) -> None:
        if self.document is None:
            return
        self._sync = True
        try:
            parts = self.document.parts
            n = min(self.topLevelItemCount(), len(parts))
            self._apply_selection([parts[i].selected for i in range(n)])
        finally:
            self._sync = False

    def _apply_selection(self, flags: list) -> None:
        """Select the rows whose flag is set, all in one ``QItemSelectionModel.select`` call
        (contiguous rows as one range).

        ``QTreeWidgetItem.setSelected`` per row went through the selection model once per item
        and its cost grew with the selection: select-all of 10,000 parts took 222 s (review 2
        §1.4, P1-8).  The rows are item ``i`` = part ``i`` (``refresh`` reuses items by
        position); rows beyond ``flags`` are deselected."""
        model = self.model()
        n = min(len(flags), self.topLevelItemCount())
        last_col = self.columnCount() - 1
        sel = QItemSelection()
        start = None
        for i in range(n):
            if flags[i]:
                if start is None:
                    start = i
            elif start is not None:
                sel.select(model.index(start, 0), model.index(i - 1, last_col))
                start = None
        if start is not None:
            sel.select(model.index(start, 0), model.index(n - 1, last_col))
        self.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)

    def _sel_changed(self):
        if self._sync:
            return
        self.selectionToCanvas.emit([it.data(0, Qt.UserRole) for it in self.selectedItems()])


# --------------------------------------------------------------------------- control
class ControlPanel(QWidget):
    """Machine control (mf141 "Run Control"); see facade.py for the lang keys of each button.

    Jog buttons (X/Y arrows, Z up/down) and the start/pause/stop buttons carry SVG icons
    (:mod:`mlaser.ui.icons`, ``jog_*`` and ``run.*``); the other buttons are text buttons, as
    the original's were labelled bitmaps.
    """
    simulateRequested = Signal()
    log = Signal(str)

    JOG_KEEPALIVE_MS = 100       # hold-to-jog: keep-alive while a jog button is held
    STEP_KEYS = ["mp90", "mp91", "mp92", "mp93", "mp94", "mp95", "mp96", "mp97", "mp98", "mp99"]

    def __init__(self, tr: Translator, controller: MachineAPI,
                 get_document: Callable[[], object], parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.ctl = controller
        self.get_document = get_document
        self.buttons: dict[str, QAbstractButton] = {}
        self.run_hooks = None            # ui.safety_ui.SafetyUI: Start/Hold/Stop with pre-flight
        self._held: set[str] = set()     # axes of held continuous-jog buttons
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(self.JOG_KEEPALIVE_MS)
        self._keepalive.timeout.connect(self._jog_keepalive)
        # Responsive layout (docs/dev/RESPONSIVE.md): the sections Jog, DO, Path and Coordinate in
        # 1..3 columns as the dock's width allows; when the dock is too short the panel turns
        # compact and then folds the sections of lowest priority (Coordinate first, Jog last).
        # Start / Hold / STOP / Frame / Dry run are the run bar's (always on screen, never a
        # second copy here: WORK_ITEMS R-4, UI audit D-2).  One rule for icons and text: the jog
        # pad is icon-only (direction and meaning in the tooltip and accessible name; every pad
        # button the same touch size), every other action is a text button.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.columns = ColumnsPanel(max_columns=3)
        self.columns.setObjectName("control_columns")
        outer.addWidget(self.columns)
        self.config = None               # ConfigService (bind_config): jog step / fast, coordinates

        # --- jog
        jog = QWidget()
        jv = QVBoxLayout(jog)
        jv.setContentsMargins(0, 0, 0, 0)
        pad = QWidget()
        pad.setObjectName("jog_pad")
        g = QGridLayout(pad)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(4)
        for (axis, d, r, c) in (("Y", 1, 0, 1), ("Y", -1, 2, 1), ("X", -1, 1, 0),
                                ("X", 1, 1, 2), ("Z", 1, 0, 3), ("Z", -1, 2, 3)):
            name = f"jog_{axis}{'+' if d > 0 else '-'}"
            b = self._pad_button(name, name, f"Jog {axis}{'+' if d > 0 else '-'}")
            b.setAutoRepeat(False)
            b.pressed.connect(lambda a=axis, d=d: self._jog_press(a, d))
            b.released.connect(lambda a=axis: self._jog_release(a))
            g.addWidget(b, r, c)
        # mp29 回零位 = move to the coordinate zero (FUN_005bc220), in the centre of the pad;
        # mp30 = home Z, between Z+ and Z-; mp29_1 系统回原 = Go Origin (machine homing, with the
        # mf156 prompt, FUN_005b5de0) is a text action below the pad
        g.addWidget(self._pad_button("mp29", "jog_zero", "", self._return_zero), 1, 1)
        g.addWidget(self._pad_button("mp30", "jog_home_z", "", lambda: self.ctl.home("Z")), 1, 3)
        g.setColumnMinimumWidth(3, 0)
        prow = QHBoxLayout()
        prow.addStretch(1)
        prow.addWidget(pad)
        prow.addStretch(1)
        jv.addLayout(prow)
        self.mode_step = QCheckBox()
        tr.bind(self.mode_step, "mp31", self.mode_step.setText)
        self.mode_fast = QCheckBox()
        tr.bind(self.mode_fast, "mp32", self.mode_fast.setText)
        self.step = QComboBox()
        for k in self.STEP_KEYS:
            self.step.addItem(tr.tr(k))
        self.step.setCurrentIndex(3)
        self.step.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        tr.bind(self.step, "mp31", self.step.setAccessibleName)
        # the jog values' home is the Machine workspace's jog pad; these are quick switches on
        # the same persisted keys (homes.SHORTCUT), like every other jog pad
        for wdg, vid in ((self.mode_step, "cfg:MC.IsStepMove"), (self.mode_fast, "cfg:MC.IsFastMode"),
                         (self.step, "cfg:MC.StepLength")):
            wdg.setProperty(homes.PROP_VALUE, vid)
            wdg.setProperty(homes.PROP_ROLE, homes.SHORTCUT)
        jv.addWidget(flow([self.mode_step, self.mode_fast, self.step], justify=False))
        # mark points (标记点1..6 = MC.MarkPtIndex, 标记 mp39 FUN_005c9390, 返回 af10 FUN_005c97f0)
        self.mark_index = QComboBox()
        for k in ("mp33", "mp34", "mp35", "mp36", "mp37", "mp38"):
            self.mark_index.addItem(tr.tr(k))
        self.mark_index.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        homes.bind_transient(self.mark_index, "run.mark_point")      # which mark Mark / Return use
        self.mark_index.currentIndexChanged.connect(self._mark_index_changed)
        jv.addWidget(flow([self._text_button("mp29_1", self._sys_home), self.mark_index,
                           self._text_button("mp39", self._set_mark), self._text_button("af10", self._return_mark)]))
        self.sec_jog = self._section("slp17", jog, priority=3)

        # --- outputs
        io = QWidget()
        iv = QVBoxLayout(io)
        iv.setContentsMargins(0, 0, 0, 0)
        outs = []
        for name, key in (("follow", "mp41"), ("gas", "mp42"), ("shutter", "mp49"),
                          ("red_light", "mp50"), ("laser", "mp51")):
            if name == "laser":
                # 激光 is a push button (CManuPanel +0x2153, FUN_005ad9f0): press = laser on
                # (FUN_005b05c0), release = laser off when MC.PtLaserTime_ms is 0 (FUN_005b0630)
                b = self._text_button(key, None)
                b.pressed.connect(lambda: self.ctl.set_output("laser", True))
                b.released.connect(lambda: self.ctl.set_output("laser", False))
            else:
                b = self._text_button(key, None, checkable=True)
                b.toggled.connect(lambda on, nm=name: self.ctl.set_output(nm, on))
            self.buttons[f"out_{name}"] = b
            outs.append(b)
        iv.addWidget(flow(outs, uniform=True))
        self.gas_type = QComboBox()
        for k in GAS_KEYS:
            self.gas_type.addItem(tr.tr(k))
        self.gas_type.currentIndexChanged.connect(lambda i: self.ctl.set_gas_type(i))
        homes.bind_transient(self.gas_type, "run.manual_gas")        # the gas the Gas button opens
        self.power = QSpinBox()
        self.power.setRange(0, 100)
        self.power.setValue(10)
        self.power.setSuffix(" %")
        self.power.valueChanged.connect(lambda p: self.ctl.set_laser_power(float(p)))
        homes.bind_transient(self.power, "run.manual_power")         # the Laser button's power
        form = ResponsiveForm()
        lbl_gas = QLabel()
        tr.bind(lbl_gas, "mp42", lbl_gas.setText)
        form.addRow(lbl_gas, self.gas_type)
        lbl = QLabel()
        tr.bind(lbl, "mp53", lbl.setText)
        form.addRow(lbl, self.power)
        iv.addWidget(form)
        self.sec_io = self._section("ss4", io, priority=2)

        # --- path: simulate and step along the job (the run itself is the run bar's)
        proc = QWidget()
        pv = QVBoxLayout(proc)
        pv.setContentsMargins(0, 0, 0, 0)
        self.bf_len = QDoubleSpinBox()
        self.bf_len.setRange(0.1, 10000)
        self.bf_len.setValue(10.0)
        tr.bind(self.bf_len, "mp62", self.bf_len.setAccessibleName)
        homes.bind_transient(self.bf_len, "run.step_length")
        pv.addWidget(flow([self._text_button("mp59", self.simulateRequested.emit),
                           self._text_button("mp61", lambda: self.ctl.step_backward(self.bf_len.value())),
                           self._text_button("mp62", lambda: self.ctl.step_forward(self.bf_len.value())),
                           self.bf_len], uniform=True))
        self.sec_run = self._section("mf141", proc, priority=1)

        # --- coordinate system (shown; chosen in Machine › Homing and jog) and its actions
        coord = QWidget()
        cv = QVBoxLayout(coord)
        cv.setContentsMargins(0, 0, 0, 0)
        self.coord_sys = QLabel()
        self.coord_sys.setObjectName("control_coord_sys")
        self.coord_sys.setWordWrap(True)
        homes.bind_mirror(self.coord_sys, "cfg:MSC.CoordSysType", tr)
        cv.addWidget(self.coord_sys)
        cv.addWidget(flow([self._text_button("mf514", self.ctl.set_origin),
                           self._text_button("mp164", self.ctl.go_origin_point),
                           self._text_button("mf21", self.ctl.clear_alarms)], uniform=True))
        self.sec_coord = self._section("mf307", coord, priority=0)
        self._show_coord()

    # -- configuration (one jog step / fast for every jog pad; the coordinate system)
    def bind_config(self, config) -> None:
        """Read and write the persisted jog values (``MC.IsStepMove``, ``MC.StepLength``,
        ``MC.IsFastMode``: the same values every jog pad shows, WORK_ITEMS R-3) and show the
        coordinate system; follows changes made elsewhere."""
        self.config = config
        self._jog_sync()
        self.mode_step.toggled.connect(lambda on: self._jog_set("MC.IsStepMove", bool(on)))
        self.mode_fast.toggled.connect(lambda on: self._jog_set("MC.IsFastMode", bool(on)))
        self.step.currentIndexChanged.connect(lambda _i: self._jog_set("MC.StepLength", self._step_mm()))
        config.subscribe(self._config_changed)
        self._show_coord()

    def _config_changed(self, key: str) -> None:
        if key in JOG_KEYS:
            self._jog_sync()
        elif key == "MSC.CoordSysType":
            self._show_coord()

    def _step_mm(self) -> float:
        try:
            return float(self.tr_.tr(self.STEP_KEYS[self.step.currentIndex()]))
        except (ValueError, IndexError):
            return 1.0

    def _jog_set(self, key: str, value) -> None:
        if self.config is not None and not self._jog_syncing and self.config.get(key) != value:
            try:
                self.config.set(key, value)
            except ValueError:
                pass

    _jog_syncing = False

    def _jog_sync(self) -> None:
        if self.config is None:
            return
        self._jog_syncing = True
        try:
            self.mode_step.setChecked(bool(self.config.get("MC.IsStepMove", False)))
            self.mode_fast.setChecked(bool(self.config.get("MC.IsFastMode", False)))
            mm = float(self.config.get("MC.StepLength", 1.0) or 1.0)
            vals = []
            for k in self.STEP_KEYS:
                try:
                    vals.append(float(self.tr_.tr(k)))
                except ValueError:
                    vals.append(float("nan"))
            best = min(range(len(vals)), key=lambda i: abs(vals[i] - mm) if vals[i] == vals[i] else 1e18)
            self.step.setCurrentIndex(best)
        finally:
            self._jog_syncing = False

    def _show_coord(self) -> None:
        from mlaser.controller import coords as C
        kind = 0
        if self.config is not None:
            try:
                kind = int(float(self.config.get("MSC.CoordSysType", 0) or 0))
            except (TypeError, ValueError):
                kind = 0
        name = self.tr_.tr(C.NAMES[kind]) if 0 <= kind < len(C.NAMES) else str(kind)
        self.coord_sys.setText(name)

    # -- machine actions
    machine_ops = None       # controller.machine_ops.MachineOps, set by ui.ext_machine.register
    confirm_fn: Optional[Callable[[str], bool]] = None   # tests / scripting: answers the prompts

    DONT_ASK_KEYS = ("mp119",)        # start-up go-origin prompt: "don't ask again" offered
    settings: Optional[dict] = None   # where "don't ask again" answers persist (window.settings)
    _confirmed_at: tuple = ("", 0.0)

    def confirm(self, key: str) -> bool:
        """OK/Cancel prompt with the text of lang key ``key`` (BCGPMessageBox 0x1001), as a real
        dialog.  The original asks twice at start-up (mp119 then mf156, both "go origin?"): a
        yes to mp119 answers the mf156 that follows at once."""
        if self.confirm_fn is not None:
            return bool(self.confirm_fn(key))
        last, t = self._confirmed_at
        if key == "mf156" and last == "mp119" and time.monotonic() - t < 2.0:
            self._confirmed_at = ("", 0.0)
            return True
        from .safety_ui import ask
        ok = ask(self, key, self.tr_.tr(key), dont_ask=key in self.DONT_ASK_KEYS, settings=self.settings)
        if ok:
            self._confirmed_at = (key, time.monotonic())
        return ok

    def _return_zero(self) -> None:
        self.ctl.return_zero(confirm=self.confirm)

    def _sys_home(self) -> None:
        if self.confirm("mf156"):
            self.ctl.home("XY")

    def _mark_index_changed(self, i: int) -> None:
        if self.machine_ops is not None:
            self.machine_ops.set_mark_index(i)

    def _set_mark(self) -> None:
        if self.machine_ops is not None:
            self.machine_ops.set_mark(self.mark_index.currentIndex())
        else:
            self.log.emit("mp39: mark points need the machine extension")

    def _return_mark(self) -> None:
        if self.machine_ops is not None:
            self.machine_ops.return_mark(self.mark_index.currentIndex())
        else:
            self.log.emit("af10: mark points need the machine extension")

    # -- helpers
    def _section(self, key: str, body: QWidget, priority: int) -> CollapsibleSection:
        sec = CollapsibleSection("", body, collapsible=True, priority=priority)
        sec.setObjectName(f"section_{key}")
        self.tr_.bind(sec, key, sec.setTitle)
        self.columns.add_section(sec)
        return sec

    def _text_button(self, key: str, fn: Optional[Callable], checkable: bool = False) -> QPushButton:
        b = QPushButton()
        self.tr_.bind(b, key, b.setText)
        b.setCheckable(checkable)
        if fn is not None:
            b.clicked.connect(lambda _=False: fn())
        b.setObjectName(key)
        self.buttons[key] = b
        return b

    def _pad_button(self, name: str, icon_cmd: str, label: str, fn: Optional[Callable] = None) -> QToolButton:
        """A jog-pad button: icon only, its meaning in the tooltip and accessible name, the same
        touch-sized square as every other pad button."""
        b = QToolButton()
        b.setObjectName(name)
        b.setIcon(command_icon(icon_cmd))
        b.setIconSize(QSize(PAD_ICON, PAD_ICON))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setMinimumSize(TARGET_MIN, TARGET_MIN)
        b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        b.setFocusPolicy(Qt.StrongFocus)

        def text(t: str, b=b) -> None:
            b.setText(t)
            b.setToolTip(t)
            b.setAccessibleName(t)
        if label:
            text(label)
        else:
            self.tr_.bind(b, name, text)
        if fn is not None:
            b.clicked.connect(lambda _=False: fn())
        self.buttons[name] = b
        return b

    def _with_doc(self, fn) -> None:
        doc = self.get_document()
        if doc is not None:
            fn(doc)

    def _start(self) -> None:
        if self.run_hooks is not None:
            self.run_hooks.start()
            return
        st = self.ctl.state()
        if st.mode == "pause":
            from .resume import resume
            resume(self.ctl, "exact", self.get_document())
            return
        doc = self.get_document()
        if doc is not None:
            self.ctl.start(doc)

    def _pause(self) -> None:
        if self.run_hooks is not None:
            self.run_hooks.hold()
            return
        if self.ctl.state().mode == "pause":
            from .resume import resume
            resume(self.ctl, "exact", self.get_document())
        else:
            self.ctl.pause()

    def _stop(self) -> None:
        self.release_jogs("stop")
        if self.run_hooks is not None:
            self.run_hooks.stop()
        else:
            self.ctl.stop()

    def _frame(self) -> None:
        if self.run_hooks is not None:
            self.run_hooks.frame()
        else:
            self._with_doc(self.ctl.frame)

    def _dry_run(self) -> None:
        if self.run_hooks is not None:
            self.run_hooks.dry_run()
        else:
            self._with_doc(self.ctl.dry_run)

    # -- safety state of the panel
    def safety_refresh(self, st, ms) -> None:
        """Called by the safety UI timer: the laser fire button only when armed, the output
        buttons show what the card reports (a refused output does not stay "on")."""
        armed = bool(getattr(st, "armed", False))
        b = self.buttons.get("out_laser")
        if b is not None:
            b.setEnabled(armed)
            b.setStyleSheet("QPushButton{background:#c62828;color:white;font-weight:bold;}" if armed else "")
            b.setToolTip("Fires the laser while held" if armed else "Arm the machine to fire the laser")
        if ms is not None:
            for name in ("gas", "shutter", "red_light", "follow"):
                bt = self.buttons.get(f"out_{name}")
                if bt is not None and bt.isCheckable():
                    on = bool(ms.outputs.get(name, False))
                    if bt.isChecked() != on:
                        bt.blockSignals(True)
                        bt.setChecked(on)
                        bt.blockSignals(False)

    def _jog_press(self, axis: str, d: int) -> None:
        if self.mode_step.isChecked():
            try:
                step = float(self.tr_.tr(self.STEP_KEYS[self.step.currentIndex()]))
            except ValueError:
                step = 1.0
            # the step jog uses the fast/slow speed too (FUN_005bdae0)
            self.ctl.jog_step(axis, d, step, fast=self.mode_fast.isChecked())
        else:
            self._held.add(axis)
            self.ctl.jog_start(axis, d, fast=self.mode_fast.isChecked())
            self._keepalive.start()

    def _jog_keepalive(self) -> None:
        if not self._held:
            self._keepalive.stop()
            return
        self.ctl.jog_keepalive()

    def _jog_release(self, axis: str) -> None:
        was = axis in self._held
        self._held.discard(axis)
        if not self._held:
            self._keepalive.stop()
        if was or not self.mode_step.isChecked():
            self.ctl.jog_stop(axis)

    def release_jogs(self, reason: str = "") -> None:
        """Dead-man: stop every held jog (focus loss, Stop, window hidden)."""
        if not self._held:
            return
        held, self._held = set(self._held), set()
        self._keepalive.stop()
        for axis in held:
            self.ctl.jog_stop(axis)
        if reason:
            self.log.emit(f"jog stopped ({reason})")

    def hideEvent(self, ev) -> None:  # noqa: N802 - Qt API
        self.release_jogs("panel hidden")
        super().hideEvent(ev)


# --------------------------------------------------------------------------- log
class LogPanel(QTabWidget):
    """mf124 "Log" + mf123 "Alarm" tabs (bottom dock)."""

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        self.alarms = QTableWidget(0, 2)
        self.alarms.horizontalHeader().setStretchLastSection(True)
        self.alarms.verticalHeader().hide()
        self.addTab(self.text, "")
        self.addTab(self.alarms, "")
        tr.bind(self, "mf124", lambda t: self.setTabText(0, t))
        tr.bind(self, "mf123", lambda t: self.setTabText(1, t))
        tr.bind(self.alarms, "gp84", lambda _t: self.alarms.setHorizontalHeaderLabels([tr.tr("gp84"), tr.tr("gp85")]))

    def sizeHint(self):  # keep the bottom dock compact by default
        from PySide6.QtCore import QSize
        return QSize(800, 140)

    def log(self, msg: str) -> None:
        key = msg.split(":", 1)[0]
        # "<key>" -> translation; "<key>: detail" -> "<translation> (detail)". Templates with
        # placeholders are formatted by the caller, never shown raw.
        if " " not in msg and self.tr_.has(msg):
            msg = self.tr_.tr(msg)
        elif self.tr_.has(key) and ":" in msg and "%" not in self.tr_.tr(key) and "{" not in self.tr_.tr(key):
            msg = f"{self.tr_.tr(key)} ({msg.split(':', 1)[1].strip()})"
        self.text.appendPlainText(time.strftime("%H:%M:%S  ") + msg)

    def add_alarm(self, text: str) -> None:
        r = self.alarms.rowCount()
        self.alarms.insertRow(r)
        self.alarms.setItem(r, 0, QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M:%S")))
        self.alarms.setItem(r, 1, QTableWidgetItem(self.tr_.tr(text)))
