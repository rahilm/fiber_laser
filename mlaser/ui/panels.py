"""Side panels: layer parameters, part list, machine control panel, log/alarm dock."""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.model import LayerParams

from .canvas import LAYER_COLORS, layer_color
from ..machine.api import ControllerFacade
from .i18n import Translator
from .params import GAS_KEYS
from .icons import command_icon


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
        row = QHBoxLayout()
        row.setSpacing(1)
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
        row.addStretch(1)
        v.addLayout(row)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
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
                    lp = doc.layers[li] = LayerParams(index=li, color=layer_color(li).name())
                r = self.table.rowCount()
                self.table.insertRow(r)
                head = QTableWidgetItem(_swatch(layer_color(li)), f"{self.tr_.tr('gp83')} {li}")
                head.setData(Qt.UserRole, li)
                head.setFlags(head.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, 0, head)
                for c, (_, attr) in enumerate(self.COLS[1:], start=1):
                    val = getattr(lp, attr)
                    txt = self.tr_.tr(GAS_KEYS[int(val) % len(GAS_KEYS)]) if attr == "gas" else f"{val:g}"
                    it = QTableWidgetItem(txt)
                    if attr == "gas":
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(r, c, it)
        self.table.blockSignals(False)
        self._headers()

    def _item_changed(self, it: QTableWidgetItem) -> None:
        if self.document is None or it.column() == 0:
            return
        li = self._row_layer(it.row())
        attr = self.COLS[it.column()][1]
        try:
            setattr(self.document.layers[li], attr, float(it.text()))
            self.changed.emit()
        except (ValueError, KeyError):
            pass


