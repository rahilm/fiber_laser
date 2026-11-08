"""Safety UI: state banner, alarm banner, persistent run bar, global Hold/Stop keys, dialogs.

``install(window)`` (called once by :class:`mlaser.ui.mainwindow.MainWindow`) adds:

* the **event dispatcher** (:mod:`.qtbridge`): controller events reach the GUI thread;
* the **state banner** (top, always visible): ``FAULT`` (with the reason and *Reset*), ``ARMED``
  (red, *Disarm*), ``DRY-RUN``, ``SIMULATOR``, ``DISARMED`` (*Arm*), ``OFFLINE``;
* the **alarm banner** (top, shown while card alarms or a fault are active): alarm text, time,
  a remedy (:mod:`.safety_texts`), *Reset* and *Details* (the alarm list dialog);
* the **run bar** (bottom toolbar, never inside a scroll area): state pill, Start (F5),
  Hold (Space), Stop (Esc / F12), Frame (F6), Dry run, Arm, progress and ETA, 56 px targets;
  one row, or two (the break after Stop) in a narrow window, every control in full;
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

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QStyle, QTextEdit,
                               QMenu, QToolBar, QToolButton, QWidget)

from .._power import keep_awake

from ..machine import events as EV
from ..services import units
from . import a11y, qtbridge, style
from .responsive import keep_min_size
from ..machine.fault_text import headline
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


def _trn(window, base: str, n: int, **values) -> str:
    """Plural catalogue text ``base`` (``<base>.one`` / ``<base>.other``) for the count ``n``."""
    tr = getattr(window, "tr_", None)
    if tr is not None:
        try:
            return tr.plural(base, n, **values)
        except Exception:  # noqa: BLE001 - a broken catalogue never hides a safety text
            pass
    from mlaser import i18n
    return i18n.plural(base, n, **values)


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
        settings: Optional[dict] = None, default_yes: bool = False, yes_text: str = "",
        warning: bool = False, informative: str = "") -> bool:
    """OK/Cancel question as a real dialog; with ``dont_ask`` a "don't ask again" box stores the
    answer in ``settings["dont_ask.<key>"]``.  Cancel is the default button unless
    ``default_yes`` (UX audit C1: a safety question never defaults to yes); ``yes_text`` names
    the OK button with its verb ("Arm laser", "Arm and start"); ``warning``: the warning icon
    (a hazard question), else the question icon."""
    store = settings if settings is not None else DONT_ASK
    k = f"dont_ask.{key}"
    if dont_ask and k in store:
        return bool(store[k])
    box = QMessageBox(parent)
    try:
        box.setIcon(QMessageBox.Warning if warning else QMessageBox.Question)
        box.setWindowTitle(title)          # empty: the platform shows the app name (Native track)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if yes_text:
            # the standard Ok role (exec() answers Ok), the verb as its text
            box.button(QMessageBox.Ok).setText(yes_text)
        box.setDefaultButton(QMessageBox.Ok if default_yes else QMessageBox.Cancel)
        box.setEscapeButton(QMessageBox.Cancel)
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
    box.setText(_tr(window, "safety.recovery.message", "The last session ended abnormally (crash, power loss or "
                    "forced exit).\n\nRecover the last document and the breakpoint of the interrupted job?"))
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


#: pre-flight action -> (title id, title default, "go anyway" id, default)
PREFLIGHT_TITLES = {
    "start": ("safety.preflight.title_start", "Before Start", "safety.preflight.anyway_start", "Start anyway"),
    "resume": ("safety.preflight.title_resume", "Before Resume", "safety.preflight.anyway_resume", "Resume anyway"),
    "frame": ("safety.preflight.title_frame", "Before Frame", "safety.preflight.anyway_frame", "Frame anyway"),
    "dry_run": ("safety.preflight.title_dry_run", "Before Dry run", "safety.preflight.anyway_dry_run",
                "Dry run anyway"),
    "move": ("safety.preflight.title_move", "Before the move", "safety.preflight.anyway_move", "Move anyway"),
    "laser_test": ("safety.preflight.title_laser_test", "Before the laser test",
                   "safety.preflight.anyway_laser_test", "Test anyway"),
}
#: issue code -> (fix button id, default, command id): the fix of a blocking error (UX audit W6)
PREFLIGHT_FIXES = {
    "layer_undefined": ("safety.preflight.fix.technology", "Choose technology…", "workspace.process"),
    "not_homed": ("safety.preflight.fix.home", "Home axes", "machine.home"),
    "alarm": ("run.btn.reset", "Reset alarm", "card.clear_alarms"),
    "fault": ("run.btn.reset", "Reset alarm", "card.clear_alarms"),
    "not_armed": ("run.btn.arm", "Arm laser", "run.arm"),
}
#: severity -> (QStyle standard icon, label id, default): icon AND word, never colour alone
_SEVERITY = {"error": ("SP_MessageBoxCritical", "safety.preflight.sev_error", "Error"),
             "warning": ("SP_MessageBoxWarning", "safety.preflight.sev_warning", "Warning"),
             "info": ("SP_MessageBoxInformation", "safety.preflight.sev_info", "Info")}


def preflight_groups(report) -> list[tuple[str, str, list]]:
    """The report's issues grouped for display: [(severity, code, issues)], errors first, one
    group per (severity, code), in the order they were found."""
    order = {"error": 0, "warning": 1, "info": 2}
    groups: dict = {}
    for i in report.issues:
        groups.setdefault((i.severity, i.code), []).append(i)
    return sorted(((sev, code, items) for (sev, code), items in groups.items()),
                  key=lambda g: order.get(g[0], 3))


class PreflightDialog(QDialog):
    """The pre-flight report as a structured list (UX audit W6): one row per issue group with a
    severity icon and word, a fix button for blocking errors ("Choose technology…" opens the
    Process workspace), a click on a row with a place in the drawing selects it; information
    rows (cutting-only issues of a laser-off run) need no acknowledgement.  Buttons: Close when
    something blocks, else "<Action> anyway" / Cancel (Cancel the default)."""

    def __init__(self, report, parent=None, title: str = "", actions=()):
        super().__init__(parent)
        from PySide6.QtWidgets import QListWidget, QListWidgetItem
        self.report = report
        self.chosen = None              # the extra action the operator picked (``actions``)
        self.fixes: dict[str, QPushButton] = {}
        win = parent.window() if parent is not None else None
        self._win = win
        tid, tdef, gid, gdef = PREFLIGHT_TITLES.get(report.action, (
            "safety.preflight.title", "Pre-flight check: {action}", "safety.preflight.continue", "Continue"))
        self.setWindowTitle(title or _tr(win, tid, tdef, action=report.action))
        lay = style.vbox(self, "dialog")
        head = QLabel()
        head.setTextFormat(Qt.PlainText)
        head.setWordWrap(True)
        style.set_font(head, "label_strong")
        if report.errors:
            head.setText(_trn(win, "safety.preflight.errors", len(report.errors)))
        elif report.warnings:
            head.setText(_trn(win, "safety.preflight.warnings", len(report.warnings)))
        else:
            head.setText(_tr(win, "safety.preflight.info_only", "For your information:"))
        lay.addWidget(head)
        self.list = QListWidget()
        self.list.setObjectName("preflight_issues")
        self.list.setWordWrap(True)
        self.list.setAccessibleName(_tr(win, "safety.preflight.list", "Pre-flight issues"))
        st = self.style()
        self.groups = preflight_groups(report)
        for sev, code, items in self.groups:
            icon_name, sid, sdef = _SEVERITY.get(sev, _SEVERITY["warning"])
            text = self._group_text(win, code, items)
            it = QListWidgetItem(st.standardIcon(getattr(QStyle, icon_name)),
                                 _tr(win, "safety.preflight.row", "{severity}: {message}",
                                     severity=_tr(win, sid, sdef), message=text))
            it.setData(Qt.UserRole, (sev, code))
            it.setToolTip("\n".join(i.message for i in items))
            self.list.addItem(it)
        self.list.itemActivated.connect(self._focus)
        self.list.itemClicked.connect(self._focus)
        lay.addWidget(self.list, 1)
        extra = []
        if report.extent is not None:
            x0, y0, x1, y1 = report.extent
            u = units.current()
            n = lambda v: u.number(v, "length", 1 if u.unit("length") == "mm" else 2)  # noqa: E731
            extra.append(_tr(win, "safety.preflight.extent", "job extent X {x0}..{x1}  Y {y0}..{y1} {unit}",
                             x0=n(x0), x1=n(x1), y0=n(y0), y1=n(y1), unit=u.suffix("length")))
        if report.travel is not None:
            u = units.current()
            tv = report.travel
            g = lambda v: u.number(v, "length", trim=True)  # noqa: E731
            extra.append(_tr(win, "safety.preflight.travel", "travel X {x0}..{x1} {unit}, Y {y0}..{y1} {unit} ({source})",
                             x0=g(tv.x[0]), x1=g(tv.x[1]), y0=g(tv.y[0]), y1=g(tv.y[1]), unit=u.suffix("length"),
                             source=tv.source))
        if extra:
            foot = QLabel("\n".join(extra))
            foot.setTextFormat(Qt.PlainText)
            foot.setWordWrap(True)
            foot.setObjectName("preflight_extent")
            lay.addWidget(foot)
        bb = QDialogButtonBox()
        if report.errors:
            close = bb.addButton(QDialogButtonBox.Close)
            close.setDefault(True)
            bb.rejected.connect(self.reject)
            for sev, code, _items in self.groups:
                fix = PREFLIGHT_FIXES.get(code)
                if fix is None or code in self.fixes or sev != "error":
                    continue
                fid, fdef, cid = fix
                b = bb.addButton(_tr(win, fid, fdef), QDialogButtonBox.ActionRole)
                b.setObjectName(f"preflight_fix_{code}")
                b.clicked.connect(lambda _=False, c=cid: self._fix(c))
                self.fixes[code] = b
        else:
            self.go = bb.addButton(_tr(win, gid, gdef, action=report.action), QDialogButtonBox.AcceptRole)
            cancel = bb.addButton(QDialogButtonBox.Cancel)
            self.go.setDefault(False)
            self.go.setAutoDefault(False)
            cancel.setDefault(True)            # Enter never confirms a warning by accident
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
        for label, fn in actions:       # e.g. "Repair..." for open contours (contributors)
            b = bb.addButton(label, QDialogButtonBox.ActionRole)
            b.clicked.connect(lambda _=False, f=fn: self._act(f))
        lay.addWidget(bb)
        self.resize(600, 320)

    @staticmethod
    def _group_text(win, code: str, items: list) -> str:
        """One line per group: "4 layers have no technology (1, 2, 3, 4)", else the message (the
        first one and "+n more" for a longer group)."""
        if code == "layer_undefined" and len(items) > 1:
            layers = ", ".join(str(i.where[0]) for i in items if i.where)
            return _trn(win, "safety.preflight.layers_undefined", len(items), layers=layers)
        if len(items) == 1:
            return items[0].message
        return _tr(win, "safety.preflight.more", "{message} (+{n} more)", message=items[0].message,
                   n=len(items) - 1)

    def _fix(self, cmd_id: str) -> None:
        """A fix button: close the dialog (the action does not run) and run the fix command."""
        self.chosen = cmd_id
        self.reject()
        cmds = getattr(self._win, "commands", None)
        if cmds is None:
            return
        try:
            cmds.run(cmd_id)
        except Exception:  # noqa: BLE001 - a missing command (another controller) only logs
            log.exception("pre-flight fix %s", cmd_id)

    def _focus(self, item) -> None:
        """Select the part an issue points at (``Issue.where`` = (part index, contour index))."""
        sev_code = item.data(Qt.UserRole)
        if not sev_code:
            return
        for sev, code, items in self.groups:
            if (sev, code) != tuple(sev_code):
                continue
            where = next((i.where for i in items if i.where), None)
            canvas = getattr(self._win, "canvas", None)
            if where is None or code == "layer_undefined" or canvas is None or len(where) != 2:
                return
            try:
                canvas.select_parts([int(where[0])])
            except Exception:  # noqa: BLE001 - selection is a convenience
                log.debug("pre-flight focus failed", exc_info=True)
            return

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
        # NOT HOMED (UX audit W3): the machine needs homing before a job; the fix at hand
        self.home_btn = QPushButton(_tr(ui.w, "safety.banner.home", "Home axes"))
        self.home_btn.setObjectName("safety_home")
        self.home_btn.setMinimumHeight(style.TARGET)
        self.home_btn.clicked.connect(lambda _=False: self.ui.home())
        self.home_btn.setVisible(False)
        h.addWidget(self.home_btn)
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

    def refresh(self, st, need_home: bool = False) -> None:
        kind = st.banner if st is not None else "OFFLINE"
        self.kind = kind
        bg, fg = COLORS.get(kind, COLORS["OFFLINE"])
        w = self.ui.w
        key, default = TEXTS.get(kind, ("", kind))
        text = _tr(w, key, default) if key else kind
        if kind == "FAULT":
            # the plain headline; the raw reason is the tooltip and the alarm banner's Details
            text = _tr(w, "safety.banner.fault_latched", "FAULT (latched): {fault}",
                       fault=headline(st.fault, getattr(w, "tr_", None)))
        elif st is not None and st.simulator and kind != "SIMULATOR":
            text = _tr(w, "safety.banner.with_simulator", "{state}  · simulator", state=text)
        if st is not None and kind == "SIMULATOR":
            text = _tr(w, "safety.banner.simulator_disarmed", "SIMULATOR: disarmed") if not st.armed else text
        if self.label.text() != text:
            self.label.setText(text)
        tip = st.fault if kind == "FAULT" and st is not None else ""
        if self.label.toolTip() != tip:
            self.label.setToolTip(tip)
        qss = f"background:{bg}; color:{fg};"
        if qss != self._style:                 # re-polishing is expensive: only on a change
            self._style = qss
            self.setStyleSheet(qss)
            self.label.setStyleSheet(f"color:{fg}; background:transparent;")
        # FAULT has no button here: the alarm banner below (always shown with a fault) carries
        # the one "Reset alarm" (UX audit W5: one banner, one reset)
        label = {"ARMED": _tr(w, "run.btn.disarm", "Disarm laser"),
                 "DISARMED": _tr(w, "run.btn.arm", "Arm laser"),
                 "SIMULATOR": _tr(w, "run.btn.arm", "Arm laser")}.get(kind)
        self.btn.setVisible(label is not None)
        if label:
            self.btn.setText(label)
        home = bool(need_home) and kind != "FAULT"
        if self.home_btn.isHidden() == home:
            self.home_btn.setVisible(home)


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
            title = _tr(w, "safety.alarm.title", "ALARM {code}: {text}", code=code, text=txt)
            if len(alarms) > 1:
                title = _tr(w, "safety.alarm.with_more", "{text}  (+{n} more)", text=title, n=len(alarms) - 1)
            if t:
                title = _tr(w, "safety.alarm.with_since", "{text}   since {time}", text=title,
                            time=time.strftime('%H:%M:%S', time.localtime(t)))
            if st is not None and st.banner == "FAULT":
                # one banner for a fault and the alarms together, one Reset alarm (UX audit W5)
                title = _tr(w, "safety.alarm.with_fault", "{text}  · FAULT: {fault}", text=title,
                            fault=headline(st.fault, tr))
            self.title.setText(title)
            self.title.setToolTip(st.fault if st is not None and st.banner == "FAULT" else "")
            self.remedy.setText(_tr(w, "safety.alarm.remedy", "Remedy: {text}", text=remedy(a, tr)))
            return True
        if st is not None and st.banner == "FAULT":
            w = self.ui.w
            self.title.setText(_tr(w, "safety.alarm.fault_since", "FAULT: {fault}   since {time}",
                                   fault=headline(st.fault, tr),
                                   time=time.strftime('%H:%M:%S', time.localtime(st.fault_t))))
            self.title.setToolTip(st.fault)
            self.remedy.setText(_tr(w, "safety.alarm.remedy", "Remedy: {text}", text=remedy(st.fault, tr)))
            return True
        return False


class _RunStrip(QWidget):
    """The run bar's controls in their order, in one row while it fits and in two rows when
    the window is narrower (a half-screen window): every control keeps its full text and its
    size, nothing goes into an overflow menu, the order (and so the keyboard order) is the
    same.  The row break is after Stop; the width the bar needs is that of the wider row."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("run_bar_strip")
        from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(style.SP1)
        self._rows = (QHBoxLayout(), QHBoxLayout())
        for r in self._rows:
            r.setContentsMargins(0, 0, 0, 0)
            r.setSpacing(style.SP1)
            self._v.addLayout(r)
        self.items: list[QWidget] = []
        self.first_row = 0          # how many items the first row holds when wrapped
        self.one_row_need = 0
        self.wrapped = False

    def add(self, w: QWidget, stretch: int = 0) -> None:
        w.setProperty("run_strip_stretch", stretch)
        self.items.append(w)
        self._rows[0].addWidget(w, stretch)

    def row_needs(self) -> tuple[int, int, int]:
        """(one row, first row, second row): the minimum widths of the arrangements."""
        sp = self._rows[0].spacing()

        def need(ws: list[QWidget]) -> int:
            shown = [w for w in ws if not w.isHidden()]
            return sum(max(w.minimumWidth(), w.minimumSizeHint().width()) for w in shown) + sp * max(len(shown) - 1, 0)
        k = self.first_row
        return need(self.items), need(self.items[:k]), need(self.items[k:])

    def set_wrapped(self, on: bool) -> None:
        if on == self.wrapped:
            return
        self.wrapped = on
        k = self.first_row if on else len(self.items)
        for r in self._rows:
            while r.count():
                r.takeAt(0)
        for i, w in enumerate(self.items):
            self._rows[0 if i < k else 1].addWidget(w, int(w.property("run_strip_stretch") or 0))
        self._v.invalidate()
        self.updateGeometry()

    def fit(self) -> None:
        self.set_wrapped(self.width() < self.one_row_need)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        # the width of the wrapped arrangement whichever is shown: the window may narrow to it
        # (it then wraps); the height of the arrangement shown
        s = super().minimumSizeHint()
        _one, first, second = self.row_needs()
        return QSize(max(first, second), s.height())

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        # the width is the window's (the bar spans it), never this choice's: no feedback
        self.fit()


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
        self.pill = QLabel(" " + _tr(w, "run.state.offline", "OFFLINE") + " ")  # i18n: ok (padding)
        self.pill.setObjectName("run_state")
        self.pill.setAlignment(Qt.AlignCenter)
        style.set_font(self.pill, "label_strong")
        self.buttons: dict[str, QPushButton] = {}
        self.strip = _RunStrip()
        self.strip.add(self.pill)
        # one factory for the run controls (style.run_button: the glossary's words, icon before
        # the text, 56 px, STOP filled in the danger colour), the same as the Run screen's
        for key, fn in (("start", ui.start), ("hold", ui.hold), ("stop", ui.stop), ("frame", ui.frame),
                        ("dry_run", ui.dry_run), ("arm", ui.toggle_arm)):
            b = style.run_button(key, tr, fn)
            self.buttons[key] = b
            if key != "start":
                self.strip.add(b)
                if key == "stop":
                    # two rows when narrow: the state, Start, Hold and Stop on the first
                    self.strip.first_row = len(self.strip.items)
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
                self.strip.add(box)
                self.start_menu = more
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFormat("")
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.strip.add(self.progress, 1)
        self.eta = QLabel("")
        self.strip.add(self.eta)
        self.strip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.addWidget(self.strip)
        # the longer texts the buttons take in their other state
        self._alt = {k: style.run_text(k, tr, alt=True) for k in ("start", "hold", "arm")}
        self.fit()
        self._style = ""

    def fit(self) -> None:
        """Every button's full text fits, also its longer state (Continue, Disarm), and the bar
        never hides a button in its overflow menu: its minimum width is what they need in two
        rows (:class:`_RunStrip`: one row when the window has room), so the window's minimum
        follows (docs/dev/RESPONSIVE.md)."""
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
        st = self.style()
        chrome = 0
        if lay is not None:
            m = lay.contentsMargins()
            chrome += m.left() + m.right()
        chrome += 2 * st.pixelMetric(QStyle.PM_ToolBarFrameWidth, None, self) + 2 * st.pixelMetric(
            QStyle.PM_ToolBarItemMargin, None, self)
        if self.isMovable():
            chrome += st.pixelMetric(QStyle.PM_ToolBarHandleExtent, None, self)
        one, first, second = self.strip.row_needs()
        # one row while the strip has the width every control needs side by side; the bar's
        # minimum is the wider of the two rows (a half-screen window wraps it)
        self.strip.one_row_need = one
        self.setMinimumWidth(max(first, second) + chrome)
        self.strip.fit()

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange) and getattr(self, "_alt", None) is not None:
            self.fit()

    #: run state -> (catalogue id, default, colour key of COLORS)
    PILL = {"offline": ("run.state.offline", "OFFLINE", "OFFLINE"), "ready": ("run.state.ready", "READY", "READY"),
            "running": ("run.state.running", "RUNNING", "RUNNING"), "hold": ("run.state.hold", "HOLD", "HOLD"),
            "alarm": ("run.state.alarm", "ALARM", "ALARM"), "fault": ("run.state.fault", "FAULT", "FAULT"),
            "estop": ("run.state.estop", "E-STOP", "ALARM"),
            "not_homed": ("run.state.not_homed", "NOT HOMED", "DISARMED")}

    def _pill_texts(self) -> list[str]:
        return [_tr(self.ui.w, k, d) for k, d, _c in self.PILL.values()]

    def run_state(self, st, ms, alarms=None, need_home: bool = False) -> str:
        """offline / fault / estop / alarm / hold / running / not_homed / ready: the machine's
        run state.  ``alarms``: the card's active alarms (``ctl.active_alarms``, the one alarm
        source the banner and the status chips read too, UX audit C2); None reads them from the
        controller.  ``need_home``: :func:`mlaser.services.readiness.homing_needed`."""
        if alarms is None:
            alarms = active_alarms(self.ui.w)
        return run_state(st, ms, alarms, need_home)

    def refresh(self, st, ms, running: bool, eta: str, alarms=None, need_home: bool = False) -> None:
        kind = st.banner if st is not None else "OFFLINE"
        rs = self.run_state(st, ms, alarms, need_home)
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
        for k, on, why in run_enabled(self.ui.w, st, ms, running, alarms):
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


