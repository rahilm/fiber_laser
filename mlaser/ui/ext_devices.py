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
                               QTableWidgetItem, QWidget, QHeaderView, QStyle)

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


def _tr(window, key: str, default: str, **values) -> str:
    """Text of ``key`` in the window's language (``default`` without a translator)."""
    tr = getattr(window, "tr_", None)
    if tr is None:
        from mlaser.ui.i18n import translator
        tr = translator()
    try:
        if tr.has(key):
            return tr.format(key, **values) if values else tr.tr(key)
    except Exception:  # noqa: BLE001
        pass
    return default.format(**values) if values else default


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
        self._a11y()
        self.update()

    def _a11y(self) -> None:
        """Accessible name and the table's range as text (F7: the curve is painted)."""
        from . import a11y
        t = a11y.translator_for(self)
        if self.points:
            raws = [a for a, _ in self.points]
            hs = [b for _, b in self.points]
            desc = t.plural("a11y.curve.desc", len(self.points), raw_min=min(raws), raw_max=max(raws),
                            h_min=min(hs), h_max=max(hs))
        else:
            desc = t.tr("a11y.curve.empty")
        a11y.name(self, t.tr("a11y.curve.name"), desc)

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self._a11y()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().base())
        m = 30
        w, h = self.width() - 2 * m, self.height() - 2 * m
        p.setPen(QPen(self.palette().text().color(), 1))
        p.drawRect(m, m, w, h)
        p.drawText(m, m - 8, _tr(None, "devices.follower.plot.raw", "raw"))
        p.drawText(m + w - 60, m + h + 20, _tr(None, "devices.follower.plot.height", "height (um)"))
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
        super().__init__(parent if parent is not None else _as_parent(window),
                         _tr(window, "devices.follower.title", "Height follower"),
                         buttons=QDialogButtonBox.Close)
        self.window_ = window
        self.path = path
        self.table = CalibrationTable()
        w = window

        st, sl = S.section(_tr(w, "devices.follower.status", "Status (card registers 10000..10017)"))
        row = S.hbox()
        self.status_lbl = QLabel(_tr(w, "devices.not_connected", "Not connected"))
        self.status_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_lbl.setWordWrap(True)
        row.addWidget(self.status_lbl, 1)
        row.addWidget(S.button(_tr(w, "devices.follower.refresh", "Refresh"), on_click=self.refresh))
        sl.addLayout(row)
        self.body.addWidget(st)

        fsec, fl = S.section(_tr(w, "devices.follower.follow", "Follow"))
        fh = QWidget()
        ff = S.form(fh)
        self.follow_value = QDoubleSpinBox()
        self.follow_value.setRange(0, 100000)
        self.follow_value.setValue(1000)
        S.form_row(ff, _tr(w, "devices.follower.follow_value", "Follow value"), self.follow_value)
        fl.addWidget(fh)
        fl.addWidget(flow([S.button(_tr(w, f"devices.follower.{key}", text), on_click=fn) for key, text, fn in (
            ("follow_on", "Follow on", lambda: self._follow(True)),
            ("follow_off", "Follow off", lambda: self._follow(False)),
            ("calibrate", "Calibrate", self._calibrate), ("cancel_calibration", "Cancel calibration", self._cancel),
            ("read_curve", "Read curve", self._read_curve))], justify=False))
        self.body.addWidget(fsec)

        csec, cl = S.section(_tr(w, "devices.follower.cal_table", "Calibration table"))
        cal = S.hbox()
        self.grid = S.table_look(QTableWidget(0, 2), numeric=(0, 1))
        self.grid.setHorizontalHeaderLabels([_tr(w, "devices.follower.col.height", "Height"),
                                             _tr(w, "devices.follower.col.raw", "Raw")])
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.grid.verticalHeader().hide()
        # both columns stretch: at least as wide as their headers (font-based), so the table
        # never scrolls sideways at the dialog's own size (Windows CI)
        hh = self.grid.horizontalHeader()
        self.grid.setMinimumWidth(sum(hh.sectionSizeHint(i) for i in range(self.grid.columnCount()))
                                  + 2 * self.grid.frameWidth()
                                  + self.grid.style().pixelMetric(QStyle.PM_ScrollBarExtent, None, self.grid))
        self.grid.itemChanged.connect(self._grid_changed)
        cal.addWidget(self.grid, 1)
        self.plot = CurvePlot()
        cal.addWidget(self.plot, 2)
        cl.addLayout(cal)
        ref_row = S.hbox()
        self.ref = QSpinBox()
        self.ref.setRange(-2 ** 31, 2 ** 31 - 1)
        ref_lab = QLabel(S.label_text(_tr(w, "devices.follower.reference", "Reference")))
        ref_lab.setBuddy(self.ref)
        ref_row.addWidget(ref_lab)
        ref_row.addWidget(self.ref)
        ref_row.addStretch(1)
        ref_row.addWidget(S.button(_tr(w, "devices.follower.load", "Load…"), on_click=self.load))
        ref_row.addWidget(S.button(_tr(w, "devices.follower.save", "Save…"), on_click=self.save))
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
        p, _ = QFileDialog.getOpenFileName(self, _tr(self.window_, "devices.follower.cal_table", "Calibration table"),
                                           self.path or "", "CSV (*.csv)")
        if p:
            self.path = p
            self.set_table(CalibrationTable.load(p))

    def save(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, _tr(self.window_, "devices.follower.cal_table", "Calibration table"),
                                           self.path or "calib.csv", "CSV (*.csv)")
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
            self.status_lbl.setText(_tr(self.window_, "devices.not_connected", "Not connected"))
            return
        try:
            st = zf.read_status()
        except Exception as e:  # device errors are shown, not raised
            self.status_lbl.setText(_tr(self.window_, "devices.error", "Error: {error}", error=e))
            return
        fs = FollowerStatus(st)
        parts = [f"{n}={fs.value(n):g}" for n, _ in STATUS_FIELDS]
        parts.append(_tr(self.window_, "devices.follower.homed_line", "homed={homed} calibration={grade}",
                         homed=fs.homed, grade=fs.calibration_grade))
        self.status_lbl.setText("\n".join(", ".join(parts[i:i + 4]) for i in range(0, len(parts), 4)))

    def _follow(self, on: bool) -> None:
        zf = self._zf()
        if zf is not None:
            zf.follow(on, self.follow_value.value())
            _log(self.window_, _tr(self.window_, "devices.follower.log_on", "Follower on") if on
                 else _tr(self.window_, "devices.follower.log_off", "Follower off"))

    def _calibrate(self) -> None:
        zf = self._zf()
        if zf is not None:
            if not zf.status().homed:
                self.status_lbl.setText(_tr(self.window_, "devices.follower.not_homed",
                                            "The follower is not homed: calibration refused (zf49)"))
                return
            zf.calibrate()
            _log(self.window_, _tr(self.window_, "devices.follower.calibration_started",
                                   "Follower: head calibration started (101 := 107)"))

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
                self.status_lbl.setText(_tr(self.window_, "devices.error", "Error: {error}", error=e))


