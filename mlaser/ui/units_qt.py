"""Qt widgets and helpers for display units (:mod:`mlaser.services.units`, docs/user/units.md).

* :class:`QuantitySpinBox` ``(kind, parent=None)``: a spin box whose :meth:`~QuantitySpinBox.value`,
  :meth:`~QuantitySpinBox.setValue`, range and ``valueChanged`` are always in the **internal**
  units (mm, mm/s, bar ...).  It shows the value in the display units in force, relabels and
  rescales live when they change, and accepts typed units (``1/8"``, ``3.2 mm``, ``2'6"``).
  An untouched value is never rewritten: the box keeps the exact internal value it was given and
  only a user edit replaces it, so opening and saving a dialog does not drift a value by a
  rounding step.  Drop-in for the ``QDoubleSpinBox`` calls the application uses
  (``setRange``, ``setDecimals``, ``setSingleStep``, ``setSuffix`` (ignored: the unit is the
  suffix), ``setSpecialValueText``, ``setKeyboardTracking``); decimals and step given by the
  caller are the *metric* ones, the imperial ones come from the kind.
* :class:`QuantityLabel`: a read-only label for an internal value (optionally in a template).
* :func:`on_units_changed` ``(widget, fn)``: call ``fn(units)`` now and on every change while
  ``widget`` lives (headers, status texts, tables).
* :func:`fmt`, :func:`header`, :func:`set_cell`, :func:`cell_value`, :func:`status_xy`:
  table-cell and status-bar text.
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QAbstractSpinBox, QLabel, QStyle, QStyleOptionSpinBox, QTableWidgetItem, QWidget

from mlaser.services import units as U
from mlaser.services.units import Units

#: the item data role that keeps the internal value of a table cell (:func:`set_cell`)
VALUE_ROLE = Qt.UserRole + 41
KIND_ROLE = Qt.UserRole + 42


class _Hub(QObject):
    """One Qt signal for every unit change (connections die with their receivers)."""

    changed = Signal(object)


_HUB: list[_Hub] = []


def hub() -> _Hub:
    if not _HUB:
        h = _Hub()
        _HUB.append(h)
        U.subscribe(lambda u: h.changed.emit(u))
    return _HUB[0]


class _Relay(QObject):
    """Child of a widget: calls ``fn(units)`` on every change, until the widget is destroyed."""

    def __init__(self, parent: QObject, fn: Callable[[Units], None]) -> None:
        super().__init__(parent)
        self._fn = fn
        hub().changed.connect(self._on)

    def _on(self, u: Units) -> None:
        self._fn(u)


def on_units_changed(widget: QObject, fn: Callable[[Units], None], now: bool = True) -> QObject:
    """Call ``fn(units)`` now (``now``) and whenever the display units change, for as long as
    ``widget`` exists.  Returns the relay object (delete it to stop)."""
    r = _Relay(widget, fn)
    if now:
        fn(U.current())
    return r


# --------------------------------------------------------------------------- text helpers
def fmt(value: Optional[float], kind: str = "length", **kw) -> str:
    """``value`` (internal units) in the current display units, with the unit."""
    return U.current().fmt(value, kind, **kw)


def number(value: float, kind: str = "length", decimals: Optional[int] = None, trim: bool = False) -> str:
    return U.current().number(value, kind, decimals, trim)


def suffix(kind: str = "length") -> str:
    return U.current().suffix(kind)


def header(title: str, kind: str) -> str:
    """A column header with the unit: "Width (in)"."""
    return U.current().header(title, kind)


def status_xy(x: float, y: float, decimals: Optional[int] = None) -> str:
    """Status-bar coordinates: "X 1.250  Y 0.500 in"."""
    return U.current().fmt_xy(x, y, "length", decimals)


def set_cell(item: QTableWidgetItem, value: Optional[float], kind: str, unit: bool = False,
             decimals: Optional[int] = None, trim: bool = False) -> QTableWidgetItem:
    """Show ``value`` (internal units) in a table cell; the internal value is kept in
    :data:`VALUE_ROLE` (read it back with :func:`cell_value`, never by parsing the text)."""
    item.setData(VALUE_ROLE, None if value is None else float(value))
    item.setData(KIND_ROLE, kind)
    item.setText("" if value is None else U.current().fmt(value, kind, decimals=decimals, trim=trim, unit=unit))
    return item


def cell(value: Optional[float], kind: str, **kw) -> QTableWidgetItem:
    return set_cell(QTableWidgetItem(), value, kind, **kw)


def cell_value(item: Optional[QTableWidgetItem]) -> Optional[float]:
    if item is None:
        return None
    v = item.data(VALUE_ROLE)
    return None if v is None else float(v)


# --------------------------------------------------------------------------- spin box
class QuantitySpinBox(QAbstractSpinBox):
    """See the module docstring.  ``value()`` is always in the kind's internal unit."""

    valueChanged = Signal(float)
    textChanged = Signal(str)

    def __init__(self, kind: str = "length", parent: Optional[QWidget] = None, *,
                 minimum: float = 0.0, maximum: float = 99.99, decimals: Optional[int] = None,
                 step: Optional[float] = None, value: Optional[float] = None) -> None:
        super().__init__(parent)
        Units.kind(kind)
        self._kind = kind
        self._min, self._max = float(minimum), float(maximum)
        self._value = min(max(0.0, self._min), self._max)   # like QDoubleSpinBox: 0, clamped
        self._base = self._value          # the value before the current typing (no-drift rule)
        self._metric_dec = decimals
        self._metric_step = step
        self._prefix = ""
        self._units = U.current()
        self._editing = False
        self.setAccelerated(True)
        self.lineEdit().textEdited.connect(self._edited)
        self.editingFinished.connect(self._commit)
        hub().changed.connect(self._units_changed)
        self._render()
        if value is not None:
            self.setValue(value)

    # ------------------------------------------------------------------ kind / units
    def kind(self) -> str:
        return self._kind

    def setKind(self, kind: str) -> None:
        Units.kind(kind)
        self._kind = kind
        self._render()
        self.updateGeometry()

    def units(self) -> Units:
        return self._units

    def _metric_unit(self) -> str:
        return Units("metric").unit(self._kind)

    def decimals(self) -> int:
        """The decimals shown now (display units)."""
        u = self._units
        if self._metric_dec is not None and u.unit(self._kind) == self._metric_unit():
            return int(self._metric_dec)
        return u.decimals(self._kind)

    def setDecimals(self, n: int) -> None:
        """Metric decimals (the imperial ones come from the kind)."""
        self._metric_dec = int(n)
        self._render()

    def singleStep(self) -> float:
        """The arrow step now, in display units."""
        u = self._units
        if self._metric_step is not None and u.unit(self._kind) == self._metric_unit():
            return float(self._metric_step)
        return u.step(self._kind)

    def setSingleStep(self, step: float) -> None:
        """Metric arrow step (in the metric display unit)."""
        self._metric_step = float(step)

    def suffix(self) -> str:
        return " " + self._units.suffix(self._kind)

    def setSuffix(self, _s: str) -> None:
        """Ignored: the suffix is the display unit (kept for QDoubleSpinBox compatibility)."""

    def prefix(self) -> str:
        return self._prefix

    def setPrefix(self, p: str) -> None:
        self._prefix = p
        self._render()

    def _units_changed(self, u: Units) -> None:
        if u == self._units:
            return
        self._units = u
        self._render()
        self.updateGeometry()

    # ------------------------------------------------------------------ range / value
    def minimum(self) -> float:
        return self._min

    def maximum(self) -> float:
        return self._max

    def setMinimum(self, v: float) -> None:
        self.setRange(v, max(self._max, v))

    def setMaximum(self, v: float) -> None:
        self.setRange(min(self._min, v), v)

    def setRange(self, lo: float, hi: float) -> None:
        self._min, self._max = float(lo), float(max(lo, hi))
        if not (self._min <= self._value <= self._max):
            self._set(min(max(self._value, self._min), self._max), emit=True)
        else:
            self._render()
        self.updateGeometry()

    def value(self) -> float:
        """The value in internal units (exactly what was set, unless the user edited it)."""
        return self._value

    def setValue(self, v: float) -> None:
        self._set(float(v), emit=True)

    def displayValue(self) -> float:
        return self._units.to_display(self._value, self._kind)

    def _clamp(self, v: float) -> float:
        if v != v:
            return self._value
        return min(max(v, self._min), self._max)

    def _set(self, v: float, emit: bool, render: bool = True) -> None:
        v = self._clamp(v)
        changed = v != self._value
        self._value = v
        if render:
            self._render()
        if changed and emit:
            self.valueChanged.emit(v)

    # ------------------------------------------------------------------ text
    def textFromValue(self, v: float) -> str:
        if self.specialValueText() and v == self._min:
            return self.specialValueText()
        return self._prefix + self._units.fmt(v, self._kind, decimals=self.decimals())

    def valueFromText(self, text: str) -> float:
        """Typed text -> internal units (raises ValueError)."""
        t = text.strip()
        if self._prefix and t.startswith(self._prefix.strip()):
            t = t[len(self._prefix.strip()):].strip()
        if self.specialValueText() and t == self.specialValueText():
            return self._min
        return self._units.parse(t, self._kind)

    def cleanText(self) -> str:
        return self._units.number(self._value, self._kind, self.decimals())

    def text(self) -> str:
        return self.lineEdit().text()

    def _render(self) -> None:
        self._base = self._value
        le = self.lineEdit()
        t = self.textFromValue(self._value)
        if le.text() != t:
            le.setText(t)
            self.textChanged.emit(t)

    def _edited(self, text: str) -> None:
        try:
            v = self.valueFromText(text)
        except ValueError:
            return
        if not math.isfinite(v):
            return
        v = self._unless_same(v)
        if self.keyboardTracking():
            self._set(v, emit=True, render=False)
        self.textChanged.emit(text)

    def _commit(self) -> None:
        try:
            v = self.valueFromText(self.lineEdit().text())
        except ValueError:
            self._render()
            return
        self._set(self._unless_same(v), emit=True)

    def _unless_same(self, v: float) -> float:
        """A typed value that shows the same as the value before the edit is that value, exactly
        (retyping "0.122 in" keeps 3.1 mm, not 3.0988)."""
        dec = self.decimals()
        if self._units.number(v, self._kind, dec) == self._units.number(self._base, self._kind, dec):
            return self._base
        return v

    def validate(self, text: str, pos: int):
        try:
            self.valueFromText(text)
            return QValidator.Acceptable, text, pos
        except ValueError:
            return QValidator.Intermediate, text, pos

    def fixup(self, text: str) -> str:
        return self.textFromValue(self._value)

    def interpretText(self) -> None:
        self._commit()

    # ------------------------------------------------------------------ stepping
    def stepBy(self, steps: int) -> None:
        u, k = self._units, self._kind
        d = u.to_display(self._value, k)
        dec = self.decimals()
        nd = round(d + steps * self.singleStep(), dec)
        nv = u.from_display(nd, k)
        if nv == self._value:
            nv = u.from_display(round(d + steps * 10.0 ** -dec, dec), k)
        self._set(nv, emit=True)
        self.lineEdit().selectAll() if self.hasFocus() else None

    def stepEnabled(self):
        if self.isReadOnly():
            return QAbstractSpinBox.StepNone
        f = QAbstractSpinBox.StepNone
        if self._value > self._min or self.wrapping():
            f |= QAbstractSpinBox.StepDownEnabled
        if self._value < self._max or self.wrapping():
            f |= QAbstractSpinBox.StepUpEnabled
        return f

    # ------------------------------------------------------------------ size
    def _hint(self, texts: list[str]) -> QSize:
        fm = self.fontMetrics()
        w = max(fm.horizontalAdvance(t) for t in texts) + 6
        h = super().sizeHint().height()
        opt = QStyleOptionSpinBox()
        self.initStyleOption(opt)
        return self.style().sizeFromContents(QStyle.CT_SpinBox, opt, QSize(w + 2, h), self)

    def sizeHint(self) -> QSize:
        """Sized to the content in the current units (no fixed width): the current value and the
        range ends, but never wider than a 5-digit value (a ±1e6 range does not widen a form)."""
        u, k, dec = self._units, self._kind, self.decimals()
        texts = [self.textFromValue(self._value)]
        for v in (self._min, self._max):
            if abs(u.to_display(v, k)) < 1e5:
                texts.append(self.textFromValue(v))
        if len(texts) < 3:
            texts.append(self._prefix + "-" + "8" * 5 + ("." + "8" * dec if dec else "") + " " + u.suffix(k))
        return self._hint(texts)

    def minimumSizeHint(self) -> QSize:
        """The current value must fit; the rest of the width is the layout's to give."""
        return self._hint([self.textFromValue(self._value)])


