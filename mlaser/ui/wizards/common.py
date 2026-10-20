"""Widgets shared by the wizards and the Machine workspace: translation helper, hold-to-jog pad
(dead-man), safety notes, the measured-sheet view and the GUI-thread bridge."""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QToolButton, QWidget)

from mlaser.services import units as U

from .. import icons
from ..units_qt import on_units_changed

AMBER_BG, AMBER_FG = "#ffd43b", "#212529"     # readable in light and dark themes
MIN_TARGET = 44                                # px, touch target (56 for the run bar)


def tr_of(window: Any):
    return getattr(window, "tr_", None)


def unit_values() -> dict:
    """Placeholders for wizard texts that name a fixed length (the commissioning jog step, the
    edge-seek minimum point distance) and the length unit, in the display units."""
    from mlaser.services.procedures.commissioning import JOG_STEP_MM
    u = U.current()
    return {"jog_step": u.fmt(JOG_STEP_MM, "length", trim=True), "min_gap": u.fmt(1.0, "length", trim=True),
            "unit": u.suffix("length")}


#: jog step choices (mm) per display length unit: metric 0.1 / 1 / 10 mm, imperial
#: 0.005 / 0.05 / 0.5 in (0.127 / 1.27 / 12.7 mm: the same orders of size, round in inches)
JOG_STEPS_MM = {"mm": (0.1, 1.0, 10.0), "in": (0.005 * 25.4, 0.05 * 25.4, 0.5 * 25.4)}


def jog_steps_mm() -> tuple[float, ...]:
    u = U.current().unit("length")
    return JOG_STEPS_MM["in" if u in ("in", "ft") else "mm"]


def T(window: Any, key: str, **values: Any) -> str:
    """Catalogue text for ``key`` with named ``values`` filled (the key itself if unknown)."""
    tr = tr_of(window)
    if tr is None:
        from mlaser.ui.i18n import translator
        tr = translator()
    if values:
        return tr.format(key, **values)
    return tr.tr(key)


