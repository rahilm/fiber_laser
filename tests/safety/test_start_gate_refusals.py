"""The start gate's refusals, one test per refusal (docs/SAFETY.md §5a; review 2026-11-09, fw.md F5).

The coverage run of the review found these refusals of ``StartGatePart._start_gate``
(``mlaser/controller/parts/gate.py``) reached by no test: not connected, a fault latched, a job
already running, homing in progress, the card not answering, the axes still moving, a card
alarm, the head moved since planning, the follower read failing, and a beam job while not
armed.  The public start paths (Start, Frame, the moves) check some of these themselves before
they reach the gate, so the gate is called here directly, exactly as every start path calls
it: the gate is the last check before the card gets the first command and must hold on its
own.

Each test checks the refusal as the UI sees it (``last_preflight`` with the ``start_gate``
error and its reason), that nothing moved, and, where it can, that the same call passes once
the condition is gone (the refusal is caused by that condition, not by something else).

Conditions the simulator cannot show between two monitor polls (the card moving or alarmed
when the gate re-reads it, before the monitor has seen it) are given to the gate's own status
read only: the gate re-reads the card at gate time precisely so that such a change is caught.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from safety_helpers import head_mm, rect_doc, stopped, wait_until


@pytest.fixture
def ctl():
    from mlaser.controller.ui_adapter import UiController
    c = UiController(poll_s=0.02, stream_process=False, require_homed=False, jog_deadman_s=None)
    assert c.connect_simulator(time_scale=1.0)
    yield c
    c.stop(wait=True)
    c.wait_job(10)
    c.disconnect()


def _refused(ctl, action: str = "dry_run", **kw) -> str:
    """Call the gate, expect a refusal; returns the published reason."""
    assert ctl._start_gate(action, **kw) is False
    rep = ctl.last_preflight
    assert rep is not None and not rep.ok
    assert "start_gate" in rep.codes()
    return rep.summary()


def _passes(ctl, action: str = "dry_run", **kw) -> None:
    assert ctl._start_gate(action, **kw) is True, ctl.last_preflight and ctl.last_preflight.summary()


def _on_gate_read(monkeypatch, ctl, edit) -> None:
    """Let ``edit(status)`` change the card status that THIS thread reads (the gate's re-read);
    the monitor thread keeps reading the simulator unchanged."""
    real = ctl._mc.read_status
    me = threading.get_ident()

    def read_status(*a, **kw):
        st = real(*a, **kw)
        if threading.get_ident() == me:
            edit(st)
        return st

    monkeypatch.setattr(ctl._mc, "read_status", read_status)


def test_idle_connected_controller_passes_the_gate(ctl):
    """The baseline every refusal below is measured against."""
    _passes(ctl, "dry_run")
    _passes(ctl, "move", job_like=False)


def test_refused_when_not_connected():
    from mlaser.controller.ui_adapter import UiController
    c = UiController(poll_s=0.02, stream_process=False, require_homed=False, jog_deadman_s=None)
    try:
        assert "not connected" in _refused(c, "start")
        assert "not connected" in _refused(c, "move", job_like=False)
    finally:
        c.disconnect()


def test_refused_while_a_fault_is_latched(ctl):
    ctl.raise_fault("test fault")
    assert ctl.fsm.faulted
    why = _refused(ctl, "dry_run")
    assert "fault" in why and "test fault" in why
    assert _refused(ctl, "move", job_like=False)
    assert stopped(ctl.sim)
    assert ctl.reset_fault()
    _passes(ctl, "dry_run")


def test_refused_while_a_job_is_running(ctl):
    ctl.dry_run(rect_doc(0, 0, 600, 400))
    assert wait_until(ctl._busy, 5) is not None
    assert "already running" in _refused(ctl, "dry_run")
    assert "already running" in _refused(ctl, "move", job_like=False)
    ctl.stop(wait=True)
    assert ctl.wait_job(10)
    assert wait_until(lambda: stopped(ctl.sim), 10) is not None
    _passes(ctl, "dry_run")


def test_refused_while_homing(ctl):
    ctl.home("XY")
    assert wait_until(lambda: ctl._homing, 2) is not None
    assert "Homing" in _refused(ctl, "dry_run")
    assert "Homing" in _refused(ctl, "move", job_like=False)
    assert not ctl._busy()
    assert wait_until(lambda: not ctl._homing, 30) is not None
    assert wait_until(lambda: stopped(ctl.sim), 10) is not None
    _passes(ctl, "dry_run")


def test_refused_when_the_card_does_not_answer(ctl, monkeypatch):
    from mlaser.controller.client import ControllerError

    def no_answer(st):
        raise ControllerError("timeout (test)", 1)

    _on_gate_read(monkeypatch, ctl, no_answer)
    why = _refused(ctl, "dry_run")
    assert "does not answer" in why and "timeout (test)" in why
    monkeypatch.undo()
    _passes(ctl, "dry_run")


def test_refused_when_the_card_reports_motion_although_idle(ctl, monkeypatch):
    """Machine state 0 but an axis still has velocity (a stop that is still decelerating)."""
    from mlaser.protocol import frames as F
    from mlaser.controller.parts.connection import AXES

    def coasting(st):
        st.axes[AXES["X"]].raw[F.AX_VELOCITY] = int(5.0 * (st.pulses_per_mm or 1000.0))   # 5 mm/s

    _on_gate_read(monkeypatch, ctl, coasting)
    assert "still moving" in _refused(ctl, "dry_run")
    assert "still moving" in _refused(ctl, "move", job_like=False)
    monkeypatch.undo()
    _passes(ctl, "dry_run")


def test_refused_when_the_card_reports_an_alarm(ctl, monkeypatch):
    """An axis alarm the card raised after the monitor's last poll: the axis bit of alarm
    word 1 and the servo bit of that axis' status word (alarms.decode)."""
    from mlaser.controller import alarms as A
    from mlaser.controller.parts.connection import AXES
    from mlaser.protocol import frames as F

    def servo_alarm(st):
        x = AXES["X"]
        st.ro[A.RO_ALARM1] |= 1 << x
        st.axes[x].raw[F.AX_STATUS] |= 1 << int(A.AX_SERVO)

    _on_gate_read(monkeypatch, ctl, servo_alarm)
    why = _refused(ctl, "dry_run")
    assert "alarm" in why and "Reset alarm" in why
    assert _refused(ctl, "move", job_like=False)
    monkeypatch.undo()
    _passes(ctl, "dry_run")


def test_refused_when_the_head_is_not_where_the_job_starts(ctl):
    hx, hy = head_mm(ctl.sim)
    tol = ctl.start_position_tol_mm
    planned_elsewhere = SimpleNamespace(start_mm=(hx + 10.0, hy))
    why = _refused(ctl, "dry_run", job=planned_elsewhere)
    assert "moved since the job was planned" in why
    just_outside = SimpleNamespace(start_mm=(hx + 3 * tol, hy))
    assert "moved since" in _refused(ctl, "dry_run", job=just_outside)
    _passes(ctl, "dry_run", job=SimpleNamespace(start_mm=(hx + tol / 2, hy)))
    _passes(ctl, "dry_run", job=SimpleNamespace(start_mm=(hx, hy)))


def test_a_failing_follower_read_does_not_open_or_block_the_gate(ctl, monkeypatch):
    """The follower read raising (the link to the follower card) falls back to the monitor's
    last follower state: the gate neither crashes nor refuses because of the read itself, and
    still refuses for the other reasons."""
    real = ctl._read_follower
    me = threading.get_ident()
    calls: list[int] = []

    def broken(mc):
        if threading.get_ident() == me:
            calls.append(1)
            raise OSError("follower link down (test)")
        return real(mc)

    monkeypatch.setattr(ctl, "_read_follower", broken)
    _passes(ctl, "dry_run")
    assert calls, "the gate did not read the follower"
    ctl.raise_fault("test fault")
    assert "fault" in _refused(ctl, "dry_run")
    monkeypatch.undo()
    assert ctl.reset_fault()


def test_beam_job_refused_while_not_armed(ctl):
    ctl.disarm("test")
    assert not ctl.fsm.armed
    why = _refused(ctl, "laser_test", emits=True)
    assert "not armed" in why
    _passes(ctl, "dry_run")                       # the same machine, no beam: allowed
    assert ctl.arm()
    _passes(ctl, "laser_test", emits=True)
    assert not ctl.sim.state.laser               # passing the gate fires nothing by itself
    ctl.disarm("test")


def test_a_refusal_publishes_and_audits_but_sends_nothing(ctl, monkeypatch):
    """A refusal leaves the card as it was: no motion, no job, no output change."""
    audits: list = []
    from mlaser.controller.parts import gate as G
    monkeypatch.setattr(G, "audit", lambda ev, *a, **kw: audits.append((ev, kw)))
    before = (head_mm(ctl.sim), ctl.sim.state.outputs, len(ctl.sim.state.history))
    ctl.raise_fault("test fault")
    _refused(ctl, "start", emits=True)
    time.sleep(0.1)
    assert (head_mm(ctl.sim), ctl.sim.state.outputs, len(ctl.sim.state.history)) == before
    assert ("start_refused", {"mode": "start", "reason": audits[-1][1]["reason"]}) == audits[-1]
    assert "fault" in audits[-1][1]["reason"]
    assert ctl.reset_fault()
