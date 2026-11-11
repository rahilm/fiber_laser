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

import html
import time
from typing import Any, Optional

import numpy as np

from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPathItem, QHBoxLayout, QLabel, QSlider, QSpinBox,
                               QVBoxLayout, QWidget)

from mlaser.services import units
from mlaser.ui import style
from mlaser.services.preview import CUT, DWELL, DWELL_ON, PROCESS, TRAVEL, Timeline

SPEEDS = (1, 2, 5, 10, 20, 50)


def _c(role: str) -> str:
    """A preview colour of the current theme (shell/theme.py ``c_preview_*``: the cut so far in
    amber, rapids, the head; readable on the light and the dark canvas)."""
    from mlaser.ui.canvas import CANVAS_COLORS
    from mlaser.ui.shell import theme
    return CANVAS_COLORS.get(f"preview_{role}") or theme.canvas_colors(theme.DARK)[f"preview_{role}"]


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
        self._pts: Optional[tuple] = None        # (seg, xs, ys, las) of the whole timeline, thinned
        self._chunks: list = []                  # (cut, rapid) QPainterPath per CHUNK points
        self._pierce_key: Optional[tuple] = None

    #: points per cached path chunk: a frame joins the finished chunks (C++) and builds only the
    #: open one in Python (issue #7: rebuilding the whole path, up to 60 000 points, on every
    #: 33 ms tick kept the event loop busy, so the buttons stopped answering while it played)
    CHUNK = 2000
    MAX_POINTS = 60000
    def _scene(self):
        # the canvas's live layer: a frame redraws these items over the cached drawing
        # (perf review 2026-11 finding 3); a plain view (tests) has only its scene
        live = getattr(self.canvas, "overlay_scene", None)
        return live() if callable(live) else self.canvas.scene()

    def _ensure(self) -> None:
        sc = self._scene()
        if self._cut is None or self._cut.scene() is not sc:
            self._pierce_key = None              # a new pierce item starts empty
            self._cut = QGraphicsPathItem()
            pen = QPen(QColor(_c("cut")), 2.0)
            pen.setCosmetic(True)
            self._cut.setPen(pen)
            self._rapid = QGraphicsPathItem()
            pen = QPen(QColor(_c("rapid")), 1.0, Qt.DashLine)
            pen.setCosmetic(True)
            self._rapid.setPen(pen)
            self._pierce = QGraphicsPathItem()
            pen = QPen(QColor(_c("head")), 1.2)
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
        self._pts = None
        self._chunks = []
        self._pierce_key = None
        if tl is None:
            self.hide()

    def _points(self, tl: Timeline) -> tuple:
        """The head path's move end points over the whole timeline, once: (segment index of
        each point, xs, ys, laser), thinned to about MAX_POINTS keeping every laser change (as
        Timeline.path_until), with the start point first (segment -1)."""
        if self._pts is None:
            mv = (tl.kind == TRAVEL) | (tl.kind == CUT)
            idx = np.flatnonzero(mv)
            xs = tl.x0[idx] + tl.dx[idx]
            ys = tl.y0[idx] + tl.dy[idx]
            las = tl.laser[idx].astype(bool)
            if len(xs) > self.MAX_POINTS:
                keep = np.zeros(len(xs), dtype=bool)
                keep[:: max(1, len(xs) // self.MAX_POINTS)] = True
                keep[-1] = True
                keep[1:] |= las[1:] != las[:-1]
                keep[:-1] |= las[1:] != las[:-1]
                idx, xs, ys, las = idx[keep], xs[keep], ys[keep], las[keep]
            x0, y0 = (float(tl.x0[0]), float(tl.y0[0])) if len(tl.x0) else (0.0, 0.0)
            self._pts = (np.concatenate([[-1], idx]), np.concatenate([[x0], xs]),
                         np.concatenate([[y0], ys]), np.concatenate([[False], las]))
        return self._pts

    @staticmethod
    def _append(cut: QPainterPath, rap: QPainterPath, xs, ys, las, k0: int, k1: int) -> None:
        """Moves to points k0..k1-1 (each from the point before it) into ``cut`` / ``rap``."""
        for k in range(max(k0, 1), k1):
            a, b = QPointF(float(xs[k - 1]), float(ys[k - 1])), QPointF(float(xs[k]), float(ys[k]))
            path = cut if las[k] else rap
            if path.elementCount() == 0 or path.currentPosition() != a:
                path.moveTo(a)
            path.lineTo(b)

    def _chunk(self, c: int, xs, ys, las) -> tuple:
        while len(self._chunks) <= c:
            j = len(self._chunks)
            cut, rap = QPainterPath(), QPainterPath()
            self._append(cut, rap, xs, ys, las, j * self.CHUNK, (j + 1) * self.CHUNK)
            self._chunks.append((cut, rap))
        return self._chunks[c]

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
        seg, xs, ys, las = self._points(tl)
        i = tl.segment_at(t)
        st = tl.state_at(t)
        # the points of segments before the current one are complete; the current segment's
        # move ends at the head's present position
        n = int(np.searchsorted(seg, i, side="left")) if i >= 0 else 1
        cut, rap = QPainterPath(), QPainterPath()
        full = n // self.CHUNK
        for c in range(full):                    # finished chunks: cached paths, joined in C++
            cc, rc = self._chunk(c, xs, ys, las)
            cut.addPath(cc)
            rap.addPath(rc)
        self._append(cut, rap, xs, ys, las, full * self.CHUNK, n)
        if i >= 0 and n >= 1 and int(tl.kind[i]) in (TRAVEL, CUT):
            a, b = QPointF(float(xs[n - 1]), float(ys[n - 1])), QPointF(float(st.x), float(st.y))
            path = cut if bool(tl.laser[i]) else rap
            if path.elementCount() == 0 or path.currentPosition() != a:
                path.moveTo(a)
            path.lineTo(b)
        assert self._cut is not None and self._rapid is not None and self._pierce is not None
        self._cut.setPath(cut)
        self._rapid.setPath(rap)
        pierces = tl.pierce[: i + 1].nonzero()[0] if i >= 0 else np.zeros(0, dtype=int)
        key = (len(pierces), id(tl))
        if key != self._pierce_key:              # pierces change rarely: rebuilt only then
            self._pierce_key = key
            pp = QPainterPath()
            for j in [int(j) for j in pierces][-2000:]:
                pp.addEllipse(QPointF(float(tl.x0[j]), float(tl.y0[j])), 1.0, 1.0)
            self._pierce.setPath(pp)
        assert self._head is not None
        self._head.setPos(st.x, st.y)
        on = st.laser
        self._head.setPen(QPen(QColor(_c("head") if on else _c("head_off")), 2.0))
        self._head.setBrush(QBrush(QColor(_c("head"))) if on else QBrush(Qt.NoBrush))


class _TimeLabel(QLabel):
    """The "elapsed / total" label: at least as wide as "00:00 / 00:00" in its own font (no
    fixed pixel width: a larger font or the macOS / Windows fonts made 110 px too narrow), so
    the row does not jump while the time runs."""

    TEMPLATE = "00:00 / 00:00"

    def _template_w(self) -> int:
        m = self.contentsMargins()
        return self.fontMetrics().horizontalAdvance(self.TEMPLATE) + 2 * self.margin() + m.left() + m.right() + 2

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        s = super().minimumSizeHint()
        return QSize(max(s.width(), self._template_w()), s.height())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        s = super().sizeHint()
        return QSize(max(s.width(), self._template_w()), s.height())


class ScrubberWidget(QWidget):
    """Timeline control (see the module docstring).  ``timeChanged(t)`` on every move.

    Its state line wraps; a dock does not lay out by height for width, so the widget's minimum
    height is what its layout needs at the width it has (it follows resizes and new text):
    the wrapped line got 30 of 32 px in the macOS runner's Process preview dock."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        s = super().minimumSizeHint()
        return QSize(s.width(), max(s.height(), self._hfw_min))

    def _sync_hfw(self) -> None:
        lay = self.layout()
        h = lay.totalHeightForWidth(self.width()) if lay is not None and lay.hasHeightForWidth() else 0
        if h != self._hfw_min:
            self._hfw_min = h
            self.updateGeometry()

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self._sync_hfw()

    def event(self, ev) -> bool:
        if ev.type() == QEvent.LayoutRequest:
            self._sync_hfw()
        return super().event(ev)

    timeChanged = Signal(float)

    def __init__(self, tr: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._hfw_min = 0                 # the height its wrapped state line needs at its width
        self.tr_ = tr
        self.timeline: Optional[Timeline] = None
        self.t = 0.0
        self.setObjectName("preview_scrubber")
        t = tr.tr
        lay = QVBoxLayout(self)
        style.margins(lay, "tight")
        row = QHBoxLayout()
        from mlaser.ui.icons import COMMAND_ICONS, icon

        self._icons = (icon("player-play"), icon("player-pause"))
        # three icon buttons, the words in the tooltip and the accessible name (never a glyph)
        self.prev_btn = style.button(icon=COMMAND_ICONS["preview_prev_contour"], name="preview_prev_contour")
        self.play_btn = style.button()
        self.play_btn.setIcon(self._icons[0])
        self.next_btn = style.button(icon=COMMAND_ICONS["preview_next_contour"], name="preview_next_contour")
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
        self.time_label = _TimeLabel("00:00 / 00:00")
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
        self.clear_btn = style.button(t("preview.clear"))
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

    #: shortest frame interval (ms); a frame that takes longer stretches the next interval so the
    #: event loop always has time for the buttons (issue #7)
    FRAME_MS = 33

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
        t0 = time.perf_counter()
        self.seek(t)
        spent_ms = (time.perf_counter() - t0) * 1000.0
        if self._timer.isActive():
            # at least as much idle time as the frame took: input is never starved
            self._timer.setInterval(int(max(self.FRAME_MS, 2.0 * spent_ms)))

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

    #: "previous contour" within this much wall time of a contour's start goes to the contour
    #: before it (issue #7); scaled by the playback speed while playing
    PREV_GRACE_S = 0.6

    def step_contour(self, step: int) -> None:
        tl = self.timeline
        if tl is None or not tl.markers:
            return
        speed = float(self.speed.currentData() or 1) if self.playing else 1.0
        self.seek(tl.next_contour_time(self.t, step, grace=self.PREV_GRACE_S * speed))

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
        style.margins(lay, "tight")
        self.title = QLabel(tr.tr("cost.title"))
        self.title.setTextFormat(Qt.PlainText)
        style.set_font(self.title, "label_strong")
        lay.addWidget(self.title)
        self.hint = QLabel(tr.tr("cost.hint"))
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)
        lay.addWidget(self.hint)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignLeft)
        lay.addLayout(self.form)
        self.rates_btn = style.button(tr.tr("cost.rates"))
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
                txt = f"<i>{html.escape(t.tr('cost.missing'))}</i>"  # i18n: ok (markup around one text)
            else:
                txt = self._fmt(ln)
                if ln.kind == "estimate":
                    txt = "≈ " + txt
            if ln.kind == "missing":
                txt += f" <span style='color:{style.css('warning')}'>{note}</span>" if note else ""
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
