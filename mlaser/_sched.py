"""Interpreter scheduling helpers shared by the streamer (controller.stream) and the planner
(services.jobs); in no layer, so either may import it."""
from __future__ import annotations

import contextlib
import threading

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
