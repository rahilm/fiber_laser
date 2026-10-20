"""Cutting strategy panel of the Process workspace and the cutting-strategy pre-flight warnings.

The panel works on the open drawing (docs/user/cutting-strategy.md):

* **Leads**: *Check* finds the lead-ins that reach another contour, their own contour or the
  sheet edge (:func:`mlaser.cam.collision.analyse`) and marks them (``Craft.lead_error``, drawn
  red on the canvas); *Fix* moves their start or shortens them (:func:`mlaser.cam.collision.fix`);
  *Place starts* puts every start on a corner or a straight segment by the rules
  (:func:`mlaser.cam.leadplace.place_leads`).
* **Tipping**: *Check* lists the freed pieces that can tip on the slats (the bed of the machine,
  ``bed.*``; :func:`mlaser.cam.tipping.analyse`); *Fix* applies the recipe's strategy
  (micro-joint, cut last, rapids avoid; :func:`mlaser.cam.tipping.apply`).

One home per value (SETTINGS_MAP §3.2, WORK_ITEMS P-9): the rules (where starts go, the
smallest feature, the lead clearance, the tip strategy) are **recipe rules** edited in
Process › Library (:func:`recipe_rules`); the bed model is a machine setting edited in
Machine › Setup › Bed.  The panel shows both read-only with "Edit in …" and keeps the actions.

Every change is one undoable edit.  :func:`augment_preflight` adds the ``lead_collision`` (and,
when ``cam.preflight_tip_check`` is on, ``tip_risk``) warnings to the pre-flight report of a
Start / Dry run (``cam.preflight_lead_check``, default on).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mlaser.services import units as U
from mlaser.ui.units_qt import on_units_changed

from . import tr
from .. import style

#: the recipe rules of the cutting strategy and their defaults (Technology.rules): the start
#: placement (``lead_place``: mlaser.cam.leadplace.LeadRules), the lead check's clearance
#: (``lead_check``) and the tip-avoiding strategy (``tip``)
RULE_DEFAULTS = {("lead_place", "prefer"): "auto", ("lead_place", "min_feature_mm"): 5.0,
                 ("lead_check", "clearance_mm"): 0.5, ("tip", "strategy"): "joint"}
PREFER = (("auto", "cutstrat.prefer.auto", "automatic"), ("corner", "cutstrat.prefer.corner", "corners"),
          ("straight", "cutstrat.prefer.straight", "long straight segments"))
TIP = (("joint", "cutstrat.tip.joint", "micro-joint on the heavy side"),
       ("last", "cutstrat.tip.last", "cut last, rapids around"),
       ("avoid", "cutstrat.tip.avoid", "rapids around only"))


def recipe_rules(tech: Any) -> dict:
    """{(group, key): value} of the cutting-strategy rules of technology ``tech`` (None: the
    defaults)."""
    rules = (getattr(tech, "rules", None) or {}) if tech is not None else {}
    out = {}
    for (g, k), dflt in RULE_DEFAULTS.items():
        grp = rules.get(g) or {}
        v = grp.get(k, grp.get(k[:-3]) if k.endswith("_mm") else None)
        out[(g, k)] = type(dflt)(v) if v is not None else dflt
    return out


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
    """The Process workspace's *Cutting strategy* tab (module doc).  ``store``: the technology
    store (the recipe rules)."""

    def __init__(self, ctx: Any, parent: Optional[QWidget] = None, store: Any = None) -> None:
        super().__init__(parent)
        from mlaser.ui.homes import EditInLink, bind_mirror
        from mlaser.ui.responsive import ColumnsPanel, flow
        from mlaser.ui.techform import Section

        self.ctx = ctx
        self.store = store
        w = self.window_ = ctx.window
        self.setObjectName("cutting_strategy")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(style.SP1, style.SP1, style.SP1, style.SP1)
        panel = ColumnsPanel(max_columns=2)
        outer.addWidget(panel)
        # ---- leads: the recipe's rules (read-only) and the actions
        lb = QWidget()
        lv = QVBoxLayout(lb)
        lv.setContentsMargins(0, 0, 0, 0)
        self.lead_rules = QLabel()
        self.lead_rules.setObjectName("cutstrat_lead_rules")
        self.lead_rules.setWordWrap(True)
        bind_mirror(self.lead_rules, "recipe.rules.lead_place.prefer", getattr(w, "tr_", None))
        lv.addWidget(self.lead_rules)
        self.rules_link = EditInLink(w, "recipe.rules.lead_place.prefer", tr=getattr(w, "tr_", None))
        self.rules_link.setObjectName("cutstrat_rules_link")
        self.rules_link.setWordWrap(True)
        lv.addWidget(self.rules_link)
        buttons = []
        for key, text, fn, name in (
            ("cutstrat.check_leads", "Check", self.check_leads, "cutstrat_check_leads"),
            ("cutstrat.fix_leads", "Fix", self.fix_leads, "cutstrat_fix_leads"),
            ("cutstrat.place_leads", "Place starts", self.place_leads, "cutstrat_place_leads"),
        ):
            b = QPushButton(tr(w, key, text))
            b.setObjectName(name)
            b.clicked.connect(lambda _=False, f=fn: f())
            buttons.append(b)
        lv.addWidget(flow(buttons, justify=False))
        panel.add_section(Section("cutstrat_leads", tr(w, "cutstrat.leads", "Lead-ins"), lb, priority=2))
        # ---- tipping: the bed (Machine › Setup) and the recipe's strategy, read-only
        tb = QWidget()
        tv = QVBoxLayout(tb)
        tv.setContentsMargins(0, 0, 0, 0)
        self.bed_label = QLabel()
        self.bed_label.setObjectName("cutstrat_bed")
        self.bed_label.setWordWrap(True)
        bind_mirror(self.bed_label, "cfg:bed.slat_pitch", getattr(w, "tr_", None))
        tv.addWidget(self.bed_label)
        self.bed_link = EditInLink(w, "cfg:bed.slat_pitch", tr=getattr(w, "tr_", None))
        self.bed_link.setObjectName("cutstrat_bed_link")
        self.bed_link.setWordWrap(True)
        tv.addWidget(self.bed_link)
        self.tip_rule = QLabel()
        self.tip_rule.setObjectName("cutstrat_tip_rule")
        self.tip_rule.setWordWrap(True)
        bind_mirror(self.tip_rule, "recipe.rules.tip.strategy", getattr(w, "tr_", None))
        tv.addWidget(self.tip_rule)
        buttons = []
        for key, text, fn, name in (
            ("cutstrat.check_tip", "Check", self.check_tipping, "cutstrat_check_tip"),
            ("cutstrat.fix_tip", "Fix", self.fix_tipping, "cutstrat_fix_tip"),
        ):
            b = QPushButton(tr(w, key, text))
            b.setObjectName(name)
            b.clicked.connect(lambda _=False, f=fn: f())
            buttons.append(b)
        tv.addWidget(flow(buttons, justify=False))
        panel.add_section(Section("cutstrat_tipping", tr(w, "cutstrat.tipping", "Tipping on the slats"), tb,
                                  priority=1))
        # ---- results (a list: may scroll)
        self.summary = QLabel()
        self.summary.setObjectName("cutstrat_summary")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)
        self.issues = QListWidget()
        self.issues.setObjectName("cutstrat_issues")
        self.issues.setMinimumHeight(self.issues.fontMetrics().height() * 3)
        outer.addWidget(self.issues, 1)
        outer.setStretch(0, 0)
        self.refresh_bed()
        on_units_changed(self, lambda _u: self.refresh_bed(), now=False)
        cfg = getattr(w, "config", None)
        if cfg is not None and hasattr(cfg, "subscribe"):
            self._unsub = cfg.subscribe(self._config_changed)

    def _config_changed(self, key: str) -> None:
        if key == "*" or key.startswith("bed."):
            try:
                self.refresh_bed()
            except RuntimeError:          # the panel is gone (window closed)
                pass

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self.refresh_bed()

    # ------------------------------------------------------------------ helpers
    def rules_tech(self) -> tuple[Any, Optional[int]]:
        """(technology, layer) whose rules apply: the recipe of the layer chosen in Process, else
        of the first layer of the drawing (None: no recipe, the defaults)."""
        from mlaser.services import inheritance as I

        doc = self._doc()
        if doc is None or self.store is None:
            return None, None
        ext = self.window_.extension("ext_process") if hasattr(self.window_, "extension") else None
        ws = getattr(ext, "workspace", None)
        layers = sorted({p.craft.layer for p in doc.parts})
        cur = getattr(ws, "current_layer", None)
        li = cur if cur in layers else (layers[0] if layers else None)
        if li is None:
            return None, None
        return I.recipe_of(doc, li, self.store), li

    def rules(self) -> dict:
        return recipe_rules(self.rules_tech()[0])

    def refresh_rules(self) -> None:
        from mlaser.services import inheritance as I

        w = self.window_
        tech, li = self.rules_tech()
        r = recipe_rules(tech)
        u = U.current()
        prefer = dict((d, tr(w, k, t)) for d, k, t in PREFER).get(r[("lead_place", "prefer")], r[("lead_place", "prefer")])
        tip = dict((d, tr(w, k, t)) for d, k, t in TIP).get(r[("tip", "strategy")], r[("tip", "strategy")])
        src = (tr(w, "cutstrat.from_recipe", "From {recipe}.", recipe=I.recipe_label(tech)) if tech is not None
               else tr(w, "cutstrat.no_recipe", "No recipe on the layer: the default rules."))
        self.lead_rules.setText(tr(
            w, "cutstrat.rules", "Start on {prefer}; no start on features shorter than {min}; clearance {clear}. {source}",
            prefer=prefer, min=u.fmt(r[("lead_place", "min_feature_mm")], "length", trim=True),
            clear=u.fmt(r[("lead_check", "clearance_mm")], "small_length", trim=True), source=src))
        self.tip_rule.setText(tr(w, "cutstrat.tip.rule", "Strategy: {strategy}. {source}", strategy=tip, source=src))
        self.rules_link.target = tech
        self.rules_link.setVisible(tech is not None)

    def refresh_bed(self) -> None:
        b = bed_of(self.window_)
        where = (
            tr(self.window_, "cutstrat.bed.unknown", "position unknown (worst case)")
            if b.offset is None
            else tr(self.window_, "cutstrat.bed.offset", "first slat at {o}", o=U.current().fmt(b.offset, "length", trim=True))
        )
        self.bed_label.setText(
            tr(
                self.window_,
                "cutstrat.bed",
                "Bed: slats along {d}, pitch {p}, width {w}, {where}",
                d=b.direction.upper(),
                p=U.current().fmt(b.pitch, "length", trim=True),
                w=U.current().fmt(b.width, "length", trim=True),
                where=where,
            )
        )
        self.refresh_rules()

    def lead_params(self):
        """The lead check / placement parameters: the recipe's rules (Process › Library)."""
        from mlaser.cam.collision import LeadCheckParams
        from mlaser.cam.leadplace import LeadRules

        r = self.rules()
        rules = LeadRules(prefer=str(r[("lead_place", "prefer")]), min_feature=float(r[("lead_place", "min_feature_mm")]))
        return LeadCheckParams(clearance=float(r[("lead_check", "clearance_mm")]), rules=rules)

    def tip_strategy(self) -> str:
        return str(self.rules()[("tip", "strategy")])

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
        bed, strat = bed_of(self.window_), self.tip_strategy()
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


def install(ctx: Any, tabs: Any, store: Any = None) -> CuttingStrategyPanel:
    """Add the panel as a tab of the Process workspace and register its commands."""
    panel = CuttingStrategyPanel(ctx, store=store)
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


__all__ = ["CuttingStrategyPanel", "augment_preflight", "bed_of", "install", "recipe_rules", "RULE_DEFAULTS"]


def _has_lead_in(doc: Any) -> bool:
    for p in getattr(doc, "parts", None) or []:
        crafts = [p.craft] + [c.craft for c in p.contours if c.craft is not None]
        if any(getattr(cr.lead_in, "kind", 0) for cr in crafts):
            return True
    return False
