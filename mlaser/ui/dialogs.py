"""Dialogs. Labels come from lang.txt keys cited next to each field.

The original builds almost every dialog at run time on top of BCGP property grids
(MainApp.exe has only 7 empty dialog templates, RT_DIALOG 319..459), so most dialogs here are
data-driven from :mod:`mlaser.ui.params`.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                               QTableWidget, QTableWidgetItem, QHeaderView)

from . import params as P
from .i18n import Translator


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

    ``values`` maps parameter names (``GP.CutSpeed``...) to values and is edited in place
    on accept.
    """

    def __init__(self, tr: Translator, title_key: str, sections: list[str], values: dict,
                 parent=None):
        super().__init__(parent)
        self.tr_ = tr
        self.values = values
        self.setWindowTitle(tr.tr(title_key))
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        self._editors: dict[str, QWidget] = {}
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
                    it.setToolTip(0, d.name)
                    g.addChild(it)
                    ed = self._editor(d)
                    tree.setItemWidget(it, 1, ed)
                    self._editors[d.name] = ed
                g.setExpanded(True)
            tabs.addTab(tree, sec)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _editor(self, d: P.ParamDef) -> QWidget:
        v = self.values.get(d.name)
        if d.is_bool():
            w = QCheckBox()
            w.setChecked(bool(v))
            return w
        w = QLineEdit("" if v is None else str(v))
        default = d.hint[0] if d.hint else ""
        if v is None and default not in ("", None, 'L""'):
            w.setPlaceholderText(str(default))
        return w

    def _ok(self) -> None:
        for name, w in self._editors.items():
            if isinstance(w, QCheckBox):
                if w.isChecked() or name in self.values:
                    self.values[name] = w.isChecked()
            else:
                t = w.text().strip()
                if not t:
                    continue
                try:
                    self.values[name] = float(t) if any(c in t for c in ".eE") else int(t)
                except ValueError:
                    self.values[name] = t
        self.accept()


class LayerParamDialog(ParamGridDialog):
    """lp0 "Layer Parameters": every GP.* field of one layer. Core fields go to LayerParams."""

    def __init__(self, tr: Translator, layer, parent=None):
        vals = dict(layer.extra.get("gp", {}))
        for gp, attr in P.LAYER_FIELD_MAP.items():
            vals[gp] = getattr(layer, attr)
        super().__init__(tr, "lp0", ["LayerParam"], vals, parent)
        self.layer = layer
        self.setWindowTitle(f"{tr.tr('lp0')} - {tr.tr('gp83')} {layer.index}")
        self.accepted.connect(self._store)

    def _store(self) -> None:
        for gp, attr in P.LAYER_FIELD_MAP.items():
            if gp in self.values:
                try:
                    setattr(self.layer, attr, type(getattr(self.layer, attr))(self.values[gp]))
                except (TypeError, ValueError):
                    pass
        self.layer.extra["gp"] = {k: v for k, v in self.values.items() if k not in P.LAYER_FIELD_MAP}


class LeadDialog(_Base):
    """mf205 "Lead line parameters": pd315 Type, pd316 Angle, pd317 Length, pd599 Radius,
    pd318 start point mode, pd319 start point position, pd320 only closed, pd321 only selected,
    pd322 "outer layer is inside cut". Separate lead-in / lead-out (``Craft.lead_in/out``)."""

    KINDS = ["mf123:-", "mf45", "mf48", "mf87"]   # none, line, arc, line+arc (labels; kind ints per model)

    def __init__(self, tr: Translator, lead_in, lead_out, parent=None):
        super().__init__(tr, "mf205", parent)
        self._w = {}
        tabs = QTabWidget()
        self.lay.insertWidget(0, tabs)
        for name, lead, title in (("in", lead_in, "Lead in"), ("out", lead_out, "Lead out")):
            page = QWidget()
            f = QFormLayout(page)
            kind = QComboBox()
            kind.addItems([tr.tr("mp123"), tr.tr("mf45"), tr.tr("mv6"), tr.tr("mf45") + "+" + tr.tr("mv6")])
            kind.setCurrentIndex(max(0, min(3, lead.kind)))
            ang = _spin(0, 180, lead.angle_deg, 1)
            ln = _spin(0, 1000, lead.length, 3, 0.5)
            rad = _spin(0, 1000, lead.radius, 3, 0.5)
            corner = QCheckBox()
            corner.setChecked(lead.at_corner)
            f.addRow(tr.tr_param("pd315")[1], kind)
            f.addRow(tr.tr_param("pd316")[1], ang)
            f.addRow(tr.tr_param("pd317")[1], ln)
            f.addRow(tr.tr_param("pd599")[1], rad)
            f.addRow(tr.tr_param("pd318")[1], corner)
            self._w[name] = (lead, kind, ang, ln, rad, corner)
            tabs.addTab(page, title)
        self.only_closed = self.row("pd320", QCheckBox())
        self.only_closed.setChecked(True)
        self.only_selected = self.row("pd321", QCheckBox())
        self.only_selected.setChecked(True)
        self.finish()
        self.accepted.connect(self._store)

    def _store(self):
        for lead, kind, ang, ln, rad, corner in self._w.values():
            lead.kind = kind.currentIndex()
            lead.angle_deg = ang.value()
            lead.length = ln.value()
            lead.radius = rad.value()
            lead.at_corner = corner.isChecked()


class KerfDialog(_Base):
    """mf486 "Set Compensate": pd506 Compensate Type (inside/outside), pd507 width."""

    def __init__(self, tr: Translator, kerf_comp: int = 1, width: float = 0.1, parent=None):
        super().__init__(tr, "mf113", parent)
        self.kind = self.row("pd506", QComboBox())
        self.kind.addItems([tr.tr("mf98"), tr.tr("mf99")])   # 0 inner (阴切), 1 outer (阳切)
        self.kind.setCurrentIndex(0 if kerf_comp == 0 else 1)
        self.width = self.row("pd507", _spin(0, 10, width, 3, 0.05))
        self.finish()

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
        self.distance = self.row("pd345", _spin(1, 10000, 100, 1))
        self.length = self.row("pd346", _spin(0.01, 50, 0.5, 2, 0.1))
        self.min_size = self.row("pd314_1", _spin(0, 10000, 0, 1))
        self.finish()


class ArrayDialog(_Base):
    """mf79 "Array": pd332 rows, pd333 columns, pd334 offset type, pd335/pd336 spacing,
    pd337/pd338 directions."""

    def __init__(self, tr: Translator, parent=None):
        super().__init__(tr, "mf79", parent)
        self.rows = self.row("pd332", QSpinBox()); self.rows.setRange(1, 1000); self.rows.setValue(2)
        self.cols = self.row("pd333", QSpinBox()); self.cols.setRange(1, 1000); self.cols.setValue(2)
        self.offset_type = self.row("pd334", QComboBox())
        self.offset_type.addItems(["Spacing", "Offset"])
        self.row_gap = self.row("pd335", _spin(-1e5, 1e5, 10, 3))
        self.col_gap = self.row("pd336", _spin(-1e5, 1e5, 10, 3))
        self.row_dir = self.row("pd337", QComboBox()); self.row_dir.addItems(["+", "-"])
        self.col_dir = self.row("pd338", QComboBox()); self.col_dir.addItems(["+", "-"])
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
                             "Mlaser v0.0.1.51 user interface."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