class Bridge(QObject):
    """Run callables on the GUI thread (queued)."""
    call = Signal(object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.call.connect(lambda fn: fn(), Qt.QueuedConnection)

    def post(self, fn: Callable[[], None]) -> None:
        self.call.emit(fn)


class SafetyNotes(QFrame):
    """Amber box with the safety notes of a step (icon + text, never colour alone)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("safetyNotes")
        self.setStyleSheet(f"#safetyNotes {{ background: {AMBER_BG}; border-radius: 6px; }}"
                           f"#safetyNotes QLabel {{ color: {AMBER_FG}; }}")
        lay = QHBoxLayout(self)
        ic = QLabel()
        ic.setPixmap(icons.icon("alert-triangle", AMBER_FG).pixmap(28, 28))
        ic.setAlignment(Qt.AlignTop)
        lay.addWidget(ic)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        lay.addWidget(self.text, 1)

    def set_notes(self, window: Any, keys: Iterable[str]) -> None:
        keys = list(keys)
        self.setVisible(bool(keys))
        self.text.setText("<b>" + T(window, "wizard.safety.heading") + "</b><ul style='margin:0'>"
                          + "".join(f"<li>{T(window, k)}</li>" for k in keys) + "</ul>")


class JogPad(QWidget):
    """Hold-to-jog buttons X± Y± (and Z± optionally) with the dead-man: a keep-alive every
    100 ms while held; release, focus loss, hiding and Stop stop the jog at once
    (docs/SAFETY.md §8).  ``step`` mode sends one step move per click instead."""

    KEEPALIVE_MS = 100
    log = Signal(str)

    #: the persisted jog values every jog pad shows (WORK_ITEMS R-3): continuous or step, the
    #: step length (mm), fast
    STEP_KEY, LENGTH_KEY, FAST_KEY = "MC.IsStepMove", "MC.StepLength", "MC.IsFastMode"

    def __init__(self, window: Any, controller: Callable[[], Any], z: bool = True,
                 parent: Optional[QWidget] = None, home: bool = False) -> None:
        """``home``: this pad is the jog values' home (the Machine workspace's); the others are
        quick switches on the same keys."""
        super().__init__(parent)
        self.w = window
        self.ctl = controller
        self._held: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(self.KEEPALIVE_MS)
        self._timer.timeout.connect(self._keepalive)
        g = QGridLayout(self)
        g.setSpacing(6)
        self.buttons: dict[str, QToolButton] = {}
        spec = [("Y", 1, 0, 1, "arrow-up", "ws.machine.jog_y_plus"),
                ("X", -1, 1, 0, "arrow-left", "ws.machine.jog_x_minus"),
                ("X", 1, 1, 2, "arrow-right", "ws.machine.jog_x_plus"),
                ("Y", -1, 2, 1, "arrow-down", "ws.machine.jog_y_minus")]
        if z:
            spec += [("Z", 1, 0, 3, "arrow-bar-to-up", "ws.machine.jog_z_plus"),
                     ("Z", -1, 2, 3, "arrow-bar-to-down", "ws.machine.jog_z_minus")]
        for axis, d, r, c, ic, key in spec:
            b = QToolButton()
            b.setIcon(icons.icon(ic))
            b.setIconSize(b.iconSize() * 1.6)
            b.setMinimumSize(MIN_TARGET + 12, MIN_TARGET + 12)
            b.setToolTip(T(window, key))
            b.setAccessibleName(T(window, key))
            b.setText(f"{axis}{'+' if d > 0 else '-'}")
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setFocusPolicy(Qt.StrongFocus)
            b.pressed.connect(lambda a=axis, dd=d: self._press(a, dd))
            b.released.connect(lambda a=axis: self._release(a))
            g.addWidget(b, r, c)
            self.buttons[f"{axis}{'+' if d > 0 else '-'}"] = b
        self.hint = QLabel(T(window, "ws.machine.jog_hint"))
        self.hint.setWordWrap(True)
        g.addWidget(self.hint, 1, 1)
        row = QHBoxLayout()
        self.steps = QButtonGroup(self)
        self.step_buttons: list[QPushButton] = []
        for i in range(4):
            b = QPushButton(T(window, "ws.machine.step_cont") if i == 0 else "")
            b.setCheckable(True)
            b.setMinimumHeight(MIN_TARGET)
            b.setProperty("step_mm", 0.0)
            self.steps.addButton(b, i)
            self.step_buttons.append(b)
            row.addWidget(b)
        self.step_buttons[0].setChecked(True)
        # the step buttons carry the step in mm (what the card moves); labels and the choices
        # follow the display units (a switch keeps the selected position in the row)
        on_units_changed(self, self._step_labels)
        self.fast = QCheckBox(T(window, "ws.machine.jog_fast"))
        row.addWidget(self.fast)
        g.addLayout(row, 3, 0, 1, 4)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._app_state)
        # one jog step / fast for every pad, persisted (WORK_ITEMS R-3)
        from .. import homes
        role = homes.EDITOR if home else homes.SHORTCUT
        for b in self.step_buttons:
            b.setProperty(homes.PROP_VALUE, f"cfg:{self.LENGTH_KEY}")
            b.setProperty(homes.PROP_ROLE, role)
        self.fast.setProperty(homes.PROP_VALUE, f"cfg:{self.FAST_KEY}")
        self.fast.setProperty(homes.PROP_ROLE, role)
        self.config = getattr(window, "config", None)
        self._syncing = False
        if self.config is not None and hasattr(self.config, "subscribe"):
            self._sync()
            self.steps.buttonClicked.connect(lambda _b: self._store())
            self.fast.toggled.connect(lambda _on: self._store())
            self._unsub = self.config.subscribe(self._config_changed)
            self.destroyed.connect(lambda *_a, u=self._unsub: u())

    # ------------------------------------------------------------------ persisted values
    def _config_changed(self, key: str) -> None:
        if key in (self.STEP_KEY, self.LENGTH_KEY, self.FAST_KEY):
            try:
                self._sync()
            except RuntimeError:          # the pad is being deleted
                pass

    def _sync(self) -> None:
        cfg = self.config
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
        b = self.steps.checkedButton()
        return float(b.property("step_mm")) if b is not None else 0.0

    def _press(self, axis: str, d: int) -> None:
        ctl = self.ctl()
        if ctl is None:
            return
        step = self.step_mm()
        if step > 0:
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

    def release_all(self, reason: str = "") -> None:
        if not self._held:
            return
        held, self._held = set(self._held), set()
        self._timer.stop()
        ctl = self.ctl()
        for a in held:
            if ctl is not None:
                ctl.jog_stop(a)
        self.log.emit(f"jog stopped ({reason})")

    def _app_state(self, state) -> None:
        if state != Qt.ApplicationActive:
            self.release_all("focus lost")

    def hideEvent(self, ev) -> None:
        self.release_all("hidden")
        super().hideEvent(ev)

    def focusOutEvent(self, ev) -> None:
        self.release_all("focus lost")
        super().focusOutEvent(ev)


class OutlineView(QWidget):
    """The measured sheet: outline polylines, measured points, corner / origin markers and the
    angle, scaled to fit (machine coordinates, Y up).  Palette colours (dark theme safe)."""

    def __init__(self, window: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.w = window
        self.data: dict = {}
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName(T(window, "wizard.edge.view_name"))
        on_units_changed(self, lambda _u: self.update(), now=False)

    def set_data(self, data: dict) -> None:
        self.data = data or {}
        self.update()

    def _bounds(self) -> Optional[QRectF]:
        pts = [p for pl in self.data.get("outline", []) for p in pl] + list(self.data.get("points", []))
        for k in ("corner", "origin", "centre"):
            if self.data.get(k):
                pts.append(self.data[k])
        if not pts:
            return None
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        r = QRectF(min(xs), min(ys), max(1e-6, max(xs) - min(xs)), max(1e-6, max(ys) - min(ys)))
        m = 0.08 * max(r.width(), r.height(), 1.0)
        return r.adjusted(-m, -m, m, m)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.fillRect(self.rect(), pal.color(QPalette.Base))
        fg, hi = pal.color(QPalette.Text), pal.color(QPalette.Highlight)
        b = self._bounds()
        if b is None:
            p.setPen(fg)
            p.drawText(self.rect(), Qt.AlignCenter, T(self.w, "wizard.edge.view_empty"))
            return
        W, H = self.width() - 20, self.height() - 40
        s = min(W / b.width(), H / b.height())
        ox = 10 + (W - s * b.width()) / 2
        oy = 30 + (H - s * b.height()) / 2

        def m(pt) -> QPointF:
            return QPointF(ox + (pt[0] - b.left()) * s, oy + (b.bottom() - pt[1]) * s)
        pen = QPen(hi, 2)
        p.setPen(pen)
        for pl in self.data.get("outline", []):
            if len(pl) < 2:
                continue
            path = QPainterPath(m(pl[0]))
            for q in pl[1:]:
                path.lineTo(m(q))
            p.drawPath(path)
        p.setPen(QPen(fg, 1.5))
        for i, q in enumerate(self.data.get("points", [])):
            c = m(q)
            p.drawLine(QPointF(c.x() - 6, c.y()), QPointF(c.x() + 6, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - 6), QPointF(c.x(), c.y() + 6))
            p.drawText(QPointF(c.x() + 6, c.y() - 6), str(i + 1))
        for k, color in (("corner", QColor(icons.RED)), ("origin", QColor(icons.GREEN)),
                         ("centre", QColor(icons.RED))):
            q = self.data.get(k)
            if q:
                c = m(q)
                p.setPen(QPen(color, 2))
                p.drawEllipse(c, 6, 6)
        p.setPen(fg)
        ang = self.data.get("angle_deg")
        txt = []
        if ang is not None:
            txt.append(T(self.w, "wizard.edge.angle_value", angle=f"{ang:.3f}"))
        u = U.current()
        if self.data.get("corner"):
            cx, cy = self.data["corner"]
            txt.append(T(self.w, "wizard.edge.corner_value", x=u.number(cx, "length"), y=u.number(cy, "length"),
                         unit=u.suffix("length")))
        if self.data.get("radius"):
            txt.append(T(self.w, "wizard.edge.radius_value", r=u.fmt(self.data["radius"], "length")))
        p.drawText(QRectF(8, 4, self.width() - 16, 24), Qt.AlignLeft | Qt.AlignVCenter, "   ".join(txt))


def angle_deg(rad: float) -> float:
    return math.degrees(rad)


__all__ = ["T", "Bridge", "SafetyNotes", "JogPad", "OutlineView", "MIN_TARGET", "unit_values", "jog_steps_mm"]
