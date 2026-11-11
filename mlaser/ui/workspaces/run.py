"""Run workspace: the touch-first operator screen and the job queue (docs/review/ux.md P1-5,
P1-7, wireframe §4.4).

* **Operator screen** (:class:`OperatorScreen`): a large state display (READY / RUNNING / HOLD /
  FAULT / ALARM / OFFLINE, colour + text + icon, with the reason), progress, elapsed time and
  ETA, sheet / part / contour counters, a live view of the sheet (cut parts grey, the current
  part highlighted, the head as a dot), the live process values (speed, power, gas, height,
  position), large Start / Hold / Stop / Frame / Dry-run / Arm targets, breakpoint resume
  ("here" / "contour start") and the alarm list with remedies (``ui.safety_texts``).
  Every machine action goes through the main window's :class:`mlaser.ui.safety_ui.SafetyUI`
  (arming question, pre-flight dialog, ``window.run_job`` -> planned job -> the controller's
  supervised ``start_job``), exactly like the run bar; nothing here talks to the card.
* **Job queue** (:class:`QueuePanel`): documents with quantities, order and status
  (:class:`mlaser.services.queue.JobQueue`, persisted in ``<data>/production/queue.json``),
  run sheet by sheet with an operator confirmation ("load the next sheet") between sheets
  (:class:`mlaser.services.queue.QueueRunner`).  After a cut sheet from a nest the leftover is
  stored as a remnant (:mod:`mlaser.services.remnants`); a remnant sheet is marked used.
* **LAN monitor** (optional, off by default): the read-only status page of :mod:`mlaser.web`.
* **Approval** (:class:`mlaser.ui.workspaces.approval.ApprovalPanel`): jobs submitted by the
  automation API or the CLI (:mod:`mlaser.services.intake`) wait above the queue until the
  operator approves them here; approving only adds them to the queue.

Targets are at least 56 px (primary) / 44 px (all others); the layout works at 1280 x 800 and
at 150 % scaling on 1920 x 1080 (``tests/test_ws_run.py``).  F11 toggles full screen in the
stand-alone window.
"""
from __future__ import annotations

import copy
import logging
import math
import os
import time
from typing import Any, Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from mlaser.machine import events as EV
from mlaser.services.config import Setting
from mlaser.services.intake import IntakeStore
from mlaser.services.job_parts import JobPartsTracker, LastJob
from mlaser.services.operator import RunStatus, fmt_duration, run_status
from mlaser.services.queue import JobQueue, QueueEntry, QueueRunner
from mlaser.services.remnants import RemnantStore, polygon_of
from mlaser.services import units as U

from .. import style
from ..icons import icon
from ..responsive import CollapsibleSection, ColumnFitter, ColumnsPanel, FlowLayout, keep_min_size
from ..safety_texts import remedy
from ..units_qt import on_units_changed
from . import TOUCH, TOUCH_MIN, show_workspace, tr
from . import trn

log = logging.getLogger("mlaser.ui.run")

STATE_TEXT = {"offline": ("run.state.offline", "OFFLINE"), "ready": ("run.state.ready", "READY"),
              "running": ("run.state.running", "RUNNING"), "hold": ("run.state.hold", "HOLD"),
              "fault": ("run.state.fault", "FAULT"), "alarm": ("run.state.alarm", "ALARM")}
REASONS = {
    "run.reason.fault": "Fault latched: {fault}",
    "run.reason.alarm": "Alarm {code}: {text}",
    "run.reason.offline": "No machine connected",
    "run.reason.breakpoint": "Held at a breakpoint ({position}): resume here or from the contour start",
    "run.reason.hold": "Held: press Continue to go on",
    "run.reason.cutting": "Cutting",
    "run.reason.dry_run": "Dry run: laser off",
    "run.reason.frame": "Framing the job outline: laser off",
    "run.reason.laser_off": "Ready: laser output disabled (dry-run mode)",
    "run.reason.armed": "Ready: ARMED, the laser fires on Start",
    "run.reason.disarmed": "Ready: disarmed, the laser cannot fire",
}
STATUS_TEXT = {"waiting": ("queue.status.waiting", "waiting"), "running": ("queue.status.running", "running"),
               "done": ("queue.status.done", "done"), "stopped": ("queue.status.stopped", "stopped"),
               "skipped": ("queue.status.skipped", "skipped"),
               "interrupted": ("queue.status.interrupted", "interrupted")}
QUEUE_MSGS = {
    "queue.msg.empty": "The queue has no job left to run.",
    "queue.msg.load_sheet": "Load the next sheet, then confirm.",
    "queue.msg.finished": "Queue finished.",
    "queue.msg.start_refused": "The sheet did not start (pre-flight or arming): check, then confirm again.",
    "queue.msg.sheet_stopped": "The sheet was stopped: confirm to run it again, or skip it in the queue.",
}


def tabular(label: QLabel) -> QLabel:
    """Figures of equal width in ``label``'s font (OpenType ``tnum``): a value that changes
    while a job runs keeps its place instead of shifting the row (UI deep review 13).  A font
    without the feature draws its own figures."""
    f = label.font()
    f.setFeature(QFont.Tag("tnum"), 1)
    label.setFont(f)
    return label


def _big(btn: QPushButton, h: int = TOUCH, w: int = 0, role: str = "label_strong") -> QPushButton:
    """A touch button: at least ``h`` px tall (kept against style sheets: an Operator-mode
    min-height rule used to drop it to 22 px after leaving Operator mode,
    responsive.keep_min_size), the type-scale ``role`` font."""
    keep_min_size(btn, QSize(w, h))
    style.set_font(btn, role)
    btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return btn


# ============================================================================== units
_MM_TEXT = None


def thickness_text(t_mm: Any) -> str:
    """A sheet thickness in the display units ("3 mm", "0.118 in"); "" when unset."""
    try:
        t = float(t_mm or 0.0)
    except (TypeError, ValueError):
        return ""
    return U.current().fmt(t, "length", trim=True) if t else ""


def mm_text(text: str) -> str:
    """Display text from a service that writes lengths as "<number> mm" (material check
    messages): each such length in the display units.  Display only: never parsed back."""
    import re
    global _MM_TEXT
    if _MM_TEXT is None:
        _MM_TEXT = re.compile(r"(-?\d+(?:\.\d+)?)\s?mm\b")
    u = U.current()
    if not u.imperial and u.unit("length") == "mm":
        return text
    return _MM_TEXT.sub(lambda m: u.fmt(float(m.group(1)), "length", trim=True), text)


def card_in_units(data: dict, entry: Any = None) -> dict:
    """The load card (services.material_check.load_card, which writes mm) in the display units:
    thickness, sheet size and the mismatch lines.  The technology names stay as stored (data)."""
    d = dict(data)
    if entry is not None:
        d["thickness"] = thickness_text(getattr(entry, "thickness_mm", 0.0))
        sb = (getattr(entry, "extra", None) or {}).get("sheet_bounds")
        if sb and len(sb) == 4:
            try:
                d["size"] = U.current().fmt_size(float(sb[2]) - float(sb[0]), float(sb[3]) - float(sb[1]),
                                                 decimals=0 if not U.current().imperial else 2)
            except (TypeError, ValueError):
                pass
    else:
        d["thickness"] = mm_text(str(d.get("thickness", "")))
        d["size"] = mm_text(str(d.get("size", "")))
    d["mismatch"] = [mm_text(str(m)) for m in d.get("mismatch", [])]
    return d


def run_tab_style() -> str:
    """The Run screen's tab bar (Live view / Queue): touch-sized tabs, and a selected tab that
    reads as selected in every theme (UX audit O11): bold text on the field colour with a 3 px
    bar in the highlight colour along its bottom, the others on the button colour.  Palette roles
    only (the theme's tokens, or the platform's palette): light, dark and high contrast alike."""
    return (f"QTabBar::tab{{min-height:{TOUCH_MIN - style.SP2}px;min-width:{style.SP5 * 4}px;"
            f"padding:{style.SP1}px {style.SP3}px;"
            "background:palette(button);color:palette(button-text);"
            "border:1px solid palette(mid);border-bottom:3px solid palette(button);"
            "border-top-left-radius:4px;border-top-right-radius:4px;}"
            "QTabBar::tab:selected{background:palette(base);color:palette(text);font-weight:bold;"
            "border-bottom:3px solid palette(highlight);}"
            "QTabBar::tab:!selected:hover{background:palette(midlight);}")


# ============================================================================== load card

class LoadSheetCard(QFrame):
    """"Load this sheet": shown while the queue waits for the next sheet.  Material, thickness,
    size, remnant id, sheet n/N in large type, a picture of the sheet with its nest (not the
    previous drawing), the technology and nozzle, the orders on it, and a red line when the
    sheet does not match the technology (Import & orders track,
    :func:`mlaser.services.material_check.load_card`)."""

    #: the picture's size (shown while the Queue tab hides the live view, which shows the sheet)
    PICTURE = (160, 100)

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window_ = window
        self.setObjectName("run_load_card")
        self.setFrameShape(QFrame.StyledPanel)
        style.styled(self, lambda: (f"#run_load_card{{border:2px solid {style.css('info')};"
                                    f"border-radius:{style.SP2}px;}}"))
        lay = style.hbox(self, "dense")
        self.picture = QLabel()
        self.picture.setObjectName("run_load_picture")
        self.picture.setFixedSize(*self.PICTURE)
        self.picture.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.picture)
        col = QVBoxLayout()
        self.title = QLabel(tr(window, "run.load.title", "Load this sheet"))
        style.set_font(self.title, "label_strong")
        col.addWidget(self.title)
        self.material = QLabel()
        self.material.setObjectName("run_load_material")
        style.set_font(self.material, "display")
        self.material.setWordWrap(True)
        col.addWidget(self.material)
        self.sheet = QLabel()
        self.sheet.setObjectName("run_load_sheet")
        style.set_font(self.sheet, "title")
        self.sheet.setWordWrap(True)
        col.addWidget(self.sheet)
        self.details = QLabel()
        self.details.setObjectName("run_load_details")
        self.details.setWordWrap(True)
        col.addWidget(self.details)
        self.warning = QLabel()
        self.warning.setObjectName("run_load_warning")
        self.warning.setWordWrap(True)
        style.styled(self.warning, lambda: (f"background:{style.css('danger_fill')};color:{style.css('on_danger')};"
                                            f"padding:{style.SP1}px {style.SP2}px;border-radius:{style.SP1}px;"))
        self.warning.setVisible(False)
        col.addWidget(self.warning)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.data: dict = {}
        self._key: Any = None
        # a units switch redraws the card at the next refresh
        on_units_changed(self, lambda _u: setattr(self, "_key", None), now=False)

    def show_card(self, data: dict, doc: Any, key: Any, entry: Any = None) -> None:
        if key == self._key:
            return
        self._key = key
        data = card_in_units(data, entry)
        self.data = data
        w = self.window_
        self.material.setText(" ".join(x for x in (data["material"], data["thickness"]) if x))
        bits = [tr(w, "run.load.size", "Sheet {size}", size=data["size"]) if data["size"] else ""]
        if data["remnant_id"]:
            bits.append(tr(w, "run.load.remnant", "remnant {id}", id=data["remnant_id"]))
        bits.append(tr(w, "run.load.n", "sheet {n}", n=data["sheet"]))
        self.sheet.setText("  ·  ".join(b for b in bits if b))
        det = []
        if data["technology"]:
            det.append(tr(w, "run.load.tech", "Technology: {tech}", tech="; ".join(data["technology"])))
        if data["nozzle"]:
            det.append(tr(w, "run.load.nozzle", "Nozzle: {nozzle}", nozzle=data["nozzle"]))
        if data["parts"]:
            det.append(tr(w, "run.load.parts", "{n} parts · {name}", n=data["parts"], name=data["name"]))
        det += data["orders"][:6]
        self.details.setText("\n".join(det))
        mm = data["mismatch"]
        self.warning.setVisible(bool(mm))
        self.warning.setText("⚠ " + "\n⚠ ".join(mm) if mm else "")
        from .orders import parts_pixmap
        parts = [p for p in getattr(doc, "parts", []) if p.contours] if doc is not None else []
        pw, ph = self.PICTURE
        self.picture.setPixmap(parts_pixmap(parts, QSize(pw - 4, ph - 4), self.palette(), data.get("rings")))


