"""Interpreter scheduling helpers shared by the streamer (controller.stream) and the planner
(services.jobs); in no layer, so either may import it."""
from __future__ import annotations

import contextlib
import threading
from typing import Callable

_si_lock = threading.Lock()
_si_state: list = [0, 0.0]          # [depth, saved interval]
#: GIL switch interval while a job streams or a compile runs in a thread (CPython's default is
#: 5 ms): a thread that wakes up (the heartbeat, the relay loop, the GUI thread) waits at most
#: this long for a CPU-bound Python thread to hand over the interpreter (review 2 §1.3, P1-2)
RESPONSIVE_SWITCH_S = 0.0005


@contextlib.contextmanager
def responsive_gil(interval_s: float = RESPONSIVE_SWITCH_S):
    """Lower ``sys.setswitchinterval`` to ``interval_s`` for the duration (process-wide; nested
    and concurrent uses share it; the previous value comes back when the last one ends).

    With the default 5 ms a thread that needs the GIL waits up to 5 ms behind every CPU-bound
    thread at each hand-over, and under contention it often loses the hand-over: measured with
    ``tools/bench_stream.py --load 2`` (two CPU-bound threads), the parent's heartbeat thread
    went up to 544 ms without running at 5 ms, 58 ms at 0.5 ms."""
    import sys
    with _si_lock:
        if _si_state[0] == 0:
            _si_state[1] = sys.getswitchinterval()
            sys.setswitchinterval(min(_si_state[1], interval_s))
        _si_state[0] += 1
    try:
        yield
    finally:
        with _si_lock:
            _si_state[0] -= 1
            if _si_state[0] == 0:
                sys.setswitchinterval(_si_state[1])


def responsive_holders() -> int:
    """How many :func:`responsive_gil` contexts are open now (any thread): while one is, the
    interval stays lowered, whoever opened the first."""
    with _si_lock:
        return int(_si_state[0])


def start_now(fn: Callable[[], None], name: str) -> None:
    """Run ``fn`` in a new daemon thread named ``name`` without waiting for it to start.

    ``threading.Thread.start`` blocks until the new thread has run far enough to signal that it
    started, and that needs the GIL: with CPU-bound threads in the process (a streaming job,
    a compile) the caller waited 10-60 ms (measured: median 11 ms, p99 56 ms with three busy
    threads; Windows CI: Stop blocked the GUI thread 58 ms).  ``_thread.start_new_thread``
    returns once the OS thread exists (~0.04 ms).  An exception ``fn`` lets out goes to
    ``threading.excepthook`` as for any thread (the application's hook halts and latches)."""
    import _thread
    import sys

    def body() -> None:
        try:
            threading.current_thread().name = name      # machine-thread checks go by the name
        except Exception:  # noqa: BLE001 - only a label
            pass
        try:
            fn()
        except BaseException:  # noqa: BLE001 - reported like a threading.Thread's
            et, ev, tb = sys.exc_info()
            threading.excepthook(threading.ExceptHookArgs((et, ev, tb, threading.current_thread())))
        finally:
            # the stand-in Thread object threading made for it (3.12 kept it listed after the
            # thread ended: threading.enumerate() showed a dead "mlaser-stop")
            try:
                with threading._active_limbo_lock:          # type: ignore[attr-defined]
                    threading._active.pop(threading.get_ident(), None)   # type: ignore[attr-defined]
            except AttributeError:
                pass

    _thread.start_new_thread(body, ())
