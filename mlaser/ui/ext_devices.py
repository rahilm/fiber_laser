"""Peripheral device dialogs (follower calibration, laser, AF/EC, pendant).

``register(ctx)`` (PluginContext) binds ``dev.follower`` / ``dev.laser`` / ``dev.pendant`` in a
"Devices" ribbon tab and the existing ``hw.remoter_match`` command ("Remoter Match", lang mf135)
to the pendant dialog.  The pendant's state lives on the :class:`DevicesExtension`, not on the
window.
Everything talks to devices through :mod:`mlaser.devices`; the motion card connection is taken
from ``window.ctl.card()`` (the supervised card handle) when the UI controller is connected (otherwise the follower dialogs work
on files only).  Evidence for every command: docs/re/devices.md.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QLabel,
                               QLineEdit, QPlainTextEdit, QSpinBox, QTableWidget,
                               QTableWidgetItem, QWidget, QHeaderView)

from mlaser.devices import pendant as P
from mlaser.devices.follower import STATUS_FIELDS, CalibrationTable, FollowerStatus, OnBoardFollower
from mlaser.devices.laser import (LASER_IPG_NET, LASER_RAYCUS_NET, IpgLaser, LaserError, RaycusLaser)
from mlaser.devices.transport import DeviceSettings

from . import style as S
from .responsive import flow
from .style import Dialog



def _mc(window):
    """The supervised card handle, None without a card."""
    return window.ctl.card()                 # MachineAPI: None without a card


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
        p.setPen(QPen(self.palette().highlight().color(), 2))     # palette: dark theme safe
        p.drawPolyline(poly)
        p.setPen(QPen(self.palette().text().color(), 1))
        p.drawText(2, m + 10, str(y1))
        p.drawText(2, m + h, str(y0))


class FollowerDialog(Dialog):
    """Follower status, follow on/off and the calibration table (Help/calib.csv)."""

    def __init__(self, window, path: Optional[str] = None, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window), "Height follower",
                         buttons=QDialogButtonBox.Close)
        self.window_ = window
        self.path = path
        self.table = CalibrationTable()

        st, sl = S.section("Status (card registers 10000..10017)")
        row = S.hbox()
        self.status_lbl = QLabel("Not connected")
        self.status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_lbl.setWordWrap(True)
        row.addWidget(self.status_lbl, 1)
        row.addWidget(S.button("Refresh", on_click=self.refresh))
        sl.addLayout(row)
        self.body.addWidget(st)

        fsec, fl = S.section("Follow")
        fh = QWidget()
        ff = S.form(fh)
        self.follow_value = QDoubleSpinBox()
        self.follow_value.setRange(0, 100000)
        self.follow_value.setValue(1000)
        S.form_row(ff, "Follow value", self.follow_value)
        fl.addWidget(fh)
        fl.addWidget(flow([S.button(text, on_click=fn) for text, fn in (
            ("Follow on", lambda: self._follow(True)), ("Follow off", lambda: self._follow(False)),
            ("Calibrate", self._calibrate), ("Cancel calibration", self._cancel),
            ("Read curve", self._read_curve))], justify=False))
        self.body.addWidget(fsec)

        csec, cl = S.section("Calibration table")
        cal = S.hbox()
        self.grid = QTableWidget(0, 2)
        self.grid.setHorizontalHeaderLabels(["Height", "Raw"])
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.verticalHeader().hide()
        self.grid.itemChanged.connect(self._grid_changed)
        cal.addWidget(self.grid, 1)
        self.plot = CurvePlot()
        cal.addWidget(self.plot, 2)
        cl.addLayout(cal)
        ref_row = S.hbox()
        self.ref = QSpinBox()
        self.ref.setRange(-2 ** 31, 2 ** 31 - 1)
        ref_lab = QLabel(S.label_text("Reference"))
        ref_lab.setBuddy(self.ref)
        ref_row.addWidget(ref_lab)
        ref_row.addWidget(self.ref)
        ref_row.addStretch(1)
        ref_row.addWidget(S.button("Load…", on_click=self.load))
        ref_row.addWidget(S.button("Save…", on_click=self.save))
        cl.addLayout(ref_row)
        self.body.addWidget(csec, 1)
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
            self.status_lbl.setText("Not connected")
            return
        try:
            st = zf.read_status()
        except Exception as e:  # device errors are shown, not raised
            self.status_lbl.setText(f"Error: {e}")
            return
        fs = FollowerStatus(st)
        parts = [f"{n}={fs.value(n):g}" for n, _ in STATUS_FIELDS]
        parts.append(f"homed={fs.homed} calibration={fs.calibration_grade}")
        self.status_lbl.setText("\n".join(", ".join(parts[i:i + 4]) for i in range(0, len(parts), 4)))

    def _follow(self, on: bool) -> None:
        zf = self._zf()
        if zf is not None:
            zf.follow(on, self.follow_value.value())
            _log(self.window_, f"follower {'on' if on else 'off'}")

    def _calibrate(self) -> None:
        zf = self._zf()
        if zf is not None:
            if not zf.status().homed:
                self.status_lbl.setText("The follower is not homed: calibration refused (zf49)")
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
                self.status_lbl.setText(f"Error: {e}")


class LaserDialog(Dialog):
    """Laser command channel (emission, aiming beam, power/current, raw query)."""

    def __init__(self, window, settings: Optional[DeviceSettings] = None, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window), "Laser source",
                         buttons=QDialogButtonBox.Close)
        self.window_ = window
        self.settings = settings or DeviceSettings()
        self.client = None
        f = self.form()
        self.kind = QComboBox()
        self.kind.addItem("Raycus via card serial (control type 1)", LASER_RAYCUS_NET)
        self.kind.addItem("IPG over network (control type 2)", LASER_IPG_NET)
        S.form_row(f, "Laser", self.kind)
        self.host = QLineEdit(self.settings.onb_laser[0])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.settings.onb_laser[1])
        self.kind.currentIndexChanged.connect(self._endpoint)
        S.form_row(f, "Host", self.host)
        S.form_row(f, "Port", self.port)
        self.power = QDoubleSpinBox()
        self.power.setRange(0, 100)
        self.power.setValue(50)
        self.power.setSuffix(" %")
        S.form_row(f, "Power or current", self.power)
        self.body.addWidget(flow([S.button(text, on_click=fn) for text, fn in (
            ("Emission on", lambda: self._do("emission", True)),
            ("Emission off", lambda: self._do("emission", False)),
            ("Aiming beam on", lambda: self._do("aim", True)), ("Aiming beam off", lambda: self._do("aim", False)),
            ("Set power", lambda: self._do("power")))], justify=False))
        q = S.hbox()
        self.cmd = QLineEdit("STA")
        self.cmd.setAccessibleName("IPG command")
        q.addWidget(self.cmd, 1)
        q.addWidget(S.button("Send (IPG)", on_click=lambda: self._do("query")))
        self.body.addLayout(q)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.body.addWidget(self.out, 1)

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
                    self.out.appendPlainText("Aiming beam: IPG only (the Raycus adapter has none)")
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


class DevicesExtension:
    """Plugin state: the open pendant, its event pump and the last key events."""

    def __init__(self, window) -> None:
        self.window = window
        self.pendant = None
        self.pendant_timer: Optional[QTimer] = None
        self.pendant_events: list[str] = []


def _ext(window) -> DevicesExtension:
    ext = window.extension("ext_devices") if hasattr(window, "extension") else None
    if ext is None:                    # a dialog opened without the plugin (tests, scripts)
        ext = getattr(window, "_devices_fallback", None)
        if ext is None:
            ext = DevicesExtension(window)
            try:
                window._devices_fallback = ext
            except AttributeError:
                pass
    return ext


class PendantDialog(Dialog):
    """Enable the XHC wireless pendant and show its status/key events."""

    def __init__(self, window, parent=None):
        avail = P.hid_available()
        super().__init__(parent if parent is not None else _as_parent(window), "Wireless pendant",
                         buttons=QDialogButtonBox.Close,
                         explanation="hidapi is available." if avail else
                         "hidapi is not installed (pip install hidapi): the pendant cannot be used.")
        self.window_ = window
        self.ext = _ext(window)
        self.info = self.explanation
        self.enable = QCheckBox("Enable pendant")
        self.enable.setChecked(self.ext.pendant is not None)
        S.set_enabled(self.enable, avail, "" if avail else "Install hidapi to use the pendant")
        self.enable.toggled.connect(self._toggle)
        self.body.addWidget(self.enable)
        self.status = QLabel("–")
        self.body.addWidget(self.status)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.body.addWidget(self.events, 1)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._show_status)
        self._timer.start(500)

    def _toggle(self, on: bool) -> None:
        if on:
            ok = start_pendant(self.window_)
            if not ok:
                self.enable.setChecked(False)
                self.status.setText("No pendant found (VID 0x10CE)")
        else:
            stop_pendant(self.window_)

    def _show_status(self) -> None:
        pend = self.ext.pendant
        if pend is None:
            return
        s = pend.status
        self.status.setText(f"DevID {s.dev_id:#06x}  channel {s.channel}  RSSI {s.rssi}"
                            + ("  (poor signal)" if s.poor_signal else ""))
        for ev in self.ext.pendant_events[-20:]:
            self.events.appendPlainText(ev)
        self.ext.pendant_events.clear()


def start_pendant(window) -> bool:
    """Open the pendant and map its keys to jog/actions on ``window.ctl`` (MainApp FUN_005ab2e0).
    Events arrive on the reader thread and are marshalled to the GUI thread with a timer.  Start
    and Frame run the planned job (``window.run_job``) like the run controls."""
    ext = _ext(window)
    if ext.pendant is not None:
        return True
    queue: list = []
    pend = P.Pendant(on_event=queue.append)
    if not pend.open():
        return False
    ctl = window.ctl

    def run(mode):
        fn = getattr(window, "run_job", None)
        if callable(fn):
            fn(mode)
        else:
            (ctl.start if mode == "process" else ctl.frame)(window.document)
    actions = {
        "home": lambda: ctl.home("XY"), "stop": ctl.stop, "pause_resume": ctl.pause,
        "start": lambda: run("process"), "frame": lambda: run("frame"),
    }
    mapper = P.PendantJogMapper(ctl, actions, busy=lambda: ctl.job_active() or ctl.state().mode in ("process", "frame", "dry_run"),
                                leds=pend.set_leds, dev_id=lambda: pend.status.dev_id)
    mapper.attach(pend)              # dead-man: HID error stops the jog, keep-alive while held
    ext.pendant_events = []

    def pump():
        while queue:
            ev = queue.pop(0)
            act = mapper.handle(ev)
            ext.pendant_events.append(f"keys {ev.key1:#04x} {ev.key2:#04x} -> {act}")

    t = QTimer(window)
    t.timeout.connect(pump)
    t.start(20)
    ext.pendant, ext.pendant_timer = pend.start(), t
    _log(window, "pendant opened")
    return True


def stop_pendant(window) -> None:
    ext = _ext(window)
    pend = ext.pendant
    if pend is not None:
        pend.close()
        if ext.pendant_timer is not None:
            ext.pendant_timer.stop()
        ext.pendant = None
        _log(window, "pendant closed")


def register(ctx) -> DevicesExtension:
    """The "Devices" tab and ``hw.remoter_match`` (``ctx``: PluginContext)."""
    window = ctx.window
    ext = DevicesExtension(window)
    cmds = {"dev.follower": ("Follower", lambda: FollowerDialog(window).exec()),
            "dev.laser": ("Laser", lambda: LaserDialog(window).exec()),
            "dev.pendant": ("Pendant", lambda: PendantDialog(window).exec())}
    for cmd, (label, fn) in cmds.items():
        ctx.register_command(cmd, fn, label=label)
    ctx.add_ribbon_tab("Devices", list(cmds))
    ctx.register_command("hw.remoter_match", lambda: PendantDialog(window).exec())
    ctx.on_close(lambda: stop_pendant(window))
    return ext