class QuantityLabel(QLabel):
    """A read-only value in the display units; ``template`` gets the formatted value as ``{}``
    (``QuantityLabel("length", template="Width {}")``)."""

    def __init__(self, kind: str = "length", parent: Optional[QWidget] = None, value: Optional[float] = None,
                 template: str = "{}", decimals: Optional[int] = None, trim: bool = False,
                 missing: str = "-") -> None:
        super().__init__(parent)
        Units.kind(kind)
        self._kind, self._value, self._template = kind, value, template
        self._decimals, self._trim, self._missing = decimals, trim, missing
        hub().changed.connect(self._on_units)
        self._render()

    def _on_units(self, _u: Units) -> None:
        self._render()

    def kind(self) -> str:
        return self._kind

    def value(self) -> Optional[float]:
        return self._value

    def setValue(self, v: Optional[float]) -> None:
        self._value = None if v is None else float(v)
        self._render()

    def setTemplate(self, t: str) -> None:
        self._template = t
        self._render()

    def _render(self) -> None:
        s = U.current().fmt(self._value, self._kind, decimals=self._decimals, trim=self._trim,
                            missing=self._missing)
        self.setText(self._template.format(s))


# --------------------------------------------------------------------------- schema settings
def kind_for(unit: str) -> Optional[str]:
    """The quantity kind of a schema / registry unit string ("mm", "mm / s", "bar"), or None."""
    return U.kind_for_unit(unit)


