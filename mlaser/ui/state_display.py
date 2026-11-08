"""One state vocabulary on two named axes (UX audit W1, W2, O7), shown the same way everywhere.

* **Machine**: Offline / Not homed / Ready / Running / Hold / Alarm / E-stop / Fault: the run
  bar's pill, the Run screen's big state block, the status bar;
* **Laser**: Disarmed / Armed / Dry run: the state banner.

:func:`machine_state` and :func:`laser_state` derive them from the controller's snapshots
(``SafetyStatus``, ``MachineState``, the one alarm source ``ctl.active_alarms``) with the
safety UI's rules (:func:`mlaser.ui.safety_ui.run_state`; NOT HOMED from
:func:`mlaser.services.readiness.homing_needed`).

:class:`StateBadge` draws a state: an SVG icon (:data:`mlaser.ui.icons.STATE_ICONS`, never a
font glyph), the axis name as a caption, the state's word, on the theme's state colours
(:data:`mlaser.ui.shell.theme.STATE_TOKENS`: neutral normal states, amber ARMED, red ALARM,
dark red FAULT).  Text and icon always carry the meaning, never colour alone.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout

from . import style
from .icons import STATE_ICONS, icon

#: the machine axis, in order of precedence when several apply
MACHINE_STATES = ("fault", "estop", "alarm", "offline", "hold", "running", "not_homed", "ready")
#: the laser axis
LASER_STATES = ("disarmed", "armed", "dry_run")

#: state -> (catalogue id, English default, colour key of the theme's state tokens)
MACHINE_TEXT = {
    "offline": ("run.state.offline", "OFFLINE", "OFFLINE"),
    "not_homed": ("run.state.not_homed", "NOT HOMED", "NOT-HOMED"),
    "ready": ("run.state.ready", "READY", "READY"),
    "running": ("run.state.running", "RUNNING", "RUNNING"),
    "hold": ("run.state.hold", "HOLD", "HOLD"),
    "alarm": ("run.state.alarm", "ALARM", "ALARM"),
    "estop": ("run.state.estop", "E-STOP", "E-STOP"),
    "fault": ("run.state.fault", "FAULT", "FAULT"),
}
LASER_TEXT = {
    "disarmed": ("state.laser.disarmed", "DISARMED", "DISARMED"),
    "armed": ("state.laser.armed", "ARMED", "ARMED"),
    "dry_run": ("state.laser.dry_run", "DRY RUN", "DRY-RUN"),
}
AXIS_TEXT = {"machine": ("state.axis.machine", "Machine"), "laser": ("state.axis.laser", "Laser")}


def _tr(window: Any, key: str, default: str, **kw: Any) -> str:
    from .safety_ui import _tr as tr
    return tr(window, key, default, **kw)


def machine_state(st: Any, ms: Any, alarms: Any = (), ctl: Any = None, need_home: Optional[bool] = None) -> str:
    """The machine axis: offline / not_homed / ready / running / hold / alarm / estop / fault.
    The safety UI's :func:`mlaser.ui.safety_ui.run_state` (fault > estop > alarm > offline >
    hold > running > not_homed > ready, from the one alarm source ``ctl.active_alarms``), plus
    HOLD while a job waits at a breakpoint.  ``need_home``: None asks
    :func:`mlaser.services.readiness.homing_needed`."""
    from .safety_ui import run_state
    if need_home is None:
        from mlaser.services.readiness import homing_needed
        need_home = homing_needed(ctl) if ctl is not None else False
    s = run_state(st, ms, alarms, bool(need_home))
    if s in ("ready", "not_homed") and ctl is not None and ctl.breakpoint is not None:
        return "hold"
    return s


def laser_state(st: Any) -> str:
    """The laser axis: disarmed / armed / dry_run."""
    if st is not None and getattr(st, "dry_run", False):
        return "dry_run"
    if st is not None and getattr(st, "armed", False):
        return "armed"
    return "disarmed"


def machine_label(window: Any, state: str) -> str:
    key, default, _c = MACHINE_TEXT[state]
    return _tr(window, key, default)


def laser_label(window: Any, state: str) -> str:
    key, default, _c = LASER_TEXT[state]
    return _tr(window, key, default)


def axis_label(window: Any, axis: str) -> str:
    key, default = AXIS_TEXT[axis]
    return _tr(window, key, default)


def colour_key(axis: str, state: str) -> str:
    return (MACHINE_TEXT if axis == "machine" else LASER_TEXT)[state][2]


class StateBadge(QFrame):
    """A state on its colours: [icon] caption / WORD.  ``caption`` (the axis name) is a small
    line above the word; ``role`` sets the word's type-scale role."""

    def __init__(self, name: str, role: str = "label_strong", icon_px: int = style.ICON_M,
                 caption: bool = True, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.icon_px = icon_px
        h = style.hbox(self, sp=style.SP2)
        h.setContentsMargins(style.SP2, style.SP1, style.SP2, style.SP1)
        self.icon = QLabel()
        self.icon.setObjectName(name + "_icon")
        self.icon.setFixedSize(QSize(icon_px, icon_px))
        h.addWidget(self.icon, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.caption = QLabel()
        self.caption.setObjectName(name + "_caption")
        style.set_font(self.caption, "caption")
        self.caption.setVisible(caption)
        self.text_label = QLabel()
        self.text_label.setObjectName(name + "_text")
        style.set_font(self.text_label, role)
        col.addWidget(self.caption)
        col.addWidget(self.text_label)
        h.addLayout(col, 1)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.kind = ""            # the colour key shown (READY, ARMED ...)
        self.state = ""           # the state shown (ready, armed ...)
        self._look: tuple = ()

    def text(self) -> str:
        """The state's word (compatibility with the former label)."""
        return self.text_label.text()

    def set_state(self, state: str, colour: str, word: str, caption: str = "", accessible: str = "") -> None:
        if self.text_label.text() != word:
            self.text_label.setText(word)
        if caption and self.caption.text() != caption:
            self.caption.setText(caption)
        name = accessible or (_tr(self.window(), "state.axis.value", "{axis}: {state}", axis=caption, state=word)
                              if caption else word)
        if self.accessibleName() != name:
            self.setAccessibleName(name)
            self.text_label.setAccessibleName(name)
        self.state = state
        bg, fg = style.state_colors(colour)
        look = (state, colour, bg, fg)
        if look == self._look:
            return
        self._look = look
        self.kind = colour
        n = self.objectName()
        self.setStyleSheet(f"QFrame#{n}{{background:{bg};border-radius:{style.SP1}px;}}"
                           f"QFrame#{n} QLabel{{color:{fg};background:transparent;}}")
        self.icon.setPixmap(icon(STATE_ICONS.get(state, ""), fg).pixmap(QSize(self.icon_px, self.icon_px)))

    def colours(self) -> tuple[str, str]:
        """(fill, text) shown now (tests)."""
        return (self._look[2], self._look[3]) if self._look else ("", "")


__all__ = ["MACHINE_STATES", "LASER_STATES", "MACHINE_TEXT", "LASER_TEXT", "machine_state", "laser_state",
           "machine_label", "laser_label", "axis_label", "colour_key", "StateBadge"]