class LaserDialog(Dialog):
    """Laser command channel (emission, aiming beam, power/current, raw query)."""

    def __init__(self, window, settings: Optional[DeviceSettings] = None, parent=None):
        super().__init__(parent if parent is not None else _as_parent(window),
                         _tr(window, "devices.laser.title", "Laser source"),
                         buttons=QDialogButtonBox.Close)
        w = window
        self.window_ = window
        self.settings = settings or DeviceSettings()
        self.client = None
        f = self.form()
        self.kind = QComboBox()
        self.kind.addItem(_tr(w, "devices.laser.raycus", "Raycus via card serial (control type 1)"), LASER_RAYCUS_NET)
        self.kind.addItem(_tr(w, "devices.laser.ipg", "IPG over network (control type 2)"), LASER_IPG_NET)
        S.form_row(f, _tr(w, "devices.laser.laser", "Laser"), self.kind)
        self.host = QLineEdit(self.settings.onb_laser[0])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.settings.onb_laser[1])
        self.kind.currentIndexChanged.connect(self._endpoint)
        S.form_row(f, _tr(w, "devices.laser.host", "Host"), self.host)
        S.form_row(f, _tr(w, "devices.laser.port", "Port"), self.port)
        self.power = QDoubleSpinBox()
        self.power.setRange(0, 100)
        self.power.setValue(50)
        self.power.setSuffix(" %")
        S.form_row(f, _tr(w, "devices.laser.power", "Power or current"), self.power)
        self.body.addWidget(flow([S.button(_tr(w, f"devices.laser.{key}", text), on_click=fn) for key, text, fn in (
            ("emission_on", "Emission on", lambda: self._do("emission", True)),
            ("emission_off", "Emission off", lambda: self._do("emission", False)),
            ("aim_on", "Aiming beam on", lambda: self._do("aim", True)),
            ("aim_off", "Aiming beam off", lambda: self._do("aim", False)),
            ("set_power", "Set power", lambda: self._do("power")))], justify=False))
        q = S.hbox()
        self.cmd = QLineEdit("STA")
        self.cmd.setAccessibleName(_tr(w, "devices.laser.ipg_command", "IPG command"))
        q.addWidget(self.cmd, 1)
        q.addWidget(S.button(_tr(w, "devices.laser.send_ipg", "Send (IPG)"), on_click=lambda: self._do("query")))
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
                    self.out.appendPlainText(_tr(self.window_, "devices.laser.aim_ipg_only",
                                                 "Aiming beam: IPG only (the Raycus adapter has none)"))
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
        w = window
        super().__init__(parent if parent is not None else _as_parent(window),
                         _tr(w, "devices.pendant.title", "Wireless pendant"),
                         buttons=QDialogButtonBox.Close,
                         explanation=_tr(w, "devices.pendant.hid_available", "hidapi is available.") if avail else
                         _tr(w, "devices.pendant.hid_missing",
                             "hidapi is not installed (pip install hidapi): the pendant cannot be used."))
        self.window_ = window
        self.ext = _ext(window)
        self.info = self.explanation
        self.enable = QCheckBox(_tr(w, "devices.pendant.enable", "Enable pendant"))
        self.enable.setChecked(self.ext.pendant is not None)
        S.set_enabled(self.enable, avail, "" if avail else _tr(w, "devices.pendant.install_hid",
                                                                "Install hidapi to use the pendant"))
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
                self.status.setText(_tr(self.window_, "devices.pendant.none_found", "No pendant found (USB vendor ID 0x10CE)"))
        else:
            stop_pendant(self.window_)

    def _show_status(self) -> None:
        pend = self.ext.pendant
        if pend is None:
            return
        s = pend.status
        key = "devices.pendant.status_poor" if s.poor_signal else "devices.pendant.status"
        self.status.setText(_tr(self.window_, key, "Device ID {dev_id}, channel {channel}, signal {rssi}",
                                dev_id=f"{s.dev_id:#06x}", channel=s.channel, rssi=s.rssi))
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
    _log(window, _tr(window, "devices.pendant.opened", "Pendant opened"))
    return True


def stop_pendant(window) -> None:
    ext = _ext(window)
    pend = ext.pendant
    if pend is not None:
        pend.close()
        if ext.pendant_timer is not None:
            ext.pendant_timer.stop()
        ext.pendant = None
        _log(window, _tr(window, "devices.pendant.closed", "Pendant closed"))


def register(ctx) -> DevicesExtension:
    """The "Devices" tab and ``hw.remoter_match`` (``ctx``: PluginContext)."""
    window = ctx.window
    ext = DevicesExtension(window)
    # labels: the catalogue's cmd.dev.* (the window looks up cmd.<id> for a command without one)
    cmds = {"dev.follower": lambda: FollowerDialog(window).exec(),
            "dev.laser": lambda: LaserDialog(window).exec(),
            "dev.pendant": lambda: PendantDialog(window).exec()}
    for cmd, fn in cmds.items():
        ctx.register_command(cmd, fn)
    ctx.add_ribbon_tab(_tr(window, "devices.tab", "Devices"), list(cmds))
    ctx.register_command("hw.remoter_match", lambda: PendantDialog(window).exec())
    ctx.on_close(lambda: stop_pendant(window))
    return ext
