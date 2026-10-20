"""Dialogs. Labels come from lang.txt keys cited next to each field.

The original builds almost every dialog at run time on top of BCGP property grids
(MainApp.exe has only 7 empty dialog templates, RT_DIALOG 319..459), so most dialogs here are
data-driven from :mod:`mlaser.ui.params` (the ConfigService schema).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QLabel, QLineEdit, QSpinBox,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                               QTableWidget, QTableWidgetItem, QHeaderView)

from mlaser.services import units as U

from . import params as P
from .i18n import Translator
from .units_qt import QuantitySpinBox, kind_for, on_units_changed

#: registry settings whose declared unit is not the value's unit: QR.QRCodeLineDensity is a print
#: density (lines per mm) the registry tags "mm"; shown without the misleading "mm" and unconverted
UNIT_NOT_A_QUANTITY = {"QR.QRCodeLineDensity"}


def setting_kind(d) -> Optional[str]:
    """The display-unit kind of a schema setting's number editor, or None (shown as stored)."""
    if d.kind not in ("float", "int") or d.key in UNIT_NOT_A_QUANTITY:
        return None
    return kind_for(d.unit)


def setting_unit_label(d) -> str:
    """The unit shown after a setting's label: the display unit for converted settings, nothing
    for check boxes and option lists (the registry tags some of them "mm"), else the stored one."""
    k = setting_kind(d)
    if k is not None:
        return U.current().suffix(k)
    if d.kind in ("bool", "enum") or d.key in UNIT_NOT_A_QUANTITY:
        return ""
    return d.unit or ""


def _qspin(kind: str, lo, hi, val, dec=3, step=1.0) -> QuantitySpinBox:
    """A spin box for a length (or other quantity) in the display units; value() in mm."""
    return QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=dec, step=step, value=val)


