"""The Settings window (``app.settings`` / ``app.options``; docs/user/settings.md).

Driven by the ConfigService schema and the catalogue of :mod:`mlaser.services.settings_catalog`:
pages in a sidebar (General, Units, Display, Import/Export, Design/CAM defaults, Nest, Run &
Queue, Machine connection, Notifications, Plugins, Advanced), a search box over every page,
the help text of each setting, a "Modified" marker (the value differs from the default), reset
of one setting or of the page, validation by the schema, "Applies after restart" where true,
and user-mode gating: in Operator mode only the operator's settings (language, units, display)
can be changed, the rest is shown read-only.  Safety values are never here: the Safety I/O
dialog is linked from the Machine page.

Platform conventions (:func:`platform_variant`): the window is called *Settings* on macOS (the
application menu's PreferencesRole, Cmd+,), *Options* on Windows (Tools), *Preferences* on Linux
(Edit, Ctrl+,); the native menu bar (``shell/menubar.py``) gives ``app.settings`` those names and
the PreferencesRole, :func:`menu_label` returns the same words.  On macOS a change applies at
once (no OK button, Close only); on Windows and Linux the changes are collected and applied with
OK or Apply, Cancel discards them; QDialogButtonBox orders the buttons natively.

Responsive: the layout follows the space it actually gets, from content-driven thresholds and
not from screen sizes.  When the sidebar and the page's widest row no longer fit side by side
the sidebar becomes a drop-down above the page; when a row's label column and its control no
longer fit, the label goes above the control.  Pages never scroll horizontally; a long page
scrolls vertically.  Standard widgets keep the palette (no style sheets); the 8 px spacing grid
is in :data:`GRID`.

Unit-bearing values use the Units track's widgets (:func:`mlaser.ui.units_qt.spin_for_setting`:
the stored value stays in mm, mm/s, mm², per m³...; the display follows ``units.*``).
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mlaser.services import settings_catalog as C
from mlaser.services import usermodes as um
from mlaser.services.config import Setting

from mlaser.services.units import kind_for_unit
from mlaser.ui.units_qt import spin_for_setting


#: the spacing grid (px): margins and gaps are multiples of it
GRID = 8
_RESET = object()          # staged: "back to the default"


# --------------------------------------------------------------------------- platform
def platform_variant(platform: Optional[str] = None) -> str:
    """``"mac"``, ``"windows"`` or ``"linux"`` (every other Unix)."""
    p = platform or sys.platform
    return "mac" if p == "darwin" else "windows" if p.startswith("win") else "linux"


#: where the menu bar puts the command, per platform (the Native track's menu bar)
MENU_PLACEMENT = {"mac": "application", "windows": "tools", "linux": "edit"}


def menu_label(tr: Any, platform: Optional[str] = None) -> str:
    """The menu text: "Settings…" (macOS), "Options…" (Windows), "Preferences…" (Linux)."""
    return tr.tr(f"settings.menu.{platform_variant(platform)}")


def form_label(text: str, platform: Optional[str] = None) -> str:
    """A form label: no trailing colon on macOS, a colon on Windows and Linux."""
    text = text.rstrip(": ")
    return text if platform_variant(platform) == "mac" else text + ":"


def window_title(tr: Any, platform: Optional[str] = None) -> str:
    return tr.tr(f"settings.title.{platform_variant(platform)}")


def instant_apply(platform: Optional[str] = None) -> bool:
    """macOS applies a change at once; Windows and Linux use OK / Cancel / Apply."""
    return platform_variant(platform) == "mac"


# --------------------------------------------------------------------------- helpers
def _t(tr: Any, key: str, default: str = "") -> str:
    return tr.tr(key) if tr.has(key) else (default or key)


def _muted(label: QLabel, smaller: bool = True) -> QLabel:
    """Secondary text: the palette's placeholder colour (no style sheet), a smaller font."""
    label.setForegroundRole(QPalette.PlaceholderText)
    if smaller:
        f = QFont(label.font())
        f.setPointSizeF(max(f.pointSizeF() * 0.85, 7.0))   # caption (DESIGN.md)
        label.setFont(f)
    return label


def _heading(text: str, scale: float = 1.0) -> QLabel:
    lab = QLabel(text)
    f = QFont(lab.font())
    f.setBold(True)
    if scale != 1.0:
        f.setPointSizeF(f.pointSizeF() * scale)
    lab.setFont(f)
    lab.setWordWrap(True)
    return lab


