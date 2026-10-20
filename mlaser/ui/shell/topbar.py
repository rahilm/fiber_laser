"""The top bar: file name with the unsaved-changes marker, command search, the safety state
chips, the user mode and the language (docs/review/ux.md §4).  The application menu is the
native menu bar (menubar.py); the old in-window "Mlaser ⌄" button stays hidden for plugins
that looked it up.

The chips summarise Safety's state (:class:`mlaser.machine.safety.SafetyStatus`, the same
snapshot the state banner shows): ``SIMULATOR``, ``DRY-RUN``, ``ARMED`` / ``DISARMED``,
``FAULT`` and ``ALARM``.  Every chip carries its meaning as text and a shape symbol, never by
colour alone, and has an accessible name and a tooltip.  The full-width banners and the run bar
of :mod:`mlaser.ui.safety_ui` stay where they are; the chips only repeat the state.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QToolButton, QWidget)

from mlaser.services import usermodes as um

from ..icons import icon
from ..safety_ui import COLORS

#: chip kind -> (lang id, symbol, colour key of safety_ui.COLORS)
CHIPS = {
    "FAULT": ("shell.chip.fault", "✖", "FAULT"),
    "ALARM": ("shell.chip.alarm", "!", "FAULT"),
    "ARMED": ("shell.chip.armed", "▲", "ARMED"),
    "DISARMED": ("shell.chip.disarmed", "●", "DISARMED"),
    "DRY-RUN": ("shell.chip.dry_run", "◌", "DRY-RUN"),
    "SIMULATOR": ("shell.chip.simulator", "◆", "SIMULATOR"),
    "OFFLINE": ("shell.chip.offline", "–", "OFFLINE"),
}
ORDER = ("FAULT", "ALARM", "ARMED", "DISARMED", "DRY-RUN", "SIMULATOR", "OFFLINE")


def chip_kinds(st, alarms) -> list[str]:
    """The chips for a safety status snapshot and the active alarms."""
    out: list[str] = []
    if st is None:
        return ["OFFLINE"]
    state = getattr(getattr(st, "state", None), "value", "offline")
    if st.banner == "FAULT":
        out.append("FAULT")
    if alarms:
        out.append("ALARM")
    if st.armed:
        out.append("ARMED")
    elif state != "offline" and not st.dry_run:
        out.append("DISARMED")
    if st.dry_run:
        out.append("DRY-RUN")
    if st.simulator:
        out.append("SIMULATOR")
    if state == "offline" and not st.simulator:
        out.append("OFFLINE")
    return [k for k in ORDER if k in out]


class SearchField(QLineEdit):
    """A search field that opens the command palette: a click, or typing (the first letters
    go to the palette).  A real line edit, so it looks like the platform's search fields."""

    def __init__(self, open_palette):
        super().__init__()
        self._open = open_palette
        self.setReadOnly(True)
        self.setClearButtonEnabled(False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(180)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def mousePressEvent(self, ev) -> None:  # noqa: N802 - Qt API
        ev.accept()
        self._open("")

    def keyPressEvent(self, ev) -> None:  # noqa: N802 - Qt API
        text = ev.text()
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space, Qt.Key_Down):
            self._open("")
        elif text and text.isprintable() and not (ev.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            self._open(text)
        else:
            super().keyPressEvent(ev)

    def click(self) -> None:
        """(compatibility with the former push button)"""
        self._open("")


class TopBar(QWidget):
    def __init__(self, shell):
        super().__init__()
        self.shell = shell
        tr = shell.tr
        self.setObjectName("shell_topbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 3, 6, 3)
        h.setSpacing(8)
        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("shell_menu")
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.menu_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.menu_btn.setIcon(icon("settings"))
        self.menu_btn.setText("Mlaser")
        tr.bind(self.menu_btn, "shell.menu", self.menu_btn.setToolTip)
        tr.bind(self.menu_btn, "shell.menu", self.menu_btn.setAccessibleName)
        h.addWidget(self.menu_btn)
        self.file = QLabel()
        self.file.setObjectName("shell_file")
        self.file.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.file.setMinimumWidth(80)
        self.file.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        h.addWidget(self.file)
        h.addStretch(1)
        self.search = SearchField(lambda text: shell.open_palette(text))
        self.search.setObjectName("shell_search")
        key = QKeySequence("Ctrl+K").toString(QKeySequence.NativeText)
        tr.bind(self.search, "shell.search", lambda t: self.search.setPlaceholderText(f"{t}   {key}"))
        tr.bind(self.search, "shell.search", self.search.setAccessibleName)
        tr.bind(self.search, "shell.search.tip", self.search.setToolTip)
        h.addWidget(self.search, 2)
        h.addStretch(1)
        self.chip_box = QWidget()
        cb = QHBoxLayout(self.chip_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setSpacing(4)
        self.chips: dict[str, QLabel] = {}
        for kind in ORDER:
            key, sym, col = CHIPS[kind]
            lab = QLabel()
            lab.setObjectName(f"chip_{kind.lower().replace('-', '_')}")
            lab.setProperty("chip", True)
            bg, fg = COLORS[col]
            lab.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:9px; padding:2px 8px; font-weight:bold;")

            def setter(t, lab=lab, sym=sym):
                lab.setText(f"{sym} {t}")
                lab.setAccessibleName(t)
            tr.bind(lab, key, setter)
            tr.bind(lab, key + ".tip", lab.setToolTip)
            lab.hide()
            cb.addWidget(lab)
            self.chips[kind] = lab
        h.addWidget(self.chip_box)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("shell_mode")
        for m in um.MODES:
            self.mode_combo.addItem("", m)
        for i, m in enumerate(um.MODES):
            tr.bind(self.mode_combo, f"shell.mode.{m}", lambda t, i=i: self.mode_combo.setItemText(i, t))
        tr.bind(self.mode_combo, "shell.mode.tip", self.mode_combo.setToolTip)
        tr.bind(self.mode_combo, "shell.mode.title", self.mode_combo.setAccessibleName)
        self.mode_combo.activated.connect(self._mode_chosen)
        h.addWidget(self.mode_combo)
        self.lang_slot = QHBoxLayout()
        self.lang_slot.setContentsMargins(0, 0, 0, 0)
        h.addLayout(self.lang_slot)
        self._shown: tuple = ()
        self.set_mode(shell.modes.mode)
        self.set_file(None, False)

    # ------------------------------------------------------------------
    def take_language_combo(self, combo: QComboBox) -> None:
        combo.setParent(self)
        self.lang_slot.addWidget(combo)
        combo.show()

    def set_mode(self, mode: str) -> None:
        i = self.mode_combo.findData(mode)
        if i >= 0 and i != self.mode_combo.currentIndex():
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(i)
            self.mode_combo.blockSignals(False)

    def _mode_chosen(self, i: int) -> None:
        target = self.mode_combo.itemData(i)
        if not self.shell.request_mode(target):
            self.set_mode(self.shell.modes.mode)

    def set_file(self, path: Optional[str], dirty: bool) -> None:
        import os
        tr = self.shell.tr
        name = os.path.basename(path) if path else tr.tr("shell.untitled")
        mark = " ●" if dirty else ""
        self.file.setText(name + mark)
        tip = path or name
        if dirty:
            tip += "\n" + tr.tr("shell.unsaved")
        self.file.setToolTip(tip)
        self.file.setAccessibleName(name + (", " + tr.tr("shell.unsaved") if dirty else ""))

    def refresh_chips(self, st, alarms) -> None:
        kinds = tuple(chip_kinds(st, alarms))
        if kinds == self._shown:
            return
        self._shown = kinds
        for k, lab in self.chips.items():
            lab.setVisible(k in kinds)
        if st is not None and st.banner == "FAULT" and getattr(st, "fault", ""):
            self.chips["FAULT"].setToolTip(f"{self.shell.tr.tr('shell.chip.fault.tip')}\n{st.fault}")


__all__ = ["TopBar", "chip_kinds", "CHIPS"]
