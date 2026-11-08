"""Save and autosave without freezing the window (UI evaluation F3).

Serialising a large drawing (``.mlz``: the codec, the zip, the ``.chf`` export) and writing it
with ``fsync`` took 0.8-1.6 s on the GUI thread at 2,700 parts, and the autosave did that every
120 s.  The work is split in two:

* the **snapshot** stays on the GUI thread, where the document lives, but costs little:
  :class:`SlicedSnapshot` fills the copy-on-write cache of
  :meth:`DocumentService.plan_snapshot <mlaser.services.document.DocumentService.plan_snapshot>`
  in time-boxed slices (one event-loop turn each, a few tens of milliseconds), then takes the
  snapshot itself, which copies only the parts changed since.  The parts of that snapshot are
  never changed by anyone, so another thread may read them;
* the **serialisation and the I/O** run in a worker thread (:func:`start_worker`,
  :func:`run_in_worker`); the files are replaced atomically (:mod:`mlaser._atomic`).

:func:`snapshot_now` and :func:`run_in_worker` stay synchronous for their caller (Save returns
whether the file was written: closing a window waits for it) by waiting in a local event loop,
as :mod:`mlaser.ui.import_worker` does, so the window keeps repainting and the safety UI keeps
running meanwhile.
"""
from __future__ import annotations

import gc
import threading
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QProgressDialog

from mlaser.model import Document

#: GUI time per snapshot slice
SLICE_S = 0.03
SHOW_AFTER_MS = 400


class _Post(QObject):
    """Hands a worker's result to the GUI thread (a queued signal)."""
    done = Signal(object)


_LIVE: set = set()


class SlicedSnapshot(QObject):
    """Snapshot ``docs.document`` in event-loop slices; ``done(doc)`` on the GUI thread.

    Each slice copies the next parts through :meth:`plan_snapshot` (a part copied before and
    not edited since is reused, not copied).  A change of the document while the slices run
    (any DocumentService notification, or another part list) starts the pass again, cheaply:
    the parts already copied come from the cache.  After :data:`MAX_RESTARTS` the last pass is
    one call, so a drawing edited without pause still gets its autosave."""

    MAX_RESTARTS = 3

    def __init__(self, docs, done: Callable[[Document], None], parent: Optional[QObject] = None,
                 slice_s: float = SLICE_S, on_error: Optional[Callable[[BaseException], None]] = None) -> None:
        super().__init__(parent)
        self.docs, self._done, self.slice_s, self._on_error = docs, done, slice_s, on_error
        self._n = 16                          # parts per slice, adapted to the measured cost
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)
        self._unsub: Optional[Callable[[], None]] = None
        self.finished = False
        self.restarts = -1
        self._restart()

    def _restart(self) -> None:
        self.restarts += 1
        self._doc = self.docs.document
        self._parts = list(self._doc.parts)   # the parts (objects) this pass copies
        self._copies: list = []
        self._i = 0
        self._changed = False

    def _on_change(self, _kind: str) -> None:
        self._changed = True

    def start(self) -> "SlicedSnapshot":
        if QApplication.instance() is None:
            self._end(self.docs.plan_snapshot())
        else:
            self._unsub = self.docs.subscribe(self._on_change)
            self._timer.start()
        return self

    def cancel(self) -> None:
        self._stop()
        self.deleteLater()

    def _stop(self) -> None:
        self._timer.stop()
        self.finished = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _step(self) -> None:
        try:
            self._slice()
        except Exception as e:  # noqa: BLE001 - handed to the owner, the timer stops
            self._stop()
            self.deleteLater()
            if self._on_error is None:
                raise
            self._on_error(e)

    def _unchanged(self) -> bool:
        doc = self.docs.document
        return (not self._changed and doc is self._doc and len(doc.parts) == len(self._parts)
                and all(a is b for a, b in zip(doc.parts, self._parts)))

    def _slice(self) -> None:
        if not self._unchanged():
            if self.restarts >= self.MAX_RESTARTS:
                self._end(self.docs.plan_snapshot())
                return
            self._restart()
        t_end = time.perf_counter() + self.slice_s
        while self._i < len(self._parts) and time.perf_counter() < t_end:
            chunk = self._parts[self._i:self._i + self._n]
            t0 = time.perf_counter()
            self._copies.extend(self.docs.plan_snapshot(Document(parts=chunk)).parts)
            dt = time.perf_counter() - t0
            self._i += len(chunk)
            per = dt / max(1, len(chunk))
            self._n = max(1, min(2000, int(self.slice_s / 2 / per) if per > 0 else 2000))
        if self._i >= len(self._parts) and self._unchanged():
            doc = self._doc
            layers, extra = self.docs.lean_copy((doc.layers, doc.extra))
            self._end(Document(parts=self._copies, layers=layers, version=doc.version, extra=extra))

    def _end(self, snap: Document) -> None:
        self._stop()
        if QApplication.instance() is not None:
            self.deleteLater()
        self._done(snap)