class SettingRow(QWidget):
    """One setting: label, help, markers, the editor and its reset button.  ``set_stacked``
    puts the label above the editor (narrow)."""

    committed = Signal(str, object)
    reset_requested = Signal(str)

    def __init__(self, dlg: "SettingsDialog", setting: Setting, entry: Optional[C.Entry], label: str,
                 help_text: str) -> None:
        super().__init__()
        self.dlg, self.setting, self.entry, self.key = dlg, setting, entry, setting.key
        self.setObjectName(f"setting_{setting.key}")
        self.memory = setting.kind == "json"          # cam.tool.*: a tool dialog's last-used values
        self.is_bool = setting.kind == "bool" and not (entry is not None and entry.readonly)
        # a check box carries its own text (DESIGN.md §3); other labels follow the platform
        # (no colon on macOS, a colon on Windows / Linux; right-aligned on macOS)
        self.label = QLabel("" if self.is_bool else form_label(label, dlg.platform))
        self.label.setWordWrap(True)
        self.label.setObjectName("setting_label")
        self.label.setAlignment((Qt.AlignRight if platform_variant(dlg.platform) == "mac" else Qt.AlignLeft)
                                | Qt.AlignTop)
        self.help = _muted(QLabel(help_text))
        self.help.setWordWrap(True)
        self.help.setVisible(bool(help_text))
        self.marker = _muted(QLabel(_t(dlg.tr, "settings.modified", "Modified")))
        self.marker.setAlignment(self.label.alignment())
        self.restart = _muted(QLabel(_t(dlg.tr, "settings.restart", "Applies after restart")))
        self.restart.setVisible(bool(entry and entry.restart))
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setForegroundRole(QPalette.BrightText)
        self.error.setVisible(False)
        self.label_text = label
        self.editor = self._make_editor()
        self.editor.setObjectName(f"editor_{setting.key}")
        # one home per value (mlaser.ui.homes): this window edits its keys; a read-only summary
        # is a mirror of a value edited elsewhere
        from . import homes
        if self.memory or (entry is not None and entry.readonly):
            homes.bind_mirror(self.editor, f"cfg:{setting.key}", dlg.tr)
        else:
            homes.bind_editor(self.editor, f"cfg:{setting.key}")
        self.editor.setAccessibleName(label)
        self.editor.setAccessibleDescription(help_text)
        self.label.setBuddy(self.editor)
        self.reset_btn = QToolButton()
        self.reset_btn.setText(_t(dlg.tr, "settings.reset", "Reset"))
        self.reset_btn.setToolTip(_t(dlg.tr, "settings.reset_tip", "Back to the default value"))
        self.reset_btn.setAccessibleName(_t(dlg.tr, "settings.reset", "Reset") + " " + label)
        self.reset_btn.setAutoRaise(True)
        self.reset_btn.setObjectName(f"reset_{setting.key}")
        self.reset_btn.clicked.connect(lambda: self.reset_requested.emit(self.key))
        self.texts = " ".join((label, help_text, setting.key)).lower()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, GRID // 2, 0, GRID // 2)
        self._grid.setHorizontalSpacing(2 * GRID)
        self._grid.setVerticalSpacing(GRID // 2)
        # the label box (label, "Modified") and the hint box under the field (help, restart, error)
        self._info = QWidget()
        iv = QVBoxLayout(self._info)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(GRID // 2)
        iv.addWidget(self.label)
        iv.addWidget(self.marker)
        self.label.setVisible(not self.is_bool)
        self._hint = QWidget()
        hv = QVBoxLayout(self._hint)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(GRID // 2)
        hv.addWidget(self.help)
        hv.addWidget(self.restart)
        hv.addWidget(self.error)
        self.stacked: Optional[bool] = None
        self.set_stacked(False)

    # ---------------------------------------------------------------- layout
    def label_min_width(self) -> int:
        """The narrowest the label column may be before the label goes above the control: a
        content-driven measure (the label's longest word, at least about 16 characters)."""
        fm = self.fontMetrics()
        word = max((fm.horizontalAdvance(w) for w in self.label_text.split()), default=0)
        return max(word, fm.averageCharWidth() * 16)

    def label_pref_width(self) -> int:
        """The width the label would like on one line (the page caps it)."""
        return self.fontMetrics().horizontalAdvance(self.label.text() or "") + GRID

    def set_label_column(self, width: int) -> None:
        """Every row of a page shares one label column, so the fields line up."""
        if self.stacked:
            self._grid.setColumnMinimumWidth(0, 0)
            self._info.setMinimumWidth(0)
            self._info.setMaximumWidth(16777215)
        else:
            self._grid.setColumnMinimumWidth(0, width)
            self._info.setFixedWidth(width)

    def side_by_side_width(self) -> int:
        return self.label_min_width() + self.editor.minimumSizeHint().width() + \
            self.reset_btn.sizeHint().width() + 4 * GRID

    def set_stacked(self, on: bool) -> None:
        if on == self.stacked:
            return
        self.stacked = on
        g = self._grid
        for w in (self._info, self.editor, self._hint, self.reset_btn):
            g.removeWidget(w)
        if on:            # label above the field
            g.addWidget(self._info, 0, 0, 1, 2)
            g.addWidget(self.editor, 1, 0, Qt.AlignLeft)
            g.addWidget(self.reset_btn, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)
            g.addWidget(self._hint, 2, 0, 1, 2)
            g.setColumnStretch(0, 1)
            g.setColumnStretch(1, 0)
            g.setColumnStretch(2, 0)
        else:             # label | field (hint under it) | reset | (spare room)
            g.addWidget(self._info, 0, 0, 2, 1, Qt.AlignTop)
            g.addWidget(self.editor, 0, 1, Qt.AlignLeft)
            g.addWidget(self._hint, 1, 1)
            g.addWidget(self.reset_btn, 0, 2, Qt.AlignTop)
            g.setColumnStretch(0, 0)
            g.setColumnStretch(1, 1)
            g.setColumnStretch(2, 0)
        # wide: the label column is always there (the fields line up); stacked: a check box's
        # empty label box takes no room
        self._info.setVisible(not (on and self.is_bool and self.marker.isHidden()))

    # ---------------------------------------------------------------- editor
    def _choices(self) -> list[tuple[Any, str]]:
        e, s, tr = self.entry, self.setting, self.dlg.tr
        if e is not None and e.dynamic == "languages":
            return [("", _t(tr, "settings.opt.system_language", "System language"))] + \
                [(li.code, li.name) for li in tr.available()]
        if e is not None and e.dynamic == "profiles":
            from .ext_options import profiles
            return [(n, n) for n in profiles()]
        if e is not None and e.choices:
            return [(v, _t(tr, k, str(v))) for v, k in e.choices]
        if s.kind == "str" and s.options:     # units.* (values; "" = follow the system)
            def text(o: str) -> str:
                if not o:
                    return _t(tr, "units.follow_system", "Follow the unit system")
                return _t(tr, f"units.system.{o}", str(o)) if s.key == "units.system" else str(o)
            return [(o, text(o)) for o in s.options]
        return []

    def _make_editor(self) -> QWidget:
        s = self.setting
        choices = self._choices()
        w: QWidget
        if self.memory or (self.entry is not None and self.entry.readonly):
            # one home per setting: shown here, edited at its home ("Edit in …" under the group);
            # a tool's last-used values: shown, with Reset
            w = QLabel()
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            w.setWordWrap(True)
            self._choice_text = dict(choices)
        elif s.kind == "bool":
            w = QCheckBox(self.label_text)
            w.toggled.connect(lambda on: self.committed.emit(self.key, bool(on)))
        elif choices:
            w = QComboBox()
            for v, text in choices:
                w.addItem(text, v)
            w.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            w.activated.connect(lambda i: self.committed.emit(self.key, w.itemData(i)))
        elif s.kind == "enum" and s.options:
            w = QComboBox()
            for i, o in enumerate(s.options):
                w.addItem(_t(self.dlg.tr, o, str(o)), i)
            w.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            w.activated.connect(lambda i: self.committed.emit(self.key, i))
        elif s.kind in ("int", "float"):
            lo = s.min if s.min is not None else -1e9
            hi = s.max if s.max is not None else 1e9
            if s.kind == "float":
                # the Units track's widgets: a value stored in mm, mm/s, mm², per m³ ... is shown
                # and typed in the display units (units.*); other units keep their suffix
                w = spin_for_setting(s)
            else:
                w = QSpinBox()
                w.setRange(int(max(lo, -2**31)), int(min(hi, 2**31 - 1)))
                if s.unit:
                    w.setSuffix(f" {s.unit}")
            w.setKeyboardTracking(False)
            if isinstance(w, QAbstractSpinBox):
                w.editingFinished.connect(lambda: self.committed.emit(self.key, w.value()))
        else:
            w = QLineEdit()
            if self.entry is not None and self.entry.secret:
                w.setEchoMode(QLineEdit.PasswordEchoOnEdit)
            w.editingFinished.connect(lambda: self.committed.emit(self.key, w.text()))
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # fields stay a readable width on a wide window (about 36 characters; content-driven)
        w.setMaximumWidth(max(w.sizeHint().width(), self.fontMetrics().averageCharWidth() * 36))
        return w

    def display_text(self, v: Any) -> str:
        """The value as read-only text (unit-bearing values in the user's units)."""
        s, tr = self.setting, self.dlg.tr
        if self.memory:
            from .tool_memory import summary
            return summary(v) or _t(tr, "settings.tool.default", "Default values")
        if s.kind == "bool":
            return _t(tr, "settings.on" if v else "settings.off", "On" if v else "Off")
        if getattr(self, "_choice_text", None) and v in self._choice_text:
            return self._choice_text[v]
        if s.kind == "enum" and s.options:
            try:
                o = s.options[int(v)]
                return _t(tr, o, str(o))
            except (TypeError, ValueError, IndexError):
                return str(v)
        if s.kind in ("int", "float"):
            kind = kind_for_unit(s.unit) if s.kind == "float" else None
            if kind is not None:
                try:
                    from mlaser.services import units as U
                    return U.fmt(float(v), kind)
                except (ImportError, AttributeError):
                    pass
            return f"{v:g} {s.unit}".strip() if isinstance(v, (int, float)) else str(v)
        return str(v) if v not in (None, "") else "–"

    def show_value(self, v: Any, modified: bool) -> None:
        w = self.editor
        w.blockSignals(True)
        try:
            if self.memory or (self.entry is not None and self.entry.readonly):
                w.setText(self.display_text(v))   # type: ignore[attr-defined]
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QComboBox):
                i = w.findData(v)
                if i < 0 and isinstance(v, (int, float)) and not isinstance(v, bool):
                    i = int(v) if 0 <= int(v) < w.count() else -1
                w.setCurrentIndex(max(i, 0))
            elif isinstance(w, QLineEdit):
                if w.text() != str(v if v is not None else ""):
                    w.setText("" if v is None else str(v))
            else:
                try:
                    w.setValue(float(v) if self.setting.kind == "float" else int(float(v)))  # type: ignore[attr-defined]
                except (TypeError, ValueError):
                    pass
        finally:
            w.blockSignals(False)
        self.marker.setVisible(modified)
        self.reset_btn.setVisible(modified and (self.memory or not (self.entry is not None and self.entry.readonly)))
        f = QFont(self.label.font())
        f.setBold(modified)
        self.label.setFont(f)
        if self.is_bool and self.stacked:   # the label box holds only the marker
            self._info.setVisible(modified)

    def value(self) -> Any:
        w = self.editor
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentData()
        if isinstance(w, QLineEdit):
            return w.text()
        return w.value()  # type: ignore[attr-defined]

    def set_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def set_editable(self, on: bool, why: str = "") -> None:
        if self.memory or (self.entry is not None and self.entry.readonly):
            self.reset_btn.setEnabled(on)
            return                        # a read-only summary stays readable (not greyed)
        self.editor.setEnabled(on)
        self.reset_btn.setEnabled(on)
        self.editor.setToolTip("" if on else why)


class SettingsPage(QWidget):
    """A page: title, optional note, groups of rows, links.  Re-flows on resize."""

    def __init__(self, page_id: str, title: str) -> None:
        super().__init__()
        self.page_id = page_id
        self.setObjectName(f"settings_page_{page_id}")
        self.rows: list[SettingRow] = []
        self.groups: list[tuple[QLabel, list[SettingRow]]] = []
        self.extras: list[QWidget] = []          # notes, summaries, links (searchable by text)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(3 * GRID, 2 * GRID, 3 * GRID, 2 * GRID)
        self.lay.setSpacing(GRID)
        self.title = _heading(title, 1.25)          # title (DESIGN.md)
        self.lay.addWidget(self.title)
        self.empty = _muted(QLabel(), smaller=False)
        self.empty.setWordWrap(True)
        self.empty.setVisible(False)
        self.lay.addWidget(self.empty)

    def add_note(self, text: str) -> QLabel:
        lab = _muted(QLabel(text), smaller=False)
        lab.setWordWrap(True)
        self.lay.addWidget(lab)
        self.extras.append(lab)
        return lab

    def add_group(self, title: str, rows: list[SettingRow]) -> None:
        h = _heading(title)
        self.lay.addSpacing(GRID)
        self.lay.addWidget(h)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        self.lay.addWidget(line)
        for r in rows:
            self.lay.addWidget(r)
        self.rows += rows
        self.groups.append((h, rows))
        h.setProperty("rule", line)

    def add_links(self, buttons: list[QPushButton]) -> None:
        if not buttons:
            return
        self.lay.addSpacing(GRID)
        box = QWidget()
        flow = QVBoxLayout(box)              # one per line: never wider than the page
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setSpacing(GRID)
        for b in buttons:
            b.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            flow.addWidget(b, 0, Qt.AlignLeft)
        self.lay.addWidget(box)
        self.extras.append(box)

    def finish(self) -> None:
        self.lay.addStretch(1)

    def narrow_width(self) -> int:
        m = self.lay.contentsMargins()
        fm = self.fontMetrics()
        rows = max((r.editor.minimumSizeHint().width() + r.reset_btn.sizeHint().width() + 2 * GRID
                    for r in self.rows), default=0)
        return max(rows, fm.averageCharWidth() * 24) + m.left() + m.right() + \
            self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent)

    def min_side_by_side(self) -> int:
        m = self.lay.contentsMargins()
        return max((r.side_by_side_width() for r in self.rows), default=0) + m.left() + m.right()

    def reflow(self, width: int) -> None:
        m = self.lay.contentsMargins()
        avail = width - m.left() - m.right()
        if not self.rows:
            return
        fm = self.fontMetrics()
        # one label column for the page: the longest label, at most ~40 % of the width, at
        # least the narrowest a label may wrap to (see SettingRow.label_min_width)
        col = min(max(r.label_pref_width() for r in self.rows), max(int(avail * 0.4), fm.averageCharWidth() * 16))
        col = max(col, max(r.label_min_width() for r in self.rows))
        for r in self.rows:
            need = col + r.editor.minimumSizeHint().width() + r.reset_btn.sizeHint().width() + 4 * GRID
            r.set_stacked(avail < need)
            r.set_label_column(col)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt
        super().resizeEvent(ev)
        self.reflow(ev.size().width())

    def filter(self, text: str) -> int:
        """Show the rows matching ``text`` (all when empty); returns the number shown."""
        t = text.strip().lower()
        n = 0
        for head, rows in self.groups:
            any_vis = False
            for r in rows:
                vis = not t or t in r.texts
                r.setVisible(vis)
                any_vis |= vis
                n += vis
            head.setVisible(any_vis)
            rule = head.property("rule")
            if rule is not None:
                rule.setVisible(any_vis)
        for x in self.extras:
            vis = not t or t in (x.text().lower() if isinstance(x, QLabel) else
                                 " ".join(b.text().lower() for b in x.findChildren(QPushButton)))
            x.setVisible(vis)
            n += vis and t != ""
        return n


class _Scroll(QScrollArea):
    """A page's scroll area: vertical only; the page always gets the viewport's width."""

    def __init__(self, page: SettingsPage) -> None:
        super().__init__()
        self.setWidget(page)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.page = page

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt
        """The narrowest the page can reflow to (every row stacked), not its wide layout."""
        return QSize(self.page.narrow_width(), 0)

    # The page reflows for the width the viewport gives it, before the scroll area sizes it:
    # reflowing from the page's own resize could not stack its rows once it was wider than the
    # viewport (a page laid out wide cannot shrink below the minimum of that wide layout, so it
    # stayed wide and scrolled sideways after the window shrank or another page was shown).
    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt
        self.page.reflow(self.viewport().width())
        super().resizeEvent(ev)
        if self.page.width() > self.viewport().width():
            self.page.reflow(self.viewport().width())
            super().resizeEvent(ev)

    def showEvent(self, ev) -> None:  # noqa: N802 - Qt
        self.page.reflow(self.viewport().width())
        super().showEvent(ev)


class SettingsDialog(QDialog):
    """See the module docstring."""

    def __init__(self, window: Any, instant: Optional[bool] = None, platform: Optional[str] = None) -> None:
        super().__init__(window)
        self.w = window
        self.tr = window.tr_
        self.cfg = window.config
        self.platform = platform
        self.instant = instant_apply(platform) if instant is None else bool(instant)
        self.pending: dict[str, Any] = {}
        self.rows: dict[str, SettingRow] = {}
        self.pages: dict[str, SettingsPage] = {}
        self.setObjectName("settings_dialog")
        self.setWindowTitle(window_title(self.tr, platform))
        self.setSizeGripEnabled(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(2 * GRID, 2 * GRID, 2 * GRID, 2 * GRID)
        root.setSpacing(GRID)
        top = QHBoxLayout()
        top.setSpacing(GRID)
        self.search = QLineEdit()
        self.search.setObjectName("settings_search")
        self.search.setPlaceholderText(_t(self.tr, "settings.search", "Search settings"))
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(_t(self.tr, "settings.search", "Search settings"))
        self.search.textChanged.connect(self._filter)
        self.page_combo = QComboBox()                 # the sidebar when narrow
        self.page_combo.setObjectName("settings_page_combo")
        self.page_combo.setAccessibleName(_t(self.tr, "settings.pages", "Settings pages"))
        self.page_combo.setVisible(False)
        top.addWidget(self.search, 1)
        root.addLayout(top)
        root.addWidget(self.page_combo)
        # search results whose home is elsewhere (Machine › Setup, Process, Nest...): navigation
        self.elsewhere = QWidget()
        self.elsewhere.setObjectName("settings_elsewhere")
        ev = QVBoxLayout(self.elsewhere)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(GRID // 2)
        self.elsewhere_title = _muted(QLabel(_t(self.tr, "settings.elsewhere", "Elsewhere")), smaller=False)
        ev.addWidget(self.elsewhere_title)
        self.elsewhere_box = QVBoxLayout()
        self.elsewhere_box.setSpacing(GRID // 2)
        ev.addLayout(self.elsewhere_box)
        self.elsewhere.setVisible(False)
        root.addWidget(self.elsewhere)
        body = QHBoxLayout()
        body.setSpacing(2 * GRID)
        self.sidebar = QListWidget()
        self.sidebar.setObjectName("settings_sidebar")
        self.sidebar.setAccessibleName(_t(self.tr, "settings.pages", "Settings pages"))
        self.sidebar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setSpacing(GRID // 2)
        self.stack = QStackedWidget()
        body.addWidget(self.sidebar)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        bottom = QHBoxLayout()
        bottom.setSpacing(GRID)
        self.reset_page_btn = QPushButton(_t(self.tr, "settings.reset_page", "Reset page"))
        self.reset_page_btn.setObjectName("settings_reset_page")
        self.reset_page_btn.clicked.connect(self.reset_page)
        self.reset_page_btn.setAutoDefault(False)
        bottom.addWidget(self.reset_page_btn)
        bottom.addStretch(1)
        if self.instant:
            self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
            self.buttons.rejected.connect(self.close)
        else:
            self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
            self.buttons.accepted.connect(self.accept)
            self.buttons.rejected.connect(self.reject)
            self.buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        bottom.addWidget(self.buttons)
        root.addLayout(bottom)
        self._build()
        self.sidebar.currentRowChanged.connect(self._show_page)
        self.page_combo.currentIndexChanged.connect(self._combo_page)
        self.sidebar.setCurrentRow(0)
        self._unsub = self.cfg.subscribe(self._config_changed)
        hooks = getattr(window, "hooks", None)
        if hooks is not None:
            hooks.add("user_mode_changed", lambda _m: self.refresh(), "settings_dialog")
        scr = self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent)
        self._page_need = max(p.min_side_by_side() for p in self.pages.values()) + scr
        # the layout does not impose its (sidebar-wide) minimum: the window can shrink to the
        # narrow layout, and resizeEvent switches to it
        self.layout().setSizeConstraint(QVBoxLayout.SetNoConstraint)
        self.setMinimumSize(self.minimumSizeHint())
        self.refresh()
        self.resize(self._initial_size())
        self._relayout(self.width())

    # ---------------------------------------------------------------- build
    def _label(self, s: Setting, e: Optional[C.Entry]) -> str:
        for key in ((e.label_key,) if e is not None else ()) + (("settings.key." + s.key, s.label) if s.label else
                                                                 ("settings.key." + s.key,)):
            if key and self.tr.has(key):
                return self.tr.tr(key)
        return s.label or s.key          # a plugin's own text label

    def _help(self, s: Setting, e: Optional[C.Entry]) -> str:
        key = e.help_key if e is not None else "settings.help." + s.key
        return self.tr.tr(key) if self.tr.has(key) else ""

    def _row(self, s: Setting, e: Optional[C.Entry]) -> SettingRow:
        r = SettingRow(self, s, e, self._label(s, e), self._help(s, e))
        r.committed.connect(self._commit)
        r.reset_requested.connect(self.reset_setting)
        self.rows[s.key] = r
        return r

    def _links(self, page: str) -> list[QPushButton]:
        out = []
        for p, cmd, key in C.LINKS:
            if p != page or (self.w.commands.get(cmd) is None and cmd not in self.w.ribbon.actions):
                continue
            b = QPushButton(_t(self.tr, key, cmd))
            b.setObjectName(f"settings_link_{cmd}")
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, c=cmd: self._run(c))
            b.setProperty("command", cmd)
            out.append(b)
        return out

    def _home_button(self, cmd: str, label_key: str) -> Optional[QPushButton]:
        if self.w.commands.get(cmd) is None and cmd not in self.w.ribbon.actions:
            return None
        b = QPushButton(_t(self.tr, label_key, cmd))
        b.setObjectName(f"settings_home_{cmd}")
        b.setAutoDefault(False)
        b.setProperty("command", cmd)
        b.clicked.connect(lambda _=False, c=cmd: self._run(c))
        return b

    def _run(self, cmd: str) -> None:
        if cmd.startswith("workspace."):
            self.w.show_workspace(cmd.split(".", 1)[1])
        else:
            self.w.run(cmd)

    def _build(self) -> None:
        schema = self.cfg.schema
        for pid, title_key in C.PAGES:
            page = SettingsPage(pid, _t(self.tr, title_key, pid))
            groups: dict[str, list[SettingRow]] = {}
            for e in C.entries_for(pid):
                s = schema.get(e.key)
                if s is not None:
                    groups.setdefault(e.group, []).append(self._row(s, e))
            if pid in C.PAGE_NOTES:
                page.add_note(_t(self.tr, C.PAGE_NOTES[pid]))
            if pid == "general":         # the Units service's keys, right after the language
                us = [s for k, s in schema.items() if k.startswith("units.")]
                if us:
                    items = list(groups.items())
                    groups = dict(items[:1] + [("settings.group.units", [self._row(s, None) for s in us])]
                                  + items[1:])
                else:
                    page.add_note(_t(self.tr, "settings.units.pending"))
            if pid == "users":
                self._users_summary(page)
            if pid == "machine":
                self._machine_summary(page)
            if pid == "plugins":
                page.add_note(_t(self.tr, "settings.plugins.note"))
                for name, settings in sorted(C.plugin_settings(schema).items()):
                    groups[name] = [self._row(s, None) for s in settings]
            all_rows = [r for rows in groups.values() for r in rows]
            page_homes = {(r.entry.home, r.entry.home_label) for r in all_rows
                          if r.entry is not None and r.entry.readonly}
            whole = bool(all_rows) and len(page_homes) == 1 and all(
                r.entry is not None and r.entry.readonly for r in all_rows)
            if whole:        # the whole page lives elsewhere: one "Edit in …" under the title
                page.add_links([b for b in (self._home_button(c, k) for c, k in page_homes) if b is not None])
            done: set = set()
            for g, rows in groups.items():
                page.add_group(_t(self.tr, g, g), rows)
                if whole:
                    continue
                homes = [h for h in dict.fromkeys((r.entry.home, r.entry.home_label) for r in rows
                                                  if r.entry is not None and r.entry.readonly) if h not in done]
                done.update(homes)
                page.add_links([b for b in (self._home_button(c, k) for c, k in homes) if b is not None])
            page.add_links(self._links(pid))
            page.finish()
            self.pages[pid] = page
            self.stack.addWidget(_Scroll(page))
            item = QListWidgetItem(page.title.text())
            item.setData(Qt.UserRole, pid)
            self.sidebar.addItem(item)
            self.page_combo.addItem(page.title.text(), pid)
        fm = self.sidebar.fontMetrics()
        self.sidebar.setFixedWidth(max(fm.horizontalAdvance(self.sidebar.item(i).text())
                                       for i in range(self.sidebar.count())) + 4 * GRID)

    def _users_summary(self, page: SettingsPage) -> None:
        """Read-only: the current user mode and which PINs are set (the PINs' home is Set PINs)."""
        page.add_note(_t(self.tr, "settings.users.note"))
        self.users = QLabel()
        self.users.setObjectName("settings_users_summary")
        self.users.setWordWrap(True)
        page.lay.addWidget(self.users)
        page.extras.append(self.users)

    def _users_text(self) -> str:
        tr, w = self.tr, self.w
        mode = tr.tr(f"shell.mode.{w.user_mode}") if tr.has(f"shell.mode.{w.user_mode}") else w.user_mode
        pins = [tr.tr(f"settings.users.pin_{m}") for m in ("engineer", "service") if self.cfg.get(f"shell.pin.{m}", "")]
        return "\n".join([f"{tr.tr('settings.machine.mode')}: {mode}",
                          f"{tr.tr('settings.users.pins')}: " + (", ".join(pins) or tr.tr("settings.users.no_pins"))])

    def _machine_summary(self, page: SettingsPage) -> None:
        """Read-only: the connection, the card address, the machine profile, the user mode."""
        page.add_note(_t(self.tr, "settings.machine.note"))
        self.summary = QLabel()
        self.summary.setObjectName("settings_machine_summary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        page.lay.addWidget(self.summary)
        page.extras.append(self.summary)

    def _summary_text(self) -> str:
        tr, w = self.tr, self.w
        try:
            st = w.ctl.state()
            conn = tr.tr("settings.machine.connected") if getattr(st, "connected", False) else \
                tr.tr("settings.machine.disconnected")
        except Exception:  # noqa: BLE001 - a controller without state
            conn = tr.tr("settings.machine.disconnected")
        ip = (self.cfg.get("ip", {}) or {}).get("pd445", "10.1.1.168") if isinstance(self.cfg.get("ip", {}), dict) \
            else "10.1.1.168"
        lines = [(tr.tr("settings.machine.state"), conn),
                 (tr.tr("settings.machine.controller"),
                  tr.tr("settings.machine.ctl_demo") if type(w.ctl).__name__ == "DemoController"
                  else tr.tr("settings.machine.ctl_card")),
                 (tr.tr("settings.machine.address"), str(ip)),
                 (tr.tr("settings.machine.profile"), self.cfg.profile.base),
                 (tr.tr("settings.machine.mode"), tr.tr(f"shell.mode.{w.user_mode}")
                  if tr.has(f"shell.mode.{w.user_mode}") else w.user_mode)]
        return "\n".join(f"{a}: {b}" for a, b in lines)

    def _initial_size(self) -> QSize:
        """What the content wants, within the screen it opens on."""
        hint = QSize(self.sidebar.width() + max(p.min_side_by_side() for p in self.pages.values()) + 8 * GRID,
                     self.fontMetrics().height() * 40)
        scr = (self.parentWidget().screen() if self.parentWidget() is not None else self.screen())
        if scr is not None:
            g = scr.availableGeometry()
            hint = hint.boundedTo(QSize(int(g.width() * 0.9), int(g.height() * 0.9)))
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt
        """The narrowest useful window: one stacked row (label above its control)."""
        m = self.layout().contentsMargins()
        widest = max((p.narrow_width() for p in self.pages.values()), default=200)
        widest = max(widest, self.buttons.sizeHint().width() + self.reset_page_btn.sizeHint().width() + GRID)
        return QSize(widest + m.left() + m.right(), self.fontMetrics().height() * 18)

    # ---------------------------------------------------------------- responsive
    def _side_by_side_fits(self, width: int) -> bool:
        """The sidebar stays while every page beside it can still keep its rows side by side
        (label | field); below that the sidebar becomes the drop-down above the page."""
        m = self.layout().contentsMargins()
        avail = width - m.left() - m.right() - self.sidebar.width() - 2 * GRID
        return avail >= getattr(self, "_page_need", 0)

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt
        super().resizeEvent(ev)
        self._relayout(ev.size().width())

    def _relayout(self, width: int) -> None:
        wide = self._side_by_side_fits(width)
        self.sidebar.setVisible(wide)
        self.page_combo.setVisible(not wide)

    @property
    def narrow(self) -> bool:
        return self.page_combo.isVisible()

    # ---------------------------------------------------------------- pages
    def current_page(self) -> Optional[SettingsPage]:
        sc = self.stack.currentWidget()
        return sc.page if isinstance(sc, _Scroll) else None

    def _show_page(self, i: int) -> None:
        if i < 0:
            return
        self.stack.setCurrentIndex(i)
        if self.page_combo.currentIndex() != i:
            self.page_combo.blockSignals(True)
            self.page_combo.setCurrentIndex(i)
            self.page_combo.blockSignals(False)

    def _combo_page(self, i: int) -> None:
        if self.sidebar.currentRow() != i:
            self.sidebar.setCurrentRow(i)

    def show_page(self, page_id: str) -> bool:
        if page_id not in C.PAGE_IDS:
            return False
        self.sidebar.setCurrentRow(C.PAGE_IDS.index(page_id))
        return True

    # ---------------------------------------------------------------- values
    def _value(self, key: str) -> Any:
        if key in self.pending:
            v = self.pending[key]
            return self.cfg.schema[key].default if v is _RESET else v
        return self.cfg.get(key)

    def _modified(self, key: str) -> bool:
        s = self.cfg.schema.get(key)
        return s is not None and self._value(key) != s.default

    def _editable(self, row: SettingRow) -> bool:
        if row.entry is not None and row.entry.readonly and not row.memory:
            return False
        need = row.entry.min_mode if row.entry is not None else um.ENGINEER
        return um.rank(self.w.user_mode) >= um.rank(need)

    def refresh(self) -> None:
        """Every editor from the stored (or staged) values; gating from the user mode."""
        why = _t(self.tr, "settings.locked", "Needs Engineer mode")
        for key, r in self.rows.items():
            if key in self.cfg.schema:
                r.show_value(self._value(key), self._modified(key))
            r.set_editable(self._editable(r), why)
        locked = um.rank(self.w.user_mode) < um.rank(um.ENGINEER)
        if hasattr(self, "summary"):
            self.summary.setText(self._summary_text())
        if hasattr(self, "users"):
            self.users.setText(self._users_text())
        page = self.current_page()
        self.reset_page_btn.setEnabled(page is not None and any(self._editable(r) for r in page.rows))
        self.setProperty("operatorLocked", locked)

    def _commit(self, key: str, value: Any) -> None:
        r = self.rows[key]
        if not self._editable(r):
            r.show_value(self._value(key), self._modified(key))
            return
        try:
            v = self.cfg.schema[key].validate(value)
        except ValueError as exc:
            r.set_error(str(exc))
            return
        r.set_error("")
        if v == self._value(key):
            return
        if self.instant:
            self.cfg.set(key, v)
            self.cfg.flush()
        else:
            self.pending[key] = v
            self._staged_changed()
        r.show_value(self._value(key), self._modified(key))

    def reset_setting(self, key: str) -> None:
        r = self.rows.get(key)
        if r is None or not self._editable(r):
            return
        if self.instant:
            self.cfg.reset(key)
            self.cfg.flush()
        else:
            self.pending[key] = _RESET
            self._staged_changed()
        r.set_error("")
        r.show_value(self._value(key), self._modified(key))

    def reset_page(self) -> None:
        page = self.current_page()
        if page is None:
            return
        for r in page.rows:
            if self._modified(r.key):
                self.reset_setting(r.key)

    def _staged_changed(self) -> None:
        b = self.buttons.button(QDialogButtonBox.Apply)
        if b is not None:
            b.setEnabled(bool(self.pending))

    def apply(self) -> int:
        """Windows / Linux: store the staged changes (each goes through the schema)."""
        n = 0
        for key, v in list(self.pending.items()):
            try:
                if v is _RESET:
                    self.cfg.reset(key)
                else:
                    self.cfg.set(key, v)
                n += 1
            except ValueError as exc:
                self.rows[key].set_error(str(exc))
        self.pending.clear()
        self.cfg.flush()
        self._staged_changed()
        self.refresh()
        return n

    def accept(self) -> None:
        self.apply()
        super().accept()

    def reject(self) -> None:
        self.pending.clear()
        self._staged_changed()
        super().reject()
        self.refresh()

    def _config_changed(self, key: str) -> None:
        r = self.rows.get(key)
        if r is not None and key not in self.pending:
            r.show_value(self._value(key), self._modified(key))
        elif key in ("*", "profile"):
            self.refresh()

    # ---------------------------------------------------------------- search
    HOME_LABELS = {"machine": "settings.where.machine", "process": "settings.where.process",
                   "nest": "settings.where.nest", "run": "settings.where.run"}
    MAX_ELSEWHERE = 6

    def _home_label(self, s: Setting) -> str:
        if s.source == "registry":
            g, name = self.tr.tr_param(s.label) if s.label else ("", s.key)
            return f"{name} ({g})" if g else name
        return self._label(s, None)

    def elsewhere_matches(self, text: str) -> list[tuple[Setting, str]]:
        """Settings whose home is not this window that match ``text``: (setting, label)."""
        t = text.strip().lower()
        if len(t) < 2:
            return []
        out = []
        for s in self.cfg.schema.values():
            if s.home not in self.HOME_LABELS or s.key in self.rows:
                continue
            if s.source == "registry" and not (s.live or s.home == "machine"):
                continue
            label = self._home_label(s)
            if t in label.lower() or t in s.key.lower():
                out.append((s, label))
        return out

    def _go_home(self, s: Setting) -> None:
        """Open the value's home (``homes.edit_in``: the workspace, its section, the field)."""
        from . import homes
        if s.home == "machine" and hasattr(self.w, "machine_setup"):
            self.w.machine_setup()                 # Machine › Setup (its section)
        if homes.home_of(f"cfg:{s.key}") is None or homes.edit_in(self.w, f"cfg:{s.key}") is None:
            if s.home != "machine":
                self.w.show_workspace(s.home)

    def _fill_elsewhere(self, text: str) -> None:
        while self.elsewhere_box.count():
            it = self.elsewhere_box.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        found = self.elsewhere_matches(text)
        for s, label in found[: self.MAX_ELSEWHERE]:
            where = _t(self.tr, self.HOME_LABELS[s.home], s.home)
            b = QPushButton(f"{label} — {where}")
            b.setObjectName("settings_elsewhere_item")
            b.setFlat(True)
            b.setAutoDefault(False)
            b.setProperty("setting", s.key)
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)     # never wider than the window
            b.setToolTip(f"{label} — {where}")
            b.clicked.connect(lambda _=False, st=s: self._go_home(st))
            self.elsewhere_box.addWidget(b)
        if len(found) > self.MAX_ELSEWHERE:
            more = _muted(QLabel(self.tr.format("settings.elsewhere_more", n=len(found) - self.MAX_ELSEWHERE)
                                 if self.tr.has("settings.elsewhere_more") else f"+{len(found) - self.MAX_ELSEWHERE}"))
            self.elsewhere_box.addWidget(more)
        self.elsewhere.setVisible(bool(found))

    def _filter(self, text: str) -> None:
        self._fill_elsewhere(text)
        first = None
        for i, pid in enumerate(C.PAGE_IDS):
            n = self.pages[pid].filter(text)
            hide = bool(text.strip()) and n == 0
            self.sidebar.item(i).setHidden(hide)
            if not hide and first is None:
                first = i
        page = self.current_page()
        cur = C.PAGE_IDS.index(page.page_id) if page is not None else 0
        if self.sidebar.item(cur).isHidden() and first is not None:
            self.sidebar.setCurrentRow(first)
        for p in self.pages.values():
            p.empty.setText(_t(self.tr, "settings.no_match", "No setting matches the search."))
            p.empty.setVisible(first is None)

    def focus_key(self, key: str) -> bool:
        """Show the row of ``key`` and focus its field (``homes.edit_in`` of a Settings value)."""
        r = self.rows.get(key)
        if r is None:
            return False
        page = r.parentWidget()
        while page is not None and not isinstance(page, SettingsPage):
            page = page.parentWidget()
        if page is not None:
            self.show_page(page.page_id)
            sc = self.stack.currentWidget()
            if isinstance(sc, _Scroll):
                sc.ensureWidgetVisible(r)
        r.editor.setFocus()
        return True

    def matches(self) -> list[str]:
        """The keys of the rows the search shows (tests)."""
        return [k for k, r in self.rows.items() if not r.isHidden()]

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt
        self.cfg.flush()
        super().closeEvent(ev)