def range_text(lo: Optional[float], hi: Optional[float], unit: str = "", kind: Optional[str] = None) -> str:
    """ "0 … 50 mm" for a range given in the internal ``unit`` (converted when it has a kind)."""
    k = kind or kind_for(unit)
    if lo is None or hi is None:
        return ""
    if k is None:
        return f"{lo:g} … {hi:g} {unit}".rstrip()
    u = U.current()
    return f"{u.to_display(lo, k):.{u.decimals(k)}g} … {u.to_display(hi, k):.{max(u.decimals(k), 4)}g} {u.suffix(k)}"


def spin_for_unit(unit: str, lo: float, hi: float, decimals: int = 3, step: float = 1.0,
                  parent: Optional[QWidget] = None) -> QAbstractSpinBox:
    """A spin box for a value stored in ``unit``: a :class:`QuantitySpinBox` when the unit
    converts (mm, mm/s, bar ...), else a QDoubleSpinBox with the unit as its suffix (%, Hz, ms)."""
    k = kind_for(unit)
    if k is not None:
        return QuantitySpinBox(k, parent, minimum=lo, maximum=hi, decimals=decimals, step=step)
    from PySide6.QtWidgets import QDoubleSpinBox
    s = QDoubleSpinBox(parent)
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    if unit:
        s.setSuffix(f" {unit}")
    return s


def spin_for_setting(setting, parent: Optional[QWidget] = None, decimals: int = 3) -> QAbstractSpinBox:
    """:func:`spin_for_unit` for a ConfigService :class:`~mlaser.services.config.Setting`
    (float kinds; the value it holds stays in the setting's own unit)."""
    lo = setting.min if setting.min is not None else -1e9
    hi = setting.max if setting.max is not None else 1e9
    return spin_for_unit(setting.unit, lo, hi, decimals, 1.0, parent)
