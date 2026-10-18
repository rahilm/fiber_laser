"""Monitoring, diagnostics, reports and auxiliary functions (phase 3, agent I).

``register(window)`` re-binds these ribbon commands (the ext_cam pattern: disconnect
``triggered``, connect the handler, record it in ``window.handlers``):

=====================  ===========  =========================================================
command                original     rebuild
=====================  ===========  =========================================================
mon.controller         mf119 36b1   :class:`ControllerMonitor` (RO / axis / system / axis RW
                                    pages, IO, single-axis pulses, DA / PWM)
mon.ftc                mf120 36b2   :class:`DeviceMonitor` "zf" (status, properties, calib.)
mon.af                 mf249 36b6   :class:`DeviceMonitor` "af"
mon.ext                mf620 36b7   :class:`DeviceMonitor` "ec"
diag.device_report     mf801 3aa5   :class:`DeviceReportDialog` (SOP.Device* counters)
diag.work_report(_all) mf252/253    report.txt + :class:`mlaser.report.window.ReportWindow`
diag.log_dir           mf431 36b5   :class:`LogViewer`
diag.measure           mf229 3a9b   toggle + :class:`MeasureWindow`
aux.custom_io(_nl)     newLang301/300  :class:`CustomIODialog`
aux.dedust             mf140 4269   toggle of DO.ClearAsh
aux.light_test         newLang200   :class:`LightTestDialog`
aux.burn_in            newLang1     :class:`BurnInDialog`
aux.ball_arm           mf702 426b   ball-bar pattern into the drawing
aux.interferometer     mf180 17000  interferometer dock (:class:`InterferometerPanel`)
aux.batch_cut          mf705 4270   :class:`BatchDialog` + batch runner / production counter
aux.maintain           A251113_0    :class:`MaintainDialog` + reminders
app.help               mf23         :class:`HelpWindow` (docs/ + About)
=====================  ===========  =========================================================

Job statistics are recorded from :class:`mlaser.diag.jobwatch.JobWatcher` events (controller
log messages + state polling).  Persistent state (counters, maintenance plan, batch list,
custom IO, statistics files, logs) lives in the data directory: ``settings["data_dir"]``, the
``MLASER_DATA_DIR`` environment variable, or ``~/.mlaser``.  Evidence: docs/diag.md.
"""
from __future__ import annotations

import json
import sys
import os
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from mlaser.diag import ballarm, batch as B, burnin, customio as CIO, devicereport as DR
from mlaser.diag import helpdocs, interferometer as IF, lighttest, logs as L, maintain as M
from mlaser.diag import registers as R
from mlaser.diag.jobwatch import JobWatcher
from mlaser.diag.measure import DataRecorder
from mlaser.diag.paramset import ParamSet, find_def
from mlaser.report.recorder import StatsRecorder
from mlaser.report.records import ReportStore

STATE_FILE = "diag_state.json"


# =========================================================================== helpers
def _tr(window, key: str, default: str) -> str:
    tr = getattr(window, "tr_", None)
    if tr is None:
        return default
    try:
        if tr.has(key):
            return tr.tr(key)
    except Exception:  # noqa: BLE001
        pass
    return default


def _parent(window):
    return window if isinstance(window, QWidget) else None


def _log(window, msg: str) -> None:
    lp = getattr(window, "log_panel", None)
    if lp is not None:
        lp.log(msg)


def _mc(window):
    return getattr(getattr(window, "ctl", None), "mc", None)


def _lang(window) -> str:
    return getattr(getattr(window, "tr_", None), "language", "en")


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    return t


def _fill(t: QTableWidget, rows: list[tuple]) -> None:
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            item = t.item(i, j)
            if item is None:
                t.setItem(i, j, QTableWidgetItem(str(v)))
            elif item.text() != str(v):
                item.setText(str(v))


class ParamForm(QWidget):
    """Editors for a :class:`ParamSet` built from the original parameter descriptors (labels =
    the name part of the ``Group.Name`` lang text, ranges / enum options from the table)."""

    def __init__(self, window, pset: ParamSet, keys=None, parent=None):
        super().__init__(parent)
        self.pset = pset
        self.editors: dict[str, QWidget] = {}
        form = QFormLayout(self)
        tr = getattr(window, "tr_", None)
        for key in keys or pset.keys:
            d = find_def(key)
            v = pset.get(key)
            label = key
            if d is not None:
                label = d.label_en.split(".", 1)[-1] if d.label_en else key
                if tr is not None and d.lang and tr.has(d.lang):
                    label = tr.tr_param(d.lang)[1]
            t = d.type if d is not None else (5 if isinstance(v, bool) else 2)
            if d is not None and d.options:
                w = QComboBox()
                for lk, en, _zh in d.options:
                    w.addItem(tr.tr(lk) if tr is not None and lk and tr.has(lk) else (en or lk))
                w.setCurrentIndex(max(0, min(int(v), w.count() - 1)))
            elif t == 5:
                w = QCheckBox()
                w.setChecked(bool(v))
            elif t in (1, 4, 6, 7, 10, 12):
                w = QSpinBox()
                lo, hi = (d.min, d.max) if d is not None and d.max > d.min else (-10 ** 9, 10 ** 9)
                w.setRange(int(lo), int(min(hi, 2 ** 31 - 1)))
                w.setValue(int(v))
            elif t == 3:
                w = QLineEdit(str(v))
            else:
                w = QDoubleSpinBox()
                lo, hi = (d.min, d.max) if d is not None and d.max > d.min else (-1e9, 1e9)
                w.setRange(lo, hi)
                w.setDecimals(3)
                w.setValue(float(v))
            if d is not None and d.unit:
                label = f"{label} ({d.unit})"
            form.addRow(label, w)
            self.editors[key] = w

    def store(self) -> None:
        for key, w in self.editors.items():
            if isinstance(w, QComboBox):
                self.pset.set(key, w.currentIndex())
            elif isinstance(w, QCheckBox):
                self.pset.set(key, w.isChecked())
            elif isinstance(w, QSpinBox):
                self.pset.set(key, w.value())
            elif isinstance(w, QLineEdit):
                self.pset.set(key, w.text())
            else:
                self.pset.set(key, w.value())


