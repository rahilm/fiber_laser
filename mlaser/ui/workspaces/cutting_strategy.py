"""Cutting strategy panel of the Process workspace and the cutting-strategy pre-flight warnings.

The panel works on the open drawing (docs/user/cutting-strategy.md):

* **Leads**: *Check* finds the lead-ins that reach another contour, their own contour or the
  sheet edge (:func:`mlaser.cam.collision.analyse`) and marks them (``Craft.lead_error``, drawn
  red on the canvas); *Fix* moves their start or shortens them (:func:`mlaser.cam.collision.fix`);
  *Place starts* puts every start on a corner or a straight segment by the rules
  (:func:`mlaser.cam.leadplace.place_leads`).
* **Tipping**: *Check* lists the freed pieces that can tip on the slats (the bed of the machine
  settings, ``bed.*``; :func:`mlaser.cam.tipping.analyse`); *Fix* applies the chosen strategy
  (micro-joint, cut last, rapids avoid; :func:`mlaser.cam.tipping.apply`).

Every change is one undoable edit.  :func:`augment_preflight` adds the ``lead_collision`` (and,
when ``cam.preflight_tip_check`` is on, ``tip_risk``) warnings to the pre-flight report of a
Start / Dry run (``cam.preflight_lead_check``, default on).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import tr


def _cfg(window: Any, key: str, default: Any) -> Any:
    cfg = getattr(window, "config", None)
    if cfg is None:
        return default
    try:
        return cfg.get(key)
    except Exception:  # noqa: BLE001 - unknown key: the default
        return default


def bed_of(window: Any):
    from mlaser.cam.tipping import BedModel

    cfg = getattr(window, "config", None)
    return BedModel.from_config(cfg) if cfg is not None else BedModel()


def augment_preflight(
    window: Any, rep: Any, action: str, doc: Any = None, selected_only: bool = False
) -> Any:
    """``rep`` plus the cutting-strategy warnings for a Start / Dry run (a new report)."""
    if rep is None or doc is None or action not in ("start", "dry_run"):
        return rep
    leads = bool(_cfg(window, "cam.preflight_lead_check", True))
    tips = bool(_cfg(window, "cam.preflight_tip_check", False))
    if not (leads or tips):
        return rep
    from mlaser.machine.preflight import cutting_issues

    try:
        extra = cutting_issues(
            doc,
            getattr(window, "cam_params", None),
            leads=leads,
            bed=bed_of(window) if tips else None,
            selected_only=selected_only,
        )
    except Exception as exc:  # noqa: BLE001 - a check must never block the pre-flight itself
        log = getattr(window, "log", None)
        if callable(log):
            log(f"cutting-strategy check: {exc}")
        return rep
    if not extra:
        return rep
    return replace(rep, issues=list(rep.issues) + extra)


class CuttingStrategyPanel(QWidget):
    """The Process workspace's *Cutting strategy* tab (module doc)."""

    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        w = self.window_ = ctx.window
        self.setObjectName("cutting_strategy")
        # the tab's one scroll area: the panel fits any height the Process column has
        from PySide6.QtWidgets import QFrame, QScrollArea

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(sa)
        body = QWidget()
        sa.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)
        # ---- leads
        from mlaser.ui.techform import FlowLayout, ResponsiveForm, Section

        lform = ResponsiveForm()   # label beside or above the field, from the width it has
        lf = lform.form
        self.prefer = QComboBox()
        self.prefer.setObjectName("cutstrat_prefer")
        for data, key, text in (
            ("auto", "cutstrat.prefer.auto", "automatic"),
            ("corner", "cutstrat.prefer.corner", "corners"),
            ("straight", "cutstrat.prefer.straight", "long straight segments"),
        ):
            self.prefer.addItem(tr(w, key, text), data)
        lf.addRow(tr(w, "cutstrat.prefer", "Start on"), self.prefer)
        self.prefer.setToolTip(
            tr(
                w,
                "cutstrat.prefer.tip",
                "automatic: a corner on parts with sharp corners, else the longest straight segment",
            )
        )
        self.min_feature = QDoubleSpinBox()
        self.min_feature.setRange(0.0, 1000.0)
        self.min_feature.setDecimals(1)
        self.min_feature.setValue(5.0)
        self.min_feature.setSuffix(" mm")
        lf.addRow(tr(w, "cutstrat.min_feature", "Avoid features shorter than"), self.min_feature)
        self.clearance = QDoubleSpinBox()
        self.clearance.setRange(0.0, 50.0)
        self.clearance.setDecimals(2)
        self.clearance.setValue(0.5)
        self.clearance.setSuffix(" mm")
        lf.addRow(tr(w, "cutstrat.clearance", "Clearance to other contours"), self.clearance)
        rw = QWidget()
        row = FlowLayout(rw, 6)
        for key, text, fn, name in (
            ("cutstrat.check_leads", "Check", self.check_leads, "cutstrat_check_leads"),
            ("cutstrat.fix_leads", "Fix", self.fix_leads, "cutstrat_fix_leads"),
            ("cutstrat.place_leads", "Place starts", self.place_leads, "cutstrat_place_leads"),
        ):
            b = QPushButton(tr(w, key, text))
            b.setObjectName(name)
            b.setMinimumHeight(32)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        lf.addRow(rw)
        root.addWidget(Section("cutstrat_leads", tr(w, "cutstrat.leads", "Lead-ins"), lform))
        # ---- tipping
        tform = ResponsiveForm()
        tf = tform.form
        self.bed_label = QLabel()
        self.bed_label.setObjectName("cutstrat_bed")
        self.bed_label.setWordWrap(True)
        tf.addRow(self.bed_label)
        # the bed model lives in the machine settings (ConfigService bed.*): edited here
        bed_w = QWidget()
        bed = FlowLayout(bed_w, 6)
        self.bed_fields: dict[str, Any] = {}
        for key, lo, hi, dec, tip_key, tip in (
            ("bed.slat_pitch", 5.0, 1000.0, 1, "bed.set.slat_pitch", "Slat pitch (centre to centre)"),
            ("bed.slat_width", 0.5, 50.0, 1, "bed.set.slat_width", "Slat width"),
            ("bed.slat_offset", -1.0, 1000.0, 1, "bed.set.slat_offset", "First slat position (-1 = unknown)"),
        ):
            sp = QDoubleSpinBox()
            sp.setObjectName("cutstrat_" + key.replace(".", "_"))
            sp.setRange(lo, hi)
            sp.setDecimals(dec)
            sp.setSuffix(" mm")
            sp.setToolTip(tr(w, tip_key, tip))
            if key == "bed.slat_offset":
                sp.setSpecialValueText(tr(w, "cutstrat.bed.offset_unknown", "unknown"))
            sp.setValue(float(_cfg(w, key, lo)))
            sp.valueChanged.connect(lambda v, k=key: self._set_bed(k, float(v)))
            self.bed_fields[key] = sp
            bed.addWidget(sp)
        self.bed_dir = QComboBox()
        self.bed_dir.setObjectName("cutstrat_bed_direction")
        self.bed_dir.addItem(tr(w, "cutstrat.bed.along_y", "slats along Y"), 0)
        self.bed_dir.addItem(tr(w, "cutstrat.bed.along_x", "slats along X"), 1)
        self.bed_dir.setCurrentIndex(1 if int(_cfg(w, "bed.slat_direction", 0)) == 1 else 0)
        self.bed_dir.currentIndexChanged.connect(lambda i: self._set_bed("bed.slat_direction", int(i)))
        bed.addWidget(self.bed_dir)
        tf.addRow(bed_w)
        self.strategy = QComboBox()
        self.strategy.setObjectName("cutstrat_tip_strategy")
        for data, key, text in (
            ("joint", "cutstrat.tip.joint", "micro-joint on the heavy side"),
            ("last", "cutstrat.tip.last", "cut last, rapids around"),
            ("avoid", "cutstrat.tip.avoid", "rapids around only"),
        ):
            self.strategy.addItem(tr(w, key, text), data)
        tf.addRow(tr(w, "cutstrat.tip.strategy", "Strategy"), self.strategy)
        rw = QWidget()
        row = FlowLayout(rw, 6)
        for key, text, fn, name in (
            ("cutstrat.check_tip", "Check", self.check_tipping, "cutstrat_check_tip"),
            ("cutstrat.fix_tip", "Fix", self.fix_tipping, "cutstrat_fix_tip"),
        ):
            b = QPushButton(tr(w, key, text))
            b.setObjectName(name)
            b.setMinimumHeight(32)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        tf.addRow(rw)
        root.addWidget(Section("cutstrat_tipping", tr(w, "cutstrat.tipping", "Tipping on the slats"), tform))
        # ---- results
        self.summary = QLabel()
        self.summary.setObjectName("cutstrat_summary")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        self.issues = QListWidget()
        self.issues.setObjectName("cutstrat_issues")
        self.issues.setMinimumHeight(self.issues.fontMetrics().height() * 4)
        root.addWidget(self.issues, 1)
        self.refresh_bed()

    # ------------------------------------------------------------------ helpers
    def _set_bed(self, key: str, value: Any) -> None:
        cfg = getattr(self.window_, "config", None)
        if cfg is None:
            return
        try:
            cfg.set(key, value)
        except ValueError as exc:
            self.summary.setText(str(exc))
        self.refresh_bed()

    def refresh_bed(self) -> None:
        b = bed_of(self.window_)
        where = (
            tr(self.window_, "cutstrat.bed.unknown", "position unknown (worst case)")
            if b.offset is None
            else tr(self.window_, "cutstrat.bed.offset", "first slat at {o:g} mm", o=b.offset)
        )
        self.bed_label.setText(
            tr(
                self.window_,
                "cutstrat.bed",
                "Bed: slats along {d}, pitch {p:g} mm, width {w:g} mm, {where}",
                d=b.direction.upper(),
                p=b.pitch,
                w=b.width,
                where=where,
            )
        )

    def lead_params(self):
        from mlaser.cam.collision import LeadCheckParams
        from mlaser.cam.leadplace import LeadRules

        rules = LeadRules(
            prefer=str(self.prefer.currentData() or "auto"), min_feature=self.min_feature.value()
        )
        return LeadCheckParams(clearance=self.clearance.value(), rules=rules)

    def _cam(self) -> Any:
        return getattr(self.window_, "cam_params", None)

    def _doc(self) -> Any:
        return self.ctx.document

    def _show(self, summary: str, lines: list[str]) -> None:
        self.summary.setText(summary)
        self.issues.clear()
        self.issues.addItems(lines[:500])

    # ------------------------------------------------------------------ leads
    def check_leads(self) -> list:
        """Find and mark the colliding lead-ins (one undoable edit when there are any)."""
        from mlaser.cam import collision

        found = collision.analyse(self._doc(), self._cam(), params=self.lead_params())
        if found:
            self.ctx.edit(
                lambda d: collision.mark(d, found), tr(self.window_, "cutstrat.undo.mark", "lead check")
            )
        n = len({i.key for i in found})
        self._show(
            tr(self.window_, "cutstrat.msg.leads", "{n} lead-ins collide", n=n)
            if n
            else tr(self.window_, "cutstrat.msg.leads_ok", "No lead-in collides"),
            [i.message for i in found],
        )
        return found

    def fix_leads(self) -> Any:
        from mlaser.cam import collision

        prm = self.lead_params()
        box: list = []
        self.ctx.edit(
            lambda d: box.append(collision.fix(d, self._cam(), params=prm)),
            tr(self.window_, "cutstrat.undo.fix", "fix lead-ins"),
        )
        rep = box[0] if box else collision.FixReport()
        self._show(
            tr(
                self.window_,
                "cutstrat.msg.fixed",
                "{moved} starts moved, {short} leads shortened, {flagged} flagged; {left} still collide",
                moved=rep.count("moved"),
                short=rep.count("shortened"),
                flagged=rep.count("flagged"),
                left=len({i.key for i in rep.remaining}),
            ),
            [i.message for i in rep.remaining],
        )
        return rep

    def place_leads(self) -> int:
        from mlaser.cam import collision
        from mlaser.cam.leadplace import place_leads

        prm = self.lead_params()
        box: list = []

        def fn(d):
            box.append(place_leads(d, prm.rules))
            box.append(collision.fix(d, self._cam(), params=prm))

        self.ctx.edit(fn, tr(self.window_, "cutstrat.undo.place", "place lead-ins"))
        n = box[0] if box else 0
        rep = box[1] if len(box) > 1 else collision.FixReport()
        summary = tr(
            self.window_,
            "cutstrat.msg.placed",
            "{n} starts placed; {left} lead-ins still collide",
            n=n,
            left=len({i.key for i in rep.remaining}),
        )
        if n == 0 and not _has_lead_in(self._doc()):
            # place_leads only moves the start of contours that have a lead-in
            summary += " " + tr(self.window_, "craft.msg.no_leads", "No contour has a lead-in yet.")
        self._show(summary, [i.message for i in rep.remaining])
        return n

    # ------------------------------------------------------------------ tipping
    def check_tipping(self) -> list:
        from mlaser.cam import tipping

        self.refresh_bed()
        pieces = tipping.analyse(self._doc(), bed_of(self.window_))
        risk = [p for p in pieces if p.at_risk]
        self._show(
            tr(
                self.window_,
                "cutstrat.msg.tip",
                "{n} of {total} pieces can tip",
                n=len(risk),
                total=len(pieces),
            ),
            [p.message for p in risk],
        )
        return risk

    def fix_tipping(self) -> Any:
        from mlaser.cam import tipping

        self.refresh_bed()
        bed, strat = bed_of(self.window_), str(self.strategy.currentData() or "joint")
        box: list = []
        self.ctx.edit(
            lambda d: box.append(tipping.apply(d, bed, strat)),
            tr(self.window_, "cutstrat.undo.tip", "tip-avoiding order"),
        )
        rep = box[0] if box else tipping.TipReport()
        self._show(
            tr(
                self.window_,
                "cutstrat.msg.tip_fixed",
                "{n} pieces at risk: {joints} micro-joints, {last} parts cut last, {avoid} avoided by the rapids",
                n=len(rep.at_risk),
                joints=len(rep.joints),
                last=len(rep.last),
                avoid=len(rep.avoided),
            ),
            [p.message for p in rep.at_risk],
        )
        return rep


