"""Cyclic garbage collection without the incremental collector of CPython 3.14.0-3.14.4.

That collector starts every automatic collection by marking the objects on the value stack of
*every* thread's frames (``mark_stacks``), trusting each frame's saved stack pointer.  The
pointer can still cover an argument the interpreter has already released: ``CALL_BUILTIN_O``
drops its argument after the call, and when that was the last reference to a PySide6 wrapper
(``item.setBrush(QBrush(c))``), shiboken's deallocator releases the GIL while the C++
destructor runs.  A thread that takes the GIL then and allocates (the plan thread) starts a
collection that reads the dying object: a segmentation fault in ``mark_stacks``, or a corrupt
collector list that crashes a later collection ("Garbage-collecting" in ``mlaser-plan_0``, CI on
Ubuntu's 3.14.4).  The generational collector (3.12, 3.13, 3.14.5+) and, on 3.14.0-3.14.4, the
explicit young (``gc.collect(0)``) and full (``gc.collect(2)``) collections find garbage from
reference counts and never read a frame's stack.

So on those interpreters :func:`install` (run when ``mlaser`` is imported) switches automatic
collection off, and a daemon thread runs the collections the generational collector would: a
young collection once ``gc.get_threshold()[0]`` objects were allocated, a full one when the
objects that survived young collections since the last full one reach a quarter of those that
survived it (CPython's rule), never while a hold says no (:func:`add_full_hold`;
``stream.quiet_gc``: no full collection while a job streams).  On every other interpreter
nothing changes.  :func:`no_collections` replaces ``gc.disable()``/``gc.enable()`` around work
that must not be interrupted by collections, on every interpreter.

In a process that has loaded Qt the thread does not collect itself: it asks the main thread to
(``Py_AddPendingCall``: the collection runs between two bytecodes of the main thread, where an
automatic collection of the GUI thread's own allocations would).  A collection frees the Qt
objects in the garbage it finds on the thread it runs on, and Qt cannot stop a QObject's timers
from another thread: a QTimer in a reference cycle freed by the collector thread stayed in the
GUI thread's timer list, and the next ``processEvents`` crashed sending its timer event
(``QTimerInfoList::activateTimers``, ``sendThroughObjectEventFilters``; CI, Ubuntu's 3.14.4,
2026-11-12: a test worker down in about one run of three).  The garbage of a process whose
main thread runs no Python for a while waits until it does."""
from __future__ import annotations

import contextlib
import functools
import gc
import logging
import os
import sys
import threading
import time
from typing import Callable, Iterator

log = logging.getLogger(__name__)

#: how often the collector thread looks at the allocation count (s)
POLL_S = 0.005
#: young collections between two full ones at least (CPython's threshold1 * threshold2)
YOUNG_PER_FULL = 100

_lock = threading.Lock()
_on = [False]
_pause = [0]
_holds: list[Callable[[], bool]] = []
_counts = {"young": 0, "full": 0, "pending": 0, "total": 0, "since_full": 0}


@functools.cache
def incremental_collector() -> bool:
    """The interpreter's collector is the incremental one of CPython 3.14.0-3.14.4: it ignores
    the generation-2 threshold (``gc.get_threshold()[2]`` reads 0 and a set is ignored)."""
    th = gc.get_threshold()
    gc.set_threshold(th[0], th[1], th[2] + 1)
    try:
        return gc.get_threshold()[2] == th[2]
    finally:
        gc.set_threshold(*th)


def active() -> bool:
    """The collector thread runs this process's collections (automatic collection is off)."""
    return _on[0]


def add_full_hold(pred: Callable[[], bool]) -> None:
    """While ``pred()`` is true the collector thread runs no full collection."""
    _holds.append(pred)


def full_held() -> bool:
    """A hold (:func:`add_full_hold`) keeps the collector thread from full collections now."""
    return _on[0] and any(p() for p in _holds)


