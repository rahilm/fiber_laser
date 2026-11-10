"""A lost FIFO reply, the client's re-send and what the card may do with the duplicate (arch.md A5).

The client re-sends a request whose reply did not come in time (the original's retry policy,
``MotionController.transact``; not changed here).  For a FIFO frame (write 102) the card has
already queued the first send, so it receives the same frame, with the same sequence number,
twice.  What the real MCC100 does with it is UNKNOWN; the bench procedure that settles it is in
docs/dev/BENCH_CHECKS.md.  The simulator models the three candidates (``duplicate_fifo``).

In-thread streamer (JobStreamer) at the simulator's 1 s link deadline, the only configuration
in which the re-send reaches a running card:

=================  =====================================  =========================================
card model         what the host does (asserted here)     consequence
=================  =====================================  =========================================
``accept-twice``   nothing: the job ends "done", no       the frame's segment runs twice (extra
(default)          fault, no warning.  The host CANNOT    travel; with the beam on, a segment cut
                   see it: RO[15] holds the same          twice).  The progress / breakpoint index
                   sequence number either way, and the    (FIFO byte accounting) lags by the
                   extra bytes only make the FIFO drain   frame's bytes until the FIFO drains.
                   later.
``dedupe``         nothing; the job ends "done"           none: the card executes the job once
``reject``         FAULT (the exception reply 503 aborts  the job stops at the frame; breakpoint
                   the stream), halt, breakpoint kept     kept, Resume continues with a re-pierce
=================  =====================================  =========================================

The process streamer (what a real card gets) never reaches the re-send on a running card: the
child waits for the lost reply (FIFO timeout 600 ms) without relaying, the parent's child
deadline (500 ms) latches a FAULT and halts the card first, under every model.  With the real
link deadline (150 ms) both streamers latch "link lost" first.  In both cases the re-send still
goes out afterwards (the retry loop in ``transact`` does not look at the halt) and reaches a
stopped, cleared FIFO, where it must never run.

"""
from __future__ import annotations

import pytest

from mlaser.controller.simulator import (DUP_FIFO_ACCEPT_TWICE, DUP_FIFO_DEDUPE, DUP_FIFO_MODELS,
                                         DUP_FIFO_REJECT, CardSimulator)
from mlaser.machine.safety import SafetyState
from mlaser.protocol import frames as F

from safety_helpers import rect_doc, safe, stopped, wait_until

STREAMERS = [pytest.param(False, id="thread"), pytest.param(True, id="process")]


def _steps(seq) -> int:
    return sum(1 for w in seq if (w[0] & 0xFFFF) == F.OP_STEP)


def _executed_steps(sim) -> int:
    return sum(1 for op, _p in list(sim.state.history) if op == F.OP_STEP)


class _Recorder:
    """Every request the simulator handled, in order: (monotonic time, request)."""

    def __init__(self, sim):
        import time
        self.log: list = []
        orig = sim.handle

        def handle(data: bytes):
            try:
                req = F.decode_request(data)
            except F.FrameError:
                req = None
            self.log.append((time.monotonic(), req))
            return orig(data)

        sim.handle = handle

    def writes(self, reg: int) -> list:
        return [(t, r) for t, r in self.log if r is not None and r.func == F.FUNC_WRITE and r.addr == reg]


def _start_and_lose_one_reply(ctl, doc):
    sim = ctl.sim
    wait_until(lambda: stopped(sim), 5)
    ctl.start(doc)
    assert wait_until(lambda: sim.state.fifo_running, 20) is not None, "job did not start"
    # the card is running the prefill: the reply of the next FIFO frame is lost
    sim.inject(fifo_reply_loss=1)


def test_the_duplicate_model_is_explicit_and_the_default_is_unchanged():
    assert CardSimulator().duplicate_fifo == DUP_FIFO_ACCEPT_TWICE
    assert set(DUP_FIFO_MODELS) == {"accept-twice", "dedupe", "reject"}
    with pytest.raises(ValueError):
        CardSimulator(duplicate_fifo="ignore")


@pytest.mark.parametrize("model", DUP_FIFO_MODELS)
def test_one_frame_sent_twice_on_the_wire(model):
    """The reviewer's measurement (arch/tests/dup_frame.py) under each model, with the real
    client: one frame of 5 items whose first reply is lost."""
    from mlaser.controller.client import MotionController, TransportConfig
    sim = CardSimulator(duplicate_fifo=model).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=100, fifo_time_ms=300,
                              fifo_timeout_ms=100)
        with MotionController(config=cfg) as mc:
            mc.fifo_job_reset()
            mc.fifo_clear()
            fr = F.build_fifo_frames([F.encode_step(10, 0, duty=0) for _ in range(5)],
                                     mc.read_status().fifo_seq + 1)[0]
            sim.inject(fifo_reply_loss=1)
            r0 = sim.requests
            if model == DUP_FIFO_REJECT:
                from mlaser.controller.client import ControllerError
                with pytest.raises(ControllerError) as ei:
                    mc.fifo_write_frame(fr)
                assert ei.value.code == F.ERR_EXCEPTION_BASE + 3     # exception "illegal value"
            else:
                mc.fifo_write_frame(fr)
            st = sim.state
            assert sim.requests - r0 == 2 and st.fifo_frames == 2 and st.fifo_duplicates == 1
            assert st.fifo_seq == 1 and mc.read_status().fifo_seq == 1      # the same either way
            queued = {DUP_FIFO_ACCEPT_TWICE: 10, DUP_FIFO_DEDUPE: 5, DUP_FIFO_REJECT: 5}[model]
            assert len(st.fifo) == queued and st.fifo_bytes == 12 * queued
            assert st.fifo_duplicates_queued == (1 if model == DUP_FIFO_ACCEPT_TWICE else 0)
    finally:
        sim.stop()