def _spin(lo, hi, val, dec=3, step=1.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    return s


class _Base(QDialog):
    def __init__(self, tr: Translator, title_key: str, parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.setWindowTitle(tr.tr(title_key))
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 16, 16, 16)     # DESIGN.md: dialog margins SP3
        self.lay.setSpacing(8)
        self.form = QFormLayout()
        self.lay.addLayout(self.form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(tr.tr("ap1"))
        bb.button(QDialogButtonBox.Cancel).setText(tr.tr("ap0"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self._bb = bb

    def finish(self):
        self.lay.addWidget(self._bb)

    def row(self, key: str, w: QWidget, param_label: bool = True) -> QWidget:
        label = self.tr_.tr_param(key)[1] if param_label else self.tr_.tr(key)
        self.form.addRow(label, w)
        return w


class ParamGridDialog(QDialog):
    """Property-grid style editor for one or more parameter sections (CBCGPPropList replacement).

    The editors are typed from the ConfigService schema (:mod:`mlaser.ui.params`): check boxes
    for booleans, option lists for enums, spin boxes with the registry range and unit for
    numbers.  ``values`` (the window's settings view, or a plain dict) receives on accept only
    the values that changed; a value the schema refuses is reported, not stored.
    """

    def __init__(self, tr: Translator, title_key: str, sections: list[str], values,
                 parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.values = values
        self.errors: list[str] = []
        self.setWindowTitle(tr.tr(title_key))
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        self._editors: dict[str, QWidget] = {}
        self._settings: dict = {}
        self._initial: dict = {}
        for sec in sections:
            defs = P.section(sec)
            if not defs:
                continue
            tree = QTreeWidget()
            tree.setColumnCount(2)
            tree.setHeaderLabels([tr.tr("hp40", "Name"), tr.tr("gv2", "Value")])
            tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            for group, items in P.grouped(defs, tr).items():
                g = QTreeWidgetItem([group])
                g.setFirstColumnSpanned(True)
                tree.addTopLevelItem(g)
                for d, label in items:
                    it = QTreeWidgetItem([label])
                    on_units_changed(tree, lambda _u, it=it, d=d, label=label: it.setText(
                        0, label + (f" ({ul})" if (ul := setting_unit_label(d)) else "")))
                    it.setToolTip(0, d.key)
                    g.addChild(it)
                    ed = self._editor(d)
                    tree.setItemWidget(it, 1, ed)
                    self._editors[d.key] = ed
                    self._settings[d.key] = d
                    self._initial[d.key] = self._read(ed)
                g.setExpanded(True)
            tabs.addTab(tree, sec)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _editor(self, d) -> QWidget:
        v = self.values.get(d.key)
        if v is None:
            v = d.default
        if d.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(v))
            return w
        if d.kind == "enum" and d.options:
            w = QComboBox()
            w.addItems([self.tr_.tr(o) if self.tr_.has(o) else str(o) for o in d.options])
            try:
                w.setCurrentIndex(int(v))
            except (TypeError, ValueError):
                pass
            return w
        if d.kind in ("int", "float", "enum"):
            lo = d.min if d.min is not None else -1e9
            hi = d.max if d.max is not None else 1e9
            k = setting_kind(d)
            if k is not None:
                # value() in the stored unit (mm, mm/s ...); shown and typed in the display units
                q = QuantitySpinBox(k, minimum=lo, maximum=hi, decimals=4 if d.kind == "float" else 0)
                try:
                    q.setValue(float(v))
                except (TypeError, ValueError):
                    pass
                q.setProperty("int_setting", d.kind == "int")
                return q
            if d.kind == "float":
                ws: QWidget = _spin(lo, hi, 0.0, 4)
            else:
                ws = QSpinBox()
                ws.setRange(int(max(lo, -2**31)), int(min(hi, 2**31 - 1)))
            try:
                ws.setValue(float(v) if d.kind == "float" else int(float(v)))
            except (TypeError, ValueError):
                pass
            if setting_unit_label(d):
                ws.setSuffix(f" {setting_unit_label(d)}")
            return ws
        return QLineEdit("" if v is None else str(v))

    @staticmethod
    def _read(w):
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentIndex()
        if isinstance(w, QuantitySpinBox):
            # an integer setting keeps integer mm (a typed 2 in = 50.8 mm is stored as 51)
            return int(round(w.value())) if w.property("int_setting") else w.value()
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            return w.value()
        return w.text().strip()

    def _ok(self) -> None:
        self.errors = []
        for name, w in self._editors.items():
            v = self._read(w)
            if v == self._initial.get(name) and name not in self.values:
                continue
            if v == self.values.get(name):
                continue
            try:
                self.values[name] = self._settings[name].validate(v)
            except ValueError as e:
                self.errors.append(str(e))
        self.accept()


# lp0 "Layer parameters": the grouped technology form (mlaser.ui.layer_params_dialog)
from .layer_params_dialog import LayerParamDialog  # noqa: E402,F401 - re-exported


class LeadDialog(_Base):
    """mf205 Lead: lead-in and lead-out (``Craft.lead_in/out``), side by side when the dialog is
    wide enough, with the live lead / kerf diagram; "only closed contours" and "only the
    selected parts" (with nothing selected the leads go to every part, the main window says
    so).  Lengths and angles are QuantitySpinBox fields (units in the suffix)."""

    def __init__(self, tr: Translator, lead_in, lead_out, parent=None):
        super().__init__(tr, "mf205", parent)
        from .properties.lead_diagram import LeadDiagram
        from .quantity import quantity
        from .techform import FlowLayout, ResponsiveForm, Section

        t = tr.tr
        self._w = {}
        intro = QLabel(t("leaddlg.intro"))
        intro.setWordWrap(True)
        self.lay.insertWidget(0, intro)
        pages = QWidget()
        flow = FlowLayout(pages, 16)
        for name, lead, title in (("in", lead_in, "props.lead_in"), ("out", lead_out, "props.lead_out")):
            form = ResponsiveForm()
            f = form.form
            kind = QComboBox()
            kind.addItems([t(k) for k in ("props.lead.none", "props.lead.line", "props.lead.arc",
                                          "props.lead.line_arc")])
            kind.setCurrentIndex(max(0, min(3, lead.kind)))
            ang = quantity("angle", 0.0, 180.0, 1)
            ang.setValue(lead.angle_deg)
            ln = quantity("length", 0.0, 1000.0, 3, 0.5)
            ln.setValue(lead.length)
            rad = quantity("length", 0.0, 1000.0, 3, 0.5)
            rad.setValue(lead.radius)
            corner = QCheckBox(t("leaddlg.at_corner"))
            corner.setChecked(lead.at_corner)
            corner.setToolTip(t("leaddlg.at_corner_tip"))
            for key, w in (("props.lead_kind", kind), ("props.lead_length", ln), ("props.lead_angle", ang),
                           ("props.lead_radius", rad)):
                lab = QLabel(t(key))
                lab.setBuddy(w)
                w.setAccessibleName(f"{t(title)} {t(key)}")
                f.addRow(lab, w)
            f.addRow(corner)
            for w in (ang, ln, rad):
                w.valueChanged.connect(lambda *_a: self._preview())
            kind.currentIndexChanged.connect(lambda *_a: self._preview())
            self._w[name] = (lead, kind, ang, ln, rad, corner)
            flow.addWidget(Section(f"lead_{name}", t(title), form))
        self.lay.insertWidget(1, pages)
        self.diagram = LeadDiagram()
        self.lay.insertWidget(2, self.diagram)
        self.only_closed = QCheckBox(t("leaddlg.only_closed"))
        self.only_closed.setChecked(True)
        self.only_selected = QCheckBox(t("leaddlg.only_selected"))
        self.only_selected.setChecked(True)
        self.only_selected.setToolTip(t("leaddlg.only_selected_tip"))
        self.form.addRow(self.only_closed)
        self.form.addRow(self.only_selected)
        self.finish()
        self.accepted.connect(self._store)
        self._preview()

    def _preview(self) -> None:
        from mlaser.model import LeadParams

        vals = []
        for name in ("in", "out"):
            _lead, kind, ang, ln, rad, _corner = self._w[name]
            vals.append(LeadParams(kind=kind.currentIndex(), angle_deg=ang.value(), length=ln.value(),
                                   radius=rad.value()))
        self.diagram.set_leads(*vals)

    def _store(self):
        for lead, kind, ang, ln, rad, corner in self._w.values():
            lead.kind = kind.currentIndex()
            lead.angle_deg = ang.value()
            lead.length = ln.value()
            lead.radius = rad.value()
            lead.at_corner = corner.isChecked()


class KerfDialog(_Base):
    """mf113 Kerf compensation: side (inside for holes, outside for parts) and the kerf width
    (QuantitySpinBox), with the lead / kerf diagram."""

    def __init__(self, tr: Translator, kerf_comp: int = 1, width: float = 0.1, parent=None):
        super().__init__(tr, "mf113", parent)
        from .properties.lead_diagram import LeadDiagram
        from .quantity import quantity

        t = tr.tr
        intro = QLabel(t("tech.h.kerf_side"))
        intro.setWordWrap(True)
        self.lay.insertWidget(0, intro)
        self.kind = QComboBox()
        self.kind.addItems([t("props.kerf.inner"), t("props.kerf.outer")])   # 0 inner, 1 outer
        self.kind.setCurrentIndex(0 if kerf_comp == 0 else 1)
        self.form.addRow(t("props.kerf_side"), self.kind)
        self.width = quantity("small_length", 0.0, 10.0, 3, 0.05)
        self.width.setValue(width)
        self.form.addRow(t("props.kerf_width"), self.width)
        from mlaser.model import LeadParams

        self.diagram = LeadDiagram()
        self.diagram.set_leads(LeadParams(kind=0), LeadParams(kind=0))
        self.form.addRow(self.diagram)
        self.kind.currentIndexChanged.connect(lambda *_a: self._preview())
        self.width.valueChanged.connect(lambda *_a: self._preview())
        self._preview()
        self.finish()

    def _preview(self) -> None:
        self.diagram.set_kerf(self.kind.currentIndex(), self.width.value())

    def result_values(self) -> tuple[int, float]:
        return self.kind.currentIndex(), self.width.value()


class MicroJointDialog(_Base):
    """mf191 "Micro Joint Parameters" (auto): pd343 type, pd344 number, pd345 distance,
    pd346 length, pd313_0 direction, pd314_1 min graph size; manual: pd325 length."""

    def __init__(self, tr: Translator, parent=None):
        super().__init__(tr, "mf191", parent)
        self.kind = self.row("pd343", QComboBox())
        self.kind.addItems([tr.tr_param("pd344")[1], tr.tr_param("pd345")[1]])
        self.number = self.row("pd344", QSpinBox())
        self.number.setRange(1, 100)
        self.number.setValue(4)
        self.distance = self.row("pd345", _qspin("length", 1, 10000, 100, 1))
        self.length = self.row("pd346", _qspin("small_length", 0.01, 50, 0.5, 2, 0.1))
        self.min_size = self.row("pd314_1", _qspin("length", 0, 10000, 0, 1))
        self.finish()


class ValueDialog(_Base):
    """Single numeric value (pd339 rotate angle, pd348 scale percent, pd81 step...)."""

    def __init__(self, tr: Translator, title_key: str, field_key: str, value: float,
                 lo=-1e6, hi=1e6, parent=None):
        super().__init__(tr, title_key, parent)
        self.value = self.row(field_key, _spin(lo, hi, value, 3))
        self.finish()


class IPDialog(_Base):
    """Device IP parameters: controller pd445-447, FTC pd448-450,
    laser pd451-453 (defaults per the brief: 10.1.1.168 / .169 / .170). The original mf133
    "Set IP" button instead sets the PC NIC to 10.1.1.x via IPSet.exe (netsh)."""

    def __init__(self, tr: Translator, values: Optional[dict] = None, parent=None):
        super().__init__(tr, "mf133", parent)
        values = values or {}
        self.edits = {}
        for title, keys, default in (("mf119", ("pd445", "pd446", "pd447"), ("10.1.1.168", "255.255.255.0", "10.1.1.1")),
                                     ("mf120", ("pd448", "pd449", "pd450"), ("10.1.1.169", "255.255.255.0", "10.1.1.1")),
                                     ("gp120", ("pd451", "pd452", "pd453"), ("10.1.1.170", "255.255.255.0", "10.1.1.1"))):
            box = QGroupBox(tr.tr(title))
            f = QFormLayout(box)
            for k, d in zip(keys, default):
                e = QLineEdit(values.get(k, d))
                e.setInputMask("000.000.000.000;_")
                e.setText(values.get(k, d))
                f.addRow(tr.tr_param(k)[1], e)
                self.edits[k] = e
            self.form.addRow(box)
        self.finish()

    def result_values(self) -> dict:
        return {k: e.text().replace("_", "") for k, e in self.edits.items()}


class PasswordDialog(_Base):
    """mf162/pi1 "Enter the password" gate for advanced settings (ss11/ss12 advanced mode).

    The original compares against a fixed numeric code stored next to ``ss11``/``mf162`` in
    MainApp .rdata; this rebuild only offers an optional, user-configured password and does
    not reproduce vendor/licence passwords.
    """

    def __init__(self, tr: Translator, parent=None):
        super().__init__(tr, "pi1", parent)
        self.edit = self.row("mf162", QLineEdit(), param_label=False)
        self.edit.setEchoMode(QLineEdit.Password)
        self.finish()


class AlarmDialog(QDialog):
    """mf123 "Alarm": table of time (gp84), description (gp85), status (gp93); gp90 clear."""

    def __init__(self, tr: Translator, alarms: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.tr("mf123"))
        lay = QVBoxLayout(self)
        t = QTableWidget(len(alarms), 2)
        t.setHorizontalHeaderLabels([tr.tr("gp84"), tr.tr("gp85")])
        for i, (txt, ts) in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(ts)))
            t.setItem(i, 1, QTableWidgetItem(tr.tr(txt)))
        t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AboutDialog(QDialog):
    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr.tr("mf24"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Mlaser rebuild (PySide6)\nCross-platform reimplementation of the "
                             "Mlaser v0.0.1.51 user interface.  MIT licence."))
        lic = QLabel("Uses Qt for Python (PySide6), licensed under the GNU LGPL v3: you may replace "
                     "the Qt / PySide6 libraries with your own build.  Licence texts: "
                     "packaging/licenses/ (LGPL-3.0, GPL-3.0) and docs/ASSETS.md.  Bundled fonts: "
                     "Noto Sans SC / Noto Sans Arabic subsets (SIL Open Font License 1.1).")
        lic.setWordWrap(True)
        lay.addWidget(lic)
        try:
            from mlaser import netcheck
            res = netcheck.last_result()
            net = res.text() if res is not None else "Network self-test: not run yet (Options+ > Network self-test)"
        except Exception as e:  # noqa: BLE001 - informative only
            net = f"Network self-test unavailable: {e}"
        nl = QLabel(net)
        nl.setWordWrap(True)
        nl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(nl)
        try:
            from mlaser.parity_ports import kernel_status
            kern = kernel_status().text()     # C kernel or Python port (MLASER_NATIVE, self-check)
        except Exception as e:  # noqa: BLE001 - informative only
            kern = f"Emitter kernel: status unavailable ({e})"
        kl = QLabel(kern)
        kl.setObjectName("about_kernel")
        kl.setWordWrap(True)
        kl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(kl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
