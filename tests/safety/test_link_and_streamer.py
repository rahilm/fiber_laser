"""Link loss, streamer supervision, second-client stop, prefill cap (architecture.md §2.1, C2-C4).

=====================================  =======================================  ====================
scenario                               before (review)                          bound asserted
=====================================  =======================================  ====================
link loss mid-job                      3.5-5.3 s / 355-528 mm, laser on;         FAULT < 0.4 s; card stops
                                       mode back to "ready", nothing latched    < 1.2 s cap + 0.4 s;
                                                                                PWM off once the link is
                                                                                back (< 1.5 s)
UI process killed (SIGKILL), child     child streamed the whole job (> 12 s)     card safe < 1.0 s
streaming
streaming child killed                 parent sent only fifo_stop               FAULT, card safe < 1.0 s
FIFO stop by a second client           streamer restarted the FIFO              no restart in 1 s, FAULT
FIFO prefill                           whole FIFO, 5.4 s of motion              <= 1.2 s (+1 frame)
=====================================  =======================================  ====================
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from mlaser.controller.client import MotionController, TransportConfig
from mlaser.machine.safety import SafetyState

from safety_helpers import SPEED, Watch, rect_doc, safe, start_and_run, stopped, wait_until


# One rerun allowed: this test runs the card simulator in the test process with the real 150 ms
# deadline, so on a heavily loaded host another test's threads can starve it.  The bounds are
# never loosened (docs/dev/TESTING.md "timing-sensitive tests").
@pytest.mark.flaky(reruns=1)
@pytest.mark.parametrize("process", [False, True])
def test_link_loss_latches_a_fault_and_bounds_the_motion(make_ctl, process):
    ctl = make_ctl(process=process, link_deadline_ms=150.0)    # the real-card deadline
    start_and_run(ctl, run_s=0.8)
    sim = ctl.sim
    w = Watch(sim)
    sim.link_down(True)
    t_fault = wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 3.0)
    assert t_fault is not None and t_fault < 0.4, f"link loss detected after {t_fault} s (was 4-5 s)"
    assert "link lost" in ctl.fsm.fault
    # the card cannot be reached: it runs out what is buffered (<= max_buffer_ms) and stops
    t_stop = wait_until(lambda: stopped(sim), 3.0)
    assert t_stop is not None and t_stop < ctl.max_buffer_ms / 1000 + 0.4, f"card ran {t_stop} s"
    travel = (abs(sim.position(0) / sim.ppm - w.p0[0]) + abs(sim.position(1) / sim.ppm - w.p0[1]))
    assert travel < SPEED * (ctl.max_buffer_ms / 1000 + 0.4), f"{travel:.0f} mm after the loss (was 355-528)"
    # latched: nothing moves while the fault holds, even with the link back
    sim.link_down(False)
    assert wait_until(lambda: not sim.beam_on, 1.5) is not None, "PWM off not delivered after the link came back"
    n = len(sim.state.commands)
    ctl.jog_start("X", 1)
    ctl.resume()
    ctl.start(rect_doc())
    time.sleep(0.2)
    assert not any(c[0] in (3, 5) for c in sim.state.commands[n:]), "motion sent while faulted"
    assert ctl.wait_job(8)
    assert ctl.breakpoint is not None
    # operator reset: link answers, no alarms -> disarmed; arm + resume continues at the breakpoint
    assert ctl.reset_fault() and ctl.fsm.state == SafetyState.DISARMED and not ctl.fsm.armed
    assert ctl.arm()
    ctl.resume()
    assert wait_until(lambda: sim.state.fifo_running, 10) is not None
    assert ctl.stop().wait(10)


def test_ui_process_killed_child_self_halts():
    """SIGKILL of the GUI process while its streaming child runs: the child sees the parent go
    (getppid / heartbeat) and halts the card itself."""
    if sys.platform == "win32":
        pytest.skip("POSIX kill semantics")
    from mlaser.controller.simulator import CardSimulator
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sim = CardSimulator(pessimistic=True).start()
    code = textwrap.dedent(f"""
        import os, signal, sys, threading, time
        sys.path.insert(0, {root!r})
        from mlaser.protocol import frames as F
        from mlaser.controller.client import TransportConfig
        from mlaser.controller.stream import ProcessStreamer, WireJob
        if __name__ == "__main__":
            job = WireJob([F.encode_step(10, 0, 5000, 100, 5000) for _ in range(30000)], period_ms=1.0)
            ps = ProcessStreamer(TransportConfig(host="127.0.0.1", port={sim.port}), 1000.0,
                                 policy={{"armed": True, "halt": {{"pwm_freq": 5000}}}})
            def die():
                while not ps.executed:
                    time.sleep(0.01)
                time.sleep(0.5)
                print("KILL", flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            threading.Thread(target=die, daemon=True).start()
            ps.run_job(job)
    """)
    path = os.path.join(os.path.dirname(__file__), "_kill_parent_tmp.py")
    try:
        with open(path, "w") as f:
            f.write(code)
        p = subprocess.Popen([sys.executable, path], stdout=subprocess.PIPE, text=True)
        assert "KILL" in p.stdout.readline()
        p.wait(10)
        t = wait_until(lambda: safe(sim), 2.0)
        assert t is not None and t < 1.0, f"orphaned child kept streaming ({t})"
        x = sim.position(0)
        time.sleep(0.5)
        assert sim.position(0) == x and not sim.state.fifo_running
        assert [9999, 3, 5000, 0, 0] in sim.state.commands
    finally:
        sim.stop()
        if os.path.exists(path):
            os.remove(path)


def test_streaming_child_killed_parent_halts(make_ctl):
    ctl = make_ctl(process=True)
    start_and_run(ctl, run_s=0.5)
    sim = ctl.sim
    w = Watch(sim)
    ctl._streamer.proc.kill()
    t = wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 2.0)
    assert t is not None and "child_died" in ctl.fsm.fault
    dt, travel = w.until_safe(2.0)
    assert dt is not None and dt < 1.0
    assert ctl.wait_job(5) and ctl.breakpoint is not None


@pytest.mark.parametrize("cmd", ["fifo_stop", "stop_manu"])
def test_second_client_stop_is_never_overridden(make_ctl, cmd):
    ctl = make_ctl(process=False)
    start_and_run(ctl, run_s=0.5)
    sim = ctl.sim
    with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as other:
        if cmd == "fifo_stop":
            other.fifo_stop()
        else:
            other.stop_manu(4000, 5000, 0)
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 1.0) is not None
    assert wait_until(lambda: stopped(sim), 1.0) is not None
    x = sim.position(0)
    time.sleep(1.0)
    assert sim.position(0) == x and not sim.state.fifo_running, "the streamer restarted the FIFO"
    assert ctl.stream_stats.get("restarts", 0) == 0
    assert ctl.breakpoint is not None


@pytest.mark.parametrize("process", [False, True])
def test_prefill_is_capped(make_ctl, process):
    ctl = make_ctl(process=process)
    start_and_run(ctl, run_s=0.3)
    st = ctl.sim.state
    # 1 ms per step item, 12 bytes each: buffered motion = buffered bytes / 12 ms
    assert (st.fifo_bytes / 12.0) <= ctl.max_buffer_ms + 150
    assert ctl.stop().wait(10)
    b = ctl.stream_stats
    assert b.get("peak_buffer_ms", 0) <= ctl.max_buffer_ms + 150
    assert b.get("prefill_ms", 0) <= ctl.max_buffer_ms + 150


def test_pause_holds_before_the_stop_in_process_mode(make_ctl):
    """C4: the pause used to send the program stop before the child knew -> FIFO restart."""
    ctl = make_ctl(process=True)
    start_and_run(ctl, run_s=0.5)
    ctl.pause()
    assert wait_until(lambda: ctl.breakpoint is not None, 5) is not None
    assert ctl.fsm.state == SafetyState.PAUSED and not ctl.fsm.faulted
    assert wait_until(lambda: safe(ctl.sim), 1.0) is not None
    x = ctl.sim.position(0)
    time.sleep(0.5)
    assert ctl.sim.position(0) == x


def test_estop_during_job_faults_with_breakpoint_then_resumes(make_ctl):
    ctl = make_ctl(process=False)
    start_and_run(ctl, run_s=0.5)
    sim = ctl.sim
    sim.set_estop(True)
    time.sleep(0.3)
    sim.set_estop(False)
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 2.0) is not None
    assert wait_until(lambda: safe(sim), 1.0) is not None
    assert ctl.wait_job(5) and ctl.breakpoint is not None
    assert not ctl.arm()                                   # latched
    assert ctl.reset_fault() and ctl.arm()
    ctl.resume()
    assert wait_until(lambda: sim.state.fifo_running, 10) is not None
    assert ctl.stop().wait(10)


@pytest.mark.parametrize("process", [False, True])
def test_hold_never_latches_a_fault(make_ctl, process):
    """Hold during streaming must pause, never fault.  The pause used to latch the halt before
    cancelling the stream, so a request in flight was refused and taken for a fault (Wave 3)."""
    ctl = make_ctl(process=process)
    for _ in range(4):
        start_and_run(ctl, run_s=0.25)
        ctl.pause()
        assert wait_until(lambda: ctl.breakpoint is not None, 5) is not None
        assert not ctl.fsm.faulted, ctl.safety().fault
        assert ctl.fsm.state == SafetyState.PAUSED
        assert ctl.stop().wait(10)
        assert not ctl.fsm.faulted, ctl.safety().fault


def test_simulator_deadline_does_not_leak_to_a_real_card():
    """The in-process simulator streams with a 1 s deadline (shared GIL); a real card always gets
    the configured one (150 ms) back."""
    from mlaser.controller.parts.connection import SIM_LINK_DEADLINE_MS
    from mlaser.controller.simulator import CardSimulator
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.02, stream_process=False)
    try:
        assert ctl.connect_simulator()
        assert ctl.sup.link_deadline_ms == SIM_LINK_DEADLINE_MS
        ctl.disconnect()
        ctl.sim = None
        card = CardSimulator("127.0.0.1", 0).start()           # stands in for an external card
        try:
            ctl.connect("127.0.0.1", card.port)
            assert ctl.sup.link_deadline_ms == 150.0
        finally:
            ctl.disconnect()
            card.stop()
    finally:
        ctl.disconnect()
