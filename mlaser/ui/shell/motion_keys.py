"""The motion-starting shortcuts act only on the Run workspace (deep review 2026-11, UX 7 and 8).

Start (F5), Frame (F6) and Dry run (Shift+F6), and their macOS ⌘ alternatives, are window-wide
QAction shortcuts: the menus and tooltips show them.  This guard sits on those three actions
and looks at every activation *by the keyboard* (``QEvent.Shortcut``; a menu click, a button
or the command palette never goes through it):

* the Run workspace is showing and no text field has the focus: the shortcut runs as before
  (pre-flight and the arm question unchanged);
* a text-entry widget has the focus (search field, a value being typed): nothing happens; the
  status bar says why;
* any other workspace: the window switches to the Run workspace and puts the focus on the
  matching button; nothing starts; the status bar says so.

Key auto-repeat never starts anything (the actions do not auto-repeat).  Space and Enter never
press a focused Start, Frame, Dry run or breakpoint-resume button either: Space is the Hold
key, and the shortcut above leaves the focus on such a button.  Those buttons still work with
the mouse, touch and accessibility tools, and from the keyboard by their shortcut on the Run
workspace.

Stop (F12, Esc while a job runs) is not touched here: it stays global and instant.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit,
                               QPushButton, QTextEdit, QWidget)

#: command id -> the run control it starts (``run_kind`` of the buttons, ui/style.run_button)
MOTION_COMMANDS = {"run.start": "start", "run.frame": "frame", "run.dry_run": "dry_run"}
#: buttons a stray Space / Enter must not press: the run controls that start motion, and the
#: Run screen's breakpoint resume
_GUARDED_KINDS = frozenset(MOTION_COMMANDS.values())
_GUARDED_NAMES = frozenset({"run_resume_here", "run_resume_contour"})
_PRESS_KEYS = (Qt.Key_Space, Qt.Key_Select, Qt.Key_Return, Qt.Key_Enter)
RUN_WORKSPACE = "run"
#: the status text after a switch to the Run workspace, per run control
_SWITCHED = {"start": "shell.motion_key.switched_start", "frame": "shell.motion_key.switched_frame",
             "dry_run": "shell.motion_key.switched_dry_run"}


def is_text_entry(w: Optional[QWidget]) -> bool:
    """``w`` takes typed text (a line edit, a text box, a spin box, an editable combo box)."""
    if w is None:
        return False
    if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return True
    return isinstance(w, QComboBox) and w.isEditable()


def guarded_button(w: Any) -> bool:
    return isinstance(w, QPushButton) and (w.property("run_kind") in _GUARDED_KINDS
                                           or w.objectName() in _GUARDED_NAMES)


class MotionKeyGuard(QObject):
    """See the module doc.  ``shell``: the :class:`~mlaser.ui.shell.Shell`."""

    def __init__(self, shell: Any) -> None:
        super().__init__(shell.w)
        self.shell = shell
        self.w = shell.w
        self._kinds: dict[int, str] = {}
        self.last: str = ""                  # what the last keyboard activation did (tests)

    def install(self) -> None:
        for cmd, kind in MOTION_COMMANDS.items():
            act = self.shell.action(cmd)
            if act is None or id(act) in self._kinds:
                continue
            act.setAutoRepeat(False)         # a held key never starts (or restarts) motion
            act.installEventFilter(self)
            self._kinds[id(act)] = kind
        self.guard_buttons()

    def guard_buttons(self) -> None:
        """Space / Enter on a focused motion button do nothing (new buttons: call again)."""
        for b in self.w.findChildren(QPushButton):
            if guarded_button(b) and not b.property("motion_key_guard"):
                b.setProperty("motion_key_guard", True)
                b.installEventFilter(self)

    # ------------------------------------------------------------------ Qt
    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        t = ev.type()
        if t == QEvent.Shortcut:
            kind = self._kinds.get(id(obj))
            if kind is None:
                return False
            return self.activated(kind)
        if t in (QEvent.KeyPress, QEvent.KeyRelease) and isinstance(obj, QPushButton):
            try:
                key = ev.key()
            except AttributeError:
                return False
            if key in _PRESS_KEYS and ev.modifiers() in (Qt.NoModifier, Qt.KeypadModifier):
                if t == QEvent.KeyPress and not ev.isAutoRepeat():
                    self._say("shell.motion_key.space_on_button")
                    self.last = "button_key"
                return True
        return False

    # ------------------------------------------------------------------ decision
    def current_workspace(self) -> str:
        return str(getattr(self.shell.host, "current", "") or "")

    def activated(self, kind: str) -> bool:
        """A motion shortcut was pressed: True = taken here (nothing starts)."""
        if is_text_entry(QApplication.focusWidget()):
            self.last = "text_field"
            self._say("shell.motion_key.typing")
            return True
        if self.current_workspace() == RUN_WORKSPACE:
            self.last = "run"
            return False                     # the command runs as before
        self.last = "switched"
        self.shell.show_workspace(RUN_WORKSPACE)
        if self.current_workspace() != RUN_WORKSPACE:
            self._say("shell.motion_key.no_run")
            return True
        self.guard_buttons()
        b = self.button(kind)
        if b is not None:
            b.setFocus(Qt.ShortcutFocusReason)
        self._say(_SWITCHED[kind])
        return True

    def button(self, kind: str) -> Optional[QPushButton]:
        """The visible run control of ``kind``: the Run screen's, else the run bar's."""
        page = getattr(self.shell.host, "pages", {}).get(RUN_WORKSPACE)
        roots = [r for r in (page, self.w) if r is not None]
        for root in roots:
            for b in root.findChildren(QPushButton):
                if b.property("run_kind") == kind and b.isVisible():
                    return b
        return None

    def _say(self, key: str) -> None:
        tr = self.shell.tr
        self.w.statusBar().showMessage(tr.tr(key), 6000)


__all__ = ["MotionKeyGuard", "MOTION_COMMANDS", "is_text_entry", "guarded_button"]
