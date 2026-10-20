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
                    it = QTreeWidgetItem([label + (f" ({d.unit})" if d.unit else "")])
                    it.setToolTip(0, d.key)
                    g.addChild(it)
                    ed = self._editor(d)
                    tree.setItemWidget(it, 1, ed)
                    self._editors[d.key] = ed
                    self._settings[d.key] = d
                    self._initial[d.key] = self._read(ed)
                g.setExpanded(True)
            tabs.addTab(tree, tr.tr(f"ws.machine.hw_{sec}") if tr.has(f"ws.machine.hw_{sec}") else sec)
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
            if d.kind == "float":
                ws: QWidget = _spin(lo, hi, 0.0, 4)
            else:
                ws = QSpinBox()
                ws.setRange(int(max(lo, -2**31)), int(min(hi, 2**31 - 1)))
            try:
                ws.setValue(float(v) if d.kind == "float" else int(float(v)))
            except (TypeError, ValueError):
                pass
            if d.unit:
                ws.setSuffix(f" {d.unit}")
            return ws
        return QLineEdit("" if v is None else str(v))

    @staticmethod
    def _read(w):
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentIndex()
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


class LayerParamDialog(ParamGridDialog):
    """lp0 "Layer Parameters": every GP.* field of one layer. Core fields go to LayerParams.

    The other values go to ``extra['process']``, the GP.* overrides the job compiler reads
    (``parity_ports.mainapp.layer.LayerView``).  They used to go to ``extra['gp']``, which
    nothing read, so a pierce stage or focus typed here never reached the job (Wave 3 Process
    track; ``mlaser.services.technology`` applies a technology the same way)."""

    def __init__(self, tr: Translator, layer, parent=None):
        ex = layer.extra or {}
        vals = dict(ex.get("values", {}) or {})
        vals.update(ex.get("gp", {}) or {})
        vals.update(ex.get("process", {}) or {})
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
        self.layer.extra.pop("gp", None)
        proc = dict(self.layer.extra.get("process", {}) or {})
        proc.update({k: v for k, v in self.values.items() if k not in P.LAYER_FIELD_MAP})
        self.layer.extra["process"] = proc


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
