"""The top bar: file name with the unsaved-changes marker, command search, the safety state
chips, the user mode and the language (docs/review/ux.md §4).  The application menu is the
native menu bar (menubar.py); the old in-window "Mlaser ⌄" button stays hidden for plugins
that looked it up.

The chips summarise Safety's state (:class:`mlaser.machine.safety.SafetyStatus`, the same
snapshot the state banner shows): ``SIMULATOR``, ``DRY-RUN``, ``ARMED`` / ``DISARMED``,
``FAULT`` and ``ALARM``.  Every chip carries its meaning as text and a shape symbol, never by
colour alone, and has an accessible name and a tooltip.  The state banner and the run bar of
:mod:`mlaser.ui.safety_ui` stay where they are; a chip is shown only for a state the banner
does not show itself (:func:`covered`: today the banner shows them all, the simulator included).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QToolButton, QWidget)

from mlaser.services import usermodes as um

from .. import style
from ..icons import STATE_ICONS, icon
from ..state_display import StateBadge

#: chip kind -> (lang id, state icon (icons.STATE_ICONS), colour key of the theme's state tokens)
CHIPS = {
    "FAULT": ("shell.chip.fault", "fault", "FAULT"),
    "ALARM": ("shell.chip.alarm", "alarm", "ALARM"),
    "ARMED": ("shell.chip.armed", "armed", "ARMED"),
    "DISARMED": ("shell.chip.disarmed", "disarmed", "DISARMED"),
    "DRY-RUN": ("shell.chip.dry_run", "dry_run", "DRY-RUN"),
    "SIMULATOR": ("shell.chip.simulator", "simulator", "SIMULATOR"),
    "OFFLINE": ("shell.chip.offline", "offline", "OFFLINE"),
}
ORDER = ("FAULT", "ALARM", "ARMED", "DISARMED", "DRY-RUN", "SIMULATOR", "OFFLINE")


def covered(st, alarms) -> set[str]:
    """The states the state banner already shows in full: its own state, the laser's (its badge,
    or its chip inside an alarm block), the SIMULATOR chip it shows in every state, the alarm
    and the fault of its alarm block.  Their chips would only repeat them (UI audit D-3: one
    display per state on one screen; UI review 2026-11 #1: the top bar no longer reflows when an
    alarm comes)."""
    if st is None:
        return {"OFFLINE"}
    out = {st.banner}
    state = getattr(getattr(st, "state", None), "value", "offline")
    if st.armed:
        out.add("ARMED")
    elif st.dry_run:
        out.add("DRY-RUN")
    elif state != "offline":
        out.add("DISARMED")
    if st.simulator:
        out.add("SIMULATOR")
    if alarms:
        out.add("ALARM")
    return out


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
        h.setContentsMargins(style.SP2, style.SP1, style.SP2, style.SP1)
        h.setSpacing(style.SP2)
        self.menu_btn = QToolButton()
        self.menu_btn.setObjectName("shell_menu")
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.menu_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.menu_btn.setIcon(icon("settings"))
        self.menu_btn.setText("Mlaser")  # i18n: ok (product name)
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
        cb.setSpacing(style.SP1)
        self.chips: dict[str, StateBadge] = {}
        for kind in ORDER:
            key, state, col = CHIPS[kind]
            lab = StateBadge(f"chip_{kind.lower().replace('-', '_')}", role="label_strong", icon_px=style.ICON_S,
                             caption=False)
            lab.setProperty("chip", True)

            def setter(t, lab=lab, state=state, col=col):
                lab.set_state(state, col, t)
            tr.bind(lab, key, setter)
            tr.bind(lab, key + ".tip", lab.setToolTip)
            lab.hide()
            cb.addWidget(lab)
            self.chips[kind] = lab
        # interlocks that are not configured: a persistent chip in every workspace that opens
        # Safety I/O (UX audit W15; it used to show only on the Run screen)
        self.interlocks = QToolButton()
        self.interlocks.setObjectName("chip_interlocks")
        self.interlocks.setProperty("chip", True)
        self.interlocks.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.interlocks.setIcon(icon(STATE_ICONS["interlock"], style.css("on_warning")))
        self.interlocks.setFocusPolicy(Qt.StrongFocus)
        style.styled(self.interlocks, lambda: (
            f"QToolButton#chip_interlocks{{background:{style.css('warning_fill')};color:{style.css('on_warning')};"
            f"border:1px solid {style.css('warning')};border-radius:{style.SP2}px;padding:0 {style.SP2}px;"
            f"font-weight:bold;}}"))
        tr.bind(self.interlocks, "shell.chip.interlocks.tip", self.interlocks.setToolTip)
        self.interlocks.clicked.connect(self._open_safety_io)
        self.interlocks.hide()
        self._interlocks_n = 0
        cb.addWidget(self.interlocks)
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
        self.file.setAccessibleName(tr.format("shell.file.unsaved_name", name=name) if dirty else name)

    def refresh_chips(self, st, alarms) -> None:
        """The chips of the states the banners do not already show (a second state next to the
        banner's: SIMULATOR while ARMED, DRY-RUN...)."""
        hide = covered(st, alarms)
        kinds = tuple(k for k in chip_kinds(st, alarms) if k not in hide)
        for k in kinds:                      # the theme's colours (a theme switch included)
            _key, state, col = CHIPS[k]
            self.chips[k].set_state(state, col, self.chips[k].text())
        if kinds == self._shown:
            return
        self._shown = kinds
        for k, lab in self.chips.items():
            lab.setVisible(k in kinds)
        if st is not None and st.banner == "FAULT" and getattr(st, "fault", ""):
            self.chips["FAULT"].setToolTip(self.shell.tr.format("shell.chip.fault.tip_reason", fault=st.fault))

    def refresh_interlocks(self, n: int) -> None:
        """"Interlocks: N not configured" while any safety input or the heartbeat is unset."""
        if n == self._interlocks_n and self.interlocks.isHidden() == (n == 0):
            return
        self._interlocks_n = n
        self._interlocks_text()
        self.interlocks.setVisible(n > 0)

    #: below this top-bar width the interlock chip shows only its icon and count (its full text
    #: is its accessible name and tooltip): a half-screen window keeps its search field
    COMPACT_W = 1100

    def _interlocks_text(self) -> None:
        n = self._interlocks_n
        if not n:
            return
        full = self.shell.tr.plural("shell.chip.interlocks", n)
        text = full if self._wide() else str(n)
        if self.interlocks.text() != text:
            self.interlocks.setText(text)
        if self.interlocks.accessibleName() != full:
            self.interlocks.setAccessibleName(full)

    def _wide(self) -> bool:
        return self.width() >= self.COMPACT_W

    def resizeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(ev)
        self._interlocks_text()
        # narrow: the state chips show their icon on their colour; the word stays their
        # accessible name and tooltip (the banner beside them names the main state in full)
        wide = self._wide()
        for lab in self.chips.values():
            if lab.text_label.isHidden() == wide:
                lab.text_label.setVisible(wide)

    def _open_safety_io(self) -> None:
        ui = getattr(self.shell.w, "safety_ui", None)
        if ui is not None:
            ui.open_safety_io()


__all__ = ["TopBar", "chip_kinds", "covered", "CHIPS"]
