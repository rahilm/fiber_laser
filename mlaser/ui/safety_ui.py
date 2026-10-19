"""Safety UI: state banner, alarm banner, persistent run bar, global Hold/Stop keys, dialogs.

``install(window)`` (called once by :class:`mlaser.ui.mainwindow.MainWindow`) adds:

* the **event dispatcher** (:mod:`.qtbridge`): controller events reach the GUI thread;
* the **state banner** (top, always visible): ``FAULT`` (with the reason and *Reset*), ``ARMED``
  (red, *Disarm*), ``DRY-RUN``, ``SIMULATOR``, ``DISARMED`` (*Arm*), ``OFFLINE``;
* the **alarm banner** (top, shown while card alarms or a fault are active): alarm text, time,
  a remedy (:mod:`.safety_texts`), *Reset* and *Details* (the alarm list dialog);
* the **run bar** (bottom toolbar, never inside a scroll area): state pill, Start (F5),
  Hold (Space), Stop (Esc / F12), Frame (F6), Dry run, Arm, progress and ETA, 56 px targets;
* **global keys** while a job runs: Space = Hold/continue (not while typing in a text field),
  Esc and F12 = Stop.  An application event filter, so they work whatever has the focus;
* the **pre-flight dialog** before Start / Frame / Dry run / Resume: errors block, warnings must
  be acknowledged;
* the **status bar** override: never "Ready" while an alarm or a fault is latched;
* the **job lock**: :func:`job_locked` (no file open / new document while a job runs);
* start-up prompts as dialogs: :func:`ask` with "don't ask again", :func:`recovery_prompt`.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTextEdit,
                               QToolBar, QVBoxLayout, QWidget)

from ..machine import events as EV
from . import qtbridge
from .safety_texts import remedy

JOB_MODES = ("process", "dry_run", "frame", "pause")

COLORS = {
    "FAULT": ("#7f0000", "#ffffff"),
    "ARMED": ("#d50000", "#ffffff"),
    "DRY-RUN": ("#ff8f00", "#000000"),
    "SIMULATOR": ("#283593", "#ffffff"),
    "DISARMED": ("#455a64", "#ffffff"),
    "OFFLINE": ("#616161", "#ffffff"),
}
TEXTS = {
    "FAULT": "FAULT",
    "ARMED": "ARMED - laser emission enabled",
    "DRY-RUN": "DRY-RUN - laser output disabled",
    "SIMULATOR": "SIMULATOR - no machine",
    "DISARMED": "DISARMED - laser cannot fire",
    "OFFLINE": "OFFLINE",
}
MIN_TARGET = 56


def _ctl(w):
    return getattr(w, "ctl", None)


def _safety(ctl):
    return ctl.safety() if ctl is not None else None


def job_running(window) -> bool:
    ctl = _ctl(window)
    if ctl is None:
        return False
    if ctl.job_active():
        return True
    return ctl.state().mode in ("process", "dry_run", "frame")


def job_locked(window, what: str = "") -> bool:
    """True (and a message) when a job runs: the document must not be replaced while it cuts."""
    if not job_running(window):
        return False
    msg = f"{what or 'this action'}: not available while a job is running (stop or finish it first)"
    panel = getattr(window, "log_panel", None)
    if panel is not None:
        panel.log(msg)
    if getattr(window, "safety_ui", None) is not None and not getattr(window, "_test_no_dialogs", False):
        QTimer.singleShot(0, lambda: QMessageBox.information(window, "Job running", msg))
    return True


# ============================================================================== dialogs
DONT_ASK: dict[str, bool] = {}      # key -> remembered answer for this session (settings persist it)


def ask(parent, key: str, text: str, title: str = "", dont_ask: bool = False,
        settings: Optional[dict] = None, default_yes: bool = True) -> bool:
    """OK/Cancel question as a real dialog; with ``dont_ask`` a "don't ask again" box stores the
    answer in ``settings["dont_ask.<key>"]``."""
    store = settings if settings is not None else DONT_ASK
    k = f"dont_ask.{key}"
    if dont_ask and k in store:
        return bool(store[k])
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle(title or "Mlaser")
    box.setText(text)
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Ok if default_yes else QMessageBox.Cancel)
    cb = None
    if dont_ask:
        cb = QCheckBox("Don't ask again")
        box.setCheckBox(cb)
    ok = box.exec() == QMessageBox.Ok
    if cb is not None and cb.isChecked():
        store[k] = ok
    return ok


def recovery_prompt(window, on_recover: Callable[[], None], detail: str = "") -> None:
    """The previous run ended abnormally: offer System Recovery in a dialog (not a log line)."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Previous run interrupted")
    box.setText("The last session ended abnormally (crash, power loss or forced exit).\n\n"
                "Recover the last document and the breakpoint of the interrupted job?")
    if detail:
        box.setInformativeText(detail)
    rec = box.addButton("Recover", QMessageBox.AcceptRole)
    box.addButton("Not now", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is rec:
        on_recover()


class PreflightDialog(QDialog):
    """The pre-flight report: errors (blocking) and warnings (to acknowledge)."""

    def __init__(self, report, parent=None, title: str = ""):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(title or f"Pre-flight check: {report.action}")
        lay = QVBoxLayout(self)
        head = QLabel()
        if report.errors:
            head.setText(f"<b style='color:#b71c1c'>{len(report.errors)} problem(s) block this action.</b>")
        else:
            head.setText(f"<b>{len(report.warnings)} warning(s): continue anyway?</b>")
        lay.addWidget(head)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        lines = [f"ERROR    {i.message}" for i in report.errors] + [f"warning  {i.message}" for i in report.warnings]
        if report.extent is not None:
            x0, y0, x1, y1 = report.extent
            lines.append(f"\njob extent X {x0:.1f}..{x1:.1f}  Y {y0:.1f}..{y1:.1f} mm")
        if report.travel is not None:
            lines.append(f"travel {report.travel.describe()}")
        txt.setPlainText("\n".join(lines))
        lay.addWidget(txt)
        bb = QDialogButtonBox()
        if report.errors:
            bb.addButton(QDialogButtonBox.Close)
            bb.rejected.connect(self.reject)
        else:
            self.go = bb.addButton("Continue", QDialogButtonBox.AcceptRole)
            bb.addButton(QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(560, 300)


# ============================================================================== widgets
class StateBanner(QWidget):
    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        # the style-sheet background only paints on a plain QWidget with WA_StyledBackground;
        # without it the white banner text sat on the window colour (invisible in light themes)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.ui = ui
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 2, 8, 2)
        self.label = QLabel()
        self.label.setObjectName("safety_state")
        f = self.label.font()
        f.setBold(True)
        f.setPointSizeF(max(f.pointSizeF(), 10.0) * 1.2)
        self.label.setFont(f)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(self.label, 1)
        self.btn = QPushButton()
        self.btn.setMinimumHeight(32)
        self.btn.clicked.connect(self._action)
        h.addWidget(self.btn)
        self.kind = ""
        self._style = ""

    def _action(self) -> None:
        k = self.kind
        if k == "FAULT":
            self.ui.reset_fault()
        elif k == "ARMED":
            self.ui.disarm()
        elif k in ("DISARMED", "SIMULATOR"):
            self.ui.arm()

    def refresh(self, st) -> None:
        kind = st.banner if st is not None else "OFFLINE"
        self.kind = kind
        bg, fg = COLORS.get(kind, COLORS["OFFLINE"])
        text = TEXTS.get(kind, kind)
        if kind == "FAULT":
            text = f"FAULT (latched): {st.fault}"
        elif st is not None and st.simulator and kind != "SIMULATOR":
            text += "  [simulator]"
        if st is not None and kind == "SIMULATOR":
            text = "SIMULATOR - disarmed" if not st.armed else text
        if self.label.text() != text:
            self.label.setText(text)
        style = f"background:{bg}; color:{fg};"
        if style != self._style:               # re-polishing is expensive: only on a change
            self._style = style
            self.setStyleSheet(style)
            self.label.setStyleSheet(f"color:{fg}; background:transparent;")
        label = {"FAULT": "Reset", "ARMED": "Disarm", "DISARMED": "Arm...", "SIMULATOR": "Arm..."}.get(kind)
        self.btn.setVisible(label is not None)
        if label:
            self.btn.setText(label)


class AlarmBanner(QWidget):
    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        # the style-sheet background only paints on a plain QWidget with WA_StyledBackground;
        # without it the white banner text sat on the window colour (invisible in light themes)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.ui = ui
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 8, 4)
        v = QVBoxLayout()
        self.title = QLabel()
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        self.remedy = QLabel()
        self.remedy.setWordWrap(True)
        v.addWidget(self.title)
        v.addWidget(self.remedy)
        h.addLayout(v, 1)
        self.reset = QPushButton("Reset")
        self.reset.setMinimumSize(96, 40)
        self.reset.clicked.connect(self.ui.reset_fault)
        self.details = QPushButton("Details")
        self.details.setMinimumHeight(40)
        self.details.clicked.connect(self.ui.alarm_details)
        h.addWidget(self.reset)
        h.addWidget(self.details)
        self.setStyleSheet("background:#b71c1c; color:#ffffff;")
        self.shown_key = None

    def refresh(self, alarms: list, st, since: dict) -> bool:
        """Returns True when the banner should be visible."""
        tr = getattr(self.ui.w, "tr_", None)
        if alarms:
            a = alarms[0]
            txt = a.text(tr) if hasattr(a, "text") else str(a)
            code = getattr(a, "code", "")
            t = since.get(code)
            more = f"  (+{len(alarms) - 1} more)" if len(alarms) > 1 else ""
            self.title.setText(f"ALARM {code}: {txt}{more}" + (f"   since {time.strftime('%H:%M:%S', time.localtime(t))}" if t else ""))
            self.remedy.setText("Remedy: " + remedy(a, tr))
            return True
        if st is not None and st.banner == "FAULT":
            self.title.setText(f"FAULT: {st.fault}   since {time.strftime('%H:%M:%S', time.localtime(st.fault_t))}")
            self.remedy.setText("Remedy: " + remedy(st.fault, tr))
            return True
        return False


