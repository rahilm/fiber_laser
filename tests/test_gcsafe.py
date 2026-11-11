"""Cyclic garbage collection on CPython 3.14.0-3.14.4 (mlaser._gcsafe).

The incremental collector of those versions marks the objects on every thread's frame stacks at
each automatic collection; while the GUI thread's ``item.setBrush(QBrush(c))`` drops its
temporary argument, shiboken's deallocator releases the GIL, and a collection that another
thread starts then reads the dying object (CI, Ubuntu's 3.14.4: a segmentation fault in the plan
thread, "Garbage-collecting").  mlaser switches those automatic collections off and collects
from a thread with young and full collections, which never read a frame's stack.  On the
generational collector (3.12, 3.13, 3.14.5+) nothing changes; these tests run on all of them."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from mlaser import _gcsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Cyc:
    __slots__ = ("me", "pad")

    def __init__(self) -> None:
        self.me = self
        self.pad = None


def _alive() -> int:
    return sum(1 for o in gc.get_objects() if type(o) is _Cyc)


def test_no_automatic_collection_is_an_increment_of_the_incremental_collector():
    """A thread that allocates cyclic garbage gets it collected, and on 3.14.0-3.14.4 never by
    an increment of the incremental collector (``mark_stacks``; reported as generation 1)."""
    gens: list[int] = []

    def cb(phase, info):
        if phase == "start":
            gens.append(info["generation"])

    def work():
        for _ in range(400_000):
            _Cyc()

    gc.callbacks.append(cb)
    try:
        th = threading.Thread(target=work, name="gcsafe-alloc")
        th.start()
        th.join()
        deadline = time.monotonic() + 5.0          # the collector thread polls every 5 ms
        while gc.get_count()[0] >= gc.get_threshold()[0] > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        gc.callbacks.remove(cb)
    assert gens, "no collection ran while a thread allocated cyclic garbage"
    if _gcsafe.incremental_collector():
        assert _gcsafe.active() and not gc.isenabled()
        assert 1 not in gens, "an increment of the incremental collector ran (stack scan)"
    assert _alive() < 50_000, "the cyclic garbage was not collected"


def test_old_cyclic_garbage_is_collected_without_an_explicit_collection():
    """Garbage that survived young collections (it was alive for a while) is reclaimed by the
    full collections, as the generational collector does: memory does not grow without bound."""
    gc.collect()
    base = _alive()
    keep: list[list] = []
    deadline = time.monotonic() + 20.0
    rounds = 0
    while time.monotonic() < deadline:
        keep.append([_Cyc() for _ in range(20_000)])   # lives through some young collections
        if len(keep) > 5:
            keep.pop(0)                               # ... then dies as old garbage
        rounds += 1
        time.sleep(0.002)
        if rounds >= 60 and _alive() - base < 8 * 20_000:
            break
    keep.clear()
    assert rounds >= 60
    assert _alive() - base < 8 * 20_000, (rounds, _alive() - base)


def test_no_collections_pauses_them_and_restores():
    was = gc.isenabled()
    with _gcsafe.no_collections():
        with _gcsafe.no_collections():
            assert not gc.isenabled()
            n = gc.get_stats()
            for _ in range(50_000):
                _Cyc()
            time.sleep(0.05)
            if _gcsafe.active():
                assert gc.get_stats() == n          # the collector thread waits
    assert gc.isenabled() is was


_PROVOKE = textwrap.dedent("""
    import os, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import mlaser  # noqa: F401
    from dataclasses import dataclass
    from PySide6.QtGui import QBrush, QColor, QPen
    from PySide6.QtWidgets import QApplication, QGraphicsRectItem
    app = QApplication.instance() or QApplication([])

    @dataclass
    class P:
        a: object
        b: object

    stop = time.monotonic() + float(sys.argv[2])

    def work():
        keep = []
        while time.monotonic() < stop:
            for i in range(500):
                p = P(i, None)
                p.b = p
                keep.append(p)
            if len(keep) > 20000:
                keep = []

    t = threading.Thread(target=work)
    t.start()
    item = QGraphicsRectItem(0, 0, 1, 1)
    c = QColor(1, 2, 3)
    while time.monotonic() < stop:
        item.setBrush(QBrush(c))       # the temporary's deallocator releases the GIL
        item.setPen(QPen(c))
    t.join()
    print("survived")
""")


def test_a_worker_thread_collection_survives_gil_releasing_deallocators_on_the_gui_thread():
    """The CI crash, provoked: the GUI thread hands temporary PySide6 value objects to setters
    (their deallocator releases the GIL), a worker thread allocates cyclic garbage meanwhile.
    Before the fix, CPython 3.14.4 crashed in 2 of 3 runs of 10 s (segmentation fault in
    ``mark_stacks``); the generational collector never crashed."""
    pytest.importorskip("PySide6")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONFAULTHANDLER="1")
    r = subprocess.run([sys.executable, "-c", _PROVOKE, REPO, "8"], capture_output=True,
                       text=True, timeout=120, env=env)
    assert r.returncode == 0 and "survived" in r.stdout, (r.returncode, r.stderr[-3000:])