class ParamDialog(QDialog):
    """OK / Cancel property dialog (FUN_005ee240 equivalent)."""

    def __init__(self, window, title: str, pset: ParamSet, extra: Optional[QWidget] = None):
        super().__init__(_parent(window))
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        self.form = ParamForm(window, pset, parent=self)
        lay.addWidget(self.form)
        if extra is not None:
            lay.addWidget(extra)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(_tr(window, "ap1", "OK"))
        bb.button(QDialogButtonBox.Cancel).setText(_tr(window, "ap0", "Cancel"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _ok(self) -> None:
        self.form.store()
        self.accept()


# =========================================================================== monitors
class ControllerMonitor(QDialog):
    """Controller monitor (mf119): the card's register blocks, IO, axis pulses, DA / PWM.

    Read-only by default (ss9 "ReadOnly"); "Read&Write" (ss10) enables the output toggles,
    pulse sending and DA / PWM tests.  The original asks an access password (ss11) for
    Read&Write; vendor passwords are intentionally not reproduced."""

    PAGES = (("RO", "RO 1000+"), ("AxisRO", "Axis RO 2000+"), ("SystemRW", "System RW 50000+"),
             ("AxisRW", "Axis RW 50200+"))

    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "mf119", "Controller"))
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.ro_mode = QCheckBox(_tr(w, "ss10", "Read&Write"))
        top.addWidget(QLabel(_tr(w, "ss9", "ReadOnly") + " / "))
        top.addWidget(self.ro_mode)
        self.status = QLabel("")
        top.addWidget(self.status, 1)
        b = QPushButton(_tr(w, "zf0", "Refresh"))
        b.clicked.connect(lambda: self.refresh(True))
        top.addWidget(b)
        b = QPushButton(_tr(w, "ss0", "System Reset"))
        b.clicked.connect(self._reset)
        top.addWidget(b)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tables = {}
        for key, title in self.PAGES:
            t = _table(["addr", "name", "value"])
            self.tables[key] = t
            self.tabs.addTab(t, title)
        self.tabs.addTab(self._io_page(), f"{_tr(w, 'ss5', 'DI')} / {_tr(w, 'ss4', 'DO')}")
        self.tabs.addTab(self._axis_page(), _tr(w, "ss13", "Axis"))
        self.tabs.addTab(self._da_page(), "DA / PWM")
        self.snapshot = R.ControllerSnapshot()
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self.refresh(False))
        self._timer.start(500)
        self.refresh(True)

    # pages
    def _io_page(self) -> QWidget:
        page = QWidget()
        g = QGridLayout(page)
        self.di = []
        self.do = []
        for i in range(28):
            lb = QLabel(f"DI{i + 1}")
            lb.setFrameShape(QLabel.Box)
            lb.setAlignment(Qt.AlignCenter)
            g.addWidget(lb, i // 7, i % 7)
            self.di.append(lb)
        for i in range(26):
            b = QPushButton(f"DO{i + 1}")
            b.setCheckable(True)
            b.clicked.connect(lambda on, n=i + 1: self._set_do(n, on))
            g.addWidget(b, 5 + i // 7, i % 7)
            self.do.append(b)
        self.limits = QLabel("")
        g.addWidget(self.limits, 10, 0, 1, 7)
        return page

    def _axis_page(self) -> QWidget:
        page = QWidget()
        f = QFormLayout(page)
        self.axis = QComboBox()
        self.axis.addItems(R.AXIS_NAMES)
        self.pulses = QSpinBox()
        self.pulses.setRange(-10 ** 9, 10 ** 9)
        self.pulses.setValue(1000)
        self.pulse_speed = QSpinBox()
        self.pulse_speed.setRange(1, 10 ** 8)
        self.pulse_speed.setValue(10000)
        f.addRow(_tr(self.ext.w, "ss6", "Axis"), self.axis)
        f.addRow(_tr(self.ext.w, "ss14", "Pulse"), self.pulses)
        f.addRow("speed (pulses/s)", self.pulse_speed)
        row = QHBoxLayout()
        b = QPushButton(_tr(self.ext.w, "ss8", "Send"))
        b.clicked.connect(self.send_pulses)
        row.addWidget(b)
        b = QPushButton(_tr(self.ext.w, "ss15", "Stop"))
        b.clicked.connect(self._stop_axes)
        row.addWidget(b)
        f.addRow(row)
        self.pos_lbl = QLabel("")
        f.addRow(_tr(self.ext.w, "newLang402", "current location:"), self.pos_lbl)
        return page

    def _da_page(self) -> QWidget:
        page = QWidget()
        f = QFormLayout(page)
        self.da1, self.da2 = QSpinBox(), QSpinBox()
        for s in (self.da1, self.da2):
            s.setRange(0, 10000)
        self.pwm_f, self.pwm_d = QSpinBox(), QSpinBox()
        self.pwm_f.setRange(0, 100000)
        self.pwm_f.setValue(5000)
        self.pwm_d.setRange(0, 100)
        f.addRow("DA1 (0..10000)", self.da1)
        f.addRow("DA2 (0..10000)", self.da2)
        f.addRow("PWM Hz", self.pwm_f)
        f.addRow("PWM %", self.pwm_d)
        b = QPushButton(_tr(self.ext.w, "ec17", "Set"))
        b.clicked.connect(self.apply_da_pwm)
        f.addRow(b)
        return page

    # actions
    def _writable(self) -> bool:
        if not self.ro_mode.isChecked():
            self.status.setText(_tr(self.ext.w, "ss9", "ReadOnly"))
            return False
        if _mc(self.ext.w) is None:
            self.status.setText(_tr(self.ext.w, "ss1", "Hardware is not connected"))
            return False
        return True

    def _set_do(self, n: int, on: bool) -> None:
        if not self._writable():
            self.do[n - 1].setChecked(not on)
            return
        try:
            _mc(self.ext.w).set_output(n, on)
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))

    def send_pulses(self) -> None:
        """Single-axis pulse jog (newLang400/401, ss13/ss14/ss8): mailbox ``[3, axis, speed,
        acc, acc*10, distance]`` (FUN_10032b80) in card units; ss3 if not an integer."""
        if not self._writable():
            return
        st = self.ext.w.ctl.state()
        if st.mode in ("process", "frame", "dry_run"):
            self.status.setText(_tr(self.ext.w, "ss2", "System is moving"))
            return
        try:
            sp = self.pulse_speed.value()
            _mc(self.ext.w).jog(self.axis.currentIndex(), sp, sp * 10, self.pulses.value())
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))

    def _stop_axes(self) -> None:
        mc = _mc(self.ext.w)
        if mc is not None:
            try:
                mc.stop_axes()
            except Exception as e:  # noqa: BLE001
                self.status.setText(str(e))

    def apply_da_pwm(self) -> None:
        """DA / PWM test ([9999, 4, ch, v], [9999, 3, f, d]); ss16..ss18 on bad values."""
        if not self._writable():
            return
        mc = _mc(self.ext.w)
        try:
            mc.set_da(0, self.da1.value())
            mc.set_da(1, self.da2.value())
            mc.set_pwm(self.pwm_f.value(), self.pwm_d.value())
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))

    def _reset(self) -> None:
        """ss0 "System Reset": alarm reset [9999, 5, 0, 0]."""
        try:
            self.ext.w.ctl.clear_alarms()
        except Exception as e:  # noqa: BLE001
            self.status.setText(str(e))

    def refresh(self, params: bool = False) -> None:
        mc = _mc(self.ext.w)
        if mc is None:
            self.status.setText(_tr(self.ext.w, "ss1", "Hardware is not connected"))
            return
        snap = R.read_snapshot(mc, params=params or not self.snapshot.sys_rw)
        if not snap.sys_rw:
            snap.sys_rw, snap.axis_rw = self.snapshot.sys_rw, self.snapshot.axis_rw
        self.snapshot = snap
        self.status.setText("; ".join(snap.errors) or "")
        pages = R.table_rows(snap, getattr(self.ext.w, "tr_", None))
        for key, rows in pages.items():
            _fill(self.tables[key], rows)
        for lb, on in zip(self.di, snap.inputs()):
            lb.setStyleSheet("background:#3c3" if on else "")
        for b, on in zip(self.do, snap.outputs()):
            if b.isChecked() != on:
                b.setChecked(on)
        neg, pos = _tr(self.ext.w, "ss19", "NEG"), _tr(self.ext.w, "ss21", "POS")
        txt = []
        for a in range(min(4, len(snap.axis_ro))):
            n, _o, p = snap.axis_limits(a)
            txt.append(f"{R.AXIS_NAMES[a]}: {neg}={'1' if n else '0'} {pos}={'1' if p else '0'}")
        self.limits.setText("   ".join(txt))
        ppm = getattr(mc, "pulses_per_mm", 1000.0) or 1000.0
        a = self.axis.currentIndex()
        if a < len(snap.axis_ro):
            self.pos_lbl.setText(f"{R.s32(snap.axis_ro[a][2]) / ppm:.3f} mm")

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        super().closeEvent(ev)