def active_alarms(window) -> list:
    """The card's active alarms: THE alarm source of the run state, the banners, the status
    chips and the run controls' enabling (``ctl.active_alarms``, re-read after every status and
    after a reset; UX audit C2)."""
    ctl = _ctl(window)
    try:
        return list(ctl.active_alarms or ()) if ctl is not None else []
    except Exception:  # noqa: BLE001
        return []


def alarm_text(window, a) -> str:
    """An alarm's text in the UI language."""
    tr = getattr(window, "tr_", None)
    return a.text(tr) if hasattr(a, "text") else str(a)


def run_state(st, ms, alarms=(), need_home: bool = False) -> str:
    """The machine's run state from the safety status ``st``, the machine state ``ms`` and the
    card's active ``alarms``: ``fault`` > ``estop`` (an emergency-stop alarm) > ``alarm`` >
    ``offline`` > ``hold`` > ``running`` > ``not_homed`` > ``ready``.  Never ``ready`` while an
    alarm is active (UX audit C2/C3/W3)."""
    if st is not None and st.banner == "FAULT":
        return "fault"
    alarms = list(alarms or ())
    if any(getattr(a, "kind", "") == "estop" for a in alarms):
        return "estop"
    if alarms:
        return "alarm"
    if ms is None or not ms.connected:
        return "offline"
    if ms.mode == "pause":
        return "hold"
    if ms.mode in ("process", "dry_run", "frame"):
        return "running"
    if need_home:
        return "not_homed"
    return "ready"


