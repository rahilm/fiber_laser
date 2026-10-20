"""Per-chain parallel planning for :func:`mlaser.nc.compiler.compile_items` (Review 2 P2-5).

Each cut chain's smoothing, velocity plan and sampling (``compiler.plan_cut_chain``) is a pure
function of its moves, the motion configuration, the lead-in index and the servo period, and so
is the plan of each ``Job.estimate`` chain (``interp.plan_path``).  This module runs them in a
pool of worker processes, one task per estimate chain (its compile chains and its own estimate
plan, inside one blend-table memo, as the serial compile does it); the emitter (the pulse carry
and the card position chain from one sample to the next), the rapids, the process items and the
packing stay sequential in the calling process and consume the results in order.  The output is
bit for bit the serial one: the same functions run on the same inputs in the same interpreter
and build, under the caller's parity profile and kernel selection (``tests/test_compile_parallel
.py``, ``tools/native_parity.py --workers``).

Configuration (:func:`configure` overrides the environment until ``configure(None)``):

* ``MLASER_COMPILE_WORKERS``: ``0`` / ``off`` = always serial; ``N`` = a pool of N processes for
  every job of at least two chains; unset / ``auto`` = ``min(8, cpu_count - 1)`` processes for jobs
  of at least :data:`AUTO_MIN_CHAINS` cut chains (smaller jobs plan faster than a pool hands
  them over), serial otherwise or on a single-core machine;
* ``MLASER_COMPILE_MIN_CHAINS``: the auto threshold.

Never inside a multiprocessing child (a worker, the streamer process).  Auto mode also stays
serial when the program's main script would run again in every spawned worker (no
``if __name__ == "__main__":`` guard, :func:`_main_is_safe`).

The pool is created on first use (``spawn``, like the streamer process: safe next to Qt
threads, the same on every OS; the first parallel compile of a process pays the start-up of the
workers, about 0.3-1 s), kept for the process lifetime and shut down at exit; its workers run at
a lower scheduling priority (nice 10 / below normal).  Any failure of the
pool (a worker that dies, a task that cannot be pickled, a profile the workers cannot select)
makes that compile finish serially from where it was; :data:`STATS` counts what ran where.
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any, Optional

ENV_WORKERS = "MLASER_COMPILE_WORKERS"
ENV_MIN = "MLASER_COMPILE_MIN_CHAINS"
#: auto mode: the pool is used for jobs with at least this many cut chains (measured: a warm
#: pool wins from about 250 chains, a cold one, which also starts the workers, from about 600;
#: docs/dev/PERFORMANCE.md)
AUTO_MIN_CHAINS = 600
#: auto mode: at most this many worker processes
AUTO_MAX_WORKERS = 8
#: tasks in flight per worker (bounds the results waiting in memory)
WINDOW_PER_WORKER = 4

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_pool: Optional[ProcessPoolExecutor] = None
_pool_workers = 0
_override: Optional[tuple] = None

#: groups planned in the pool / serially after a pool failure, and pool failures
STATS: dict[str, int] = {"parallel_jobs": 0, "pool_groups": 0, "serial_groups": 0, "pool_failures": 0}


def configure(workers: Any = None, min_chains: Optional[int] = None) -> None:
    """Override ``MLASER_COMPILE_WORKERS`` (``0`` / ``"off"``, ``N``, ``"auto"``) and the auto
    threshold for the next compiles; ``configure(None)`` follows the environment again."""
    global _override
    _override = None if workers is None and min_chains is None else (workers, min_chains)


def _setting() -> tuple[str, int]:
    """(mode, value): ("off", 0), ("fixed", N) or ("auto", min chains)."""
    raw: Any
    if _override is not None:
        raw, mn = _override
    else:
        raw, mn = os.environ.get(ENV_WORKERS), None
    if mn is None:
        try:
            mn = int(os.environ.get(ENV_MIN, AUTO_MIN_CHAINS))
        except ValueError:
            mn = AUTO_MIN_CHAINS
    s = "" if raw is None else str(raw).strip().lower()
    if s in ("", "auto"):
        return "auto", int(mn)
    if s in ("0", "off", "no", "false", "1"):
        return "off", 0
    try:
        n = int(s)
    except ValueError:
        _log.warning("%s=%r is not 0 / off, a number or auto: using auto", ENV_WORKERS, raw)
        return "auto", int(mn)
    return ("fixed", n) if n > 1 else ("off", 0)


def _in_worker() -> bool:
    """A multiprocessing child (a pool worker, the streamer process): never a pool of its own."""
    import multiprocessing as mp
    return mp.parent_process() is not None


def _main_is_safe() -> bool:
    """Can ``spawn`` workers start without re-running the program?  A spawned child imports
    the parent's main script (as ``__mp_main__``): one without an ``if __name__ ==
    "__main__":`` guard would run again in every worker.  True for a frozen program (its entry
    point calls ``multiprocessing.freeze_support()``, mlaser.ui.__main__), an interactive or
    ``-c`` session (no main file), and a main file that has the guard.  Auto mode only."""
    if getattr(sys, "frozen", False):
        return True
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return False
    return "__name__" in src and "__main__" in src.replace("__name__", "")


def workers_for(chains: int) -> int:
    """Worker processes a compile of ``chains`` cut chains uses (0 = serial)."""
    mode, v = _setting()
    if mode == "off" or chains < 2 or _in_worker():
        return 0
    if mode == "fixed":
        return v
    cpus = os.cpu_count() or 1
    if cpus < 2 or chains < v or not _main_is_safe():
        return 0
    return max(2, min(AUTO_MAX_WORKERS, cpus - 1))


def _get_pool(n: int) -> ProcessPoolExecutor:
    global _pool, _pool_workers
    with _lock:
        if _pool is None or _pool_workers != n:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            import multiprocessing as mp
            _pool = ProcessPoolExecutor(n, mp_context=mp.get_context("spawn"), initializer=_worker_init)
            _pool_workers = n
        return _pool


def shutdown() -> None:
    """Stop the pool (also at interpreter exit)."""
    global _pool, _pool_workers
    with _lock:
        p, _pool, _pool_workers = _pool, None, 0
    if p is not None:
        p.shutdown(wait=True, cancel_futures=True)


atexit.register(shutdown)


# --------------------------------------------------------------------------- worker side
_worker_setup: Optional[tuple] = None


def _worker_init() -> None:
    """Pool workers run at a lower priority than the program (the GUI and the streamer process
    keep their CPU time while a big job is planned ahead)."""
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        elif sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:  # noqa: BLE001 - the priority is a courtesy
        pass


def _setup(profile: str, native: str) -> None:
    """Select the caller's parity profile and kernel mode in this worker (once per change)."""
    global _worker_setup
    key = (profile, native)
    if _worker_setup == key:
        return
    from ..parity_ports import set_profile
    from ..parity_ports.profiles import native as N
    N.configure(native)
    set_profile(profile)
    _worker_setup = key


