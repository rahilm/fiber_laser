"""Long document work with progress and Cancel, without freezing the window.

Arraying a large selection made every copy on the GUI thread: seconds without a repaint, no
way to stop it (UX review 2026-11).  :func:`run_with_progress` runs ``fn(progress)`` in a
worker thread (:func:`mlaser.ui.save_worker.start_worker`) and waits in a local event loop, so
the window keeps repainting and the safety UI keeps running (Stop / Hold keys while a job
runs).  After :data:`SHOW_AFTER_MS` a window-modal progress dialog shows "n of N" and a Cancel
button; ``progress(done, total)`` returns False once Cancel was pressed, and the work stops at
its next step.

The work must only *read* the document and return what it made; the caller applies the result
on the GUI thread in one undoable edit, so Cancel leaves the document as it was.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QProgressDialog

#: no dialog for work that ends quickly
SHOW_AFTER_MS = 300
#: how often the dialog shows the count
TICK_MS = 100

Progress = Callable[[int, int], bool]


class TaskCancelled(Exception):
    """Cancel was pressed: the work stopped and its result was dropped."""


def run_with_progress(parent: Any, fn: Callable[[Progress], Any], label: str, title: str = "",
                      cancel_text: str = "") -> Any:
    """``fn(progress)`` in a worker; its result, or its exception, or :class:`TaskCancelled`.

    Without a QApplication (scripts) ``fn`` runs here."""
    if QApplication.instance() is None:
        return fn(lambda _d, _t: True)
    from .save_worker import start_worker
    cancel = threading.Event()
    state = {"done": 0, "total": 0}

    def progress(done: int, total: int) -> bool:
        state["done"], state["total"] = int(done), int(total)
        return not cancel.is_set()

    box: dict[str, Any] = {}
    loop = QEventLoop()

    def finished(res: Any, err: Optional[BaseException]) -> None:
        box["result"], box["error"] = res, err
        loop.quit()
    start_worker(lambda: fn(progress), finished, name="mlaser-task")
    dlg = QProgressDialog(label, cancel_text, 0, 0, parent if hasattr(parent, "windowHandle") else None)
    dlg.setObjectName("progress_task")
    if title:
        dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumDuration(SHOW_AFTER_MS)
    dlg.hide()

    def on_cancel() -> None:
        cancel.set()
        dlg.setLabelText(label)
    dlg.canceled.connect(on_cancel)
    show = QTimer()
    show.setSingleShot(True)
    show.timeout.connect(lambda: None if "error" in box else dlg.show())
    tick = QTimer()
    tick.setInterval(TICK_MS)

    def update() -> None:
        if dlg.wasCanceled():                 # Escape / closing the dialog cancel too
            on_cancel()
        if state["total"] and not cancel.is_set():
            dlg.setMaximum(state["total"])
            dlg.setValue(min(state["done"], state["total"]))
    tick.timeout.connect(update)
    if "error" not in box:
        show.start(SHOW_AFTER_MS)
        tick.start()
        loop.exec()
    show.stop()
    tick.stop()
    dlg.canceled.disconnect(on_cancel)
    dlg.close()
    dlg.deleteLater()
    if cancel.is_set():
        raise TaskCancelled("cancelled")
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


__all__ = ["run_with_progress", "TaskCancelled", "SHOW_AFTER_MS"]
