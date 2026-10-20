"""Choosing a technology record (recipe): material × thickness chips, then gas and nozzle
(cascading over the records of the store).  Used by the layer inspector (which recipe a layer
follows) and the Library page (which record to edit)."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QLabel, QToolButton, QVBoxLayout, QWidget

from mlaser.services import technology as T
from mlaser.ui.techform import FlowLayout, ResponsiveForm


def _nz(nozzle: str, power_w: int) -> str:
    return f"{nozzle}\x1f{int(power_w)}"


def _nz_split(v: str) -> tuple[str, int]:
    name, _, p = str(v).rpartition("\x1f")
    return name, int(p or 0)


def _nz_label(v: str) -> str:
    name, p = _nz_split(v)
    return name + (f" · {p / 1000:g} kW" if p else "")


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


class RecipePicker(QWidget):
    """Chips + gas + nozzle; :meth:`picked` is the chosen record (None while incomplete).
    The material / thickness combos are kept (hidden) for keyboard use and tests."""

    changed = Signal()

    def __init__(self, tr: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tr_, self.store = tr, store
        t = tr.tr
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.matrix = MaterialPicker()
        self.matrix.picked.connect(self._matrix_picked)
        v.addWidget(self.matrix)
        pf = ResponsiveForm()
        self.material, self.thickness, self.gas, self.nozzle = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        for key, w in (("process.material", self.material), ("process.thickness", self.thickness),
                       ("process.gas", self.gas), ("process.nozzle", self.nozzle)):
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(8)
            lab = QLabel(t(key))
            lab.setBuddy(w)
            w.setAccessibleName(t(key))
            pf.form.addRow(lab, w)
        pf.form.setRowVisible(self.material, False)
        pf.form.setRowVisible(self.thickness, False)
        self.form = pf
        v.addWidget(pf)
        self.material.currentIndexChanged.connect(lambda _i: self._cascade(1))
        self.thickness.currentIndexChanged.connect(lambda _i: self._cascade(2))
        self.gas.currentIndexChanged.connect(lambda _i: self._cascade(3))
        self.nozzle.currentIndexChanged.connect(lambda _i: self._done())
        self._all: list[T.Technology] = []
        self.reload()

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

    @staticmethod
    def _combo_set(combo: QComboBox, values: list, fmt=str) -> None:
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
        self._done()

    def _done(self) -> None:
        self.matrix.set_current(self.material.currentData(), self.thickness.currentData())
        self.changed.emit()

    def _matrix_picked(self, material: str, thickness: float) -> None:
        i = self.material.findData(material)
        if i >= 0:
            self.material.setCurrentIndex(i)
            self._cascade(1)
        j = self.thickness.findData(thickness)
        if j >= 0:
            self.thickness.setCurrentIndex(j)
            self._cascade(2)

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
        self._done()


__all__ = ["RecipePicker", "MaterialPicker", "_nz", "_nz_split", "_nz_label"]
