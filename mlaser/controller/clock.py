"""The time source of the job runner, the streamer, the card client and the supervisor's link
watch (docs/dev/TESTING.md, "A seam under the job runner").

Production code uses :data:`SYSTEM` (``time.monotonic`` / ``time.sleep``).  A test passes a
:class:`ManualClock` to :class:`~mlaser.controller.ui_adapter.UiController` (``clock=``) and
connects with ``connect_simulator(link="memory")``: the card simulator then answers in the
calling thread (:mod:`mlaser.controller.memlink`), runs no thread of its own, and its time
advances only when the controller's code sleeps on this clock (the streamer's poll interval, a
receive timeout) or when the test calls :meth:`ManualClock.advance`.  A job of minutes runs in
milliseconds and the same steps happen in the same order on every run.

On a virtual clock the controller starts no status poller and no monitor thread: the safety
monitor's link and heartbeat ticks run as a listener of the clock (:meth:`ManualClock.
add_listener`), in the thread that moved the time, when its deadlines pass; the status is read
by the job's streamer, the fault reset and Clear Alarm, or by the test (``ctl._poll_once()``).
What stays on the wall clock (not part of the job's timeline): the command worker, thread
joins, the interlock rate limit and the jog dead-man.
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class SystemClock:
    """The wall clock (the default everywhere)."""

    virtual = False

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


SYSTEM = SystemClock()


class ManualClock:
    """Virtual time for tests: :meth:`sleep` and :meth:`advance` move it forward and step an
    attached :class:`~mlaser.controller.simulator.CardSimulator` (``advance(dt)``) by the same
    amount, in slices of at most ``slice_s`` (the simulator's motion loop steps 2 ms).

    :meth:`hold_at` parks every sleeper that would pass a given time until :meth:`release`: a
    test stops "in the middle of a job" at a known point of the job, not after a wall-clock
    sleep that may or may not have been long enough."""

    virtual = True

    def __init__(self, start: float = 1000.0, slice_s: float = 0.002):
        self._t = float(start)
        self.slice_s = float(slice_s)
        self._sim = None
        self._cv = threading.Condition(threading.RLock())
        self._gate: Optional[float] = None
        self.parked = 0                     # threads waiting at the gate now
        self._parks = 0                     # arrivals at a gate so far
        self._listeners: list = []
        self._in_listener = threading.local()

    def attach(self, sim) -> None:
        """Step ``sim`` with this clock (``connect_simulator(link="memory")`` calls it)."""
        self._sim = sim

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)
        time.sleep(0)                       # let the other threads run, as a real sleep would

    def add_listener(self, fn) -> None:
        """``fn(now)`` after every :meth:`advance` (the supervisor's virtual monitor), in the
        advancing thread, outside the clock's lock; never re-entered from inside itself."""
        with self._cv:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._cv:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def advance(self, seconds: float) -> None:
        """Move the time forward by ``seconds`` (stepping the simulator); waits at the gate."""
        self._advance(seconds)
        if getattr(self._in_listener, "on", False):
            return
        self._in_listener.on = True
        try:
            for fn in list(self._listeners):
                fn(self._t)
        finally:
            self._in_listener.on = False

    def _advance(self, seconds: float) -> None:
        left = float(seconds)
        with self._cv:
            while left > 0.0:
                if self._gate is not None and self._t + left > self._gate:
                    step = max(0.0, self._gate - self._t)
                    self._step(step)
                    left -= step
                    self.parked += 1
                    self._parks += 1
                    self._cv.notify_all()
                    try:
                        while self._gate is not None and self._t >= self._gate:
                            self._cv.wait(0.05)
                    finally:
                        self.parked -= 1
                    continue
                self._step(left)
                left = 0.0

    def _step(self, dt: float) -> None:
        sim = self._sim
        while dt > 1e-12:
            d = min(dt, self.slice_s)
            if sim is not None:
                with sim.lock:
                    sim.advance(d)
            self._t += d
            dt -= d

    # ------------------------------------------------------------------ the gate
    def hold_at(self, t: float) -> None:
        """Park every thread whose sleep would pass ``t`` (absolute) until :meth:`release`."""
        with self._cv:
            self._gate = float(t)
            self._cv.notify_all()

    def hold_after(self, seconds: float) -> None:
        """:meth:`hold_at` ``seconds`` from now."""
        self.hold_at(self._t + float(seconds))

    def release(self) -> None:
        with self._cv:
            self._gate = None
            self._cv.notify_all()

    def wait_parked(self, timeout: float = 10.0) -> bool:
        """Block (wall clock) until a thread waits at the gate; False on timeout."""
        return self._wait_parks(lambda: self.parked > 0, timeout)

    def run_to_gate(self, seconds: float, timeout: float = 10.0) -> bool:
        """Move the gate ``seconds`` later and block until a thread arrives at it (the time
        then stands at the new gate); False when nobody did (the job ended meanwhile)."""
        with self._cv:
            n = self._parks
            self._gate = self._t + float(seconds)
            self._cv.notify_all()
        return self._wait_parks(lambda: self._parks > n, timeout)

    def _wait_parks(self, pred, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while not pred():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True


__all__ = ["SYSTEM", "SystemClock", "ManualClock"]
