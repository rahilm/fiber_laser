"""A busy state for a command that works on the GUI thread for a noticeable time (copying,
pasting or arraying a few hundred parts): the wait cursor and a status-bar line ("Copying 256
parts…") are shown *before* the work starts, so the window does not look frozen, and taken away
after it (UX review 2026-11 finding 12).

Work that takes seconds belongs in a worker with a progress dialog (``ui/save_worker.py``);
this is for the fraction of a second to a second that is not worth one.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@contextmanager
def busy(window: Any, text: str, on: bool = True) -> Iterator[None]:
    """Show ``text`` in ``window``'s status bar and the wait cursor while the block runs
    (``on=False``: nothing, for small amounts of work)."""
    if not on or QApplication.instance() is None:
        yield
        return
    sb = None
    try:
        sb = window.statusBar()
        sb.showMessage(text)
        sb.repaint()                      # painted now: the work below blocks the event loop
    except (AttributeError, RuntimeError):
        sb = None
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()
        if sb is not None:
            try:
                if sb.currentMessage() == text:
                    sb.clearMessage()
            except RuntimeError:
                pass


__all__ = ["busy"]