class DeviceMonitor(QDialog):
    """FTC (zf), auto-focus (af) and extension card (ec) monitors."""

    TITLES = {"zf": ("mf120", "FTC"), "af": ("mf249", "Auto Focus"), "ec": ("mf620", "Extended Card")}
    TYPE_KEYS = {"zf": "ZF.ZFType", "af": "AF.AFType", "ec": "EC.ECType"}

    def __init__(self, ext, kind: str):
        super().__init__(_parent(ext.w))
        self.ext, self.kind, w = ext, kind, ext.w
        self.client = None
        key, default = self.TITLES[kind]
        self.setWindowTitle(_tr(w, key, default))
        self.resize(560, 520)
        lay = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.table = _table(["#", "name", "value"])
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self._buttons(row)
        lay.addLayout(row)
        if kind == "zf":
            lay.addWidget(QLabel(_tr(w, "zf36", "FTC Property")))
            self.props = _table(["#", "name", "raw"])
            self.props.setEditTriggers(QAbstractItemView.DoubleClicked)
            lay.addWidget(self.props, 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        t = ext.settings.get(self.TYPE_KEYS[kind])
        if t is not None and int(t) == 0:
            self.info.setText(f"{self.TYPE_KEYS[kind]} = 0: device not configured "
                              "(the original disables this monitor)")
        self.refresh()

    def _btn(self, row, key, default, fn):
        b = QPushButton(_tr(self.ext.w, key, default))
        b.clicked.connect(lambda _=False: self._safe(fn))
        row.addWidget(b)
        return b

    def _buttons(self, row) -> None:
        if self.kind == "zf":
            self._btn(row, "zf0", "Refresh Params", self.read_props)
            self._btn(row, "zf1", "Write Params", self.write_props)
            self._btn(row, "zf24", "Head Calibration", self.calibrate)
            self._btn(row, "zf27", "Cancel Head Calibration", lambda: self._zf().cancel_calibration())
            self._btn(row, "zf103", "Export Param", self.export_zpf)
            self._btn(row, "zf102", "Import Param", self.import_zpf)
        elif self.kind == "af":
            self.step = QDoubleSpinBox()
            self.step.setRange(0.001, 100)
            self.step.setValue(1.0)
            row.addWidget(QLabel(_tr(self.ext.w, "af9", "StepLen")))
            row.addWidget(self.step)
            self._btn(row, "mf0", "Connect", self.connect_device)
            self._btn(row, "af13", "Go Origin", lambda: self.client.home())
            self._btn(row, "af14", "Step Up", lambda: self._af_step(+1))
            self._btn(row, "af15", "Step Down", lambda: self._af_step(-1))
            self._btn(row, "af16", "Stop", lambda: self.client.stop())
        else:
            self.speed = QDoubleSpinBox()
            self.speed.setRange(1, 200)                # ec25 1..200 mm/s
            self.speed.setValue(10)
            self.length = QDoubleSpinBox()
            self.length.setRange(0, 10000)             # ec23 0..10000 mm
            self.length.setValue(10)
            row.addWidget(QLabel(_tr(self.ext.w, "ec26", "Speed")))
            row.addWidget(self.speed)
            row.addWidget(self.length)
            self._btn(row, "mf0", "Connect", self.connect_device)
            self._btn(row, "ec14", "GoOrigin", lambda: self.client.home())
            self._btn(row, "ec13", "Stop", lambda: self.client.stop())
            self._btn(row, "", "+", lambda: self.client.jog(self.speed.value(), self.length.value()))
            self._btn(row, "", "-", lambda: self.client.jog(self.speed.value(), -self.length.value()))

    def _safe(self, fn) -> None:
        try:
            if self.kind in ("af", "ec") and self.client is None and fn != self.connect_device:
                self.info.setText("not connected")
                return
            fn()
        except Exception as e:  # noqa: BLE001
            self.info.setText(f"error: {e}")

    def _zf(self):
        from mlaser.devices.follower import OnBoardFollower
        mc = _mc(self.ext.w)
        if mc is None:
            raise RuntimeError("hardware is not connected")
        return OnBoardFollower(mc)

    def connect_device(self) -> None:
        """Network AF (types 1/2) / EC bridge endpoints from File/ipAdd.ini (DeviceSettings)."""
        from mlaser.devices.transport import DeviceSettings
        ds = self.ext.device_settings or DeviceSettings()
        if self.kind == "af":
            from mlaser.devices.autofocus import AutoFocus
            self.client = AutoFocus.connect(*ds.af, ds.access, ds.af_timeout_ms)
        else:
            from mlaser.devices.ethercat import EtherCATBridge
            self.client = EtherCATBridge.connect(*ds.ec, ds.access, ds.ec_timeout_ms)
        self.refresh()

    def _af_step(self, sign: int) -> None:
        st = self.client.read_status()
        pos = R.s32(st[7]) * 0.001
        self.client.move_to(pos + sign * self.step.value(), 10.0)

    def calibrate(self) -> None:
        zf = self._zf()
        if not zf.status().homed:
            self.info.setText(_tr(self.ext.w, "zf49", "Z Follower does not go origin, calibration refused"))
            return
        zf.calibrate()

    def read_props(self) -> None:
        from mlaser.devices.follower import PROP_FIELDS
        raw = self._zf().read_props()
        rows = [(i, PROP_FIELDS.get(i, (f"prop{i}", 1))[0], R.s32(v)) for i, v in enumerate(raw)]
        _fill(self.props, rows)
        for i in range(self.props.rowCount()):
            self.props.item(i, 2).setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable)

    def write_props(self) -> None:
        """zf1: writeReg(11000 + 2*idx, v) for changed values, then 150 := 9999 (FUN_00633000)."""
        zf = self._zf()
        old = zf.read_props()
        n = 0
        for i in range(self.props.rowCount()):
            try:
                v = int(self.props.item(i, 2).text())
            except (AttributeError, ValueError):
                continue
            if i < len(old) and R.s32(old[i]) != v:
                zf.write_prop(i, v & 0xFFFFFFFF, commit=False)
                n += 1
        if n:
            from mlaser.devices.follower import COMMIT_SAVE, REG_COMMIT
            _mc(self.ext.w).write_reg(REG_COMMIT, COMMIT_SAVE)
            self.info.setText(_tr(self.ext.w, "zf37", "Parameters write successfully!"))

    def export_zpf(self, path: Optional[str] = None) -> Optional[str]:
        """zf103: ``N\\r\\n`` + N x ``%d\\r\\n`` + ``eof`` from 11000 (FUN_006343c0)."""
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, _tr(self.ext.w, "zf103", "Export Param"), "", "*.zpf")
        if not path:
            return None
        raw = self._zf().read_props()
        with open(path, "w", newline="") as f:
            f.write(f"{len(raw)}\r\n" + "".join(f"{R.s32(v)}\r\n" for v in raw) + "eof")
        self.info.setText(_tr(self.ext.w, "zf105", "The param data export success"))
        return path

    def import_zpf(self, path: Optional[str] = None) -> bool:
        """zf102: write the values back at 11000 (func 0x40, FUN_00634780)."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, _tr(self.ext.w, "zf102", "Import Param"), "", "*.zpf")
        if not path:
            return False
        lines = [ln.strip() for ln in open(path).read().splitlines() if ln.strip()]
        try:
            n = int(lines[0])
            vals = [int(x) & 0xFFFFFFFF for x in lines[1:1 + n]]
        except (ValueError, IndexError):
            self.info.setText(_tr(self.ext.w, "zf106", "The param data import failed!"))
            return False
        _mc(self.ext.w).write_regs(11000, vals)
        self.info.setText(_tr(self.ext.w, "zf107", "The param data import success"))
        return True

    def refresh(self) -> None:
        raw, err = R.read_device(self.kind, _mc(self.ext.w), self.client)
        if err:
            if not self.info.text() or self.info.text().startswith(("error", "not")):
                self.info.setText(f"not available: {err}")
            return
        rows = R.device_rows(self.kind, raw, getattr(self.ext.w, "tr_", None))
        if self.kind == "zf":
            from mlaser.devices.follower import FollowerStatus
            fs = FollowerStatus(list(raw) + [0] * max(0, 18 - len(raw)))
            grade = {"excellent": "zf20", "good": "zf21", "bad": "zf22"}.get(fs.calibration_grade)
            rows.append(("", "homed", _tr(self.ext.w, "af11", "Go Origin Done") if fs.homed
                         else _tr(self.ext.w, "af12", "Go Origin Undone")))
            rows.append(("", "calibration", _tr(self.ext.w, grade, fs.calibration_grade) if grade
                         else fs.calibration_grade))
        _fill(self.table, rows)

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(ev)


# =========================================================================== diagnostics
class DeviceReportDialog(QDialog):
    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "mf801", "Device Report"))
        lay = QVBoxLayout(self)
        self.table = _table(["", _tr(w, "hp40", "Name"), "Value"])
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        b = QPushButton(_tr(w, "ap20", "Set Zero"))
        b.clicked.connect(self.zero_selected)
        row.addWidget(b)
        b = QPushButton(_tr(w, "ap21", "All Set Zero"))
        b.clicked.connect(lambda: self.zero_all())
        row.addWidget(b)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        _fill(self.table, self.ext.counters.as_rows(getattr(self.ext.w, "tr_", None)))

    def zero_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        for r in rows:
            self.ext.counters.reset(self.table.item(r, 0).text())
        self.ext.save_state()
        self.refresh()

    def zero_all(self, confirm: bool = True) -> None:
        """ap21 with confirmation ap22."""
        if confirm and QMessageBox.question(self, _tr(self.ext.w, "ap21", "All Set Zero"),
                                            _tr(self.ext.w, "ap22", "The device run info will be set zero, continue?"),
                                            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.ext.counters.reset()
        self.ext.save_state()
        self.refresh()


class LogViewer(QDialog):
    """Log directory viewer (mf431): files of the log directories, filter and level."""

    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext = ext
        self.setWindowTitle(_tr(ext.w, "mf431", "Open Log Directory"))
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter")
        self.filter.textChanged.connect(self._show)
        self.level = QComboBox()
        self.level.addItems(["", "Info", "Warning", "Error"])
        self.level.currentIndexChanged.connect(self._show)
        top.addWidget(self.filter, 1)
        top.addWidget(self.level)
        b = QPushButton("Refresh")
        b.clicked.connect(self.reload)
        top.addWidget(b)
        b = QPushButton(_tr(ext.w, "mf431", "Open Log Directory"))
        b.clicked.connect(self.open_folder)
        top.addWidget(b)
        lay.addLayout(top)
        sp = QSplitter()
        self.files = QListWidget()
        self.files.currentRowChanged.connect(self._load)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        sp.addWidget(self.files)
        sp.addWidget(self.text)
        sp.setSizes([250, 650])
        lay.addWidget(sp, 1)
        self.lines: list = []
        self.reload()

    def reload(self) -> None:
        self.paths = L.list_logs(self.ext.log_dirs())
        self.files.clear()
        for p in self.paths:
            self.files.addItem(QListWidgetItem(os.path.basename(p)))
        if self.paths:
            self.files.setCurrentRow(0)
        else:
            self.text.setPlainText("no log files in: " + ", ".join(self.ext.log_dirs()))

    def _load(self, row: int) -> None:
        if 0 <= row < len(self.paths):
            self.lines = L.parse_log(L.read_log(self.paths[row]))
            self._show()

    def _show(self, *_):
        sel = L.filter_lines(self.lines, self.filter.text(), self.level.currentText())
        self.text.setPlainText("\n".join(f"{ln.time} [{ln.level}] {ln.text}" if ln.time else ln.text for ln in sel))

    def open_folder(self) -> None:
        d = self.ext.log_dir
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))


class MeasureWindow(QDialog):
    """Data measurement samples (mf229)."""

    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext = ext
        self.setWindowTitle(_tr(ext.w, "mf229", "Data measurement"))
        self.resize(620, 420)
        lay = QVBoxLayout(self)
        self.summary = QLabel("")
        lay.addWidget(self.summary)
        self.table = _table(["t (s)", "X", "Y", "Z", "speed", "laser", "mode"])
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        b = QPushButton("Save CSV...")
        b.clicked.connect(lambda: self.save())
        row.addWidget(b)
        b = QPushButton("Clear")
        b.clicked.connect(self.ext.measure.start)
        row.addWidget(b)
        lay.addLayout(row)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)

    def refresh(self) -> None:
        s = self.ext.measure.samples[-200:]
        _fill(self.table, [(f"{x.t:.2f}", f"{x.x:.3f}", f"{x.y:.3f}", f"{x.z:.3f}", f"{x.speed:.1f}",
                            int(x.laser), x.mode) for x in reversed(s)])
        sm = self.ext.measure.summary()
        self.summary.setText(f"{sm.get('n', 0)} samples"
                             + (f", {sm['duration_s']:.1f} s, max speed {sm['max_speed']:.1f} mm/s"
                                if sm.get("n") else ""))

    def save(self, path: Optional[str] = None) -> Optional[str]:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "CSV", "measure.csv", "CSV (*.csv)")
        if path:
            self.ext.measure.save_csv(path)
            return path
        return None

    def closeEvent(self, ev) -> None:
        self._timer.stop()
        super().closeEvent(ev)


# =========================================================================== aux
class CustomIODialog(QDialog):
    """Custom IO buttons of one type plus the DO.CustomDOStr editor."""

    def __init__(self, ext, self_locking: bool):
        super().__init__(_parent(ext.w))
        self.ext, self.self_locking = ext, self_locking
        w = ext.w
        self.setWindowTitle(_tr(w, "newLang301" if self_locking else "newLang300",
                                "Custom IO [self-locking]" if self_locking else "Custom IO [non-self-locking]"))
        lay = QVBoxLayout(self)
        self.grid = QGridLayout()
        lay.addLayout(self.grid)
        self.buttons: dict[int, QPushButton] = {}
        lay.addWidget(QLabel("DO.CustomDOStr (pd521): name / DO / self-locking"))
        self.editor = _table([_tr(w, "hp40", "Name"), "DO", "self-locking"])
        self.editor.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        lay.addWidget(self.editor)
        row = QHBoxLayout()
        for text, fn in (("+", self._add_row), ("-", self._del_row), (_tr(w, "cp3", "Save"), self.save)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self._load_editor()
        self._build_buttons()

    def items(self) -> list:
        return [c for c in self.ext.custom_io() if c.self_locking == self.self_locking]

    def _build_buttons(self) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.buttons.clear()
        items = self.items()
        if not items:
            self.grid.addWidget(QLabel("no custom DO of this type configured"), 0, 0)
        for i, c in enumerate(items):
            b = QPushButton(f"{c.name} (DO{c.port})")
            if self.self_locking:
                b.setCheckable(True)
                b.setChecked(self.ext.outputs.get(c.port))
                b.clicked.connect(lambda _=False, c=c: self.press(c))
            else:
                b.pressed.connect(lambda c=c: self.ext.outputs.set(c.port, True))
                b.released.connect(lambda c=c: self.ext.outputs.set(c.port, False))
            self.grid.addWidget(b, i // 4, i % 4)
            self.buttons[c.port] = b

    def press(self, c) -> bool:
        """FUN_004bc460: type 1 toggles its DO."""
        on = self.ext.outputs.toggle(c.port)
        b = self.buttons.get(c.port)
        if b is not None:
            b.setChecked(on)
        return on

    def _load_editor(self) -> None:
        rows = [(c.name, c.port, "1" if c.self_locking else "0") for c in self.ext.custom_io()]
        _fill(self.editor, rows)

    def _add_row(self) -> None:
        r = self.editor.rowCount()
        self.editor.setRowCount(r + 1)
        for j, v in enumerate(("DO", "1", "1" if self.self_locking else "0")):
            self.editor.setItem(r, j, QTableWidgetItem(v))

    def _del_row(self) -> None:
        r = self.editor.currentRow()
        if r >= 0:
            self.editor.removeRow(r)

    def save(self) -> None:
        items = []
        for r in range(self.editor.rowCount()):
            try:
                name = self.editor.item(r, 0).text()
                port = int(self.editor.item(r, 1).text())
                lock = self.editor.item(r, 2).text().strip() in ("1", "-", "true", "yes")
            except (AttributeError, ValueError):
                continue
            items.append(CIO.CustomIO(name, port, lock))
        self.ext.settings["DO.CustomDOStr"] = CIO.format_custom_do(items)
        self.ext.save_state()
        self._build_buttons()


class LightTestDialog(QDialog):
    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "newLang200", "Light test"))
        lay = QVBoxLayout(self)
        self.p = lighttest.params(ext.settings)
        self.form = ParamForm(w, self.p, parent=self)
        lay.addWidget(self.form)
        self.remain = QLabel("")
        lay.addWidget(self.remain)
        row = QHBoxLayout()
        b = QPushButton(_tr(w, "mp54", "Start"))
        b.clicked.connect(self.start)
        row.addWidget(b)
        b = QPushButton(_tr(w, "mp56", "Stop"))
        b.clicked.connect(self.stop)
        row.addWidget(b)
        lay.addLayout(row)
        self.test = lighttest.LightTest(ext.w.ctl, self.p, log=lambda m: _log(w, m))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.finished.connect(lambda _r: self.stop())   # FUN_005686c0: stop before EndDialog

    def start(self) -> None:
        self.form.store()
        self.test.ctl = self.ext.w.ctl
        if self.test.start():
            self._timer.start(200)
            self._tick()

    def _tick(self) -> None:
        on = self.test.tick()
        label = _tr(self.ext.w, "newLang206", "Remaining light time")
        self.remain.setText(f"{label}: {self.test.remaining} s" if on and self.test.timed else
                            ("laser on" if on else ""))
        if not on:
            self._timer.stop()

    def stop(self) -> None:
        self.test.stop()
        self._timer.stop()
        self.remain.setText("")


class BurnInDialog(QDialog):
    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "newLang1", "Burn-in test"))
        lay = QVBoxLayout(self)
        self.p = burnin.params(ext.settings)
        self.form = ParamForm(w, self.p, parent=self)
        lay.addWidget(self.form)
        self.count = QLabel("")
        lay.addWidget(self.count)
        warn = QLabel(_tr(w, "newLang24", "When this page is exited, the baking test will stop!"))
        warn.setStyleSheet("color:#c00")
        lay.addWidget(warn)
        row = QHBoxLayout()
        b = QPushButton(_tr(w, "mp54", "Start"))
        b.clicked.connect(self.start)
        row.addWidget(b)
        b = QPushButton(_tr(w, "mp56", "Stop"))
        b.clicked.connect(self.stop)
        row.addWidget(b)
        lay.addLayout(row)
        self.test: Optional[burnin.BurnIn] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.finished.connect(lambda _r: self.stop())

    def start(self) -> None:
        self.form.store()
        self.test = burnin.BurnIn(self.ext.w.ctl, self.p, self.ext.w.document, self.ext.marks(),
                                  log=lambda m: _log(self.ext.w, m))
        if self.test.start():
            self._timer.start(50)
            self._tick()

    def _tick(self) -> None:
        if self.test is None:
            return
        running = self.test.tick()
        done, total = self.test.progress()
        fmt = _tr(self.ext.w, "newLang7", "Number of baking tests: %d/%d")
        try:
            self.count.setText(fmt % (done, total))
        except (TypeError, ValueError):
            self.count.setText(f"{done}/{total}")
        if not running:
            self._timer.stop()

    def stop(self) -> None:
        if self.test is not None:
            self.test.stop()
        self._timer.stop()


class InterferometerPanel(QWidget):
    """Docked interferometer panel (FUN_004bbc60 toggles it)."""

    def __init__(self, ext):
        super().__init__()
        self.ext, w = ext, ext.w
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        page = QWidget()
        pl = QVBoxLayout(page)
        self.p = IF.params(ext.settings)
        self.form = ParamForm(w, self.p, parent=page)
        pl.addWidget(self.form)
        row = QHBoxLayout()
        for key, default, fn in (("mp59", "Sim", self.simulate), ("mp54", "Start", self.start),
                                 ("mp56", "Stop", self.stop)):
            b = QPushButton(_tr(w, key, default))
            b.clicked.connect(fn)
            row.addWidget(b)
        pl.addLayout(row)
        self.status = QLabel("")
        pl.addWidget(self.status)
        tabs.addTab(page, _tr(w, "mf180", "Laser Interferometer"))
        comp = QWidget()
        cl = QVBoxLayout(comp)
        self.ctable = _table([_tr(w, "cp7", "Position"), _tr(w, "cp8", "Positive Measured"),
                              _tr(w, "cp10", "Negative Measured"), _tr(w, "cp9", "Positive Deviation"),
                              _tr(w, "cp11", "Negative Deviation"), _tr(w, "cp12", "Back Lash")])
        cl.addWidget(self.ctable, 1)
        self.backlash = QLabel("")
        cl.addWidget(self.backlash)
        row = QHBoxLayout()
        self.use_comp = QCheckBox("compensate targets (MP.CompensateType 2)")
        row.addWidget(self.use_comp)
        for key, default, fn in (("mf9", "Open", self.import_table), ("cp3", "Save", self.save_table),
                                 ("cp2", "Clear", self.clear_table)):
            b = QPushButton(_tr(w, key, default))
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        cl.addLayout(row)
        tabs.addTab(comp, _tr(w, "cp6", "Precision Compensate"))
        self.table = IF.PitchTable()
        self.runner = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _program(self):
        self.form.store()
        st = self.ext.w.ctl.state()
        axis = int(self.p["SP.InterfereAxisIndex"])
        home_dir = int(self.ext.settings.get(f"MAC_{axis}.GoOriginalDirection",
                                             self.ext.settings.get("MAC.GoOriginalDirection", 0)) or 0)
        mc = _mc(self.ext.w)
        ppm = getattr(mc, "pulses_per_mm", 1000.0) if mc is not None else 1000.0
        return IF.build_program(self.p, (st.x, st.y), home_dir, ppm, self.table,
                                2 if self.use_comp.isChecked() and self.table.positions else 0)

    def simulate(self) -> None:
        try:
            steps = self._program()
        except ValueError as e:
            self.status.setText(str(e))
            return
        path = [(x, y, False) for x, y in IF.program_path(steps)]
        st = self.ext.w.ctl.state()
        canvas = getattr(self.ext.w, "canvas", None)
        if canvas is not None and path:
            canvas.start_simulation(path=[(st.x, st.y, False)] + path)
        self.status.setText(f"{len(path)} moves")

    def start(self) -> None:
        from mlaser.diag.program import ProgramRunner
        try:
            steps = self._program()
        except ValueError as e:
            self.status.setText(str(e))
            return
        if not self.ext.w.ctl.state().connected:
            self.status.setText(_tr(self.ext.w, "ss1", "Hardware is not connected"))
            return
        self.runner = ProgramRunner(self.ext.w.ctl, steps, log=lambda m: _log(self.ext.w, m))
        self.runner.start()
        self._timer.start(50)

    def _tick(self) -> None:
        if self.runner is None:
            return
        if not self.runner.tick():
            self._timer.stop()
        cur = self.runner.current
        self.status.setText(f"{self.runner.state}: step {self.runner.index + 1}/{len(self.runner.steps)} "
                            f"{getattr(cur, 'label', '')}")

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()
        self._timer.stop()

    def set_table(self, t: IF.PitchTable) -> None:
        self.table = t
        fe, be = t.errors()
        bl = t.backlash()
        _fill(self.ctable, [(f"{p:.4f}", f"{t.forward[i]:.4f}", f"{t.backward[i]:.4f}", f"{fe[i]:.4f}",
                             f"{be[i]:.4f}", f"{bl[i]:.4f}") for i, p in enumerate(t.positions)])
        fmt = _tr(self.ext.w, "cp15", "Avg.Backlash (mm): %.4f")
        try:
            self.backlash.setText(fmt % t.mean_backlash())
        except (TypeError, ValueError):
            self.backlash.setText(f"{t.mean_backlash():.4f}")

    def import_table(self, path: Optional[str] = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, _tr(self.ext.w, "cp6", "Precision Compensate"), "",
                                                  "*.csv *.txt *.pos *.rtl *.ren")
        if not path:
            return False
        try:
            raw = open(path, "rb").read()
        except OSError:
            self.backlash.setText(_tr(self.ext.w, "cp30", "Invalid file path"))
            return False
        if not raw:
            self.backlash.setText(_tr(self.ext.w, "cp31", "Empty file"))
            return False
        t = IF.PitchTable.parse(L.decode(raw))
        if not t.positions:
            self.backlash.setText(_tr(self.ext.w, "cp33", "Can't parse file"))
            return False
        self.set_table(t)
        return True

    def save_table(self, path: Optional[str] = None) -> Optional[str]:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, _tr(self.ext.w, "cp3", "Save"), "pitch.csv", "*.csv")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.table.to_csv())
            return path
        return None

    def clear_table(self) -> None:
        self.set_table(IF.PitchTable())


class BatchDialog(QDialog):
    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "mf705", "Batch Cut"))
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        self.p = B.params(ext.settings)
        self.form = ParamForm(w, self.p, parent=self)
        lay.addWidget(self.form)
        self.files = _table([_tr(w, "ap101", "File Path"), _tr(w, "ap102", "Planned quantity"),
                             _tr(w, "ap103", "Done quantity")])
        self.files.setEditTriggers(QAbstractItemView.DoubleClicked)
        lay.addWidget(self.files, 1)
        row = QHBoxLayout()
        for text, fn in (("+", self.add_file), ("-", self.remove_file), ("0", self.reset_done)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        box = QGroupBox(_tr(w, "pd858", "Plan Count").split(".")[0])
        self.counter = ParamForm(w, ParamSet(B.COUNTER_KEYS, ext.settings), parent=box)
        QVBoxLayout(box).addWidget(self.counter)
        lay.addWidget(box)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load()

    def _load(self) -> None:
        _fill(self.files, [(e.path, e.planned, e.done) for e in self.ext.batch_list.entries])

    def _collect(self) -> None:
        entries = []
        for r in range(self.files.rowCount()):
            try:
                entries.append(B.BatchEntry(self.files.item(r, 0).text(), int(self.files.item(r, 1).text()),
                                            int(self.files.item(r, 2).text())))
            except (AttributeError, ValueError):
                continue
        self.ext.batch_list.entries = entries

    def add_file(self, path: Optional[str] = None, planned: int = 1) -> None:
        paths = [path] if path else QFileDialog.getOpenFileNames(self, _tr(self.ext.w, "mf9", "Open"), "",
                                                                 getattr(self.ext.w, "FILTER", "*"))[0]
        self._collect()
        for p in paths:
            self.ext.batch_list.entries.append(B.BatchEntry(p, planned, 0))
        self._load()

    def remove_file(self) -> None:
        r = self.files.currentRow()
        if r >= 0:
            self.files.removeRow(r)

    def reset_done(self) -> None:
        self._collect()
        self.ext.batch_list.reset()
        self._load()

    def _ok(self) -> None:
        self.form.store()
        self.counter.store()
        self._collect()
        self.ext.settings[B.TABLE_KEY] = self.ext.batch_list.to_text()
        self.ext.save_state()
        self.accept()


class MaintainDialog(QDialog):
    COLS = ("A251113_7", "A251113_8", "A251113_9", "A251113_10")

    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext, w = ext, ext.w
        self.setWindowTitle(_tr(w, "A251113_0", "Maintain"))
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        heads = ["", _tr(w, "hp40", "Name")] + [_tr(w, k, k) for k in self.COLS] + ["status"]
        self.table = _table(heads)
        self.table.setColumnHidden(0, True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        b = QPushButton(_tr(w, "A251113_3", "Operation") + " OK")
        b.clicked.connect(self.mark_done)
        row.addWidget(b)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText(_tr(w, "A251113_13", "Customized maintenance items"))
        row.addWidget(self.custom, 1)
        b = QPushButton("+")
        b.clicked.connect(self.add_custom)
        row.addWidget(b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.editors: list[tuple] = []
        self._build()

    def _build(self) -> None:
        w = self.ext.w
        plan = self.ext.maintain_plan
        self.table.setRowCount(len(plan.items))
        self.editors = []
        now, ps = time.time(), self.ext.process_seconds()
        for r, it in enumerate(plan.items):
            self.table.setItem(r, 0, QTableWidgetItem(str(r)))
            group = _tr(w, it.group, it.group)
            name = it.key if it.custom else _tr(w, it.key, it.key)
            self.table.setItem(r, 1, QTableWidgetItem(f"[{group}] {name}"))
            cyc = QDoubleSpinBox()
            cyc.setRange(0, 3650)
            cyc.setValue(it.cycle_days)
            mode = QComboBox()
            mode.addItems([_tr(w, "A251113_11", "World Time"), _tr(w, "A251113_12", "Processing time")])
            mode.setCurrentIndex(it.mode)
            warn = QDoubleSpinBox()
            warn.setRange(0, 3650)
            warn.setValue(it.warn_days)
            prompt = QComboBox()
            prompt.addItems([_tr(w, "A251113_4", "Pop-up prompt"), _tr(w, "A251113_5", "Error bar"),
                             _tr(w, "A251113_6", "Log information bar")])
            prompt.setCurrentIndex(it.prompt)
            for c, ed in enumerate((cyc, mode, warn, prompt), start=2):
                self.table.setCellWidget(r, c, ed)
            self.table.setItem(r, 6, QTableWidgetItem(it.status(now, ps)))
            self.editors.append((it, cyc, mode, warn, prompt))

    def _store(self) -> None:
        for it, cyc, mode, warn, prompt in self.editors:
            it.cycle_days, it.mode, it.warn_days, it.prompt = cyc.value(), mode.currentIndex(), warn.value(), prompt.currentIndex()

    def mark_done(self) -> None:
        self._store()
        rows = {i.row() for i in self.table.selectedIndexes()} or (
            {self.table.currentRow()} if self.table.currentRow() >= 0 else set())
        for r in rows:
            self.ext.maintain_plan.items[r].mark_done(time.time(), self.ext.process_seconds())
        self._build()

    def add_custom(self) -> None:
        self._store()
        name = self.custom.text().strip()
        if name:
            self.ext.maintain_plan.add_custom(name)
            self.custom.clear()
            self._build()

    def _ok(self) -> None:
        self._store()
        self.ext.settings[M.PARAM_KEY] = self.ext.maintain_plan.to_text()
        self.ext.save_state()
        self.accept()


class HelpWindow(QDialog):
    """Offline help from docs/ plus About (mf23 / mf24)."""

    def __init__(self, ext):
        super().__init__(_parent(ext.w))
        self.ext = ext
        self.setWindowTitle(_tr(ext.w, "mf23", "Help"))
        self.resize(980, 680)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("search")
        self.search.returnPressed.connect(self.do_search)
        top.addWidget(self.search, 1)
        lay.addLayout(top)
        sp = QSplitter()
        self.list = QListWidget()
        self.topics = helpdocs.topics()
        for t in self.topics:
            self.list.addItem(t.title)
        self.list.addItem(_tr(ext.w, "mf24", "About"))
        self.list.currentRowChanged.connect(self.show_topic)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)
        sp.addWidget(self.list)
        sp.addWidget(self.view)
        sp.setSizes([240, 740])
        lay.addWidget(sp, 1)
        self.list.setCurrentRow(0)

    def show_topic(self, row: int) -> None:
        if 0 <= row < len(self.topics):
            self.view.setMarkdown(helpdocs.read_topic(self.topics[row].path))
        else:
            self.view.setPlainText(helpdocs.about_text())

    def _link(self, url: QUrl) -> None:
        """Only local documents are followed (no network access)."""
        name = os.path.basename(url.path() or url.toString())
        for i, t in enumerate(self.topics):
            if os.path.basename(t.path) == name:
                self.list.setCurrentRow(i)
                return

    def do_search(self) -> None:
        hits = helpdocs.search(self.search.text())
        self.view.setPlainText("\n".join(f"{os.path.basename(t.path)}:{n}: {ln}" for t, n, ln in hits)
                               or "no match")


# =========================================================================== extension
class DiagExtension:
    def __init__(self, window):
        self.w = window
        if not isinstance(getattr(window, "settings", None), dict):
            window.settings = {}
        self.settings = window.settings
        self.data_dir = data_dir(self.settings)
        self.log_dir = os.path.join(self.data_dir, "Log")
        self.device_settings = None
        self._load_state()
        lang = "zh" if _lang(window) == "zh" else "en"
        self.store = ReportStore(self.data_dir, lang)
        self.counters = DR.DeviceCounters.from_settings(self.settings)
        self.use_timer = DR.UseTimer(self.counters)
        self.watcher = JobWatcher()
        self.stats = StatsRecorder(self.store, self._job_snapshot)
        self.watcher.start_listeners.append(self.stats.on_start)
        self.watcher.start_listeners.append(self._job_started)
        self.watcher.finish_listeners.append(self.stats.on_finish)
        self.watcher.finish_listeners.append(self._job_finished)
        self.measure = DataRecorder()
        self.outputs = CIO.OutputDriver(window.ctl, lambda m: _log(window, m))
        self.production = B.ProductionCounter(self.settings)
        self.batch_list = B.BatchList.from_text(self.settings.get(B.TABLE_KEY, ""))
        self.batch = B.BatchRunner(window, B.params(self.settings), self.batch_list, lambda m: _log(window, m))
        self.maintain_plan = M.MaintainPlan.from_text(self.settings.get(M.PARAM_KEY))
        self.log_writer = L.LogFileWriter(self.log_dir) if self.settings.get("write_log_files", True) else None
        self.windows: dict[str, QWidget] = {}
        self.interferometer_dock: Optional[QDockWidget] = None
        self._attached = None
        self._last_maint = 0.0
        self._announced: set = set()
        self.attach()
        self.timer = QTimer(window if isinstance(window, QWidget) else None)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        if self.log_writer is not None:
            self.log_writer.session_start()

    # ------------------------------------------------------------------ state
    def _state_path(self) -> str:
        return os.path.join(self.data_dir, STATE_FILE)

    def _load_state(self) -> None:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for k, v in data.items():
            self.settings.setdefault(k, v)

    def save_state(self) -> None:
        self.counters.to_settings(self.settings)
        self.settings[M.PARAM_KEY] = self.maintain_plan.to_text()
        self.settings[B.TABLE_KEY] = self.batch_list.to_text()
        keep = {k: v for k, v in self.settings.items()
                if k.startswith(("SOP.", "ST.", "LT.", "SP.Interfere", "GRP.BallArm", "GRP.BatchCut",
                                 "GRP.EnableBatchCut", "GRP.EnableDIInput", "MC.Maintain", "MC.BatchCut",
                                 "DO.CustomDOStr", "DO.ClearAsh")) and _jsonable(v)}
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------------ controller hooks
    def attach(self) -> None:
        ctl = self.w.ctl
        if ctl is self._attached:
            return
        self._attached = ctl
        self.outputs.ctl = ctl
        ctl.add_log_listener(self._on_log)

    def _on_log(self, msg: str) -> None:
        if self.log_writer is not None:
            self.log_writer.write(msg)
        if self.w.ctl is self._attached:
            self.watcher.feed_log(msg)

    def _job_snapshot(self):
        w = self.w
        return (w.document, getattr(w, "toolpath", None), getattr(w, "estimate", None) or None,
                getattr(w, "path", None), getattr(w, "machine", None))

    def _job_started(self, mode: str, info: str) -> None:
        if mode == "process" and not self.production.start_allowed():
            _log(self.w, "pd859: the planned quantity is reached - processing is prohibited")
            QTimer.singleShot(0, self.w.ctl.stop)

    def _job_finished(self, ev) -> None:
        if ev.mode == "process":
            if ev.ok:
                self.counters.add_job(ev.duration_s)
                if self.production.job_done() == "prompt":
                    _log(self.w, f"pd858: planned quantity {self.production.plan} reached")
                    QTimer.singleShot(0, lambda: QMessageBox.information(
                        _parent(self.w), _tr(self.w, "mf705", "Batch Cut"),
                        f"{_tr(self.w, 'pd858', 'Plan Count')}: {self.production.done}/{self.production.plan}"))
            self.save_state()
            self.batch.job_done(ev.ok)

    def process_seconds(self) -> float:
        return self.counters.total_process_h * 3600.0

    def marks(self) -> dict:
        m = {}
        for i in range(1, 7):
            v = self.settings.get(f"mark_P{i}")
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                m[f"P{i}"] = (float(v[0]), float(v[1]))
        return m

    def custom_io(self) -> list:
        return CIO.parse_custom_do(self.settings.get("DO.CustomDOStr", ""))

    def log_dirs(self) -> list[str]:
        dirs = [self.log_dir]
        extra = self.settings.get("log_dirs") or []
        if isinstance(extra, str):
            extra = [extra]
        dirs += [d for d in extra if d]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "NexCut", "Log"))     # the original's mf431 target
        return dirs

    # ------------------------------------------------------------------ timer
    def tick(self) -> None:
        if self.w.ctl is not self._attached:
            self.attach()
        try:
            st = self.w.ctl.state()
        except Exception:  # noqa: BLE001
            return
        self.watcher.poll(st)
        self.use_timer.tick(bool(getattr(st, "connected", False)) and _mc(self.w) is not None)
        if self.measure.enabled:
            self.measure.sample(st)
        self.batch.tick()
        now = time.time()
        if now - self._last_maint > 60:
            self._last_maint = now
            self.check_maintenance(now)

    def check_maintenance(self, now: Optional[float] = None) -> list:
        due = self.maintain_plan.check(now, self.process_seconds())
        for it, status in due:
            tag = (it.key, status)
            if tag in self._announced:
                continue
            self._announced.add(tag)
            name = it.key if it.custom else _tr(self.w, it.key, it.key)
            msg = f"{_tr(self.w, 'A251113_0', 'Maintain')}: {name} ({status})"
            if it.prompt == M.PROMPT_POPUP:
                QTimer.singleShot(0, lambda m=msg: QMessageBox.information(_parent(self.w), "Maintain", m))
            elif it.prompt == M.PROMPT_ERRBAR and hasattr(getattr(self.w, "log_panel", None), "add_alarm"):
                self.w.log_panel.add_alarm(msg)
            _log(self.w, msg)
        return due

    # ------------------------------------------------------------------ commands
    def _show(self, key: str, factory: Callable[[], QWidget]) -> QWidget:
        win = self.windows.get(key)
        if win is None or not _alive(win):
            win = factory()
            self.windows[key] = win
        win.show()
        _raise(win)
        return win

    def mon_controller(self):
        return self._show("controller", lambda: ControllerMonitor(self))

    def mon_ftc(self):
        return self._show("zf", lambda: DeviceMonitor(self, "zf"))

    def mon_af(self):
        return self._show("af", lambda: DeviceMonitor(self, "af"))

    def mon_ext(self):
        return self._show("ec", lambda: DeviceMonitor(self, "ec"))

    def device_report(self):
        """FUN_004b9260: add the card's run info when connected, then show the counters."""
        mc = _mc(self.w)
        if mc is not None and self.w.ctl.state().connected:
            try:
                z = self.settings.get("ZF.ZFType")
                info, mileage = DR.read_card_info(mc, (0, 1), 2 if z else None)
                self.counters.add_card_info(info, mileage, getattr(mc, "pulses_per_mm", 1000.0))
            except Exception as e:  # noqa: BLE001
                _log(self.w, f"mf801: card run info not readable: {e}")
        else:
            _log(self.w, "mf801: hardware is not connected - showing the stored counters")
        self.save_state()
        d = DeviceReportDialog(self)
        d.exec()
        return d

    def work_report(self, all_records: bool = False):
        """FUN_004b96c0: report.txt with the newest / all records, then the viewer."""
        from mlaser.report.window import ReportWindow
        recs = self.stats.selection(all_records)
        self.store.lang = "zh" if _lang(self.w) == "zh" else "en"
        self.store.write_report_txt(recs)
        if self.w.document is not None and recs and not os.path.exists(self.store.thumbnail_path(recs[-1].name)):
            try:
                from mlaser.report.thumbnail import save_thumbnail
                save_thumbnail(self.w.document, self.store.thumbnail_path(recs[-1].name))
            except Exception:  # noqa: BLE001
                pass
        win = self.windows.get("report")
        if win is not None and _alive(win):
            win.reload()
        else:
            win = ReportWindow(self.store)
            self.windows["report"] = win
        win.show()
        _raise(win)
        return win

    def log_dir_viewer(self):
        return self._show("logs", lambda: LogViewer(self))

    def toggle_measure(self) -> bool:
        """mf229: toggles the measurement switch (cfg+0x4327) and the recorder."""
        if self.measure.enabled:
            self.measure.stop()
            _log(self.w, "mf229: data measurement off")
        else:
            self.measure.start()
            self._show("measure", lambda: MeasureWindow(self))
            _log(self.w, "mf229: data measurement on")
        return self.measure.enabled

    def custom_io_dialog(self, self_locking: bool = True):
        d = CustomIODialog(self, self_locking)
        d.exec()
        return d

    def dedust(self, on: Optional[bool] = None) -> bool:
        """FUN_004bc020: toggle DO.ClearAsh; caption mf171 Enable / mf170 Disable."""
        port = CIO.dedust_port(self.settings, self.w.ctl)
        if port is None:
            _log(self.w, "mf140: no dedust output configured (DO.ClearAsh pd1400)")
            state = False
        else:
            if on is None:
                state = self.outputs.toggle(port)
            else:
                self.outputs.set(port, bool(on))
                state = bool(on)
            try:
                self.w.ctl.state().outputs["dedust"] = state
            except Exception:  # noqa: BLE001
                pass
        act = self.w.ribbon.actions.get("aux.dedust")
        if act is not None:
            act.blockSignals(True)
            act.setChecked(state)
            act.setText(_tr(self.w, "mf170" if state else "mf171", "Disable Dedust" if state else "Enable Dedust"))
            act.blockSignals(False)
        return state

    def light_test(self):
        d = LightTestDialog(self)
        d.exec()
        return d

    def burn_in(self):
        d = BurnInDialog(self)
        d.exec()
        return d

    def ball_arm(self):
        """FUN_004bbd20: parameter grid, then CAD slot +0xcc replaces the drawing."""
        p = ballarm.params(self.settings)
        d = ParamDialog(self.w, _tr(self.w, "mf702", "Ball Arm"), p)
        if d.exec():
            return self.apply_ball_arm(p)
        return None

    def apply_ball_arm(self, p: ParamSet):
        doc = ballarm.make_document(p, getattr(self.w.document, "layers", None))
        snap = getattr(self.w, "_snapshot", None)
        if callable(snap):
            snap()
        self.w.set_document(doc)
        _log(self.w, f"mf702: ball arm pattern R={p['GRP.BallArmCircleRadius']:g} x{p['GRP.BallArmCircleTimes']}")
        return doc

    def interferometer(self):
        """FUN_004bbc60: toggle the interferometer panel (cfg+0x40b8)."""
        if self.interferometer_dock is None:
            dock = QDockWidget(_tr(self.w, "mf180", "Laser Interferometer"), _parent(self.w))
            dock.setObjectName("diag_interferometer")
            dock.setWidget(InterferometerPanel(self))
            if isinstance(self.w, QMainWindow):
                self.w.addDockWidget(Qt.RightDockWidgetArea, dock)
            self.interferometer_dock = dock
            dock.show()
        else:
            self.interferometer_dock.setVisible(self.interferometer_dock.isHidden())
        self.settings["SP.InterfereShow"] = not self.interferometer_dock.isHidden()
        return self.interferometer_dock

    def batch_cut(self):
        d = BatchDialog(self)
        if d.exec() and bool(self.batch.p["GRP.EnableBatchCut"]):
            self.batch.list = self.batch_list
            self.batch.start()
        return d

    def maintain(self):
        d = MaintainDialog(self)
        d.exec()
        return d

    def help(self):
        return self._show("help", lambda: HelpWindow(self))


