"""The command palette (Ctrl+K / F1): fuzzy search over every registered command.

Each row shows the translated label, the command's workspace and its shortcut; Enter (or a
click) runs it through the main window (``window.run``, so the user mode, the safety checks and
the job lock apply exactly as for a toolbar button).  Recently used commands come first.
Commands the user mode does not allow are not listed; disabled ones are listed greyed (Enter
explains why instead of running them).  A search without a result shows "No command matches"
instead of an empty list.  The search itself is Qt-free
(:mod:`mlaser.services.command_search`).
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from mlaser.services.command_search import Entry, search

RIGHT_ROLE = Qt.UserRole + 1          # "Workspace   Ctrl+K" shown right-aligned


class _RowDelegate(QStyledItemDelegate):
    """Label on the left, workspace and shortcut right-aligned and muted."""

    def sizeHint(self, option, index):  # noqa: N802 - Qt API
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), option.fontMetrics.height() + 10))

    def paint(self, painter, option, index) -> None:
        right = index.data(RIGHT_ROLE) or ""
        super().paint(painter, option, index)
        if not right:
            return
        painter.save()
        sel = bool(option.state & QStyle.State_Selected)
        pal = option.palette
        col = pal.color(QPalette.HighlightedText) if sel else pal.color(QPalette.PlaceholderText)
        painter.setPen(col)
        r = option.rect.adjusted(0, 0, -8, 0)
        painter.drawText(r, Qt.AlignRight | Qt.AlignVCenter, right)
        painter.restore()



class CommandPalette(QFrame):
    """A popup over the main window (not modal, so it never blocks the run bar)."""

    commandChosen = Signal(str)
    MAX_ROWS = 60

    def __init__(self, window, entries: Callable[[], list[Entry]], recent: Callable[[], list[str]], tr):
        super().__init__(window)
        self.setObjectName("shell_palette")
        self.w = window
        self.tr = tr
        self._entries = entries
        self._recent = recent
        self.setFrameShape(QFrame.StyledPanel)
        self.setAutoFillBackground(True)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)
        self.edit = QLineEdit()
        self.edit.setObjectName("shell_palette_query")
        tr.bind(self.edit, "shell.palette.placeholder", self.edit.setPlaceholderText)
        tr.bind(self.edit, "shell.palette.title", self.edit.setAccessibleName)
        self.edit.textChanged.connect(self.refresh)
        self.edit.installEventFilter(self)
        v.addWidget(self.edit)
        self.list = QListWidget()
        self.list.setObjectName("shell_palette_list")
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.itemActivated.connect(self._activate_item)
        self.list.itemClicked.connect(self._activate_item)
        tr.bind(self.list, "shell.palette.title", self.list.setAccessibleName)
        v.addWidget(self.list, 1)
        # a search without a result says so instead of showing an empty list
        self.empty = QLabel()
        self.empty.setObjectName("shell_palette_empty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setAccessibleName("No command matches")
        self.empty.hide()
        v.addWidget(self.empty, 1)
        self.hint = QLabel()
        self.hint.setObjectName("shell_empty_text")
        tr.bind(self.hint, "shell.palette.hint", self.hint.setText)
        v.addWidget(self.hint)
        self.hide()
        self._matches: list = []

    # ------------------------------------------------------------------ show / hide
    def open(self, text: str = "") -> None:
        self._place()
        self.edit.setText(text)
        self.refresh()
        self.show()
        self.raise_()
        self.edit.setFocus(Qt.PopupFocusReason)
        self.edit.selectAll()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def close_palette(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.hide()

    def _place(self) -> None:
        pw = self.w.width()
        width = max(360, min(720, int(pw * 0.6)))
        height = max(260, min(520, int(self.w.height() * 0.6)))
        self.setGeometry((pw - width) // 2, 48, width, height)

    # ------------------------------------------------------------------ search
    def refresh(self) -> None:
        q = self.edit.text()
        matches = search(self._entries(), q, self._recent(), include_disabled=True)
        self._matches = matches[: self.MAX_ROWS]
        self.list.clear()
        recent_lbl = self.tr.tr("shell.palette.recent")
        for m in self._matches:
            e = m.entry
            right = "    ".join(x for x in (e.workspace, e.shortcut) if x)
            prefix = "★ " if m.recent else ""
            it = QListWidgetItem(f"{prefix}{e.label}")
            it.setData(Qt.UserRole, e.id)
            it.setData(RIGHT_ROLE, right)
            tip = [e.id]
            if m.recent:
                tip.append(recent_lbl)
            if not e.enabled:
                tip.append(self.tr.tr("shell.palette.disabled"))
                it.setForeground(QColor(self.palette().color(self.palette().ColorGroup.Disabled,
                                                             self.palette().ColorRole.Text)))
            it.setToolTip(" · ".join(tip))
            it.setData(Qt.AccessibleTextRole, f"{e.label}, {e.workspace}, {e.shortcut}".strip(", "))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)
        none = not self._matches
        if none:
            self.empty.setText(self.tr.format("shell.palette.no_matches", query=q.strip()))
        self.empty.setVisible(none)
        self.list.setVisible(not none)

    def matches(self) -> list:
        return list(self._matches)

    def _activate_item(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            return
        cmd = item.data(Qt.UserRole)
        ent = next((m.entry for m in self._matches if m.entry.id == cmd), None)
        if ent is not None and not ent.enabled:
            self.hint.setText(self.tr.format("shell.palette.not_now", label=ent.label))
            return
        self.close_palette()
        self.commandChosen.emit(cmd)

    def run_current(self) -> None:
        self._activate_item(self.list.currentItem())

    # ------------------------------------------------------------------ keys
    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt API
        t = ev.type()
        if t == QEvent.MouseButtonPress and self.isVisible():
            # a click outside closes the palette
            if isinstance(obj, QWidget) and obj.window() is self.w and not self.isAncestorOf(obj) and obj is not self:
                self.close_palette()
            return False
        if obj is self.edit and t == QEvent.ShortcutOverride and ev.key() == Qt.Key_Escape:
            from ..safety_ui import job_running
            if not job_running(self.w):      # Esc closes the palette (not the window's deselect)
                ev.accept()
                return True
            return False
        if obj is self.edit and t == QEvent.KeyPress:
            k = ev.key()
            if k in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp):
                QApplication.sendEvent(self.list, ev)
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self.run_current()
                return True
            if k == Qt.Key_Escape:
                # not consumed: while a job runs, Esc is also Stop (safety_ui's global keys)
                self.close_palette()
                return False
        return False


__all__ = ["CommandPalette"]