def snapshot_now(docs, parent: Optional[QObject] = None) -> Document:
    """:class:`SlicedSnapshot`, waited for in a local event loop (synchronous for the caller)."""
    box: dict[str, Any] = {}
    if QApplication.instance() is None:
        return docs.plan_snapshot()
    loop = QEventLoop()

    def done(doc: Document) -> None:
        box["doc"] = doc
        loop.quit()
    s = SlicedSnapshot(docs, done, parent)
    s.start()
    if not s.finished:
        loop.exec()
    return box["doc"]


def start_worker(fn: Callable[[], Any], on_done: Callable[[Any, Optional[BaseException]], None],
                 name: str = "mlaser-save") -> threading.Thread:
    """``fn()`` in a worker thread; ``on_done(result, error)`` afterwards on the GUI thread."""
    post = _Post()
    _LIVE.add(post)          # released on the GUI thread only: a QObject must not die in the worker

    def deliver(r) -> None:
        _LIVE.discard(post)
        post.deleteLater()
        on_done(*r)
    post.done.connect(deliver, Qt.QueuedConnection)

    def work() -> None:
        from mlaser._sched import responsive_gil
        res: Any = None
        err: Optional[BaseException] = None
        # the serialisers allocate millions of short-lived, acyclic objects: each one of the
        # collections they trigger holds the interpreter for the whole heap of a large session
        # (measured 0.1-0.2 s at 2,700 parts, every thread stopped, the window too), and frees
        # nothing (as mlaser.cam.toolpath.build_toolpath does)
        was = gc.isenabled()
        gc.disable()
        try:
            with responsive_gil():           # the GUI thread gets the interpreter within 0.5 ms
                res = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the GUI thread
            err = e
        finally:
            if was:
                gc.enable()
        try:
            post.done.emit((res, err))
        except RuntimeError:                 # the application is gone
            pass

    th = threading.Thread(target=work, name=name, daemon=False)   # not abandoned at exit
    th.start()
    return th


def run_in_worker(parent, fn: Callable[[], Any], label: str, title: str = "") -> Any:
    """``fn()`` in a worker; returns its result or raises its exception.  Waits in a local event
    loop; after :data:`SHOW_AFTER_MS` a window-modal progress note without Cancel (a half-done
    save is never kept anyway, the old file stays until the new one is complete)."""
    if QApplication.instance() is None:
        return fn()
    box: dict[str, Any] = {}
    loop = QEventLoop()

    def done(res: Any, err: Optional[BaseException]) -> None:
        box["result"], box["error"] = res, err
        loop.quit()
    start_worker(fn, done)
    dlg: list[QProgressDialog] = []

    def show() -> None:
        if "error" in box:
            return
        d = QProgressDialog(label, "", 0, 0, parent)
        d.setCancelButton(None)
        if title:
            d.setWindowTitle(title)
        d.setWindowModality(Qt.WindowModal)
        d.setAutoClose(False)
        d.setAutoReset(False)
        d.show()
        dlg.append(d)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(show)
    timer.start(SHOW_AFTER_MS)
    if "error" not in box:
        loop.exec()
    timer.stop()
    for d in dlg:
        d.close()
        d.deleteLater()
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


def seal_undo(docs) -> None:
    """After the document was saved: the next edit starts a new undo step (no coalescing across
    the save point; ``UndoStack.seal`` where the undo stack has it)."""
    seal = getattr(getattr(docs, "stack", None), "seal", None)
    if callable(seal):
        seal()


__all__ = ["seal_undo", "SlicedSnapshot", "snapshot_now", "start_worker", "run_in_worker", "SLICE_S"]
