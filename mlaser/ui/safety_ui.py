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

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QStyle, QTextEdit,
                               QMenu, QToolBar, QToolButton, QWidget)

from .._power import keep_awake

from ..machine import events as EV
from ..services import units
from . import qtbridge, style
from .responsive import keep_min_size
from .safety_texts import remedy
from .style import TARGET_RUN

log = logging.getLogger("mlaser.ui.safety")

JOB_MODES = ("process", "dry_run", "frame", "pause")

# style-lint: colour table
#: the safety colour table: (background, text) of every machine and safety state, the same in
#: every theme (docs/SAFETY.md: the state is always written out too, never colour alone).  The
#: state banner, the top-bar chips, the run bar's pill and the Run screen all read it.
COLORS = {
    "FAULT": ("#7f0000", "#ffffff"),
    "ALARM": ("#b71c1c", "#ffffff"),
    "ARMED": ("#d50000", "#ffffff"),
    "DRY-RUN": ("#ff8f00", "#000000"),
    "SIMULATOR": ("#283593", "#ffffff"),
    "DISARMED": ("#455a64", "#ffffff"),
    "OFFLINE": ("#616161", "#ffffff"),
    "READY": ("#1565c0", "#ffffff"),
    "RUNNING": ("#2e7d32", "#ffffff"),
    "HOLD": ("#ff8f00", "#000000"),
}
# style-lint: end
#: the state banner's text per state: (catalogue id, English default)
TEXTS = {
    "FAULT": ("safety.banner.fault", "FAULT"),
    "ARMED": ("safety.banner.armed", "ARMED: laser emission enabled"),
    "DRY-RUN": ("safety.banner.dry_run", "DRY-RUN: laser output disabled"),
    "SIMULATOR": ("safety.banner.simulator", "SIMULATOR: no machine"),
    "DISARMED": ("safety.banner.disarmed", "DISARMED: the laser cannot fire"),
    "OFFLINE": ("safety.banner.offline", "OFFLINE"),
}


def _tr(window, key: str, default: str, **values) -> str:
    """Catalogue text of ``key`` (``default`` without a catalogue), placeholders filled."""
    tr = getattr(window, "tr_", None)
    text = default
    if tr is not None:
        try:
            if tr.has(key):
                return tr.format(key, **values) if values else tr.tr(key)
        except Exception:  # noqa: BLE001 - a broken catalogue never hides a safety text
            pass
    return text.format(**values) if values else text
MIN_TARGET = TARGET_RUN


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
    ctl = _ctl(window)
    if ctl is not None and ctl.breakpoint is not None and ctl.state().mode not in JOB_MODES:
        msg = _tr(window, "safety.locked.breakpoint", "{what}: not available while a job is paused at a "
                  "breakpoint (resume it, or stop it to discard the breakpoint first)",
                  what=what or _tr(window, "safety.locked.this", "This action"))
    else:
        msg = _tr(window, "safety.locked.running", "{what}: not available while a job is running (stop or "
                  "finish it first)", what=what or _tr(window, "safety.locked.this", "This action"))
    panel = getattr(window, "log_panel", None)
    if panel is not None:
        panel.log(msg)
    if getattr(window, "safety_ui", None) is not None and not getattr(window, "_test_no_dialogs", False):
        title = _tr(window, "safety.locked.title", "Job running")
        QTimer.singleShot(0, lambda: QMessageBox.information(window, title, msg))
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
    try:
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(title)          # empty: the platform shows the app name (Native track)
        box.setText(text)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Ok if default_yes else QMessageBox.Cancel)
        cb = None
        if dont_ask:
            # parented to the box: PySide's setCheckBox() leaves the C++ checkbox owned by its
            # Python wrapper, which deleted it when ``cb`` was collected, and the box (kept as a
            # child of ``parent``) crashed on its next re-layout, e.g. a style-sheet change
            cb = QCheckBox(_tr(parent.window() if parent is not None else None, "safety.dont_ask",
                               "Don't ask again"), box)
            box.setCheckBox(cb)
        ok = box.exec() == QMessageBox.Ok
        if cb is not None and cb.isChecked():
            store[k] = ok
        return ok
    finally:
        box.deleteLater()               # not a child of ``parent`` forever


