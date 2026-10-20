"""Review 2 safety P1s (code_health.md): queue disarm, breakpoint lock across a fault reset,
the FIFO pause mode, the resume re-pierce layers, the laser-source guards and the raw query."""
from __future__ import annotations

import time

import pytest

from safety_helpers import rect_doc, start_and_run, stopped, wait_until

from mlaser.devices import laser as L
from mlaser.machine import events as EV
from mlaser.machine.safety import SafetyState
from mlaser.protocol import frames as F


# ----------------------------------------------------------------------------- queue (features #8)
def test_machine_is_disarmed_when_the_queue_finishes(tmp_path):
    from mlaser.machine.demo import DemoController
    from mlaser.services.queue import JobQueue, QueueRunner
    ctl = DemoController()
    ctl.connect()
    assert ctl.arm() and ctl.fsm.armed

    def start():
        ctl.events.publish(EV.JobStarted("process", "sheet"))
        return True
    q = JobQueue(tmp_path / "q.json")
    q.add("a.chf", 2)
    r = QueueRunner(q, load=lambda e: True, start=start, disarm=ctl.disarm)
    r.attach(ctl.events)
    for _ in range(2):
        assert r.confirm_sheet()
        assert ctl.fsm.armed                       # still armed between the sheets
        ctl.events.publish(EV.JobFinished("process", True, "done", duration_s=1.0))
    assert r.state == "idle" and r.message == "queue.msg.finished"
    assert not ctl.fsm.armed


# ----------------------------------------------------------------------------- P1-5 breakpoint lock
@pytest.fixture
def armed_ctl(make_ctl):
    return make_ctl(pessimistic=False)


def test_breakpoint_keeps_the_job_active_across_a_fault_reset(armed_ctl):
    ctl = armed_ctl
    doc = rect_doc()
    start_and_run(ctl, doc, 0.4)
    ctl.raise_fault("test: injected fault")
    assert ctl.wait_job(10)
    assert ctl.breakpoint is not None
    assert wait_until(lambda: stopped(ctl.sim), 5) is not None
    assert ctl.reset_fault()
    # the job is still active: the document lock (safety_ui.job_locked) holds
    assert ctl.fsm.state == SafetyState.PAUSED and not ctl.fsm.armed
    assert ctl.job_active()
    from mlaser.ui import safety_ui

    class W:
        pass
    w = W()
    w.ctl = ctl
    assert safety_ui.job_locked(w, "File > Open")
    # Start with another document does not resume the old job on it
    n = len(ctl.sim.state.commands)
    assert ctl.arm()
    ctl.start(rect_doc(0, 0, 50, 50))
    time.sleep(0.2)
    assert not ctl._busy() and ctl.breakpoint is not None
    assert "start_gate" in ctl.last_preflight.codes()
    assert not any(c[:1] == [F.OP_MOVE] for c in ctl.sim.state.commands[n:])
    # the same drawing (a fresh snapshot) resumes
    import copy
    ctl.start(copy.deepcopy(doc))
    assert wait_until(lambda: ctl._busy() and ctl.sim.state.fifo_running, 10) is not None
    ctl.stop(wait=True)
    assert ctl.breakpoint is None and not ctl.job_active()


def test_document_key_follows_the_cutting_content():
    import copy
    from mlaser.nc.resume import document_key
    a = rect_doc()
    assert document_key(a) == document_key(copy.deepcopy(a)) != ""
    assert document_key(a) != document_key(rect_doc(0, 0, 801, 600))
    b = copy.deepcopy(a)
    b.layers[1].power_pct = 60
    assert document_key(a) != document_key(b)
    assert document_key(None) == ""


# ----------------------------------------------------------------------------- P1-6 fifo pause mode
def test_fifo_pause_latches_an_estop_and_resumes_through_a_breakpoint(make_ctl):
    ctl = make_ctl(pause_mode="fifo", pessimistic=False)
    start_and_run(ctl, rect_doc(), 0.5)
    ctl.pause()
    assert wait_until(lambda: not ctl.sim.state.fifo_running, 2) is not None
    ctl.sim.set_estop(True)
    assert wait_until(lambda: ctl.fsm.faulted, 2) is not None, "e-stop while held not latched"
    ctl.sim.set_estop(False)
    ctl.resume()
    time.sleep(0.3)
    assert not ctl.sim.state.fifo_running and not ctl.sim.state.laser   # no restart after an e-stop
    assert ctl.wait_job(10) and ctl.breakpoint is not None


def test_fifo_resume_goes_through_the_gate_and_repierces(make_ctl):
    ctl = make_ctl(pause_mode="fifo", pessimistic=False)
    start_and_run(ctl, rect_doc(), 0.5)
    ctl.pause()
    assert wait_until(lambda: not ctl.sim.state.fifo_running, 2) is not None
    ctl.disarm("test")                     # arming is checked again by the resume
    ctl.resume()                           # refused: disarmed; the held job ends as a breakpoint
    # the job thread records the breakpoint asynchronously (a slow runner saw None here)
    assert wait_until(lambda: ctl.breakpoint is not None and not ctl._busy(), 10) is not None
    assert not ctl.sim.state.fifo_running
    assert ctl.arm()
    ctl.resume()
    assert wait_until(lambda: ctl._busy() and ctl.sim.state.fifo_running, 10) is not None
    rj = ctl._run_job_obj
    assert getattr(rj, "resume_from", None) is not None             # a breakpoint resume job
    ctl.stop(wait=True)


