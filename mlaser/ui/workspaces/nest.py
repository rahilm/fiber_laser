"""Nest workspace: parts with quantities and priorities, sheet and remnant stock, the engine
choice, a nesting run with progress in a worker, graphical results (one thumbnail per sheet
with utilisation and part count), comparison of runs, and "send to queue"
(docs/review/ux.md P1-6, wireframe §4.3).

Nesting quality and cutting strategy (review 2 P1-5, docs/user/cutting-strategy.md): per-part
rotation (sheet setting, fixed, 0/180°, 90° steps, grain) and mirror columns; the *Options* drawer
with common line, part in part, mirror, gravity and grain direction, and the cutting strategy
(skeleton cut-up, remnant separation cut, lead collision fix, tip-avoiding order) applied to the
sheets that go to the queue; the comparison of runs shows cut length and pierces.

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
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListView, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from mlaser.nest import (ENGINE_RASTER, ENGINE_SPYRROW, GRAVITY_AUTO, GRAVITY_BOTTOM, GRAVITY_LEFT,
                         GRAVITY_RIGHT, GRAVITY_TOP, NestJob, NestSheet)
from mlaser.nest.engine import available_engines
from mlaser.nest.model import GRAIN_NONE, GRAIN_X, GRAIN_Y, SCRAP_GAPS, SCRAP_GRID, SCRAP_OFF
from mlaser.services.nesting import NestRun, NestService, send_to_queue

#: per-part rotation choices of the parts table: (id, label key, default text, NestPart.angles, grain)
ROTATIONS = (("sheet", "nestws.rotp.sheet", "sheet setting", None, False),
             ("fixed", "nestws.rotp.fixed", "fixed 0°", [0.0], False),
             ("half", "nestws.rotp.half", "0° / 180°", [0.0, 180.0], False),
             ("quarter", "nestws.rotp.quarter", "90° steps", [0.0, 90.0, 180.0, 270.0], False),
             ("grain", "nestws.rotp.grain", "with the grain", None, True))


#: NestParams fields of the cutting strategy (applied to a finished run without nesting again)
STRATEGY_FIELDS = ("scrap_cut", "scrap_pitch", "scrap_clearance", "scrap_min_length", "remnant_cut",
                   "remnant_min_width", "strategy_kerf", "lead_fix", "tip_check")


def rotation_choice(np_) -> str:
    """The ROTATIONS id of a nest part."""
    if np_.extra.get("grain") and not np_.angles:
        return "grain"
    if not np_.angles:
        return "sheet"
    a = sorted(round(float(x) % 360.0, 6) for x in np_.angles)
    for rid, _k, _d, angles, _g in ROTATIONS:
        if angles is not None and a == sorted(angles):
            return rid
    return "custom"


def set_rotation(np_, rid: str) -> None:
    for r, _k, _d, angles, grain in ROTATIONS:
        if r == rid:
            np_.angles = None if angles is None else list(angles)
            if grain:
                np_.extra["grain"] = True
            else:
                np_.extra.pop("grain", None)
            return

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
                 tol: float = 1.0, lines=()) -> None:
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
    for a, b, kind in lines:            # cutting strategy: separation cut, skeleton cut-up
        pen = QPen(QColor("#2e7d32") if kind == "remnant_cut" else QColor("#ef6c00"), 0)
        pen.setCosmetic(True)
        pen.setWidthF(2.0 if kind == "remnant_cut" else 1.2)
        p.setPen(pen)
        p.drawLine(a[0], a[1], b[0], b[1])
    p.restore()


def strategy_segments(job: NestJob, layout: Any) -> list:
    """(a, b, kind) of the cut-up / separation lines the job's options give for ``layout``."""
    from mlaser.nest.apply import layout_parts
    from mlaser.nest.shapes import parts_geometry, sheet_geometry
    from mlaser.nest.skeleton import strategy_lines
    prm = job.params
    if prm.scrap_cut == SCRAP_OFF and not prm.remnant_cut:
        return []
    full, _ = sheet_geometry(job.sheet(layout.sheet_id), 0.0)
    used = parts_geometry(layout_parts(job, layout))
    res = strategy_lines(full, used, layout.remnant, getattr(job.result, "gravity", "") or prm.gravity, prm)
    out = []
    for part in res.lines:
        for c in part.contours:
            for sg in c.segments:
                out.append((sg.start, sg.end, part.extra.get("strategy", "")))
    return out


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
        self.lines: list = []

    def show_layout(self, job: Optional[NestJob], layout: Any, lines=()) -> None:
        self.job, self.layout_, self.lines = job, layout, list(lines)
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
            paint_layout(p, QRectF(self.rect()).adjusted(12, 12, -12, -12), self.job, self.layout_, pal, 0.5,
                         self.lines)
        finally:
            p.end()


