"""Widgets shared by the wizards and the Machine workspace: translation helper, hold-to-jog pad
(dead-man), safety notes, the measured-sheet view and the GUI-thread bridge."""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .. import icons

AMBER_BG, AMBER_FG = "#ffd43b", "#212529"     # readable in light and dark themes
MIN_TARGET = 44                                # px, touch target (56 for the run bar)


def tr_of(window: Any):
    return getattr(window, "tr_", None)


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

    def __init__(self, window: Any, controller: Callable[[], Any], z: bool = True,
                 parent: Optional[QWidget] = None) -> None:
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
        for i, (val, key) in enumerate(((0.0, "ws.machine.step_cont"), (0.1, ""), (1.0, ""), (10.0, ""))):
            b = QPushButton(T(window, key) if key else f"{val:g} mm")
            b.setCheckable(True)
            b.setMinimumHeight(MIN_TARGET)
            b.setProperty("step_mm", val)
            self.steps.addButton(b, i)
            self.step_buttons.append(b)
            row.addWidget(b)
        self.step_buttons[0].setChecked(True)
        self.fast = QCheckBox(T(window, "ws.machine.jog_fast"))
        row.addWidget(self.fast)
        g.addLayout(row, 3, 0, 1, 4)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._app_state)

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
        if self.data.get("corner"):
            cx, cy = self.data["corner"]
            txt.append(T(self.w, "wizard.edge.corner_value", x=f"{cx:.2f}", y=f"{cy:.2f}"))
        if self.data.get("radius"):
            txt.append(T(self.w, "wizard.edge.radius_value", r=f"{self.data['radius']:.2f}"))
        p.drawText(QRectF(8, 4, self.width() - 16, 24), Qt.AlignLeft | Qt.AlignVCenter, "   ".join(txt))


def angle_deg(rad: float) -> float:
    return math.degrees(rad)


__all__ = ["T", "Bridge", "SafetyNotes", "JogPad", "OutlineView", "MIN_TARGET"]
