"""Process workspace (SETTINGS_MAP H2 + H3): two pages.

* **Drawing**: the drawing's layers (recipe, state: as recipe / n values set on the layer / newer
  recipe version / example values) and, for the selected layer, the **layer inspector**
  (:mod:`.layer_inspector`: which recipe the layer follows, its values, its lead / kerf
  defaults, every value set on the layer marked with a restore button); the cutting-strategy
  checks; the time / cost estimate.  "n layers use an older recipe version: Update".
* **Library**: the technology records (material › thickness › gas · nozzle, search), the
  editor of the selected record (:class:`TechnologyEditor`, grouped form: Cutting, Piercing, Gas
  and nozzle, Height follow, Leads and kerf with the diagram, Corners and small features, About,
  Advanced), what changed, history, Save version / Save as new / Revert to factory,
  Import / Export, Compare.

A record is *assigned* to layers (the layers follow it; nothing set on a layer, a part or a
contour is overwritten: :mod:`mlaser.services.inheritance`), it is no longer copied once.  Each
page adapts to the width it gets (rows wrap label-above-field, the Library's list goes above
the editor).  Records live in :class:`mlaser.services.technology.TechnologyStore`.
"""

from __future__ import annotations

import copy
import time
from dataclasses import replace
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSizePolicy, QSpinBox, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from mlaser.model import LeadParams
from mlaser.services import technology as T
from mlaser.ui.techform import quantity
from mlaser.ui.responsive import FlowLayout, ResponsiveForm
from mlaser.ui.techform import TechForm

from .recipe_picker import MaterialPicker, RecipePicker, _nz, _nz_label, thickness_label  # noqa: F401