def _assert_resend_hits_a_halted_card(sim, rec) -> None:
    """The re-send went out after the halt's FIFO clear and the frame never runs."""
    import time
    assert wait_until(lambda: sim.state.fifo_duplicates == 1, 3.0) is not None, \
        "the client did not re-send the frame"
    assert wait_until(lambda: safe(sim), 2.0) is not None
    frames = rec.writes(F.REG_FIFO_DATA)
    seqs = [r.values[0] for _t, r in frames]
    dup_seq = next(s for i, s in enumerate(seqs) if i and seqs[i - 1] == s)
    t_resend = [t for t, r in frames if r.values[0] == dup_seq][1]
    clears = [t for t, r in rec.writes(F.REG_FIFO_CTRL) if r.values == [F.FIFO_CLEAR]]
    assert any(t < t_resend for t in clears[1:]), "the halt's FIFO clear came after the re-send"
    # accept-twice: the re-sent frame now sits in a stopped FIFO; it must never run
    x = sim.position(0), sim.position(1)
    time.sleep(0.3)
    assert (sim.position(0), sim.position(1)) == x and not sim.state.fifo_running


@pytest.mark.parametrize("model", DUP_FIFO_MODELS)
@pytest.mark.parametrize("process", STREAMERS)
def test_lost_fifo_reply_during_a_job(make_ctl, process, model):
    """A job with one lost FIFO reply, at the simulator's link deadline (1 s, longer than the
    600 ms FIFO timeout)."""
    ctl = make_ctl(process=process)
    sim = ctl.sim
    sim.duplicate_fifo = model
    rec = _Recorder(sim)
    _start_and_lose_one_reply(ctl, rect_doc(w=100.0, h=60.0))
    assert ctl.wait_job(40)
    st = sim.state
    planned = _steps(ctl.job.wire)
    if process:
        # the real card's streamer: the child waits for the lost reply (FIFO timeout 600 ms)
        # without relaying, and the parent's child deadline (500 ms) faults first, under every
        # model; the halt goes out, then the child's re-send
        assert ctl.fsm.state == SafetyState.FAULT, ctl.fsm.state
        assert "link lost" in ctl.fsm.fault or "child_stalled" in ctl.fsm.fault, ctl.fsm.fault
        assert ctl.breakpoint is not None
        _assert_resend_hits_a_halted_card(sim, rec)
        return
    assert st.fifo_duplicates == 1, "the lost reply did not make the client send the frame again"
    assert wait_until(lambda: safe(sim), 2.0) is not None
    if model == DUP_FIFO_REJECT:
        # the exception reply aborts the stream: FAULT, halt, the place is kept for Resume
        assert ctl.fsm.state == SafetyState.FAULT, ctl.fsm.state
        assert str(F.ERR_EXCEPTION_BASE + 3) in ctl.fsm.fault, ctl.fsm.fault
        assert ctl.breakpoint is not None
        assert _executed_steps(sim) < planned
        return
    assert not ctl.fsm.faulted, ctl.fsm.fault
    assert ctl.breakpoint is None
    executed = _executed_steps(sim)
    if model == DUP_FIFO_DEDUPE:
        assert executed == planned
        return
    # accept-twice: the FINDING.  The host ended the job as "done" without a fault or a warning
    # (asserted above), while the card executed one frame twice.  Whether the real card does
    # this is UNKNOWN (docs/dev/BENCH_CHECKS.md); this test pins what the host does if it does.
    assert st.fifo_duplicates_queued == 1
    assert 0 < executed - planned <= 1224 // 12, f"{executed - planned} extra steps"


@pytest.mark.flaky(reruns=1)       # the real 150 ms deadline (docs/dev/TESTING.md)
@pytest.mark.parametrize("process", STREAMERS)
def test_lost_fifo_reply_with_the_real_deadline_is_a_link_loss(make_ctl, process):
    """With the real card's deadline (150 ms) the host halts before the client re-sends: FAULT
    "link lost", halt first; the re-send then reaches a stopped, cleared FIFO."""
    ctl = make_ctl(process=process, link_deadline_ms=150.0)
    sim = ctl.sim
    sim.duplicate_fifo = DUP_FIFO_ACCEPT_TWICE           # the worst case: queued again
    rec = _Recorder(sim)
    _start_and_lose_one_reply(ctl, rect_doc(w=100.0, h=60.0))
    assert wait_until(lambda: ctl.fsm.state == SafetyState.FAULT, 3.0) is not None
    assert "link lost" in ctl.fsm.fault, ctl.fsm.fault
    assert ctl.wait_job(10) and ctl.breakpoint is not None
    _assert_resend_hits_a_halted_card(sim, rec)
