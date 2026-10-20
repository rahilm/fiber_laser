"""Dialogs. Labels come from lang.txt keys cited next to each field.

The original builds almost every dialog at run time on top of BCGP property grids
(MainApp.exe has only 7 empty dialog templates, RT_DIALOG 319..459), so most dialogs here are
data-driven from :mod:`mlaser.ui.params` (the ConfigService schema).
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QLabel, QLineEdit, QSpinBox,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                               QTableWidgetItem, QHeaderView)

from mlaser.services import units as U

from . import params as P
from . import style as S
from .style import Dialog
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


#: a unit written into a label ("Angle (deg)", "Width (MM)", "Power (%)"): it goes to the
#: field's suffix instead (DESIGN.md §3: units in the field only)
_UNIT_IN_LABEL = re.compile(r"\s*[(（]\s*(mm/s|mm/min|mm|%|°|deg|degrees?|ms|s|hz|bar|mpa|w|kw)\s*[)）]\s*[:：]?\s*$",
                            re.I)
_SUFFIX = {"%": " %", "°": " °", "deg": " °", "degree": " °", "degrees": " °", "ms": " ms", "s": " s",
           "hz": " Hz", "w": " W", "kw": " kW"}
#: label units that are display-unit quantities (a QuantitySpinBox of that kind)
QUANTITY_UNIT = {"mm": "length", "mm/s": "speed", "mm/min": "speed", "bar": "pressure", "mpa": "pressure"}


def split_unit(label: str) -> tuple[str, str]:
    """(label without a trailing "(unit)", the unit in lower case or "")."""
    m = _UNIT_IN_LABEL.search(label or "")
    if not m:
        return (label or "").strip().rstrip(":：").strip(), ""
    return label[:m.start()].strip().rstrip(":：").strip(), m.group(1).lower()


def suffix_for(unit: str) -> str:
    """The spin-box suffix of a label unit that is not a display-unit quantity ("%", "°", "s")."""
    return _SUFFIX.get(unit.lower(), f" {unit}" if unit else "")


class _Base(Dialog):
    """A small value prompt on the one dialog frame (style.Dialog): window title, a form, OK and
    Cancel in the platform's order."""

    def __init__(self, tr: Translator, title_key: str, parent=None, explanation: str = ""):
        super().__init__(parent, tr.tr(title_key), explanation=explanation)
        self.tr_ = tr
        ok, cancel = self.buttons.button(QDialogButtonBox.Ok), self.buttons.button(QDialogButtonBox.Cancel)
        ok.setText(tr.tr("ap1"))
        cancel.setText(tr.tr("ap0"))

    def finish(self):
        """Kept for the subclasses: the frame already holds its buttons."""
        self.adjustSize()

    def row(self, key: str, w: QWidget, param_label: bool = True, unit_kind: Optional[str] = None) -> QWidget:
        label = self.tr_.tr_param(key)[1] if param_label else self.tr_.tr(key)
        label, unit = split_unit(label)
        if unit and isinstance(w, (QDoubleSpinBox, QSpinBox)) and not w.suffix():
            w.setSuffix(suffix_for(unit))
        S.form_row(self.form(), label, w, unit_kind=unit_kind)
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


# lp0 "Layer parameters" is no longer a dialog: a layer's values are edited in one place, the
# layer inspector of Process › Layers (mlaser.ui.workspaces.layer_inspector).


# Lead, kerf and micro-joint values are edited in one place, Process › Properties (SETTINGS_MAP
# 3.3); their dialogs (mf205, mf113, mf103) are gone and the commands open the Properties field.


class ValueDialog(_Base):
    """Single numeric value (pd339 rotate angle, pd348 scale percent, mf121 simulation speed...).
    ``suffix``: the value's unit (" °", " %", " ×"); a unit in the field's label moves there."""

    def __init__(self, tr: Translator, title_key: str, field_key: str, value: float,
                 lo=-1e6, hi=1e6, parent=None, suffix: str = ""):
        super().__init__(tr, title_key, parent)
        spin = _spin(lo, hi, value, 3)
        if suffix:
            spin.setSuffix(suffix)
        self.value = self.row(field_key, spin)
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
            sec, body = S.section(tr.tr(title))
            host = QWidget()
            f = S.form(host)
            body.addWidget(host)
            for k, d in zip(keys, default):
                e = QLineEdit(values.get(k, d))
                e.setInputMask("000.000.000.000;_")
                e.setText(values.get(k, d))
                S.form_row(f, tr.tr_param(k)[1], e)
                self.edits[k] = e
            self.body.addWidget(sec)
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
        self.setWindowTitle(split_unit(self.windowTitle())[0])
        self.edit = self.row("mf162", QLineEdit(), param_label=False)
        self.edit.setEchoMode(QLineEdit.Password)
        self.finish()


class AlarmDialog(Dialog):
    """mf123 "Alarm": table of time (gp84), description (gp85), status (gp93); gp90 clear."""

    def __init__(self, tr: Translator, alarms: list, parent=None):
        super().__init__(parent, tr.tr("mf123"), buttons=QDialogButtonBox.Close)
        t = S.table([S.Col(tr.tr("gp84"), priority=1), S.Col(tr.tr("gp85"), stretch=True)])
        t.setRowCount(len(alarms))
        for i, (txt, ts) in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(ts)))
            t.setItem(i, 1, QTableWidgetItem(tr.tr(txt)))
        self.table = t
        self.body.addWidget(t)


class AboutDialog(Dialog):
    def __init__(self, tr: Translator, parent=None):
        super().__init__(parent, tr.tr("mf24"), buttons=QDialogButtonBox.Ok,
                         heading_text="Mlaser",
                         explanation="Mlaser rebuild (PySide6): a cross-platform reimplementation of the "
                                     "Mlaser v0.0.1.51 user interface.  MIT licence.")
        lic = S.caption("Uses Qt for Python (PySide6), licensed under the GNU LGPL v3: you may replace "
                        "the Qt / PySide6 libraries with your own build.  Licence texts: "
                        "packaging/licenses/ (LGPL-3.0, GPL-3.0) and docs/ASSETS.md.  Bundled fonts: "
                        "Noto Sans SC / Noto Sans Arabic subsets (SIL Open Font License 1.1).")
        self.body.addWidget(lic)
        try:
            from mlaser import netcheck
            res = netcheck.last_result()
            net = res.text() if res is not None else "Network self-test: not run yet (Settings > Network self-test)"
        except Exception as e:  # noqa: BLE001 - informative only
            net = f"Network self-test unavailable: {e}"
        nl = QLabel(net)
        nl.setWordWrap(True)
        nl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(nl)
        try:
            from mlaser.parity_ports import kernel_status
            kern = kernel_status().text()     # C kernel or Python port (MLASER_NATIVE, self-check)
        except Exception as e:  # noqa: BLE001 - informative only
            kern = f"Emitter kernel: status unavailable ({e})"
        kl = QLabel(kern)
        kl.setObjectName("about_kernel")
        kl.setWordWrap(True)
        kl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(kl)