# ============================================================================== readout
class ReadoutRow(QWidget):
    """The live process values as label / value pairs (UI deep review 13): a muted caption
    above each value, the value in tabular figures (it does not shift as it changes); the pairs
    wrap between them (FlowLayout), never inside one.  :meth:`text`: every pair on one line
    (the accessible description, tests)."""

    KEYS = (("speed", "run.cap.speed", "Speed"), ("power", "run.cap.power", "Power"),
            ("gas", "run.cap.gas", "Gas"), ("height", "run.cap.height", "Height"),
            ("x", "run.cap.x", "X"), ("y", "run.cap.y", "Y"), ("z", "run.cap.z", "Z"))

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window_ = window
        self.setObjectName("run_values")
        self.flow = FlowLayout(self, justify=False, hspacing=style.SP4)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.captions: dict[str, QLabel] = {}
        self.fields: dict[str, QLabel] = {}
        for key, tid, default in self.KEYS:
            tile = QWidget()
            tile.setObjectName(f"run_value_{key}")
            v = style.vbox(tile, sp=0)
            cap = style.caption(tr(window, tid, default), wrap=False)
            val = QLabel("-")
            val.setObjectName(f"run_value_{key}_value")
            val.setTextFormat(Qt.PlainText)
            style.set_font(val, "label_strong")
            tabular(val)
            v.addWidget(cap)
            v.addWidget(val)
            self.flow.addWidget(tile)
            self.captions[key], self.fields[key] = cap, val
        # the link is lost: the last values the card reported, muted, and said so (W2)
        self.note = style.caption(tr(window, "run.val.last_known_note", "Last known values"), wrap=False)
        self.note.setObjectName("run_values_note")
        self.note.setVisible(False)
        self.flow.addWidget(self.note)
        self.stale = False
        self._text = ""

    def set_values(self, values: dict, stale: bool = False) -> None:
        for key, text in values.items():
            lab = self.fields[key]
            if lab.text() != text:
                lab.setText(text)
                # a tile only grows: the row does not jump back and forth as figures change
                need = lab.fontMetrics().horizontalAdvance(text)
                if need > lab.minimumWidth():
                    lab.setMinimumWidth(need)
        if stale != self.stale:
            self.stale = stale
            for lab in self.fields.values():
                style.styled(lab, (lambda: f"color:{style.css('muted')};") if stale else (lambda: ""))
            self.note.setVisible(stale)
        text = "    ".join(f"{self.captions[k].text()} {self.fields[k].text()}" for k, _t, _d in self.KEYS)
        if stale:
            text = tr(self.window_, "run.val.last_known", "{values}   (last known)", values=text)
        if text != self._text:
            self._text = text
            self.setAccessibleDescription(text)

    def text(self) -> str:
        return self._text


