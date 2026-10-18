"""Peripheral device dialogs (follower calibration, laser, AF/EC, pendant).

``register(window)`` adds a "Devices" ribbon tab whose actions open the dialogs below, and wires
the existing ``hw.remoter_match`` action ("Remoter Match", lang mf135) to the pendant dialog.
Everything talks to devices through :mod:`mlaser.devices`; the motion card connection is taken
from ``window.ctl.mc`` when the UI controller is connected (otherwise the follower dialogs work
on files only).  Evidence for every command: docs/devices.md.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QHeaderView)

from mlaser.devices import pendant as P
from mlaser.devices.follower import STATUS_FIELDS, CalibrationTable, FollowerStatus, OnBoardFollower
from mlaser.devices.laser import (LASER_IPG_NET, LASER_RAYCUS_NET, IpgLaser, LaserError, RaycusLaser)
from mlaser.devices.transport import DeviceSettings

from .icons import command_icon


def _mc(window):
    ctl = getattr(window, "ctl", None)
    return getattr(ctl, "mc", None)


def _as_parent(window):
    return window if isinstance(window, QWidget) else None


def _log(window, msg: str) -> None:
    lp = getattr(window, "log_panel", None)
    if lp is not None:
        lp.log(msg)


class CurvePlot(QWidget):
    """Height vs raw-capacitance plot for the calibration table (no plotting dependency)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[tuple[int, int]] = []
        self.setMinimumSize(320, 220)

    def set_points(self, pts) -> None:
        self.points = sorted(pts)
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().base())
        m = 30
        w, h = self.width() - 2 * m, self.height() - 2 * m
        p.setPen(QPen(self.palette().text().color(), 1))
        p.drawRect(m, m, w, h)
        p.drawText(m, m - 8, "raw")
        p.drawText(m + w - 60, m + h + 20, "height (um)")
        if len(self.points) < 2:
            return
        xs = [a for a, _ in self.points]
        ys = [b for _, b in self.points]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        sx = w / max(1, x1 - x0)
        sy = h / max(1, y1 - y0)
        poly = QPolygonF([QPointF(m + (x - x0) * sx, m + h - (y - y0) * sy) for x, y in self.points])
        p.setPen(QPen(Qt.blue, 2))
        p.drawPolyline(poly)
        p.setPen(QPen(self.palette().text().color(), 1))
        p.drawText(2, m + 10, str(y1))
        p.drawText(2, m + h, str(y0))