def run_enabled(window, st, ms, running: bool, alarms=None) -> list[tuple[str, bool, str]]:
    """(run control, enabled, why not) for the run bar and the Run screen: one rule set.
    STOP is always enabled.  ``alarms``: the card's active alarms (None: read from the
    controller, :func:`active_alarms`): while one is active Start, Frame and Dry run are
    disabled with "Reset the alarm first: <alarm>" (UX audit C4), and so is Arm while an
    e-stop or servo alarm is active (C3)."""
    connected = bool(ms is not None and ms.connected)
    fault = st is not None and st.banner == "FAULT"
    paused = ms is not None and ms.mode == "pause"
    dry = bool(getattr(st, "dry_run", False))
    if alarms is None:
        alarms = active_alarms(window)
    alarms = list(alarms or ())
    no_conn = _tr(window, "run.why.offline", "Connect the machine or the simulator first")
    why_fault = _tr(window, "run.why.fault", "Reset the fault first")
    why_running = _tr(window, "run.why.running", "Not while a job runs")
    why_alarm = (_tr(window, "run.why.alarm", "Reset the alarm first: {alarm}", alarm=alarm_text(window, alarms[0]))
                 if alarms else "")
    from ..controller.alarms import DISARM_KINDS
    arm_alarm = next((a for a in alarms if getattr(a, "kind", "") in DISARM_KINDS), None)

    def reason(*checks: tuple[bool, str]) -> tuple[bool, str]:
        for bad, why in checks:
            if bad:
                return False, why
        return True, ""
    out = []
    ok, why = reason((not connected, no_conn), (fault, why_fault), (bool(alarms), why_alarm),
                     (running and not paused, why_running))
    out.append(("start", ok, why))
    ok, why = reason((not connected, no_conn), (not running, _tr(window, "run.why.no_job", "No job is running")))
    out.append(("hold", ok, why))
    out.append(("stop", True, ""))
    for k in ("frame", "dry_run"):
        ok, why = reason((not connected, no_conn), (fault, why_fault), (bool(alarms), why_alarm), (running, why_running))
        out.append((k, ok, why))
    armed = bool(getattr(st, "armed", False))
    ok, why = reason((not connected, no_conn), (fault, why_fault),
                     (dry, _tr(window, "run.why.laser_off", "Laser output is disabled (dry-run mode)")),
                     (arm_alarm is not None and not armed,           # Disarm always stays possible
                      _tr(window, "run.why.alarm", "Reset the alarm first: {alarm}",
                          alarm=alarm_text(window, arm_alarm)) if arm_alarm is not None else ""))
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
        if key == Qt.Key_Escape and self._in_dialog(obj):
            # Esc in a dialog while a job runs: the job stops (unchanged, docs/SAFETY.md) AND
            # the key goes on to the dialog, so its own Esc = Cancel still works.  The event
            # filter sees the key once per widget it propagates through: stop once.
            if not self._esc_taken:
                self._esc_taken = True
                QTimer.singleShot(0, self._esc_reset)
                self.ui.stop()
            return False
        if key == Qt.Key_Space:
            self.ui.hold()
        else:
            self.ui.stop()
        return True

    _esc_taken = False

    def _esc_reset(self) -> None:
        self._esc_taken = False

    def _in_dialog(self, obj) -> bool:
        """``obj`` is in a dialog window (not the main window)."""
        win = obj.window() if isinstance(obj, QWidget) else None
        return isinstance(win, QDialog) and win is not self.ui.w