def plan_group(task: tuple) -> tuple:
    """Worker: plan the compile chains of one estimate chain and the estimate chain itself.

    ``task`` = (profile name, kernel mode, MotionConfig, period, [(moves, lead index), ...],
    estimate chain moves).  Returns ``(results, estimate)``: per compile chain
    ``(ss, pts, total, gaps)`` (None from the first chain that raised: the caller plans that one
    itself and so raises the serial exception), and ``(duration, length)`` of the estimate chain,
    ``"error"`` when planning it raised, or None when a compile chain failed."""
    profile, native, cfg, period, chains, est = task
    _setup(profile, native)
    from ..motion import interp
    from ..parity_ports.cadmodule.smooth import table_memo
    from .compiler import plan_cut_chain
    out: list = []
    last: Any = None
    with table_memo():
        for moves, li in chains:
            try:
                plan, ss, pts, total, gaps = plan_cut_chain(moves, cfg, li, period)
            except Exception:  # noqa: BLE001 - re-raised by the caller's own planning
                out.append(None)
                return out, None
            out.append((ss, pts, total, gaps))
            last = (moves, li, plan)
        try:
            moves, li, plan = last
            if (len(chains) == 1 and li is None and len(moves) == len(est)
                    and all(a is b for a, b in zip(moves, est))):
                ev: Any = (plan.duration, plan.length)
            else:
                p = interp.plan_path(est, cfg)
                ev = (p.duration, p.length)
        except Exception:  # noqa: BLE001 - as estimate_time raising: no estimate
            ev = "error"
    return out, ev


# --------------------------------------------------------------------------- caller side
class PoolPlans:
    """The results of the groups of one compile, in order, from the pool (a bounded window of
    tasks in flight); ``None`` for a group once the pool failed (the caller plans it)."""

    def __init__(self, tasks: list, workers: int) -> None:
        self.tasks = tasks
        self.workers = workers
        self.window = max(2, workers * WINDOW_PER_WORKER)
        self.pending: deque[Future] = deque()
        self.next_submit = 0
        self.failed = False
        try:
            self.pool: Optional[ProcessPoolExecutor] = _get_pool(workers)
        except Exception as e:  # noqa: BLE001 - no pool: serial
            self._fail(e)
            self.pool = None
        STATS["parallel_jobs"] += 1
        self._fill()

    def _fail(self, e: BaseException) -> None:
        if not self.failed:
            STATS["pool_failures"] += 1
            _log.warning("parallel compile: the planning pool failed (%s: %s); planning serially",
                         type(e).__name__, e)
        self.failed = True
        for f in self.pending:
            f.cancel()
        self.pending.clear()

    def _fill(self) -> None:
        while not self.failed and self.pool is not None and len(self.pending) < self.window \
                and self.next_submit < len(self.tasks):
            try:
                self.pending.append(self.pool.submit(plan_group, self.tasks[self.next_submit]))
                self.tasks[self.next_submit] = None      # pickled: not needed here any more
            except Exception as e:  # noqa: BLE001 - a broken pool / unpicklable task
                self._fail(e)
                return
            self.next_submit += 1

    def get(self, gi: int) -> Optional[tuple]:
        """Result of group ``gi`` (called with 0, 1, 2, ... in order), or None."""
        if self.failed:
            STATS["serial_groups"] += 1
            return None
        f = self.pending.popleft()
        try:
            r = f.result()
        except Exception as e:  # noqa: BLE001 - BrokenProcessPool, a pickling error, ...
            self._fail(e)
            STATS["serial_groups"] += 1
            return None
        self._fill()
        STATS["pool_groups"] += 1
        return r

    def close(self) -> None:
        for f in self.pending:
            f.cancel()
        self.pending.clear()
