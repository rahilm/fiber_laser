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
that must not be interrupted by collections, on every interpreter."""
from __future__ import annotations

import contextlib
import functools
import gc
import logging
import os
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


def _run() -> None:
    while True:
        time.sleep(POLL_S)
        try:
            collect_step()
        except Exception:  # noqa: BLE001 - the collector thread must not die
            log.exception("garbage collection step failed")


def _start_thread() -> None:
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