class RunBar(QToolBar):
    """Persistent run controls (bottom of the main window, never scrolled away)."""

    def __init__(self, ui: "SafetyUI"):
        super().__init__("Run")
        self.ui = ui
        self.setObjectName("run_bar")
        self.setMovable(False)
        self.setFloatable(False)
        self.toggleViewAction().setVisible(False)
        self.setIconSize(QSize(28, 28))
        self.pill = QLabel(" OFFLINE ")
        self.pill.setObjectName("run_state")
        self.pill.setMinimumWidth(120)
        self.pill.setAlignment(Qt.AlignCenter)
        f = self.pill.font()
        f.setBold(True)
        self.pill.setFont(f)
        self.addWidget(self.pill)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip, fn in (
                ("start", "Start", "Start / continue (F5) - pre-flight check first", ui.start),
                ("hold", "Hold", "Hold / continue (Space)", ui.hold),
                ("stop", "STOP", "Stop (Esc or F12)", ui.stop),
                ("frame", "Frame", "Trace the job outline, laser off (F6)", ui.frame),
                ("dry_run", "Dry run", "Run the job with the laser off", ui.dry_run),
                ("arm", "Arm", "Arm / disarm laser emission", ui.toggle_arm)):
            b = QPushButton(text)
            b.setObjectName(f"run_{key}")
            b.setToolTip(tip)
            b.setMinimumSize(QSize(MIN_TARGET + (24 if key == "stop" else 0), MIN_TARGET))
            b.clicked.connect(lambda _=False, f=fn: f())
            if key == "stop":
                b.setStyleSheet("QPushButton{background:#c62828;color:white;font-weight:bold;}")
            self.addWidget(b)
            self.buttons[key] = b
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setMinimumWidth(160)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        self.addWidget(self.progress)
        self.eta = QLabel("")
        self.eta.setMinimumWidth(110)
        self.addWidget(self.eta)
        self._style = ""

    def refresh(self, st, ms, running: bool, eta: str) -> None:
        kind = st.banner if st is not None else "OFFLINE"
        state = getattr(st, "state", None)
        s = getattr(state, "value", "offline") if state is not None else "offline"
        label = {"running": "RUNNING", "paused": "PAUSED"}.get(s, kind)
        if kind == "FAULT":
            label = "FAULT"
        elif ms is not None and ms.alarms:
            label = "ALARM"
        bg, fg = COLORS.get("FAULT" if label in ("FAULT", "ALARM") else
                            ("ARMED" if getattr(st, "armed", False) else kind), COLORS["OFFLINE"])
        if self.pill.text() != f" {label} ":
            self.pill.setText(f" {label} ")
        style = f"background:{bg}; color:{fg}; padding:4px; border-radius:4px;"
        if style != self._style:
            self._style = style
            self.pill.setStyleSheet(style)
        connected = bool(ms is not None and ms.connected)
        fault = kind == "FAULT"
        self.buttons["start"].setEnabled(connected and not fault and (not running or ms.mode == "pause"))
        self.buttons["hold"].setEnabled(connected and running)
        self.buttons["hold"].setText("Continue" if ms is not None and ms.mode == "pause" else "Hold")
        self.buttons["stop"].setEnabled(True)
        self.buttons["frame"].setEnabled(connected and not fault and not running)
        self.buttons["dry_run"].setEnabled(connected and not fault and not running)
        armed = bool(getattr(st, "armed", False))
        self.buttons["arm"].setText("Disarm" if armed else "Arm")
        self.buttons["arm"].setEnabled(connected and not fault and not getattr(st, "dry_run", False))
        p = float(getattr(ms, "progress", 0.0) or 0.0) if running else 0.0
        self.progress.setValue(int(p * 1000))
        self.progress.setFormat(f"{p * 100:.0f} %" if running else "")
        self.eta.setText(eta)


