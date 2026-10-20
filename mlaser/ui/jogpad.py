"""The one jog pad (DESIGN.md §7, UI audit I-1): the Run Control dock, the Machine workspace and
the wizards all use :class:`JogPad`, with one look and one rule.

* The original's shape is kept: Y+ at the top, X− left, X+ right, Y− at the bottom, Z+ / Z− in a
  column on the right.  Every cell is the same touch-sized square, an icon with its axis
  ("Y+") under it; the meaning is in the tooltip and the accessible name.
* The centre cell is **Go to origin** (move to the program zero) where the page has no other
  Go-to-origin control (the Run Control dock); elsewhere it stays empty.  Homing (the reference
  switches) is never in the cross: it is the **Home** row below the pad, so the two cannot be
  confused (DESIGN.md §6 glossary).
* The step choice is one segmented row, Continuous · 0.1 · 1 · 10 mm (0.005 · 0.05 · 0.5 in in
  imperial), and Fast next to it.  One step and one Fast for every pad, persisted
  (``MC.IsStepMove``, ``MC.StepLength``, ``MC.IsFastMode``; WORK_ITEMS R-3): the Machine
  workspace's pad is their home, the others are quick switches on the same keys.
* Hold-to-jog with the dead-man (docs/SAFETY.md §8): a keep-alive every 100 ms while a button is
  held; release, focus loss, hiding the pad and Stop stop the jog at once.  With a step size, a
  click moves one step.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QGridLayout, QPushButton, QSizePolicy,
                               QToolButton, QWidget)

from mlaser.services import units as U

from . import icons, style
from .units_qt import on_units_changed

#: jog step choices (mm) per display length unit: metric 0.1 / 1 / 10 mm, imperial
#: 0.005 / 0.05 / 0.5 in (0.127 / 1.27 / 12.7 mm: the same orders of size, round in inches)
JOG_STEPS_MM = {"mm": (0.1, 1.0, 10.0), "in": (0.005 * 25.4, 0.05 * 25.4, 0.5 * 25.4)}
#: the side of every pad cell (px): a touch target with room for the icon and the axis label
CELL = style.TARGET_TOUCH + style.SP2


def jog_steps_mm() -> tuple[float, ...]:
    u = U.current().unit("length")
    return JOG_STEPS_MM["in" if u in ("in", "ft") else "mm"]


def _text(tr: Any, key: str, default: str = "") -> str:
    if tr is None:
        from .i18n import translator
        tr = translator()
    try:
        if hasattr(tr, "has") and not tr.has(key):
            return default or key
        return tr.tr(key)
    except Exception:  # noqa: BLE001
        return default or key


class JogPad(QWidget):
    """Hold-to-jog buttons X± Y± (and Z± optionally) with the dead-man, the step choice and
    Fast; optionally Go to origin in the centre, a hint under the pad and a Home row.

    ``controller()`` returns the machine API (``jog_start / jog_keepalive / jog_stop /
    jog_step``) or None.  ``window`` gives the translator (``tr_``) and the ConfigService
    (``config``); :meth:`bind_config` binds a config later."""

    KEEPALIVE_MS = 100
    log = Signal(str)

    #: the persisted jog values every jog pad shows (WORK_ITEMS R-3): continuous or step, the
    #: step length (mm), fast
    STEP_KEY, LENGTH_KEY, FAST_KEY = "MC.IsStepMove", "MC.StepLength", "MC.IsFastMode"

    #: (axis, direction, row, column, icon, tooltip id)
    CROSS = (("Y", 1, 0, 1, "arrow-up", "ws.machine.jog_y_plus"),
             ("X", -1, 1, 0, "arrow-left", "ws.machine.jog_x_minus"),
             ("X", 1, 1, 2, "arrow-right", "ws.machine.jog_x_plus"),
             ("Y", -1, 2, 1, "arrow-down", "ws.machine.jog_y_minus"))
    ZCOL = (("Z", 1, 0, 4, "arrow-up", "ws.machine.jog_z_plus"),
            ("Z", -1, 2, 4, "arrow-down", "ws.machine.jog_z_minus"))

    def __init__(self, window: Any, controller: Callable[[], Any], z: bool = True,
                 parent: Optional[QWidget] = None, home: bool = False, *,
                 centre: Optional[Callable[[], None]] = None, hint: bool = False,
                 home_actions: tuple = ()) -> None:
        """``home``: this pad is the jog values' home (the Machine workspace's); the others are
        quick switches on the same keys.  ``centre``: Go to origin in the centre cell.
        ``hint``: the hold-to-move hint under the pad.  ``home_actions``: (object name, text id,
        callable) of the Home row under the pad."""
        super().__init__(parent)
        self.w = window
        self.tr_ = getattr(window, "tr_", None)
        self.ctl = controller
        self._home_role = home
        self._held: set[str] = set()
        self.config: Any = None
        self._syncing = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.KEEPALIVE_MS)
        self._timer.timeout.connect(self._keepalive)
        self.setObjectName("jog_pad")
        v = style.vbox(self)
        cross = QWidget()
        cross.setObjectName("jog_cross")
        g = QGridLayout(cross)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(style.SP1)
        self.grid = g
        self.buttons: dict[str, QToolButton] = {}
        for axis, d, r, c, ic, key in self.CROSS + (self.ZCOL if z else ()):
            name = f"{axis}{'+' if d > 0 else '-'}"
            b = self._cell(f"jog_{name}", ic, name.replace("-", "−"), _text(self.tr_, key, f"Jog {name}"))
            b.pressed.connect(lambda a=axis, dd=d: self._press(a, dd))
            b.released.connect(lambda a=axis: self._release(a))
            g.addWidget(b, r, c)
            self.buttons[name] = b
        self.centre: Optional[QToolButton] = None
        if centre is not None:
            label = _text(self.tr_, "jog.centre", "Origin")
            self.centre = self._cell("jog_go_origin", "current-location", label,
                                     _text(self.tr_, "jog.centre_tip", "Go to origin: move to the program zero"))
            self.centre.clicked.connect(lambda _=False: centre())
            g.addWidget(self.centre, 1, 1)
        if z:
            g.setColumnMinimumWidth(3, style.SP2)        # the Z column apart from the cross
        row = style.hbox()
        row.addStretch(1)
        row.addWidget(cross)
        row.addStretch(1)
        v.addLayout(row)
        self.hint = None
        if hint:
            self.hint = style.caption(_text(self.tr_, "ws.machine.jog_hint"))
            self.hint.setAlignment(Qt.AlignHCenter)
            v.addWidget(self.hint)
        # the step choice: one segmented row (equal widths) and Fast
        self.steps = QButtonGroup(self)
        self.step_buttons: list[QPushButton] = []
        steps_row: list[QWidget] = []
        for i in range(4):
            b = QPushButton(_text(self.tr_, "ws.machine.step_cont", "Continuous") if i == 0 else "")
            b.setObjectName("jog_step_cont" if i == 0 else f"jog_step_{i}")
            b.setCheckable(True)
            b.setMinimumHeight(style.TARGET_TOUCH)
            b.setProperty("step_mm", 0.0)
            self.steps.addButton(b, i)
            self.step_buttons.append(b)
            steps_row.append(b)
        self.step_buttons[0].setChecked(True)
        self.steps.setExclusive(True)
        on_units_changed(self, self._step_labels)
        self.fast = QCheckBox(_text(self.tr_, "ws.machine.jog_fast", "Fast"))
        self.fast.setObjectName("jog_fast")
        from .responsive import flow
        self.step_row = flow(steps_row, uniform=True)
        self.step_row.setObjectName("jog_steps")
        v.addWidget(self.step_row)
        v.addWidget(self.fast)
        # the Home row (reference switches): text buttons of equal width
        self.home_buttons: dict[str, QPushButton] = {}
        if home_actions:
            hb = []
            for obj, key, fn in home_actions:
                b = QPushButton(_text(self.tr_, key))
                b.setObjectName(obj)
                b.setMinimumHeight(style.TARGET_TOUCH)
                b.clicked.connect(lambda _=False, f=fn: f())
                self.home_buttons[obj] = b
                hb.append(b)
            self.home_row = flow(hb, uniform=True)
            self.home_row.setObjectName("jog_home_row")
            v.addWidget(self.home_row)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._app_state)
        from . import homes
        role = homes.EDITOR if home else homes.SHORTCUT
        for b in self.step_buttons:
            b.setProperty(homes.PROP_VALUE, f"cfg:{self.LENGTH_KEY}")
            b.setProperty(homes.PROP_ROLE, role)
        self.fast.setProperty(homes.PROP_VALUE, f"cfg:{self.FAST_KEY}")
        self.fast.setProperty(homes.PROP_ROLE, role)
        self.steps.buttonClicked.connect(lambda _b: self._store())
        self.fast.toggled.connect(lambda _on: self._store())
        cfg = getattr(window, "config", None)
        if cfg is not None and hasattr(cfg, "subscribe"):
            self.bind_config(cfg)

    def _cell(self, name: str, ic: str, label: str, tip: str) -> QToolButton:
        """A pad cell: an icon with a short label under it, the same square for every cell."""
        b = QToolButton()
        b.setObjectName(name)
        b.setIcon(icons.icon(ic))
        b.setIconSize(QSize(style.ICON_M, style.ICON_M))
        b.setText(label)
        b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.setFocusPolicy(Qt.StrongFocus)
        b.setAutoRepeat(False)
        side = max(CELL, b.sizeHint().width(), b.sizeHint().height())
        b.setMinimumSize(side, side)
        b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return b

    # ------------------------------------------------------------------ persisted values
    def bind_config(self, config: Any) -> None:
        if self.config is config:
            return
        self.config = config
        self._sync()
        unsub = config.subscribe(self._config_changed)
        if callable(unsub):
            self.destroyed.connect(lambda *_a, u=unsub: u())

    def _config_changed(self, key: str) -> None:
        if key in (self.STEP_KEY, self.LENGTH_KEY, self.FAST_KEY):
            try:
                self._sync()
            except RuntimeError:          # the pad is being deleted
                pass

    def _sync(self) -> None:
        cfg = self.config
        if cfg is None:
            return
        self._syncing = True
        try:
            stepping = bool(cfg.get(self.STEP_KEY, False))
            mm = float(cfg.get(self.LENGTH_KEY, 1.0) or 1.0)
            if not stepping:
                self.step_buttons[0].setChecked(True)
            else:
                steps = [b for b in self.step_buttons if float(b.property("step_mm")) > 0]
                best = min(steps, key=lambda b: abs(float(b.property("step_mm")) - mm))
                best.setChecked(True)
            self.fast.setChecked(bool(cfg.get(self.FAST_KEY, False)))
        finally:
            self._syncing = False

    def _store(self) -> None:
        if self._syncing or self.config is None:
            return
        step = self.step_mm()
        try:
            if self.config.get(self.STEP_KEY, False) != (step > 0):
                self.config.set(self.STEP_KEY, step > 0)
            if step > 0 and self.config.get(self.LENGTH_KEY) != step:
                self.config.set(self.LENGTH_KEY, step)
            if self.config.get(self.FAST_KEY, False) != self.fast.isChecked():
                self.config.set(self.FAST_KEY, self.fast.isChecked())
        except ValueError:
            pass

    def _step_labels(self, u: Any) -> None:
        for b, mm in zip(self.step_buttons[1:], jog_steps_mm()):
            b.setProperty("step_mm", mm)
            b.setText(u.fmt(mm, "length", trim=True))

    # ------------------------------------------------------------------ jog
    def step_mm(self) -> float:
        """The chosen step (mm), 0 for continuous."""
        b = self.steps.checkedButton()
        return float(b.property("step_mm")) if b is not None else 0.0

    def stepping(self) -> bool:
        return self.step_mm() > 0

    def press(self, axis: str, d: int) -> None:
        """A jog button pressed (tests and the pendant call it too)."""
        self._press(axis, d)

    def _press(self, axis: str, d: int) -> None:
        ctl = self.ctl()
        if ctl is None:
            return
        step = self.step_mm()
        if step > 0:
            # the step jog uses the fast / slow speed too (FUN_005bdae0)
            ctl.jog_step(axis, d, step, fast=self.fast.isChecked())
            return
        self._held.add(axis)
        ctl.jog_start(axis, d, fast=self.fast.isChecked())
        self._timer.start()

    def _keepalive(self) -> None:
        ctl = self.ctl()
        if not self._held or ctl is None:
            self._timer.stop()
            return
        ctl.jog_keepalive()

    def _release(self, axis: str) -> None:
        if axis not in self._held:
            return
        self._held.discard(axis)
        if not self._held:
            self._timer.stop()
        ctl = self.ctl()
        if ctl is not None:
            ctl.jog_stop(axis)

    def held(self) -> set[str]:
        return set(self._held)

    def release_all(self, reason: str = "") -> None:
        """Dead-man: stop every held jog (focus loss, Stop, the pad hidden)."""
        if not self._held:
            return
        held, self._held = set(self._held), set()
        self._timer.stop()
        ctl = self.ctl()
        for a in held:
            if ctl is not None:
                ctl.jog_stop(a)
        self.log.emit(_text(self.tr_, "jog.stopped", "Jog stopped") + (f" ({reason})" if reason else ""))

    def _app_state(self, state) -> None:
        if state != Qt.ApplicationActive:
            self.release_all("focus lost")

    def hideEvent(self, ev) -> None:  # noqa: N802 - Qt API
        self.release_all("hidden")
        super().hideEvent(ev)

    def focusOutEvent(self, ev) -> None:  # noqa: N802 - Qt API
        self.release_all("focus lost")
        super().focusOutEvent(ev)


__all__ = ["JogPad", "JOG_STEPS_MM", "jog_steps_mm", "CELL"]
