"""Nest workspace: parts with quantities and priorities, sheet and remnant stock, the engine
choice, a nesting run with progress in a worker, graphical results (one thumbnail per sheet
with utilisation and part count), comparison of runs, and "send to queue"
(docs/review/ux.md P1-6, wireframe §4.3).

The nest project is the one of the Nest dock (``ui/ext_nest.NestExtension.job``), so both views
edit the same parts and sheets.  The run goes through :class:`mlaser.services.nesting.NestService`
(worker thread, results kept for comparison); the queue and the remnant stock are the Run
workspace's (``ctx`` providers ``queue`` / ``remnants``).
"""
from __future__ import annotations

import copy
import os
from typing import Any, Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from mlaser.nest import ENGINE_RASTER, ENGINE_SPYRROW, NestJob, NestSheet
from mlaser.nest.engine import available_engines
from mlaser.services.nesting import NestRun, NestService, send_to_queue

from . import TOUCH_MIN, show_workspace, tr


# ============================================================================== drawing
def _part_path(parts, tol: float = 0.5) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    for p in parts:
        for c in p.contours:
            pts = list(c.points(tol))
            if len(pts) < 2:
                continue
            path.moveTo(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                path.lineTo(x, y)
            if c.closed:
                path.closeSubpath()
    return path


def paint_layout(p: QPainter, rect: QRectF, job: NestJob, layout: Any, palette: QPalette,
                 tol: float = 1.0) -> None:
    """The sheet (or remnant outline) with its placed parts, fitted into ``rect`` (Y up)."""
    from mlaser.nest.apply import layout_parts, sheet_part
    sh = job.sheet(layout.sheet_id)
    sheet = _part_path([sheet_part(sh)], tol)
    br = sheet.boundingRect()
    if br.width() <= 0 or br.height() <= 0:
        return
    s = min(rect.width() / br.width(), rect.height() / br.height()) * 0.96
    p.save()
    p.translate(rect.center().x() - br.center().x() * s, rect.center().y() + br.center().y() * s)
    p.scale(s, -s)
    p.setPen(QPen(palette.color(QPalette.Mid), 0))
    p.setBrush(palette.color(QPalette.AlternateBase))
    p.drawPath(sheet)
    if layout.remnant:
        x0, y0, x1, y1 = layout.remnant
        pen = QPen(QColor("#2e7d32"), 0)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(x0, y0, x1 - x0, y1 - y0))
    p.setPen(QPen(QColor("#1565c0"), 0))
    p.setBrush(QColor(21, 101, 192, 70))
    p.drawPath(_part_path(layout_parts(job, layout), tol))
    p.restore()


def layout_pixmap(job: NestJob, layout: Any, size: QSize, palette: QPalette) -> QPixmap:
    pm = QPixmap(size)
    pm.fill(palette.color(QPalette.Base))
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        paint_layout(p, QRectF(2, 2, size.width() - 4, size.height() - 4), job, layout, palette, 2.0)
    finally:
        p.end()
    return pm


