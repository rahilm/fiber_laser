"""Test helper for the imperial sweeps (tests/test_units_sweep*.py): collect every visible text
of a widget tree (labels, buttons, group titles, spin-box text and suffixes, line-edit text and
placeholders, combo items, table / tree / list headers and cells, tab titles, tooltips, window
titles) and fail on metric unit text.

The allowlist (:data:`ALLOWLIST`) carries a reason for every entry; docs/dev/units-audit.md
repeats it."""
from __future__ import annotations

import re
from typing import Iterable, Iterator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSpinBox, QComboBox, QGroupBox,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QTabBar,
                               QTableWidget, QTextEdit, QTreeWidget, QWidget)

#: metric unit text that must not show in imperial
METRIC_RE = re.compile(r"(?<![\w/])(?:mm|m|cm|bar|MPa|kPa|l)(?:/(?:s|min|h)(?:²|³|2|3)?|²|³|2)?(?![\w²³])"
                       r"|\bm/min\b|\bmm/s\b|\bl/min\b|g/cm³|\bkg\b")

#: (pattern, reason): a text matching ``pattern`` may contain metric units
ALLOWLIST: list[tuple[str, str]] = [
    (r"p\s*/\s*mm", "pulse equivalent (pulses per mm) is the motion card's calibration constant, "
                    "defined per mm by the card and its parameter files; shown as the card reports it"),
    (r"^(?:[A-Za-z][A-Za-z0-9 .\-]*?)\s+\d+(?:\.\d+)?\s*mm\b.*(?:N2|O2|Air|nitrogen|oxygen)",
     "technology database record names (\"Mild steel 3 mm O2\") are data: the key a record is stored and "
     "matched under, not a measurement shown for editing"),
    (r"\bm\d", "not a unit (m followed by digits: an identifier)"),
    (r"(?i)\bms\b", "milliseconds are time (not converted)"),
]

_ALLOW = [(re.compile(p), r) for p, r in ALLOWLIST]

#: widget subtrees converted by the parallel ui-process track (it owns these files in this wave
#: and converts them to QuantitySpinBox itself); the lead removes these entries once it merges.
#: (ancestor class name, or "groupbox:<i18n id>" for a QGroupBox titled with that id, reason)
EXEMPT: list[tuple[str, str]] = []


def _exempt_titles() -> set[str]:
    try:
        from mlaser.ui.i18n import translator
        tr = translator()
        return {tr.tr(e[len("groupbox:"):]) for e, _ in EXEMPT if e.startswith("groupbox:")}
    except Exception:  # noqa: BLE001
        return set()


def exempt(w: QWidget, titles: set[str]) -> bool:
    classes = {e for e, _ in EXEMPT if not e.startswith("groupbox:")}
    p: QWidget | None = w
    while p is not None:
        if type(p).__name__ in classes:
            return True
        if isinstance(p, QGroupBox) and p.title() in titles:
            return True
        p = p.parentWidget()
    return False


def metric_hits(text: str) -> list[str]:
    """The metric unit tokens in ``text`` not covered by the allowlist."""
    if not text:
        return []
    if any(p.search(text) for p, _ in _ALLOW):
        # remove the allowed parts, check the rest
        for p, _ in _ALLOW:
            text = p.sub(" ", text)
    hits = []
    for m in METRIC_RE.finditer(text):
        tok = m.group(0)
        # a bare "m" or "l" only counts after a number ("3 m", "12 l"), never as a word ("I'm")
        if tok in ("m", "l", "cm"):
            before = text[:m.start()].rstrip()
            if not before or not (before[-1].isdigit() or before.endswith(("(",))):
                continue
        hits.append(tok)
    return hits


def _visible(w: QWidget) -> bool:
    return w.isVisible() or w.isVisibleTo(w.window())