class KeptNotice(QWidget):
    """The resume point a Stop while held kept (UX audit W17): "Stopped: resume point kept at
    X…, Y… mm" with *Resume from here* (the breakpoint comes back; Start / Continue resumes it
    with its pre-flight) and *Discard*."""

    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        self.ui = ui
        w = ui.w
        h = style.hbox(self)
        h.setContentsMargins(style.SP2, style.SP1, style.SP2, style.SP1)
        self.label = QLabel()
        self.label.setObjectName("kept_label")
        self.label.setWordWrap(True)
        h.addWidget(self.label, 1)
        self.resume = QPushButton(_tr(w, "safety.kept.resume", "Resume from here"))
        self.resume.setObjectName("kept_resume")
        self.resume.setMinimumHeight(style.TARGET)
        self.resume.clicked.connect(lambda _=False: ui.resume_kept())
        self.discard = QPushButton(_tr(w, "safety.kept.discard", "Discard"))
        self.discard.setObjectName("kept_discard")
        self.discard.setMinimumHeight(style.TARGET)
        self.discard.clicked.connect(lambda _=False: ui.discard_kept())
        h.addWidget(self.resume)
        h.addWidget(self.discard)

    def show_point(self, bp) -> None:
        text = _tr(self.ui.w, "safety.kept.text", "Stopped: resume point kept at X {x}, Y {y} mm",
                   x=f"{float(getattr(bp, 'x', 0.0)):.1f}", y=f"{float(getattr(bp, 'y', 0.0)):.1f}")
        if self.label.text() != text:
            self.label.setText(text)
            a11y.name(self, text)