def recovery_prompt(window, on_recover: Callable[[], None], detail: str = "") -> None:
    """The previous run ended abnormally: offer System Recovery in a dialog (not a log line)."""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle(_tr(window, "safety.recovery.title", "Previous run interrupted"))
    box.setText(_tr(window, "safety.recovery.text", "The last session ended abnormally (crash, power loss or "
                    "forced exit).") + "\n\n" + _tr(window, "safety.recovery.question", "Recover the last "
                                                       "document and the breakpoint of the interrupted job?"))
    if detail:
        box.setInformativeText(detail)
    rec = box.addButton(_tr(window, "safety.recovery.recover", "Recover"), QMessageBox.AcceptRole)
    box.addButton(_tr(window, "safety.recovery.not_now", "Not now"), QMessageBox.RejectRole)
    try:
        box.exec()
        recover = box.clickedButton() is rec
    finally:
        box.deleteLater()
    if recover:
        on_recover()


class PreflightDialog(QDialog):
    """The pre-flight report: errors (blocking) and warnings (to acknowledge)."""

    def __init__(self, report, parent=None, title: str = "", actions=()):
        super().__init__(parent)
        self.report = report
        self.chosen = None              # the extra action the operator picked (``actions``)
        self.setWindowTitle(title or _tr(parent.window() if parent is not None else None, "safety.preflight.title",
                                         "Pre-flight check: {action}", action=report.action))
        lay = style.vbox(self, "dialog")
        head = QLabel()
        win = parent.window() if parent is not None else None
        if report.errors:
            head.setText(f"<b style='color:{style.css('danger')}'>"
                         + _tr(win, "safety.preflight.errors", "{n} problem(s) block this action.",
                               n=len(report.errors)) + "</b>")
        else:
            head.setText("<b>" + _tr(win, "safety.preflight.warnings", "{n} warning(s): continue anyway?",
                                     n=len(report.warnings)) + "</b>")
        lay.addWidget(head)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        lines = [f"ERROR    {i.message}" for i in report.errors] + [f"warning  {i.message}" for i in report.warnings]
        if report.extent is not None:
            x0, y0, x1, y1 = report.extent
            u = units.current()
            n = lambda v: u.number(v, "length", 1 if u.unit("length") == "mm" else 2)  # noqa: E731
            lines.append(f"\njob extent X {n(x0)}..{n(x1)}  Y {n(y0)}..{n(y1)} {u.suffix('length')}")
        if report.travel is not None:
            u = units.current()
            tv = report.travel
            g = lambda v: u.number(v, "length", trim=True)  # noqa: E731
            lines.append(f"travel X {g(tv.x[0])}..{g(tv.x[1])} {u.suffix('length')}, "
                         f"Y {g(tv.y[0])}..{g(tv.y[1])} {u.suffix('length')} ({tv.source})")
        txt.setPlainText("\n".join(lines))
        lay.addWidget(txt)
        bb = QDialogButtonBox()
        if report.errors:
            bb.addButton(QDialogButtonBox.Close)
            bb.rejected.connect(self.reject)
        else:
            self.go = bb.addButton(_tr(win, "safety.preflight.continue", "Continue"), QDialogButtonBox.AcceptRole)
            bb.addButton(QDialogButtonBox.Cancel)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
        for label, fn in actions:       # e.g. "Repair..." for open contours (contributors)
            b = bb.addButton(label, QDialogButtonBox.ActionRole)
            b.clicked.connect(lambda _=False, f=fn: self._act(f))
        lay.addWidget(bb)
        self.resize(560, 300)

    def _act(self, fn) -> None:
        self.chosen = fn
        self.reject()                   # the action replaces the start: it never continues it
        fn()


