"""Preview with a scrubber (ux.md P1-4, §4.2): play the PlannedJob on the canvas.

* :class:`PreviewOverlay` draws, on the canvas scene, the head path streamed up to the scrubber
  time (cut moves solid, rapids dashed), the pierce points passed so far and the head marker
  (filled while the beam is on).  It owns its scene items; ``canvas.py`` is not changed.
* :class:`ScrubberWidget`: timeline slider (milliseconds), play / pause, speed x1 ... x50,
  previous / next contour and "go to contour", the time ``00:14 / 00:22`` and the state.
* :class:`CostCardWidget`: the time and cost card (:mod:`mlaser.services.costing`), every value
  with its unit and marked measured / estimate / missing.

Both read a :class:`mlaser.services.preview.Timeline` built from the plan the JobService
delivers (``ctx.on_plan``): the same PlannedJob Start streams (preview = cut).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPathItem, QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from mlaser.services import units
from mlaser.services.preview import CUT, DWELL, DWELL_ON, PROCESS, TRAVEL, Timeline

SPEEDS = (1, 2, 5, 10, 20, 50)
CUT_COLOR = "#ffb000"         # amber: the cut so far (readable on the dark and light canvas)
RAPID_COLOR = "#4da3ff"
HEAD_ON = "#ff3030"


def fmt_time(s: float) -> str:
    s = max(0.0, float(s))
    h, rem = divmod(int(round(s)), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class PreviewOverlay:
    """Scene items over the canvas for one timeline position (see the module docstring)."""

    Z = 25

    def __init__(self, canvas: Any) -> None:
        self.canvas = canvas
        self.timeline: Optional[Timeline] = None
        self._cut: Optional[QGraphicsPathItem] = None
        self._rapid: Optional[QGraphicsPathItem] = None
        self._pierce: Optional[QGraphicsPathItem] = None
        self._head: Optional[QGraphicsEllipseItem] = None
        self.t = 0.0
        self.visible = False

    def _scene(self):
        return self.canvas.scene()

    def _ensure(self) -> None:
        sc = self._scene()
        if self._cut is None or self._cut.scene() is not sc:
            self._cut = QGraphicsPathItem()
            pen = QPen(QColor(CUT_COLOR), 2.0)
            pen.setCosmetic(True)
            self._cut.setPen(pen)
            self._rapid = QGraphicsPathItem()
            pen = QPen(QColor(RAPID_COLOR), 1.0, Qt.DashLine)
            pen.setCosmetic(True)
            self._rapid.setPen(pen)
            self._pierce = QGraphicsPathItem()
            pen = QPen(QColor(HEAD_ON), 1.2)
            pen.setCosmetic(True)
            self._pierce.setPen(pen)
            self._head = QGraphicsEllipseItem(-6, -6, 12, 12)
            self._head.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            for it in (self._rapid, self._cut, self._pierce, self._head):
                it.setZValue(self.Z)
                sc.addItem(it)
            self._head.setZValue(self.Z + 1)

    def set_timeline(self, tl: Optional[Timeline]) -> None:
        self.timeline = tl
        self.t = 0.0
        if tl is None:
            self.hide()

    def hide(self) -> None:
        self.visible = False
        for it in (self._cut, self._rapid, self._pierce, self._head):
            if it is not None and it.scene() is not None:
                it.scene().removeItem(it)
        self._cut = self._rapid = self._pierce = self._head = None

    def set_time(self, t: float) -> None:
        tl = self.timeline
        if tl is None or not len(tl):
            return
        self._ensure()
        self.visible = True
        self.t = t
        xs, ys, las = tl.path_until(t)
        cut, rap = QPainterPath(), QPainterPath()
        for k in range(1, len(xs)):
            a, b = QPointF(xs[k - 1], ys[k - 1]), QPointF(xs[k], ys[k])
            path = cut if las[k] else rap
            if path.currentPosition() != a or path.elementCount() == 0:
                path.moveTo(a)
            path.lineTo(b)
        assert self._cut is not None and self._rapid is not None and self._pierce is not None
        self._cut.setPath(cut)
        self._rapid.setPath(rap)
        pp = QPainterPath()
        i = tl.segment_at(t)
        for j in [int(j) for j in (tl.pierce[: i + 1].nonzero()[0])][-2000:]:
            pp.addEllipse(QPointF(float(tl.x0[j]), float(tl.y0[j])), 1.0, 1.0)
        self._pierce.setPath(pp)
        st = tl.state_at(t)
        assert self._head is not None
        self._head.setPos(st.x, st.y)
        on = st.laser
        self._head.setPen(QPen(QColor(HEAD_ON if on else "#c0c0c0"), 2.0))
        self._head.setBrush(QBrush(QColor(HEAD_ON)) if on else QBrush(Qt.NoBrush))


class ScrubberWidget(QWidget):
    """Timeline control (see the module docstring).  ``timeChanged(t)`` on every move."""

    timeChanged = Signal(float)

    def __init__(self, tr: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.timeline: Optional[Timeline] = None
        self.t = 0.0
        self.setObjectName("preview_scrubber")
        t = tr.tr
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        row = QHBoxLayout()
        from mlaser.ui.icons import icon

        self._icons = (icon("player-play"), icon("player-pause"))
        self.prev_btn = QPushButton("|◀")
        self.play_btn = QPushButton()
        self.play_btn.setIcon(self._icons[0])
        self.next_btn = QPushButton("▶|")
        for b, key in ((self.prev_btn, "preview.prev_contour"), (self.play_btn, "preview.play"),
                       (self.next_btn, "preview.next_contour")):
            b.setToolTip(t(key))
            b.setAccessibleName(t(key))
            b.setMinimumSize(40, 32)
            row.addWidget(b)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setAccessibleName(t("preview.timeline"))
        self.slider.setRange(0, 0)
        self.slider.setPageStep(1000)
        self.slider.setSingleStep(100)
        row.addWidget(self.slider, 1)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f = QFont(self.time_label.font())
        f.setStyleHint(QFont.Monospace)
        f.setFamily("monospace")
        self.time_label.setFont(f)
        row.addWidget(self.time_label)
        self.speed = QComboBox()
        for s in SPEEDS:
            self.speed.addItem(f"×{s}", s)
        self.speed.setCurrentIndex(SPEEDS.index(10))
        self.speed.setToolTip(t("preview.speed"))
        self.speed.setAccessibleName(t("preview.speed"))
        row.addWidget(self.speed)
        lay.addLayout(row)
        row2 = QHBoxLayout()
        self.state_label = QLabel(t("preview.no_plan"))
        from mlaser.ui.units_qt import on_units_changed
        on_units_changed(self.state_label, lambda _u: self._update_labels(), now=False)
        self.state_label.setWordWrap(True)
        row2.addWidget(self.state_label, 1)
        lab = QLabel(t("preview.goto_contour"))
        self.contour = QSpinBox()
        self.contour.setRange(0, 0)
        self.contour.setAccessibleName(t("preview.goto_contour"))
        self.contour.setKeyboardTracking(False)
        lab.setBuddy(self.contour)
        row2.addWidget(lab)
        row2.addWidget(self.contour)
        self.clear_btn = QPushButton(t("preview.clear"))
        row2.addWidget(self.clear_btn)
        lay.addLayout(row2)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        self.play_btn.clicked.connect(self.toggle)
        self.prev_btn.clicked.connect(lambda: self.step_contour(-1))
        self.next_btn.clicked.connect(lambda: self.step_contour(1))
        self.slider.valueChanged.connect(lambda v: self.seek(v / 1000.0, from_slider=True))
        self.contour.valueChanged.connect(self._goto_contour)
        self.clear_btn.clicked.connect(self.clear)
        # no Space shortcut here: Space is the global Hold key of the run bar (docs/SAFETY.md)
        self.cleared = False
        self.need_timeline: Optional[Any] = None   # callable: build the timeline of a large plan
        self.set_timeline(None)

    # ---------------------------------------------------------------- data
    def set_timeline(self, tl: Optional[Timeline]) -> None:
        self.pause()
        self.timeline = tl
        ok = tl is not None and len(tl) > 0
        for w in (self.slider, self.play_btn, self.prev_btn, self.next_btn, self.contour, self.speed):
            w.setEnabled(ok)
        self.slider.blockSignals(True)
        self.slider.setRange(0, int(round((tl.total_s if ok else 0.0) * 1000)))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.contour.blockSignals(True)
        self.contour.setRange(0, len(tl.markers) if ok else 0)
        self.contour.setValue(0)
        self.contour.blockSignals(False)
        self.t = 0.0
        self._update_labels()

    # ---------------------------------------------------------------- transport
    @property
    def playing(self) -> bool:
        return self._timer.isActive()

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def play(self) -> None:
        if self.timeline is None and self.need_timeline is not None:
            self.need_timeline()              # a large plan builds its timeline on demand
        tl = self.timeline
        if tl is None or not len(tl):
            return
        if self.t >= tl.total_s:
            self.seek(0.0)
        self.cleared = False
        self._clock.start()
        self._timer.start()
        self.play_btn.setIcon(self._icons[1])
        self.play_btn.setToolTip(self.tr_.tr("preview.pause"))

    def pause(self) -> None:
        self._timer.stop()
        self.play_btn.setIcon(self._icons[0])
        self.play_btn.setToolTip(self.tr_.tr("preview.play"))

    def clear(self) -> None:
        self.pause()
        self.cleared = True
        self.seek(0.0)
        self.timeChanged.emit(-1.0)

    def _tick(self) -> None:
        tl = self.timeline
        if tl is None:
            self.pause()
            return
        dt = self._clock.restart() / 1000.0
        t = self.t + dt * float(self.speed.currentData() or 1)
        if t >= tl.total_s:
            t = tl.total_s
            self.pause()
        self.seek(t)

    def seek(self, t: float, from_slider: bool = False) -> None:
        tl = self.timeline
        if tl is None:
            return
        self.t = min(max(0.0, float(t)), tl.total_s)
        if not from_slider:
            self.slider.blockSignals(True)
            self.slider.setValue(int(round(self.t * 1000)))
            self.slider.blockSignals(False)
        else:
            self.cleared = False
        self._update_labels()
        self.timeChanged.emit(self.t)

    def step_contour(self, step: int) -> None:
        tl = self.timeline
        if tl is None or not tl.markers:
            return
        self.seek(tl.next_contour_time(self.t, step))

    def _goto_contour(self, n: int) -> None:
        tl = self.timeline
        if tl is None or n <= 0 or n > len(tl.markers):
            return
        self.seek(tl.markers[n - 1][0])

    # ---------------------------------------------------------------- labels
    def _update_labels(self) -> None:
        tl = self.timeline
        t = self.tr_.tr
        if tl is None or not len(tl):
            self.time_label.setText("00:00 / 00:00")
            self.state_label.setText(t("preview.no_plan"))
            return
        self.time_label.setText(f"{fmt_time(self.t)} / {fmt_time(tl.total_s)}")
        st = tl.state_at(self.t)
        what = {CUT: "preview.state.cut", TRAVEL: "preview.state.travel", DWELL: "preview.state.dwell",
                DWELL_ON: "preview.state.pierce", PROCESS: "preview.state.pierce" if st.laser else "preview.state.process"
                }.get(st.kind, "preview.state.idle")
        n = 0
        for k, m in enumerate(tl.markers):
            if m[0] <= self.t + 1e-9:
                n = k + 1
        self.contour.blockSignals(True)
        self.contour.setValue(n)
        self.contour.blockSignals(False)
        self.state_label.setText(self.tr_.format(
            "preview.status", state=t(what), laser=t("preview.laser_on" if st.laser else "preview.laser_off"),
            n=n, total=len(tl.markers), x=units.current().number(st.x, "length", 1),
            y=units.current().fmt(st.y, "length", decimals=1)))


class CostCardWidget(QWidget):
    """The time and cost card (see the module docstring)."""

    ORDER = ("time.total", "time.cut", "time.travel", "time.pierce", "count.pierces", "length.cut",
             "length.travel", "gas.volume", "energy.laser", "energy.machine", "energy.total", "power.peak",
             "material.mass", "cost.machine", "cost.gas", "cost.energy", "cost.material", "cost.total")

    def __init__(self, tr: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.setObjectName("cost_card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.title = QLabel(f"<b>{tr.tr('cost.title')}</b>")
        lay.addWidget(self.title)
        self.hint = QLabel(tr.tr("cost.hint"))
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)
        lay.addWidget(self.hint)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignLeft)
        lay.addLayout(self.form)
        self.rates_btn = QPushButton(tr.tr("cost.rates"))
        lay.addWidget(self.rates_btn)
        self.labels: dict[str, QLabel] = {}
        self.card = None
        for key in self.ORDER:
            lab = QLabel("—")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lab.setWordWrap(True)
            self.labels[key] = lab
            self.form.addRow(tr.tr(f"cost.line.{key}"), lab)
        from mlaser.ui.units_qt import on_units_changed
        on_units_changed(self, lambda _u: self.set_card(self.card) if self.card is not None else None, now=False)

    def set_card(self, card: Any) -> None:
        self.card = card
        t = self.tr_
        for key, lab in self.labels.items():
            ln = card.get(key) if card is not None else None
            if ln is None:
                lab.setText("—")
                continue
            note = t.format(ln.note, **dict(ln.args)) if ln.note else ""
            if ln.value is None:
                txt = f"<i>{t.tr('cost.missing')}</i>"
            else:
                txt = self._fmt(ln)
                if ln.kind == "estimate":
                    txt = "≈ " + txt
            if ln.kind == "missing":
                txt += f" <span style='color:#d08000'>⚠ {note}</span>" if note else ""
            elif note:
                txt += f" <small>({note})</small>"
            lab.setText(txt)
            lab.setToolTip(t.tr(f"cost.kind.{ln.kind}"))

    @staticmethod
    def _fmt(ln: Any) -> str:
        v, u = ln.value, ln.unit
        if u == "s":
            return f"{fmt_time(v)} ({v:.1f} s)"
        if ln.key == "count.pierces":
            return f"{int(v)}"
        if u in ("€", "$", "£") or len(u) <= 3 and ln.key.startswith("cost."):
            return f"{v:.2f} {u}"
        cur = units.current()
        if u == "m³":                      # gas: the services' m³ (= 1000 l) in the display units
            if cur.unit("volume") == "l":
                return f"{v:.3f} m³ ({v * 1000:.0f} l)"
            return cur.fmt(v * 1000.0, "volume", decimals=3)
        if u == "m":                       # cut / travel length (m in the services)
            return cur.fmt(v * 1000.0, "long_length", decimals=2 if cur.unit("long_length") == "m" else 1)
        if u == "kg":
            return cur.fmt(v, "mass")
        return f"{v:.3g} {u}" if abs(v) < 100 else f"{v:.1f} {u}"
