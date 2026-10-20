"""Open and import files off the GUI thread, with a progress dialog and Cancel.

A customer file is untrusted input (:mod:`mlaser.io.limits`): even within the limits a large
drawing can take seconds to read, and the GUI thread also carries the run screen's Stop.
:func:`run_import` runs the loader in a worker thread inside an
:class:`~mlaser.io.limits.ImportContext` and waits in a local event loop, so the window keeps
repainting and the safety UI keeps running.  After 0.3 s a window-modal progress dialog shows
the running count (entities, elements, bytes) and a Cancel button: Cancel sets the context's
flag, which the importers check at their budget counters (:class:`ImportCancelled`); a loader
that does not reach a check point in time is abandoned (its result is discarded).

The call itself stays synchronous for the caller (``MainWindow.load_path`` returns a bool).
:func:`show_error` shows a refused file as a non-blocking dialog.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from mlaser.io import limits as LIM
from mlaser.io.limits import ImportCancelled

SHOW_AFTER_MS = 300
WORKER_STACK = 64 * 2**20        # deep but legal nesting must not overflow a thread's C stack
_busy = threading.Event()        # one import at a time (the local event loop re-enters the GUI)


class _Done(QObject):
    done = Signal()


class ImportBusy(RuntimeError):
    """Another file is being opened."""


def busy() -> bool:
    return _busy.is_set()


def run_import(parent, fn: Callable[[], Any], label: str = "Opening...") -> Any:
    """``fn()`` in a worker thread (see the module doc); returns its result or raises its
    exception, :class:`ImportCancelled` after Cancel, :class:`ImportBusy` while another import
    runs."""
    if QApplication.instance() is None:
        with LIM.context():
            return fn()
    if _busy.is_set():
        raise ImportBusy("another file is being opened")
    _busy.set()
    try:
        return _run(parent, fn, label)
    finally:
        _busy.clear()


def _run(parent, fn: Callable[[], Any], label: str) -> Any:
    ctx = LIM.ImportContext()
    box: dict[str, Any] = {}
    sig = _Done()
    loop = QEventLoop()
    sig.done.connect(loop.quit, Qt.QueuedConnection)

    def work() -> None:
        try:
            with LIM.context(ctx):
                box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the GUI thread
            box["error"] = e
        finally:
            box["finished"] = True
            try:
                sig.done.emit()
            except RuntimeError:       # the bridge object is gone (the caller gave up)
                pass

    old = threading.stack_size()
    try:
        threading.stack_size(WORKER_STACK)
    except (ValueError, RuntimeError):
        pass
    try:
        th = threading.Thread(target=work, name="mlaser-import", daemon=True)
        th.start()
    finally:
        try:
            threading.stack_size(old)
        except (ValueError, RuntimeError):
            pass
    th.join(0.02)                       # most files are read before a dialog would show
    dlg: Optional[QProgressDialog] = None
    if not box.get("finished"):
        dlg = QProgressDialog(label, "Cancel", 0, 0, parent)
        dlg.setWindowTitle("Opening file")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.hide()                          # shown by the timer below (no setValue calls)
        show = QTimer()
        show.setSingleShot(True)
        show.timeout.connect(dlg.show)
        show.start(SHOW_AFTER_MS)

        def cancel() -> None:
            ctx.cancel()
            loop.quit()
        dlg.canceled.connect(cancel)
        tick = QTimer()
        tick.setInterval(100)

        def update() -> None:
            if ctx.count and dlg is not None:
                dlg.setLabelText(f"{label}\n{ctx.count:,} {ctx.what}")
        tick.timeout.connect(update)
        tick.start()
        if not box.get("finished"):     # finished between the check and the connect: no wait
            # the worker is Python and holds the GIL between its calls: with the default 5 ms
            # switch interval the GUI thread lost most hand-overs (macOS CI: 3 ticks of a 20 ms
            # timer in 1 s); 0.5 ms while the dialog waits (mlaser._sched, review 2 P1-1)
            from mlaser._sched import responsive_gil
            with responsive_gil():
                loop.exec()
        tick.stop()
        show.stop()
        dlg.canceled.disconnect(cancel)
        dlg.close()
        dlg.deleteLater()
    if ctx.cancelled.is_set() and not box.get("finished"):
        raise ImportCancelled("import cancelled")
    if "error" in box:
        raise box["error"]
    if ctx.cancelled.is_set() and "result" not in box:
        raise ImportCancelled("import cancelled")
    return box.get("result")


def show_error(parent, title: str, text: str) -> None:
    """A refused file as a dialog that does not block the caller (nor a test)."""
    if parent is None or getattr(parent, "_test_no_dialogs", False):
        return
    box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Ok, parent)
    box.setAttribute(Qt.WA_DeleteOnClose, True)
    box.open()


__all__ = ["run_import", "show_error", "busy", "ImportBusy", "ImportCancelled"]