def install(ctx: Any, tabs: Any) -> CuttingStrategyPanel:
    """Add the panel as a tab of the Process workspace and register its commands."""
    panel = CuttingStrategyPanel(ctx)
    tabs.addTab(panel, tr(ctx.window, "cutstrat.tab", "Cutting strategy"))
    for cmd, fn, label in (
        ("cam.lead_check", panel.check_leads, "cutstrat.cmd.lead_check"),
        ("cam.lead_fix", panel.fix_leads, "cutstrat.cmd.lead_fix"),
        ("cam.lead_place", panel.place_leads, "cutstrat.cmd.lead_place"),
        ("cam.tip_check", panel.check_tipping, "cutstrat.cmd.tip_check"),
        ("cam.tip_fix", panel.fix_tipping, "cutstrat.cmd.tip_fix"),
    ):
        ctx.register_command(cmd, fn, label=label, workspace="process")
    return panel


__all__ = ["CuttingStrategyPanel", "augment_preflight", "bed_of", "install"]


def _has_lead_in(doc: Any) -> bool:
    for p in getattr(doc, "parts", None) or []:
        crafts = [p.craft] + [c.craft for c in p.contours if c.craft is not None]
        if any(getattr(cr.lead_in, "kind", 0) for cr in crafts):
            return True
    return False
