"""The controller's ``MachineState`` is written under ``_slock`` and the GUI reads a snapshot
(review A6).  The status poll writes it on its own thread; the writers named in the review
(pause, jog, outputs, gas type, laser power, the kept resume point) skipped the lock, and the
GUI's status tick read the live object, so it could see fields from two different polls.
"""
from __future__ import annotations

import threading
import time

import pytest

from mlaser.machine.api import MachineState

pytestmark = pytest.mark.safety


class _Watch:
    """Records writes to the state made while ``lock`` was not held."""

    def __init__(self, lock) -> None:
        self.lock = lock
        self.bad: list[str] = []


def _checked_state(ctl) -> _Watch:
    watch = _Watch(ctl._slock)

    class Outputs(dict):
        def __setitem__(self, k, v):
            if not watch.lock.locked():
                watch.bad.append(f"outputs[{k!r}]")
            super().__setitem__(k, v)

    class Checked(MachineState):
        def __setattr__(self, k, v):
            if k != "outputs" and getattr(self, "_armed_check", False) and not watch.lock.locked():
                watch.bad.append(k)
            object.__setattr__(self, k, v)

    with ctl._slock:
        old = ctl._s
        s = Checked(**{f: getattr(old, f) for f in old.__dataclass_fields__})
        s.outputs = Outputs(old.outputs)
        object.__setattr__(s, "_armed_check", True)
        ctl._s = s
    return watch


def test_writers_hold_the_state_lock(make_ctl):
    ctl = make_ctl(arm=False)
    watch = _checked_state(ctl)
    ctl.set_gas_type(2)
    ctl.set_laser_power(30.0)
    ctl.set_output("red_light", True)
    ctl.set_output("red_light", False)
    ctl.jog_start("X", 1)
    ctl.jog_stop("X")
    assert ctl.wait_commands(5.0)
    ctl.state()
    ctl.snapshot()
    assert not watch.bad, f"state written without _slock: {watch.bad}"


def test_snapshot_is_a_consistent_independent_copy(make_ctl):
    ctl = make_ctl(arm=False)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            with ctl._slock:
                ctl._s.x = float(i)
                ctl._s.y = float(i)
                ctl._s.outputs["gas"] = bool(i & 1)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 0.3
        n = 0
        while time.monotonic() < end:
            s = ctl.snapshot()
            assert s.x == s.y
            assert s is not ctl._s and s.outputs is not ctl._s.outputs
            n += 1
        assert n > 10
    finally:
        stop.set()
        t.join(2.0)
    s = ctl.snapshot()
    s.outputs["laser"] = True                   # a copy: the controller's state is untouched
    assert ctl._s.outputs["laser"] is False
    assert s.safety == ctl.fsm.state.value and s.link_ok == ctl.state().link_ok


def test_gui_status_tick_reads_a_snapshot():
    import inspect

    from mlaser.ui.mainwindow import MainWindow
    src = inspect.getsource(MainWindow._poll)
    assert "self.ctl.snapshot()" in src and "self.ctl.state()" not in src
