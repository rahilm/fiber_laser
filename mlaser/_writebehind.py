"""File writes off the calling thread: the run log and the alarm history are written from
event handlers that run on the GUI thread, and a file write there can stall the window.

On Windows an ``open`` / ``close`` of a file being written is held by the antivirus and the
indexer (Windows CI: the GUI thread stood 0.23-0.75 s in the run log's ``open(..., "a")``
right after the card lost power, issue #5), and ``fsync`` on a busy disk takes as long.
:func:`submit` queues a write for one daemon thread (``mlaser-files``) that runs the queued
writes in order; :func:`flush` waits until everything queued has been written (tests, before
reading a file back; the interpreter's exit, through ``atexit``).  In no layer, so any
package may import it.
"""
from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Callable, Hashable, Optional

log = logging.getLogger(__name__)

_cond = threading.Condition()
_queue: list[tuple[Optional[Hashable], Callable[[], None]]] = []
_busy = [False]
_running: list[Optional[Hashable]] = []      # the keys of the batch being written, in order
_started = [False]
#: how long the interpreter's exit waits for the queued writes
EXIT_FLUSH_S = 2.0


def submit(fn: Callable[[], None], key: Optional[Hashable] = None) -> None:
    """Run ``fn`` (a file write) on the writer thread, after every write queued before it.
    With ``key``, a write still queued under the same key is replaced in its place (a file
    rewritten as a whole: only the newest content matters).  An exception ``fn`` raises is
    logged, never raised here."""
    with _cond:
        if key is not None:
            for i, (k, _fn) in enumerate(_queue):
                if k == key:
                    _queue[i] = (key, fn)
                    break
            else:
                _queue.append((key, fn))
        else:
            _queue.append((None, fn))
        if not _started[0]:
            _started[0] = True
            from ._sched import start_now
            start_now(_run, "mlaser-files")
            atexit.register(flush, EXIT_FLUSH_S)
        _cond.notify_all()


def flush(timeout: float = 10.0, key: Optional[Hashable] = None) -> bool:
    """Block until every write queued so far has run (with ``key``: the write queued under
    that key; bounded); True when it did."""
    end = time.monotonic() + timeout

    def pending() -> bool:
        if key is None:
            return bool(_queue) or _busy[0]
        return key in _running or any(k == key for k, _fn in _queue)
    with _cond:
        while pending():
            left = end - time.monotonic()
            if left <= 0:
                return False
            _cond.wait(left)
    return True


def _run() -> None:
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
            batch = list(_queue)
            del _queue[:]
            _running[:] = [k for k, _fn in batch]
            _busy[0] = True
        try:
            for _key, fn in batch:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - a failed write never ends the writer
                    log.exception("file write failed")
                with _cond:
                    del _running[:1]
                    _cond.notify_all()
        finally:
            with _cond:
                _running.clear()
                _busy[0] = False
                _cond.notify_all()


__all__ = ["submit", "flush", "EXIT_FLUSH_S"]
