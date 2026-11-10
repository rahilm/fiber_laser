"""The widgets a plugin may use (plugin API 2, docs/dev/PLUGIN_STANDARD.md §3).

A plugin builds its own Qt widgets, but the application's layout rules (no horizontal scroll
bar, wrapping in a half-screen window, values in the operator's display units) come from a few
core widgets.  This module is the stable name for them: the core may move or rename them in
``mlaser.ui.units_qt`` and ``mlaser.ui.responsive``; it keeps these names and signatures for
plugin API 2.  Everything else under ``mlaser.ui`` is internal.

* :class:`QuantitySpinBox` ``(kind="length", parent=None, *, decimals=None, ...)``: a number in
  the display units (``value()`` / ``setValue()`` always in the kind's internal unit, mm for
  lengths);
* :func:`on_units_changed` ``(widget, fn, now=True)``: ``fn(units)`` whenever the display units
  change, for as long as ``widget`` exists;
* :class:`WrapCheckBox` ``(text)``: a check box whose text wraps in a narrow window;
* :class:`FlowLayout` ``(parent=None, uniform=False, justify=True)``: a row of buttons that
  wraps onto more rows;
* :class:`ColumnFitter` ``(table, priorities, stretch=key)``: a table whose less important
  columns hide while it is narrow (never a horizontal scroll bar).
"""
from __future__ import annotations

from .responsive import ColumnFitter, FlowLayout, WrapCheckBox
from .units_qt import QuantitySpinBox, on_units_changed

__all__ = ["ColumnFitter", "FlowLayout", "QuantitySpinBox", "WrapCheckBox", "on_units_changed"]