# ============================================================================== widgets
class StateBanner(QWidget):
    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        # the style-sheet background only paints on a plain QWidget with WA_StyledBackground;
        # without it the white banner text sat on the window colour (invisible in light themes)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.ui = ui
        h = style.hbox(self)
        h.setContentsMargins(style.SP2, 0, style.SP2, 0)
        self.label = QLabel()
        self.label.setObjectName("safety_state")
        style.set_font(self.label, "title")
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(self.label, 1)
        self.btn = QPushButton()
        self.btn.setMinimumHeight(style.TARGET)
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
        w = self.ui.w
        key, default = TEXTS.get(kind, ("", kind))
        text = _tr(w, key, default) if key else kind
        if kind == "FAULT":
            text = _tr(w, "safety.banner.fault_latched", "FAULT (latched): {fault}", fault=st.fault)
        elif st is not None and st.simulator and kind != "SIMULATOR":
            text += "  · " + _tr(w, "safety.banner.on_simulator", "simulator")
        if st is not None and kind == "SIMULATOR":
            text = _tr(w, "safety.banner.simulator_disarmed", "SIMULATOR: disarmed") if not st.armed else text
        if self.label.text() != text:
            self.label.setText(text)
        qss = f"background:{bg}; color:{fg};"
        if qss != self._style:                 # re-polishing is expensive: only on a change
            self._style = qss
            self.setStyleSheet(qss)
            self.label.setStyleSheet(f"color:{fg}; background:transparent;")
        label = {"FAULT": _tr(w, "safety.banner.reset", "Reset"),
                 "ARMED": _tr(w, "run.btn.disarm", "Disarm laser"),
                 "DISARMED": _tr(w, "safety.banner.arm", "Arm laser…"),
                 "SIMULATOR": _tr(w, "safety.banner.arm", "Arm laser…")}.get(kind)
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
        h = style.hbox(self)
        h.setContentsMargins(style.SP2, style.SP1, style.SP2, style.SP1)
        v = style.vbox(sp=style.SP1)
        self.title = QLabel()
        style.set_font(self.title, "label_strong")
        self.title.setWordWrap(True)
        self.remedy = QLabel()
        self.remedy.setWordWrap(True)
        v.addWidget(self.title)
        v.addWidget(self.remedy)
        h.addLayout(v, 1)
        w = ui.w
        self.reset = QPushButton(_tr(w, "run.btn.reset", "Reset alarm"))
        self.reset.setMinimumHeight(style.TARGET_TOUCH)
        self.reset.clicked.connect(self.ui.reset_fault)
        self.details = QPushButton(_tr(w, "run.btn.details", "Details"))
        self.details.setMinimumHeight(style.TARGET_TOUCH)
        self.details.clicked.connect(self.ui.alarm_details)
        h.addWidget(self.reset)
        h.addWidget(self.details)
        bg, fg = COLORS["ALARM"]
        self.setStyleSheet(f"background:{bg}; color:{fg};")
        self.shown_key = None

    def refresh(self, alarms: list, st, since: dict) -> bool:
        """Returns True when the banner should be visible."""
        tr = getattr(self.ui.w, "tr_", None)
        if alarms:
            a = alarms[0]
            txt = a.text(tr) if hasattr(a, "text") else str(a)
            code = getattr(a, "code", "")
            t = since.get(code)
            w = self.ui.w
            more = ("  " + _tr(w, "safety.alarm.more", "(+{n} more)", n=len(alarms) - 1)) if len(alarms) > 1 else ""
            since = ("   " + _tr(w, "safety.alarm.since", "since {time}",
                                 time=time.strftime('%H:%M:%S', time.localtime(t)))) if t else ""
            self.title.setText(_tr(w, "safety.alarm.title", "ALARM {code}: {text}", code=code, text=txt) + more + since)
            self.remedy.setText(_tr(w, "safety.alarm.remedy", "Remedy: {text}", text=remedy(a, tr)))
            return True
        if st is not None and st.banner == "FAULT":
            w = self.ui.w
            self.title.setText(_tr(w, "safety.alarm.fault", "FAULT: {fault}", fault=st.fault) + "   " + _tr(
                w, "safety.alarm.since", "since {time}", time=time.strftime('%H:%M:%S', time.localtime(st.fault_t))))
            self.remedy.setText(_tr(w, "safety.alarm.remedy", "Remedy: {text}", text=remedy(st.fault, tr)))
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
        self.setIconSize(QSize(style.ICON_L, style.ICON_L))
        w = ui.w
        tr = getattr(w, "tr_", None)
        # the pill shows the machine's run state (READY, RUNNING, HOLD, ALARM, FAULT, OFFLINE):
        # the safety state (ARMED, SIMULATOR, ...) is the banner's and the chips' (UI audit D-3)
        self.pill = QLabel(" OFFLINE ")
        self.pill.setObjectName("run_state")
        self.pill.setAlignment(Qt.AlignCenter)
        style.set_font(self.pill, "label_strong")
        self.addWidget(self.pill)
        self.buttons: dict[str, QPushButton] = {}
        # one factory for the run controls (style.run_button: the glossary's words, icon before
        # the text, 56 px, STOP filled in the danger colour), the same as the Run screen's
        for key, fn in (("start", ui.start), ("hold", ui.hold), ("stop", ui.stop), ("frame", ui.frame),
                        ("dry_run", ui.dry_run), ("arm", ui.toggle_arm)):
            b = style.run_button(key, tr, fn)
            self.buttons[key] = b
            if key != "start":
                self.addWidget(b)
            else:
                # explicit scope: "Start selection only" (it used to hide in a Run Control check
                # box that silently changed what Start did)
                more = QToolButton()
                more.setObjectName("run_start_menu")
                more.setArrowType(Qt.DownArrow)
                more.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
                more.setToolTip(_tr(w, "run.start_more", "More ways to start"))
                more.setAccessibleName(_tr(w, "run.start_options", "Start options"))
                menu = QMenu(more)
                act = menu.addAction(_tr(w, "run.start_selection", "Start selection only"))
                act.setObjectName("run_start_selection")
                act.triggered.connect(lambda _=False: ui.start_selection())
                more.menu_ = menu
                more.clicked.connect(lambda _=False, m=menu, b=more: m.popup(b.mapToGlobal(b.rect().bottomLeft())))
                keep_min_size(more, QSize(MIN_TARGET // 3, MIN_TARGET))
                box = QWidget()                   # Start and its menu arrow side by side
                box.setObjectName("run_start_box")
                bl = style.hbox(box, sp=0)
                bl.addWidget(b)
                bl.addWidget(more)
                self.addWidget(box)
                self.start_menu = more
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.addWidget(self.progress)
        self.eta = QLabel("")
        self.addWidget(self.eta)
        # the longer texts the buttons take in their other state
        self._alt = {k: style.run_text(k, tr, alt=True) for k in ("start", "hold", "arm")}
        self.fit()
        self._style = ""

    def fit(self) -> None:
        """Every button's full text fits, also its longer state (Continue, Disarm), and the bar
        never hides a button in its overflow menu: its minimum width is what they need, so the
        window's minimum follows (docs/dev/RESPONSIVE.md)."""
        fm = self.fontMetrics()
        for key, b in self.buttons.items():
            bfm = b.fontMetrics()
            text_w = max(bfm.horizontalAdvance(t) for t in (b.text(), self._alt.get(key, "")) if t)
            pad = b.sizeHint().width() - bfm.horizontalAdvance(b.text())
            keep_min_size(b, QSize(max(MIN_TARGET + (style.STOP_EXTRA if key == "stop" else 0), text_w + pad),
                                   MIN_TARGET))
        pfm = self.pill.fontMetrics()
        self.pill.setMinimumWidth(max(pfm.horizontalAdvance(f" {t} ") for t in self._pill_texts())
                                  + 2 * fm.horizontalAdvance("M"))
        self.progress.setMinimumWidth(fm.horizontalAdvance("100 %") * 2)
        self.eta.setMinimumWidth(fm.horizontalAdvance("00:00:00") + fm.horizontalAdvance("M"))
        lay = self.layout()
        sp = lay.spacing() if lay is not None else 0
        st = self.style()
        need = sum(w.minimumWidth() + sp for w in (self.pill, *self.buttons.values(), self.start_menu,
                                                   self.progress, self.eta))
        if lay is not None:
            m = lay.contentsMargins()
            need += m.left() + m.right()
        need += 2 * st.pixelMetric(QStyle.PM_ToolBarFrameWidth, None, self) + 2 * st.pixelMetric(
            QStyle.PM_ToolBarItemMargin, None, self)
        if self.isMovable():
            need += st.pixelMetric(QStyle.PM_ToolBarHandleExtent, None, self)
        self.setMinimumWidth(need)

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange) and getattr(self, "_alt", None) is not None:
            self.fit()

    #: run state -> (catalogue id, default, colour key of COLORS)
    PILL = {"offline": ("run.state.offline", "OFFLINE", "OFFLINE"), "ready": ("run.state.ready", "READY", "READY"),
            "running": ("run.state.running", "RUNNING", "RUNNING"), "hold": ("run.state.hold", "HOLD", "HOLD"),
            "alarm": ("run.state.alarm", "ALARM", "ALARM"), "fault": ("run.state.fault", "FAULT", "FAULT")}

    def _pill_texts(self) -> list[str]:
        return [_tr(self.ui.w, k, d) for k, d, _c in self.PILL.values()]

    def run_state(self, st, ms) -> str:
        """offline / ready / running / hold / alarm / fault: the machine's run state."""
        if st is not None and st.banner == "FAULT":
            return "fault"
        if ms is not None and ms.alarms:
            return "alarm"
        if ms is None or not ms.connected:
            return "offline"
        if ms.mode == "pause":
            return "hold"
        if ms.mode in ("process", "dry_run", "frame"):
            return "running"
        return "ready"

    def refresh(self, st, ms, running: bool, eta: str) -> None:
        kind = st.banner if st is not None else "OFFLINE"
        rs = self.run_state(st, ms)
        key, default, col = self.PILL[rs]
        label = _tr(self.ui.w, key, default)
        # READY while armed shows the ARMED colour (the laser fires on Start: never the calm blue)
        if rs == "ready" and getattr(st, "armed", False):
            col = "ARMED"
        bg, fg = COLORS[col]
        if self.pill.text() != f" {label} ":
            self.pill.setText(f" {label} ")
            self.pill.setAccessibleName(label)
        qss = (f"background:{bg}; color:{fg}; padding:{style.SP1}px {style.SP2}px; "
               f"border-radius:{style.SP1}px;")
        if qss != self._style:
            self._style = qss
            self.pill.setStyleSheet(qss)
        connected = bool(ms is not None and ms.connected)
        fault = kind == "FAULT"
        paused = ms is not None and ms.mode == "pause"
        for k, on, why in run_enabled(self.ui.w, st, ms, running):
            b = self.buttons[k]
            if b.isEnabled() != on or b.property("disabled_reason") != ("" if on else why):
                style.set_enabled(b, on, why)
        tr = getattr(self.ui.w, "tr_", None)
        self._text(self.buttons["start"], style.run_text("start", tr, alt=paused))
        self._text(self.buttons["hold"], style.run_text("hold", tr, alt=paused))
        armed = bool(getattr(st, "armed", False))
        self._text(self.buttons["arm"], style.run_text("arm", tr, alt=armed))
        del connected, fault
        p = float(getattr(ms, "progress", 0.0) or 0.0) if running else 0.0
        self.progress.setValue(int(p * 1000))
        self.progress.setFormat(f"{p * 100:.0f} %" if running else "")
        self.eta.setText(eta)


    @staticmethod
    def _text(b: QPushButton, text: str) -> None:
        if b.text() != text:
            b.setText(text)
            b.setAccessibleName(text)


def run_enabled(window, st, ms, running: bool) -> list[tuple[str, bool, str]]:
    """(run control, enabled, why not) for the run bar and the Run screen: one rule set.
    STOP is always enabled."""
    connected = bool(ms is not None and ms.connected)
    fault = st is not None and st.banner == "FAULT"
    paused = ms is not None and ms.mode == "pause"
    dry = bool(getattr(st, "dry_run", False))
    no_conn = _tr(window, "run.why.offline", "Connect the machine or the simulator first")
    why_fault = _tr(window, "run.why.fault", "Reset the fault first")
    why_running = _tr(window, "run.why.running", "Not while a job runs")

    def reason(*checks: tuple[bool, str]) -> tuple[bool, str]:
        for bad, why in checks:
            if bad:
                return False, why
        return True, ""
    out = []
    ok, why = reason((not connected, no_conn), (fault, why_fault), (running and not paused, why_running))
    out.append(("start", ok, why))
    ok, why = reason((not connected, no_conn), (not running, _tr(window, "run.why.no_job", "No job is running")))
    out.append(("hold", ok, why))
    out.append(("stop", True, ""))
    for k in ("frame", "dry_run"):
        ok, why = reason((not connected, no_conn), (fault, why_fault), (running, why_running))
        out.append((k, ok, why))
    ok, why = reason((not connected, no_conn), (fault, why_fault),
                     (dry, _tr(window, "run.why.laser_off", "Laser output is disabled (dry-run mode)")))
    out.append(("arm", ok, why))
    return out


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
        keep_awake(running)                 # no idle system sleep while a job runs (macOS, Windows)
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
        bg, fg = COLORS["ALARM"]
        red = f"color:{fg}; background:{bg}; font-weight:bold; padding:0 {style.SP1}px;"
        if st is not None and st.banner == "FAULT":
            lbl.setText(_tr(self.w, "status.motion_fault", "Motion card: FAULT"))
            qss = red
        elif alarms:
            lbl.setText(_tr(self.w, "status.motion_alarm", "Motion card: ALARM {code}",
                            code=getattr(alarms[0], 'code', '')))
            qss = red
        else:
            qss = ""
        if qss != self._mc_style:
            self._mc_style = qss
            lbl.setStyleSheet(qss)

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

    def start_selection(self) -> None:
        """Start only the selected parts (refused, with a message, when nothing is selected)."""
        doc = self.w.document
        if doc is None or not any(getattr(p, "selected", False) for p in doc.parts):
            self.w.statusBar().showMessage(_tr(self.w, "run.start_selection_none",
                                               "Start selection: select the parts to cut first"), 6000)
            return
        self.start(selected_only=True)

    def _extent(self, doc):
        try:
            from ..nc.compiler import placed_document
            bb = placed_document(self.w.ctl.place(doc)).bbox()
        except Exception:  # noqa: BLE001
            bb = doc.bbox() if hasattr(doc, "bbox") else None
        return None if bb in (None, (0, 0, 0, 0)) else bb

    def preflight_ok(self, action: str, doc=None, emits: Optional[bool] = None, selected_only: bool = False) -> bool:
        """Run the controller's pre-flight check; show the dialog when it finds anything."""
        ctl = self.w.ctl
        try:
            rep = ctl.preflight(action, doc, selected_only, extent=self._extent(doc) if doc else None,
                                emits=emits)
        except TypeError:
            rep = ctl.preflight(action, doc)
        from .safety_io import augment_preflight          # "interlock not configured" warnings
        rep = augment_preflight(self.w, rep, action, emits)
        from .workspaces.cutting_strategy import augment_preflight as augment_cutting
        rep = augment_cutting(self.w, rep, action, doc, selected_only)   # lead / tip warnings
        rep = contribute_preflight(self.w, rep, action, doc, emits)   # plugins (material check)
        if not rep.issues:
            return True
        if not self._dialogs():
            return rep.ok
        rep = override_preflight(self.w, rep)
        if not rep.issues:
            return True
        actions = [a for c in _contributors(self.w) for a in (c.actions(rep) if hasattr(c, "actions") else ())]
        d = PreflightDialog(rep, self.w, actions=actions)
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

    def start(self, selected_only: bool = False) -> None:
        """Start (or resume) the job; ``selected_only``: only the selected parts, asked for
        explicitly (the run bar's Start menu, ``run.start_selection``), never a hidden switch."""
        ctl, doc = self.w.ctl, self.w.document
        ms = ctl.state()
        if ms.mode == "pause" or ctl.breakpoint is not None:
            from .resume import resume
            resume(ctl, "exact", doc)           # a breakpoint of another document is refused
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
        if not self.preflight_ok("start" if emits else "dry_run", doc, emits, selected_only=bool(selected_only)):
            return
        self._shown = None
        self._runner().start(doc, selected_only=bool(selected_only))
        self._after("Start")

    def hold(self) -> None:
        ctl = self.w.ctl
        if ctl.state().mode == "pause":
            from .resume import resume
            resume(ctl, "exact", self.w.document)
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


def _contributors(window) -> list:
    return list(getattr(window, "preflight_contributors", None) or ())


def contribute_preflight(window, rep, action: str, doc, emits):
    """``rep`` plus the issues of the window's pre-flight contributors
    (``window.preflight_contributors``: objects with ``issues(action, doc, emits)``, and
    optionally ``overridable`` codes with ``override(issues, parent) -> bool`` and
    ``actions(report) -> [(label, fn)]``).  A new report; the controller's is not changed.
    A contributor only adds issues: it cannot remove the controller's."""
    extra = []
    for c in _contributors(window):
        try:
            extra += list(c.issues(action, doc, emits) or ())
        except Exception:  # noqa: BLE001 - a broken contributor must not hide the dialog
            log.exception("pre-flight contributor %r failed", c)
    if not extra or rep is None:
        return rep
    from dataclasses import replace
    return replace(rep, issues=list(rep.issues) + extra)


def override_preflight(window, rep):
    """Ask the contributors to override their own overridable errors (each asks the operator
    and audits it).  Overridden errors stay in the report as warnings; nothing else changes."""
    from dataclasses import replace
    from mlaser.machine.preflight import WARNING
    issues = list(rep.issues)
    for c in _contributors(window):
        codes = set(getattr(c, "overridable", ()) or ())
        mine = [i for i in issues if i.blocking and i.code in codes]
        if not mine:
            continue
        if c.override(mine, window):
            issues = [replace(i, severity=WARNING, message=f"overridden: {i.message}") if i in mine else i
                      for i in issues]
    return replace(rep, issues=issues)


def install(window) -> SafetyUI:
    ui = SafetyUI(window)
    window.safety_ui = ui
    return ui


__all__ = ["install", "SafetyUI", "RunBar", "StateBanner", "AlarmBanner", "PreflightDialog",
           "job_locked", "job_running", "ask", "recovery_prompt"]