class FollowerDialog(QDialog):
    """Follower status, follow on/off and the calibration table (Help/calib.csv)."""

    def __init__(self, window, path: Optional[str] = None, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window))
        self.window_ = window
        self.setWindowTitle("Height follower")
        self.path = path
        self.table = CalibrationTable()
        lay = QVBoxLayout(self)

        st = QGroupBox("Status (card registers 10000..10017)")
        sl = QHBoxLayout(st)
        self.status_lbl = QLabel("not connected")
        self.status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.status_lbl, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        sl.addWidget(b)
        lay.addWidget(st)

        ctl = QHBoxLayout()
        self.height = QDoubleSpinBox()
        self.height.setRange(0, 100000)
        self.height.setValue(1000)
        ctl.addWidget(QLabel("Follow value"))
        ctl.addWidget(self.height)
        for text, fn in (("Follow on", lambda: self._follow(True)), ("Follow off", lambda: self._follow(False)),
                         ("Calibrate", self._calibrate), ("Cancel", self._cancel),
                         ("Read curve", self._read_curve)):
            bb = QPushButton(text)
            bb.clicked.connect(fn)
            ctl.addWidget(bb)
        lay.addLayout(ctl)

        cal = QHBoxLayout()
        self.grid = QTableWidget(0, 2)
        self.grid.setHorizontalHeaderLabels(["Height", "Raw"])
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.itemChanged.connect(self._grid_changed)
        cal.addWidget(self.grid, 1)
        self.plot = CurvePlot()
        cal.addWidget(self.plot, 2)
        lay.addLayout(cal)
        row = QHBoxLayout()
        self.ref = QSpinBox()
        self.ref.setRange(-2 ** 31, 2 ** 31 - 1)
        row.addWidget(QLabel("Reference"))
        row.addWidget(self.ref)
        for text, fn in (("Load...", self.load), ("Save...", self.save)):
            bb = QPushButton(text)
            bb.clicked.connect(fn)
            row.addWidget(bb)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if path:
            try:
                self.set_table(CalibrationTable.load(path))
            except OSError:
                pass

    def set_table(self, t: CalibrationTable) -> None:
        self.table = t
        self.ref.setValue(t.reference)
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(t.points))
        for i, (h, r) in enumerate(t.points):
            self.grid.setItem(i, 0, QTableWidgetItem(str(h)))
            self.grid.setItem(i, 1, QTableWidgetItem(str(r)))
        self.grid.blockSignals(False)
        self.plot.set_points(t.points)

    def _grid_changed(self, *_):
        pts = []
        for i in range(self.grid.rowCount()):
            try:
                pts.append((int(self.grid.item(i, 0).text()), int(self.grid.item(i, 1).text())))
            except (AttributeError, ValueError):
                continue
        self.table.points = pts
        self.plot.set_points(pts)

    def load(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Calibration table", self.path or "", "CSV (*.csv)")
        if p:
            self.path = p
            self.set_table(CalibrationTable.load(p))

    def save(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Calibration table", self.path or "calib.csv", "CSV (*.csv)")
        if p:
            self.table.reference = self.ref.value()
            self.table.save(p)
            self.path = p

    def _zf(self) -> Optional[OnBoardFollower]:
        mc = _mc(self.window_)
        return OnBoardFollower(mc) if mc is not None else None

    def refresh(self) -> None:
        zf = self._zf()
        if zf is None:
            self.status_lbl.setText("not connected")
            return
        try:
            st = zf.read_status()
        except Exception as e:  # device errors are shown, not raised
            self.status_lbl.setText(f"error: {e}")
            return
        fs = FollowerStatus(st)
        parts = [f"{n}={fs.value(n):g}" for n, _ in STATUS_FIELDS]
        parts.append(f"homed={fs.homed} calibration={fs.calibration_grade}")
        self.status_lbl.setText("\n".join(", ".join(parts[i:i + 4]) for i in range(0, len(parts), 4)))

    def _follow(self, on: bool) -> None:
        zf = self._zf()
        if zf is not None:
            zf.follow(on, self.height.value())
            _log(self.window_, f"follower {'on' if on else 'off'}")

    def _calibrate(self) -> None:
        zf = self._zf()
        if zf is not None:
            if not zf.status().homed:
                self.status_lbl.setText("follower not homed: calibration refused (zf49)")
                return
            zf.calibrate()
            _log(self.window_, "follower: head calibration started (101 := 107)")

    def _cancel(self) -> None:
        zf = self._zf()
        if zf is not None:
            zf.cancel_calibration()

    def _read_curve(self) -> None:
        zf = self._zf()
        if zf is not None:
            try:
                self.set_table(zf.read_calibration_curve())
            except Exception as e:
                self.status_lbl.setText(f"error: {e}")


class LaserDialog(QDialog):
    """Laser command channel (emission, aiming beam, power/current, raw query)."""

    def __init__(self, window, settings: Optional[DeviceSettings] = None, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window))
        self.window_ = window
        self.settings = settings or DeviceSettings()
        self.client = None
        self.setWindowTitle("Laser source")
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("Raycus via card serial (control type 1)", LASER_RAYCUS_NET)
        self.kind.addItem("IPG over network (control type 2)", LASER_IPG_NET)
        f.addRow("Laser", self.kind)
        self.host = QLineEdit(self.settings.onb_laser[0])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.settings.onb_laser[1])
        self.kind.currentIndexChanged.connect(self._endpoint)
        f.addRow("Host", self.host)
        f.addRow("Port", self.port)
        self.power = QDoubleSpinBox()
        self.power.setRange(0, 100)
        self.power.setValue(50)
        f.addRow("Power / current %", self.power)
        lay.addLayout(f)
        row = QHBoxLayout()
        for text, fn in (("Emission on", lambda: self._do("emission", True)),
                         ("Emission off", lambda: self._do("emission", False)),
                         ("Aim on", lambda: self._do("aim", True)), ("Aim off", lambda: self._do("aim", False)),
                         ("Set power", lambda: self._do("power"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        q = QHBoxLayout()
        self.cmd = QLineEdit("STA")
        b = QPushButton("Send (IPG)")
        b.clicked.connect(lambda: self._do("query"))
        q.addWidget(self.cmd)
        q.addWidget(b)
        lay.addLayout(q)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _endpoint(self) -> None:
        ep = self.settings.laser if self.kind.currentData() == LASER_IPG_NET else self.settings.onb_laser
        self.host.setText(ep[0])
        self.port.setValue(ep[1])
        self.client = None

    def _client(self):
        if self.client is None:
            cls = IpgLaser if self.kind.currentData() == LASER_IPG_NET else RaycusLaser
            self.client = cls.connect(self.host.text(), self.port.value(), self.settings.access,
                                      self.settings.laser_timeout_ms, self.settings.laser_max_send)
        return self.client

    def _do(self, what: str, on: bool = False) -> None:
        try:
            c = self._client()
            if what == "emission":
                r = c.emission(on)
            elif what == "aim":
                if not isinstance(c, IpgLaser):
                    self.out.appendPlainText("aiming beam: IPG only (Raycus adapter slots are empty)")
                    return
                r = c.aiming_beam(on)
            elif what == "power":
                r = c.set_power(self.power.value()) if isinstance(c, RaycusLaser) else c.set_current(self.power.value())
            else:
                if not isinstance(c, IpgLaser):
                    return
                r = c.query(self.cmd.text())
            self.out.appendPlainText(f"{what}: {r!r}")
        except (LaserError, OSError) as e:
            self.out.appendPlainText(f"{what}: {e}")

    def done(self, r) -> None:
        if self.client is not None:
            self.client.close()
        super().done(r)


class PendantDialog(QDialog):
    """Enable the XHC wireless pendant and show its status/key events."""

    def __init__(self, window, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window))
        self.window_ = window
        self.setWindowTitle("Wireless pendant")
        lay = QVBoxLayout(self)
        avail = P.hid_available()
        self.info = QLabel("hidapi available" if avail else "hidapi not installed (pip install hidapi)")
        lay.addWidget(self.info)
        self.enable = QCheckBox("Enable pendant")
        self.enable.setEnabled(avail)
        self.enable.setChecked(getattr(window, "_pendant", None) is not None)
        self.enable.toggled.connect(self._toggle)
        lay.addWidget(self.enable)
        self.status = QLabel("-")
        lay.addWidget(self.status)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        lay.addWidget(self.events)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._show_status)
        self._timer.start(500)

    def _toggle(self, on: bool) -> None:
        if on:
            ok = start_pendant(self.window_)
            if not ok:
                self.enable.setChecked(False)
                self.status.setText("no pendant found (VID 0x10CE)")
        else:
            stop_pendant(self.window_)

    def _show_status(self) -> None:
        pend = getattr(self.window_, "_pendant", None)
        if pend is None:
            return
        s = pend.status
        self.status.setText(f"DevID {s.dev_id:#06x}  channel {s.channel}  RSSI {s.rssi}"
                            + ("  (poor signal)" if s.poor_signal else ""))
        for ev in getattr(self.window_, "_pendant_events", [])[-20:]:
            self.events.appendPlainText(ev)
        if hasattr(self.window_, "_pendant_events"):
            self.window_._pendant_events.clear()


def start_pendant(window) -> bool:
    """Open the pendant and map its keys to jog/actions on ``window.ctl`` (MainApp FUN_005ab2e0).
    Events arrive on the reader thread and are marshalled to the GUI thread with a timer."""
    if getattr(window, "_pendant", None) is not None:
        return True
    queue: list = []
    pend = P.Pendant(on_event=queue.append)
    if not pend.open():
        return False
    ctl = window.ctl
    actions = {
        "home": lambda: ctl.home("XY"), "stop": ctl.stop, "pause_resume": ctl.pause,
        "start": lambda: ctl.start(window.document), "frame": lambda: ctl.frame(window.document),
    }
    mapper = P.PendantJogMapper(ctl, actions, busy=lambda: ctl.state().mode in ("process", "frame", "dry_run"),
                                leds=pend.set_leds, dev_id=lambda: pend.status.dev_id)
    window._pendant_events = []

    def pump():
        while queue:
            ev = queue.pop(0)
            act = mapper.handle(ev)
            window._pendant_events.append(f"keys {ev.key1:#04x} {ev.key2:#04x} -> {act}")

    t = QTimer(window)
    t.timeout.connect(pump)
    t.start(20)
    window._pendant, window._pendant_timer = pend.start(), t
    _log(window, "pendant opened")
    return True


def stop_pendant(window) -> None:
    pend = getattr(window, "_pendant", None)
    if pend is not None:
        pend.close()
        window._pendant_timer.stop()
        window._pendant = None
        _log(window, "pendant closed")


def register(window) -> None:
    """Add the "Devices" ribbon tab and connect ``hw.remoter_match``."""
    page = QWidget()
    lay = QHBoxLayout(page)
    lay.setContentsMargins(2, 2, 2, 0)
    window.device_actions = {}
    for cmd, text, fn in (
        ("dev.follower", "Follower", lambda: FollowerDialog(window).exec()),
        ("dev.laser", "Laser", lambda: LaserDialog(window).exec()),
        ("dev.pendant", "Pendant", lambda: PendantDialog(window).exec()),
    ):
        act = QAction(text, window)
        act.setObjectName(cmd)
        act.setIcon(command_icon(cmd))
        act.triggered.connect(fn)
        window.device_actions[cmd] = act
        tb = QToolButton()
        tb.setDefaultAction(act)
        tb.setAutoRaise(True)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(32, 32))
        lay.addWidget(tb)
    lay.addStretch(1)
    window.ribbon.addTab(page, "Devices")
    rm = window.ribbon.actions.get("hw.remoter_match")
    if rm is not None:
        try:
            rm.triggered.disconnect()
        except (RuntimeError, TypeError):
            pass
        rm.triggered.connect(lambda _=False: PendantDialog(window).exec())
