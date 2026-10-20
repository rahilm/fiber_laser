"""Process workspace (ux.md P1-2, §4.2): technology per layer, the technology editor, "what
changed", and the time / cost estimate.

* **Pickers**: material, thickness, gas, nozzle (cascading over the records of the store) and
  *Apply* to the chosen layer or to every layer in use, optionally with the record's craft
  defaults (leads, kerf, micro-joints) and rules.  Applying is one ``PluginContext.edit``.
* **Layers**: every layer in use with its technology and its state (example values, modified
  after applying, not set).  *Save layer as technology* stores a hand-tuned layer as a record.
* **Technology editor**: the record's cut, pierce, motion and craft values with units and the
  registry ranges, every other layer parameter under *All parameters*; *Save* makes a new
  version, *Save as new* a new record; *Revert to factory*; the version history.
* **What changed**: the record against its factory version (or the registry defaults), or the
  layer against the record it was applied from.
* **Estimate**: the time and cost card of the current plan.

Records live in :class:`mlaser.services.technology.TechnologyStore`; this module is only the UI.
"""

from __future__ import annotations

import copy
import time
from dataclasses import replace
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from mlaser.model import LayerParams
from mlaser.services import technology as T
from mlaser.ui.params import GAS_KEYS

ADVANCED_SKIP = ("GP.Note", "GP.RecentFile", "GP.LayerFileName", "GP.PWMCurveNodes", "GP.FreqCurveNodes")
MANU_TYPES = (0, 1, 2, 3)        # registry options of GP.ManuType: direct, fix height, 1-step, 2-step


def _nz(nozzle: str, power_w: int) -> str:
    return f"{nozzle}\x1f{int(power_w)}"


def _nz_split(v: str) -> tuple[str, int]:
    name, _, p = str(v).rpartition("\x1f")
    return name, int(p or 0)


def _nz_label(v: str) -> str:
    name, p = _nz_split(v)
    return name + (f" · {p / 1000:g} kW" if p else "")


# --------------------------------------------------------------------------- editors
class ParamField:
    """A typed editor for one GP.* layer parameter (registry type, unit and range)."""

    def __init__(self, key: str, tr: Any, on_change: Callable[[], None]) -> None:
        self.key = key
        self.d = T.layer_registry()[key]
        d = self.d
        unit = T.display_unit(key)
        if d.type_name == "bool":
            w: QWidget = QCheckBox()
            w.toggled.connect(lambda _v: on_change())
        elif d.type_name == "enum" and ("GasType" in key or key.endswith("_GasType")):
            w = QComboBox()                       # the six gas valves (MGP.Low*/High*, mp43..mp48)
            n = max(len(GAS_KEYS), int(d.max) + 1 if d.max > d.min else 0)
            for i in range(n):
                w.addItem(tr.tr(GAS_KEYS[i]) if i < len(GAS_KEYS) else str(i))
            w.currentIndexChanged.connect(lambda _v: on_change())
        elif d.type_name == "enum" and d.options:
            w = QComboBox()
            for o in d.options:
                w.addItem(tr.tr(o[0]) if o[0] else str(o[1]))
            for i in range(len(d.options), int(d.max) + 1 if d.max > d.min else 0):
                w.addItem(str(i))
            w.currentIndexChanged.connect(lambda _v: on_change())
        elif d.type_name in ("int", "enum"):
            w = QSpinBox()
            w.setRange(int(d.min) if d.max > d.min else -2 ** 31, int(d.max) if d.max > d.min else 2 ** 31 - 1)
            w.setKeyboardTracking(False)
            if unit:
                w.setSuffix(f" {unit}")
            w.valueChanged.connect(lambda _v: on_change())
        elif d.type_name == "string":
            w = QLineEdit()
            w.editingFinished.connect(on_change)
        else:
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(d.min if d.max > d.min else -1e9, d.max if d.max > d.min else 1e9)
            w.setKeyboardTracking(False)
            if unit:
                w.setSuffix(f" {unit}")
            w.valueChanged.connect(lambda _v: on_change())
        rng = f"{d.min:g} … {d.max:g} {unit}".strip() if d.max > d.min and d.type_name not in ("bool", "enum") else ""
        w.setToolTip(f"{key}" + (f"\n{tr.tr('tech.range')}: {rng}" if rng else ""))
        self.widget = w
        group, name = tr.tr_param(d.lang)
        self.group, self.label = group, name or key
        w.setAccessibleName(self.label)

    def set(self, v: Any) -> None:
        w = self.widget
        w.blockSignals(True)
        try:
            if isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, min(w.count() - 1, int(v or 0))))
            elif isinstance(w, QSpinBox):
                w.setValue(int(float(v or 0)))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(float(v or 0.0))
            else:
                w.setText("" if v is None else str(v))
        finally:
            w.blockSignals(False)

    def get(self) -> Any:
        w = self.widget
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentIndex()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return round(w.value(), 6)
        return w.text()


