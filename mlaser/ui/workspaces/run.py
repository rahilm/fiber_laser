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
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from mlaser.machine import events as EV
from mlaser.services.config import Setting
from mlaser.services.intake import IntakeStore
from mlaser.services.operator import RunStatus, fmt_duration, run_status
from mlaser.services.queue import JobQueue, QueueEntry, QueueRunner
from mlaser.services.remnants import RemnantStore, polygon_of
from mlaser.services import units as U

from ..icons import icon
from ..safety_texts import remedy
from ..units_qt import on_units_changed
from . import TOUCH, TOUCH_MIN, show_workspace, tr

log = logging.getLogger("mlaser.ui.run")

STATE_COLORS = {            # background, text (colour is never the only cue: text + icon too)
    "offline": ("#616161", "#ffffff"),
    "ready": ("#1565c0", "#ffffff"),
    "running": ("#2e7d32", "#ffffff"),
    "hold": ("#ff8f00", "#000000"),
    "fault": ("#7f0000", "#ffffff"),
    "alarm": ("#b71c1c", "#ffffff"),
}
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


def _big(btn: QPushButton, h: int = TOUCH, w: int = 0, pt: float = 15.0, bold: bool = True) -> QPushButton:
    btn.setMinimumHeight(h)
    if w:
        btn.setMinimumWidth(w)
    f = btn.font()
    f.setPointSizeF(pt)
    f.setBold(bold)
    btn.setFont(f)
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


# ============================================================================== load card
class LoadSheetCard(QFrame):
    """"Load this sheet": shown while the queue waits for the next sheet.  Material, thickness,
    size, remnant id, sheet n/N in large type, a picture of the sheet with its nest (not the
    previous drawing), the technology and nozzle, the orders on it, and a red line when the
    sheet does not match the technology (Import & orders track,
    :func:`mlaser.services.material_check.load_card`)."""

    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window_ = window
        self.setObjectName("run_load_card")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("#run_load_card{border:2px solid #1565c0;border-radius:8px;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self.picture = QLabel()
        self.picture.setObjectName("run_load_picture")
        self.picture.setFixedSize(220, 140)
        self.picture.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.picture)
        col = QVBoxLayout()
        self.title = QLabel(tr(window, "run.load.title", "Load this sheet"))
        f = self.title.font()
        f.setBold(True)
        f.setPointSizeF(12)
        self.title.setFont(f)
        col.addWidget(self.title)
        self.material = QLabel()
        self.material.setObjectName("run_load_material")
        f = self.material.font()
        f.setBold(True)
        f.setPointSizeF(20)
        self.material.setFont(f)
        self.material.setWordWrap(True)
        col.addWidget(self.material)
        self.sheet = QLabel()
        self.sheet.setObjectName("run_load_sheet")
        f = self.sheet.font()
        f.setBold(True)
        f.setPointSizeF(15)
        self.sheet.setFont(f)
        self.sheet.setWordWrap(True)
        col.addWidget(self.sheet)
        self.details = QLabel()
        self.details.setObjectName("run_load_details")
        self.details.setWordWrap(True)
        col.addWidget(self.details)
        self.warning = QLabel()
        self.warning.setObjectName("run_load_warning")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("background:#b71c1c;color:#ffffff;padding:4px 8px;border-radius:4px;")
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
        self.picture.setPixmap(parts_pixmap(parts, QSize(216, 136), self.palette(), data.get("rings")))