# ----------------------------------------------------------------------------- P1-7 resume layers
def test_resume_repierce_uses_the_layer_table():
    from mlaser.model import LayerParams
    from mlaser.nc import Machine, compile_job
    from mlaser.nc.resume import resume_job
    m = Machine()
    doc = rect_doc(define=False)                          # layer 1 only in the machine table
    table = {1: LayerParams(1, speed=100, power_pct=30, duty_pct=40, frequency_hz=2000, gas=1,
                            gas_pressure=1.5)}
    job = compile_job(doc, None, m, start=(0, 0), layers=table)
    a, b, _ly = job.cut_blocks[0]
    idx = (a + b) // 2

    def pwm(wire):
        return [list(w[1:5]) for w in wire
                if w and (w[0] & 0xFFFF) == F.OP_SYS and len(w) > 3 and w[1] in (F.SYS_PWM, F.SYS_PWM_BUS)]
    original = pwm(job.wire[:idx])[-1]
    rj = resume_job(job, idx, (0.0, 0.0), mode="exact", doc=doc)
    body = rj.wire[:len(rj.wire) - (len(job.wire) - idx)]
    assert pwm(body) and all(p == original for p in pwm(body)), (pwm(body), original)
    # the gas pressure DA of the re-pierce is the layer's, not the default
    das = [list(w[1:4]) for w in body if w and (w[0] & 0xFFFF) == F.OP_SYS and len(w) > 3 and w[1] == F.SYS_DA]
    orig_das = [list(w[1:4]) for w in job.wire[:idx] if w and (w[0] & 0xFFFF) == F.OP_SYS and len(w) > 3
                and w[1] == F.SYS_DA]
    assert das and das[-1] == orig_das[-1]


# ----------------------------------------------------------------------------- P1-1 laser-source guard
class _Armed:
    def __init__(self, armed):
        self.armed = armed

    def __call__(self, on):
        if on and not self.armed:
            raise L.LaserError("not armed")


@pytest.fixture
def clean_guards():
    saved = dict(L._GUARDS)
    L._GUARDS.clear()
    yield
    L._GUARDS.clear()
    L._GUARDS.update(saved)


def test_a_second_controller_cannot_hijack_the_laser_source_guard(clean_guards):
    real, rig = object(), object()
    g_real, g_rig = _Armed(False), _Armed(True)
    L.register_emission_guard(g_real, real)
    L.register_emission_guard(g_rig, rig)                 # the wizard's rig, armed
    with pytest.raises(L.LaserError):
        L.check_emission(True)                            # the real machine is disarmed
    L.unregister_emission_guard(rig)                      # closing the rig removes only its own
    g_real.armed = True
    L.check_emission(True)
    L.unregister_emission_guard(real)
    with pytest.raises(L.LaserError):
        L.check_emission(True)                            # nobody: refused


def test_a_bound_laser_source_follows_only_its_controller(clean_guards):
    sim = L.LaserSimulator("ipg")
    las = L.IpgLaser(sim.loopback())
    mine = _Armed(False)
    las.bind_guard(mine)
    L.set_emission_guard(lambda on: None)                 # any other (permissive) guard
    with pytest.raises(L.LaserError):
        las.emission(True)
    mine.armed = True
    las.emission(True)
    assert sim.emission


def test_simulator_rig_and_real_controller(clean_guards):
    """End to end (probe p02 b): the procedures' rig armed never lets the real controller's laser
    source fire while the real machine is disarmed; closing the rig keeps the real guard."""
    from mlaser.controller.ui_adapter import UiController
    from mlaser.services.procedures import simrig
    real = UiController(require_homed=False, poll_s=0.05)
    assert real.connect_simulator()
    try:
        with pytest.raises(L.LaserError):
            L.check_emission(True)
        rig = simrig.simulator_controller("card")
        try:
            assert rig.arm()
            with pytest.raises(L.LaserError):
                L.check_emission(True)
        finally:
            simrig.close(rig)
        assert real.arm()
        L.check_emission(True)
    finally:
        real.disconnect()


# ----------------------------------------------------------------------------- P1-2 raw query
@pytest.mark.parametrize("text", ["EMON", " emon", "STA\rEMON", "STA\nEMON", "RCT;EMON", "ABN", "ELE",
                                  "EMOD", "STA\x00EMON"])
def test_raw_query_cannot_smuggle_an_emission_command(clean_guards, text):
    sim = L.LaserSimulator("ipg")
    las = L.IpgLaser(sim.loopback())
    with pytest.raises(L.LaserError):
        las.query(text)
    assert not sim.emission and not sim.aiming
    assert not sim.log


def test_raw_send_payload_is_checked(clean_guards):
    sim = L.LaserSimulator("ipg")
    las = L.IpgLaser(sim.loopback())
    with pytest.raises(L.LaserError):
        las.send(b"STA\rEMON\r")
    r = L.RaycusLaser(L.LaserSimulator("raycus").loopback())
    with pytest.raises(L.LaserError):
        r.send(b"\x1bCP\x10\r" + L.RaycusCommands.emission_on())
    L.set_emission_guard(lambda on: None)
    assert las.query("STA").startswith("STA")             # read-only queries still work