def data_dir(settings: dict) -> str:
    """``settings["data_dir"]``, ``$MLASER_DATA_DIR`` or ``~/.mlaser`` (a per-process temporary
    directory while pytest runs, so test windows never write into the home directory)."""
    d = settings.get("data_dir") or os.environ.get("MLASER_DATA_DIR")
    if d:
        return d
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        import tempfile
        return os.path.join(tempfile.gettempdir(), f"mlaser-test-{os.getpid()}")
    return os.path.join(os.path.expanduser("~"), ".mlaser")


def _raise(w) -> None:
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() != "offscreen":
        w.raise_()


def _alive(w) -> bool:
    try:
        w.isVisible()
        return True
    except RuntimeError:
        return False


def _jsonable(v) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


COMMANDS = {
    "mon.controller": "mon_controller", "mon.ftc": "mon_ftc", "mon.af": "mon_af", "mon.ext": "mon_ext",
    "diag.device_report": "device_report", "diag.work_report": ("work_report", False),
    "diag.work_report_all": ("work_report", True), "diag.log_dir": "log_dir_viewer",
    "diag.measure": "toggle_measure", "aux.custom_io": ("custom_io_dialog", True),
    "aux.custom_io_nl": ("custom_io_dialog", False), "aux.dedust": "dedust",
    "aux.light_test": "light_test", "aux.burn_in": "burn_in", "aux.ball_arm": "ball_arm",
    "aux.interferometer": "interferometer", "aux.batch_cut": "batch_cut", "aux.maintain": "maintain",
    "app.help": "help",
}


