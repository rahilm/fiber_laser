"""Wave 3 task workspaces (docs/review/ux.md §4): Run (operator screen and job queue), Nest and
Report.  Each module is a plugin (``register(ctx)``, docs/dev/PLUGINS.md) that adds its widget
with ``ctx.add_workspace`` and a ``workspace.<name>`` command to show it.

The modules are listed in ``mlaser.ui.mainwindow.EXTENSIONS`` (full module paths; plugin
names ``run``, ``nest``, ``report``) and the shell hosts each widget in its workspace slot
(:mod:`mlaser.ui.shell`).  :func:`show_workspace` switches through the main window's
``show_workspace(name)``; a host without the shell (a bare test window) gets the workspace in a
window of its own.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QVBoxLayout, QWidget

log = logging.getLogger("mlaser.ui.workspaces")

PLUGINS = ("mlaser.ui.workspaces.run", "mlaser.ui.workspaces.nest", "mlaser.ui.workspaces.report",
           "mlaser.ui.workspaces.orders")
from ..style import TARGET_RUN as TOUCH, TARGET_TOUCH as TOUCH_MIN  # noqa: E402 - primary / other touch targets


def tr(window: Any, key: str, default: str = "", **kw: Any) -> str:
    """Translated text of the readable id ``key`` (``default`` when the catalogue lacks it),
    named placeholders filled from ``kw``."""
    t = getattr(window, "tr_", None)
    if t is None:
        text = default or key
        try:
            return text.format(**kw) if kw else text
        except (KeyError, IndexError, ValueError):
            return text
    try:
        if kw:
            return t.format(key, default=default or None, **kw)
        return t.tr(key, default or None)
    except Exception:  # noqa: BLE001 - a broken catalogue entry must not break the screen
        return default or key


class WorkspaceWindow(QWidget):
    """A workspace in its own top-level window (until the shell's switcher exists)."""

    def __init__(self, title: str, widget: QWidget, parent: Optional[QWidget] = None,
                 on_close: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.setObjectName(f"workspace_window_{widget.objectName()}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget)
        self.widget = widget
        self._on_close = on_close
        fs = QShortcut(QKeySequence(Qt.Key_F11), self)
        fs.activated.connect(self.toggle_full_screen)
        self.resize(1280, 800)

    def toggle_full_screen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt API
        if self._on_close is not None:
            self._on_close()
        super().closeEvent(ev)


def show_workspace(ctx: Any, name: str, widget: QWidget, title: str) -> Optional[QWidget]:
    """Show workspace ``name`` (the full name ``<plugin>.<name>``): the shell's switcher when
    the main window has one, else its own window."""
    window = ctx.window
    full = f"{ctx.name}.{name}"
    switch = getattr(window, "show_workspace", None)
    if callable(switch):
        switch(full)
        return None
    host = getattr(widget, "_workspace_window", None)
    if host is None or widget.window() is not host:
        if widget.parent() is not None and not isinstance(widget.window(), WorkspaceWindow):
            # adopted by a container the shell made: just make it visible there
            widget.show()
            return None
        host = WorkspaceWindow(title, widget, window if isinstance(window, QWidget) else None)
        widget._workspace_window = host
    host.show()
    host.raise_()
    host.activateWindow()
    return host


__all__ = ["PLUGINS", "TOUCH", "TOUCH_MIN", "tr", "show_workspace", "WorkspaceWindow"]