# --------------------------------------------------------------------------- editor
class TechnologyEditor(QWidget):
    """Edits one :class:`Technology`: the layer parameters in a :class:`TechForm` plus the
    record's craft defaults, rules and notes as sections of it.  ``base_fn(tech)`` gives the
    record the change marks compare with (the factory version; None = registry defaults)."""

    changed = Signal()

    def __init__(self, tr: Any, parent: Optional[QWidget] = None,
                 base_fn: Optional[Callable[[T.Technology], Optional[T.Technology]]] = None,
                 pressure_unit: int = 0) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.tech: Optional[T.Technology] = None
        self.base_fn = base_fn
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.header.setTextFormat(Qt.RichText)
        lay.addWidget(self.header)
        self.badge = QLabel()
        self.badge.setWordWrap(True)
        self.badge.setObjectName("tech_example_badge")
        lay.addWidget(self.badge)
        self.form = TechForm(tr, pressure_unit, scope="recipe")
        self.form.changed.connect(self._edited)
        lay.addWidget(self.form, 1)
        self.fields = self.form.fields
        self._craft_sections()

    # ---------------------------------------------------------------- craft sections
    def _craft_sections(self) -> None:
        from mlaser.ui.properties.lead_diagram import LeadDiagram

        t = self.tr_.tr
        kinds = [t(k) for k in ("props.lead.none", "props.lead.line", "props.lead.arc", "props.lead.line_arc")]
        self.lead_kind = QComboBox()
        self.lead_kind.addItems(kinds)
        self.lead_len = quantity("length", 0.0, 100.0)
        self.lead_ang = quantity("angle", 0.0, 180.0, 1)
        self.lead_rad = quantity("length", 0.0, 100.0)
        self.lout_kind = QComboBox()
        self.lout_kind.addItems(kinds)
        self.lout_len = quantity("length", 0.0, 100.0)
        self.kerf_kind = QComboBox()
        for v, k in ((-1, "props.kerf.none"), (0, "props.kerf.inner"), (1, "props.kerf.outer")):
            self.kerf_kind.addItem(t(k), v)
        self.kerf_w = quantity("small_length", 0.0, 10.0, 3)
        self.mj_count = QSpinBox()
        self.mj_count.setRange(0, 100)
        self.mj_len = quantity("length", 0.0, 50.0)
        self.diagram = LeadDiagram()
        self.form.add_section("leads", t("techform.sec.leads"), [
            (t("tech.lead_kind"), self.lead_kind, t("tech.h.lead_kind"), False),
            (t("tech.lead_length"), self.lead_len, t("tech.h.lead_length"), False),
            (t("tech.lead_angle"), self.lead_ang, t("tech.h.lead_angle"), False),
            (t("tech.lead_radius"), self.lead_rad, t("tech.h.lead_radius"), True),
            (t("tech.lead_out_kind"), self.lout_kind, t("tech.h.lead_out_kind"), True),
            (t("tech.lead_out_length"), self.lout_len, t("tech.h.lead_out_length"), True),
            (t("tech.kerf_side"), self.kerf_kind, t("tech.h.kerf_side"), False),
            (t("tech.kerf_width"), self.kerf_w, t("tech.h.kerf_width"), False),
            (t("tech.joint_count"), self.mj_count, t("tech.h.joint_count"), True),
            (t("tech.joint_length"), self.mj_len, t("tech.h.joint_length"), True),
        ], extra=self.diagram)
        self.sf_size = quantity("length", 0.0, 1000.0, 1)
        self.sf_lead = quantity("length", 0.0, 100.0)
        self.corner_on = QCheckBox(t("tech.corner_cool"))
        self.corner_angle = quantity("angle", 1.0, 179.0, 0)
        self.form.add_section("corners", t("techform.sec.corners"), [
            (t("tech.small_size"), self.sf_size, t("tech.h.small_size"), False),
            (t("tech.small_lead"), self.sf_lead, t("tech.h.small_lead"), False),
            ("", self.corner_on, t("tech.h.corner_cool"), False),
            (t("tech.corner_angle"), self.corner_angle, t("tech.h.corner_angle"), False),
        ])
        # the cutting-strategy rules (Process › Cutting strategy shows them read-only)
        from .cutting_strategy import PREFER, TIP
        self.prefer = QComboBox()
        for data, key, _text in PREFER:
            self.prefer.addItem(t(key), data)
        self.min_feature = quantity("length", 0.0, 1000.0, 1)
        self.clearance = quantity("small_length", 0.0, 50.0, 2)
        self.tip_strategy = QComboBox()
        for data, key, _text in TIP:
            self.tip_strategy.addItem(t(key), data)
        self.form.add_section("strategy", t("techform.sec.strategy"), [
            (t("cutstrat.prefer"), self.prefer, t("tech.h.prefer"), False),
            (t("cutstrat.min_feature"), self.min_feature, t("tech.h.min_feature"), False),
            (t("cutstrat.clearance"), self.clearance, t("tech.h.clearance"), False),
            (t("tech.tip_strategy"), self.tip_strategy, t("tech.h.tip_strategy"), False),
        ])
        self.flow = quantity("flow", 0.0, 100000.0, 1)
        self.flow.setSpecialValueText(t("tech.flow_unknown"))
        self.density = quantity("density", 0.0, 30.0, 3)
        self.density.setSpecialValueText(t("tech.unknown"))
        self.verified = QCheckBox(t("tech.verified"))
        self.notes = QLineEdit()
        self.form.add_section("record", t("techform.sec.record"), [
            ("", self.verified, t("tech.h.verified"), False),
            (t("tech.notes"), self.notes, t("tech.h.notes"), False),
            (t("tech.gas_flow"), self.flow, t("tech.h.gas_flow"), True),
            (t("tech.density"), self.density, t("tech.h.density"), True),
        ], expanded=False)
        for w in (self.lead_kind, self.lead_len, self.lead_ang, self.lead_rad, self.lout_kind, self.lout_len,
                  self.kerf_kind, self.kerf_w, self.mj_count, self.mj_len, self.sf_size, self.sf_lead,
                  self.corner_on, self.corner_angle, self.prefer, self.min_feature, self.clearance,
                  self.tip_strategy, self.flow, self.density, self.verified, self.notes):
            for sig in ("valueChanged", "currentIndexChanged", "toggled"):
                if hasattr(w, sig):
                    getattr(w, sig).connect(lambda *_a: self._edited())
                    break
            else:
                w.editingFinished.connect(self._edited)
        from mlaser.ui.homes import bind_editor

        for vid, w in (("lead_in.kind", self.lead_kind), ("lead_in.length", self.lead_len),
                       ("lead_in.angle_deg", self.lead_ang), ("lead_in.radius", self.lead_rad),
                       ("lead_out.kind", self.lout_kind), ("lead_out.length", self.lout_len),
                       ("kerf_comp", self.kerf_kind), ("kerf_width", self.kerf_w),
                       ("micro_joint.count", self.mj_count), ("micro_joint.length", self.mj_len)):
            bind_editor(w, f"recipe.craft.{vid}")
        for vid, w in (("rules.small_feature.max_size_mm", self.sf_size),
                       ("rules.small_feature.lead_length_mm", self.sf_lead),
                       ("rules.corner.cool_points", self.corner_on), ("rules.corner.max_angle_deg", self.corner_angle),
                       ("rules.lead_place.prefer", self.prefer), ("rules.lead_place.min_feature_mm", self.min_feature),
                       ("rules.lead_check.clearance_mm", self.clearance), ("rules.tip.strategy", self.tip_strategy),
                       ("gas_flow_l_min", self.flow), ("density_g_cm3", self.density),
                       ("verified", self.verified), ("notes", self.notes)):
            bind_editor(w, f"recipe.{vid}")
        self.form.apply_visibility()

    # ---------------------------------------------------------------- load / read
    def set_technology(self, tech: Optional[T.Technology]) -> None:
        self._loading = True
        try:
            self.tech = copy.deepcopy(tech)
            self.setEnabled(tech is not None)
            t = self.tr_.tr
            if tech is None:
                self.header.setText(t("tech.none"))
                self.badge.setText("")
                return
            self.header.setText(f"<b>{tech.key.label()}</b> · {t('tech.version')} {tech.version or '-'}")
            if tech.is_example:
                self.badge.setText(t("tech.example_badge"))
                self.badge.setProperty("chip", "warning")
                f = self.badge.font()
                f.setBold(True)
                self.badge.setFont(f)
            elif tech.verified:
                self.badge.setText(t("tech.verified_badge"))
            else:
                self.badge.setText("")
            self.badge.setVisible(bool(self.badge.text()))
            base = self.base_fn(tech) if self.base_fn is not None else None
            base_full = base.full_params() if base is not None else T.registry_defaults()
            label = (t("tech.base.factory") if base is not None and base.version != tech.version
                     else t("tech.base.this_version") if base is not None else t("tech.base.defaults"))
            self.form.set_values(tech.full_params(), base_full, label)
            cr = {**T.DEFAULT_CRAFT, **(tech.craft or {})}
            li = cr.get("lead_in") or {}
            self.lead_kind.setCurrentIndex(int(li.get("kind", 1)))
            self.lead_len.setValue(float(li.get("length", 3.0)))
            self.lead_ang.setValue(float(li.get("angle_deg", 90.0)))
            self.lead_rad.setValue(float(li.get("radius", 1.0)))
            lo = cr.get("lead_out") or {}
            self.lout_kind.setCurrentIndex(int(lo.get("kind", 0)))
            self.lout_len.setValue(float(lo.get("length", 0.0)))
            self.kerf_kind.setCurrentIndex(max(0, self.kerf_kind.findData(int(cr.get("kerf_comp", -1)))))
            self.kerf_w.setValue(float(cr.get("kerf_width", 0.0)))
            mj = cr.get("micro_joint") or {}
            self.mj_count.setValue(int(mj.get("count", 0)))
            self.mj_len.setValue(float(mj.get("length", 0.5)))
            sf = (tech.rules or {}).get("small_feature") or {}
            self.sf_size.setValue(float(sf.get("max_size_mm", 0.0)))
            self.sf_lead.setValue(float(sf.get("lead_length_mm", 1.0)))
            co = (tech.rules or {}).get("corner") or {}
            self.corner_on.setChecked(bool(co.get("cool_points", False)))
            self.corner_angle.setValue(float(co.get("max_angle_deg", 90.0)))
            from .cutting_strategy import recipe_rules
            r = recipe_rules(tech)
            self.prefer.setCurrentIndex(max(0, self.prefer.findData(r[("lead_place", "prefer")])))
            self.min_feature.setValue(float(r[("lead_place", "min_feature_mm")]))
            self.clearance.setValue(float(r[("lead_check", "clearance_mm")]))
            self.tip_strategy.setCurrentIndex(max(0, self.tip_strategy.findData(r[("tip", "strategy")])))
            self.flow.setValue(float(tech.gas_flow_l_min or 0.0))
            self.density.setValue(float(tech.density_g_cm3 or 0.0))
            self.verified.setChecked(bool(tech.verified))
            self.notes.setText(tech.notes)
            self._update_diagram()
        finally:
            self._loading = False

    def _update_diagram(self) -> None:
        lin = LeadParams(kind=self.lead_kind.currentIndex(), angle_deg=self.lead_ang.value(),
                         length=self.lead_len.value(), radius=self.lead_rad.value())
        lout = LeadParams(kind=self.lout_kind.currentIndex(), length=self.lout_len.value(),
                          angle_deg=self.lead_ang.value(), radius=self.lead_rad.value())
        self.diagram.set_leads(lin, lout)
        self.diagram.set_kerf(int(self.kerf_kind.currentData()), self.kerf_w.value())

    def _edited(self) -> None:
        if self._loading:
            return
        self._update_diagram()
        self.changed.emit()

    def current(self) -> T.Technology:
        """The record as edited (only values that differ from the registry defaults are kept,
        plus those the record already had)."""
        assert self.tech is not None
        defaults = T.registry_defaults()
        params = dict(self.tech.params)
        params.update(self.form.values())
        params = {k: v for k, v in params.items() if not T._same(v, defaults.get(k)) or k in self.tech.params}
        craft = copy.deepcopy(self.tech.craft or {})
        li = dict(craft.get("lead_in") or T.DEFAULT_CRAFT["lead_in"])
        li.update(kind=self.lead_kind.currentIndex(), length=self.lead_len.value(), angle_deg=self.lead_ang.value(),
                  radius=self.lead_rad.value())
        craft["lead_in"] = li
        lo = dict(craft.get("lead_out") or T.DEFAULT_CRAFT["lead_out"])
        if self.lout_kind.currentIndex() != int(lo.get("kind", 0)) or \
                not T._same(round(self.lout_len.value(), 6), float(lo.get("length", 0.0))):
            lo.update(kind=self.lout_kind.currentIndex(), length=round(self.lout_len.value(), 6))
        craft["lead_out"] = lo
        craft["kerf_comp"] = int(self.kerf_kind.currentData())
        craft["kerf_width"] = round(self.kerf_w.value(), 6)
        mj = dict(craft.get("micro_joint") or T.DEFAULT_CRAFT["micro_joint"])
        mj.update(count=self.mj_count.value(), length=round(self.mj_len.value(), 6))
        craft["micro_joint"] = mj
        rules = copy.deepcopy(self.tech.rules or {})
        sf = dict(rules.get("small_feature") or {})
        sf.update(max_size_mm=round(self.sf_size.value(), 6), lead_length_mm=round(self.sf_lead.value(), 6))
        sf.setdefault("lead_kind", 1)
        sf.setdefault("no_joints", True)
        rules["small_feature"] = sf
        rules["corner"] = {**(rules.get("corner") or {}), "cool_points": self.corner_on.isChecked(),
                           "max_angle_deg": round(self.corner_angle.value(), 6)}
        from .cutting_strategy import RULE_DEFAULTS
        new = {("lead_place", "prefer"): str(self.prefer.currentData()),
               ("lead_place", "min_feature_mm"): round(self.min_feature.value(), 6),
               ("lead_check", "clearance_mm"): round(self.clearance.value(), 6),
               ("tip", "strategy"): str(self.tip_strategy.currentData())}
        for (g, k), v in new.items():
            grp = dict(rules.get(g) or {})
            if k in grp or not T._same(v, RULE_DEFAULTS[(g, k)]):     # only what the record sets
                grp[k] = v
                rules[g] = grp
        flow = self.flow.value() or None
        dens = self.density.value() or None
        return replace(self.tech, params=params, craft=craft, rules=rules, gas_flow_l_min=flow,
                       density_g_cm3=dens, verified=self.verified.isChecked(), notes=self.notes.text())