class SafetyUI(QObject):
    #: (ticket, ok, message) of a Start that armed the machine first (any thread -> GUI thread)
    _start_result = Signal(object, bool, str)
    need_home = False

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.dispatcher = qtbridge.QtDispatcher(window)
        self._subs: list = []
        self._ctl = None
        self._alarm_since: dict = {}
        self._job_t0 = 0.0
        self._spoken_alarm: object = None      # the alarm / fault last announced (F7)
        self._spoken_state: Optional[str] = None
        self._spoken_running = False
        self.top = QToolBar(_tr(window, "safety.bar.state", "Safety state"), window)
        self.top.setObjectName("safety_banner")
        self.top.setMovable(False)
        self.top.setFloatable(False)
        self.top.toggleViewAction().setVisible(False)
        self.banner = StateBanner(self)
        self.banner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.top.addWidget(self.banner)
        window.addToolBar(Qt.TopToolBarArea, self.top)
        window.addToolBarBreak(Qt.TopToolBarArea)
        self.alarm_bar = QToolBar(_tr(window, "safety.bar.alarm", "Alarm"), window)
        self.alarm_bar.setObjectName("alarm_banner")
        self.alarm_bar.setMovable(False)
        self.alarm_bar.setFloatable(False)
        self.alarm_bar.toggleViewAction().setVisible(False)
        self.alarm = AlarmBanner(self)
        self.alarm.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.alarm_bar.addWidget(self.alarm)
        window.addToolBar(Qt.TopToolBarArea, self.alarm_bar)
        self.alarm_bar.setVisible(False)
        # "Stopped: resume point kept · Resume · Discard" after a Stop while held (UX audit W17):
        # Stop stays immediate (no question); the resume point is not silently thrown away
        self.kept_bar = QToolBar(_tr(window, "safety.bar.kept", "Resume point"), window)
        self.kept_bar.setObjectName("kept_resume_point")
        self.kept_bar.setMovable(False)
        self.kept_bar.setFloatable(False)
        self.kept_bar.toggleViewAction().setVisible(False)
        self.kept = KeptNotice(self)
        self.kept.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.kept_bar.addWidget(self.kept)
        window.addToolBar(Qt.BottomToolBarArea, self.kept_bar)
        window.addToolBarBreak(Qt.BottomToolBarArea)
        self.kept_bar.setVisible(False)
        self.run_bar = RunBar(self)
        window.addToolBar(Qt.BottomToolBarArea, self.run_bar)
        self._start_result.connect(self._start_done)
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
            self._subs.append(bus.subscribe(EV.CommandsPending, self._commands_pending))
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

    # a card command waiting longer than this shows "Waiting for the card" in the status bar
    BUSY_AFTER_MS = 400

    def _commands_pending(self, ev) -> None:
        """The command worker's queue (controller.commands): a command the card has not
        answered for a moment is shown as pending, never as a frozen window (issue #5)."""
        t = getattr(self, "_busy_timer", None)
        if ev.pending > 0:
            if t is None:
                t = self._busy_timer = QTimer(self)
                t.setSingleShot(True)
                t.timeout.connect(self._show_busy)
            if not t.isActive() and not getattr(self, "_busy_text", ""):
                t.start(self.BUSY_AFTER_MS)
            return
        if t is not None:
            t.stop()
        text = getattr(self, "_busy_text", "")
        if text:
            self._busy_text = ""
            sb = self.w.statusBar()
            if sb.currentMessage() == text:
                sb.clearMessage()

    def _show_busy(self) -> None:
        if not self.w.ctl.commands_pending():
            return
        self._busy_text = _tr(self.w, "run.card_busy", "Waiting for the card to answer…")
        self.w.statusBar().showMessage(self._busy_text)

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
        alarms = active_alarms(self.w)          # the one alarm source (UX audit C2)
        from ..services.readiness import homing_needed
        running = job_running(self.w)
        need_home = (not running and ms is not None and ms.mode not in ("pause",) and
                     ctl.breakpoint is None and homing_needed(ctl))
        self.need_home = need_home
        self.banner.refresh(st, need_home and not alarms)
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
        self._refresh_kept(ctl, running)
        self._announce(st, alarms, show, running)
        self._set_filter(running)
        keep_awake(running)                 # no idle system sleep while a job runs (macOS, Windows)
        eta = ""
        if running and ms is not None and ms.progress > 0.02 and self._job_t0:
            el = time.monotonic() - self._job_t0
            left = el * (1.0 - ms.progress) / ms.progress
            eta = f"{int(el // 60)}:{int(el % 60):02d} / ETA {int(left // 60)}:{int(left % 60):02d}"
        self.run_bar.refresh(st, ms, running, eta, alarms, need_home)
        self._status_override(st, alarms, ms)
        panel = getattr(self.w, "control", None)
        if panel is not None and hasattr(panel, "safety_refresh"):
            panel.safety_refresh(st, ms)

    def _announce(self, st, alarms: list, alarm_shown: bool, running: bool) -> None:
        """Tell assistive technology about changes nobody focused (UI evaluation F7): a new
        alarm or fault (assertive, with its remedy), alarms cleared, a safety state change, a
        job that started or ended (polite). Only transitions are spoken, never a refresh tick."""
        w = self.w
        alarm_key = (getattr(alarms[0], "code", str(alarms[0])) if alarms
                     else ("fault", st.fault) if st is not None and st.banner == "FAULT" else None)
        if alarm_key != self._spoken_alarm:
            if alarm_key is not None and alarm_shown:
                title, rem = self.alarm.title.text(), self.alarm.remedy.text()
                a11y.name(self.alarm, title, rem)
                a11y.announce(self.alarm, _tr(w, "a11y.alarm.spoken", "{title}\n{remedy}", title=title, remedy=rem),
                              assertive=True, force=True)
            elif self._spoken_alarm is not None:
                a11y.name(self.alarm, "", "")
                a11y.announce(self.alarm, _tr(w, "a11y.alarm.cleared", "Alarms cleared"), force=True)
            self._spoken_alarm = alarm_key
        kind = self.banner.kind
        if kind != self._spoken_state:
            if self._spoken_state is not None and kind != "FAULT":     # a fault is spoken above
                a11y.announce(self.banner, self.banner.label.text(), assertive=kind == "ARMED", force=True)
            self._spoken_state = kind
        a11y.name(self.banner, self.banner.label.text())
        if running != self._spoken_running:
            if running:
                a11y.announce(self.run_bar, _tr(w, "a11y.job.started", "Job started"), force=True)
            elif self._spoken_running:
                a11y.announce(self.run_bar, _tr(w, "a11y.job.ended", "Job ended"), force=True)
            self._spoken_running = running

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
        if st is not None and st.banner == "FAULT":
            text = _tr(self.w, "status.motion_fault", "Motion card: FAULT")
        elif alarms:
            text = _tr(self.w, "status.motion_alarm", "Motion card: ALARM {code}",
                       code=getattr(alarms[0], 'code', ''))
        else:
            text = ""
        lbl.set_alert(text)                      # the status-bar chip (mainwindow.StatusChip)

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

    def preflight_ok(self, action: str, doc=None, emits: Optional[bool] = None, selected_only: bool = False,
                     assume_armed: bool = False) -> bool:
        """Run the controller's pre-flight check; show the dialog when it finds anything.
        ``assume_armed``: check as if the machine were armed (Start while disarmed checks
        everything else first and only then asks to arm, UX audit C1); every other issue is
        checked as usual.  Cutting-only warnings of a laser-off run are information (W6): with
        nothing else to report no dialog is shown."""
        from dataclasses import replace

        from ..machine.preflight import demote_cutting_only
        ctl = self.w.ctl
        try:
            rep = ctl.preflight(action, doc, selected_only, extent=self._extent(doc) if doc else None,
                                emits=emits)
        except TypeError:
            rep = ctl.preflight(action, doc)
        if assume_armed:
            rep = replace(rep, issues=[i for i in rep.issues if i.code != "not_armed"])
        from .safety_io import augment_preflight          # "interlock not configured" warnings
        rep = augment_preflight(self.w, rep, action, emits)
        from .workspaces.cutting_strategy import augment_preflight as augment_cutting
        rep = augment_cutting(self.w, rep, action, doc, selected_only)   # lead / tip warnings
        rep = contribute_preflight(self.w, rep, action, doc, emits)   # plugins (material check)
        rep = demote_cutting_only(rep)
        self.last_report = rep
        if not rep.errors and not rep.warnings:
            if rep.infos:                      # information only: said, never to acknowledge
                first = preflight_groups(rep)[0]
                tid, tdef, _g, _gd = PREFLIGHT_TITLES.get(action, ("safety.preflight.title", "Pre-flight check: {action}",
                                                                    "", ""))
                self.w.statusBar().showMessage(_tr(
                    self.w, "safety.preflight.info_status", "{title}: {message}",
                    title=_tr(self.w, tid, tdef, action=action),
                    message=PreflightDialog._group_text(self.w, first[1], first[2])), 8000)
            return True                        # nothing, or information only
        if not self._dialogs():
            return rep.ok
        rep = override_preflight(self.w, rep)
        if not rep.errors and not rep.warnings:
            return True
        actions = [a for c in _contributors(self.w) for a in (c.actions(rep) if hasattr(c, "actions") else ())]
        d = PreflightDialog(rep, self.w, actions=actions)
        return bool(d.exec()) and rep.ok

    last_report = None             # the last pre-flight report preflight_ok() decided on (tests)

    def _after(self, what: str) -> None:
        """Show the controller's own refusal (exact job extent, safety state) after an action."""
        rep = self.w.ctl.last_preflight
        if rep is not None and not rep.ok and self._dialogs() and rep is not self._shown:
            self._shown = rep
            PreflightDialog(rep, self.w, what).exec()

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
            self._after(_tr(self.w, "safety.refused.resume", "Resume refused"))
            return
        st = _safety(ctl)
        emits = bool(ctl.laser_enabled)
        action = "start" if emits else "dry_run"
        arm_first = emits and st is not None and not st.armed and self._dialogs()
        if arm_first:
            # UX audit C1: the pre-flight check runs first, as if armed (an e-stop, a missing
            # technology or an axis out of travel refuses here, before anything is armed); only
            # then one question with the arming safety text, Cancel the default
            if not self.preflight_ok(action, doc, emits, selected_only=bool(selected_only), assume_armed=True):
                return
            if not self.ask_arm_and_start():
                return
            if not self.arm(confirm=False):
                return
        elif not self.preflight_ok(action, doc, emits, selected_only=bool(selected_only)):
            return
        self._shown = None
        self._run_start(doc, bool(selected_only), arm_first)

    def ask_arm_and_start(self) -> bool:
        """The one question of a Start while disarmed: the arming safety text, "Arm and start" /
        "Cancel" (Cancel the default), the warning icon (UX audit C1, O13)."""
        return ask(self.w, "start_disarmed", _tr(self.w, "safety.ask.start_disarmed",
                                                 "The machine is not armed: the laser cannot fire."),
                   _tr(self.w, "safety.ask.start_title", "Arm and start"),
                   yes_text=_tr(self.w, "safety.ask.arm_and_start", "Arm and start"), warning=True,
                   informative=_tr(self.w, "safety.ask.arm_info", "Laser emission will be enabled: the beam "
                                   "fires on Start, Laser and laser tests. Make sure the work area is clear "
                                   "and the enclosure is closed."))

    _arm_token: object = None       # the Start that armed the machine itself (C1)

    def _run_start(self, doc, selected_only: bool, armed_here: bool) -> None:
        """Hand the start to the planned-job runner; when this Start armed the machine and the
        start is refused for any reason (compile error, start gate, the controller's own
        pre-flight, a Stop meanwhile) the machine is disarmed again and the operator told
        (:meth:`_start_done`): a refused start never leaves the machine armed."""
        runner = self._runner()
        token = object() if armed_here else None
        self._arm_token = token
        if token is None:
            runner.start(doc, selected_only=selected_only)
            self._after(_tr(self.w, "safety.refused.start", "Start refused"))
            return

        def result(ok: bool, msg: str = "") -> None:
            self._start_result.emit(token, bool(ok), str(msg or ""))
        if getattr(runner, "reports_start_result", False):
            runner.start(doc, selected_only=selected_only, on_result=result)
        else:
            ret = runner.start(doc, selected_only=selected_only)
            ctl = self.w.ctl
            qtbridge.when_done(ret, lambda r: result(r is not False and bool(ctl.job_active())))
        if self._arm_token is token:
            self._after(_tr(self.w, "safety.refused.start", "Start refused"))

    def _start_done(self, token, ok: bool, msg: str) -> None:
        if token is None or token is not self._arm_token:
            return
        self._arm_token = None
        if ok:
            return
        ctl = self.w.ctl
        st = _safety(ctl)
        if st is not None and st.armed and not ctl.job_active():
            ctl.disarm("disarmed: the start was refused")
            text = _tr(self.w, "safety.refused.start_disarmed", "Start refused: the laser was disarmed again.")
            self._log(text)
            self.w.statusBar().showMessage(text, 10000)
        self.refresh()
        self._after(_tr(self.w, "safety.refused.start_disarmed_title", "Start refused, laser disarmed"))

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
        self._after(_tr(self.w, "safety.refused.frame", "Frame refused"))

    def dry_run(self) -> None:
        doc = self.w.document
        if not self.preflight_ok("dry_run", doc, False):
            return
        self._shown = None
        self._runner().dry_run(doc)
        self._after(_tr(self.w, "safety.refused.dry_run", "Dry run refused"))

    def arm(self, confirm: bool = True) -> bool:
        ctl = self.w.ctl
        if confirm and self._dialogs():
            if not ask(self.w, "arm", _tr(self.w, "safety.ask.arm_question", "Arm the laser?"),
                       _tr(self.w, "safety.ask.arm_title", "Arm laser"), default_yes=False,
                       yes_text=_tr(self.w, "run.btn.arm", "Arm laser"), warning=True,
                       informative=_tr(self.w, "safety.ask.arm_info", "Laser emission will be enabled: the beam "
                                       "fires on Start, Laser and laser tests. Make sure the work area is clear "
                                       "and the enclosure is closed.")):
                return False
        ok = bool(ctl.arm())
        if not ok and self._dialogs():
            QMessageBox.warning(self.w, _tr(self.w, "safety.refused.arm", "Arm refused"),
                                self._last_reason() or _tr(self.w, "safety.refused.arm_text", "arming refused"))
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
        """"Reset alarm" (the one reset of the banner, the Run screen, the dock and the menu,
        UX audit W5): a latched fault is reset (the card alarms with it), else the card alarms.
        Nothing is reported as reset while the card still reports its cause (C2): "Reset
        refused: <alarm> still active — <remedy>"."""
        ctl = self.w.ctl
        st = _safety(ctl)
        if st is not None and st.banner != "FAULT" and active_alarms(self.w):
            # the reset reads the card again: queued on the command worker (issue #5)
            qtbridge.when_done(ctl.clear_alarms(), self._reset_done)
            return
        qtbridge.when_done(ctl.reset_fault(), self._reset_done)

    #: the last reset refusal shown (tests; the dialog is only shown with dialogs)
    last_reset_refusal = ""

    def _reset_done(self, ok) -> None:
        self.refresh()
        left = active_alarms(self.w)
        st = _safety(self.w.ctl)
        if ok is not False and not left and not (st is not None and st.banner == "FAULT"):
            sb = self.w.statusBar()
            if self.last_reset_refusal and sb.currentMessage() == self.last_reset_refusal:
                sb.clearMessage()                 # the earlier refusal no longer holds
            self.last_reset_refusal = ""
            return
        if left:
            a = left[0]
            text = _tr(self.w, "safety.refused.reset_alarm", "Reset refused: {alarm} still active — {remedy}",
                       alarm=alarm_text(self.w, a), remedy=remedy(a, getattr(self.w, "tr_", None)))
        else:
            text = self._last_reason() or _tr(self.w, "safety.refused.reset_text", "reset refused")
        self.last_reset_refusal = text
        self.w.statusBar().showMessage(text, 10000)
        if self._dialogs():
            QMessageBox.warning(self.w, _tr(self.w, "safety.refused.reset", "Reset refused"), text)

    def alarm_details(self) -> None:
        """The active alarms, and a latched fault's raw reason (the detail behind the plain
        headline, UX audit W12)."""
        from .ext_card import AlarmListDialog
        st = _safety(self.w.ctl)
        fault = st.fault if st is not None and st.banner == "FAULT" else ""
        AlarmListDialog(self.w, active_alarms(self.w), fault=fault).exec()

    # ------------------------------------------------------------------ readiness (W3)
    def readiness(self) -> list:
        """The start prerequisites (:func:`mlaser.services.readiness.readiness`): items with
        ``id``, ``ok``, ``label``, ``reason`` and ``action`` (a command id) for a readiness
        strip."""
        from ..services.readiness import readiness
        sio = None
        b = getattr(self.w, "safety_io", None)
        if b is not None and hasattr(b, "config"):
            try:
                sio = b.config()
            except Exception:  # noqa: BLE001
                sio = None
        return readiness(self.w.ctl, self.w.document, sio)

    def run_readiness_action(self, action: str) -> None:
        """Run a readiness item's fix (its command id)."""
        cmds = getattr(self.w, "commands", None)
        if action and cmds is not None:
            cmds.run(action)

    def home(self) -> None:
        """NOT HOMED's fix: home the axes (the Machine › Home command, with its question)."""
        cmds = getattr(self.w, "commands", None)
        if cmds is not None and "machine.home" in cmds:
            cmds.run("machine.home")
        else:
            self.w.ctl.home("XY")

    # ------------------------------------------------------------------ kept resume point (W17)
    def _refresh_kept(self, ctl, running: bool) -> None:
        bp = ctl.kept_breakpoint
        show = bp is not None and not running and ctl.breakpoint is None
        if show:
            self.kept.show_point(bp)
        if self.kept_bar.isHidden() == show:
            self.kept_bar.setVisible(show)

    def resume_kept(self) -> None:
        """Bring the kept resume point back (held at it again); Start / Continue then resumes
        with its pre-flight."""
        self.w.ctl.restore_kept_breakpoint()
        self.refresh()

    def discard_kept(self) -> None:
        self.w.ctl.discard_kept_breakpoint()
        self.refresh()


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
            issues = [replace(i, severity=WARNING, message=_tr(window, "safety.preflight.overridden", "overridden: {message}",
                                                               message=i.message)) if i in mine else i
                      for i in issues]
    return replace(rep, issues=issues)


def install(window) -> SafetyUI:
    ui = SafetyUI(window)
    window.safety_ui = ui
    return ui


__all__ = ["install", "SafetyUI", "RunBar", "StateBanner", "AlarmBanner", "PreflightDialog",
           "job_locked", "job_running", "ask", "recovery_prompt"]
