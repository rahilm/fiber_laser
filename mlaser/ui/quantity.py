"""Unit-bearing spin boxes for the Process & technology panels (Properties, Layer parameters,
technology editor).

The Units track provides :class:`mlaser.ui.units_qt.QuantitySpinBox` (stored value always in the
internal unit, mm / mm/s / bar ...; it relabels and rescales when the user switches between
metric and imperial).  Until that module is in the tree this file supplies a **shim with the
same name and constructor** (metric display only), so the panels are written against the final
API and need no change when it lands: ``QuantitySpinBox(kind, parent, *, minimum, maximum,
decimals, step, value)``, ``value()`` / ``setValue()`` in the internal unit, ``valueChanged``,
``editingFinished``.

:func:`kind_for_unit` maps a registry unit string ("mm", "mm/s", "bar" ...) to a kind, or None
for values that are never converted (%, Hz, ms ...).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDoubleSpinBox, QWidget

try:                                     # the Units track's widget
    from mlaser.ui.units_qt import QuantitySpinBox  # type: ignore[import-not-found,unused-ignore]
    HAVE_UNITS = True
except ImportError:                      # shim (metric only) until it lands
    HAVE_UNITS = False

    _SUFFIX = {"length": "mm", "small_length": "mm", "speed": "mm/s", "accel": "mm/s²", "pressure": "bar",
               "angle": "°", "time": "s", "flow": "l/min", "density": "g/cm³", "mass": "kg", "area": "mm²"}
    _DEC = {"length": 2, "small_length": 3, "speed": 1, "pressure": 2, "angle": 1, "flow": 1, "density": 3}
    _STEP = {"length": 1.0, "small_length": 0.01, "speed": 1.0, "pressure": 0.1, "angle": 1.0, "flow": 1.0,
             "density": 0.01}

    class QuantitySpinBox(QDoubleSpinBox):  # type: ignore[no-redef]
        """Shim of ``mlaser.ui.units_qt.QuantitySpinBox`` (metric display)."""

        def __init__(self, kind: str = "length", parent: Optional[QWidget] = None, *,
                     minimum: float = 0.0, maximum: float = 99.99, decimals: Optional[int] = None,
                     step: Optional[float] = None, value: Optional[float] = None) -> None:
            super().__init__(parent)
            self._kind = kind
            self.setDecimals(decimals if decimals is not None else _DEC.get(kind, 2))
            self.setRange(minimum, maximum)
            self.setSingleStep(step if step is not None else _STEP.get(kind, 1.0))
            self.setAccelerated(True)
            unit = _SUFFIX.get(kind, "")
            if unit:
                self.setSuffix(f" {unit}")
            if value is not None:
                self.setValue(value)

        def kind(self) -> str:
            return self._kind


_UNIT_KIND = {
    "mm": "length", "mm/s": "speed", "mm / s": "speed", "mm/min": "speed", "bar": "pressure",
    "MPa": "pressure", "°": "angle", "l/min": "flow", "g/cm³": "density",
}


def kind_for_unit(unit: str) -> Optional[str]:
    """The quantity kind of a registry / display unit, or None when it is not converted."""
    try:
        from mlaser.services.units import kind_for_unit as k  # type: ignore[import-not-found,unused-ignore]
        return k(unit)
    except ImportError:
        return _UNIT_KIND.get((unit or "").strip())


def quantity(kind: str, lo: float, hi: float, decimals: Optional[int] = None,
             step: Optional[float] = None) -> QuantitySpinBox:
    """A QuantitySpinBox with the panels' defaults (no keyboard tracking: a value is taken when
    the field is confirmed)."""
    s = QuantitySpinBox(kind, minimum=lo, maximum=hi, decimals=decimals, step=step)
    s.setKeyboardTracking(False)
    return s


__all__ = ["QuantitySpinBox", "kind_for_unit", "quantity", "HAVE_UNITS"]