# ============================================================================== controller
class _KeyFilter(QObject):
    """Global Hold (Space) and Stop (Esc, F12) while a job runs."""

    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        self.ui = ui

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt API
        t = ev.type()
        if t not in (QEvent.ShortcutOverride, QEvent.KeyPress):
            return False
        try:
            key = ev.key()
        except AttributeError:
            return False
        if key not in (Qt.Key_Escape, Qt.Key_F12, Qt.Key_Space):
            return False
        if not job_running(self.ui.w):
            return False
        if key == Qt.Key_Space:
            fw = QApplication.focusWidget()
            if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
                return False
        if t == QEvent.ShortcutOverride:
            ev.accept()          # we take the key: no other shortcut (Esc = deselect) fires
            return True
        if getattr(ev, "isAutoRepeat", lambda: False)():
            return True
        if key == Qt.Key_Space:
            self.ui.hold()
        else:
            self.ui.stop()
        return True


class SafetyUI(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.dispatcher = qtbridge.QtDispatcher(window)
        self._subs: list = []
        self._ctl = None
        self._alarm_since: dict = {}
        self._job_t0 = 0.0
        self.top = QToolBar("Safety state", window)
        self.top.setObjectName("safety_banner")
        self.top.setMovable(False)
        self.top.setFloatable(False)
        self.top.toggleViewAction().setVisible(False)
        self.banner = StateBanner(self)
        self.banner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.top.addWidget(self.banner)
        window.addToolBar(Qt.TopToolBarArea, self.top)
        window.addToolBarBreak(Qt.TopToolBarArea)
        self.alarm_bar = QToolBar("Alarm", window)
        self.alarm_bar.setObjectName("alarm_banner")
        self.alarm_bar.setMovable(False)
        self.alarm_bar.setFloatable(False)
        self.alarm_bar.toggleViewAction().setVisible(False)
        self.alarm = AlarmBanner(self)
        self.alarm.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.alarm_bar.addWidget(self.alarm)
        window.addToolBar(Qt.TopToolBarArea, self.alarm_bar)
        self.alarm_bar.setVisible(False)
        self.run_bar = RunBar(self)
        window.addToolBar(Qt.BottomToolBarArea, self.run_bar)
        self.keys = _KeyFilter(self)
        self._filtering = False          # the key filter is installed only while a job runs
        self._mc_style = ""
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._app_state)
        window.destroyed.connect(lambda *_: self._set_filter(False))
        self.attach(window.ctl)
        hooks = getattr(window, "hooks", None)
        if hooks is not None:                 # services.plugins.Hooks: no method wrapping
            hooks.add("controller_changed", self.attach, "safety_ui")
        else:  # pragma: no cover - a window without the services layer
            orig = getattr(window, "set_controller", None)
            if callable(orig):
                def set_controller(ctl, _orig=orig):
                    _orig(ctl)
                    self.attach(ctl)
                window.set_controller = set_controller
        panel = getattr(window, "control", None)
        if panel is not None:
            panel.run_hooks = self
            if isinstance(getattr(window, "settings", None), dict):
                panel.settings = window.settings      # "don't ask again" answers persist
        t = getattr(window, "_timer", None)
        if t is not None:
            t.timeout.connect(self.refresh)       # runs after MainWindow._poll (same timer)
        else:  # pragma: no cover
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(100)
        self.refresh()

    # ------------------------------------------------------------------ controller
    def attach(self, ctl) -> None:
        if ctl is self._ctl:
            return
        for un in self._subs:
            try:
                un()
            except Exception:  # noqa: BLE001
                pass
        self._subs = []
        old = self._ctl
        if old is not None and getattr(getattr(old, "events", None), "dispatcher", None) is self.dispatcher:
            old.events.set_dispatcher(None)
        self._ctl = ctl
        qtbridge.install(ctl, self.dispatcher)
        bus = ctl.events
        if bus is not None:
            for kind in (EV.StateChanged, EV.AlarmRaised, EV.AlarmCleared, EV.LinkLost, EV.Armed, EV.Disarmed):
                self._subs.append(bus.subscribe(kind, lambda _e: self.refresh()))
            self._subs.append(bus.subscribe(EV.JobStarted, self._job_started))
        try:
            from ..devices import laser as L
            L.set_emission_guard(self._laser_source_guard)
        except Exception:  # noqa: BLE001
            pass

    def _laser_source_guard(self, on: bool) -> None:
        from ..devices.laser import LaserError
        st = _safety(self.w.ctl)
        if on and not (st is not None and st.armed and st.state.value in ("armed", "running")):
            raise LaserError("laser source emission refused: the machine is not armed")

    def _job_started(self, ev) -> None:
        self._job_t0 = time.monotonic()
        self._set_filter(True)

    def _app_state(self, state) -> None:
        """Focus loss (the application went to the background): stop a held jog (dead-man)."""
        if state != Qt.ApplicationActive:
            panel = getattr(self.w, "control", None)
            if panel is not None and hasattr(panel, "release_jogs"):
                panel.release_jogs("focus lost")

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> None:
        ctl = self.w.ctl
        if ctl is not self._ctl:
            self.attach(ctl)
        st = _safety(ctl)
        try:
            ms = ctl.state()
        except Exception:  # noqa: BLE001
            ms = None
        self.banner.refresh(st)
        alarms = list(ctl.active_alarms or [])
        now = time.time()
        codes = {getattr(a, "code", str(a)) for a in alarms}
        for c in codes:
            self._alarm_since.setdefault(c, now)
        for c in list(self._alarm_since):
            if c not in codes:
                del self._alarm_since[c]
        show = self.alarm.refresh(alarms, st, self._alarm_since)
        if show != self.alarm_bar.isVisible():
            self.alarm_bar.setVisible(show)
        running = job_running(self.w)
        self._set_filter(running)
        eta = ""
        if running and ms is not None and ms.progress > 0.02 and self._job_t0:
            el = time.monotonic() - self._job_t0
            left = el * (1.0 - ms.progress) / ms.progress
            eta = f"{int(el // 60)}:{int(el % 60):02d} / ETA {int(left // 60)}:{int(left % 60):02d}"
        self.run_bar.refresh(st, ms, running, eta)
        self._status_override(st, alarms, ms)
        panel = getattr(self.w, "control", None)
        if panel is not None and hasattr(panel, "safety_refresh"):
            panel.safety_refresh(st, ms)

    def _set_filter(self, on: bool) -> None:
        """Global Hold/Stop keys: an application event filter while a job runs (only then: a
        filter on every Qt event of the application costs a Python call each)."""
        if on == self._filtering:
            return
        app = QApplication.instance()
        if app is None:
            return
        if on:
            app.installEventFilter(self.keys)
        else:
            app.removeEventFilter(self.keys)
        self._filtering = on

    def _status_override(self, st, alarms, ms) -> None:
        """The status bar never says "Ready" while an alarm or a fault is latched."""
        lbl = getattr(self.w, "st_mc", None)
        if lbl is None:
            return
        red = "color:#ffffff; background:#b71c1c; font-weight:bold;"
        if st is not None and st.banner == "FAULT":
            lbl.setText("MCC <FAULT>")
            style = red
        elif alarms:
            lbl.setText(f"MCC <ALARM {getattr(alarms[0], 'code', '')}>")
            style = red
        else:
            style = ""
        if style != self._mc_style:
            self._mc_style = style
            lbl.setStyleSheet(style)

    # ------------------------------------------------------------------ actions
    def _log(self, msg: str) -> None:
        panel = getattr(self.w, "log_panel", None)
        if panel is not None:
            panel.log(msg)

    def _last_reason(self) -> str:
        ev = self.w.ctl.events.last(EV.Log)
        return ev.message if ev is not None else ""

    def _dialogs(self) -> bool:
        return not getattr(self.w, "_test_no_dialogs", False)

    def _selected_only(self) -> bool:
        panel = getattr(self.w, "control", None)
        cb = getattr(panel, "selected_only", None)
        return bool(cb.isChecked()) if cb is not None else False

    def _extent(self, doc):
        try:
            from ..nc.compiler import placed_document
            bb = placed_document(self.w.ctl.place(doc)).bbox()
        except Exception:  # noqa: BLE001
            bb = doc.bbox() if hasattr(doc, "bbox") else None
        return None if bb in (None, (0, 0, 0, 0)) else bb

    def preflight_ok(self, action: str, doc=None, emits: Optional[bool] = None) -> bool:
        """Run the controller's pre-flight check; show the dialog when it finds anything."""
        ctl = self.w.ctl
        try:
            rep = ctl.preflight(action, doc, self._selected_only(), extent=self._extent(doc) if doc else None,
                                emits=emits)
        except TypeError:
            rep = ctl.preflight(action, doc)
        if not rep.issues:
            return True
        if not self._dialogs():
            return rep.ok
        d = PreflightDialog(rep, self.w)
        return bool(d.exec()) and rep.ok

    def _after(self, what: str) -> None:
        """Show the controller's own refusal (exact job extent, safety state) after an action."""
        rep = self.w.ctl.last_preflight
        if rep is not None and not rep.ok and self._dialogs() and rep is not self._shown:
            self._shown = rep
            PreflightDialog(rep, self.w, f"{what} refused").exec()

    _shown = None

    def _runner(self):
        """Start / Frame / Dry run go through the planned job (services.jobs: the plan the
        canvas shows, compiled off the GUI thread; the controller's start_job runs pre-flight
        on it).  Without the services layer: the controller itself."""
        return getattr(self.w, "run_controls", None) or self.w.ctl

    def start(self) -> None:
        ctl, doc = self.w.ctl, self.w.document
        ms = ctl.state()
        if ms.mode == "pause" or ctl.breakpoint is not None:
            ctl.resume()
            self._after("Resume")
            return
        st = _safety(ctl)
        emits = bool(ctl.laser_enabled)
        if emits and st is not None and not st.armed and self._dialogs():
            if not ask(self.w, "start_disarmed", "The machine is not armed: the laser cannot fire.\n\n"
                       "Arm it now (laser emission enabled) and start?", "Start"):
                return
            if not self.arm(confirm=False):
                return
        if not self.preflight_ok("start" if emits else "dry_run", doc, emits):
            return
        self._shown = None
        self._runner().start(doc, selected_only=self._selected_only())
        self._after("Start")

    def hold(self) -> None:
        ctl = self.w.ctl
        if ctl.state().mode == "pause":
            ctl.resume()
        else:
            ctl.pause()

    def stop(self) -> None:
        self.w.ctl.stop()          # asynchronous: the halt goes out first, the UI never blocks
        panel = getattr(self.w, "control", None)
        if panel is not None and hasattr(panel, "release_jogs"):
            panel.release_jogs("stop")

    def frame(self) -> None:
        doc = self.w.document
        if not self.preflight_ok("frame", doc, False):
            return
        self._shown = None
        self._runner().frame(doc)
        self._after("Frame")

    def dry_run(self) -> None:
        doc = self.w.document
        if not self.preflight_ok("dry_run", doc, False):
            return
        self._shown = None
        self._runner().dry_run(doc)
        self._after("Dry run")

    def arm(self, confirm: bool = True) -> bool:
        ctl = self.w.ctl
        if confirm and self._dialogs():
            if not ask(self.w, "arm", "ARM the machine?\n\nLaser emission will be enabled: the beam "
                       "fires on Start, Laser and laser tests. Make sure the work area is clear and "
                       "the enclosure is closed.", "Arm laser", default_yes=False):
                return False
        ok = bool(ctl.arm())
        if not ok and self._dialogs():
            QMessageBox.warning(self.w, "Arm refused", self._last_reason() or "arming refused")
        self.refresh()
        return ok

    def disarm(self) -> None:
        self.w.ctl.disarm("disarmed by operator")
        self.refresh()

    def toggle_arm(self) -> None:
        st = _safety(self.w.ctl)
        if st is not None and st.armed:
            self.disarm()
        else:
            self.arm()

    def reset_fault(self) -> None:
        ctl = self.w.ctl
        st = _safety(ctl)
        if st is not None and st.banner != "FAULT" and ctl.active_alarms:
            ctl.clear_alarms()
            self.refresh()
            return
        ok = bool(ctl.reset_fault())
        if not ok and self._dialogs():
            QMessageBox.warning(self.w, "Reset refused", self._last_reason() or "reset refused")
        self.refresh()

    def alarm_details(self) -> None:
        from .ext_card import AlarmListDialog
        AlarmListDialog(self.w, list(self.w.ctl.active_alarms)).exec()


def install(window) -> SafetyUI:
    ui = SafetyUI(window)
    window.safety_ui = ui
    return ui


__all__ = ["install", "SafetyUI", "RunBar", "StateBanner", "AlarmBanner", "PreflightDialog",
           "job_locked", "job_running", "ask", "recovery_prompt"]
