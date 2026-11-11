"""The unit seam under the job runner (docs/dev/TESTING.md, "A seam under the job runner").

``seam_ctl`` makes a :class:`UiController` on a :class:`~mlaser.controller.clock.ManualClock`
connected to the card simulator through :mod:`mlaser.controller.memlink`: no socket, no
simulator thread, no poll or monitor thread.  The simulator's time moves only when the
controller sleeps on the clock (the streamer's poll interval, a receive timeout) or when the
test advances it, so a job of seconds runs in milliseconds and every run takes the same steps.
The safety monitor's link and heartbeat ticks follow the clock.

Synchronisation is by events, never by wall-clock time: ``ctl.wait_job()`` (the worker
thread's end), the Event returned by ``ctl.stop()``, and the clock's gate (``clock.hold_after``
/ ``clock.wait_parked`` / ``clock.release``) to act "in the middle of a job" at a known point
of it.  The threaded tests on the UDP simulator stay the acceptance set (real-time deadlines).
"""
from __future__ import annotations

import pytest

from mlaser.controller.clock import ManualClock
from mlaser.controller.ui_adapter import UiController

from safety_helpers import rect_doc  # noqa: F401 - re-exported for the seam tests


def make_seam_ctl(arm: bool = True, *, pessimistic: bool = True, controller_kw: dict | None = None,
                  **sim_kw) -> tuple[UiController, ManualClock]:
    """An (armed) controller on the in-memory simulator, and its clock."""
    clock = ManualClock()
    ctl = UiController(clock=clock, **(controller_kw or {}))
    assert ctl.connect_simulator(link="memory", pessimistic=pessimistic, **sim_kw), ctl.connect_error
    if arm:
        assert ctl.arm()
    return ctl, clock


@pytest.fixture
def seam_ctl():
    """Factory of :func:`make_seam_ctl`; every controller is disconnected at the end."""
    made: list[tuple[UiController, ManualClock]] = []

    def make(arm: bool = True, **kw):
        pair = make_seam_ctl(arm, **kw)
        made.append(pair)
        return pair

    yield make
    for ctl, clock in made:
        clock.release()
        try:
            ctl.disconnect()
        except Exception:  # noqa: BLE001
            pass


def cutting(ctl) -> bool:
    st = ctl.sim.state
    return bool(st.fifo_running and st.laser)


def mid_job(ctl, clock, doc, after_s: float = 0.2, step_s: float = 0.05, max_s: float = 30.0,
            until=cutting) -> None:
    """Start ``doc`` and hold the job's time ``after_s`` (card time) after ``until(ctl)``
    first held (default: the card cuts with the beam on): on return the worker waits at the
    clock's gate.  ``clock.release()`` lets it go on."""
    clock.hold_after(step_s)
    ctl.start(doc)
    assert clock.wait_parked(), "the job did not reach the gate"
    t = 0.0
    while not until(ctl):
        t += step_s
        assert t < max_s and clock.run_to_gate(step_s), "the job did not get there"
    if after_s > 0:
        assert clock.run_to_gate(after_s), "the job ended before the gate"


def settle(ctl, clock, seconds: float = 0.5) -> None:
    """Let the card come to rest (the pessimistic halt lag runs out): card time only."""
    clock.advance(seconds)
