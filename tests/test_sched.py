"""mlaser._sched: start_now (a thread start the caller does not wait for)."""
from __future__ import annotations

import threading
import time

from mlaser._sched import start_now


def test_start_now_runs_named_and_leaves_no_thread_behind():
    seen = []
    done = threading.Event()

    def fn():
        seen.append(threading.current_thread().name)
        done.set()
    start_now(fn, "mlaser-stop")
    assert done.wait(5) and seen == ["mlaser-stop"]
    time.sleep(0.1)
    assert "mlaser-stop" not in [t.name for t in threading.enumerate()]


def test_start_now_reports_an_escaped_exception_to_threading_excepthook(monkeypatch):
    got = []
    done = threading.Event()
    monkeypatch.setattr(threading, "excepthook", lambda a: (got.append((a.exc_type, a.thread.name)), done.set()))

    def bad():
        raise RuntimeError("boom")
    start_now(bad, "mlaser-stop")
    assert done.wait(5) and got == [(RuntimeError, "mlaser-stop")]


def test_start_now_does_not_wait_for_the_thread(monkeypatch):
    """threading.Thread.start waits until the new thread runs (10-60 ms with busy threads)."""
    monkeypatch.setattr(threading.Thread, "start", lambda self: time.sleep(0.5))
    gate = threading.Event()
    t0 = time.monotonic()
    start_now(gate.wait, "x")
    assert time.monotonic() - t0 < 0.1
    gate.set()