# ============================================================================== canvas
class RunCanvas(QWidget):
    """Live view of the sheet: done parts grey, the current part bright and thick, the rest in
    their layer colour, the head as a red dot (Y up, fitted)."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(320, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName("sheet view")
        self.document: Any = None
        self.sheet: Optional[tuple[float, float, float, float]] = None
        self.done: set = set()
        self.current = -1
        self.head: Optional[tuple[float, float]] = None
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
        self.update()

    def set_progress(self, done, current: int, head: Optional[tuple[float, float]]) -> None:
        done = set(done)
        moved = head is not None and (self.head is None or math.dist(head, self.head) > 0.2)
        if done != self.done or current != self.current or moved or (head is None) != (self.head is None):
            self.done, self.current, self.head = done, current, head
            self.update()

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
        from ..canvas import layer_color
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
            done_pen = QPen(pal.color(QPalette.Mid), 0)
            cur_col = QColor("#ff6d00")
            for i, layer, path in self._paths:
                if i in self.done:
                    p.setPen(done_pen)
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
                p.setPen(QPen(QColor("#ffffff"), 1.5 / s))
                p.setBrush(QColor("#d50000"))
                p.drawEllipse(QPointF(*self.head), r, r)
        finally:
            p.end()


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
        g = QGridLayout()
        self.buttons: dict[str, QPushButton] = {}
        specs = (("add", "queue.btn.add", "Add job...", self.add_file),
                 ("up", "queue.btn.up", "Move up", lambda: self._move(-1)),
                 ("down", "queue.btn.down", "Move down", lambda: self._move(1)),
                 ("remove", "queue.btn.remove", "Remove", self.remove),
                 ("skip", "queue.btn.skip", "Skip / unskip", self.skip),
                 ("reset", "queue.btn.reset", "Run again", self.reset),
                 ("clear", "queue.btn.clear", "Clear finished", self.clear_finished))
        for n, (key, tid, text, fn) in enumerate(specs):
            b = _big(QPushButton(tr(w, tid, text)), TOUCH_MIN, pt=11.0, bold=False)
            b.setObjectName(f"queue_{key}")
            b.clicked.connect(lambda _=False, f=fn: f())
            g.addWidget(b, n // 4, n % 4)
            self.buttons[key] = b
        qty = QHBoxLayout()
        qty.addWidget(QLabel(tr(w, "queue.qty", "Quantity (sheets)")))
        self.qty = QSpinBox()
        self.qty.setObjectName("queue_qty")
        self.qty.setRange(1, 9999)
        self.qty.setMinimumHeight(TOUCH_MIN)
        self.qty.setMinimumWidth(110)
        self.qty.editingFinished.connect(self._qty_changed)
        qty.addWidget(self.qty)
        g.addLayout(qty, 1, 3)
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
        self.totals.setText(tr(w, "queue.totals", "{entries} jobs · {done}/{sheets} sheets · {left} left",
                               entries=t["entries"], done=t["done"], sheets=t["sheets"],
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
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # --- header: state, reason, counters, progress, times
        head = QFrame()
        head.setObjectName("run_header")
        hl = QGridLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        self.state = QLabel()
        self.state.setObjectName("run_state_big")
        self.state.setAlignment(Qt.AlignCenter)
        self.state.setMinimumSize(220, 72)
        f = self.state.font()
        f.setPointSizeF(24)
        f.setBold(True)
        self.state.setFont(f)
        hl.addWidget(self.state, 0, 0, 2, 1)
        self.reason = QLabel()
        self.reason.setObjectName("run_reason")
        self.reason.setWordWrap(True)
        f = self.reason.font()
        f.setPointSizeF(13)
        self.reason.setFont(f)
        hl.addWidget(self.reason, 0, 1, 1, 2)
        self.counters = QLabel()
        self.counters.setObjectName("run_counters")
        f = self.counters.font()
        f.setPointSizeF(13)
        f.setBold(True)
        self.counters.setFont(f)
        hl.addWidget(self.counters, 1, 1)
        self.monitor_chip = QLabel()
        self.monitor_chip.setObjectName("run_monitor_chip")
        self.monitor_chip.setStyleSheet("background:#ff8f00;color:#000;padding:4px 8px;border-radius:4px;")
        self.monitor_chip.setVisible(False)
        # the automation API (mlaser.ui.ext_automation): read-only, queue intake needs approval
        self.api_chip = QLabel()
        self.api_chip.setObjectName("run_api_chip")
        self.api_chip.setStyleSheet("background:#ff8f00;color:#000;padding:4px 8px;border-radius:4px;")
        self.api_chip.setVisible(False)
        chips = QHBoxLayout()
        chips.addStretch(1)
        chips.addWidget(self.api_chip)
        chips.addWidget(self.monitor_chip)
        hl.addLayout(chips, 1, 2)
        self.progress = QProgressBar()
        self.progress.setObjectName("run_progress")
        self.progress.setRange(0, 1000)
        self.progress.setMinimumHeight(32)
        self.progress.setTextVisible(True)
        hl.addWidget(self.progress, 2, 0, 1, 2)
        self.times = QLabel()
        self.times.setObjectName("run_times")
        f = self.times.font()
        f.setPointSizeF(14)
        f.setBold(True)
        self.times.setFont(f)
        hl.addWidget(self.times, 2, 2)
        hl.setColumnStretch(1, 1)
        root.addWidget(head)
        # safety I/O that is not configured (ui/safety_io.py): "door interlock not configured ..."
        self.safety_notice = QLabel()
        self.safety_notice.setObjectName("run_safety_notice")
        self.safety_notice.setWordWrap(True)
        self.safety_notice.setStyleSheet("background:#ff8f00;color:#000;padding:4px 8px;border-radius:4px;")
        self.safety_notice.setVisible(False)
        root.addWidget(self.safety_notice)

        body = QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body, 1)

        # --- left: live view / queue tabs, process values, alarms
        left = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.setObjectName("run_tabs")
        self.tabs.tabBar().setMinimumHeight(TOUCH_MIN)
        self.tabs.setStyleSheet(f"QTabBar::tab{{min-height:{TOUCH_MIN - 8}px;min-width:120px;padding:4px 12px;}}")
        self.canvas = RunCanvas()
        self.tabs.addTab(self.canvas, tr(w, "run.tab.live", "Live view"))
        self.queue_panel = QueuePanel(ws)
        self.tabs.addTab(self.queue_panel, tr(w, "run.tab.queue", "Queue"))
        self.load_card = LoadSheetCard(w)          # "Load this sheet" (queue waiting for a sheet)
        self.load_card.setVisible(False)
        left.addWidget(self.load_card)
        left.addWidget(self.tabs, 1)
        self.values = QLabel()
        self.values.setObjectName("run_values")
        self.values.setTextFormat(Qt.PlainText)
        f = self.values.font()
        f.setPointSizeF(13)
        self.values.setFont(f)
        self.values.setWordWrap(True)
        left.addWidget(self.values)
        self.alarm_box = QFrame()
        self.alarm_box.setObjectName("run_alarms")
        self.alarm_box.setFrameShape(QFrame.StyledPanel)
        ab = QVBoxLayout(self.alarm_box)
        self.alarm_title = QLabel()
        self.alarm_title.setObjectName("run_alarm_title")
        f = self.alarm_title.font()
        f.setBold(True)
        f.setPointSizeF(13)
        self.alarm_title.setFont(f)
        self.alarm_title.setStyleSheet("color:#c62828;")
        ab.addWidget(self.alarm_title)
        self.alarm_list = QListWidget()
        self.alarm_list.setObjectName("run_alarm_list")
        self.alarm_list.setWordWrap(True)
        self.alarm_list.setMaximumHeight(150)
        ab.addWidget(self.alarm_list)
        arow = QHBoxLayout()
        self.reset_btn = _big(QPushButton(tr(w, "run.btn.reset", "Reset alarm")), TOUCH_MIN, pt=12)
        self.reset_btn.setObjectName("run_reset")
        self.reset_btn.clicked.connect(lambda: self._ui("reset_fault"))
        self.details_btn = _big(QPushButton(tr(w, "run.btn.details", "Details")), TOUCH_MIN, pt=12, bold=False)
        self.details_btn.setObjectName("run_details")
        self.details_btn.clicked.connect(lambda: self._ui("alarm_details"))
        arow.addWidget(self.reset_btn)
        arow.addWidget(self.details_btn)
        ab.addLayout(arow)
        left.addWidget(self.alarm_box)
        body.addLayout(left, 1)

        # --- right: the big targets
        right = QVBoxLayout()
        right.setSpacing(8)
        col = QWidget()
        col.setObjectName("run_controls_column")
        col.setLayout(right)
        col.setMinimumWidth(300)
        col.setMaximumWidth(380)
        self.buttons: dict[str, QPushButton] = {}

        def add(key: str, tid: str, text: str, fn, h: int = TOUCH, pt: float = 16.0, ico: str = "",
                color: str = "") -> QPushButton:
            b = _big(QPushButton(tr(w, tid, text)), h, pt=pt)
            b.setObjectName(f"run_{key}")
            if ico:
                b.setIcon(icon(ico, color or None))
                b.setIconSize(QSize(28, 28))
            b.clicked.connect(lambda _=False, f=fn: f())
            self.buttons[key] = b
            return b

        stop = add("stop", "run.btn.stop", "STOP", lambda: self._ui("stop"), int(TOUCH * 1.4), 22, "player-stop", "#ffffff")
        stop.setToolTip(tr(w, "run.tip.stop", "Stop the job (Esc or F12)"))
        stop.setStyleSheet("QPushButton{background:#c62828;color:#ffffff;border-radius:6px;}"
                           "QPushButton:pressed{background:#8e0000;}")
        right.addWidget(stop)
        hs = QHBoxLayout()
        hs.addWidget(add("hold", "run.btn.hold", "HOLD", lambda: self._ui("hold"), int(TOUCH * 1.2), 18,
                         "player-pause", "#ff8f00"))
        hs.addWidget(add("start", "run.btn.start", "START", lambda: self._ui("start"), int(TOUCH * 1.2), 18,
                         "player-play", "#2e7d32"))
        right.addLayout(hs)
        self.buttons["hold"].setToolTip(tr(w, "run.tip.hold", "Hold / continue (Space)"))
        self.buttons["start"].setToolTip(tr(w, "run.tip.start", "Start or continue (F5): pre-flight check first"))
        fd = QHBoxLayout()
        fd.addWidget(add("frame", "run.btn.frame", "Frame", lambda: self._ui("frame"), pt=13, ico="frame"))
        fd.addWidget(add("dry_run", "run.btn.dry_run", "Dry run", lambda: self._ui("dry_run"), pt=13, ico="route"))
        right.addLayout(fd)
        right.addWidget(add("arm", "run.btn.arm", "Arm laser", lambda: self._ui("toggle_arm"), pt=13, ico="bolt"))
        rl = QLabel(tr(w, "run.resume", "Resume from the breakpoint"))
        right.addWidget(rl)
        rs = QHBoxLayout()
        rs.addWidget(add("resume_here", "run.btn.resume_here", "Here", lambda: self.resume("exact"), TOUCH_MIN, 12,
                         "player-skip-forward"))
        rs.addWidget(add("resume_contour", "run.btn.resume_contour", "Contour start",
                         lambda: self.resume("contour"), TOUCH_MIN, 12, "restore"))
        right.addLayout(rs)
        # queue card
        card = QFrame()
        card.setObjectName("run_queue_card")
        card.setFrameShape(QFrame.StyledPanel)
        cv = QVBoxLayout(card)
        self.queue_title = QLabel()
        f = self.queue_title.font()
        f.setBold(True)
        self.queue_title.setFont(f)
        cv.addWidget(self.queue_title)
        self.queue_msg = QLabel()
        self.queue_msg.setObjectName("run_queue_msg")
        self.queue_msg.setWordWrap(True)
        cv.addWidget(self.queue_msg)
        cv.addWidget(add("confirm_sheet", "run.btn.confirm_sheet", "Sheet loaded: start",
                         self.confirm_sheet, TOUCH, 14, "truck-loading"))
        cv.addWidget(add("queue_mode", "run.btn.queue_run", "Run queue", self.toggle_queue, TOUCH_MIN, 12,
                         "list-numbers"))
        right.addWidget(card)
        right.addStretch(1)
        body.addWidget(col)

        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.refresh)
        self._kind = ""
        self.status = RunStatus()

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
        w = self.ws.window
        s = self.ws.status()
        self.status = s
        # state
        key, default = STATE_TEXT[s.kind]
        label = f"{s.icon}  {tr(w, key, default)}"
        if self.state.text() != label:
            self.state.setText(label)
        if s.kind != self._kind:
            bg, fg = STATE_COLORS[s.kind]
            self.state.setStyleSheet(f"background:{bg};color:{fg};border-radius:8px;padding:6px 14px;")
            self._kind = s.kind
        self.reason.setText(self.ws.reason_text(s))
        b = getattr(w, "safety_io", None)
        note = b.notice_text() if b is not None else ""
        if note != self.safety_notice.text():
            self.safety_notice.setText(note)
        if bool(note) != self.safety_notice.isVisibleTo(self):
            self.safety_notice.setVisible(bool(note))
        # counters
        bits = []
        ent = self.ws.runner.current
        if ent is not None and self.ws.runner.state != "idle":
            bits.append(tr(w, "run.count.sheet", "Sheet {n}/{total}", n=min(ent.done + 1, ent.quantity),
                           total=ent.quantity) + f" · {ent.name}")
        if s.parts:
            bits.append(tr(w, "run.count.part", "part {n}/{total}", n=s.part, total=s.parts))
        if s.contours:
            bits.append(tr(w, "run.count.contour", "contour {n}/{total}", n=s.contour, total=s.contours))
        self.counters.setText("  ·  ".join(bits) or self.ws.job_name())
        running = s.kind in ("running", "hold") or s.mode != ""
        p = s.progress if running else 0.0
        self.progress.setValue(int(round(p * 1000)))
        self.progress.setFormat(f"{p * 100:.0f} %" if running else "")
        self.times.setText(tr(w, "run.times", "elapsed {elapsed}   ETA {eta}",
                              elapsed=fmt_duration(s.elapsed_s) if running else "-",
                              eta=fmt_duration(s.eta_s) if running else "-"))
        # process values
        dash = "-"
        u = U.current()
        vals = [tr(w, "run.val.speed", "Speed {v}", v=u.fmt(s.speed, "speed")),
                tr(w, "run.val.power", "Power {v} %", v=f"{s.power_pct:.0f}" if s.power_pct is not None else dash),
                tr(w, "run.val.gas", "Gas {v}", v=(s.gas + (f" {u.fmt(s.gas_bar, 'pressure', decimals=1)}"
                                                            if s.gas_bar else "")) or dash),
                tr(w, "run.val.height", "Height {v}", v=u.fmt(s.height_mm, "length", missing=dash)),
                tr(w, "run.val.position", "X {x}  Y {y}  Z {z} {unit}", x=u.number(s.x, "length"),
                   y=u.number(s.y, "length"), z=u.number(s.z, "length"), unit=u.suffix("length"))]
        self.values.setText("    ".join(vals))
        # alarms
        self._alarms(s)
        # buttons (the same rules as the run bar)
        ms = self.ws.machine_state()
        connected = bool(ms is not None and ms.connected)
        fault = s.kind == "fault"
        job = self.ws.job_running()
        paused = ms is not None and ms.mode == "pause"
        b = self.buttons
        b["start"].setEnabled(connected and not fault and (not job or paused))
        b["start"].setText(tr(w, "run.btn.continue", "CONTINUE") if paused or self.ws.window.ctl.breakpoint is not None
                           else tr(w, "run.btn.start", "START"))
        b["hold"].setEnabled(connected and job and not paused)
        b["stop"].setEnabled(True)
        b["frame"].setEnabled(connected and not fault and not job)
        b["dry_run"].setEnabled(connected and not fault and not job)
        armed = s.armed
        b["arm"].setText(tr(w, "run.btn.disarm", "Disarm laser") if armed else tr(w, "run.btn.arm", "Arm laser"))
        b["arm"].setEnabled(connected and not fault and not s.dry_run)
        bp = self.ws.window.ctl.breakpoint is not None
        b["resume_here"].setEnabled(bp and not fault and not s.alarms)
        b["resume_contour"].setEnabled(bp and not fault and not s.alarms)
        # queue card
        r = self.ws.runner
        self.queue_title.setText(tr(w, "run.queue.title", "Queue: {n} jobs, {left} sheets left",
                                    n=len(self.ws.queue), left=self.ws.queue.totals()["remaining"]))
        msg = _msg(w, r.message)
        if r.current is not None and r.state != "idle":
            msg = (msg + "\n" if msg else "") + tr(w, "run.queue.next", "Next: {name} (sheet {n}/{total}{sheet})",
                                                   name=r.current.name, n=min(r.current.done + 1, r.current.quantity),
                                                   total=r.current.quantity,
                                                   sheet=f", {r.current.remnant_id or r.current.sheet}"
                                                   if (r.current.remnant_id or r.current.sheet) else "")
        n_pending = len(self.ws.intake.pending)
        if n_pending:
            msg = (msg + "\n" if msg else "") + tr(w, "run.queue.pending", "{n} submitted job(s) wait for approval "
                                                   "(Queue tab)", n=n_pending)
        self.queue_msg.setText(msg)
        waiting = r.state == "confirm" and r.current is not None and not job
        if waiting:
            ent = r.current
            doc = self.ws.entry_document(ent)
            from mlaser.services.material_check import load_card
            self.load_card.show_card(load_card(ent, doc), doc, (ent.id, ent.done, ent.path, id(doc)), ent)
        if waiting != self.load_card.isVisibleTo(self):
            self.load_card.setVisible(waiting)
        b["confirm_sheet"].setVisible(r.state in ("confirm", "starting"))
        b["confirm_sheet"].setEnabled(r.state == "confirm" and connected and not fault and not job)
        b["queue_mode"].setText(tr(w, "run.btn.queue_leave", "Leave queue") if r.state != "idle"
                                else tr(w, "run.btn.queue_run", "Run queue"))
        b["queue_mode"].setEnabled(r.state != "idle" or self.ws.queue.next_entry() is not None)
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
        # canvas
        doc, sheet = self.ws.view_document()
        self.canvas.set_document(doc, sheet)
        self.canvas.set_progress(s.done_parts if running else (), s.part_index if running else -1,
                                 (s.x, s.y) if connected else None)

    def _alarms(self, s: RunStatus) -> None:
        w = self.ws.window
        ctl = w.ctl
        st = ctl.safety()
        alarms = list(ctl.active_alarms or [])
        items: list[str] = []
        trn = getattr(w, "tr_", None)
        if st.banner == "FAULT":
            items.append(f"{tr(w, 'run.alarm.fault', 'FAULT')}: {st.fault}\n"
                         f"{tr(w, 'run.alarm.remedy', 'Remedy')}: {remedy(st.fault, trn)}")
        for a in alarms:
            text = a.text(trn) if hasattr(a, "text") else str(a)
            since = self.ws.alarm_since.get(getattr(a, "code", str(a)))
            t = time.strftime("%H:%M:%S", time.localtime(since)) if since else ""
            items.append(f"{tr(w, 'run.alarm.alarm', 'ALARM')} {getattr(a, 'code', '')}: {text}   {t}\n"
                         f"{tr(w, 'run.alarm.remedy', 'Remedy')}: {remedy(a, trn)}")
        show = bool(items)
        self.alarm_box.setVisible(show)
        if show:
            self.alarm_title.setText(tr(w, "run.alarm.title", "⚠ {n} active alarm(s)", n=len(items)))
            cur = [self.alarm_list.item(i).text() for i in range(self.alarm_list.count())]
            if cur != items:
                self.alarm_list.clear()
                for text in items:
                    QListWidgetItem(text, self.alarm_list)
            self.reset_btn.setEnabled(True)


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
                                  disarm=lambda why: self.window.ctl.disarm(why))
        self.runner.sheet_listeners.append(self._sheet_done)
        self.monitor: Any = None
        self._svg_cache: Any = None          # mlaser.web.snapshot.SheetSvgCache (monitor on)
        self._svg_published: Any = None
        self._job_t0 = 0.0
        self._run_plan: Any = None
        self._sheet_doc: Any = None
        self._entry_docs: dict = {}          # path -> (mtime, document): the next sheet's picture
        self.alarm_since: dict = {}
        self._unsub: list = []
        self._attached: Any = None
        self.screen = OperatorScreen(self)
        ctx.add_workspace("run", self.screen, tr(self.window, "ws.run", "Run"))
        self.queue.subscribe(self._queue_changed)
        self.runner.subscribe(self._queue_changed)
        self.intake.subscribe(self._queue_changed)
        ctx.provide("queue", lambda: self.queue)
        ctx.provide("intake", lambda: self.intake)
        ctx.provide("remnants", lambda: self.remnants)
        ctx.provide("queue_runner", lambda: self.runner)
        ctx.on_controller_changed(lambda _c: self.attach())
        ctx.on_close(self.close)
        self.attach()
        self.tick_timer = QTimer(self.screen)
        self.tick_timer.setInterval(500)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start()
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
        except Exception:  # noqa: BLE001
            return None

    def job_running(self) -> bool:
        from ..safety_ui import job_running
        return job_running(self.window)

    def job_name(self) -> str:
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

    def _job_started(self, ev: Any) -> None:
        if not ev.resume:
            self._job_t0 = time.time()
        run = getattr(self.window.jobs, "last_run", None)
        plan = getattr(run, "plan", None) if run is not None else None
        if plan is None or ev.resume:
            plan = plan or getattr(self.window, "plan", None)
        if not ev.resume or self._run_plan is None:
            self._run_plan = plan

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
        running = ms is not None and (ms.mode in ("process", "dry_run", "frame", "pause") or ctl.breakpoint is not None)
        plan = self._run_plan if running and self._run_plan is not None else getattr(self.window, "plan", None)
        elapsed = now - self._job_t0 if running and self._job_t0 else 0.0
        return run_status(ms, st, alarms, plan, elapsed, ctl.breakpoint)

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

    def _start_sheet(self) -> bool:
        """Start the loaded sheet through the run bar's path (arming, pre-flight, planned job).
        False when it was refused before reaching the job service."""
        ui = getattr(self.window, "safety_ui", None)
        if ui is None:
            return False
        jobs = self.window.jobs
        before = getattr(jobs, "last_run", None)
        ui.start()
        after = getattr(jobs, "last_run", None)
        return after is not None and after is not before

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
        self.runner.tick()
        try:
            for job in self.intake.scan():
                self.log(tr(self.window, "approval.msg.submitted", "Job submitted for approval: {name} ({src})",
                            name=job.name, src=f"{job.source} {job.client}".strip()))
        except OSError as exc:  # noqa: PERF203 - the inbox must never stop the Run screen
            log.debug("intake scan failed: %s", exc)
        if self.monitor is not None and self.monitor.running:
            self.publish_monitor()

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
        trn = getattr(w, "tr_", None)
        alarms = []
        st = w.ctl.safety()
        if st.banner == "FAULT":
            alarms.append({"text": f"FAULT: {st.fault}", "remedy": remedy(st.fault, trn)})
        for a in list(w.ctl.active_alarms or []):
            alarms.append({"text": f"{getattr(a, 'code', '')} {a.text(trn) if hasattr(a, 'text') else a}",
                           "remedy": remedy(a, trn)})
        queue = []
        for e in self.queue:
            if e.status in ("done", "skipped"):
                continue
            st_key, st_def = STATUS_TEXT.get(e.status, (e.status, e.status))
            queue.append(f"{e.name}  {e.done}/{e.quantity}  {tr(w, st_key, st_def)}")
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
            "sheet_rev": rev, "lang": getattr(trn, "language", "en"),
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


def register(ctx: Any) -> RunWorkspace:
    ws = RunWorkspace(ctx)
    title = tr(ctx.window, "ws.run", "Run")
    ctx.register_command("workspace.run", lambda: show_workspace(ctx, "run", ws.screen, title),
                         label="ws.run")
    ctx.register_command("queue.add_current", lambda: _queue_current(ws), label="queue.cmd.add_current")
    ctx.register_command("queue.confirm_sheet", ws.screen.confirm_sheet, label="run.btn.confirm_sheet")
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
