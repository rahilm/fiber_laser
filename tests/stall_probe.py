"""What the GUI thread was doing while a "the GUI stays live" test's heartbeat was late.

The tests that bound the GUI thread's longest pause (an autosave, a Save, the import review in a
child) only measure the pause; on a runner where it fails nobody can see why.  A
:class:`StallProbe` watches from its own thread: the measured loop calls :meth:`beat`, and while
the last beat is older than ``late_s`` the watcher samples the main thread's stack (and the other
threads') with ``sys._current_frames()``.  It also records the garbage collections that took
longer than ``late_s`` (with the thread that ran them) and how late the watcher itself woke: when
another thread holds the interpreter in a C call, or the whole process is not scheduled, the
watcher cannot run either, and ``process_time`` over that wake-up tells the two apart (busy CPU =
a thread held the interpreter; idle = the process waited or was not scheduled).

Diagnostics only: :meth:`report` goes into the test's failure message, no bound changes::

    with StallProbe() as probe:
        ...loop that calls probe.beat()...
    assert gap < 0.2, f"stalled {gap}\\n{probe.report()}"
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import time
from collections import Counter


def _stack(frame, depth: int = 10) -> str:
    """The innermost ``depth`` frames, outermost first, one ``file:line func`` per line.  Read
    from the frames' code objects only (no source lines: ``traceback`` reads files through
    linecache, which lets the sampled thread run on while its stack is walked)."""
    items = []
    while frame is not None and len(items) < depth:
        code = frame.f_code
        items.append(f"      {os.path.basename(code.co_filename)}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
    return "\n".join(reversed(items))


class StallProbe:
    def __init__(self, late_s: float = 0.05, period_s: float = 0.005, top: int = 3) -> None:
        self.late_s = late_s
        self.period_s = period_s
        self.top = top
        self._main = threading.main_thread().ident
        self._last = time.perf_counter()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.main_stacks: Counter = Counter()
        self.other_stacks: Counter = Counter()
        self.samples = 0
        self.worst_late = 0.0
        self.worst_wake: tuple[float, float] = (0.0, 0.0)     # (watcher's late wake-up, CPU in it)
        self.collections: list[tuple[int, str, float]] = []   # (generation, thread, seconds)
        self._gc_start: dict[int, float] = {}

    # the measured loop's heartbeat
    def beat(self) -> None:
        self._last = time.perf_counter()

    def _gc_cb(self, phase: str, info: dict) -> None:
        tid = threading.get_ident()
        if phase == "start":
            self._gc_start[tid] = time.perf_counter()
        else:
            t0 = self._gc_start.pop(tid, None)
            if t0 is not None and time.perf_counter() - t0 >= self.late_s:
                self.collections.append((info.get("generation", -1), threading.current_thread().name,
                                         time.perf_counter() - t0))

    def _watch(self) -> None:
        prev = time.perf_counter()
        prev_cpu = time.process_time()
        while not self._stop.wait(self.period_s):
            now = time.perf_counter()
            cpu = time.process_time()
            wake = now - prev - self.period_s
            if wake > self.worst_wake[0]:
                self.worst_wake = (wake, cpu - prev_cpu)
            prev, prev_cpu = now, cpu
            late = now - self._last
            if late < self.late_s:
                continue
            self.worst_late = max(self.worst_late, late)
            self.samples += 1
            names = {t.ident: t.name for t in threading.enumerate()}
            frames, frame = sys._current_frames(), None
            for tid, frame in frames.items():
                if tid == threading.get_ident():
                    continue
                if tid == self._main:
                    self.main_stacks[_stack(frame)] += 1
                else:
                    self.other_stacks[f"    [{names.get(tid, tid)}]\n" + _stack(frame, 6)] += 1
            del frames, frame                             # no thread's frames kept alive here

    def __enter__(self) -> StallProbe:
        self._last = time.perf_counter()
        gc.callbacks.append(self._gc_cb)
        self._thread = threading.Thread(target=self._watch, name="stall-probe", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        try:
            gc.callbacks.remove(self._gc_cb)
        except ValueError:
            pass

    def report(self) -> str:
        lines = [f"stall probe: {self.samples} samples while the heartbeat was >= "
                 f"{self.late_s * 1000:.0f} ms late (worst {self.worst_late * 1000:.0f} ms); the "
                 f"watcher's latest wake-up {self.worst_wake[0] * 1000:.0f} ms late with "
                 f"{self.worst_wake[1] * 1000:.0f} ms process CPU in it; switch interval "
                 f"{sys.getswitchinterval() * 1000:g} ms"]
        if self.collections:
            lines.append("  slow garbage collections (generation, thread, ms): " + ", ".join(
                f"({g}, {n}, {s * 1000:.0f})" for g, n, s in self.collections))
        for title, stacks in (("main thread", self.main_stacks), ("other threads", self.other_stacks)):
            for stack, n in stacks.most_common(self.top):
                lines.append(f"  {title}, {n} samples:\n{stack}")
        return "\n".join(lines)
