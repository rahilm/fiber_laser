"""Process workspace (ux.md P1-2, §4.2): technology per layer, the technology editor, "what
changed", and the time / cost estimate.

* **Material × thickness**: one row per material with a chip per thickness in the store; the
  gas and nozzle of the chosen pair below (cascading over the records), then *Apply* to the
  chosen layer or to every layer in use, optionally with the record's craft defaults (leads,
  kerf, micro-joints) and rules.  Applying is one ``PluginContext.edit``.
* **Layers**: every layer in use with its technology and its state (example values, modified
  after applying, not set).  *Save layer as technology* stores a hand-tuned layer as a record.
* **Technology editor** (:class:`mlaser.ui.techform.TechForm`): the parameters in sections, the
  important few first (Cutting, Piercing, Gas and nozzle, Height follow, Leads and kerf with the
  lead / kerf diagram, Corners and small features, About), every other parameter under
  *Advanced*; search; a mark on each value that differs from the factory version.  *Save* makes
  a new version, *Save as new* a new record; *Revert to factory*; *Compare…* shows two records
  side by side.
* **What changed**: the record against its factory version (or the registry defaults), or the
  layer against the record it was applied from; **History**: the versions.
* **Estimate**: the time and cost card of the current plan.

One tab level and one scroll area (the editor's): the panel adapts to the width it is given
(rows wrap label-above-field, chips and options wrap to new lines).

Records live in :class:`mlaser.services.technology.TechnologyStore`; this module is only the UI.
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

from mlaser.model import LayerParams, LeadParams
from mlaser.services import technology as T
from mlaser.ui.quantity import quantity
from mlaser.ui.techform import FlowLayout, ResponsiveForm, Section, TechForm


def _nz(nozzle: str, power_w: int) -> str:
    return f"{nozzle}\x1f{int(power_w)}"


def _nz_split(v: str) -> tuple[str, int]:
    name, _, p = str(v).rpartition("\x1f")
    return name, int(p or 0)


def _nz_label(v: str) -> str:
    name, p = _nz_split(v)
    return name + (f" · {p / 1000:g} kW" if p else "")


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
                  self.corner_on, self.corner_angle, self.flow, self.density, self.verified, self.notes):
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
        flow = self.flow.value() or None
        dens = self.density.value() or None
        return replace(self.tech, params=params, craft=craft, rules=rules, gas_flow_l_min=flow,
                       density_g_cm3=dens, verified=self.verified.isChecked(), notes=self.notes.text())


# --------------------------------------------------------------------------- diff view
def change_rows(changes: list[T.Change], tr: Any) -> list[tuple[str, str, str, str, str]]:
    """(label, unit, old, new, key) per change, for the "what changed" view."""
    reg = T.layer_registry()
    out = []
    for c in changes:
        if c.is_param and c.key in reg:
            label = tr.tr_param(reg[c.key].lang)[1] or c.key
            unit = T.display_unit(c.key)
        else:
            label = tr.tr(f"tech.field.{c.key}", c.key)
            unit = "mm" if c.key.endswith(("width", "length", "_mm", "radius")) else ""
        fmt = lambda v: "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))  # noqa: E731
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
        pick.form.addRow(QLabel(t("tech.compare.a")), self.a)
        pick.form.addRow(QLabel(t("tech.compare.b")), self.b)
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
        for label, get in ((t("tech.lead_length"), lambda c: f"{(c.get('lead_in') or {}).get('length', 0):g} mm"),
                           (t("tech.kerf_width"), lambda c: f"{c.get('kerf_width', 0):g} mm"),
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


# --------------------------------------------------------------------------- picker
class MaterialPicker(QWidget):
    """Material × thickness: one line per material, a check chip per thickness the store has
    (chips wrap to new lines in a narrow panel)."""

    picked = Signal(str, float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("material_picker")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(4)
        self.chips: dict[tuple[str, float], QToolButton] = {}
        self._rows: list[QWidget] = []

    def set_records(self, techs: list[T.Technology]) -> None:
        for w in self._rows:
            w.setParent(None)
            w.deleteLater()
        self._rows = []
        self.chips = {}
        by: dict[str, set] = {}
        for t in techs:
            by.setdefault(t.key.material, set()).add(t.key.thickness_mm)
        for m in sorted(by):
            row = QWidget()
            fl = FlowLayout(row, 4)
            lab = QLabel(f"<b>{m}</b>")
            lab.setTextFormat(Qt.RichText)
            fl.addWidget(lab)
            for th in sorted(by[m]):
                b = QToolButton()
                b.setText(f"{th:g} mm")
                b.setCheckable(True)
                b.setAutoRaise(False)
                b.setObjectName("material_chip")
                b.setAccessibleName(f"{m} {th:g} mm")
                b.clicked.connect(lambda _c=False, mm=m, tt=th: self.picked.emit(mm, tt))
                fl.addWidget(b)
                self.chips[(m, th)] = b
            self.lay.addWidget(row)
            self._rows.append(row)

    def set_current(self, material: Optional[str], thickness: Optional[float]) -> None:
        for (m, th), b in self.chips.items():
            b.setChecked(m == material and thickness is not None and abs(th - thickness) < 1e-9)


# --------------------------------------------------------------------------- workspace
class ProcessWorkspace(QWidget):
    """The Process workspace (see the module docstring)."""

    def __init__(self, ctx: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = store
        self.tr_ = ctx.tr
        self.setObjectName("process_workspace")
        t = self.tr_.tr
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        # ---- choose and apply
        pick_body = QWidget()
        pv = QVBoxLayout(pick_body)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(4)
        self.matrix = MaterialPicker()
        self.matrix.picked.connect(self._matrix_picked)
        pv.addWidget(self.matrix)
        pf = ResponsiveForm()
        self.material = QComboBox()
        self.thickness = QComboBox()
        self.gas = QComboBox()
        self.nozzle = QComboBox()
        for key, w in (("process.material", self.material), ("process.thickness", self.thickness),
                       ("process.gas", self.gas), ("process.nozzle", self.nozzle)):
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(8)
            lab = QLabel(t(key))
            lab.setBuddy(w)
            w.setAccessibleName(t(key))
            pf.form.addRow(lab, w)
        # material and thickness come from the chips; the combos stay for keyboard use
        pf.form.setRowVisible(self.material, False)
        pf.form.setRowVisible(self.thickness, False)
        self.material.currentIndexChanged.connect(lambda _i: self._cascade(1))
        self.thickness.currentIndexChanged.connect(lambda _i: self._cascade(2))
        self.gas.currentIndexChanged.connect(lambda _i: self._cascade(3))
        self.nozzle.currentIndexChanged.connect(lambda _i: self._picked())
        self.target = QComboBox()
        self.target.setAccessibleName(t("process.apply_to"))
        self.target.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.target.setMinimumContentsLength(8)
        lab = QLabel(t("process.apply_to"))
        lab.setBuddy(self.target)
        pf.form.addRow(lab, self.target)
        self.pick_form = pf
        pv.addWidget(pf)
        opts = QWidget()
        fl = FlowLayout(opts, 8)
        self.with_leads = QCheckBox(t("process.with_leads"))
        self.with_kerf = QCheckBox(t("process.with_kerf"))
        self.with_joints = QCheckBox(t("process.with_joints"))
        self.with_rules = QCheckBox(t("process.with_rules"))
        for cb in (self.with_leads, self.with_kerf, self.with_joints, self.with_rules):
            cb.setChecked(cb is not self.with_joints)
            fl.addWidget(cb)
        opts.setToolTip(t("process.with_tip"))
        pv.addWidget(opts)
        row = QHBoxLayout()
        row.addStretch(1)
        self.compare_btn = QPushButton(t("tech.compare"))
        self.compare_btn.clicked.connect(self.compare)
        row.addWidget(self.compare_btn)
        self.apply_btn = QPushButton(t("process.apply"))
        self.apply_btn.setDefault(True)
        self.apply_btn.clicked.connect(self.apply_selected)
        row.addWidget(self.apply_btn)
        pv.addLayout(row)
        self.pick_section = Section("pick", t("process.pick"), pick_body, expanded=True)
        self.pick_section.header.clicked.connect(self._pick_toggled)
        root.addWidget(self.pick_section)
        # ---- layers | editor tabs (one tab level)
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)
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
        self.layers.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        ll.addWidget(self.layers, 1)
        b = QPushButton(t("process.save_layer"))
        b.clicked.connect(self.save_layer_as_technology)
        bl = QHBoxLayout()
        bl.addStretch(1)
        bl.addWidget(b)
        ll.addLayout(bl)
        split.addWidget(lw)
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
        el.addWidget(self.comment)
        brow = QWidget()
        bfl = FlowLayout(brow, 6)
        self.save_btn = QPushButton(t("tech.save"))
        self.save_btn.clicked.connect(self.save_version)
        bfl.addWidget(self.save_btn)
        for key, fn in (("tech.save_new", self.save_as_new), ("tech.revert", self.revert_factory),
                        ("tech.import", self.import_file), ("tech.export", self.export_file)):
            bb = QPushButton(t(key))
            bb.clicked.connect(fn)
            bfl.addWidget(bb)
        el.addWidget(brow)
        tabs.addTab(ed, t("process.tab.technology"))
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
        df.form.addRow(QLabel(t("process.diff.mode")), self.diff_mode)
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
        split.addWidget(tabs)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        # ---- estimate: its own tab (the card is tall; the column keeps its height for editing)
        from PySide6.QtWidgets import QFrame, QScrollArea

        from mlaser.ui.preview_scrubber import CostCardWidget

        self.card = CostCardWidget(self.tr_)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(self.card)
        tabs.addTab(sa, t("process.tab.estimate"))
        self.current_layer: Optional[int] = None
        self.reload()

    # ---------------------------------------------------------------- adapt to the height
    def available_height(self) -> int:
        """The height the column really has (the shell's scroll viewport, else our own)."""
        p = self.parentWidget()
        return p.height() if p is not None and p.objectName() == "qt_scrollarea_viewport" else self.height()

    def adapt(self) -> None:
        """Collapse the Technology chooser when the column is too short for it and the editor,
        open it again when there is room (content-driven; a section the user toggled stays)."""
        sec = self.pick_section
        if getattr(self, "_user_pick", False):
            return
        body_h = sec.body.sizeHint().height()
        need_open = self.minimumSizeHint().height() + (0 if sec.expanded() else body_h)
        want = self.available_height() >= need_open
        if sec.expanded() != want:
            self._adapting = True
            try:
                sec.set_expanded(want)
            finally:
                self._adapting = False

    def resizeEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().resizeEvent(ev)
        self.adapt()

    def showEvent(self, ev) -> None:   # noqa: N802 - Qt
        super().showEvent(ev)
        p = self.parentWidget()
        if p is not None and p is not getattr(self, "_watched", None):
            p.installEventFilter(self)       # the scroll viewport's height is the one that counts
            self._watched = p
        self.adapt()

    def eventFilter(self, obj, ev) -> bool:   # noqa: N802 - Qt
        from PySide6.QtCore import QEvent
        if obj is getattr(self, "_watched", None) and ev.type() == QEvent.Resize:
            self.adapt()
        return False

    def _pick_toggled(self, _on: bool) -> None:
        if not getattr(self, "_adapting", False):
            self._user_pick = True

    def _factory_of(self, tech: T.Technology) -> Optional[T.Technology]:
        if tech.id is None:
            return None
        try:
            return self.store.factory(tech.id)
        except KeyError:
            return None

    def _matrix_picked(self, material: str, thickness: float) -> None:
        i = self.material.findData(material)
        if i >= 0:
            self.material.setCurrentIndex(i)
            self._cascade(1)
        j = self.thickness.findData(thickness)
        if j >= 0:
            self.thickness.setCurrentIndex(j)
            self._cascade(2)
        self.matrix.set_current(self.material.currentData(), self.thickness.currentData())

    def compare(self) -> TechCompareDialog:
        cur = self.picked()
        d = TechCompareDialog(self.tr_, self.store, cur, None, self)
        if not getattr(self.ctx.window, "_test_no_dialogs", False):
            d.open()
        return d

    # ---------------------------------------------------------------- store / pickers
    def reload(self, select: Optional[T.Technology] = None) -> None:
        self._all = self.store.all()
        self.material.blockSignals(True)
        self.material.clear()
        for m in sorted({t.key.material for t in self._all}):
            self.material.addItem(m, m)
        self.material.blockSignals(False)
        self.matrix.set_records(self._all)
        if select is not None:
            self.pick(select.key)
        else:
            self._cascade(1)
        self.refresh_layers()

    def _combo_set(self, combo: QComboBox, values: list, fmt=str) -> None:
        cur = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for v in values:
            combo.addItem(fmt(v), v)
        i = combo.findData(cur)
        combo.setCurrentIndex(i if i >= 0 else (0 if values else -1))
        combo.blockSignals(False)

    def _cascade(self, level: int) -> None:
        m = self.material.currentData()
        recs = [t for t in self._all if t.key.material == m]
        if level <= 1:
            self._combo_set(self.thickness, sorted({t.key.thickness_mm for t in recs}), lambda v: f"{v:g} mm")
        th = self.thickness.currentData()
        recs = [t for t in recs if th is None or abs(t.key.thickness_mm - th) < 1e-9]
        if level <= 2:
            self._combo_set(self.gas, sorted({t.key.gas for t in recs}))
        g = self.gas.currentData()
        recs = [t for t in recs if t.key.gas == g]
        if level <= 3:
            self._combo_set(self.nozzle, sorted({_nz(t.key.nozzle, t.key.power_w) for t in recs}), _nz_label)
        self._picked()

    def picked(self) -> Optional[T.Technology]:
        m, th, g, nz = (self.material.currentData(), self.thickness.currentData(), self.gas.currentData(),
                        self.nozzle.currentData())
        if None in (m, th, g, nz):
            return None
        name, power = _nz_split(nz)
        return self.store.find(T.TechKey(m, th, g, name, power))

    def pick(self, key: T.TechKey) -> None:
        for combo, v in ((self.material, key.material), (self.thickness, key.thickness_mm), (self.gas, key.gas),
                         (self.nozzle, _nz(key.nozzle, key.power_w))):
            i = combo.findData(v)
            if i >= 0:
                combo.setCurrentIndex(i)
            level = {self.material: 1, self.thickness: 2, self.gas: 3}.get(combo)
            if level:
                self._cascade(level)

    def _picked(self) -> None:
        self.matrix.set_current(self.material.currentData(), self.thickness.currentData())
        tech = self.picked()
        self.editor.set_technology(tech)
        self.apply_btn.setEnabled(tech is not None)
        self.compare_btn.setEnabled(len(self._all) > 1)
        self.refresh_history()
        self.refresh_diff()

    # ---------------------------------------------------------------- layers
    def layers_in_use(self) -> list[int]:
        doc = self.ctx.document
        return sorted({p.craft.layer for p in doc.parts})

    def refresh_layers(self) -> None:
        from mlaser.ui.canvas import layer_color
        from mlaser.ui.properties.panel import _swatch

        t = self.tr_.tr
        doc = self.ctx.document
        used = self.layers_in_use()
        keep = self.current_layer
        self.layers.blockSignals(True)
        self.layers.setRowCount(0)
        for li in used:
            lp = doc.layers.get(li)
            ref = T.layer_technology_ref(lp)
            r = self.layers.rowCount()
            self.layers.insertRow(r)
            it = QTableWidgetItem(_swatch(layer_color(li)), f"{t('props.layer')} {li}")
            it.setData(Qt.UserRole, li)
            self.layers.setItem(r, 0, it)
            name, state = self.layer_state(li)
            self.layers.setItem(r, 1, QTableWidgetItem(name))
            self.layers.setItem(r, 2, QTableWidgetItem(state))
            if ref is not None and ref.get("example"):
                self.layers.item(r, 2).setToolTip(t("tech.example_badge"))
        self.layers.blockSignals(False)
        self._combo_set(self.target, ["all"] + used,
                        lambda v: t("process.all_layers") if v == "all" else f"{t('props.layer')} {v}")
        if keep in used:
            self.select_layer(keep)
        self.refresh_diff()

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
        t = self.tr_.tr
        lp = self.ctx.document.layers.get(layer)
        ref = T.layer_technology_ref(lp)
        if lp is None or (lp.extra or {}).get("placeholder"):
            return t("process.no_technology"), t("process.state.undefined")
        if ref is None:
            return t("process.no_technology"), t("process.state.manual")
        tech = self._ref_tech(ref)
        if tech is None:
            return self.technology_name(layer), t("process.state.missing")
        n = len(T.layer_changes(lp, tech))
        state = self.tr_.format("process.state.modified", n=n) if n else t("process.state.as_applied")
        if ref.get("example"):
            state += " · " + t("process.state.example")
        try:
            head = self.store.get(int(ref["id"])).version if ref.get("id") is not None else 0
        except KeyError:
            head = 0
        if head and ref.get("version") and head > int(ref["version"]):
            state += " · " + self.tr_.format("process.state.newer", v=head)
        return self.technology_name(layer), state

    def _ref_tech(self, ref: dict) -> Optional[T.Technology]:
        try:
            if ref.get("id") is not None:
                return self.store.get(int(ref["id"]), int(ref["version"]) if ref.get("version") else None)
        except KeyError:
            pass
        try:
            return self.store.find(T.TechKey(**(ref.get("key") or {})))
        except TypeError:
            return None

    def select_layer(self, layer: int) -> None:
        for r in range(self.layers.rowCount()):
            if self.layers.item(r, 0).data(Qt.UserRole) == layer:
                self.layers.selectRow(r)
                return

    def _layer_selected(self) -> None:
        rows = self.layers.selectionModel().selectedRows() if self.layers.selectionModel() else []
        if not rows:
            return
        li = self.layers.item(rows[0].row(), 0).data(Qt.UserRole)
        self.current_layer = li
        i = self.target.findData(li)
        if i >= 0:
            self.target.setCurrentIndex(i)
        ref = T.layer_technology_ref(self.ctx.document.layers.get(li))
        if ref:
            tech = self._ref_tech(ref)
            if tech is not None:
                self.pick(tech.key)
        self.refresh_diff()

    # ---------------------------------------------------------------- apply
    def _locked(self, what: str) -> bool:
        from mlaser.ui import safety_ui

        win = self.ctx.window
        return bool(getattr(win, "safety_ui", None)) and safety_ui.job_locked(win, what)

    def apply_selected(self) -> int:
        tech = self.picked()
        if tech is None:
            return 0
        tgt = self.target.currentData()
        layers = self.layers_in_use() if tgt in (None, "all") else [int(tgt)]
        return self.apply(tech, layers)

    def apply(self, tech: T.Technology, layers: list[int], leads: Optional[bool] = None, kerf: Optional[bool] = None,
              joints: Optional[bool] = None, rules: Optional[bool] = None) -> int:
        """Apply ``tech`` to ``layers`` (one undo step).  Returns the number of layers."""
        from mlaser.ui.canvas import layer_color

        if not layers or self._locked(self.tr_.tr("process.apply")):
            return 0
        opts = dict(leads=self.with_leads.isChecked() if leads is None else leads,
                    kerf=self.with_kerf.isChecked() if kerf is None else kerf,
                    joints=self.with_joints.isChecked() if joints is None else joints,
                    rules=self.with_rules.isChecked() if rules is None else rules)
        craft = any(opts.values())
        doc = self.ctx.document
        scope = [p for p in doc.parts if p.craft.layer in layers] if craft else []

        def fn(d):
            for li in layers:
                lp = d.layers.get(li) or LayerParams(index=li, color=layer_color(li).name())
                d.layers[li] = T.apply_to_layer(lp, tech)
            if craft:
                T.apply_craft(d, tech, [p for p in d.parts if p.craft.layer in layers], **opts)
        self.ctx.edit(fn, self.tr_.format("process.edit.apply", name=tech.key.label()), parts=scope)
        self.ctx.log(self.tr_.format("process.applied", name=tech.key.label(),
                                     layers=", ".join(map(str, layers))))
        self.refresh_layers()
        return len(layers)

    def save_layer_as_technology(self) -> Optional[T.Technology]:
        li = self.current_layer or (self.layers_in_use() or [None])[0]
        if li is None:
            return None
        lp = self.ctx.document.layers.get(li)
        cur = self.picked()
        base = cur.key if cur is not None else T.TechKey("Material", 1.0, "N2", "nozzle")
        text, ok = QInputDialog.getText(self, self.tr_.tr("process.save_layer"), self.tr_.tr("tech.new_material"),
                                        text=f"{base.material} (layer {li})")
        if not ok or not text:
            return None
        tech = T.technology_from_layer(lp, replace(base, material=text))
        saved = self.store.save(tech, self.tr_.format("process.from_layer", n=li))
        self.reload(select=saved)
        return saved

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
        self.reload(select=saved)
        return saved

    def save_as_new(self) -> Optional[T.Technology]:
        if self.editor.tech is None:
            return None
        cur = self.editor.current()
        text, ok = QInputDialog.getText(self, self.tr_.tr("tech.save_new"), self.tr_.tr("tech.new_material"),
                                        text=cur.key.material + " *")
        if not ok or not text:
            return None
        new = replace(cur, key=replace(cur.key, material=text), id=None, version=0, origin="user", verified=False)
        if new.notes == T.EXAMPLE_NOTE:
            new = replace(new, notes="")
        saved = self.store.save(new, self.tr_.tr("tech.created"))
        self.reload(select=saved)
        return saved

    def revert_factory(self) -> Optional[T.Technology]:
        tech = self.editor.tech
        if tech is None or tech.id is None:
            return None
        saved = self.store.revert(tech.id)
        self.reload(select=saved)
        return saved

    def restore_version(self) -> Optional[T.Technology]:
        tech = self.editor.tech
        it = self.history.currentItem()
        if tech is None or tech.id is None or it is None:
            return None
        saved = self.store.revert(tech.id, int(it.data(0, Qt.UserRole)))
        self.reload(select=saved)
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
            ref = T.layer_technology_ref(lp)
            base = self._ref_tech(ref) if ref else None
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
        for c in range(4):
            self.diff.resizeColumnToContents(c)
        self.diff_summary.setText(self.tr_.format("process.diff.count", n=len(ch)))
