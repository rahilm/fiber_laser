"""Wave 3 task workspaces (docs/review/ux.md §4): Run (operator screen and job queue), Nest and
Report.  Each module is a plugin (``register(ctx)``, docs/dev/PLUGINS.md) that adds its widget
with ``ctx.add_workspace`` and a ``workspace.<name>`` command to show it.

Until the shell has a workspace switcher, :func:`show_workspace` shows a workspace in its own
window (the Run screen can go full screen, F11); when the main window provides
``show_workspace(name)`` (the Shell track's switcher), that is used instead.

:func:`bootstrap` registers the workspace plugins from ``ext_nest`` while
``mlaser.ui.mainwindow.EXTENSIONS`` does not list them yet (merge note: add
``"mlaser.ui.workspaces.run", "mlaser.ui.workspaces.nest", "mlaser.ui.workspaces.report"``
to EXTENSIONS (full module paths) and this becomes a
no-op).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QVBoxLayout, QWidget

log = logging.getLogger("mlaser.ui.workspaces")

PLUGINS = ("mlaser.ui.workspaces.run", "mlaser.ui.workspaces.nest", "mlaser.ui.workspaces.report")
TOUCH = 56          # primary touch targets (px, docs/SAFETY.md run bar)
TOUCH_MIN = 44      # every other control on the touch screens


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


def bootstrap(ctx: Any) -> list[str]:
    """Register the workspace plugins (see the module docstring); returns their names."""
    window = ctx.window
    try:
        from mlaser.ui import mainwindow
        listed = {n.rsplit(".", 1)[-1] for n in getattr(mainwindow, "EXTENSIONS", ())}
    except Exception:  # noqa: BLE001
        listed = set()
    from mlaser.services.plugins import PluginInfo, register_plugin
    done = []
    contexts = getattr(window, "plugin_contexts", None)
    exts = getattr(window, "extensions", None)
    if contexts is None or exts is None:
        return done
    for target in PLUGINS:
        name = target.rsplit(".", 1)[-1]
        if name in listed or name in contexts:
            continue
        pctx, result = register_plugin(window, PluginInfo(name, target))
        if pctx is not None:
            contexts[name] = pctx
            exts[name] = result
            done.append(name)
    return done


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


__all__ = ["PLUGINS", "TOUCH", "TOUCH_MIN", "tr", "bootstrap", "show_workspace", "WorkspaceWindow"]