def _handler(ext: DiagExtension, spec):
    if isinstance(spec, tuple):
        name, arg = spec
        return lambda _=False: getattr(ext, name)(arg)
    if spec == "dedust":
        return lambda _=False: ext.dedust()
    return lambda _=False: getattr(ext, spec)()


def register(window) -> DiagExtension:
    """Re-bind the monitor / diagnostics / auxiliary commands of ``window`` (MainWindow)."""
    import warnings
    ext = DiagExtension(window)
    window.diag_ext = ext
    handlers = getattr(window, "handlers", None)
    for cmd, spec in COMMANDS.items():
        fn = _handler(ext, spec)
        act = window.ribbon.actions.get(cmd)
        if act is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                try:
                    act.triggered.disconnect()
                except (RuntimeError, TypeError):
                    pass
            act.triggered.connect(fn)
        if isinstance(handlers, dict):
            handlers[cmd] = fn
    act = window.ribbon.actions.get("aux.dedust")
    if act is not None:
        act.setText(_tr(window, "mf171", "Enable Dedust"))
    orig = getattr(window, "set_controller", None)
    if callable(orig):
        def set_controller(ctl, _orig=orig):
            _orig(ctl)
            ext.attach()
        window.set_controller = set_controller
    if isinstance(window, QWidget):
        window.destroyed.connect(lambda *_: ext.save_state())
    return ext
