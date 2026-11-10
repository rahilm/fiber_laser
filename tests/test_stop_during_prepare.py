"""A Stop while the job is still being prepared (perf review 2026-11-09, finding 2).

Before, the worker carried on after the Stop: it finished the preparation (10 s and more for a
few hundred parts), then set the streaming deadline, switched the process-signal output on,
reset and cleared the card's FIFO and halted a second time, tens of seconds after the
operator's Stop.  Now the preparation tests the job's cancel flag between its stages and the
worker leaves right after it: the halt (first, from the Stop) is the last thing the card hears
of this job, and the controller is ready again at once."""
from __future__ import annotations

import threading
import time

import pytest

from mlaser.controller import stream as S
from mlaser.controller.client import MotionController
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
from mlaser.protocol import frames as F

pytestmark = pytest.mark.sim

PROCESS_PORT = 6
SLOW_S = 2.0


def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def _doc():
    return Document([Part([_rect(0, 0, 40, 20)], Craft(layer=1)),
                     Part([_rect(60, 0, 20, 20)], Craft(layer=1))],
                    {1: LayerParams(1, speed=100, power_pct=50)})


def _wait(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("dry", [False, True])
def test_stop_during_a_slow_prepare_sends_nothing_after_the_halt(monkeypatch, dry):
    from mlaser.controller.ui_adapter import UiController
    sent: list = []                                 # (thread, request) of every card request
    real_transact = MotionController.transact

    def transact(mc, req):
        sent.append((threading.current_thread().name, req))
        return real_transact(mc, req)
    monkeypatch.setattr(MotionController, "transact", transact)
    entered = threading.Event()
    stages: list = []

    def slow(name, real):
        def fn(*a, **k):
            stages.append(name)
            entered.set()
            time.sleep(SLOW_S)                       # a large nest: seconds per stage
            return real(*a, **k)
        return fn
    monkeypatch.setattr(S, "_packed_timing", slow("timing", S._packed_timing))
    monkeypatch.setattr(S.PackedFrames, "__init__", slow("frames", S.PackedFrames.__init__))

    ctl = UiController(poll_s=0.02, output_map={"process": PROCESS_PORT})
    try:
        assert ctl.connect_simulator(time_scale=1.0)
        assert ctl.arm()
        (ctl.dry_run if dry else ctl.start)(_doc())
        assert entered.wait(30), "the job never reached its preparation"
        t_stop = time.monotonic()
        done = ctl.stop()
        assert done.wait(SLOW_S + 1.5), "the Stop waited for the whole preparation"
        t_done = time.monotonic() - t_stop
        assert stages == ["timing"], "the preparation went on after the Stop"
        assert ctl.wait_job(1.0)
        assert ctl.state().mode == "ready"
        time.sleep(0.3)                              # anything late would show up now
        worker = [r for t, r in sent if t == "mlaser-job"]
        assert worker == [], f"the job worker talked to the card: {worker[:5]}"
        on = F.mailbox_sys(F.output_bit(PROCESS_PORT)[0], 1 << F.output_bit(PROCESS_PORT)[1],
                           1 << F.output_bit(PROCESS_PORT)[1])
        mailbox = [r.values for _t, r in sent if r.func == F.FUNC_WRITE and r.addr == F.REG_MAILBOX]
        assert list(on) not in mailbox, "the process signal was switched on"
        assert [F.OP_SYS, F.SYS_JOB_RESET] not in mailbox, "a FIFO job reset after the Stop"
        # and the next Start is accepted at once (no "a job is already running")
        assert not ctl._busy()
        assert t_done < SLOW_S + 1.5
    finally:
        ctl.disconnect()


def test_prepare_returns_at_once_when_cancelled():
    """The streamer's own preparation tests the cancel flag between its stages."""
    from mlaser.nc import compile_job
    job = compile_job(_doc())
    js = S.JobStreamer.__new__(S.JobStreamer)
    js._prepared = None
    ev = threading.Event()
    ev.set()
    js._prepare(job, ev)
    assert js._prepared is None
    js._prepare(job, threading.Event())
    assert js._prepared is not None and js._prepared[0] is job