@contextlib.contextmanager
def no_collections() -> Iterator[None]:
    """No cyclic collection starts while the body runs (any thread; nested uses share it):
    ``gc.disable()`` on the generational collector, the collector thread paused here."""
    if _on[0]:
        with _lock:
            _pause[0] += 1
        try:
            yield
        finally:
            with _lock:
                _pause[0] -= 1
        return
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def collect_step() -> int:
    """One look of the collector thread: a young collection if enough objects were allocated,
    then a full one if CPython's rule asks for it and no hold says no.  Returns the generation
    collected (-1: none)."""
    if _pause[0]:
        return -1
    if gc.isenabled():                  # automatic collection must stay off on this interpreter
        gc.disable()
    th0 = gc.get_threshold()[0]
    n = gc.get_count()[0]
    if th0 <= 0 or n < th0:
        return -1
    found = gc.collect(0)
    c = _counts
    c["young"] += 1
    c["since_full"] += 1
    c["pending"] += max(0, n - found)
    if c["since_full"] < YOUNG_PER_FULL or c["pending"] <= c["total"] // 4 or full_held():
        return 0
    gc.collect(2)
    c["full"] += 1
    c["since_full"] = 0
    c["pending"] = 0
    c["total"] = len(gc.get_objects())
    return 2


def due() -> bool:
    """A young collection is due and nothing pauses the collector (what :func:`collect_step`
    looks at first)."""
    th0 = gc.get_threshold()[0]
    return not _pause[0] and th0 > 0 and gc.get_count()[0] >= th0


def qt_loaded() -> bool:
    """This process has loaded Qt: its collections must run on the main thread (the module
    documentation).  Never imports it."""
    return "PySide6.QtCore" in sys.modules


#: a collection the main thread was asked for and has not run yet (one at a time)
_asked = [False]
_pending_call: list = []


def _main_thread_step(_arg: object = None) -> int:
    """The pending call: one collector step on the main thread.  Always 0 (a pending call that
    returns -1 raises in whatever the main thread was running)."""
    _asked[0] = False
    try:
        collect_step()
    except Exception:  # noqa: BLE001 - must not reach the main thread's code
        log.exception("garbage collection step failed")
    return 0


def ask_main_thread() -> bool:
    """Ask the main thread for one collector step (any thread; at most one request waits).
    False: it could not be asked (no ``ctypes``, the interpreter's queue is full)."""
    if _asked[0]:
        return True
    try:
        if not _pending_call:
            import ctypes
            cb = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_main_thread_step)
            add = ctypes.pythonapi.Py_AddPendingCall
            add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            add.restype = ctypes.c_int
            _pending_call.extend((add, cb, ctypes.cast(cb, ctypes.c_void_p)))
        add, _cb, ptr = _pending_call
    except Exception:  # noqa: BLE001 - an interpreter without ctypes
        return False
    _asked[0] = True
    if add(ptr, None) != 0:
        _asked[0] = False
        return False
    return True


def tick() -> None:
    """One look of the collector thread: in a process with Qt the main thread is asked for the
    step when a collection is due, otherwise the step runs here."""
    if not qt_loaded() or threading.current_thread() is threading.main_thread():
        collect_step()
    elif due():
        ask_main_thread()


def _run() -> None:
    while True:
        time.sleep(POLL_S)
        try:
            tick()
        except Exception:  # noqa: BLE001 - the collector thread must not die
            log.exception("garbage collection step failed")


def _start_thread() -> None:
    _asked[0] = False                   # (a forked child: the parent's request is not its own)
    threading.Thread(target=_run, name="mlaser-gc", daemon=True).start()


def install() -> bool:
    """On CPython 3.14.0-3.14.4 (incremental collector): automatic collection off, the collector
    thread on (once per process; again in a forked child).  Returns :func:`active`."""
    with _lock:
        if _on[0] or not incremental_collector():
            return _on[0]
        gc.disable()
        _counts["total"] = len(gc.get_objects())
        _on[0] = True
    _start_thread()
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_start_thread)
    return True