def texts(root: QWidget, visible_only: bool = True) -> Iterator[tuple[str, str]]:
    """(where, text) for ``root`` and its descendants."""
    widgets: Iterable[QWidget] = [root] + root.findChildren(QWidget)
    titles = _exempt_titles()
    for w in widgets:
        if visible_only and not w.isVisible():
            continue
        if exempt(w, titles):
            continue
        name = f"{type(w).__name__}({w.objectName() or ''})"
        if w.toolTip():
            yield f"{name}.toolTip", w.toolTip()
        if w.accessibleName():
            yield f"{name}.accessibleName", w.accessibleName()
        if w.isWindow() and w.windowTitle():
            yield f"{name}.windowTitle", w.windowTitle()
        if isinstance(w, QLabel):
            yield f"{name}.text", w.text()
        elif isinstance(w, QAbstractButton):
            yield f"{name}.text", w.text()
        elif isinstance(w, QGroupBox):
            yield f"{name}.title", w.title()
        elif isinstance(w, QAbstractSpinBox):
            yield f"{name}.text", w.text()
            if hasattr(w, "suffix"):
                yield f"{name}.suffix", w.suffix()
        elif isinstance(w, QLineEdit):
            if not isinstance(w.parent(), (QAbstractSpinBox, QComboBox)):
                yield f"{name}.text", w.text()
                yield f"{name}.placeholder", w.placeholderText()
        elif isinstance(w, QComboBox):
            for i in range(w.count()):
                yield f"{name}.item[{i}]", w.itemText(i)
        elif isinstance(w, QTabBar):
            for i in range(w.count()):
                yield f"{name}.tab[{i}]", w.tabText(i)
                yield f"{name}.tabToolTip[{i}]", w.tabToolTip(i)
        elif isinstance(w, (QTextEdit, QPlainTextEdit)):
            # logs show history (what the operator did); checked separately where they matter
            pass
        if isinstance(w, QAbstractItemView) and not isinstance(w, QHeaderView):
            yield from _view_texts(name, w)


def _view_texts(name: str, v: QAbstractItemView) -> Iterator[tuple[str, str]]:
    m = v.model()
    if m is None:
        return
    from PySide6.QtCore import QModelIndex
    root = v.rootIndex() if v.rootIndex().isValid() else QModelIndex()
    try:
        cols = m.columnCount(root)
    except TypeError:            # QListWidget's model: one column
        cols = 1
    for c in range(cols):
        h = m.headerData(c, Qt.Horizontal, Qt.DisplayRole)
        if h:
            yield f"{name}.header[{c}]", str(h)
        t = m.headerData(c, Qt.Horizontal, Qt.ToolTipRole)
        if t:
            yield f"{name}.headerTip[{c}]", str(t)
    rows = min(m.rowCount(root), 200)
    for r in range(rows):
        if hasattr(v, "isRowHidden") and _row_hidden(v, r, root):
            continue
        for c in range(cols):
            if hasattr(v, "isColumnHidden") and v.isColumnHidden(c):
                continue
            idx = m.index(r, c, root)
            for role in (Qt.DisplayRole, Qt.ToolTipRole):
                d = m.data(idx, role)
                if d:
                    yield f"{name}[{r},{c}]", str(d)
    if isinstance(v, (QTreeWidget,)):
        hi = v.headerItem()
        for c in range(v.columnCount()):
            if hi.text(c):
                yield f"{name}.header[{c}]", hi.text(c)
    if isinstance(v, (QTableWidget, QListWidget)):
        pass


def _row_hidden(v, r, root) -> bool:
    try:
        return v.isRowHidden(r, root)
    except TypeError:
        return v.isRowHidden(r)


#: combo-box items that are exactly a unit name: a unit chooser (the units.* overrides in
#: Settings, UN.GasPressureUnit, the storage unit of the layer pressures) lists unit names as its
#: choices; that is not a measurement shown in metric
UNIT_CHOICE_RE = re.compile(r"^\s*(?:mm|cm|m|mm/s|mm/min|m/min|m/s|mm/s²|m/s²|mm²|cm²|m²|bar|MPa|kPa|l/min|m³/h|kg)\s*$")


def offenders(root: QWidget, visible_only: bool = True) -> list[str]:
    out = []
    for where, t in texts(root, visible_only):
        if ".item[" in where and UNIT_CHOICE_RE.match(t):
            continue
        h = metric_hits(t)
        if h:
            out.append(f"{where}: {t!r} -> {h}")
    return out
