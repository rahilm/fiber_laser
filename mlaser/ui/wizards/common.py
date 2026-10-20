"""Widgets shared by the wizards and the Machine workspace: translation helper, hold-to-jog pad
(dead-man), safety notes, the measured-sheet view and the GUI-thread bridge."""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QWidget

from mlaser.services import units as U

from .. import icons, style
from ..jogpad import JOG_STEPS_MM, JogPad, jog_steps_mm  # noqa: F401 - the one jog pad (re-exported)
from ..style import TARGET_TOUCH
from ..units_qt import on_units_changed

MIN_TARGET = TARGET_TOUCH                      # px, touch target (TARGET_RUN for the run bar)


def tr_of(window: Any):
    return getattr(window, "tr_", None)


def unit_values() -> dict:
    """Placeholders for wizard texts that name a fixed length (the commissioning jog step, the
    edge-seek minimum point distance) and the length unit, in the display units."""
    from mlaser.services.procedures.commissioning import JOG_STEP_MM
    u = U.current()
    return {"jog_step": u.fmt(JOG_STEP_MM, "length", trim=True), "min_gap": u.fmt(1.0, "length", trim=True),
            "unit": u.suffix("length")}


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
        # the warning role (readable in light and dark: style.py / theme.py)
        style.styled(self, lambda: (f"#safetyNotes {{ background: {style.css('warning_fill')}; "
                                    f"border: 1px solid {style.css('warning')}; border-radius: {style.SP1 + 2}px; }}"
                                    f"#safetyNotes QLabel {{ color: {style.css('on_warning')}; }}"))
        lay = style.hbox(self, "dense")
        ic = QLabel()
        style.styled(ic, lambda: (ic.setPixmap(icons.icon("alert-triangle", style.css("on_warning")).pixmap(
            style.ICON_L, style.ICON_L)), "")[1])
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
        for k, color in (("corner", style.color("danger")), ("origin", style.color("success")),
                         ("centre", style.color("danger"))):
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