def _dspin(lo: float, hi: float, dec: int, unit: str) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setKeyboardTracking(False)
    if unit:
        s.setSuffix(f" {unit}")
    return s


class TechnologyEditor(QWidget):
    """Edits one :class:`Technology` (see the module docstring)."""

    changed = Signal()

    def __init__(self, tr: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.tech: Optional[T.Technology] = None
        self._loading = False
        t = tr.tr
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
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.fields: dict[str, ParamField] = {}
        # cut
        self.tabs.addTab(self._form_page(T.CUT_KEYS), t("tech.tab.cut"))
        # pierce
        page = QWidget()
        pl = QVBoxLayout(page)
        f = QFormLayout()
        self.manu = QComboBox()
        d = T.layer_registry()["GP.ManuType"]
        for o in d.options:
            self.manu.addItem(t(o[0]))
        self.manu.currentIndexChanged.connect(self._manu_changed)
        f.addRow(QLabel(t("tech.pierce_mode")), self.manu)
        pl.addLayout(f)
        self.stage_table = QTableWidget(0, len(T.STAGE_KEYS))
        self.stage_table.verticalHeader().setVisible(True)
        self.stage_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        pl.addWidget(self.stage_table, 1)
        note = QLabel(t("tech.pierce_order"))
        note.setWordWrap(True)
        note.setEnabled(False)
        pl.addWidget(note)
        self.tabs.addTab(page, t("tech.tab.pierce"))
        self.stage_fields: list[list[ParamField]] = []
        # motion / delays
        self.tabs.addTab(self._form_page(T.SLOW_KEYS), t("tech.tab.motion"))
        # craft and rules
        self.tabs.addTab(self._craft_page(), t("tech.tab.craft"))
        # all other parameters
        used = set(T.CUT_KEYS) | set(T.SLOW_KEYS) | {"GP.ManuType"} | \
            {f"GP.{k}{s}" for k in T.STAGE_KEYS for s in range(5)}
        rest = [k for k in T.layer_registry() if k not in used and k not in ADVANCED_SKIP]
        self.tabs.addTab(self._form_page(rest, grouped=True), t("tech.tab.all"))

    # ---------------------------------------------------------------- pages
    def _form_page(self, keys, grouped: bool = False) -> QWidget:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        body = QWidget()
        v = QVBoxLayout(body)
        forms: dict[str, QFormLayout] = {}
        for k in keys:
            pf = ParamField(k, self.tr_, self._edited)
            self.fields[k] = pf
            g = pf.group if grouped else ""
            if g not in forms:
                box = QGroupBox(g) if grouped else QWidget()
                forms[g] = QFormLayout(box)
                v.addWidget(box)
            lab = QLabel(pf.label)
            lab.setBuddy(pf.widget)
            forms[g].addRow(lab, pf.widget)
        v.addStretch(1)
        sa.setWidget(body)
        return sa

    def _craft_page(self) -> QWidget:
        t = self.tr_.tr
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        body = QWidget()
        f = QFormLayout(body)
        self.lead_kind = QComboBox()
        self.lead_kind.addItems([t(k) for k in ("props.lead.none", "props.lead.line", "props.lead.arc",
                                                "props.lead.line_arc")])
        self.lead_len = _dspin(0.0, 100.0, 2, "mm")
        self.lead_ang = _dspin(0.0, 180.0, 1, "°")
        self.lead_rad = _dspin(0.0, 100.0, 2, "mm")
        self.kerf_kind = QComboBox()
        for v, k in ((-1, "props.kerf.none"), (0, "props.kerf.inner"), (1, "props.kerf.outer")):
            self.kerf_kind.addItem(t(k), v)
        self.kerf_w = _dspin(0.0, 10.0, 3, "mm")
        self.mj_count = QSpinBox()
        self.mj_count.setRange(0, 100)
        self.mj_len = _dspin(0.0, 50.0, 2, "mm")
        self.sf_size = _dspin(0.0, 1000.0, 1, "mm")
        self.sf_lead = _dspin(0.0, 100.0, 2, "mm")
        self.corner_on = QCheckBox()
        self.corner_angle = _dspin(1.0, 179.0, 0, "°")
        self.flow = _dspin(0.0, 100000.0, 1, "l/min")
        self.flow.setSpecialValueText(t("tech.flow_unknown"))
        self.density = _dspin(0.0, 30.0, 3, "g/cm³")
        self.density.setSpecialValueText(t("tech.unknown"))
        self.verified = QCheckBox()
        self.notes = QLineEdit()
        rows = (("tech.lead_kind", self.lead_kind), ("tech.lead_length", self.lead_len),
                ("tech.lead_angle", self.lead_ang), ("tech.lead_radius", self.lead_rad),
                ("tech.kerf_side", self.kerf_kind), ("tech.kerf_width", self.kerf_w),
                ("tech.joint_count", self.mj_count), ("tech.joint_length", self.mj_len),
                ("tech.small_size", self.sf_size), ("tech.small_lead", self.sf_lead),
                ("tech.corner_cool", self.corner_on), ("tech.corner_angle", self.corner_angle),
                ("tech.gas_flow", self.flow), ("tech.density", self.density),
                ("tech.verified", self.verified), ("tech.notes", self.notes))
        for key, w in rows:
            lab = QLabel(t(key))
            lab.setBuddy(w)
            w.setAccessibleName(t(key))
            f.addRow(lab, w)
            for sig in ("valueChanged", "currentIndexChanged", "toggled"):
                if hasattr(w, sig):
                    getattr(w, sig).connect(lambda *_a: self._edited())
                    break
            else:
                w.editingFinished.connect(self._edited)
        sa.setWidget(body)
        return sa

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
                self.badge.setText("⚠ " + t("tech.example_badge"))
                self.badge.setStyleSheet("color:#d08000; font-weight:bold")
            elif tech.verified:
                self.badge.setText("✓ " + t("tech.verified_badge"))
                self.badge.setStyleSheet("")
            else:
                self.badge.setText("")
            full = tech.full_params()
            for k, pf in self.fields.items():
                pf.set(full.get(k))
            mt = int(full.get("GP.ManuType", 0) or 0)
            self.manu.setCurrentIndex(MANU_TYPES.index(mt) if mt in MANU_TYPES else 0)
            self._build_stages(full)
            cr = {**T.DEFAULT_CRAFT, **(tech.craft or {})}
            li = cr.get("lead_in") or {}
            self.lead_kind.setCurrentIndex(int(li.get("kind", 1)))
            self.lead_len.setValue(float(li.get("length", 3.0)))
            self.lead_ang.setValue(float(li.get("angle_deg", 90.0)))
            self.lead_rad.setValue(float(li.get("radius", 1.0)))
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
        finally:
            self._loading = False

    def _build_stages(self, full: dict) -> None:
        n = T.stage_count({"GP.ManuType": MANU_TYPES[self.manu.currentIndex()]})
        tb = self.stage_table
        tb.setRowCount(n)
        self.stage_fields = []
        heads = []
        for k in T.STAGE_KEYS:
            unit = T.display_unit(f"GP.{k}0")
            heads.append(self.tr_.tr(f"tech.stage_col.{k}") + (f" ({unit})" if unit else ""))
        tb.setHorizontalHeaderLabels(heads)
        tb.setVerticalHeaderLabels([self.tr_.format("tech.stage", n=s + 1) for s in range(n)])
        for s in range(n):
            row = []
            for c, k in enumerate(T.STAGE_KEYS):
                pf = ParamField(f"GP.{k}{s}", self.tr_, self._edited)
                pf.set(full.get(f"GP.{k}{s}"))
                tb.setCellWidget(s, c, pf.widget)
                row.append(pf)
            self.stage_fields.append(row)

    def _manu_changed(self) -> None:
        if self._loading:
            return
        full = self.current().full_params() if self.tech is not None else T.registry_defaults()
        self._loading = True
        try:
            self._build_stages(full)
        finally:
            self._loading = False
        self._edited()

    def _edited(self) -> None:
        if not self._loading:
            self.changed.emit()

    def current(self) -> T.Technology:
        """The record as edited (only values that differ from the registry defaults are kept)."""
        assert self.tech is not None
        defaults = T.registry_defaults()
        params = dict(self.tech.params)
        for k, pf in self.fields.items():
            params[k] = pf.get()
        params["GP.ManuType"] = MANU_TYPES[self.manu.currentIndex()]
        for row in self.stage_fields:
            for pf in row:
                params[pf.key] = pf.get()
        params = {k: v for k, v in params.items() if not T._same(v, defaults.get(k)) or k in self.tech.params}
        craft = copy.deepcopy(self.tech.craft or {})
        li = dict(craft.get("lead_in") or T.DEFAULT_CRAFT["lead_in"])
        li.update(kind=self.lead_kind.currentIndex(), length=self.lead_len.value(), angle_deg=self.lead_ang.value(),
                  radius=self.lead_rad.value())
        craft["lead_in"] = li
        craft.setdefault("lead_out", dict(T.DEFAULT_CRAFT["lead_out"]))
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


# --------------------------------------------------------------------------- workspace
def _thickness_label(v: float) -> str:
    """A sheet thickness (mm, the technology key) in the display units: "3 mm", "0.118 in"."""
    from mlaser.services import units
    return units.current().fmt(float(v), "length", trim=True)


class ProcessWorkspace(QWidget):
    """The Process workspace (see the module docstring)."""

    def __init__(self, ctx: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.store = store
        self.tr_ = ctx.tr
        self.setObjectName("process_workspace")
        t = self.tr_.tr
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()            # no large minimum height: the window fits 1280 x 800
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)
        # ---- pickers
        pick = QGroupBox(t("process.pick"))
        pf = QFormLayout(pick)
        self.material = QComboBox()
        self.thickness = QComboBox()
        self.gas = QComboBox()
        self.nozzle = QComboBox()
        for key, w in (("process.material", self.material), ("process.thickness", self.thickness),
                       ("process.gas", self.gas), ("process.nozzle", self.nozzle)):
            lab = QLabel(t(key))
            lab.setBuddy(w)
            w.setAccessibleName(t(key))
            pf.addRow(lab, w)
        self.material.currentIndexChanged.connect(lambda _i: self._cascade(1))
        self.thickness.currentIndexChanged.connect(lambda _i: self._cascade(2))
        self.gas.currentIndexChanged.connect(lambda _i: self._cascade(3))
        self.nozzle.currentIndexChanged.connect(lambda _i: self._picked())
        # thickness items: data mm (the technology key), text in the display units
        from mlaser.ui.units_qt import on_units_changed
        on_units_changed(self.thickness, lambda _u: [self.thickness.setItemText(i, _thickness_label(
            self.thickness.itemData(i))) for i in range(self.thickness.count())], now=False)
        row = QHBoxLayout()
        self.target = QComboBox()
        self.target.setAccessibleName(t("process.apply_to"))
        row.addWidget(QLabel(t("process.apply_to")))
        row.addWidget(self.target, 1)
        pf.addRow(row)
        row = QHBoxLayout()
        self.with_leads = QCheckBox(t("process.with_leads"))
        self.with_kerf = QCheckBox(t("process.with_kerf"))
        self.with_joints = QCheckBox(t("process.with_joints"))
        self.with_rules = QCheckBox(t("process.with_rules"))
        for cb in (self.with_leads, self.with_kerf, self.with_joints, self.with_rules):
            cb.setChecked(cb is not self.with_joints)
            row.addWidget(cb)
        pf.addRow(row)
        self.apply_btn = QPushButton(t("process.apply"))
        self.apply_btn.setMinimumHeight(32)
        self.apply_btn.clicked.connect(self.apply_selected)
        pf.addRow(self.apply_btn)
        root.addWidget(pick)
        split = QSplitter(Qt.Vertical)
        root.addWidget(split, 1)
        # ---- layers
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel(f"<b>{t('process.layers')}</b>"))
        self.layers = QTableWidget(0, 3)
        self.layers.setHorizontalHeaderLabels([t("process.col.layer"), t("process.col.technology"),
                                               t("process.col.state")])
        self.layers.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.layers.verticalHeader().hide()
        self.layers.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.layers.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layers.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layers.itemSelectionChanged.connect(self._layer_selected)
        self.layers.setMinimumHeight(110)
        ll.addWidget(self.layers, 1)
        b = QPushButton(t("process.save_layer"))
        b.clicked.connect(self.save_layer_as_technology)
        ll.addWidget(b)
        split.addWidget(lw)
        # ---- editor + diff
        tabs = QTabWidget()
        self.editor = TechnologyEditor(self.tr_)
        self.editor.changed.connect(self._editor_changed)
        ed = QWidget()
        el = QVBoxLayout(ed)
        el.setContentsMargins(0, 0, 0, 0)
        el.addWidget(self.editor, 1)
        row = QHBoxLayout()
        self.comment = QLineEdit()
        self.comment.setPlaceholderText(t("tech.comment"))
        self.comment.setAccessibleName(t("tech.comment"))
        row.addWidget(self.comment, 1)
        self.save_btn = QPushButton(t("tech.save"))
        self.save_btn.clicked.connect(self.save_version)
        row.addWidget(self.save_btn)
        el.addLayout(row)
        row = QHBoxLayout()
        for key, fn in (("tech.save_new", self.save_as_new), ("tech.revert", self.revert_factory),
                        ("tech.import", self.import_file), ("tech.export", self.export_file)):
            bb = QPushButton(t(key))
            bb.clicked.connect(fn)
            row.addWidget(bb)
        el.addLayout(row)
        tabs.addTab(ed, t("process.tab.technology"))
        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.diff_mode = QComboBox()
        self.diff_mode.addItem(t("process.diff.factory"), "factory")
        self.diff_mode.addItem(t("process.diff.layer"), "layer")
        self.diff_mode.addItem(t("process.diff.unsaved"), "unsaved")
        self.diff_mode.setAccessibleName(t("process.diff.mode"))
        self.diff_mode.currentIndexChanged.connect(lambda _i: self.refresh_diff())
        row.addWidget(QLabel(t("process.diff.mode")))
        row.addWidget(self.diff_mode, 1)
        dl.addLayout(row)
        self.diff = QTreeWidget()
        self.diff.setColumnCount(4)
        self.diff.setHeaderLabels([t("process.diff.param"), t("process.diff.unit"), t("process.diff.old"),
                                   t("process.diff.new")])
        self.diff.setRootIsDecorated(False)
        dl.addWidget(self.diff, 1)
        self.diff_summary = QLabel()
        dl.addWidget(self.diff_summary)
        tabs.addTab(dw, t("process.tab.changes"))
        hw = QWidget()
        hl = QVBoxLayout(hw)
        hl.setContentsMargins(0, 0, 0, 0)
        self.history = QTreeWidget()
        self.history.setColumnCount(3)
        self.history.setHeaderLabels([t("tech.version"), t("tech.date"), t("tech.comment")])
        self.history.setRootIsDecorated(False)
        hl.addWidget(self.history, 1)
        bb = QPushButton(t("tech.restore_version"))
        bb.clicked.connect(self.restore_version)
        hl.addWidget(bb)
        tabs.addTab(hw, t("process.tab.history"))
        self.tabs = tabs
        tabs.setMinimumHeight(330)
        split.addWidget(tabs)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        # ---- estimate
        from mlaser.ui.preview_scrubber import CostCardWidget

        self.card = CostCardWidget(self.tr_)
        root.addWidget(self.card)
        self.current_layer: Optional[int] = None
        self.reload()

    # ---------------------------------------------------------------- store / pickers
    def reload(self, select: Optional[T.Technology] = None) -> None:
        self._all = self.store.all()
        self.material.blockSignals(True)
        self.material.clear()
        for m in sorted({t.key.material for t in self._all}):
            self.material.addItem(m, m)
        self.material.blockSignals(False)
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
            self._combo_set(self.thickness, sorted({t.key.thickness_mm for t in recs}), _thickness_label)
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
        tech = self.picked()
        self.editor.set_technology(tech)
        self.apply_btn.setEnabled(tech is not None)
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
