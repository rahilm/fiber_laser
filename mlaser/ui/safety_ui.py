"""Safety UI: state banner, alarm banner, persistent run bar, global Hold/Stop keys, dialogs.

``install(window)`` (called once by :class:`mlaser.ui.mainwindow.MainWindow`) adds:

* the **event dispatcher** (:mod:`.qtbridge`): controller events reach the GUI thread;
* the **state banner** (top, always visible): ``FAULT`` (with the reason and *Reset*), ``ARMED``
  (amber, *Disarm*), ``DRY-RUN``, ``SIMULATOR``, ``DISARMED`` (*Arm*), ``OFFLINE`` (*Connect…*);
* the **alarm banner** (top, shown while card alarms or a fault are active): alarm text, time,
  a remedy (:mod:`.safety_texts`), *Reset* and *Details* (the alarm list dialog); not on the
  Run workspace, whose state block shows the alarm once with *Reset alarm*;
* the **run bar** (bottom toolbar, never inside a scroll area): state pill, Start (F5),
  Hold (Space), Stop (Esc / F12), Frame (F6), Dry run, Arm, progress and ETA, 56 px targets;
  one row, or two (the break after Stop) in a narrow window, every control in full.  On the Run
  workspace only the pill, the progress and the times: the Run screen has the one set of run
  controls there, in the same order;
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
from collections.abc import Mapping
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QDialog,
                               QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QStyle, QTextEdit,
                               QMenu, QToolBar, QToolButton, QWidget)

from .._power import keep_awake

from ..machine import events as EV
from ..services import units
from . import a11y, qtbridge, style
from .responsive import keep_min_size
from .safety_texts import remedy
from .style import TARGET_RUN

log = logging.getLogger("mlaser.ui.safety")

JOB_MODES = ("process", "dry_run", "frame", "pause")

class _StateColors(Mapping):
    """The safety colour table: (background, text) of every machine and laser state in the
    theme shown now (:data:`mlaser.ui.shell.theme.STATE_TOKENS`: neutral normal states, amber
    ARMED, red ALARM, dark red FAULT; docs/SAFETY.md: the state is always written out too, never
    colour alone).  The state banner, the top-bar chips, the run bar's pill and the Run screen
    all read it."""

    def _table(self) -> dict:
        from .shell import theme as T
        return T.STATE_TOKENS.get(style.current_theme(), T.STATE_TOKENS[T.LIGHT])

    def __getitem__(self, kind: str) -> tuple[str, str]:
        return self._table()[kind]

    def __iter__(self):
        return iter(self._table())

    def __len__(self) -> int:
        return len(self._table())


COLORS: Mapping[str, tuple[str, str]] = _StateColors()


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
        head.setTextFormat(Qt.PlainText)
        style.set_font(head, "label_strong")
        if report.errors:
            head.setStyleSheet(f"color:{style.css('danger')};")
            head.setText(_trn(win, "safety.preflight.errors", len(report.errors)))
        else:
            head.setText(_trn(win, "safety.preflight.warnings", len(report.warnings)))
        lay.addWidget(head)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        lines = [_tr(win, "safety.preflight.line_error", "ERROR    {message}", message=i.message) for i in report.errors]
        lines += [_tr(win, "safety.preflight.line_warning", "warning  {message}", message=i.message)
                  for i in report.warnings]
        if report.extent is not None:
            x0, y0, x1, y1 = report.extent
            u = units.current()
            n = lambda v: u.number(v, "length", 1 if u.unit("length") == "mm" else 2)  # noqa: E731
            lines.append("\n" + _tr(win, "safety.preflight.extent", "job extent X {x0}..{x1}  Y {y0}..{y1} {unit}",  # i18n: ok (blank line before)
                                     x0=n(x0), x1=n(x1), y0=n(y0), y1=n(y1), unit=u.suffix("length")))
        if report.travel is not None:
            u = units.current()
            tv = report.travel
            g = lambda v: u.number(v, "length", trim=True)  # noqa: E731
            lines.append(_tr(win, "safety.preflight.travel", "travel X {x0}..{x1} {unit}, Y {y0}..{y1} {unit} ({source})",
                             x0=g(tv.x[0]), x1=g(tv.x[1]), y0=g(tv.y[0]), y1=g(tv.y[1]), unit=u.suffix("length"),
                             source=tv.source))
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
    """The state banner: the laser axis (``Laser: ARMED / DISARMED / DRY RUN``) while connected,
    the machine axis when that is what matters (``Machine: OFFLINE`` with *Connect…*,
    ``Machine: FAULT`` with *Reset*), each as an icon, the axis name and the state's word on the
    theme's state colours, with a plain description beside it (UX audit W1, W2, O2).  Its height
    never changes with the state (no layout jump)."""

    def __init__(self, ui: "SafetyUI"):
        super().__init__()
        # the style-sheet background only paints on a plain QWidget with WA_StyledBackground;
        # without it the white banner text sat on the window colour (invisible in light themes)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("safety_state_banner")
        self.ui = ui
        h = style.hbox(self)
        h.setContentsMargins(style.SP2, style.SP1, style.SP2, style.SP1)
        from .state_display import StateBadge
        self.badge = StateBadge("safety_state_badge", role="title", icon_px=style.ICON_L)
        h.addWidget(self.badge)
        self.label = QLabel()
        self.label.setObjectName("safety_state")
        style.set_font(self.label, "label_strong")
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        h.addWidget(self.label, 1)
        self.btn = QPushButton()
        self.btn.setObjectName("safety_state_action")
        self.btn.setMinimumHeight(style.TARGET)
        self.btn.clicked.connect(self._action)
        h.addWidget(self.btn)
        self.kind = ""
        self.action = ""                # what the button does: reset / disarm / arm / connect
        self._style = ""
        self._fit_height()

    def _fit_height(self) -> None:
        """One fixed height for every state: the badge (caption + word) or the button, whichever
        is taller (O2: the OFFLINE banner used to jump when its button appeared)."""
        m = self.layout().contentsMargins()
        bm = self.badge.layout().contentsMargins()
        # from the fonts, not from the text shown: the same for every state
        badge = (self.badge.caption.fontMetrics().height() + self.badge.text_label.fontMetrics().height()
                 + bm.top() + bm.bottom())
        need = max(badge, self.badge.icon_px + bm.top() + bm.bottom(), self.btn.sizeHint().height(), style.TARGET)
        self.setFixedHeight(need + m.top() + m.bottom())

    def changeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().changeEvent(ev)
        if ev.type() in (QEvent.FontChange, QEvent.StyleChange) and getattr(self, "badge", None) is not None:
            self._fit_height()

    def _action(self) -> None:
        a = self.action
        if a == "reset":
            self.ui.reset_fault()
        elif a == "disarm":
            self.ui.disarm()
        elif a == "arm":
            self.ui.arm()
        elif a == "connect":
            self.ui.open_connection()

    def refresh(self, st, ms=None) -> None:
        from . import state_display as SD
        kind = st.banner if st is not None else "OFFLINE"
        self.kind = kind
        w = self.ui.w
        connected = bool(ms is not None and getattr(ms, "connected", False)) if ms is not None else (
            st is not None and getattr(getattr(st, "state", None), "value", "offline") != "offline")
        if kind == "FAULT":
            axis, state = "machine", "fault"
            text = _tr(w, "safety.banner.fault_latched", "FAULT (latched): {fault}", fault=st.fault)
            action = "reset"
        elif not connected and not (st is not None and st.armed):
            axis, state = "machine", "offline"
            text = _tr(w, "safety.banner.offline", "Not connected")
            action = "connect"
        else:
            axis, state = "laser", SD.laser_state(st)
            key, default = {"armed": ("safety.banner.armed", "Emission enabled: the beam fires on Start"),
                            "dry_run": ("safety.banner.dry_run", "Laser output disabled (dry-run mode)")}.get(
                state, ("safety.banner.disarmed", "The laser cannot fire"))
            text = _tr(w, key, default)
            action = {"armed": "disarm", "disarmed": "arm"}.get(state, "")
        if st is not None and st.simulator and kind != "FAULT":
            text = _tr(w, "safety.banner.with_simulator", "{state}  · simulator", state=text)
        word = SD.machine_label(w, state) if axis == "machine" else SD.laser_label(w, state)
        caption = SD.axis_label(w, axis)
        self.badge.set_state(state, SD.colour_key(axis, state), word, caption)
        if self.label.text() != text:
            self.label.setText(text)
            self.label.setAccessibleName(text)
        bg, fg = COLORS.get(SD.colour_key(axis, state), COLORS["OFFLINE"])
        qss = f"QWidget#safety_state_banner{{background:{bg};}} QLabel#safety_state{{color:{fg};background:transparent;}}"
        if qss != self._style:                 # re-polishing is expensive: only on a change
            self._style = qss
            self.setStyleSheet(qss)
        self.action = action
        label = {"reset": _tr(w, "safety.banner.reset", "Reset"),
                 "disarm": _tr(w, "run.btn.disarm", "Disarm laser"),
                 "arm": _tr(w, "safety.banner.arm", "Arm laser…"),
                 "connect": _tr(w, "safety.banner.connect", "Connect…")}.get(action)
        if label:
            self.btn.setText(label)
        # hidden without changing the banner's height (fixed): no jump between states
        self.btn.setVisible(label is not None)


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
        self.setObjectName("safety_alarm_banner")
        # the theme's ALARM colours (red, white text), following a theme switch
        style.styled(self, lambda: "QWidget#safety_alarm_banner{{background:{0};}} QWidget#safety_alarm_banner QLabel"
                     "{{color:{1};background:transparent;}}".format(*COLORS["ALARM"]))
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
            self.title.setText(title)
            self.remedy.setText(_tr(w, "safety.alarm.remedy", "Remedy: {text}", text=remedy(a, tr)))
            return True
        if st is not None and st.banner == "FAULT":
            w = self.ui.w
            self.title.setText(_tr(w, "safety.alarm.fault_since", "FAULT: {fault}   since {time}", fault=st.fault,
                                   time=time.strftime('%H:%M:%S', time.localtime(st.fault_t))))
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
        # the pill shows the machine axis (Machine: OFFLINE, NOT HOMED, READY, RUNNING, HOLD,
        # ALARM, FAULT; state_display): the laser axis (ARMED, DISARMED, DRY RUN) is the banner's
        # (UI audit D-3, UX audit W2)
        from .state_display import StateBadge
        self.pill = StateBadge("run_state", role="label_strong", icon_px=style.ICON_M)
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
                # with a queue waiting: what Start runs (the drawing) and the queue's next job,
                # both named (UX audit W18); Start itself still runs the drawing
                self.start_drawing_act = menu.addAction("")
                self.start_drawing_act.setObjectName("run_start_drawing")
                self.start_drawing_act.triggered.connect(lambda _=False: ui.start())
                self.start_queue_act = menu.addAction("")
                self.start_queue_act.setObjectName("run_start_queue")
                self.start_queue_act.triggered.connect(lambda _=False: ui.run_queue())
                act = menu.addAction(_tr(w, "run.start_selection", "Start selection only"))
                act.setObjectName("run_start_selection")
                act.triggered.connect(lambda _=False: ui.start_selection())
                menu.aboutToShow.connect(self._start_menu_show)
                self._start_menu_show()
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
        # the pill keeps one width whatever the state (no jump): its widest word
        pfm = self.pill.text_label.fontMetrics()
        word = max(pfm.horizontalAdvance(t) for t in self._pill_texts() + [self.pill.caption.text()])
        m = self.pill.layout().contentsMargins()
        self.pill.setMinimumWidth(word + self.pill.icon_px + self.pill.layout().spacing() + m.left() + m.right()
                                  + fm.horizontalAdvance("M"))
        self.progress.setMinimumWidth(fm.horizontalAdvance("100 %") * 2)
        # "Elapsed 0:00:00" over "ETA 0:00:00" (two lines: the bar is 56 px tall)
        self.eta.setMinimumWidth(max(fm.horizontalAdvance(t) for t in self._times_text("0:00:00", "0:00:00").split("\n"))
                                 + fm.horizontalAdvance("M"))
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

    def _pill_texts(self) -> list[str]:
        from .state_display import MACHINE_TEXT
        return [_tr(self.ui.w, k, d) for k, d, _c in MACHINE_TEXT.values()]

    def _times_text(self, elapsed: str, eta: str) -> str:
        return _tr(self.ui.w, "run.times.bar", "Elapsed {elapsed}\nETA {eta}", elapsed=elapsed, eta=eta)

    def run_state(self, st, ms) -> str:
        """The machine axis (state_display.machine_state): offline / not_homed / ready /
        running / hold / alarm / estop / fault."""
        from .state_display import machine_state
        ctl = _ctl(self.ui.w)
        return machine_state(st, ms, list((ctl.active_alarms if ctl is not None else None) or []), ctl)

    def set_controls_shown(self, on: bool) -> None:
        """The run controls (Start .. Arm) shown, or only the state, the progress and the times:
        the Run workspace has its own large set in the same order (UX audit W4: one set of run
        controls per screen).  STOP stays on screen either way (the Run screen's)."""
        if on == (not self.buttons["stop"].isHidden()):
            return
        # the bar keeps its height (no window resize on a workspace switch: a small screen's
        # window would grow and stay grown)
        self.strip.setMinimumHeight(0 if on else self.strip.height())
        for k, b in self.buttons.items():
            (b.parentWidget() if k == "start" else b).setVisible(on)
        self.fit()

    def _start_menu_show(self) -> None:
        """The Start menu: with a queue waiting, "Start drawing: <name>" (what Start runs) and
        "Run queue: job n of N" (UX audit W18)."""
        q = self.ui.queue_waiting()
        self.start_drawing_act.setVisible(q is not None)
        self.start_queue_act.setVisible(q is not None)
        if q is not None:
            self.start_drawing_act.setText(_tr(self.ui.w, "run.start_drawing", "Start drawing: {name}",
                                               name=self.ui.start_target_name()))
            self.start_queue_act.setText(_tr(self.ui.w, "run.start_queue", "Run queue: job {n} of {total} ({name})",
                                             n=q[0], total=q[1], name=q[2]))

    def refresh(self, st, ms, running: bool, eta: str, status=None) -> None:
        from . import state_display as SD
        w = self.ui.w
        rs = self.run_state(st, ms)
        label = SD.machine_label(w, rs)
        col = SD.colour_key("machine", rs)
        armed = bool(getattr(st, "armed", False))
        name = ""
        # READY while armed shows the ARMED colour (the laser fires on Start: never the calm look)
        if rs == "ready" and armed:
            col = "ARMED"
            name = _tr(w, "run.state.ready_armed", "Machine: {state}, laser armed", state=label)
        self.pill.set_state(rs, col, label, SD.axis_label(w, "machine"), name)
        paused = ms is not None and ms.mode == "pause"
        for k, on, why in run_enabled(w, st, ms, running):
            if k == "hold" and on and paused:
                # held: Start is the one "Continue"; Hold stays Hold, disabled (UX audit W4)
                on, why = False, _tr(w, "run.why.held", "Held: Start continues the job")
            b = self.buttons[k]
            if b.isEnabled() != on or b.property("disabled_reason") != ("" if on else why):
                style.set_enabled(b, on, why)
        tr = getattr(w, "tr_", None)
        # Start continues a held job (paused, or at a breakpoint): the one "Continue" (W4)
        held = paused or getattr(_ctl(w), "breakpoint", None) is not None
        self._text(self.buttons["start"], style.run_text("start", tr, alt=held))
        self._text(self.buttons["hold"], style.run_text("hold", tr))
        self._text(self.buttons["arm"], style.run_text("arm", tr, alt=armed))
        # a job running or held (also at a breakpoint): the whole job's progress (one RunStatus)
        active = running or bool(getattr(status, "mode", ""))
        p = float(getattr(status, "progress", 0.0) or 0.0) if active else 0.0
        self.progress.setValue(int(round(p * 1000)))
        if active:
            fmt = f"{p * 100:.0f} %"
        else:
            # what Start will run (UX audit W18): "Start: gear.dxf"
            fmt = _tr(w, "run.start_target", "Start: {name}", name=self.ui.start_target_name())
            fmt = self.progress.fontMetrics().elidedText(fmt, Qt.ElideMiddle, max(self.progress.width() - style.SP3, 40))
        if self.progress.format() != fmt:
            self.progress.setFormat(fmt)
            self.progress.setAccessibleName(fmt)
        tip = _tr(w, "run.start_target", "Start: {name}", name=self.ui.start_target_name())
        b = self.buttons["start"]
        if b.property("start_target") != tip:
            b.setProperty("start_target", tip)
            b.setAccessibleDescription(tip)
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


class SafetyUI(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.dispatcher = qtbridge.QtDispatcher(window)
        self._subs: list = []
        self._ctl = None
        self._alarm_since: dict = {}
        self._job_t0 = 0.0
        from ..services.operator import JobClock
        self.clock = JobClock()             # elapsed / whole-job progress: run bar and Run screen
        self.status = None                  # the RunStatus the run bar shows (services.operator)
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
            # the run bar's controls hide on the Run workspace at once (its own set: W4)
            hooks.add("workspace_changed", lambda _ws: self.refresh(), "safety_ui")
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
        now = time.monotonic()
        if not getattr(ev, "resume", False) or not self._job_t0:
            self._job_t0 = now
        self.clock.started(bool(getattr(ev, "resume", False)), now)
        self._set_filter(True)

    def _app_state(self, state) -> None:
        """Focus loss (the application went to the background): stop a held jog (dead-man)."""
        if state != Qt.ApplicationActive:
            panel = getattr(self.w, "control", None)
            if panel is not None and hasattr(panel, "release_jogs"):
                panel.release_jogs("focus lost")

    # ------------------------------------------------------------------ shared figures
    def run_status(self, ms=None, st=None, alarms=None):
        """The RunStatus of this moment (services.operator): the Run workspace's when it is
        loaded (``hooks.provider("run_status")``: it knows the plan being run), else one built
        here on the same :class:`~mlaser.services.operator.JobClock`."""
        hooks = getattr(self.w, "hooks", None)
        fn = hooks.provider("run_status") if hooks is not None else None
        if fn is not None:
            try:
                return fn()
            except Exception:  # noqa: BLE001 - a broken plugin never hides the run bar
                log.exception("run_status provider failed")
        from ..services.operator import run_status
        ctl = self.w.ctl
        try:
            return run_status(ms if ms is not None else ctl.state(), st if st is not None else _safety(ctl),
                              list(alarms if alarms is not None else (ctl.active_alarms or [])),
                              getattr(self.w, "plan", None), 0.0, ctl.breakpoint, clock=self.clock)
        except Exception:  # noqa: BLE001
            return None

    def _current_workspace(self) -> str:
        host = getattr(getattr(self.w, "shell", None), "host", None)
        return str(getattr(host, "current", "") or "")

    def start_target_name(self) -> str:
        """What Start runs: the open drawing's name (UX audit W18)."""
        path = getattr(self.w, "path", None)
        if path:
            import os
            return os.path.basename(path)
        return _tr(self.w, "run.untitled", "(untitled)")

    def queue_waiting(self) -> Optional[tuple[int, int, str]]:
        """(n, total, name) of the queue's next job when the queue is idle with a job waiting;
        None otherwise (the Run workspace's queue, ``hooks.provider("queue")``)."""
        hooks = getattr(self.w, "hooks", None)
        if hooks is None:
            return None
        qf, rf = hooks.provider("queue"), hooks.provider("queue_runner")
        try:
            q = qf() if qf is not None else None
            r = rf() if rf is not None else None
            if q is None or (r is not None and r.state != "idle"):
                return None
            nxt = q.next_entry()
            if nxt is None:
                return None
            entries = list(q)
            return entries.index(nxt) + 1, len(entries), nxt.name
        except Exception:  # noqa: BLE001
            return None

    def run_queue(self) -> None:
        """Start's menu: run the queue (the Run workspace: load the first sheet, then confirm)."""
        hooks = getattr(self.w, "hooks", None)
        rf = hooks.provider("queue_runner") if hooks is not None else None
        r = rf() if rf is not None else None
        if r is None or r.state != "idle":
            return
        show = getattr(self.w, "show_workspace", None)
        if callable(show):
            show("run")
        r.begin()
        self.refresh()

    def open_connection(self) -> None:
        """The OFFLINE banner's *Connect…*: Machine › Connection (UX audit O2)."""
        show = getattr(self.w, "show_workspace", None)
        if callable(show):
            show("machine")
        ext = self.w.extension("ext_machine") if hasattr(self.w, "extension") else None
        ws = getattr(ext, "workspace", None)
        if ws is not None:
            ws.show_section("connection")

    def open_safety_io(self) -> None:
        """The interlock chip: the Safety I/O settings (UX audit W15)."""
        from .safety_io import open_dialog
        open_dialog(self.w)

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
        self.banner.refresh(st, ms)
        alarms = list(ctl.active_alarms or [])
        now = time.time()
        codes = {getattr(a, "code", str(a)) for a in alarms}
        for c in codes:
            self._alarm_since.setdefault(c, now)
        for c in list(self._alarm_since):
            if c not in codes:
                del self._alarm_since[c]
        show = self.alarm.refresh(alarms, st, self._alarm_since)
        # the Run screen shows the alarm once, in its state block with Reset alarm (UX audit
        # W11): the banner would repeat it there
        on_run = self._current_workspace() == "run" and self.w.extension("run") is not None \
            if hasattr(self.w, "extension") else False
        visible = show and not on_run
        if visible != self.alarm_bar.isVisible():
            self.alarm_bar.setVisible(visible)
        running = job_running(self.w)
        self._announce(st, alarms, show, running)
        self._set_filter(running)
        keep_awake(running)                 # no idle system sleep while a job runs (macOS, Windows)
        # one RunStatus for the run bar and the Run screen (UX audit W10): the Run workspace's
        # (it knows the plan being run) when it is loaded, else one from the same clock
        status = self.run_status(ms, st, alarms)
        self.status = status
        eta = ""
        if status is not None and (running or status.mode):
            from ..services.operator import fmt_duration
            eta = self.run_bar._times_text(fmt_duration(status.elapsed_s), fmt_duration(status.eta_s))
        self.run_bar.set_controls_shown(self._current_workspace() != "run")
        self.run_bar.refresh(st, ms, running, eta, status)
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
        if emits and st is not None and not st.armed and self._dialogs():
            if not ask(self.w, "start_disarmed", _tr(self.w, "safety.ask.start_disarmed",
                                                     "The machine is not armed: the laser cannot fire.\n\n"
                                                     "Arm it now (laser emission enabled) and start?"),
                       _tr(self.w, "safety.ask.start_title", "Start")):
                return
            if not self.arm(confirm=False):
                return
        if not self.preflight_ok("start" if emits else "dry_run", doc, emits, selected_only=bool(selected_only)):
            return
        self._shown = None
        self._runner().start(doc, selected_only=bool(selected_only))
        self._after(_tr(self.w, "safety.refused.start", "Start refused"))

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
            if not ask(self.w, "arm", _tr(self.w, "safety.ask.arm", "ARM the machine?\n\nLaser emission will be "
                                          "enabled: the beam fires on Start, Laser and laser tests. Make sure the "
                                          "work area is clear and the enclosure is closed."),
                       _tr(self.w, "safety.ask.arm_title", "Arm laser"), default_yes=False):
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
        ctl = self.w.ctl
        st = _safety(ctl)
        if st is not None and st.banner != "FAULT" and ctl.active_alarms:
            ctl.clear_alarms()
            self.refresh()
            return
        def done(ok) -> None:
            if not ok and self._dialogs():
                QMessageBox.warning(self.w, _tr(self.w, "safety.refused.reset", "Reset refused"),
                                    self._last_reason() or _tr(self.w, "safety.refused.reset_text", "reset refused"))
            self.refresh()
        # the reset reads the card: queued on the command worker (issue #5), answered here
        qtbridge.when_done(ctl.reset_fault(), done)

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