# --------------------------------------------------------------------------- diff view
def change_rows(changes: list[T.Change], tr: Any) -> list[tuple[str, str, str, str, str]]:
    """(label, unit, old, new, key) per change, for the "what changed" view."""
    from mlaser.services import units
    from mlaser.services.units import kind_for_unit

    u = units.current()
    reg = T.layer_registry()
    out = []
    for c in changes:
        if c.is_param and c.key in reg:
            label = tr.tr_param(reg[c.key].lang)[1] or c.key
            unit = T.display_unit(c.key)
        else:
            label = tr.tr(f"tech.field.{c.key}", c.key)
            unit = "mm" if c.key.endswith(("width", "length", "_mm", "radius")) else ""
        kind = kind_for_unit(unit) if unit else None
        if kind is not None:                  # shown in the display units (Units track)
            unit = u.suffix(kind).strip()

        def fmt(v, k=kind):
            if v is None:
                return "—"
            if k is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
                return u.number(float(v), k, trim=True)
            return f"{v:g}" if isinstance(v, float) else str(v)
        out.append((label, unit, fmt(c.old), fmt(c.new), c.key))
    return out


# --------------------------------------------------------------------------- compare
class TechCompareDialog(QDialog):
    """Two technologies side by side: every parameter by section (the editor's sections and
    labels), the differing ones in bold; *Only differences* (on by default) and a search."""

    def __init__(self, tr: Any, store: T.TechnologyStore, a: Optional[T.Technology] = None,
                 b: Optional[T.Technology] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.store = store
        t = tr.tr
        self.setWindowTitle(t("tech.compare.title"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)
        intro = QLabel(t("tech.compare.intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self._all = store.all()
        pick = ResponsiveForm()
        self.a = QComboBox()
        self.b = QComboBox()
        for combo, cur in ((self.a, a), (self.b, b)):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            for tech in self._all:
                combo.addItem(tech.key.label(), tech.id)
            if cur is not None:
                i = combo.findData(cur.id)
                if i >= 0:
                    combo.setCurrentIndex(i)
            combo.currentIndexChanged.connect(lambda _i: self.refresh())
        if b is None and self.b.count() > 1 and self.b.currentIndex() == self.a.currentIndex():
            self.b.setCurrentIndex((self.a.currentIndex() + 1) % self.b.count())
        pick.addRow(QLabel(t("tech.compare.a")), self.a)
        pick.addRow(QLabel(t("tech.compare.b")), self.b)
        lay.addWidget(pick)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(t("techform.search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh())
        row.addWidget(self.search, 1)
        self.only_diff = QCheckBox(t("tech.compare.only_diff"))
        self.only_diff.setChecked(True)
        self.only_diff.toggled.connect(lambda _v: self.refresh())
        row.addWidget(self.only_diff)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([t("process.diff.param"), "A", "B"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.ElideRight)
        lay.addWidget(self.tree, 1)
        self.summary = QLabel()
        lay.addWidget(self.summary)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        lay.addWidget(bb)
        self._form = TechForm(tr)          # labels, sections and formatting of every parameter
        self._form.hide()
        self.resize(self.sizeHint().expandedTo(self.minimumSizeHint()))
        self.refresh()

    def techs(self) -> tuple[Optional[T.Technology], Optional[T.Technology]]:
        def get(combo):
            tid = combo.currentData()
            return self.store.get(int(tid)) if tid is not None else None
        return get(self.a), get(self.b)

    def rows(self) -> list[tuple[str, str, str, str, bool]]:
        """(section, label, value A, value B, differs) of the shown parameters."""
        ta, tb = self.techs()
        if ta is None or tb is None:
            return []
        fa, fb = ta.full_params(), tb.full_params()
        q = self.search.text().strip().lower()
        out = []
        sec_title = {sid: sec.header.text() for sid, sec in self._form.sections.items()}
        for k, f in self._form.fields.items():
            va, vb = fa.get(k), fb.get(k)
            differs = not T._same(va, vb)
            if self.only_diff.isChecked() and not differs:
                continue
            if q and not f.matches(q):
                continue
            label = f.label_text if f.stage is None else f"{self.tr_.format('techform.stage', n=f.stage + 1)}: {f.label_text}"
            out.append((sec_title.get(f.section, f.section), label, f.text_value(va), f.text_value(vb), differs))
        for name, a_v, b_v in self._craft_rows(ta, tb):
            differs = a_v != b_v
            if (self.only_diff.isChecked() and not differs) or (q and q not in name.lower()):
                continue
            out.append((sec_title.get("leads", ""), name, a_v, b_v, differs))
        return out

    def _craft_rows(self, ta: T.Technology, tb: T.Technology) -> list[tuple[str, str, str]]:
        t = self.tr_.tr
        out = []
        ca, cb = {**T.DEFAULT_CRAFT, **(ta.craft or {})}, {**T.DEFAULT_CRAFT, **(tb.craft or {})}
        from mlaser.services import units

        u = units.current()
        for label, get in ((t("tech.lead_length"),
                            lambda c: u.fmt(float((c.get('lead_in') or {}).get('length', 0)), "length", trim=True)),
                           (t("tech.kerf_width"), lambda c: u.fmt(float(c.get('kerf_width', 0)), "small_length",
                                                                  trim=True)),
                           (t("tech.joint_count"), lambda c: str((c.get('micro_joint') or {}).get('count', 0)))):
            out.append((label, get(ca), get(cb)))
        return out

    def refresh(self) -> None:
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        n = 0
        for sec, label, va, vb, differs in self.rows():
            g = groups.get(sec)
            if g is None:
                g = groups[sec] = QTreeWidgetItem([sec])
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setFirstColumnSpanned(True)
                self.tree.addTopLevelItem(g)
                g.setExpanded(True)
            it = QTreeWidgetItem([label, va, vb])
            it.setToolTip(0, label)
            if differs:
                n += 1
                for c in range(3):
                    f = it.font(c)
                    f.setBold(True)
                    it.setFont(c, f)
            g.addChild(it)
        for c in (1, 2):
            self.tree.resizeColumnToContents(c)
        self.summary.setText(self.tr_.format("tech.compare.count", n=n))


# --------------------------------------------------------------------------- library page
class LibraryPage(QWidget):
    """Process › Library (SETTINGS_MAP H2): the technology records.  The record list (material ›
    thickness › gas · nozzle, search) and the editor of the selected record with its history and
    "what changed"; Save version / Save as new / Revert to factory, Import / Export, Compare.
    Next to each other when the page is wide enough, one above the other when not."""

    saved = Signal(object)          # a new version / record was stored

    def __init__(self, ctx: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from mlaser.ui.homes import bind_transient

        self.ctx, self.store, self.tr_ = ctx, store, ctx.tr
        self.setObjectName("process_library")
        t = self.tr_.tr
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        root.addWidget(self.split)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(t("library.search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.refresh_tree())
        bind_transient(self.search, "view.search")
        lv.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setObjectName("library_tree")
        self.tree.setHeaderHidden(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setTextElideMode(Qt.ElideRight)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._tree_picked(cur))
        lv.addWidget(self.tree, 1)
        brow = QWidget()
        bfl = FlowLayout(brow, justify=False, hspacing=6, vspacing=6)
        for key, fn in (("tech.import", self.import_file), ("tech.export", self.export_file),
                        ("tech.compare", self.compare)):
            b = QPushButton(t(key))
            b.clicked.connect(lambda _c=False, f=fn: f())
            bfl.addWidget(b)
        lv.addWidget(brow)
        self.split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.used_by = QLabel()
        self.used_by.setWordWrap(True)
        rv.addWidget(self.used_by)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        unit = int(ctx.config.get("UN.GasPressureUnit", 0) or 0) if getattr(ctx, "config", None) is not None else 0
        self.editor = TechnologyEditor(self.tr_, base_fn=self._factory_of, pressure_unit=unit)
        self.editor.changed.connect(self._editor_changed)
        ed = QWidget()
        el = QVBoxLayout(ed)
        el.setContentsMargins(0, 4, 0, 0)
        el.addWidget(self.editor, 1)
        self.comment = QLineEdit()
        self.comment.setPlaceholderText(t("tech.comment"))
        self.comment.setAccessibleName(t("tech.comment"))
        bind_transient(self.comment, "library.comment")
        el.addWidget(self.comment)
        arow = QWidget()
        afl = FlowLayout(arow, justify=False, hspacing=6, vspacing=6)
        self.save_btn = QPushButton(t("tech.save"))
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save_version)
        afl.addWidget(self.save_btn)
        for key, fn in (("tech.save_new", self.save_as_new), ("tech.revert", self.revert_factory)):
            b = QPushButton(t(key))
            b.clicked.connect(lambda _c=False, f=fn: f())
            afl.addWidget(b)
        el.addWidget(arow)
        tabs.addTab(ed, t("library.tab.values"))
        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(0, 4, 0, 0)
        df = ResponsiveForm()
        self.diff_mode = QComboBox()
        self.diff_mode.addItem(t("process.diff.factory"), "factory")
        self.diff_mode.addItem(t("process.diff.layer"), "layer")
        self.diff_mode.addItem(t("process.diff.unsaved"), "unsaved")
        self.diff_mode.setAccessibleName(t("process.diff.mode"))
        self.diff_mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.diff_mode.setMinimumContentsLength(8)
        self.diff_mode.currentIndexChanged.connect(lambda _i: self.refresh_diff())
        bind_transient(self.diff_mode, "view.diff_mode")
        df.addRow(QLabel(t("process.diff.mode")), self.diff_mode)
        dl.addWidget(df)
        self.diff = QTreeWidget()
        self.diff.setColumnCount(4)
        self.diff.setHeaderLabels([t("process.diff.param"), t("process.diff.unit"), t("process.diff.old"),
                                   t("process.diff.new")])
        self.diff.setRootIsDecorated(False)
        self.diff.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.diff.header().setStretchLastSection(False)
        self.diff.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        dl.addWidget(self.diff, 1)
        self.diff_summary = QLabel()
        dl.addWidget(self.diff_summary)
        tabs.addTab(dw, t("process.tab.changes"))
        hw = QWidget()
        hl = QVBoxLayout(hw)
        hl.setContentsMargins(0, 4, 0, 0)
        self.history = QTreeWidget()
        self.history.setColumnCount(3)
        self.history.setHeaderLabels([t("tech.version"), t("tech.date"), t("tech.comment")])
        self.history.setRootIsDecorated(False)
        self.history.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.history.header().setStretchLastSection(False)
        self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        hl.addWidget(self.history, 1)
        bb = QPushButton(t("tech.restore_version"))
        bb.clicked.connect(self.restore_version)
        rl = QHBoxLayout()
        rl.addStretch(1)
        rl.addWidget(bb)
        hl.addLayout(rl)
        tabs.addTab(hw, t("process.tab.history"))
        self.tabs = tabs
        rv.addWidget(tabs, 1)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 3)
        self.current_layer: Optional[int] = None      # for "layer vs recipe"
        self._all: list[T.Technology] = []
        self.reload()

    # ---------------------------------------------------------------- layout
    def resizeEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().resizeEvent(ev)
        # side by side while the list's natural width and the editor's minimum fit
        need = self.split.widget(0).sizeHint().width() + self.split.widget(1).minimumSizeHint().width() + 24
        want = Qt.Vertical if self.width() < need else Qt.Horizontal
        if self.split.orientation() != want:
            self.split.setOrientation(want)

    # ---------------------------------------------------------------- records
    def _factory_of(self, tech: T.Technology) -> Optional[T.Technology]:
        if tech.id is None:
            return None
        try:
            return self.store.factory(tech.id)
        except KeyError:
            return None

    def reload(self, select: Optional[T.Technology] = None) -> None:
        cur = select or self.editor.tech
        self._all = self.store.all()
        self.refresh_tree()
        if cur is not None:
            self.select(cur.key)
        elif self._all:
            self.select(self._all[0].key)

    def refresh_tree(self) -> None:
        q = self.search.text().strip().lower()
        cur = self.selected()
        self.tree.blockSignals(True)
        self.tree.clear()
        mats: dict[str, QTreeWidgetItem] = {}
        ths: dict[tuple, QTreeWidgetItem] = {}
        for tech in self._all:
            k = tech.key
            if q and q not in k.label().lower():
                continue
            m = mats.get(k.material)
            if m is None:
                m = mats[k.material] = QTreeWidgetItem([k.material])
                self.tree.addTopLevelItem(m)
                m.setExpanded(True)
            th = ths.get((k.material, k.thickness_mm))
            if th is None:
                th = ths[(k.material, k.thickness_mm)] = QTreeWidgetItem([thickness_label(k.thickness_mm)])
                m.addChild(th)
                th.setExpanded(True)
            it = QTreeWidgetItem([f"{k.gas} · {_nz_label(_nz(k.nozzle, k.power_w))}" +
                                  (f" · v{tech.version}" if tech.version else "")])
            it.setData(0, Qt.UserRole, tech.id)
            it.setToolTip(0, k.label())
            th.addChild(it)
        self.tree.blockSignals(False)
        if cur is not None:
            self.select(cur.key)

    def _items(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            m = self.tree.topLevelItem(i)
            for j in range(m.childCount()):
                th = m.child(j)
                for k in range(th.childCount()):
                    out.append(th.child(k))
        return out

    def select(self, key: T.TechKey) -> None:
        tech = self.store.find(key)
        if tech is None:
            return
        for it in self._items():
            if it.data(0, Qt.UserRole) == tech.id:
                self.tree.setCurrentItem(it)
                break
        self._show(tech)

    def selected(self) -> Optional[T.Technology]:
        return self.editor.tech

    def _tree_picked(self, it) -> None:
        if it is None or it.data(0, Qt.UserRole) is None:
            return
        try:
            self._show(self.store.get(int(it.data(0, Qt.UserRole))))
        except KeyError:
            pass

    def _show(self, tech: Optional[T.Technology]) -> None:
        if self.editor.tech is not None and tech is not None and self.editor.tech.id == tech.id and \
                self.editor.tech.version == tech.version:
            return
        self.editor.set_technology(tech)
        self.refresh_used_by()
        self.refresh_history()
        self.refresh_diff()

    def refresh_used_by(self) -> None:
        tech = self.editor.tech
        if tech is None:
            self.used_by.setText("")
            return
        used = [li for li, lp in sorted(self.ctx.document.layers.items())
                if (T.layer_technology_ref(lp) or {}).get("id") == tech.id]
        self.used_by.setText(self.tr_.format("library.used_by", layers=", ".join(map(str, used))) if used
                             else self.tr_.tr("library.unused"))

    # ---------------------------------------------------------------- editor actions
    def _editor_changed(self) -> None:
        if self.diff_mode.currentData() == "unsaved":
            self.refresh_diff()

    def save_version(self) -> Optional[T.Technology]:
        if self.editor.tech is None:
            return None
        try:
            saved = self.store.save(self.editor.current(), self.comment.text().strip())
        except (ValueError, KeyError) as e:
            QMessageBox.warning(self, self.tr_.tr("tech.save"), str(e))
            return None
        self.comment.clear()
        self.editor.tech = None
        self.reload(select=saved)
        self.saved.emit(saved)
        return saved

    def save_as_new(self) -> Optional[T.Technology]:
        if self.editor.tech is None:
            return None
        cur = self.editor.current()
        if getattr(self.ctx.window, "_test_no_dialogs", False):
            text, ok = cur.key.material + " *", True
        else:
            text, ok = QInputDialog.getText(self, self.tr_.tr("tech.save_new"), self.tr_.tr("tech.new_material"),
                                            text=cur.key.material + " *")
        if not ok or not text:
            return None
        new = replace(cur, key=replace(cur.key, material=text), id=None, version=0, origin="user", verified=False)
        if new.notes == T.EXAMPLE_NOTE:
            new = replace(new, notes="")
        saved = self.store.save(new, self.tr_.tr("tech.created"))
        self.editor.tech = None
        self.reload(select=saved)
        self.saved.emit(saved)
        return saved

    def revert_factory(self) -> Optional[T.Technology]:
        tech = self.editor.tech
        if tech is None or tech.id is None:
            return None
        saved = self.store.revert(tech.id)
        self.editor.tech = None
        self.reload(select=saved)
        self.saved.emit(saved)
        return saved

    def restore_version(self) -> Optional[T.Technology]:
        tech = self.editor.tech
        it = self.history.currentItem()
        if tech is None or tech.id is None or it is None:
            return None
        saved = self.store.revert(tech.id, int(it.data(0, Qt.UserRole)))
        self.editor.tech = None
        self.reload(select=saved)
        self.saved.emit(saved)
        return saved

    def import_file(self) -> list:
        path, _ = QFileDialog.getOpenFileName(self, self.tr_.tr("tech.import"), "",
                                              "Technology (*.json *.xml);;JSON (*.json);;LayerPara / craft XML (*.xml)")
        if not path:
            return []
        try:
            got = self.store.import_xml(path) if path.lower().endswith(".xml") else self.store.import_json(path)
        except Exception as e:  # noqa: BLE001 - shown to the user
            QMessageBox.warning(self, self.tr_.tr("tech.import"), str(e))
            return []
        self.ctx.log(self.tr_.format("tech.imported", n=len(got), file=path))
        self.reload(select=got[0] if got else None)
        return got

    def export_file(self) -> Optional[str]:
        path, _ = QFileDialog.getSaveFileName(self, self.tr_.tr("tech.export"), "technology.json",
                                              "JSON (*.json);;LayerPara XML (*.xml)")
        if not path:
            return None
        if path.lower().endswith(".xml"):
            tech = self.editor.current() if self.editor.tech is not None else None
            self.store.export_xml(path, [tech] if tech is not None else self.store.all())
        else:
            self.store.export_json(path)
        self.ctx.log(self.tr_.format("tech.exported", file=path))
        return path

    def compare(self) -> TechCompareDialog:
        d = TechCompareDialog(self.tr_, self.store, self.editor.tech, None, self)
        if not getattr(self.ctx.window, "_test_no_dialogs", False):
            d.open()
        return d

    # ---------------------------------------------------------------- history / diff
    def refresh_history(self) -> None:
        self.history.clear()
        tech = self.editor.tech
        if tech is None or tech.id is None:
            return
        for h in reversed(self.store.history(tech.id)):
            it = QTreeWidgetItem([str(h.version), time.strftime("%Y-%m-%d %H:%M", time.localtime(h.created)),
                                  h.comment])
            it.setData(0, Qt.UserRole, h.version)
            self.history.addTopLevelItem(it)

    def changes(self) -> list[T.Change]:
        mode = self.diff_mode.currentData()
        tech = self.editor.tech
        if mode == "layer":
            li = self.current_layer
            lp = self.ctx.document.layers.get(li) if li is not None else None
            from mlaser.services import inheritance as I
            base = I.recipe_of(self.ctx.document, li, self.store) if li is not None else None
            return T.layer_changes(lp, base) if (lp is not None and base is not None) else []
        if tech is None:
            return []
        if mode == "unsaved":
            return T.diff(tech, self.editor.current())
        if tech.id is None:
            return T.diff(None, tech)
        return T.diff(self.store.factory(tech.id), tech)

    def refresh_diff(self) -> None:
        self.diff.clear()
        ch = self.changes()
        for label, unit, old, new, key in change_rows(ch, self.tr_):
            it = QTreeWidgetItem([label, unit, old, new])
            it.setToolTip(0, key)
            self.diff.addTopLevelItem(it)
        for c in range(1, 4):
            self.diff.resizeColumnToContents(c)
        self.diff_summary.setText(self.tr_.format("process.diff.count", n=len(ch)))


# --------------------------------------------------------------------------- workspace
class ProcessWorkspace(QWidget):
    """The Process workspace: **Drawing** (the drawing's layers and the layer inspector, the
    cutting-strategy checks and the estimate) and **Library** (the technology records).  See the
    module docstring."""

    def __init__(self, ctx: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QButtonGroup, QFrame, QScrollArea

        from mlaser.ui.responsive import CurrentPageStack

        from mlaser.ui.preview_scrubber import CostCardWidget

        from .layer_inspector import LayerInspector

        self.ctx = ctx
        self.store = store
        self.tr_ = ctx.tr
        self.setObjectName("process_workspace")
        t = self.tr_.tr
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        # ---- Drawing | Library
        sw = QHBoxLayout()
        sw.setSpacing(0)
        self.page_buttons = QButtonGroup(self)
        self.page_buttons.setExclusive(True)
        for i, key in enumerate(("process.page.drawing", "process.page.library")):
            b = QToolButton()
            b.setText(t(key))
            b.setCheckable(True)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.setObjectName(f"process_page_{i}")
            self.page_buttons.addButton(b, i)
            sw.addWidget(b)
        # the layout harness visits the Library page too (tests/test_responsive_layout.py)
        self.page_buttons.button(1).setProperty("responsive_views", True)
        self.page_buttons.idToggled.connect(lambda i, on: self.show_page(i) if on else None)
        root.addLayout(sw)
        self.pages = CurrentPageStack()       # sized by the page shown (the Library is taller)
        root.addWidget(self.pages, 1)
        # ---- Drawing page
        drawing = QWidget()
        dv = QVBoxLayout(drawing)
        dv.setContentsMargins(0, 0, 0, 0)
        self.outdated = QWidget()
        ol = QHBoxLayout(self.outdated)
        ol.setContentsMargins(0, 0, 0, 0)
        self.outdated_label = QLabel()
        self.outdated_label.setWordWrap(True)
        ol.addWidget(self.outdated_label, 1)
        self.update_btn = QPushButton(t("process.update_layers"))
        self.update_btn.clicked.connect(self.update_outdated)
        ol.addWidget(self.update_btn)
        self.outdated.hide()
        dv.addWidget(self.outdated)
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        dv.addWidget(split, 1)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        lt = QLabel(t("process.layers"))
        f = lt.font()
        f.setBold(True)
        lt.setFont(f)
        ll.addWidget(lt)
        self.layers = QTableWidget(0, 3)
        self.layers.setHorizontalHeaderLabels([t("process.col.layer"), t("process.col.technology"),
                                               t("process.col.state")])
        hh = self.layers.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.layers.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.layers.setTextElideMode(Qt.ElideRight)
        self.layers.verticalHeader().hide()
        self.layers.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.layers.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layers.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layers.itemSelectionChanged.connect(self._layer_selected)
        ll.addWidget(self.layers, 1)
        split.addWidget(lw)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        self.inspector = LayerInspector(ctx, store)
        self.inspector.changed.connect(self._inspector_changed)
        tabs.addTab(self.inspector, t("process.tab.layer"))
        self.card = CostCardWidget(self.tr_)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(self.card)
        tabs.addTab(sa, t("process.tab.estimate"))
        self.tabs = tabs                       # the Cutting strategy tab is added by ext_process
        split.addWidget(tabs)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        self.pages.addWidget(drawing)
        # ---- Library page
        self.library = LibraryPage(ctx, store)
        self.library.saved.connect(lambda _t: self.refresh_layers())
        self.pages.addWidget(self.library)
        self.editor = self.library.editor
        self.diff_mode = self.library.diff_mode
        self.current_layer: Optional[int] = None
        self.show_page(0)
        self.reload()

    # ---------------------------------------------------------------- pages
    def show_page(self, i: int) -> None:
        """0: Drawing (beside the canvas), 1: Library (the whole width of the workspace: the
        canvas makes room; it comes back with the Drawing page)."""
        self.pages.setCurrentIndex(int(i))
        b = self.page_buttons.button(int(i))
        if b is not None and not b.isChecked():
            b.setChecked(True)
        self._canvas_room(int(i) == 0)

    def _canvas_room(self, canvas: bool) -> None:
        page = self.parentWidget()
        while page is not None and not hasattr(page, "canvas_slot"):
            page = page.parentWidget()
        slot = getattr(page, "canvas_slot", None) if page is not None else None
        if slot is not None and slot.isHidden() == canvas:
            slot.setVisible(canvas)

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().showEvent(ev)
        self._canvas_room(self.pages.currentIndex() == 0)

    # ---------------------------------------------------------------- proxies (picker, library)
    @property
    def material(self) -> QComboBox:
        return self.inspector.picker.material

    @property
    def thickness(self) -> QComboBox:
        return self.inspector.picker.thickness

    @property
    def gas(self) -> QComboBox:
        return self.inspector.picker.gas

    @property
    def nozzle(self) -> QComboBox:
        return self.inspector.picker.nozzle

    def picked(self) -> Optional[T.Technology]:
        return self.inspector.picker.picked()

    def pick(self, key: T.TechKey) -> None:
        """Choose a record: in the layer inspector's recipe picker and in the Library."""
        self.inspector.picker.pick(key)
        self.library.select(key)

    def reload(self, select: Optional[T.Technology] = None) -> None:
        self.inspector.picker.reload(select)
        self.library.reload(select)
        self.refresh_layers()

    def save_version(self) -> Optional[T.Technology]:
        return self.library.save_version()

    def save_as_new(self) -> Optional[T.Technology]:
        return self.library.save_as_new()

    def revert_factory(self) -> Optional[T.Technology]:
        return self.library.revert_factory()

    def restore_version(self) -> Optional[T.Technology]:
        return self.library.restore_version()

    def changes(self) -> list[T.Change]:
        self.library.current_layer = self.current_layer
        return self.library.changes()

    def refresh_diff(self) -> None:
        self.library.current_layer = self.current_layer
        self.library.refresh_diff()

    def compare(self) -> TechCompareDialog:
        return self.library.compare()

    # ---------------------------------------------------------------- layers
    def layers_in_use(self) -> list[int]:
        doc = self.ctx.document
        return sorted({p.craft.layer for p in doc.parts})

    def refresh_layers(self) -> None:
        from mlaser.ui.canvas import layer_color
        from mlaser.ui.properties.panel import _swatch

        t = self.tr_.tr
        used = self.layers_in_use()
        keep = self.current_layer
        self.layers.blockSignals(True)
        self.layers.setRowCount(0)
        outdated = []
        for li in used:
            r = self.layers.rowCount()
            self.layers.insertRow(r)
            it = QTableWidgetItem(_swatch(layer_color(li)), f"{t('props.layer')} {li}")
            it.setData(Qt.UserRole, li)
            self.layers.setItem(r, 0, it)
            name, state = self.layer_state(li)
            self.layers.setItem(r, 1, QTableWidgetItem(name))
            si = QTableWidgetItem(state)
            si.setToolTip(state)
            self.layers.setItem(r, 2, si)
            self.layers.item(r, 1).setToolTip(name)
            if self._newer(li):
                outdated.append(li)
        self.layers.blockSignals(False)
        self.outdated.setVisible(bool(outdated))
        self.outdated_label.setText(self.tr_.format("process.outdated", layers=", ".join(map(str, outdated))))
        self._outdated = outdated
        if keep in used:
            self.select_layer(keep)
        elif used:
            self.select_layer(used[0])
        else:
            self.current_layer = None
            self.inspector.set_layer(None)
        self.library.refresh_used_by()
        self.refresh_diff()

    def _newer(self, layer: int) -> Optional[T.Technology]:
        ref = T.layer_technology_ref(self.ctx.document.layers.get(layer))
        if not ref or ref.get("id") is None:
            return None
        try:
            head = self.store.get(int(ref["id"]))
        except KeyError:
            return None
        return head if ref.get("version") and head.version > int(ref["version"]) else None

    def update_outdated(self) -> int:
        """Layers that follow an older version of their recipe follow the newest one (their own
        values are kept): one undo step per recipe."""
        n = 0
        by: dict[int, tuple[T.Technology, list[int]]] = {}
        for li in list(getattr(self, "_outdated", [])):
            head = self._newer(li)
            if head is not None:
                by.setdefault(head.id or 0, (head, []))[1].append(li)
        for head, lis in by.values():
            self.inspector.assign(head, lis)
            n += len(lis)
        self.refresh_layers()
        return n

    def technology_name(self, layer: int) -> str:
        ref = T.layer_technology_ref(self.ctx.document.layers.get(layer))
        if not ref:
            return ""
        k = ref.get("key") or {}
        try:
            return T.TechKey(**k).label() + (f" · v{ref.get('version')}" if ref.get("version") else "")
        except TypeError:
            return str(k)

    def layer_state(self, layer: int) -> tuple[str, str]:
        from mlaser.services import inheritance as I

        t = self.tr_.tr
        lp = self.ctx.document.layers.get(layer)
        ref = T.layer_technology_ref(lp)
        if lp is None or (lp.extra or {}).get("placeholder"):
            return t("process.no_technology"), t("process.state.undefined")
        if ref is None:
            return t("process.no_technology"), t("process.state.manual")
        tech = I.recipe_of(self.ctx.document, layer, self.store)
        if tech is None:
            return self.technology_name(layer), t("process.state.missing")
        n = len(I.layer_overrides(lp)) if I.OVERRIDES in (lp.extra or {}) else len(T.layer_changes(lp, tech))
        state = self.tr_.format("process.state.modified", n=n) if n else t("process.state.as_applied")
        if ref.get("example"):
            state += " · " + t("process.state.example")
        head = self._newer(layer)
        if head is not None:
            state += " · " + self.tr_.format("process.state.newer", v=head.version)
        return self.technology_name(layer), state

    def select_layer(self, layer: int) -> None:
        for r in range(self.layers.rowCount()):
            if self.layers.item(r, 0).data(Qt.UserRole) == layer:
                self.layers.selectRow(r)
                self.current_layer = layer
                self.inspector.set_layer(layer)
                return

    def _layer_selected(self) -> None:
        rows = self.layers.selectionModel().selectedRows() if self.layers.selectionModel() else []
        if not rows:
            return
        li = self.layers.item(rows[0].row(), 0).data(Qt.UserRole)
        self.current_layer = li
        self.inspector.set_layer(li)
        self.refresh_diff()

    def _inspector_changed(self) -> None:
        self.refresh_layers()

    # ---------------------------------------------------------------- assign (was: apply)
    def apply_selected(self) -> int:
        """``process.apply``: assign the picked recipe to the current layer (all layers in use
        when none is current)."""
        tech = self.picked()
        if tech is None:
            return 0
        layers = [self.current_layer] if self.current_layer is not None else self.layers_in_use()
        return self.apply(tech, layers)

    def apply(self, tech: T.Technology, layers: list[int], leads: Optional[bool] = None, kerf: Optional[bool] = None,
              joints: Optional[bool] = None, rules: Optional[bool] = None) -> int:
        """Assign ``tech`` to ``layers`` (SETTINGS_MAP *Assign recipe*; ``leads`` / ``kerf`` are
        no longer options: the parts inherit the recipe's leads and kerf unless they set their
        own).  ``joints`` / ``rules``: also generate the record's micro-joints / rules."""
        if not layers:
            return 0
        res = self.inspector.assign(tech, layers, joints=bool(joints), rules=bool(rules))
        self.refresh_layers()
        return len(res.get("layers", [])) if res else 0

    def save_layer_as_technology(self) -> Optional[T.Technology]:
        saved = self.inspector.save_as_recipe()
        if saved is not None:
            self.reload(select=saved)
        return saved


# --------------------------------------------------------------------------- "Edit in …"
def _workspace(window: Any) -> Optional[ProcessWorkspace]:
    ext = window.extension("ext_process") if hasattr(window, "extension") else None
    return getattr(ext, "workspace", None)


def _resolve_layer(window: Any, vid: str, target: Any) -> Optional[QWidget]:
    ws = _workspace(window)
    if ws is None:
        return None
    ws.show_page(0)
    if isinstance(target, int):
        ws.select_layer(target)
    ws.tabs.setCurrentWidget(ws.inspector)
    from mlaser.ui.homes import editors
    found = editors(ws.inspector, vid)
    return found[0] if found else None


def _resolve_recipe(window: Any, vid: str, target: Any) -> Optional[QWidget]:
    ws = _workspace(window)
    if ws is None:
        return None
    ws.show_page(1)
    if isinstance(target, T.Technology):
        ws.library.select(target.key)
    ws.library.tabs.setCurrentIndex(0)
    from mlaser.ui.homes import editors
    found = editors(ws.library, vid)
    return found[0] if found else None


def _register_resolvers() -> None:
    from mlaser.ui import homes
    if not any(p == "layer." for p, _f in homes._RESOLVERS):
        homes.register_resolver("layer.", _resolve_layer)
        homes.register_resolver("recipe.", _resolve_recipe)


_register_resolvers()
