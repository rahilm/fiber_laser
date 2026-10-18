"""Every exit path ends in a safe state (review measurements, architecture.md §2.1).

=====================================  =======================================  ==============
scenario                               before (review)                          bound asserted
=====================================  =======================================  ==============
disconnect() mid-job                   card cut on 5.27 s / 527 mm, PWM on       safe < 0.5 s, < 20 mm
Stop, link up (thread / process)       0.007 / 0.052 s, 0.6 / 5.0 mm             call < 50 ms, safe < 0.5 s
Stop with the link down                UI blocked 24.5 s                         call < 50 ms
worker exception                       only fifo_stop for Controller/OSError     FAULT + halt < 0.5 s
excepthook / threading.excepthook      nothing                                   FAULT + halt < 0.5 s
closeEvent / atexit / SIGTERM          disconnect only / nothing                 halt sent, safe < 1 s
=====================================  =======================================  ==============
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from mlaser.machine import lifecycle
from mlaser.machine.safety import SafetyState

from safety_helpers import Watch, rect_doc, safe, start_and_run, wait_until


@pytest.mark.parametrize("process", [False, True])
def test_disconnect_mid_job_halts_the_card(make_ctl, process):
    ctl = make_ctl(process=process)
    start_and_run(ctl)
    sim = ctl.sim
    w = Watch(sim)
    ctl.disconnect(keep_sim=True)
    dt, travel = w.until_safe(1.0)
    assert dt is not None and dt < 0.5, f"card not safe {dt} s after disconnect"
    assert travel < 20.0, f"head moved {travel:.1f} mm after disconnect (was 527 mm)"
    assert not sim.state.pwm_on and not sim.state.laser
    cmds = sim.state.commands
    assert [1, 0x1F, 2, ctl._decel_counts(), 200000] in cmds
    assert [9999, 3, ctl.machine.pt_laser_freq, 0, 0] in cmds
    sim.stop()


@pytest.mark.parametrize("process", [False, True])
def test_stop_is_asynchronous_and_halts_first(make_ctl, process):
    ctl = make_ctl(process=process)
    start_and_run(ctl)
    sim = ctl.sim
    w = Watch(sim)
    t0 = time.monotonic()
    done = ctl.stop()
    call_s = time.monotonic() - t0
    assert call_s < 0.05, f"stop() blocked the caller {call_s:.3f} s"
    dt, travel = w.until_safe(1.0)
    assert dt is not None and dt < 0.5 and travel < 20.0
    assert done.wait(10)
    assert wait_until(lambda: ctl.state().mode == "ready", 0.5) is not None
    assert ctl.fsm.state == SafetyState.ARMED
    # the parity stop order comes first: decelerated stop ... FIFO stop, FIFO clear
    from mlaser.parity.capture import expected_stop_sequence
    want = [w for r, w in expected_stop_sequence(ctl.machine) if r == 101]
    cmds = sim.state.commands
    i = cmds.index(want[0])
    assert cmds[i:i + len(want)] == want


def test_stop_with_the_link_down_never_blocks(make_ctl):
    ctl = make_ctl(process=False)
    start_and_run(ctl, run_s=0.3)
    ctl.sim.link_down(True)
    time.sleep(0.05)
    t0 = time.monotonic()
    done = ctl.stop()
    assert time.monotonic() - t0 < 0.05, "stop() blocked while the link is down (was 24.5 s)"
    ctl.sim.link_down(False)
    assert done.wait(15)


def test_worker_exception_latches_a_fault_and_halts(make_ctl, monkeypatch):
    ctl = make_ctl(process=False)
    from mlaser.controller import stream as S
    orig = S.JobStreamer.run_job
    calls = {"n": 0}

    def boom(self, *a, **kw):
        calls["n"] += 1
        old = self.on_status

        def status(st):
            old(st)
            if self.buf.polls > 40:
                raise ZeroDivisionError("injected bug in the streamer")
        self.on_status = status
        return orig(self, *a, **kw)

    monkeypatch.setattr(S.JobStreamer, "run_job", boom)
    ctl.start(rect_doc())
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 10) is not None
    assert "ZeroDivisionError" in ctl.fsm.fault
    assert wait_until(lambda: safe(ctl.sim), 0.5) is not None
    assert ctl.breakpoint is not None                 # resumable after the reset
    assert ctl.wait_job(5)


def test_excepthooks_halt_and_latch(make_ctl):
    ctl = make_ctl(process=False)
    lifecycle.install(signals=False)
    try:
        start_and_run(ctl, run_s=0.3)
        sim = ctl.sim

        def bug():
            raise RuntimeError("injected uncaught error")
        t = threading.Thread(target=bug, name="buggy-thread")
        t.start()
        t.join()
        assert ctl.fsm.state == SafetyState.FAULT and "buggy-thread" in ctl.fsm.fault
        assert wait_until(lambda: safe(sim), 0.5) is not None
        assert ctl.wait_job(5)
        assert ctl.reset_fault() and ctl.fsm.state == SafetyState.DISARMED
        # the main-thread hook (Qt routes slot exceptions there)
        try:
            raise ValueError("main-thread bug")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        assert ctl.fsm.state == SafetyState.FAULT and "ValueError" in ctl.fsm.fault
        assert ("excepthook", ) == lifecycle.calls[-1][:1]
    finally:
        lifecycle.uninstall()


def test_close_event_halts_and_disconnects(make_ctl, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mlaser.ui.mainwindow import MainWindow
    QApplication.instance() or QApplication([])
    ctl = make_ctl(process=False)
    win = MainWindow(ctl)
    win._test_no_dialogs = True
    start_and_run(ctl, run_s=0.3)
    sim = ctl.sim
    w = Watch(sim)
    ctl.disconnect = (lambda orig=ctl.disconnect: (lambda *a, **k: orig(*a, keep_sim=True, **k)))()
    win.close()
    dt, travel = w.until_safe(1.0)
    assert dt is not None and dt < 0.5 and travel < 20.0
    assert not ctl.sup.connected
    sim.stop()


_CHILD = textwrap.dedent("""
    import os, signal, sys, time, threading
    sys.path.insert(0, {root!r})
    from mlaser.machine import lifecycle
    from mlaser.controller.ui_adapter import UiController
    from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
    pts = [(0, 0), (800, 0), (800, 600), (0, 600)]
    doc = Document(parts=[Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
                   Craft(layer=1))], layers={{1: LayerParams(1, speed=100, power_pct=50)}})
    lifecycle.install()
    ctl = UiController(poll_s=0.02, stream_process=False, require_homed=False)
    ctl.cfg.transport = "udp"
    assert ctl.connect("127.0.0.1", {port})
    assert ctl.arm()
    ctl.start(doc)
    time.sleep(0.8)
    print("RUNNING", flush=True)
    {how}
    time.sleep(10)
""")


@pytest.mark.parametrize("how", ["os.kill(os.getpid(), signal.SIGTERM)", "sys.exit(0)"])
def test_signal_and_atexit_halt(how):
    if how.startswith("os.kill") and sys.platform == "win32":
        pytest.skip("SIGTERM cannot be caught on Windows")
    from mlaser.controller.simulator import CardSimulator
    from mlaser.machine.preflight import TravelLimits  # noqa: F401 - import check
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sim = CardSimulator(pessimistic=True).start()
    try:
        code = _CHILD.format(root=root, port=sim.port, how=how)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        line = p.stdout.readline()
        assert "RUNNING" in line
        p.wait(10)
        t_exit = time.monotonic()
        assert wait_until(lambda: safe(sim), 1.0) is not None, "card still running after the exit"
        assert time.monotonic() - t_exit < 1.0
        assert any(c[:3] == [1, 0x1F, 2] for c in sim.state.commands)
    finally:
        sim.stop()