class SheetView(QWidget):
    """The selected result sheet, large."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleName("nest sheet")
        self.job: Optional[NestJob] = None
        self.layout_: Any = None

    def show_layout(self, job: Optional[NestJob], layout: Any) -> None:
        self.job, self.layout_ = job, layout
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt API
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            pal = self.palette()
            p.fillRect(self.rect(), pal.color(QPalette.Base))
            if self.job is None or self.layout_ is None:
                p.setPen(pal.color(QPalette.PlaceholderText))
                p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                           tr(self.window(), "nestws.empty", "Add parts and a sheet, then press Nest"))
                return
            paint_layout(p, QRectF(self.rect()).adjusted(12, 12, -12, -12), self.job, self.layout_, pal, 0.5)
        finally:
            p.end()


# ============================================================================== workspace
class NestWorkspace(QWidget):
    PART_COLS = (("nestws.col.part", "Part"), ("nestws.col.qty", "Qty"), ("nestws.col.max", "Max"),
                 ("nestws.col.priority", "Priority"))

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx
        self.window_ = w = ctx.window
        self.setObjectName("nest_workspace")
        bridge = getattr(w, "_bridge", None)
        self.service = NestService(post=bridge.call.emit if bridge is not None else (lambda fn: fn()))
        self.run: Optional[NestRun] = None
        self._sync = False
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        # ---------------- left: parts, sheets, remnants
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._heading(tr(w, "nestws.parts", "PARTS")))
        self.parts = QTableWidget(0, len(self.PART_COLS))
        self.parts.setObjectName("nestws_parts")
        self.parts.setHorizontalHeaderLabels([tr(w, k, d) for k, d in self.PART_COLS])
        self.parts.verticalHeader().setVisible(False)
        self.parts.verticalHeader().setDefaultSectionSize(34)
        self.parts.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.parts.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.parts.itemChanged.connect(self._part_edited)
        lv.addWidget(self.parts, 2)
        row = QHBoxLayout()
        row.addWidget(self._button("nestws.btn.add_drawing", "+ From drawing", self.add_from_drawing))
        row.addWidget(self._button("nestws.btn.import", "Import file...", self.import_part))
        row.addWidget(self._button("nestws.btn.remove", "Remove", self.remove_part))
        lv.addLayout(row)
        lv.addWidget(self._heading(tr(w, "nestws.stock", "SHEETS / REMNANTS")))
        mat = QHBoxLayout()
        self.material = QLineEdit()
        self.material.setObjectName("nestws_material")
        self.material.setPlaceholderText(tr(w, "nestws.material", "Material"))
        self.material.setMinimumHeight(TOUCH_MIN - 8)
        self.thickness = QDoubleSpinBox()
        self.thickness.setObjectName("nestws_thickness")
        self.thickness.setRange(0.0, 100.0)
        self.thickness.setDecimals(2)
        self.thickness.setSuffix(" mm")
        self.thickness.setMinimumHeight(TOUCH_MIN - 8)
        self.material.editingFinished.connect(self.refresh_stock)
        self.thickness.valueChanged.connect(lambda _v: self.refresh_stock())
        mat.addWidget(self.material, 2)
        mat.addWidget(self.thickness, 1)
        lv.addLayout(mat)
        self.stock = QListWidget()
        self.stock.setObjectName("nestws_stock")
        self.stock.itemChanged.connect(self._stock_checked)
        lv.addWidget(self.stock, 1)
        srow = QHBoxLayout()
        self.sheet_w = self._spin(1, 100000, 3000.0, " mm")
        self.sheet_h = self._spin(1, 100000, 1500.0, " mm")
        self.sheet_n = QSpinBox()
        self.sheet_n.setRange(1, 999)
        self.sheet_n.setValue(5)
        self.sheet_n.setPrefix("× ")
        self.sheet_n.setMinimumHeight(TOUCH_MIN - 8)
        for x in (self.sheet_w, self.sheet_h, self.sheet_n):
            srow.addWidget(x)
        srow.addWidget(self._button("nestws.btn.add_sheet", "Add sheet", self.add_sheet))
        lv.addLayout(srow)
        split.addWidget(left)

        # ---------------- centre: sheet view, parameters, run
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(0, 0, 0, 0)
        self.sheet_title = QLabel()
        self.sheet_title.setObjectName("nestws_sheet_title")
        f = self.sheet_title.font()
        f.setBold(True)
        f.setPointSizeF(13)
        self.sheet_title.setFont(f)
        mv.addWidget(self.sheet_title)
        self.view = SheetView()
        mv.addWidget(self.view, 1)
        prm = QGridLayout()
        self.spacing = self._spin(0, 100, 3.0, " mm")
        self.margin = self._spin(0, 500, 5.0, " mm")
        self.rot = QComboBox()
        for deg in (0, 90, 45, 15):
            self.rot.addItem(tr(w, "nestws.rot.none", "no rotation") if deg == 0 else f"{deg}°", float(deg))
        self.rot.setMinimumHeight(TOUCH_MIN - 8)
        self.engine = QComboBox()
        self.engine.setObjectName("nestws_engine")
        self.engine.addItem(tr(w, "nestws.engine.raster", "Raster (built-in)"), ENGINE_RASTER)
        self.engine.addItem(tr(w, "nestws.engine.spyrrow", "Sparrow (spyrrow)"), ENGINE_SPYRROW)
        if ENGINE_SPYRROW not in available_engines():
            it = self.engine.model().item(1)
            if it is not None:
                it.setEnabled(False)
            self.engine.setItemData(1, tr(w, "nestws.engine.missing", "needs the optional package spyrrow"),
                                    Qt.ToolTipRole)
        self.engine.setMinimumHeight(TOUCH_MIN - 8)
        self.time_limit = self._spin(1, 3600, 30.0, " s", 0)
        for n, (key, text, wid) in enumerate((("nestws.spacing", "Spacing", self.spacing),
                                               ("nestws.margin", "Edge", self.margin),
                                               ("nestws.rotation", "Rotation", self.rot),
                                               ("nestws.engine", "Engine", self.engine),
                                               ("nestws.time", "Time limit", self.time_limit))):
            prm.addWidget(QLabel(tr(w, key, text)), 0, n)
            prm.addWidget(wid, 1, n)
        mv.addLayout(prm)
        run = QHBoxLayout()
        self.nest_btn = self._button("nestws.btn.nest", "Nest ▶", self.start, TOUCH_MIN + 8)
        self.nest_btn.setObjectName("nestws_nest")
        f = self.nest_btn.font()
        f.setBold(True)
        f.setPointSizeF(14)
        self.nest_btn.setFont(f)
        self.cancel_btn = self._button("nestws.btn.cancel", "Cancel", self.service.cancel, TOUCH_MIN + 8)
        self.cancel_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setObjectName("nestws_progress")
        self.progress.setRange(0, 1000)
        self.progress.setMinimumHeight(28)
        run.addWidget(self.nest_btn, 2)
        run.addWidget(self.cancel_btn, 1)
        run.addWidget(self.progress, 3)
        mv.addLayout(run)
        self.status = QLabel()
        self.status.setObjectName("nestws_status")
        self.status.setWordWrap(True)
        mv.addWidget(self.status)
        split.addWidget(mid)

        # ---------------- right: results, compare, send
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self._heading(tr(w, "nestws.results", "RESULTS")))
        self.results = QListWidget()
        self.results.setObjectName("nestws_results")
        self.results.setViewMode(QListView.ListMode)
        self.results.setIconSize(QSize(120, 72))
        self.results.setSpacing(4)
        self.results.currentRowChanged.connect(self._result_selected)
        rv.addWidget(self.results, 3)
        self.total = QLabel()
        self.total.setObjectName("nestws_total")
        self.total.setWordWrap(True)
        rv.addWidget(self.total)
        rv.addWidget(self._heading(tr(w, "nestws.compare", "COMPARE RUNS")))
        self.compare = QTableWidget(0, 5)
        self.compare.setObjectName("nestws_compare")
        self.compare.setHorizontalHeaderLabels([tr(w, "nestws.col.run", "Run"), tr(w, "nestws.col.engine", "Engine"),
                                                tr(w, "nestws.col.sheets", "Sheets"), tr(w, "nestws.col.util", "Util."),
                                                tr(w, "nestws.col.unplaced", "Unplaced")])
        self.compare.verticalHeader().setVisible(False)
        self.compare.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.compare.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.compare.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.compare.itemSelectionChanged.connect(self._run_selected)
        rv.addWidget(self.compare, 2)
        self.queue_btn = self._button("nestws.btn.queue", "Send to queue", self.send_to_queue, TOUCH_MIN + 8)
        self.queue_btn.setObjectName("nestws_send")
        rv.addWidget(self.queue_btn)
        self.main_btn = self._button("nestws.btn.to_drawing", "Copy sheet to drawing", self.to_drawing)
        rv.addWidget(self.main_btn)
        split.addWidget(right)
        split.setSizes([330, 560, 330])
        split.setStretchFactor(1, 1)
        self._update_buttons()

    # ------------------------------------------------------------------ small widgets
    def _heading(self, text: str) -> QLabel:
        lb = QLabel(text)
        f = lb.font()
        f.setBold(True)
        lb.setFont(f)
        return lb

    def _button(self, key: str, text: str, fn, h: int = TOUCH_MIN) -> QPushButton:
        b = QPushButton(tr(self.window_, key, text))
        b.setMinimumHeight(h)
        b.clicked.connect(lambda _=False: fn())
        return b

    def _spin(self, lo: float, hi: float, val: float, suffix: str, dec: int = 1) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(val)
        s.setSuffix(suffix)
        s.setMinimumHeight(TOUCH_MIN - 8)
        return s

    # ------------------------------------------------------------------ model
    @property
    def ext(self) -> Any:
        e = self.window_.extension("ext_nest") if hasattr(self.window_, "extension") else None
        return e[0] if isinstance(e, tuple) else e

    @property
    def job(self) -> NestJob:
        ext = self.ext
        if ext is None:
            if not hasattr(self, "_own_job"):
                self._own_job = NestJob()
            return self._own_job
        return ext.job

    def _provider(self, name: str) -> Any:
        hooks = getattr(self.window_, "hooks", None)
        fn = hooks.provider(name) if hooks is not None else None
        return fn() if fn is not None else None

    @property
    def remnants(self) -> Any:
        return self._provider("remnants")

    @property
    def queue(self) -> Any:
        return self._provider("queue")

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        p = job.params
        self._sync = True
        self.spacing.setValue(p.spacing)
        self.margin.setValue(p.margin)
        i = self.rot.findData(float(p.rotation_step if 0 < p.rotation_step < 360 else 0.0))
        self.rot.setCurrentIndex(max(0, i))
        self.engine.setCurrentIndex(max(0, self.engine.findData(p.engine)))
        self.time_limit.setValue(p.time_limit_s)
        self.parts.setRowCount(len(job.parts))
        for r, np_ in enumerate(job.parts):
            vals = [np_.name, str(np_.quantity), "" if np_.max_quantity is None else str(np_.max_quantity),
                    str(np_.priority)]
            for c, v in enumerate(vals):
                self.parts.setItem(r, c, QTableWidgetItem(v))
        self._sync = False
        self.refresh_stock()
        self._show_run(self.run)

    def refresh_stock(self) -> None:
        w = self.window_
        self._sync = True
        self.stock.clear()
        job = self.job
        used_remnants = {s.extra.get("remnant_id") for s in job.sheets if s.is_remnant}
        for s in job.sheets:
            if s.is_remnant and s.extra.get("remnant_id"):
                continue
            it = QListWidgetItem(tr(w, "nestws.sheet_item", "▭ {name} {w:.0f} × {h:.0f} mm × {n}",
                                    name=s.name, w=s.width, h=s.height, n=s.quantity))
            it.setData(Qt.UserRole, ("sheet", s.id))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.stock.addItem(it)
        store = self.remnants
        if store is not None:
            for r in store.available(self.material.text().strip(), self.thickness.value()):
                it = QListWidgetItem(tr(w, "nestws.remnant_item", "◩ remnant {label}", label=r.label()))
                it.setData(Qt.UserRole, ("remnant", r.id))
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if r.id in used_remnants else Qt.Unchecked)
                self.stock.addItem(it)
        self._sync = False

    def _stock_checked(self, it: QListWidgetItem) -> None:
        if self._sync:
            return
        kind, ident = it.data(Qt.UserRole)
        job = self.job
        on = it.checkState() == Qt.Checked
        if kind == "sheet":
            sh = job.sheet(ident)
            sh.extra["disabled"] = not on
        elif kind == "remnant":
            have = [s for s in job.sheets if s.extra.get("remnant_id") == ident]
            if on and not have and self.remnants is not None:
                rs = self.remnants.get(ident).nest_sheet()
                job.sheets.insert(0, rs)                    # remnants are used first
                rs.id = max([s.id for s in job.sheets] + [-1]) + 1
            elif not on:
                for s in have:
                    job.sheets.remove(s)
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        ext = self.ext
        if ext is not None:
            ext.dirty = True

    def _part_edited(self, it: QTableWidgetItem) -> None:
        if self._sync:
            return
        np_ = self.job.parts[it.row()]
        txt = it.text().strip()
        try:
            if it.column() == 0:
                np_.name = txt
            elif it.column() == 1:
                np_.quantity = max(0, int(txt))
            elif it.column() == 2:
                np_.max_quantity = int(txt) if txt else None
            elif it.column() == 3:
                np_.priority = int(txt)
        except ValueError:
            self._sync = True
            it.setText({1: str(np_.quantity), 2: "" if np_.max_quantity is None else str(np_.max_quantity),
                        3: str(np_.priority)}.get(it.column(), np_.name))
            self._sync = False
        self._mark_dirty()

    def _apply_params(self) -> None:
        p = self.job.params
        p.spacing, p.margin = self.spacing.value(), self.margin.value()
        p.rotation_step = float(self.rot.currentData() or 0.0) or 360.0
        p.engine = self.engine.currentData() or ENGINE_RASTER
        p.time_limit_s = self.time_limit.value()

    # ------------------------------------------------------------------ actions
    def add_from_drawing(self) -> int:
        ext = self.ext
        n = ext.add_part_from_selection() if ext is not None else 0
        self.refresh()
        return n

    def import_part(self, path: Optional[str] = None) -> int:
        ext = self.ext
        n = ext.import_part(path) if ext is not None else 0
        self.refresh()
        return n

    def remove_part(self) -> None:
        rows = sorted({i.row() for i in self.parts.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.job.parts[r]
        if rows:
            self._mark_dirty()
            self.refresh()

    def add_sheet(self) -> NestSheet:
        sh = self.job.add_sheet(NestSheet(self.sheet_w.value(), self.sheet_h.value(), self.sheet_n.value()))
        self._mark_dirty()
        self.refresh_stock()
        return sh

    def _nest_job(self) -> NestJob:
        """The project as the engine gets it: unchecked sheets left out."""
        job = copy.copy(self.job)
        job.sheets = [s for s in self.job.sheets if not s.extra.get("disabled")]
        return job

    def start(self) -> bool:
        w = self.window_
        self._apply_params()
        job = self._nest_job()
        if not job.parts:
            self.status.setText(tr(w, "nestws.msg.no_parts", "Add parts first (from the drawing or a file)."))
            return False
        if not job.sheets:
            job.sheets = [NestSheet()]
            self.job.add_sheet(job.sheets[0])
            self.refresh_stock()
        ok = self.service.start(job, self._done, self._failed, self._progress)
        if ok:
            self.status.setText(tr(w, "nestws.msg.running", "Nesting..."))
            self.progress.setValue(0)
        self._update_buttons()
        return ok

    def _progress(self, frac: float, msg: str) -> None:
        self.progress.setValue(int(1000 * min(1.0, frac)))
        if msg:
            self.status.setText(msg)

    def _failed(self, exc: BaseException) -> None:
        self.status.setText(tr(self.window_, "nestws.msg.failed", "Nesting failed: {err}", err=exc))
        self._update_buttons()

    def _done(self, run: NestRun) -> None:
        self.progress.setValue(1000)
        ext = self.ext
        if ext is not None:
            ext.job.result = run.result
            ext.dirty = True
            try:
                ext.panel.refresh()
            except RuntimeError:
                pass
        for note in run.result.notes:
            self.ctx.log(f"nest: {note}")
        self.run = run
        self.status.setText(tr(self.window_, "nestws.msg.done",
                               "Run {n}: {placed} parts on {sheets} sheets, utilisation {util:.1f} %, {unplaced} unplaced, {t:.1f} s",
                               n=run.number, placed=run.placed, sheets=run.sheets, util=100 * run.utilisation,
                               unplaced=run.unplaced, t=run.elapsed_s))
        self._show_run(run)
        self._update_buttons()

    def _show_run(self, run: Optional[NestRun]) -> None:
        w = self.window_
        self.results.clear()
        pal = self.palette()
        if run is not None:
            for k, lay in enumerate(run.result.layouts):
                sh = run.job.sheet(lay.sheet_id)
                pm = layout_pixmap(run.job, lay, QSize(120, 72), pal)
                label = tr(w, "nestws.result_item", "Sheet {n}  {util:.0f} %  ·  {parts} parts\n{sheet}",
                           n=k + 1, util=100 * lay.utilisation, parts=len(lay.placements),
                           sheet=sh.extra.get("remnant_id") or f"{sh.width:.0f} × {sh.height:.0f} mm")
                if lay.remnant:
                    x0, y0, x1, y1 = lay.remnant
                    label += tr(w, "nestws.leftover", "  → leftover {w:.0f} × {h:.0f}", w=x1 - x0, h=y1 - y0)
                it = QListWidgetItem(QIcon(pm), label)
                it.setSizeHint(QSize(260, 80))
                self.results.addItem(it)
            self.total.setText(tr(w, "nestws.total", "Total {sheets} sheets · {util:.1f} % · {placed} placed · {unplaced} unplaced",
                                  sheets=run.sheets, util=100 * run.utilisation, placed=run.placed,
                                  unplaced=run.unplaced))
            if run.result.layouts:
                self.results.setCurrentRow(0)
        else:
            self.total.setText("")
            self.view.show_layout(None, None)
            self.sheet_title.setText(tr(w, "nestws.no_result", "No result yet"))
        self._fill_compare()

    def _fill_compare(self) -> None:
        runs = self.service.runs
        self.compare.blockSignals(True)
        self.compare.setRowCount(len(runs))
        best = self.service.best()
        for r, run in enumerate(runs):
            vals = [f"{run.number}{' ★' if run is best else ''}", run.engine, str(run.sheets),
                    f"{100 * run.utilisation:.1f} %", str(run.unplaced)]
            for c, v in enumerate(vals):
                self.compare.setItem(r, c, QTableWidgetItem(v))
            if run is self.run:
                self.compare.selectRow(r)
        self.compare.blockSignals(False)

    def _run_selected(self) -> None:
        rows = sorted({i.row() for i in self.compare.selectedIndexes()})
        if rows and rows[0] < len(self.service.runs):
            run = self.service.runs[rows[0]]
            if run is not self.run:
                self.run = run
                ext = self.ext
                if ext is not None:
                    ext.job.result = run.result
                self._show_run(run)

    def _result_selected(self, row: int) -> None:
        run = self.run
        if run is None or not (0 <= row < len(run.result.layouts)):
            return
        lay = run.result.layouts[row]
        sh = run.job.sheet(lay.sheet_id)
        self.view.show_layout(run.job, lay)
        self.sheet_title.setText(tr(self.window_, "nestws.sheet_title", "Sheet {n} / {total}   {w:.0f} × {h:.0f} mm   {util:.1f} %",
                                    n=row + 1, total=len(run.result.layouts), w=sh.width, h=sh.height,
                                    util=100 * lay.utilisation))

    def _update_buttons(self) -> None:
        busy = self.service.running
        self.nest_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        has = self.run is not None and bool(self.run.result.layouts)
        self.queue_btn.setEnabled(has and not busy and self.queue is not None)
        self.main_btn.setEnabled(has and not busy)

    def send_to_queue(self) -> list:
        w = self.window_
        run, q = self.run, self.queue
        if run is None or q is None:
            return []
        from mlaser.services.config import data_dir
        d = os.path.join(str(w.settings.get("data_dir") or data_dir()), "production", "sheets")
        name = (self.ext.path and os.path.splitext(os.path.basename(self.ext.path))[0]) if self.ext else None
        entries = send_to_queue(run.job, q, d, name or "nest", layers=copy.deepcopy(w.document.layers),
                                material=self.material.text().strip(), thickness_mm=self.thickness.value(),
                                remnants=self.remnants)
        self.status.setText(tr(w, "nestws.msg.queued", "{n} sheet jobs added to the queue", n=len(entries)))
        self.refresh_stock()
        return entries

    def to_drawing(self) -> bool:
        ext = self.ext
        run = self.run
        if ext is None or run is None:
            return False
        ext.job.result = run.result
        row = max(0, self.results.currentRow())
        base = self.window_.document
        doc = ext.result_document(row, include_sheet=False)
        doc.layers = copy.deepcopy(base.layers)
        self.ctx.replace_document(doc, tr(self.window_, "nestws.undo.to_drawing", "nest result"))
        return True


def register(ctx: Any) -> NestWorkspace:
    ws = NestWorkspace(ctx)
    title = tr(ctx.window, "ws.nest", "Nest")
    ctx.add_workspace("nest", ws, title)
    ctx.register_command("workspace.nest", lambda: show_workspace(ctx, "nest", ws, title), label="ws.nest")
    return ws


__all__ = ["register", "NestWorkspace", "SheetView", "layout_pixmap", "paint_layout"]