# ============================================================================== workspace
class NestWorkspace(QWidget):
    PART_COLS = (("nestws.col.part", "Part"), ("nestws.col.qty", "Qty"), ("nestws.col.max", "Max"),
                 ("nestws.col.priority", "Priority"), ("nestws.col.rotation", "Rotation"),
                 ("nestws.col.mirror", "Mirror"))
    COMPARE_COLS = (("nestws.col.run", "Run"), ("nestws.col.engine", "Engine"), ("nestws.col.sheets", "Sheets"),
                    ("nestws.col.util", "Util."), ("nestws.col.cut", "Cut m"), ("nestws.col.pierces", "Pierces"),
                    ("nestws.col.unplaced", "Unplaced"))

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
        self.options_btn = QToolButton()
        self.options_btn.setObjectName("nestws_options_btn")
        self.options_btn.setCheckable(True)
        self.options_btn.setText(tr(w, "nestws.options", "Options"))
        self.options_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.options_btn.setArrowType(Qt.RightArrow)
        self.options_btn.setMinimumHeight(TOUCH_MIN - 8)
        self.options_btn.toggled.connect(self.show_options)
        prm.addWidget(self.options_btn, 1, 5)
        mv.addLayout(prm)
        self.options = self._options_panel()
        self.options.setVisible(False)
        mv.addWidget(self.options)
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
        self.compare = QTableWidget(0, len(self.COMPARE_COLS))
        self.compare.setObjectName("nestws_compare")
        self.compare.setHorizontalHeaderLabels([tr(w, k, d) for k, d in self.COMPARE_COLS])
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

    # ------------------------------------------------------------------ options drawer
    def _check(self, key: str, text: str, name: str) -> QCheckBox:
        cb = QCheckBox(tr(self.window_, key, text))
        cb.setObjectName(name)
        cb.setMinimumHeight(TOUCH_MIN - 12)
        return cb

    def _combo(self, items, name: str) -> QComboBox:
        cb = QComboBox()
        cb.setObjectName(name)
        for data, key, text in items:
            cb.addItem(tr(self.window_, key, text), data)
        cb.setMinimumHeight(TOUCH_MIN - 12)
        return cb

    def _options_panel(self) -> QWidget:
        """Nesting options (engine) and cutting strategy (sheets as cut), side by side."""
        w = self.window_
        box = QWidget()
        box.setObjectName("nestws_options")
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        nb = QGroupBox(tr(w, "nestws.opt.nesting", "Nesting"))
        nf = QFormLayout(nb)
        self.common_line = self._check("nestws.opt.common_line", "Common line", "nestws_common_line")
        self.share_min = self._spin(0.1, 10000, 20.0, " mm")
        self.share_min.setToolTip(tr(w, "nestws.opt.share_min", "shortest shared edge"))
        self.kerf = self._spin(0, 10, 0.2, " mm", 2)
        self.kerf.setToolTip(tr(w, "nestws.opt.kerf", "kerf: the part distance with common line"))
        cl = QHBoxLayout()
        cl.addWidget(self.common_line)
        cl.addWidget(self.share_min)
        cl.addWidget(self.kerf)
        nf.addRow(cl)
        self.part_in_part = self._check("nestws.opt.part_in_part", "Part in part", "nestws_part_in_part")
        self.mirror = self._check("nestws.opt.mirror", "Mirror (all parts)", "nestws_mirror")
        pp = QHBoxLayout()
        pp.addWidget(self.part_in_part)
        pp.addWidget(self.mirror)
        nf.addRow(pp)
        self.gravity = self._combo(((GRAVITY_LEFT, "nestws.grav.left", "left"),
                                    (GRAVITY_BOTTOM, "nestws.grav.bottom", "bottom"),
                                    (GRAVITY_RIGHT, "nestws.grav.right", "right"),
                                    (GRAVITY_TOP, "nestws.grav.top", "top"),
                                    (GRAVITY_AUTO, "nestws.grav.auto", "automatic")), "nestws_gravity")
        nf.addRow(tr(w, "nestws.opt.gravity", "Gravity"), self.gravity)
        self.grain = self._combo(((GRAIN_NONE, "nestws.grain.none", "none"),
                                  (GRAIN_X, "nestws.grain.x", "along X"),
                                  (GRAIN_Y, "nestws.grain.y", "along Y")), "nestws_grain")
        nf.addRow(tr(w, "nestws.opt.grain", "Sheet grain"), self.grain)
        row.addWidget(nb, 1)
        sb = QGroupBox(tr(w, "nestws.opt.strategy", "Cutting strategy"))
        sf = QFormLayout(sb)
        self.scrap = self._combo(((SCRAP_OFF, "nestws.scrap.off", "off"),
                                  (SCRAP_GRID, "nestws.scrap.grid", "grid"),
                                  (SCRAP_GAPS, "nestws.scrap.gaps", "in the gaps")), "nestws_scrap")
        self.scrap_pitch = self._spin(10, 10000, 500.0, " mm", 0)
        self.scrap_pitch.setToolTip(tr(w, "nestws.opt.scrap_pitch", "largest scrap piece"))
        sc = QHBoxLayout()
        sc.addWidget(self.scrap, 1)
        sc.addWidget(self.scrap_pitch)
        sf.addRow(tr(w, "nestws.opt.scrap", "Skeleton cut-up"), sc)
        self.remnant_cut = self._check("nestws.opt.remnant_cut", "Remnant separation cut", "nestws_remnant_cut")
        self.remnant_min = self._spin(1, 10000, 100.0, " mm", 0)
        self.remnant_min.setToolTip(tr(w, "nestws.opt.remnant_min", "narrowest remnant worth separating"))
        rc = QHBoxLayout()
        rc.addWidget(self.remnant_cut, 1)
        rc.addWidget(self.remnant_min)
        sf.addRow(rc)
        self.lead_fix = self._check("nestws.opt.lead_fix", "Fix lead collisions", "nestws_lead_fix")
        sf.addRow(self.lead_fix)
        self.tip_check = self._check("nestws.opt.tip_check", "Tip-avoiding order", "nestws_tip_check")
        self.tip_strategy = self._combo((("joint", "nestws.tip.joint", "micro-joint"),
                                         ("last", "nestws.tip.last", "cut last"),
                                         ("avoid", "nestws.tip.avoid", "rapids avoid")), "nestws_tip_strategy")
        tc = QHBoxLayout()
        tc.addWidget(self.tip_check, 1)
        tc.addWidget(self.tip_strategy)
        sf.addRow(tc)
        row.addWidget(sb, 1)
        for wid in (self.scrap, self.scrap_pitch, self.remnant_cut, self.remnant_min):
            sig = getattr(wid, "currentIndexChanged", None) or getattr(wid, "valueChanged", None) \
                or wid.toggled
            sig.connect(lambda *_a: self._strategy_changed())
        return box

    def _strategy_changed(self) -> None:
        if not self._sync and self.run is not None:
            self._result_selected(self.results.currentRow())

    def show_options(self, on: bool = True) -> None:
        if self.options_btn.isChecked() != bool(on):
            self.options_btn.setChecked(bool(on))
            return
        self.options.setVisible(bool(on))
        self.options_btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def bed(self) -> Any:
        """The slat bed of the tip check (ConfigService ``bed.*``)."""
        from mlaser.cam.tipping import BedModel
        cfg = getattr(self.ctx, "config", None)
        return BedModel.from_config(cfg) if cfg is not None else BedModel()

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
        self.common_line.setChecked(p.common_line)
        self.share_min.setValue(p.share_edge_min_len)
        self.kerf.setValue(p.kerf)
        self.part_in_part.setChecked(p.part_in_part)
        self.mirror.setChecked(p.allow_mirror)
        self.gravity.setCurrentIndex(max(0, self.gravity.findData(p.gravity)))
        self.grain.setCurrentIndex(max(0, self.grain.findData(p.grain)))
        self.scrap.setCurrentIndex(max(0, self.scrap.findData(p.scrap_cut)))
        self.scrap_pitch.setValue(p.scrap_pitch)
        self.remnant_cut.setChecked(p.remnant_cut)
        self.remnant_min.setValue(p.remnant_min_width)
        self.lead_fix.setChecked(p.lead_fix)
        self.tip_check.setChecked(p.tip_check)
        self.tip_strategy.setCurrentIndex(max(0, self.tip_strategy.findData(
            str(job.extra.get("tip_strategy", "joint")))))
        self.parts.setRowCount(len(job.parts))
        for r, np_ in enumerate(job.parts):
            vals = [np_.name, str(np_.quantity), "" if np_.max_quantity is None else str(np_.max_quantity),
                    str(np_.priority)]
            for c, v in enumerate(vals):
                self.parts.setItem(r, c, QTableWidgetItem(v))
            self.parts.setCellWidget(r, 4, self._rotation_combo(np_))
            it = QTableWidgetItem("")
            it.setFlags((it.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            it.setCheckState(Qt.Checked if np_.mirror else Qt.Unchecked)
            self.parts.setItem(r, 5, it)
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

    def _rotation_combo(self, np_) -> QComboBox:
        cb = QComboBox()
        for rid, key, text, _a, _g in ROTATIONS:
            cb.addItem(tr(self.window_, key, text), rid)
        cur = rotation_choice(np_)
        if cur == "custom":
            cb.addItem(", ".join(f"{a:g}°" for a in np_.angles or []), "custom")
        cb.setCurrentIndex(max(0, cb.findData(cur)))
        cb.currentIndexChanged.connect(lambda _i, n=np_, c=cb: self._rotation_changed(n, c))
        return cb

    def _rotation_changed(self, np_, cb: QComboBox) -> None:
        if self._sync or cb.currentData() == "custom":
            return
        set_rotation(np_, str(cb.currentData()))
        self._mark_dirty()

    def _part_edited(self, it: QTableWidgetItem) -> None:
        if self._sync:
            return
        np_ = self.job.parts[it.row()]
        if it.column() == 5:
            np_.mirror = it.checkState() == Qt.Checked
            self._mark_dirty()
            return
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
        p.common_line = self.common_line.isChecked()
        p.share_edge_min_len = self.share_min.value()
        p.kerf = self.kerf.value()
        p.part_in_part = self.part_in_part.isChecked()
        p.allow_mirror = self.mirror.isChecked()
        p.gravity = self.gravity.currentData() or GRAVITY_LEFT
        p.grain = self.grain.currentData() or GRAIN_NONE
        p.scrap_cut = self.scrap.currentData() or SCRAP_OFF
        p.scrap_pitch = self.scrap_pitch.value()
        p.remnant_cut = self.remnant_cut.isChecked()
        p.remnant_min_width = self.remnant_min.value()
        p.lead_fix = self.lead_fix.isChecked()
        p.tip_check = self.tip_check.isChecked()
        self.job.extra["tip_strategy"] = self.tip_strategy.currentData() or "joint"

    def _strategy_job(self, run: NestRun) -> NestJob:
        """The run's job with the cutting strategy as set now (it needs no new nesting run)."""
        self._apply_params()
        job = copy.copy(run.job)
        job.params = copy.copy(run.job.params)
        for k in STRATEGY_FIELDS:
            setattr(job.params, k, getattr(self.job.params, k))
        return job

    def strategy_options(self) -> dict:
        """Keyword arguments of ``services.nesting.sheet_document`` for the queue / drawing."""
        self._apply_params()
        return {"bed": self.bed(), "tip_strategy": str(self.job.extra.get("tip_strategy", "joint"))}

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
                    f"{100 * run.utilisation:.1f} %", f"{run.cut_length_mm / 1000.0:.1f}", str(run.pierces),
                    str(run.unplaced)]
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
        job = self._strategy_job(run)
        try:
            lines = strategy_segments(job, lay)
        except Exception as exc:  # noqa: BLE001 - a preview must not break the workspace
            self.ctx.log(f"nest: strategy preview: {exc}")
            lines = []
        self.view.show_layout(run.job, lay, lines)
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
        job = self._strategy_job(run)
        entries = send_to_queue(job, q, d, name or "nest", layers=copy.deepcopy(w.document.layers),
                                material=self.material.text().strip(), thickness_mm=self.thickness.value(),
                                remnants=self.remnants, strategy=self.strategy_options())
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
        from mlaser.services.nesting import sheet_document
        job = self._strategy_job(run)
        opts = self.strategy_options()
        lays = run.result.layouts
        if not lays:
            return False
        doc, _info = sheet_document(job, lays[min(row, len(lays) - 1)], base.layers, **opts)
        doc.layers = copy.deepcopy(base.layers)
        self.ctx.replace_document(doc, tr(self.window_, "nestws.undo.to_drawing", "nest result"))
        return True


def register(ctx: Any) -> NestWorkspace:
    ws = NestWorkspace(ctx)
    title = tr(ctx.window, "ws.nest", "Nest")
    ctx.add_workspace("nest", ws, title)
    ctx.register_command("workspace.nest", lambda: show_workspace(ctx, "nest", ws, title), label="ws.nest")
    return ws


__all__ = ["register", "NestWorkspace", "SheetView", "layout_pixmap", "paint_layout", "strategy_segments",
           "ROTATIONS", "rotation_choice", "set_rotation"]
