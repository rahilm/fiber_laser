"""The None checks the type checker asked for in the control core (review arch A7 / fw F6).

With ``mlaser/controller``, ``mlaser/machine`` and ``mlaser/devices`` under mypy, every use of a
channel that can be None (the card's main channel while disconnected, a device that is not open)
got a real check.  Where the old code went on with None it raised AttributeError somewhere
unexpected; these tests pin what happens now.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mlaser.controller.ui_adapter import UiController
from mlaser.machine.safety import SafetyState

pytestmark = pytest.mark.safety


def test_pendant_reader_reports_a_device_that_is_not_open():
    """The reader of a pendant that is not open (open() failed, start() called anyway) used to
    die on an AttributeError without telling anyone; now it is a read error: ``on_error`` runs
    (the jog mapper stops any jog) and the reader keeps trying."""
    from mlaser.devices import pendant as P
    pend = P.Pendant()
    errors: list[Exception] = []
    seen = threading.Event()

    def on_error(e: Exception) -> None:
        errors.append(e)
        seen.set()
    pend.on_error = on_error
    pend.start()
    try:
        assert seen.wait(2.0), "the reader did not report the closed device"
        assert isinstance(errors[0], OSError)
        assert pend._thread is not None and pend._thread.is_alive()
    finally:
        pend.close()


def test_laser_client_without_a_command_set_refuses_emission():
    from mlaser.devices.laser import LaserClient, LaserError
    from mlaser.devices.transport import LoopbackTransport
    c = LaserClient(LoopbackTransport(lambda b: b"ok"))
    with pytest.raises(LaserError):
        c.emission(False)


def test_jog_stop_without_a_main_channel_sends_the_halt():
    """The dead-man's stop of a jog with the main channel gone: the halt channel, as for a
    failed stop (it used to raise AttributeError before anything went out)."""
    ctl = UiController()
    halts: list[str] = []
    ctl._sup.safe_halt = lambda reason, *a, **kw: halts.append(reason)  # type: ignore[method-assign]
    ctl._vm_stop_jog()
    assert halts == ["stop (fallback, main channel failed)"]


def test_job_start_refused_before_the_fsm_moves_without_a_main_channel(make_ctl, monkeypatch):
    ctl = make_ctl(arm=False)
    monkeypatch.setattr(UiController, "_mc", property(lambda self: None))
    job = SimpleNamespace(wire=[], markers=[])
    assert ctl._start_worker(job, "process", "test", True, None, 0, None, False, False) is False
    assert ctl.fsm.state not in (SafetyState.RUNNING, SafetyState.FAULT)
    assert not ctl._busy()


def test_fault_reset_refused_without_a_main_channel(make_ctl, monkeypatch):
    ctl = make_ctl(arm=False)
    ctl.raise_fault("test fault")
    deadline = time.monotonic() + 5.0
    while ctl._busy() and time.monotonic() < deadline:
        time.sleep(0.01)
    monkeypatch.setattr(UiController, "_mc", property(lambda self: None))
    assert ctl._sup.connected
    assert ctl.reset_fault() is False
    assert ctl.fsm.faulted