# ============================================================================== canvas
class RunCanvas(QWidget):
    """Live view of the sheet: done parts grey and dashed, the current part bright and thick,
    the rest in their layer colour, the head as a red dot (Y up, fitted).

    After a job (:meth:`set_rest`): the parts the last job finished dashed, the parts a new
    "cut the rest" job would cut in their layer colour, the parts it would leave out dotted, and
    each part's number in the last job's cut order; a click on a part picks it (:attr:`pick`,
    "Start from part")."""

    #: minimum height: as the shared canvas (CANVAS_MIN, a half-screen window); while the load
    #: card above it asks for the next sheet (:class:`LoadSheetCard`)
    MIN_H = 220
    MIN_H_WAITING = 100

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(260, self.MIN_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.document: Any = None
        self.sheet: Optional[tuple[float, float, float, float]] = None
        self.done: set = set()
        self.current = -1
        self.head: Optional[tuple[float, float]] = None
        self.skip: set = set()             # after a job: parts a new job would leave out
        self.numbers: dict = {}            # after a job: part index -> its number in the cut order
        self.pick: Any = None              # fn(part index): a click on a part (after a job)
        self._paths: list[tuple[int, int, QPainterPath]] = []      # (part index, layer, path)
        self._doc_id: Optional[int] = None
        self._bounds = (0.0, 0.0, 1.0, 1.0)

    def set_document(self, doc: Any, sheet: Optional[tuple[float, float, float, float]] = None) -> None:
        if doc is not None and id(doc) == self._doc_id and sheet == self.sheet:
            return
        self.document, self.sheet = doc, sheet
        self._doc_id = id(doc) if doc is not None else None
        self._paths = []
        parts = list(getattr(doc, "parts", []) or [])
        segs = sum(len(c.segments) for p in parts for c in p.contours)
        tol = 0.2 if segs < 5000 else 1.0
        xs: list[float] = []
        ys: list[float] = []
        for i, p in enumerate(parts):
            path = QPainterPath()
            for c in p.contours:
                pts = list(c.points(tol))
                if len(pts) < 2:
                    continue
                path.moveTo(pts[0][0], pts[0][1])
                for x, y in pts[1:]:
                    path.lineTo(x, y)
                if c.closed:
                    path.closeSubpath()
            if not path.isEmpty():
                r = path.boundingRect()
                xs += [r.left(), r.right()]
                ys += [r.top(), r.bottom()]
                self._paths.append((i, int(getattr(p.craft, "layer", 0)), path))
        if sheet is not None:
            xs += [sheet[0], sheet[2]]
            ys += [sheet[1], sheet[3]]
        self._bounds = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 1.0, 1.0)
        self._a11y()
        self.update()

    def set_progress(self, done, current: int, head: Optional[tuple[float, float]]) -> None:
        done = set(done)
        moved = head is not None and (self.head is None or math.dist(head, self.head) > 0.2)
        if done != self.done or current != self.current or moved or (head is None) != (self.head is None):
            if done != self.done or current != self.current:
                self.done, self.current = done, current
                self._a11y()
            self.head = head
            self.update()

    def set_rest(self, done, skip, numbers: dict) -> None:
        """After a job: ``done`` the parts it finished, ``skip`` the parts the offered new job
        leaves out, ``numbers`` the cut-order numbers to draw (empty: the job view)."""
        done, skip = set(done), set(skip)
        if done != self.done or skip != self.skip or numbers != self.numbers:
            self.done, self.skip, self.numbers = done, skip, dict(numbers)
            self.current = -1
            self._a11y()
            self.update()
        self.setCursor(Qt.PointingHandCursor if self.numbers and self.pick is not None else Qt.ArrowCursor)

    def part_at(self, pos) -> int:
        """The part index under the widget point ``pos`` (-1: none): inside a closed outline,
        or within a few pixels of it; the smallest such part wins."""
        if not self._paths:
            return -1
        s, ox, oy = self._transform()
        pt = QPointF((pos.x() - ox) / s, (oy - pos.y()) / s)
        tol = 6.0 / s
        best, area = -1, float("inf")
        for i, _layer, path in self._paths:
            r = path.boundingRect().adjusted(-tol, -tol, tol, tol)
            if not r.contains(pt):
                continue
            a = r.width() * r.height()
            if a < area:
                best, area = i, a
        return best

    def mousePressEvent(self, ev) -> None:  # noqa: N802 - Qt API
        if self.pick is not None and self.numbers and ev.button() == Qt.LeftButton:
            i = self.part_at(ev.position())
            if i in self.numbers:
                self.pick(i)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def _a11y(self) -> None:
        """Accessible name and the progress as its description (F7: the view is painted)."""
        from .. import a11y
        t = a11y.translator_for(self)
        n = len(self._paths)
        a11y.name(self, t.tr("a11y.run.sheet"),
                  t.plural("a11y.run.sheet_progress", n, done=len(self.done & set(range(n)))) if n else
                  t.tr("a11y.run.sheet_empty"))

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self._a11y()

    def _transform(self) -> tuple[float, float, float]:
        x0, y0, x1, y1 = self._bounds
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        m = 16
        s = min((self.width() - 2 * m) / w, (self.height() - 2 * m) / h)
        s = max(s, 1e-6)
        ox = (self.width() - w * s) / 2 - x0 * s
        oy = (self.height() + h * s) / 2 + y0 * s
        return s, ox, oy

    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt API
        from ..canvas import CANVAS_COLORS
        from ..canvas import display_layer_color as layer_color   # theme-adjusted (contrast vs. canvas)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            pal = self.palette()
            p.fillRect(self.rect(), pal.color(QPalette.Base))
            if not self._paths:
                p.setPen(pal.color(QPalette.PlaceholderText))
                p.drawText(self.rect(), Qt.AlignCenter, tr(self.window(), "run.canvas.empty", "No job loaded"))
                return
            s, ox, oy = self._transform()
            p.translate(ox, oy)
            p.scale(s, -s)
            if self.sheet is not None:
                x0, y0, x1, y1 = self.sheet
                p.setPen(QPen(pal.color(QPalette.Mid), 0))
                p.setBrush(pal.color(QPalette.AlternateBase))
                p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            p.setBrush(Qt.NoBrush)
            # done parts grey, the part being cut in the accent colour (never the alarm red:
            # UX audit O8), the rest in their layer colour
            done_pen = QPen(self.done_color(), 0, Qt.DashLine)     # not by colour alone
            skip_pen = QPen(self.done_color(), 0, Qt.DotLine)
            cur_col = self.current_color()
            for i, layer, path in self._paths:
                if i in self.done:
                    p.setPen(done_pen)
                elif i in self.skip:
                    p.setPen(skip_pen)
                elif i == self.current:
                    continue
                else:
                    p.setPen(QPen(layer_color(layer), 0))
                p.drawPath(path)
            for i, _layer, path in self._paths:
                if i == self.current:
                    pen = QPen(cur_col, 3.0 / s)
                    p.setPen(pen)
                    p.drawPath(path)
            if self.head is not None:
                r = 6.0 / s
                p.setPen(QPen(pal.color(QPalette.Base), 1.5 / s))
                p.setBrush(QColor(CANVAS_COLORS["head"]))
                p.drawEllipse(QPointF(*self.head), r, r)
            p.resetTransform()
            self._numbers(p, s, ox, oy)
            self._legend(p)
        finally:
            p.end()

    @staticmethod
    def done_color() -> QColor:
        return style.color("muted")

    @staticmethod
    def current_color() -> QColor:
        return style.color("accent")

    def _numbers(self, p: QPainter, s: float, ox: float, oy: float) -> None:
        """Each part's number in the last job's cut order, on a small plate at its centre."""
        if not self.numbers:
            return
        fm = p.fontMetrics()
        pal = self.palette()
        p.setPen(QPen(pal.color(QPalette.Mid), 1))
        for i, _layer, path in self._paths:
            n = self.numbers.get(i)
            if not n:
                continue
            c = path.boundingRect().center()
            text = str(n)
            w, h = fm.horizontalAdvance(text) + style.SP2, fm.height()
            r = QRectF(c.x() * s + ox - w / 2, oy - c.y() * s - h / 2, w, h)
            p.setPen(QPen(pal.color(QPalette.Mid), 1))
            p.setBrush(pal.color(QPalette.Base))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(pal.color(QPalette.Text))
            p.drawText(r, Qt.AlignCenter, text)
        p.setBrush(Qt.NoBrush)

    def legend_items(self) -> list[tuple[str, QColor, Qt.PenStyle]]:
        w = self.window()
        if self.numbers:
            return [(tr(w, "run.legend.done", "Done"), self.done_color(), Qt.DashLine),
                    (tr(w, "run.legend.todo", "To cut"), self.palette().color(QPalette.Text), Qt.SolidLine),
                    (tr(w, "run.legend.not_cut", "Left out"), self.done_color(), Qt.DotLine)]
        return [(tr(w, "run.legend.done", "Done"), self.done_color(), Qt.DashLine),
                (tr(w, "run.legend.current", "Cutting now"), self.current_color(), Qt.SolidLine),
                (tr(w, "run.legend.todo", "To cut"), self.palette().color(QPalette.Text), Qt.SolidLine)]

    def _legend(self, p: QPainter) -> None:
        """A small legend in the bottom-left corner: what grey, the accent and the layer
        colours mean (O8), and the line styles that tell them apart without colour."""
        fm = p.fontMetrics()
        x, y = style.SP2, self.height() - style.SP2 - fm.height()
        pal = self.palette()
        for text, col, pen_style in self.legend_items():
            sw = style.SP4
            p.setPen(QPen(col, 2, pen_style))
            p.drawLine(x, y + fm.height() // 2, x + sw, y + fm.height() // 2)
            x += sw + style.SP1
            p.setPen(pal.color(QPalette.Text))
            p.drawText(x, y + fm.ascent(), text)
            x += fm.horizontalAdvance(text) + style.SP3


# ============================================================================== queue panel
class QueuePanel(QWidget):
    """The job queue table with touch-size controls."""

    COLS = (("queue.col.order", "#"), ("queue.col.name", "Job"), ("queue.col.qty", "Qty"),
            ("queue.col.done", "Done"), ("queue.col.status", "Status"), ("queue.col.material", "Material"),
            ("queue.col.sheet", "Sheet"), ("queue.col.est", "Est. / sheet"), ("queue.col.actual", "Actual"),
            ("queue.col.note", "Note"))

    def __init__(self, ws: "RunWorkspace") -> None:
        super().__init__()
        self.ws = ws
        w = ws.window
        v = QVBoxLayout(self)
        # the tab pane's frame is the border: the default margins made the Queue tab taller
        # than a short screen's room for it
        v.setContentsMargins(style.SP1, style.SP1, style.SP1, style.SP1)
        from .approval import ApprovalPanel
        self.approval = ApprovalPanel(ws)            # submitted jobs waiting for the operator
        v.addWidget(self.approval)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setObjectName("queue_table")
        self.table.setHorizontalHeaderLabels([tr(w, k, d) for k, d in self.COLS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(TOUCH_MIN)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.table, 1)
        self.totals = QLabel()
        v.addWidget(self.totals)
        # the key column (Job) stretches; the others hide by priority when the view is narrow
        ColumnFitter(self.table, {0: 2, 2: 3, 3: 3, 4: 4, 5: 1, 6: 1, 7: 0, 8: 0, 9: -1}, stretch=1)
        # each button at its own width: the uniform width of the widest item (the quantity box)
        # put one button per row in a half-screen window, seven rows of them
        g = FlowLayout(uniform=False)
        self.buttons: dict[str, QPushButton] = {}
        specs = (("add", "queue.btn.add", "Add job...", self.add_file),
                 ("up", "queue.btn.up", "Move up", lambda: self._move(-1)),
                 ("down", "queue.btn.down", "Move down", lambda: self._move(1)),
                 ("remove", "queue.btn.remove", "Remove", self.remove),
                 ("skip", "queue.btn.skip", "Skip / unskip", self.skip),
                 ("reset", "queue.btn.reset", "Run again", self.reset),
                 ("clear", "queue.btn.clear", "Clear finished", self.clear_finished))
        for n, (key, tid, text, fn) in enumerate(specs):
            b = _big(QPushButton(tr(w, tid, text)), TOUCH_MIN, role="body")
            b.setObjectName(f"queue_{key}")
            b.clicked.connect(lambda _=False, f=fn: f())
            g.addWidget(b)
            self.buttons[key] = b
        qty_box = QWidget()
        qty = QHBoxLayout(qty_box)
        qty.setContentsMargins(0, 0, 0, 0)
        qty.addWidget(QLabel(tr(w, "queue.qty", "Quantity (sheets)")))
        self.qty = QSpinBox()
        self.qty.setObjectName("queue_qty")
        self.qty.setRange(1, 9999)
        self.qty.setMinimumHeight(TOUCH_MIN)
        self.qty.setMinimumWidth(110)
        self.qty.editingFinished.connect(self._qty_changed)
        qty.addWidget(self.qty)
        g.addWidget(qty_box)
        v.addLayout(g)
        self.table.itemSelectionChanged.connect(self._selected)
        self._sync = False
        on_units_changed(self, lambda _u: self.refresh(), now=False)   # thickness column

    def selected_id(self) -> Optional[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return None
        it = self.table.item(rows[0], 0)
        return it.data(Qt.UserRole) if it is not None else None

    def refresh(self) -> None:
        q = self.ws.queue
        w = self.ws.window
        self.approval.refresh()
        keep = self.selected_id()
        self._sync = True
        self.table.setRowCount(len(q))
        cur = self.ws.runner.current
        for r, e in enumerate(q):
            st_key, st_def = STATUS_TEXT.get(e.status, (e.status, e.status))
            sheet = e.remnant_id or e.sheet
            mat = " ".join(x for x in (e.material, thickness_text(e.thickness_mm)) if x)
            vals = [str(r + 1), e.name, str(e.quantity), str(e.done), tr(w, st_key, st_def), mat, sheet,
                    fmt_duration(e.est_s) if e.est_s else "-", fmt_duration(e.actual_s) if e.actual_s else "-",
                    _msg(w, e.note)]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setData(Qt.UserRole, e.id)
                if cur is not None and e.id == cur.id:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                self.table.setItem(r, c, it)
            if e.id == keep:
                self.table.selectRow(r)
        t = q.totals()
        self.totals.setText(trn(w, "queue.totals", t["entries"], done=t["done"], sheets=t["sheets"],
                               left=fmt_duration(t["remaining_s"]) if t["remaining_s"] else "-"))
        self._sync = False
        self._selected()

    def _selected(self) -> None:
        eid = self.selected_id()
        e = None
        if eid is not None:
            try:
                e = self.ws.queue.get(eid)
            except KeyError:
                e = None
        running = e is not None and e.status == "running"
        for k in ("up", "down", "remove", "skip", "reset"):
            self.buttons[k].setEnabled(e is not None and not (running and k in ("remove", "skip", "reset")))
        self.qty.setEnabled(e is not None)
        if e is not None and not self._sync:
            self.qty.blockSignals(True)
            self.qty.setValue(e.quantity)
            self.qty.blockSignals(False)

    # ------------------------------------------------------------------ actions
    def add_file(self, path: Optional[str] = None, quantity: int = 1) -> Optional[QueueEntry]:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self.ws.window, tr(self.ws.window, "queue.btn.add", "Add job..."),
                                                  "", "Jobs (*.chf *.dxf *.svg);;All (*)")
        if not path:
            return None
        e = self.ws.queue.add(path, quantity)
        return e

    def _move(self, d: int) -> None:
        eid = self.selected_id()
        if eid:
            self.ws.queue.move(eid, d)

    def remove(self) -> None:
        eid = self.selected_id()
        if eid:
            e = self.ws.queue.get(eid)
            if e.status == "running":
                return
            self.ws.release_remnant(e)
            self.ws.queue.remove(eid)

    def skip(self) -> None:
        eid = self.selected_id()
        if eid:
            e = self.ws.queue.get(eid)
            try:
                self.ws.queue.skip(eid, e.status != "skipped")
            except ValueError as exc:
                self.ws.log(str(exc))

    def reset(self) -> None:
        eid = self.selected_id()
        if eid:
            try:
                self.ws.queue.reset(eid)
            except ValueError as exc:
                self.ws.log(str(exc))

    def clear_finished(self) -> None:
        self.ws.queue.clear_finished()

    def _qty_changed(self) -> None:
        eid = self.selected_id()
        if eid:
            self.ws.queue.set_quantity(eid, self.qty.value())


def _msg(window: Any, text: str) -> str:
    """A queue message: an i18n id of :data:`QUEUE_MSGS` or plain text."""
    if text in QUEUE_MSGS:
        return tr(window, text, QUEUE_MSGS[text])
    return text


# ============================================================================== operator screen
class OperatorScreen(QWidget):
    """The Run workspace widget (wireframe §4.4)."""

    def __init__(self, ws: "RunWorkspace") -> None:
        super().__init__()
        self.ws = ws
        w = ws.window
        self.setObjectName("run_workspace")
        root = style.vbox(self, "dense")

        # --- header: the state block (the machine state with its icon, the reason, and while an
        # alarm or a fault is active its remedy with Reset alarm and Details: never below the
        # fold, UX audit W11), counters, progress, times
        head = QFrame()
        head.setObjectName("run_header")
        hl = QGridLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        from ..state_display import StateBadge
        self.state_block = QFrame()
        self.state_block.setObjectName("run_state_block")
        self.state_block.setAttribute(Qt.WA_StyledBackground, True)
        sb = style.hbox(self.state_block, "tight")
        self.state = StateBadge("run_state_big", role="display_run", icon_px=style.ICON_L + style.SP2)
        self.state.setMinimumWidth(style.SP5 * 6)
        sb.addWidget(self.state)
        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(style.SP1)
        self.reason = QLabel()
        self.reason.setObjectName("run_reason")
        self.reason.setWordWrap(True)
        style.set_font(self.reason, "title")
        texts.addWidget(self.reason)
        self.remedy = QLabel()
        self.remedy.setObjectName("run_remedy")
        self.remedy.setWordWrap(True)
        self.remedy.setVisible(False)
        texts.addWidget(self.remedy)
        sb.addLayout(texts, 1)
        self.reset_btn = _big(QPushButton(tr(w, "run.btn.reset", "Reset alarm")), TOUCH_MIN)
        self.reset_btn.setObjectName("run_reset")
        self.reset_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.reset_btn.clicked.connect(lambda: self._ui("reset_fault"))
        self.details_btn = _big(QPushButton(tr(w, "run.btn.details", "Details")), TOUCH_MIN, role="body")
        self.details_btn.setObjectName("run_details")
        self.details_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.details_btn.clicked.connect(lambda: self._ui("alarm_details"))
        for x in (self.reset_btn, self.details_btn):
            x.setVisible(False)
            sb.addWidget(x, 0, Qt.AlignVCenter)
        hl.addWidget(self.state_block, 0, 0, 1, 3)
        self.counters = QLabel()
        self.counters.setObjectName("run_counters")
        style.set_font(self.counters, "title")
        texts.addWidget(self.counters)          # in the block: the header is as tall as before
        # the last job (UX deep review 5): "Last job stopped at 14:02 in part 5 of 6 ...", kept
        # until the next start; with the reason when its "cut the rest" offer is withdrawn
        self.last_job = QLabel()
        self.last_job.setObjectName("run_last_job")
        self.last_job.setWordWrap(True)
        self.last_job.setVisible(False)
        texts.addWidget(self.last_job)
        self.monitor_chip = QLabel()
        self.monitor_chip.setObjectName("run_monitor_chip")
        style.chip(self.monitor_chip, "warning")
        self.monitor_chip.setVisible(False)
        # the automation API (mlaser.ui.ext_automation): read-only, queue intake needs approval
        self.api_chip = QLabel()
        self.api_chip.setObjectName("run_api_chip")
        style.chip(self.api_chip, "warning")
        self.api_chip.setVisible(False)
        chips = QHBoxLayout()
        # the readiness strip (UX audit W3): what still stops a Start, each with its fix
        # (services.readiness through SafetyUI.readiness); the connection, the alarms and the
        # arming have their own place (banner, state block)
        self.ready_box = QWidget()
        self.ready_box.setObjectName("run_readiness")
        self.ready_flow = FlowLayout(self.ready_box, justify=False)
        self.ready_buttons: dict[str, QPushButton] = {}         # each item's action
        self.ready_notices: dict[str, QWidget] = {}
        self._ready_tick = 0
        self.ready_box.setVisible(False)
        chips.addWidget(self.ready_box, 1)
        chips.addWidget(self.api_chip)
        chips.addWidget(self.monitor_chip)
        hl.addLayout(chips, 1, 0, 1, 3)
        self.progress = QProgressBar()
        self.progress.setObjectName("run_progress")
        self.progress.setRange(0, 1000)
        self.progress.setMinimumHeight(style.TARGET)
        self.progress.setTextVisible(True)
        hl.addWidget(self.progress, 2, 0, 1, 2)
        self.times = QLabel()
        self.times.setObjectName("run_times")
        style.set_font(self.times, "label_strong")
        hl.addWidget(self.times, 2, 2)
        hl.setColumnStretch(1, 1)
        root.addWidget(head)
        # safety I/O that is not configured (ui/safety_io.py): "door interlock not configured ..."
        self.safety_notice = QLabel()
        self.safety_notice.setObjectName("run_safety_notice")
        self.safety_notice.setWordWrap(True)
        style.chip(self.safety_notice, "warning")
        self.safety_notice.setVisible(False)
        root.addWidget(self.safety_notice)

        body = style.hbox()
        root.addLayout(body, 1)

        # --- left: live view / queue tabs, process values, alarms
        left = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("run_tabs")
        self.tabs.tabBar().setMinimumHeight(TOUCH_MIN)
        # touch-sized tabs on the operator screen (a domain screen: the only tab-bar style sheet)
        self.tabs.setStyleSheet(run_tab_style())
        self.canvas = RunCanvas()
        self.tabs.addTab(self.canvas, tr(w, "run.tab.live", "Live view"))
        self.queue_panel = QueuePanel(ws)
        self.tabs.addTab(self.queue_panel, tr(w, "run.tab.queue", "Queue"))
        self.load_card = LoadSheetCard(w)          # "Load this sheet" (queue waiting for a sheet)
        self.load_card.setVisible(False)
        left.addWidget(self.load_card)
        left.addWidget(self.tabs, 1)
        self.values = ReadoutRow(w)
        left.addWidget(self.values)
        body.addLayout(left, 1)
        # only the page on top counts for the page's minimum height: the queue table's controls,
        # wrapped at a narrow width, made the live view's column taller than a short screen
        # (a QStackedWidget's minimum is its tallest page's)
        self._tab_policies = {id(p): p.sizePolicy() for p in (self.canvas, self.queue_panel)}
        self.tabs.currentChanged.connect(lambda _i: self._tab_minimums())
        self._tab_minimums()

        # --- right: the big targets (ui/responsive.py ColumnsPanel): STOP, Hold / Start,
        # Frame / Dry run and Arm always shown in full; the breakpoint resume and the queue card
        # below them, in a second column when the page is too short for one, folded (resume
        # first) only when even that does not fit; the queue card stays open while it asks for
        # the next sheet
        self.controls = ColumnsPanel(max_columns=2, compact=False, grow=True)
        self.controls.setObjectName("run_controls_column")
        self.buttons: dict[str, QPushButton] = {}

        def add(key: str, tid: str, text: str, fn, h: int = TOUCH, role: str = "label_strong",
                ico: str = "") -> QPushButton:
            b = _big(QPushButton(tr(w, tid, text)), h, role=role)
            b.setObjectName(f"run_{key}")
            if ico:
                b.setIcon(icon(ico))
                b.setIconSize(QSize(style.ICON_L, style.ICON_L))
            b.clicked.connect(lambda _=False, f=fn: f())
            self.buttons[key] = b
            return b

        def run(kind: str, h: int, role: str) -> QPushButton:
            """A run control from the one factory (style.run_button: the run bar's words, icons
            and colours), touch-sized for this screen."""
            b = style.run_button(kind, getattr(w, "tr_", None), lambda k=kind: self._ui(
                {"arm": "toggle_arm"}.get(k, k)), height=h)
            style.set_font(b, role)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.buttons[kind] = b
            return b

        def row(*widgets: QWidget) -> QWidget:
            box = QWidget()
            lay = style.hbox(box)
            for x in widgets:
                lay.addWidget(x)
            return box

        # the run bar's order (Start | Hold | STOP | Frame | Dry run | Arm), also for the keyboard;
        # the run bar shows only its state and progress here: one set of run controls (W4)
        self.controls.add_pinned(row(run("start", int(TOUCH * 1.2), "title"), run("hold", int(TOUCH * 1.2), "title")))
        self.controls.add_pinned(run("stop", int(TOUCH * 1.4), "display"))
        self.controls.add_pinned(row(run("frame", TOUCH, "label_strong"), run("dry_run", TOUCH, "label_strong")))
        self.controls.add_pinned(run("arm", TOUCH, "label_strong"))
        resume = row(add("resume_here", "run.btn.resume_here", "Here", lambda: self.resume("exact"), TOUCH_MIN,
                         ico="player-skip-forward"),
                     add("resume_contour", "run.btn.resume_contour", "Contour start",
                         lambda: self.resume("contour"), TOUCH_MIN, ico="restore"))
        self.resume_section = CollapsibleSection(tr(w, "run.resume", "Resume from the breakpoint"), resume,
                                                 priority=0)
        self.resume_section.setObjectName("run_resume_section")
        self.controls.add_section(self.resume_section)
        # after a stopped or finished job: a NEW job of the parts not finished, or from a part
        # in the cut order (UX deep review 5).  Both select those parts and use the ordinary
        # "Start selection only" (pre-flight, arming question, compile: unchanged); unlike the
        # resume above they start each part from its beginning
        rest = QWidget()
        rest.setObjectName("run_rest_box")
        rv = style.vbox(rest)
        self.rest_hint = style.caption(tr(w, "run.rest.hint", "A new job: each part from its beginning, with "
                                          "Start's checks. Resume continues inside the stopped contour."))
        self.rest_hint.setObjectName("run_rest_hint")
        rv.addWidget(self.rest_hint)
        rv.addWidget(add("cut_remaining", "run.btn.cut_remaining", "Cut remaining parts", self.cut_remaining,
                         TOUCH_MIN, ico="cut"))
        self.from_spin = QSpinBox()
        self.from_spin.setObjectName("run_from_part")
        self.from_spin.setRange(1, 1)
        self.from_spin.setMinimumHeight(TOUCH_MIN)
        self.from_spin.setAccessibleName(tr(w, "run.rest.from_name", "First part to cut (cut-order number)"))
        self.from_spin.setToolTip(tr(w, "run.rest.from_tip", "The number of the first part to cut, as numbered "
                                     "in the live view: type it, or click the part there"))
        self.from_spin.valueChanged.connect(self._from_changed)
        self._from_chosen = False             # the operator chose the part to start from
        from_btn = add("start_from", "run.btn.start_from", "Start from part...", self.start_from, TOUCH_MIN,
                       ico="arrow-bar-right")
        rv.addWidget(row(self.from_spin, from_btn))
        self.rest_section = CollapsibleSection(tr(w, "run.rest.title", "Cut the rest: new job"), rest, priority=0)
        self.rest_section.setObjectName("run_rest_section")
        self.rest_section.setVisible(False)
        self.controls.add_section(self.rest_section)
        self.canvas.pick = self.pick_part
        self._rest_key: Any = None
        # queue card
        card = QWidget()
        card.setObjectName("run_queue_card")
        cv = style.vbox(card)
        self.queue_msg = QLabel()
        self.queue_msg.setObjectName("run_queue_msg")
        self.queue_msg.setWordWrap(True)
        cv.addWidget(self.queue_msg)
        cv.addWidget(add("confirm_sheet", "run.btn.confirm_sheet", "Sheet loaded: start",
                         self.confirm_sheet, TOUCH, "title", "truck-loading"))
        cv.addWidget(add("queue_mode", "run.btn.queue_run", "Run queue", self.toggle_queue, TOUCH_MIN,
                         ico="list-numbers"))
        self.queue_section = CollapsibleSection("", card, priority=1)
        self.queue_section.setObjectName("run_queue_section")
        self.controls.add_section(self.queue_section)
        body.addWidget(self.controls)

        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self._kind = ""
        self.status = RunStatus()

    def _tab_minimums(self) -> None:
        cur = self.tabs.currentWidget()
        # the load card's picture of the sheet only when the live view (the same sheet and its
        # nest, larger) is not on top: the card fits a half-screen window beside the controls
        self.load_card.picture.setVisible(cur is not self.canvas)
        for i in range(self.tabs.count()):
            page = self.tabs.widget(i)
            pol = self._tab_policies.get(id(page))
            if pol is not None:
                page.setSizePolicy(pol if page is cur else QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored))

    # ------------------------------------------------------------------ actions
    def _ui(self, name: str) -> None:
        """Run a SafetyUI action (the run bar's: pre-flight, arming, planned job)."""
        ui = getattr(self.ws.window, "safety_ui", None)
        fn = getattr(ui, name, None)
        if fn is None:
            self.ws.log(f"run: {name} is not available")
            return
        fn()
        self.refresh()

    def resume(self, mode: str) -> None:
        """Breakpoint resume ("exact" = here, "contour" = from the contour start).  The
        controller runs the pre-flight on the resumed job; arming is asked first when the job
        fires the laser."""
        w = self.ws.window
        ctl = w.ctl
        from ..resume import moving, resume
        if ctl.breakpoint is None or moving(ctl):
            return
        st = ctl.safety()
        ui = getattr(w, "safety_ui", None)
        if ctl.laser_enabled and not st.armed and ctl.breakpoint.mode == "process":
            if ui is None or not ui.arm():
                return
        resume(ctl, mode, w.document)        # a breakpoint of another document is refused
        if ui is not None:
            ui._after(tr(w, "run.resume", "Resume from the breakpoint"))
        self.refresh()

    def confirm_sheet(self) -> None:
        self.ws.runner.confirm_sheet()
        self.refresh()

    def cut_remaining(self) -> None:
        """A new job of the parts the last job did not finish (UX deep review 5)."""
        self.ws.cut_rest(None)
        self.refresh()

    def start_from(self) -> None:
        """A new job from the part numbered in the spin box (the last job's cut order) to the end."""
        self.ws.cut_rest(self.from_spin.value())
        self.refresh()

    def pick_part(self, canvas_index: int) -> None:
        """A click on a part in the live view after a job: "Start from" that part."""
        n = self.canvas.numbers.get(canvas_index, 0)
        if n:
            self.from_spin.setValue(n)
            self.from_spin.setFocus(Qt.MouseFocusReason)

    def toggle_queue(self) -> None:
        r = self.ws.runner
        if r.state == "idle":
            r.begin()
            self.tabs.setCurrentWidget(self.canvas)
        else:
            r.cancel()
        self.refresh()

    # ------------------------------------------------------------------ refresh
    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.timer.start()
        self.queue_panel.refresh()
        self.refresh()

    def hideEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().hideEvent(ev)
        self.timer.stop()

    def refresh(self) -> None:
        """Bring the screen up to the controller's state (every 150 ms while shown): one
        :class:`RunStatus`, then one updater per region of the screen (review 2026-11-09, arch
        A15): the state block, the counters and progress, the process values, the alarms, the
        run buttons, the queue card, the readiness strip, the chips, the live view."""
        s = self.ws.status()
        self.status = s
        ms = self.ws.machine_state()
        connected = bool(ms is not None and ms.connected)
        running = s.kind in ("running", "hold") or s.mode != ""
        self._refresh_state(s, ms)
        self._refresh_progress(s, running)
        self._refresh_values(s, connected)
        self._alarms(s)
        job = self.ws.job_running()
        self._refresh_buttons(s, ms, job)
        self._refresh_queue(job, connected, s.kind == "fault")
        self._readiness(job)
        self._refresh_chips()
        self._refresh_canvas(s, running, connected)

    def _refresh_state(self, s: RunStatus, ms: Any) -> None:
        """The state block: the machine state with its icon and colours, the reason, and the
        safety I/O notice."""
        w = self.ws.window
        # state: the machine axis (state_display), with its icon, on the state colours
        from .. import state_display as SD
        ctl = w.ctl
        ui = getattr(w, "safety_ui", None)
        kind = SD.machine_state(ctl.safety(), ms, list(ctl.active_alarms or []), ctl,
                                getattr(ui, "need_home", None) if ui is not None else None)
        if kind not in ("not_homed", "estop"):
            kind = s.kind                      # the RunStatus's state (its reason matches it)
        word = SD.machine_label(w, kind)
        if kind == "ready" and s.armed:
            word = tr(w, "run.state.label_armed", "{state}  ·  {armed}", state=word,
                      armed=SD.laser_label(w, "armed"))
        # READY while armed shows the ARMED colour (the laser can fire: never the calm look)
        look = "ARMED" if kind == "ready" and s.armed else SD.colour_key("machine", kind)
        self.state.set_state(kind, look, word, SD.axis_label(w, "machine"))
        bg, fg = style.state_colors(look)
        qss = (f"QFrame#run_state_block{{background:{bg};border-radius:{style.SP2}px;}}"
               f"QFrame#run_state_block QLabel{{color:{fg};background:transparent;}}")
        if qss != self._kind:
            self.state_block.setStyleSheet(qss)
            self._kind = qss
        reason = self.ws.reason_text(s)
        if kind == "not_homed":
            reason = tr(w, "run.reason.not_homed", "Not homed: home X and Y before you start")
        if self.reason.text() != reason:
            self.reason.setText(reason)
        b = getattr(w, "safety_io", None)
        note = b.notice_text() if b is not None else ""
        if note != self.safety_notice.text():
            self.safety_notice.setText(note)
        if bool(note) != self.safety_notice.isVisibleTo(self):
            self.safety_notice.setVisible(bool(note))

    def _refresh_progress(self, s: RunStatus, running: bool) -> None:
        """The counters (sheet, part, contour; idle: what Start runs), the last job, progress
        and times."""
        w = self.ws.window
        # counters
        bits = []
        ent = self.ws.runner.current
        if ent is not None and self.ws.runner.state != "idle":
            bits.append(tr(w, "run.count.sheet_job", "Sheet {n}/{total} · {name}", n=min(ent.done + 1, ent.quantity),
                           total=ent.quantity, name=ent.name))
        if s.parts:
            bits.append(tr(w, "run.count.part", "part {n}/{total}", n=s.part, total=s.parts))
        if s.contours:
            bits.append(tr(w, "run.count.contour", "contour {n}/{total}", n=s.contour, total=s.contours))
        # idle: what Start will run (UX audit W18)
        self.counters.setText("  ·  ".join(bits) or tr(w, "run.start_target", "Start: {name}", name=self.ws.job_name()))
        self._last_job(running)
        p = s.progress if running else 0.0
        self.progress.setValue(int(round(p * 1000)))
        self.progress.setFormat(f"{p * 100:.0f} %" if running else "")
        # the run bar's figures (one RunStatus, one clock: UX audit W10)
        self.times.setText(tr(w, "run.times", "Elapsed {elapsed}   ETA {eta}",
                              elapsed=fmt_duration(s.elapsed_s) if running else "-",
                              eta=fmt_duration(s.eta_s) if running else "-"))

    def _refresh_values(self, s: RunStatus, connected: bool) -> None:
        """The process values (greyed and marked when the link is lost)."""
        w = self.ws.window
        # process values
        dash = "-"
        u = U.current()
        gas = s.gas
        if s.gas_bar:
            gas = tr(w, "run.val.gas_bar", "{gas} {pressure}", gas=gas, pressure=u.fmt(s.gas_bar, "pressure", decimals=1))
        vals = {"speed": u.fmt(s.speed, "speed"),
                "power": tr(w, "run.val.power_pct", "{v} %", v=f"{s.power_pct:.0f}") if s.power_pct is not None
                else dash,
                "gas": gas or dash,
                "height": u.fmt(s.height_mm, "length", missing=dash),
                "x": u.fmt(s.x, "length"), "y": u.fmt(s.y, "length"), "z": u.fmt(s.z, "length")}
        # the link is lost: the last values the card reported, greyed and marked (W2)
        self.values.set_values(vals, stale=not connected and self.ws.ever_connected)

    def _refresh_buttons(self, s: RunStatus, ms: Any, job: bool) -> None:
        """The run buttons (the run bar's rules and reasons), the resume and "cut the rest"
        buttons."""
        w = self.ws.window
        # buttons (the same rules as the run bar)
        fault = s.kind == "fault"
        paused = ms is not None and ms.mode == "pause"
        b = self.buttons
        # the run bar's rules and reasons (safety_ui.run_enabled); on this screen Start is the
        # Continue of a held job, so Hold is off while held
        from ..safety_ui import run_enabled
        st = w.ctl.safety()
        for k, on, why in run_enabled(w, st, ms, job):
            if k == "hold" and on and paused:
                on, why = False, tr(w, "run.why.held", "Held: Start continues the job")
            style.set_enabled(b[k], on, why)
        tr_obj = getattr(w, "tr_", None)
        cont = paused or self.ws.window.ctl.breakpoint is not None
        for k, text in (("start", style.run_text("start", tr_obj, alt=cont)), ("arm", style.run_text("arm", tr_obj, alt=s.armed))):
            if b[k].text() != text:
                b[k].setText(text)
                b[k].setAccessibleName(text)
        bp = self.ws.window.ctl.breakpoint is not None
        why_bp = (tr(w, "run.why.no_breakpoint", "No job is held at a breakpoint") if not bp else
                  tr(w, "run.why.fault", "Reset the fault first") if fault or s.alarms else "")
        style.set_enabled(b["resume_here"], not why_bp, why_bp)
        style.set_enabled(b["resume_contour"], not why_bp, why_bp)
        self._rest_buttons(b["start"].isEnabled(), str(b["start"].property("disabled_reason") or ""))

    def _refresh_queue(self, job: bool, connected: bool, fault: bool) -> None:
        """The queue card: its title and message, "Load this sheet", "Sheet loaded: start"."""
        w = self.ws.window
        b = self.buttons
        # queue card
        r = self.ws.runner
        title = trn(w, "run.queue.title", len(self.ws.queue),
                    left=trn(w, "run.queue.left", self.ws.queue.totals()["remaining"]))
        if self.queue_section.title() != title:
            self.queue_section.setTitle(title)
        lines = [_msg(w, r.message)]
        if r.current is not None and r.state != "idle":
            cur = r.current
            on = cur.remnant_id or cur.sheet
            nxt = dict(name=cur.name, n=min(cur.done + 1, cur.quantity), total=cur.quantity)
            lines.append(tr(w, "run.queue.next_on", "Next: {name} (sheet {n}/{total}, {sheet})", sheet=on, **nxt)
                         if on else tr(w, "run.queue.next", "Next: {name} (sheet {n}/{total})", **nxt))
        n_pending = len(self.ws.intake.pending)
        if n_pending:
            lines.append(trn(w, "run.queue.pending", n_pending))
        msg = "\n".join(x for x in lines if x)
        self.queue_msg.setText(msg)
        waiting = r.state == "confirm" and r.current is not None and not job
        if waiting:
            ent = r.current
            doc = self.ws.entry_document(ent)
            from mlaser.services.material_check import load_card
            self.load_card.show_card(load_card(ent, doc), doc, (ent.id, ent.done, ent.path, id(doc)), ent)
        if waiting != self.load_card.isVisibleTo(self):
            self.load_card.setVisible(waiting)
            # while the card asks for the sheet, the live view below it (the same sheet) may be
            # shorter: card and view fit a short screen beside the run controls
            self.canvas.setMinimumHeight(RunCanvas.MIN_H_WAITING if waiting else RunCanvas.MIN_H)
        asking = r.state in ("confirm", "starting")
        b["confirm_sheet"].setVisible(asking)
        prio = 10 if asking else 1             # the operator must see "Sheet loaded: start"
        if self.queue_section.priority != prio:
            self.queue_section.priority = prio
            if asking:
                self.queue_section.user_choice = None
            self.controls.schedule()
        style.set_enabled(b["confirm_sheet"], r.state == "confirm" and connected and not fault and not job,
                          tr(w, "run.why.no_sheet_wait", "The queue is not waiting for a sheet"))
        b["queue_mode"].setText(tr(w, "run.btn.queue_leave", "Leave queue") if r.state != "idle"
                                else tr(w, "run.btn.queue_run", "Run queue"))
        style.set_enabled(b["queue_mode"], r.state != "idle" or self.ws.queue.next_entry() is not None,
                          tr(w, "run.why.queue_empty", "The queue has no job left to run"))

    def _refresh_chips(self) -> None:
        """The LAN monitor and automation API chips."""
        w = self.ws.window
        # monitor chip
        mon = self.ws.monitor
        self.monitor_chip.setVisible(mon is not None and mon.running)
        if mon is not None and mon.running:
            h, port = mon.address
            self.monitor_chip.setText(tr(w, "run.monitor_on", "LAN monitor ON {host}:{port}", host=h, port=port))
        hooks = getattr(w, "hooks", None)
        api_fn = hooks.provider("automation.server") if hooks is not None else None
        api = api_fn() if api_fn is not None else None
        on = api is not None and api.running
        self.api_chip.setVisible(on)
        if on:
            h, port = api.address
            self.api_chip.setText(tr(w, "run.api_on", "Automation API ON {host}:{port}", host=h, port=port))

    def _refresh_canvas(self, s: RunStatus, running: bool, connected: bool) -> None:
        """The live view: the sheet, the progress or, after a job, the parts left."""
        # canvas
        doc, sheet = self.ws.view_document()
        self.canvas.set_document(doc, sheet)
        rest = None if running else self._rest_view(doc)
        if rest is None:
            if self.canvas.numbers or self.canvas.skip:
                self.canvas.set_rest((), (), {})
            self.canvas.set_progress(s.done_parts if running else (), s.part_index if running else -1,
                                     (s.x, s.y) if connected else None)
        else:
            self.canvas.set_progress(rest[0], -1, (s.x, s.y) if connected else None)

    # ------------------------------------------------------------------ the last job (UX deep review 5)
    def _last_job(self, running: bool) -> None:
        """The last job's line in the state block and the "Cut the rest" section."""
        w = self.ws.window
        last, why = self.ws.rest_state()
        any_last = self.ws.parts.last
        text = ""
        if any_last is not None and any_last.over and not running:
            text = self.ws.last_job_text(any_last)
            if why and last is not None:
                text = tr(w, "run.last.with_reason", "{summary}\n{reason}", summary=text, reason=why)
        if self.last_job.text() != text:
            self.last_job.setText(text)
        if bool(text) != self.last_job.isVisibleTo(self):
            self.last_job.setVisible(bool(text))
        show = last is not None and not running
        key = (id(last), last.outcome, last.done, last.total, why) if last is not None else None
        if key != self._rest_key:
            self._rest_key = key
            if last is not None:
                self.from_spin.blockSignals(True)
                self.from_spin.setRange(1, max(1, last.total))
                self.from_spin.setValue(last.done + 1 if last.done < last.total else 1)
                self.from_spin.blockSignals(False)
                self._from_chosen = False
                # open after a stop: the operator decides what happens to the rest
                prio = 5 if last.outcome == "stopped" and not why else 0
                if self.rest_section.priority != prio:
                    self.rest_section.priority = prio
                    if prio:
                        self.rest_section.user_choice = None
                    self.controls.schedule()
        if show != self.rest_section.isVisibleTo(self.controls):
            self.rest_section.setVisible(show)
            self.controls.schedule()
        # one of the two ways on: after a job with no breakpoint held there is nothing to
        # resume, and "Cut the rest" takes its place (its hint names the difference); with a
        # breakpoint, Resume is shown (and "Cut the rest" says to resume or stop first)
        resume = not show or w.ctl.breakpoint is not None
        if resume != self.resume_section.isVisibleTo(self.controls):
            self.resume_section.setVisible(resume)
            self.controls.schedule()

    def _rest_buttons(self, start_ok: bool, start_why: str) -> None:
        """"Cut remaining parts" / "Start from part": Start's own rules (connected, no alarm, no
        job running), then the offer's (the same drawing, no job held at a breakpoint)."""
        w = self.ws.window
        last, why = self.ws.rest_state()
        b = self.buttons
        n_left = len(last.remaining()) if last is not None else 0
        text = (trn(w, "run.btn.cut_remaining", n_left) if n_left
                else tr(w, "run.btn.cut_remaining_none", "Cut remaining parts"))
        if b["cut_remaining"].text() != text:
            b["cut_remaining"].setText(text)
            b["cut_remaining"].setAccessibleName(text)
        text = tr(w, "run.btn.start_from", "Start from part {n}", n=self.from_spin.value())
        if b["start_from"].text() != text:
            b["start_from"].setText(text)
            b["start_from"].setAccessibleName(text)
        why_left = why or (tr(w, "run.why.rest_none", "The last job finished every part") if not n_left else "")
        style.set_enabled(b["cut_remaining"], start_ok and not why_left, why_left or start_why)
        style.set_enabled(b["start_from"], start_ok and not why, why or start_why)
        style.set_enabled(self.from_spin, not why, why)

    def _rest_view(self, doc: Any) -> Optional[tuple]:
        """The live view after a job: (done, left out, numbers) as part indices of ``doc`` (the
        view's document; set on the canvas), or None when there is no offer to show."""
        last, why = self.ws.rest_state()
        if last is None or why:
            return None
        wparts = list(getattr(self.ws.window.document, "parts", []) or [])
        where = {getattr(p, "uid", None): i for i, p in enumerate(getattr(doc, "parts", []) or [])}

        def view(indices) -> list[int]:
            out = []
            for i in indices:
                j = where.get(getattr(wparts[i], "uid", None)) if 0 <= i < len(wparts) else None
                if j is not None:
                    out.append(j)
            return out
        # the last job as it ended (its finished parts dashed, the rest to cut); once the
        # operator chose a part to start from, the job that would run
        todo = set(last.from_part(self.from_spin.value()) if self._from_chosen else last.remaining())
        done = [i for i in last.done_parts() if i not in todo]
        numbers = dict(zip(view(last.order), range(1, last.total + 1)))
        if len(numbers) != last.total:
            return None                                 # the view shows another document
        keep = set(view(todo)) | set(view(done))
        skip = [j for j in range(len(getattr(doc, "parts", []) or [])) if j not in keep]
        out = (view(done), skip, numbers)
        self.canvas.set_rest(*out)
        return out

    def _from_changed(self, _v: int = 0) -> None:
        self._from_chosen = True
        self._rest_refresh()

    def _rest_refresh(self) -> None:
        """The spin box moved: the live view's preview and the button's number."""
        self._rest_buttons(self.buttons["start"].isEnabled(),
                           str(self.buttons["start"].property("disabled_reason") or ""))
        doc, _sheet = self.ws.view_document()
        self._rest_view(doc)

    #: the readiness items shown on this screen (the others have their own display)
    READY_IDS = ("homed", "technology")
    #: the readiness fixes' button texts (their command ids, services.readiness)
    READY_ACTIONS = {"machine.home": ("run.ready.do_home", "Home X/Y"),
                     "workspace.process": ("run.ready.do_technology", "Assign technology...")}

    def _readiness(self, job: bool) -> None:
        self._ready_tick += 1
        if self._ready_tick % 4 != 1 and not job:        # a few times a second is enough
            return
        ui = getattr(self.ws.window, "safety_ui", None)
        items = []
        if ui is not None and not job and hasattr(ui, "readiness"):
            try:
                items = [i for i in ui.readiness() if i.id in self.READY_IDS and not i.ok]
            except Exception:  # noqa: BLE001 - a display never fails the screen
                log.debug("readiness failed", exc_info=True)
        shown = {i.id for i in items}
        for rid, box in list(self.ready_notices.items()):
            if rid not in shown:
                box.setVisible(False)
        for i in items:
            b = self.ready_buttons.get(i.id)
            if b is None:
                # a notice with its action (UI deep review 13): the text on the warning (or
                # information) chip, its fix as an ordinary button beside it; never a full-width
                # button that reads as the action itself
                box = QWidget()
                box.setObjectName(f"run_ready_notice_{i.id}")
                bl = style.hbox(box, sp=style.SP2)
                lab = QLabel()
                lab.setObjectName(f"run_ready_text_{i.id}")
                lab.setWordWrap(True)
                style.chip(lab, "warning" if i.blocking else "info")
                bl.addWidget(lab, 1)
                b = _big(QPushButton(), TOUCH_MIN, role="body")
                b.setObjectName(f"run_ready_{i.id}")
                b.setIcon(icon("alert-triangle" if i.blocking else "info-circle"))
                b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                b.clicked.connect(lambda _=False, rid=i.id: self._ready_fix(rid))
                bl.addWidget(b, 0, Qt.AlignVCenter)
                box.label = lab                                   # type: ignore[attr-defined]
                self.ready_flow.addWidget(box)
                self.ready_buttons[i.id] = b
                self.ready_notices[i.id] = box
            box = self.ready_notices[i.id]
            text = i.reason or i.label
            if box.label.text() != text:
                box.label.setText(text)
                b.setAccessibleDescription(text)
            key, default = self.READY_ACTIONS.get(i.action, ("run.ready.do", "Fix..."))
            act = tr(self.ws.window, key, default)
            if b.text() != act:
                b.setText(act)
                b.setAccessibleName(act)
            b.setProperty("action", i.action)
            b.setVisible(bool(i.action))
            box.setVisible(True)
        if bool(items) != self.ready_box.isVisibleTo(self):
            self.ready_box.setVisible(bool(items))

    def _ready_fix(self, rid: str) -> None:
        b = self.ready_buttons.get(rid)
        ui = getattr(self.ws.window, "safety_ui", None)
        if b is not None and ui is not None and b.property("action"):
            ui.run_readiness_action(str(b.property("action")))

    def _alarms(self, s: RunStatus) -> None:
        """The alarm or fault, once, in the state block: the reason line is its title, the
        remedy below it, Reset alarm and Details (the full list) beside it: never below the
        fold (UX audit W11)."""
        w = self.ws.window
        ctl = w.ctl
        st = ctl.safety()
        alarms = list(ctl.active_alarms or [])
        tr_obj = getattr(w, "tr_", None)
        rem = ""
        if st.banner == "FAULT":
            rem = remedy(st.fault, tr_obj)
        elif alarms:
            rem = remedy(alarms[0], tr_obj)
            if len(alarms) > 1:
                rem = tr(w, "run.alarm.more", "{remedy}\n(+{n} more: see Details)", remedy=rem, n=len(alarms) - 1)
        show = st.banner == "FAULT" or bool(alarms)
        text = tr(w, "safety.alarm.remedy", "Remedy: {text}", text=rem) if show else ""
        if self.remedy.text() != text:
            self.remedy.setText(text)
        for x in (self.remedy, self.reset_btn, self.details_btn):
            if x.isVisibleTo(self) != show:
                x.setVisible(show)
        if show:
            self.reset_btn.setEnabled(True)
        style.set_enabled(self.details_btn, bool(alarms) or st.banner == "FAULT", "")


# ============================================================================== workspace
class RunWorkspace:
    """Plugin object: owns the queue, the runner, the remnant store, the monitor server and the
    operator screen."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.window = ctx.window
        self.settings = ctx.settings_schema([
            Setting("remnant_auto", "bool", True, label="run.set.remnant_auto"),
            Setting("remnant_min_width", "float", 50.0, unit="mm", min=1.0, max=5000.0,
                    label="run.set.remnant_min_width"),
            Setting("remnant_min_area", "float", 40000.0, unit="mm²", min=1.0, max=1e8,
                    label="run.set.remnant_min_area"),
            Setting("monitor_enabled", "bool", False, label="run.set.monitor_enabled"),
            Setting("monitor_host", "str", "127.0.0.1", label="run.set.monitor_host"),
            Setting("monitor_port", "int", 8765, min=1024, max=65535, label="run.set.monitor_port"),
            Setting("monitor_token", "str", "", label="run.set.monitor_token"),
        ])
        d = self.data_dir()
        self.queue = JobQueue(os.path.join(d, "queue.json"))
        if self.queue.load_error:
            self.log(f"queue: {self.queue.load_error} (the file was kept as queue.json.bad)")
        self.remnants = RemnantStore(os.path.join(d, "remnants.json"))
        # jobs from the automation API / CLI: pending until the operator approves them here
        self.intake = IntakeStore(d)
        self.intake.scan()
        self.runner = QueueRunner(self.queue, load=self._load_entry, start=self._start_sheet,
                                  start_allowed=self._start_allowed,
                                  disarm=lambda why: self.window.ctl.disarm(why),
                                  keep_armed=lambda on: self.window.ctl.keep_armed_after_next_job(on))
        self.runner.sheet_listeners.append(self._sheet_done)
        self.monitor: Any = None
        self._svg_cache: Any = None          # mlaser.web.snapshot.SheetSvgCache (monitor on)
        self._svg_published: Any = None
        self._job_t0 = 0.0
        self._run_plan: Any = None
        self._sheet_doc: Any = None
        self._entry_docs: dict = {}          # path -> (mtime, document): the next sheet's picture
        self.alarm_since: dict = {}
        self.ever_connected = False          # a lost link shows the last values as "last known"
        self._unsub: list = []
        self._attached: Any = None
        # the last job's parts: done / not done, where it stopped (UX deep review 5)
        self.parts = JobPartsTracker(self._job_context)
        self._doc_rev = 0
        self._key_cache: tuple = (None, "")
        docs = getattr(self.window, "docs", None)
        if docs is not None and hasattr(docs, "subscribe"):
            docs.subscribe(self._doc_changed)
        self.screen = OperatorScreen(self)
        ctx.add_workspace("run", self.screen, tr(self.window, "ws.run", "Run"))
        self.queue.subscribe(self._queue_changed)
        self.runner.subscribe(self._queue_changed)
        self.intake.subscribe(self._queue_changed)
        ctx.provide("queue", lambda: self.queue)
        ctx.provide("intake", lambda: self.intake)
        ctx.provide("remnants", lambda: self.remnants)
        ctx.provide("queue_runner", lambda: self.runner)
        ctx.provide("run_status", self.status)     # the run bar's figures (safety_ui.run_status)
        ctx.on_controller_changed(lambda _c: self.attach())
        ctx.on_close(self.close)
        self.attach()
        self.tick_timer = QTimer(self.screen)
        self.tick_timer.setInterval(500)
        self.tick_timer.timeout.connect(self._timer_tick)
        self.tick_timer.start()
        self._ticks = 0
        # the inbox: watched (QFileSystemWatcher) instead of listed on the GUI thread every
        # 500 ms (UI evaluation F12); polled as before where the folder cannot be watched
        self._inbox_watch = None
        self._inbox_timer = QTimer(self.screen)
        self._inbox_timer.setSingleShot(True)
        self._inbox_timer.setInterval(100)               # a burst of file events: one scan
        self._inbox_timer.timeout.connect(self.scan_inbox)
        self._watch_inbox()
        pause = getattr(self.window, "idle_pause", None)
        if pause is not None:
            # minimised: the tick runs on only for the LAN monitor and a pending start request
            pause.add(self.tick_timer, keep=self._tick_needed, on_resume=self.tick)
        if bool(self.settings["monitor_enabled"]):
            QTimer.singleShot(0, lambda: self.set_monitor(True, confirm=False))

    # ------------------------------------------------------------------ helpers
    def data_dir(self) -> str:
        s = getattr(self.window, "settings", None)
        d = s.get("data_dir") if s is not None else None
        if not d:
            from mlaser.services.config import data_dir
            d = str(data_dir())
        return os.path.join(d, "production")

    def log(self, msg: str) -> None:
        self.ctx.log(msg)

    def machine_state(self) -> Any:
        try:
            return self.window.ctl.state()
        except Exception:  # noqa: BLE001 - cannot read the state: shown as offline, run controls off
            return None

    def job_running(self) -> bool:
        from ..safety_ui import job_running
        return job_running(self.window)

    def job_name(self) -> str:
        ui = getattr(self.window, "safety_ui", None)
        if ui is not None:           # one text source with the run bar's "Start: ..." (UX 13)
            return ui.start_target_name()
        path = getattr(self.window, "path", None)
        return os.path.basename(path) if path else tr(self.window, "run.untitled", "(untitled)")

    def attach(self) -> None:
        ctl = self.window.ctl
        if ctl is self._attached:
            return
        for un in self._unsub:
            un()
        self._attached = ctl
        bus = ctl.events
        self._unsub = [bus.subscribe(EV.JobStarted, self._job_started)]
        self.runner.attach(bus)
        self.parts.attach(bus)

    def _job_started(self, ev: Any) -> None:
        if not ev.resume:
            self._job_t0 = time.time()
        run = getattr(self.window.jobs, "last_run", None)
        plan = getattr(run, "plan", None) if run is not None else None
        if plan is None or ev.resume:
            plan = plan or getattr(self.window, "plan", None)
        if not ev.resume or self._run_plan is None:
            self._run_plan = plan

    # ------------------------------------------------------------------ the last job (UX deep review 5)
    def _job_context(self) -> tuple[Any, Any, str]:
        """For the part tracker when a job starts: the plan being run, the document it was
        started from, its name."""
        run = getattr(getattr(self.window, "jobs", None), "last_run", None)
        plan = getattr(run, "plan", None) if run is not None else None
        return plan, self.window.document, self.job_name()

    def _doc_changed(self, _kind: str = "") -> None:
        self._doc_rev += 1

    def document_key(self) -> str:
        """:func:`mlaser.nc.resume.document_key` of the open document (the breakpoint's
        "is it the same drawing" test), computed again only after an edit, another document or
        a new plan (a layer's technology changed)."""
        doc = self.window.document
        k = (id(doc), id(getattr(self.window, "plan", None)), self._doc_rev)
        if self._key_cache[0] != k:
            from mlaser.nc.resume import document_key
            try:
                key = document_key(doc)
            except Exception:  # noqa: BLE001 - unknown: the offer is withdrawn
                key = ""
            self._key_cache = (k, key)
        return self._key_cache[1]

    def rest_state(self) -> tuple[Optional[LastJob], str]:
        """The last cutting job when it is over (None: none), and why "Cut remaining parts" /
        "Start from part" are not offered for it ("" = offered)."""
        w = self.window
        held = w.ctl.breakpoint is not None or self.job_running()
        self.parts.settle(held)
        last = self.parts.last
        if last is None or last.mode != "process" or not last.over:
            return None, ""
        if not last.mapped or not last.doc_key:
            return last, tr(w, "run.why.rest_unknown", "The parts of the last job are not known")
        if self.document_key() != last.doc_key:
            return last, tr(w, "run.why.rest_changed", "The drawing changed since the last job: "
                            "cutting its rest is no longer offered")
        if held:
            return last, tr(w, "run.why.rest_held", "A job is held at a breakpoint: resume it or stop it first")
        return last, ""

    def last_job_text(self, last: LastJob) -> str:
        """"Last job stopped at 14:02 in part 5 of 6 (4 finished), after 2:15 · gear.dxf"."""
        from PySide6.QtCore import QDateTime, QLocale
        w = self.window
        at = QLocale().toString(QDateTime.fromSecsSinceEpoch(int(last.ended or time.time())).time(),
                                QLocale.ShortFormat)
        args = dict(time=at, duration=fmt_duration(last.duration_s), name=last.name or self.job_name(),
                    total=last.total)
        dry = last.mode != "process"
        if last.outcome == "finished":
            if dry:
                return tr(w, "run.last.dry_finished", "Last dry run finished at {time}, after {duration} · {name}",
                          **args)
            return trn(w, "run.last.finished", last.total, **args)
        part = last.stopped_at or min(last.done + 1, last.total)
        if dry:
            return tr(w, "run.last.dry_stopped", "Last dry run stopped at {time} in part {part} of {total} · {name}",
                      part=part, **args)
        return tr(w, "run.last.stopped", "Last job stopped at {time} in part {part} of {total} ({done} finished), "
                  "after {duration} · {name}", part=part, done=last.done, **args)

    def cut_rest(self, from_part: Optional[int] = None) -> bool:
        """A new job of the last job's parts it did not finish (``from_part`` None), or from its
        cut-order number ``from_part`` to the end: those parts are selected (visibly, as a click
        on them would) and the ordinary "Start selection only" starts them, with its pre-flight,
        arming question and compile.  False when the offer is withdrawn (said in the status bar)."""
        w = self.window
        self._key_cache = (None, "")              # the exact test, not the cached one
        last, why = self.rest_state()
        parts = [] if last is None else (last.remaining() if from_part is None else last.from_part(from_part))
        if last is None or why or not parts:
            msg = why or tr(w, "run.why.rest_none", "The last job finished every part")
            if hasattr(w, "statusBar"):
                w.statusBar().showMessage(msg, 8000)
            self.log(msg)
            return False
        canvas = getattr(w, "canvas", None)
        if canvas is not None and getattr(canvas, "document", None) is w.document:
            canvas.select_parts(parts)
        else:
            keep = set(parts)
            for i, p in enumerate(w.document.parts):
                p.selected = i in keep
        ui = getattr(w, "safety_ui", None)
        if ui is None:
            return False
        ui.start_selection()
        return True

    def status(self) -> RunStatus:
        ctl = self.window.ctl
        ms = self.machine_state()
        st = ctl.safety()
        alarms = list(ctl.active_alarms or [])
        now = time.time()
        codes = {getattr(a, "code", str(a)) for a in alarms}
        for c in codes:
            self.alarm_since.setdefault(c, now)
        for c in [c for c in self.alarm_since if c not in codes]:
            del self.alarm_since[c]
        if ms is not None and ms.connected:
            self.ever_connected = True
        running = ms is not None and (ms.mode in ("process", "dry_run", "frame", "pause") or ctl.breakpoint is not None)
        plan = self._run_plan if running and self._run_plan is not None else getattr(self.window, "plan", None)
        elapsed = now - self._job_t0 if running and self._job_t0 else 0.0
        # the safety UI's clock: the run bar and this screen show the same elapsed time and the
        # whole job's progress (it never goes backwards after Continue: UX audit W10)
        clock = getattr(getattr(self.window, "safety_ui", None), "clock", None)
        if ms is not None and ms.mode in ("process", "dry_run") and int(getattr(ms, "job_index", -1)) >= 0:
            # the marker the card executes, polled: the part tracker's events are published at
            # most every 0.1 s (a part finished just before a Stop still counts)
            self.parts.progress(ms.mode, int(ms.job_index))
        return run_status(ms, st, alarms, plan, elapsed, ctl.breakpoint, clock=clock)

    def reason_text(self, s: RunStatus) -> str:
        default = REASONS.get(s.reason, s.reason)
        args = dict(s.reason_args)
        if s.reason == "run.reason.breakpoint":
            u = U.current()
            args["position"] = u.fmt_xy(float(args.get("x", 0.0)), float(args.get("y", 0.0)), decimals=1
                                        if not u.imperial else 2).replace("  Y", ", Y")
        return tr(self.window, s.reason, default, **args)

    def view_document(self) -> tuple[Any, Optional[tuple]]:
        """The document the live view shows: the running plan's placed document, else the
        preview plan's, else the open document."""
        ms = self.machine_state()
        running = ms is not None and ms.mode in ("process", "dry_run", "frame", "pause")
        plan = self._run_plan if running and self._run_plan is not None else getattr(self.window, "plan", None)
        doc = getattr(plan, "document", None) if plan is not None else None
        ent = self.runner.current
        if not running and self.runner.state == "confirm" and ent is not None:
            nxt = self.entry_document(ent)          # the sheet to load, not the previous drawing
            if nxt is not None:
                doc = nxt
        if doc is None:
            doc = self.window.document
        sheet = None
        if ent is not None and ent.extra.get("sheet_bounds"):
            b = ent.extra["sheet_bounds"]
            off = (doc.extra.get("job_offset") or (0.0, 0.0)) if hasattr(doc, "extra") else (0.0, 0.0)
            try:
                sheet = (b[0] + off[0], b[1] + off[1], b[2] + off[0], b[3] + off[1])
            except (TypeError, IndexError):
                sheet = None
        return doc, sheet

    # ------------------------------------------------------------------ queue wiring
    def _queue_changed(self) -> None:
        if self.screen.isVisible():
            self.screen.queue_panel.refresh()
            self.screen.refresh()

    def _start_allowed(self) -> tuple[bool, str]:
        diag = self.window.extension("ext_diag") if hasattr(self.window, "extension") else None
        svc = getattr(diag, "svc", None)
        if svc is not None:
            return svc.start_allowed("process")
        return True, ""

    def entry_document(self, e: QueueEntry) -> Any:
        """The entry's sheet document with the layer parameters it was queued with (cached per
        file modification time; None when it cannot be read).  For the load card and the live
        view while the queue waits; the copy that is cut is read again by :meth:`_load_entry`."""
        try:
            mt = os.path.getmtime(e.path)
        except OSError:
            return None
        hit = self._entry_docs.get(e.path)
        if hit is not None and hit[0] == mt:
            return hit[1]
        doc = self._read_entry(e)
        if len(self._entry_docs) > 8:
            self._entry_docs.clear()
        self._entry_docs[e.path] = (mt, doc)
        return doc

    @staticmethod
    def _read_entry(e: QueueEntry) -> Any:
        from ..ext_nest import _load_doc
        doc = _load_doc(e.path)
        if doc is None or not doc.parts:
            return None
        from mlaser.model import LayerParams
        for k, d in (e.extra.get("layers") or {}).items():    # the parameters it was queued with
            try:
                doc.layers.setdefault(int(k), LayerParams(**d))
            except (TypeError, ValueError):
                continue
        return doc

    def _load_entry(self, e: QueueEntry) -> bool:
        if self.job_running():
            e.note = tr(self.window, "queue.note.busy", "a job is running")
            return False
        if not os.path.exists(e.path):
            e.note = tr(self.window, "queue.note.missing", "file not found: {path}", path=e.path)
            return False
        doc = self._read_entry(e)
        if doc is None:
            e.note = tr(self.window, "queue.note.unreadable", "cannot read {path}", path=e.path)
            return False
        # the sheet it is cut from: the material check before Start (services.material_check)
        from mlaser.services.material_check import SheetSpec, set_sheet
        set_sheet(doc, SheetSpec(e.material, float(e.thickness_mm or 0.0), "queue", e.id, e.done + 1,
                                 e.sheet, e.remnant_id))
        base = self.window.document
        for k, lp in getattr(base, "layers", {}).items():     # keep layer parameters the file lacks
            if not lp.extra.get("placeholder"):
                doc.layers.setdefault(k, copy.deepcopy(lp))
        self.ctx.replace_document(doc, tr(self.window, "queue.undo.load", "queue: load sheet"), e.path)
        self._sheet_doc = self.window.document
        return True

    def _start_sheet(self) -> Optional[bool]:
        """Start the loaded sheet through the run bar's path (pre-flight, the one arming
        question, planned job).  The check runs off the GUI thread, as for Start (perf review
        2026-11-09, finding 10): None, and the runner learns later through
        ``QueueRunner.start_result`` whether the start reached the job service; True / False
        when that was decided at once.  A queue left meanwhile drops the start."""
        ui = getattr(self.window, "safety_ui", None)
        if ui is None:
            return False
        jobs = self.window.jobs
        before = getattr(jobs, "last_run", None)
        runner, entry = self.runner, self.runner.current
        box: dict = {}

        def handled() -> None:
            after = getattr(jobs, "last_run", None)
            started = after is not None and after is not before
            if "pending" in box:
                runner.start_result(started)
            else:
                box["started"] = started

        def wanted() -> bool:
            return runner.state == "starting" and runner.current is entry
        ui.start(then=handled, proceed=wanted)
        if "started" in box:
            return box["started"]
        box["pending"] = True
        return None

    def _sheet_done(self, e: QueueEntry, ok: bool) -> None:
        if not ok:
            return
        if e.remnant_id:
            try:
                self.remnants.mark_used(e.remnant_id, f"queue:{e.id}")
            except KeyError:
                pass
        rings = e.extra.get("sheet_rings")
        if not rings or not bool(self.settings["remnant_auto"]):
            return
        doc = self._sheet_doc or self.window.document
        try:
            from mlaser.nest.shapes import parts_geometry
            sep = e.extra.get("remnant_rings")
            if sep:     # a separation cut freed this remnant (nest cutting strategy): store it as cut
                poly = polygon_of(sep)
                min_area = float(self.settings["remnant_min_area"])
                made = [self.remnants.add_polygon(poly, e.material, e.thickness_mm,
                                                  source=f"{e.name} (queue:{e.id})")] \
                    if not poly.is_empty and poly.area >= min_area else []
                for r in made:
                    self.log(tr(self.window, "remnant.saved", "Remnant {label} saved", label=r.label()))
                    self.runner.message = tr(self.window, "remnant.saved", "Remnant {label} saved",
                                             label=r.label())
                return
            sheet = polygon_of(rings)
            # cut-up / separation lines (nest cutting strategy) are not parts
            used = parts_geometry([p for p in doc.parts if p.contours and not p.extra.get("strategy")])
            made = self.remnants.capture(sheet, used, e.material, e.thickness_mm,
                                         spacing=float(e.extra.get("spacing", 3.0)),
                                         gravity=str(e.extra.get("gravity", "left")),
                                         min_width=float(self.settings["remnant_min_width"]),
                                         min_area=float(self.settings["remnant_min_area"]),
                                         source=f"{e.name} (queue:{e.id})")
        except Exception as exc:  # noqa: BLE001 - a remnant must never stop the queue
            self.log(f"remnant: {exc}")
            return
        for r in made:
            self.log(tr(self.window, "remnant.saved", "Remnant {label} saved", label=r.label()))
            self.runner.message = tr(self.window, "remnant.saved", "Remnant {label} saved", label=r.label())

    def release_remnant(self, e: QueueEntry) -> None:
        if e.remnant_id:
            try:
                r = self.remnants.get(e.remnant_id)
                if r.status == "reserved":
                    self.remnants.release(r.id)
            except KeyError:
                pass

    def tick(self) -> None:
        """The runner's start timeout, the inbox and the LAN monitor (also called directly)."""
        self.runner.tick()
        self.scan_inbox()
        if self.monitor is not None and self.monitor.running:
            self.publish_monitor()

    #: timer ticks (500 ms) between two scans of a watched inbox (a missed file event)
    INBOX_RESCAN_TICKS = 60

    def _timer_tick(self) -> None:
        self._ticks += 1
        self.runner.tick()
        if self._inbox_watch is None or self._ticks % self.INBOX_RESCAN_TICKS == 0:
            self.scan_inbox()
        if self.monitor is not None and self.monitor.running:
            self.publish_monitor()
        pause = getattr(self.window, "idle_pause", None)
        if pause is not None:
            pause.check(self.tick_timer)

    def _tick_needed(self) -> bool:
        return bool((self.monitor is not None and self.monitor.running) or self.runner.state == "starting")

    def scan_inbox(self) -> None:
        try:
            for job in self.intake.scan():
                self.log(tr(self.window, "approval.msg.submitted", "Job submitted for approval: {name} ({src})",
                            name=job.name, src=f"{job.source} {job.client}".strip()))
        except OSError as exc:  # noqa: PERF203 - the inbox must never stop the Run screen
            log.debug("intake scan failed: %s", exc)

    def _watch_inbox(self) -> None:
        from PySide6.QtCore import QFileSystemWatcher
        try:
            os.makedirs(self.intake.inbox, exist_ok=True)
        except OSError:
            return
        w = QFileSystemWatcher(self.screen)
        if not w.addPath(str(self.intake.inbox)):
            w.deleteLater()
            return
        w.directoryChanged.connect(lambda _p: self._inbox_timer.start())
        self._inbox_watch = w
        self._inbox_timer.start()                        # what arrived while the app was closed

    # ------------------------------------------------------------------ LAN monitor
    def set_monitor(self, on: bool, confirm: bool = True, port: Optional[int] = None) -> bool:
        """Enable / disable the read-only LAN monitor.  Enabling shows a warning first.  ``port``
        overrides the setting for this start (0: any free port, read back from ``monitor.address``)."""
        from mlaser.web import MonitorServer, new_token
        w = self.window
        if not on:
            if self.monitor is not None:
                self.monitor.stop()
                self.monitor = None
                self._svg_cache = self._svg_published = None
                self.log(tr(w, "web.log.stopped", "LAN monitor stopped"))
            self.settings["monitor_enabled"] = False
            return False
        if self.monitor is not None and self.monitor.running:
            return True
        token = str(self.settings["monitor_token"] or "")
        if len(token) < 12:
            token = new_token()
            self.settings["monitor_token"] = token
        host = str(self.settings["monitor_host"] or "127.0.0.1")
        port = int(self.settings["monitor_port"]) if port is None else int(port)
        if confirm and not getattr(w, "_test_no_dialogs", False):
            text = tr(w, "web.warn.text",
                      "The LAN monitor serves a read-only status page (state, progress, alarms, the "
                      "current sheet) on {host}:{port} to anyone on the network who has the link with its "
                      "access token. It cannot control the machine. Share the link only with people who "
                      "may see production data, and keep it off on untrusted networks.\n\nEnable it?",
                      host=host, port=port)
            if QMessageBox.warning(w, tr(w, "web.warn.title", "Enable the LAN monitor"), text,
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return False
        try:
            self.monitor = MonitorServer(host, port, token).start()
        except (OSError, ValueError) as exc:
            self.monitor = None
            self.log(tr(w, "web.log.failed", "LAN monitor could not start on {host}:{port}: {err}",
                        host=host, port=port, err=exc))
            return False
        self.settings["monitor_enabled"] = True
        self.publish_monitor()
        self.log(tr(w, "web.log.started", "LAN monitor ON (read-only): {url}", url=self.monitor.url()))
        return True

    def monitor_snapshot(self) -> tuple[dict, str]:
        from mlaser.web.page import LABEL_IDS
        w = self.window
        s = self.status()
        key, default = STATE_TEXT[s.kind]
        tr_obj = getattr(w, "tr_", None)
        alarms = []
        st = w.ctl.safety()
        if st.banner == "FAULT":
            alarms.append({"text": tr(w, "run.monitor.fault", "FAULT: {fault}", fault=st.fault),
                           "remedy": remedy(st.fault, tr_obj)})
        for a in list(w.ctl.active_alarms or []):
            alarms.append({"text": tr(w, "run.monitor.alarm", "{code} {text}", code=getattr(a, "code", ""),
                                      text=a.text(tr_obj) if hasattr(a, "text") else a),
                           "remedy": remedy(a, tr_obj)})
        queue = []
        for e in self.queue:
            if e.status in ("done", "skipped"):
                continue
            st_key, st_def = STATUS_TEXT.get(e.status, (e.status, e.status))
            queue.append(tr(w, "run.monitor.queue_item", "{name}  {done}/{quantity}  {status}", name=e.name,
                            done=e.done, quantity=e.quantity, status=tr(w, st_key, st_def)))
        ent = self.runner.current if self.runner.state != "idle" else None
        doc, sheet = self.view_document()
        running = s.kind in ("running", "hold")
        # the sheet SVG is cached per part and rebuilt only when the document, the cut parts or
        # the current part change (performance review P1-7); the revision tells the page
        if self._svg_cache is None:
            from mlaser.web.snapshot import SheetSvgCache
            self._svg_cache = SheetSvgCache()
        svg = self._svg_cache.render(doc, s.done_parts if running else (), s.part_index if running else None,
                                     (s.x, s.y), sheet=sheet)
        rev = f"{id(doc)}:{len(s.done_parts)}:{s.part_index}:{self._svg_cache.builds}"
        snap = {
            "state": tr(w, key, default), "kind": s.kind, "icon": s.icon, "reason": self.reason_text(s),
            "progress": s.progress if running else 0.0, "elapsed_s": s.elapsed_s if running else None,
            "eta_s": s.eta_s if running else None, "job": self.job_name(),
            "sheet": f"{min(ent.done + 1, ent.quantity)}/{ent.quantity} {ent.name}" if ent else "",
            "speed": U.current().fmt(s.speed, "speed"),
            "power": f"{s.power_pct:.0f} %" if s.power_pct is not None else "-",
            "position": U.current().fmt_xy(s.x, s.y, decimals=1 if not U.current().imperial else 2),
            "alarms": alarms, "queue": queue[:20],
            "sheet_rev": rev, "lang": getattr(tr_obj, "language", "en"),
            "labels": {k: tr(w, k, "") for k in LABEL_IDS},
        }
        return snap, svg

    def publish_monitor(self) -> None:
        if self.monitor is None:
            return
        try:
            snap, svg = self.monitor_snapshot()
        except Exception as exc:  # noqa: BLE001 - the monitor must never disturb the UI
            log.debug("monitor snapshot failed: %s", exc)
            return
        # the server keeps the last SVG: hand it over only when it is a new one
        self.monitor.publish(snap, None if svg is self._svg_published else svg)
        self._svg_published = svg

    def close(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor = None
        self.tick_timer.stop()
        self._inbox_timer.stop()
        if self._inbox_watch is not None:
            self._inbox_watch.removePaths(self._inbox_watch.directories())


def register(ctx: Any) -> RunWorkspace:
    ws = RunWorkspace(ctx)
    title = tr(ctx.window, "ws.run", "Run")
    ctx.register_command("workspace.run", lambda: show_workspace(ctx, "run", ws.screen, title),
                         label="ws.run")
    ctx.register_command("queue.add_current", lambda: _queue_current(ws), label="queue.cmd.add_current")
    ctx.register_command("queue.confirm_sheet", ws.screen.confirm_sheet, label="run.btn.confirm_sheet")
    # after a stopped job (UX deep review 5): found by the command search as well
    ctx.register_command("run.cut_remaining", ws.screen.cut_remaining, label="run.cmd.cut_remaining")
    ctx.register_command("monitor.toggle", lambda on: ws.set_monitor(bool(on)), label="web.cmd.toggle",
                         checkable=True)
    return ws


def _queue_current(ws: RunWorkspace) -> Optional[QueueEntry]:
    """Add the open document to the queue (it must be saved: the queue keeps file paths)."""
    path = getattr(ws.window, "path", None)
    if not path or not os.path.exists(path):
        ws.log(tr(ws.window, "queue.note.save_first", "Save the document first: the queue runs files"))
        return None
    est = float((getattr(ws.window, "estimate", None) or {}).get("total", 0.0) or 0.0)
    return ws.queue.add(path, 1, est_s=est)


__all__ = ["register", "RunWorkspace", "OperatorScreen", "RunCanvas", "QueuePanel"]