# --------------------------------------------------------------------------- parts
class PartListPanel(QTreeWidget):
    """Part list (np_Part / np_PartName): index, name, layer, contour count, size, closed."""
    selectionToCanvas = Signal(list)

    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.document = None
        self.setColumnCount(5)
        self.setRootIsDecorated(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._sync = False
        self.itemSelectionChanged.connect(self._sel_changed)
        tr.bind(self, "np_Part", lambda _t: self._headers())

    def _headers(self):
        t = self.tr_
        self.setHeaderLabels([t.tr("gp75"), t.tr("np_PartName"), t.tr("gp83"), t.tr("gp76"), t.tr("sf1").strip(" :：")])

    def set_document(self, doc) -> None:
        self.document = doc
        self.refresh()

    def refresh(self) -> None:
        self._sync = True
        self.clear()
        doc = self.document
        if doc is not None:
            for i, p in enumerate(doc.parts):
                x0, y0, x1, y1 = p.bbox()
                it = QTreeWidgetItem([str(i + 1), p.name or f"{self.tr_.tr('np_Part')}{i + 1}",
                                      str(p.craft.layer), str(len(p.contours)), f"{x1 - x0:.2f}×{y1 - y0:.2f}"])
                it.setIcon(2, _swatch(layer_color(p.craft.layer)))
                if any(not c.closed for c in p.contours):
                    it.setForeground(1, QColor("#d00000"))
                it.setData(0, Qt.UserRole, i)
                self.addTopLevelItem(it)
                it.setSelected(p.selected)
        self._sync = False

    def sync_selection(self) -> None:
        if self.document is None:
            return
        self._sync = True
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            idx = it.data(0, Qt.UserRole)
            it.setSelected(self.document.parts[idx].selected)
        self._sync = False

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

    STEP_KEYS = ["mp90", "mp91", "mp92", "mp93", "mp94", "mp95", "mp96", "mp97", "mp98", "mp99"]

    def __init__(self, tr: Translator, controller: ControllerFacade,
                 get_document: Callable[[], object], parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.ctl = controller
        self.get_document = get_document
        self.buttons: dict[str, QAbstractButton] = {}
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)

        # --- jog
        jog = QGroupBox()
        tr.bind(jog, "slp17", jog.setTitle)
        g = QGridLayout(jog)
        for (axis, d, r, c) in (("Y", 1, 0, 1), ("Y", -1, 2, 1), ("X", -1, 1, 0),
                                ("X", 1, 1, 2), ("Z", 1, 0, 3), ("Z", -1, 2, 3)):
            b = QToolButton()
            b.setObjectName(f"jog_{axis}{'+' if d > 0 else '-'}")
            b.setIcon(command_icon(b.objectName()))
            b.setIconSize(QSize(40, 40))
            b.setAccessibleName(f"Jog {axis}{'+' if d > 0 else '-'}")
            b.setToolTip(b.accessibleName())
            b.setAutoRepeat(False)
            b.pressed.connect(lambda a=axis, d=d: self._jog_press(a, d))
            b.released.connect(lambda a=axis: self._jog_release(a))
            g.addWidget(b, r, c)
            self.buttons[b.objectName()] = b
        # mp29 回零位 "Home" = move to the coordinate zero (FUN_005bc220); mp29_1 系统回原 = Go
        # Origin with the mf156 prompt (FUN_005b5de0)
        home = self._text_button("mp29", self._return_zero)
        g.addWidget(home, 1, 1)
        g.addWidget(self._text_button("mp29_1", self._sys_home), 4, 0)
        self.mode_step = QCheckBox()
        tr.bind(self.mode_step, "mp31", self.mode_step.setText)
        self.mode_fast = QCheckBox()
        tr.bind(self.mode_fast, "mp32", self.mode_fast.setText)
        self.step = QComboBox()
        for k in self.STEP_KEYS:
            self.step.addItem(tr.tr(k))
        self.step.setCurrentIndex(3)
        g.addWidget(self.mode_step, 3, 0)
        g.addWidget(self.mode_fast, 3, 1)
        g.addWidget(self.step, 3, 2, 1, 2)
        zf = self._text_button("mp30", lambda: self.ctl.home("Z"))
        g.addWidget(zf, 1, 3)
        # mark points (标记点1..6 = MC.MarkPtIndex, 标记 mp39 FUN_005c9390, 返回 af10 FUN_005c97f0)
        self.mark_index = QComboBox()
        for k in ("mp33", "mp34", "mp35", "mp36", "mp37", "mp38"):
            self.mark_index.addItem(tr.tr(k))
        self.mark_index.currentIndexChanged.connect(self._mark_index_changed)
        g.addWidget(self.mark_index, 4, 1)
        g.addWidget(self._text_button("mp39", self._set_mark), 4, 2)
        g.addWidget(self._text_button("af10", self._return_mark), 4, 3)
        v.addWidget(jog)

        # --- outputs
        io = QGroupBox()
        tr.bind(io, "ss4", io.setTitle)
        gi = QGridLayout(io)
        for n, (name, key) in enumerate((("follow", "mp41"), ("gas", "mp42"), ("shutter", "mp49"),
                                         ("red_light", "mp50"), ("laser", "mp51"))):
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
            gi.addWidget(b, n // 3, n % 3)
        self.gas_type = QComboBox()
        for k in GAS_KEYS:
            self.gas_type.addItem(tr.tr(k))
        self.gas_type.currentIndexChanged.connect(lambda i: self.ctl.set_gas_type(i))
        gi.addWidget(self.gas_type, 1, 2)
        self.power = QSpinBox()
        self.power.setRange(0, 100)
        self.power.setValue(10)
        self.power.setSuffix(" %")
        self.power.valueChanged.connect(lambda p: self.ctl.set_laser_power(float(p)))
        lbl = QLabel()
        tr.bind(lbl, "mp53", lbl.setText)
        gi.addWidget(lbl, 2, 0)
        gi.addWidget(self.power, 2, 1)
        v.addWidget(io)

        # --- process
        proc = QGroupBox()
        tr.bind(proc, "mf141", proc.setTitle)
        gp = QGridLayout(proc)
        start = self._icon_button("mp54", "run.start", self._start)
        pause = self._icon_button("mp55", "run.pause", self._pause)
        stop = self._icon_button("mp56", "run.stop", self.ctl.stop)
        gp.addWidget(start, 0, 0)
        gp.addWidget(pause, 0, 1)
        gp.addWidget(stop, 0, 2)
        gp.addWidget(self._text_button("mp57", lambda: self._with_doc(self.ctl.dry_run)), 1, 0)
        gp.addWidget(self._text_button("mp58", lambda: self._with_doc(self.ctl.frame)), 1, 1)
        gp.addWidget(self._text_button("mp59", self.simulateRequested.emit), 1, 2)
        self.bf_len = QDoubleSpinBox()
        self.bf_len.setRange(0.1, 10000)
        self.bf_len.setValue(10.0)
        gp.addWidget(self._text_button("mp61", lambda: self.ctl.step_backward(self.bf_len.value())), 2, 0)
        gp.addWidget(self._text_button("mp62", lambda: self.ctl.step_forward(self.bf_len.value())), 2, 1)
        gp.addWidget(self.bf_len, 2, 2)
        self.selected_only = QCheckBox()
        tr.bind(self.selected_only, "mp52", self.selected_only.setText)
        gp.addWidget(self.selected_only, 3, 0, 1, 2)
        self.return_to = QComboBox()
        for k in ("mp71", "mp68", "mp69", "mp70"):
            self.return_to.addItem(tr.tr(k))
        lbl2 = QLabel()
        tr.bind(lbl2, "mp67", lbl2.setText)
        gp.addWidget(lbl2, 4, 0)
        gp.addWidget(self.return_to, 4, 1, 1, 2)
        self.soft_limit = QCheckBox()
        tr.bind(self.soft_limit, "mp73", self.soft_limit.setText)
        self.soft_limit.setChecked(True)
        gp.addWidget(self.soft_limit, 5, 0, 1, 3)
        v.addWidget(proc)

        # --- coordinate / misc
        coord = QGroupBox()
        tr.bind(coord, "mf307", coord.setTitle)
        gc = QGridLayout(coord)
        self.coord_sys = QComboBox()
        for k in ("mp200", "mp201", "mp202", "mp203", "mp204", "mp205", "mp206", "mp207"):
            self.coord_sys.addItem(tr.tr(k))
        gc.addWidget(self.coord_sys, 0, 0, 1, 2)
        gc.addWidget(self._text_button("mf514", self.ctl.set_origin), 1, 0)
        gc.addWidget(self._text_button("mp164", self.ctl.go_origin_point), 1, 1)
        gc.addWidget(self._text_button("mf21", self.ctl.clear_alarms), 2, 0)
        v.addWidget(coord)
        v.addStretch(1)

    # -- machine actions
    machine_ops = None       # controller.machine_ops.MachineOps, set by ui.ext_machine.register
    confirm_fn: Optional[Callable[[str], bool]] = None   # tests / scripting: answers the prompts

    def confirm(self, key: str) -> bool:
        """OK/Cancel prompt with the text of lang key ``key`` (BCGPMessageBox 0x1001)."""
        if self.confirm_fn is not None:
            return bool(self.confirm_fn(key))
        from PySide6.QtWidgets import QMessageBox
        r = QMessageBox.question(self, "", self.tr_.tr(key), QMessageBox.Ok | QMessageBox.Cancel)
        return r == QMessageBox.Ok

    def _return_zero(self) -> None:
        fn = getattr(self.ctl, "return_zero", None)
        if callable(fn):
            fn(confirm=self.confirm)
        else:
            self.ctl.go_origin_point()

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
    def _text_button(self, key: str, fn: Optional[Callable], checkable: bool = False) -> QPushButton:
        b = QPushButton()
        self.tr_.bind(b, key, b.setText)
        b.setCheckable(checkable)
        if fn is not None:
            b.clicked.connect(lambda _=False: fn())
        b.setObjectName(key)
        self.buttons[key] = b
        return b

    def _icon_button(self, key: str, icon_cmd: str, fn: Callable) -> QToolButton:
        b = QToolButton()
        b.setIcon(command_icon(icon_cmd))
        b.setIconSize(QSize(36, 36))
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.tr_.bind(b, key, b.setText)
        b.clicked.connect(lambda _=False: fn())
        b.setObjectName(key)
        self.buttons[key] = b
        return b

    def _with_doc(self, fn) -> None:
        doc = self.get_document()
        if doc is not None:
            fn(doc)

    def _start(self) -> None:
        st = self.ctl.state()
        if st.mode == "pause":
            self.ctl.resume()
            return
        doc = self.get_document()
        if doc is not None:
            self.ctl.start(doc, selected_only=self.selected_only.isChecked())

    def _pause(self) -> None:
        if self.ctl.state().mode == "pause":
            self.ctl.resume()
        else:
            self.ctl.pause()

    def _jog_press(self, axis: str, d: int) -> None:
        if self.mode_step.isChecked():
            try:
                step = float(self.tr_.tr(self.STEP_KEYS[self.step.currentIndex()]))
            except ValueError:
                step = 1.0
            import inspect
            try:
                takes_fast = "fast" in inspect.signature(self.ctl.jog_step).parameters
            except (TypeError, ValueError):
                takes_fast = False
            if takes_fast:       # the step jog uses the fast/slow speed too (FUN_005bdae0)
                self.ctl.jog_step(axis, d, step, fast=self.mode_fast.isChecked())
            else:
                self.ctl.jog_step(axis, d, step)
        else:
            self.ctl.jog_start(axis, d, fast=self.mode_fast.isChecked())

    def _jog_release(self, axis: str) -> None:
        if not self.mode_step.isChecked():
            self.ctl.jog_stop(axis)


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
