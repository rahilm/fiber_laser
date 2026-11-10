"""The streamer keeps a compiled job's wire packed (perf review 2026-11-09, finding 1).

Before, ``WireJob.of`` unpacked the packed wire into a list per item, ``wire_timing`` and the
frames were built in Python and the streaming child received all of it pickled: 10-40 s and
over a gigabyte before the first frame of a large nest.  Now the packed wire is used as it is
and goes to the child through shared memory.  Everything the card receives must be what it
received before, bit for bit: the frames (numbering from any first sequence number included),
the per-item byte and time tables the progress and the motion cap read, the frame-end indexes
and the seek over them (resume, breakpoints).  The reference below is the list code path, which
is unchanged."""
from __future__ import annotations

import bisect
import random
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from mlaser.controller import stream as S
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.machine import preflight as PF
from mlaser.nc import compile_job
from mlaser.nc import compiler as C
from mlaser.nc.packed import PackedWire

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def _legacy(job) -> S.WireJob:
    """``WireJob.of`` as it was: every item a list, the groups from ``Job.groups()``."""
    per = float(getattr(job.machine, "period_ms", 1.0) or 1.0)
    starts, k = [], 0
    for grp in job.groups():
        starts.append(k)
        k += len(grp)
    return S.WireJob([list(w) for w in job.wire], per, float(job.duration_s or 0.0),
                     tuple(starts) if k == len(job.wire) else None)


def _sample_jobs():
    import make_goldens as G
    out = []
    for name, doc, _host in G.sample_docs():
        if name in ("chf/sample_v5", "dxf/basic_r2000", "dxf/gear", "dxf/inserts"):
            out.append((name, compile_job(doc)))
    for seed in (0, 2, 4):
        doc, cam, m, dry = G.random_doc(seed)
        out.append((f"seed{seed}", compile_job(doc, cam, m, dry_run=dry)))
    return out


@pytest.fixture(scope="module")
def jobs():
    return _sample_jobs()


def _prepared(job):
    js = S.JobStreamer.__new__(S.JobStreamer)
    js._prepared = None
    js._prepare(job)
    return js._prepared


def test_compiled_jobs_stay_packed_and_frame_identically(jobs):
    for name, job in jobs:
        assert isinstance(job.wire, PackedWire), name
        wj, ref = S.WireJob.of(job), _legacy(job)
        assert wj.packed and wj.wire is job.wire, name           # no copy, no lists
        for first in (0, 1, 77, 0xFFFFFFF0):                     # incl. the 32-bit wrap
            new = wj.frames(first)
            assert new == ref.frames(first) == job.frames(first), (name, first)
        assert job.frame_count() == len(job.frames()), name


def test_timing_frame_ends_and_seek_are_identical(jobs):
    rnd = random.Random(5)
    for name, job in jobs:
        _j, wj, cum, ctime, frames, frame_end, frame_bytes = _prepared(job)
        _j, rwj, rcum, rctime, rframes, rframe_end, rframe_bytes = _prepared(_legacy(job))
        assert isinstance(cum, np.ndarray) and isinstance(frames, S.PackedFrames), name
        assert cum.tolist() == rcum, name
        assert ctime.tolist() == rctime, name                    # the same floats, not "close"
        assert np.asarray(frame_end).tolist() == rframe_end, name
        assert np.asarray(frame_bytes).tolist() == rframe_bytes, name
        assert len(frames) == len(rframes), name
        # frames from a non-zero index (the order run_job sends them in, and out of order)
        frames.first = 1234
        for i in list(range(len(frames))) + rnd.sample(range(len(frames)), min(20, len(frames))):
            assert frames[i] == [(1234 + i) & 0xFFFFFFFF, *rframes[i][1:]], (name, i)
        # the executed-index seek of run_job (FIFO bytes done -> wire index): at every frame end,
        # one byte either side, and at random points
        probes = [0, 1, rcum[-1], rcum[-1] + 5]
        acc = 0
        for b in rframe_bytes:
            acc += b
            probes += [acc - 1, acc, acc + 1]
        probes += [rnd.randrange(rcum[-1] + 1) for _ in range(500)]
        for d in probes:
            assert S._search_left(cum, d) == bisect.bisect_left(rcum, d), (name, d)
        # wire_timing of the packed wire equals the list version
        a, b = S.wire_timing(job.wire, wj.period_ms)
        assert a.tolist() == rcum and b.tolist() == rctime


def test_odd_periods_accumulate_the_same_floats(jobs):
    _name, job = jobs[0]
    for per in (0.1, 0.8, 1.0 / 3.0, 2.5):
        a, b = S.wire_timing(job.wire, per)
        ra, rb = S.wire_timing([list(w) for w in job.wire], per)
        assert a.tolist() == ra and b.tolist() == rb, per


@pytest.mark.parametrize("seed", range(6))
def test_any_grouping_frames_as_before(seed):
    """Random packed wires, groups starting at item 0 or not (a wire whose groups do not cover
    every item is framed item by item, as the list path does)."""
    from mlaser.protocol import frames as F
    rnd = random.Random(seed)
    items, starts = [], []
    for i in range(rnd.randrange(1, 3000)):
        if rnd.random() < 0.3 or (i == 0 and seed % 2 == 0):
            starts.append(i)
        r = rnd.random()
        if r < 0.6 and items and rnd.random() < 0.5:
            items.append(list(items[-1]))                       # runs of identical items
        elif r < 0.8:
            items.append(F.encode_step(rnd.randint(-100, 100), rnd.randint(-100, 100), 5000, rnd.randint(0, 255)))
        else:
            items.append([F.OP_SYS | (rnd.randint(1, 30) << 16)] + [rnd.randrange(1 << 32) for _ in range(rnd.randint(0, 6))])
    pw = PackedWire.from_items(items, starts)
    job = C.Job(wire=pw)
    job._groups = (pw, len(pw), "packed")
    wj, ref = S.WireJob.of(job), _legacy(job)
    assert wj.packed == bool(starts and starts[0] == 0)
    assert wj.frames(9) == ref.frames(9) == job.frames(9) if wj.packed else wj.frames(9) == ref.frames(9)
    p, r = _prepared(job), _prepared(ref)
    assert np.asarray(p[2]).tolist() == r[2] and np.asarray(p[3]).tolist() == r[3]
    assert np.asarray(p[5]).tolist() == r[5]


def test_extent_from_the_runs_is_the_extent_of_every_step(jobs):
    for name, job in jobs:
        lst = C.Job(wire=[list(w) for w in job.wire], machine=job.machine, start_pulses=job.start_pulses)
        assert PF.job_extent_mm(job) == PF.job_extent_mm(lst), name


def test_summary_counts_frames_without_building_them(jobs, monkeypatch):
    _name, job = jobs[1]
    n = len(job.frames())
    job._nframes = None
    monkeypatch.setattr(PackedWire, "iter_frames", lambda *a, **k: pytest.fail("frames built for a count"))
    assert f"{n} frames" in job.summary()
    assert job.frame_count() == n


def test_a_list_wire_job_streams_as_before():
    """A resume job (nc.resume: a list wire, no groups) takes the list path unchanged."""
    from mlaser.protocol import frames as F
    job = C.Job(wire=[F.encode_step(3, 1, 5000, 0, 5000) for _ in range(500)])
    wj = S.WireJob.of(job)
    assert not wj.packed and wj.frames(3) == job.frames(3)


# ------------------------------------------------------------------ on the (simulated) card
class _Recorder:
    """The frames the simulated card received (it runs in this process, also for the child)."""

    def __init__(self, monkeypatch):
        self.frames: list = []
        real = CardSimulator._fifo_frame

        def rec(sim, words):
            self.frames.append(list(words))
            return real(sim, words)
        monkeypatch.setattr(CardSimulator, "_fifo_frame", rec)


def _card_job():
    import make_goldens as G
    doc, cam, m, _dry = G.random_doc(4)
    return compile_job(doc, cam, m, dry_run=True)


def _resume_job(job):
    from mlaser.nc.resume import resume_job
    return resume_job(job, len(job.wire) // 2, mode="exact")


def test_thread_streamer_sends_the_jobs_frames(monkeypatch):
    rec = _Recorder(monkeypatch)
    job = _card_job()
    sim = CardSimulator(time_scale=8.0).start()
    try:
        sim.state.fifo_seq = 41
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            js = S.JobStreamer(mc)
            js.prepare(job)
            assert isinstance(js._prepared[4], S.PackedFrames)
            js.run_job(job, timeout_s=60)
            assert rec.frames == job.frames(1)          # the job reset numbers them from 1
            rec.frames.clear()
            # a resume from a breakpoint in the middle (list wire, the unchanged path)
            rj = _resume_job(job)
            js.run_job(rj, timeout_s=60)
            assert rec.frames == rj.frames(1)
    finally:
        sim.stop()


ARMED = {"armed": True}


def test_process_streamer_hands_the_wire_over_in_shared_memory(monkeypatch):
    rec = _Recorder(monkeypatch)
    job = _card_job()
    blocks = []
    real_share = S.WireJob.share

    def share(self):
        sh = real_share(self)
        blocks.append(sh)
        return sh
    monkeypatch.setattr(S.WireJob, "share", share)
    sim = CardSimulator(time_scale=8.0).start()
    try:
        sim.state.fifo_seq = 7
        ps = S.ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0, policy=ARMED)
        stats = ps.run_job(job, timeout_s=60)
        assert stats.items == len(job.wire)
        assert rec.frames == job.frames(1)
        assert not ps.proc.is_alive()
        # the hand-off was a shared block (a handle was pickled, not the items), freed afterwards
        assert len(blocks) == 1 and blocks[0].shared is not None and blocks[0]._shm is None
        from multiprocessing import shared_memory
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=blocks[0].shared.name)
    finally:
        sim.stop()


def test_process_streamer_frees_the_block_when_the_child_dies(monkeypatch):
    """A child that dies before it attached (here: killed at once) is a fault, and the block is
    freed by the parent's finally."""
    job = _card_job()
    blocks = []
    real_share = S.WireJob.share

    def share(self):
        sh = real_share(self)
        blocks.append(sh)
        return sh
    monkeypatch.setattr(S.WireJob, "share", share)
    sim = CardSimulator(time_scale=1.0).start()
    try:
        ps = S.ProcessStreamer(TransportConfig(host="127.0.0.1", port=sim.port), 1000.0, policy=ARMED)
        real_start = type(ps.ctx.Process(target=print)).start

        def start_and_kill(proc):
            real_start(proc)
            proc.kill()
        monkeypatch.setattr(type(ps.ctx.Process(target=print)), "start", start_and_kill)
        with pytest.raises(S.StreamFault) as ei:
            ps.run_job(job, timeout_s=60)
        assert ei.value.kind == "child_died"
        assert blocks and blocks[0]._shm is None
        from multiprocessing import shared_memory
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=blocks[0].shared.name)
        assert sim.requests == 0
    finally:
        sim.stop()


def test_a_missing_shared_block_is_a_clear_fault_and_nothing_streams():
    """The child cannot attach (the block is gone): it says so in its hello and opens no socket;
    the parent faults the job (kind "handoff").  Fail safe: no frame reaches the card."""
    import multiprocessing as mp
    import queue
    from dataclasses import asdict
    job = _card_job()
    sh = S.WireJob.of(job).share()
    handoff = S.WireJob.__new__(S.WireJob)
    handoff.__setstate__(sh.__getstate__())           # what the child unpickles
    sh.release()                                       # ... and the block is gone
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        evt_q: queue.Queue = queue.Queue()
        S._child_main(asdict(cfg), 1000.0, handoff, {}, queue.Queue(), evt_q, S._shared_flags(mp),
                      {**ARMED, "parent_timeout_s": 60.0, "proto": S.STREAM_PROTO})
        events = []
        while not evt_q.empty():
            events.append(evt_q.get())
        assert len(events) == 1 and events[0][0] == "hello"
        hello = events[0][1]
        assert hello["ok"] is False and hello["kind"] == "handoff"
        assert sim.requests == 0, "the child talked to the card without its job"
        ps = S.ProcessStreamer(cfg, 1000.0)
        ps.flags = S._shared_flags(mp)
        with pytest.raises(S.StreamFault) as ei:
            ps._check_hello(events[0])
        assert ei.value.kind == "handoff" and "streaming process" in str(ei.value)
        assert ps.flags["cancel"].value == 1
    finally:
        sim.stop()


def test_parent_death_watch_does_not_depend_on_the_hand_off(monkeypatch):
    """The child's watchdog (parent gone / heartbeat stopped -> its own halt) runs as before with
    a packed, shared-memory job: here the parent's heartbeat never comes."""
    import multiprocessing as mp
    import queue
    from dataclasses import asdict
    job = _card_job()
    sh = S.WireJob.of(job).share()
    handoff = S.WireJob.__new__(S.WireJob)
    handoff.__setstate__(sh.__getstate__())
    sim = CardSimulator(time_scale=1.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        evt_q: queue.Queue = queue.Queue()
        t = threading.Thread(target=S._child_main, args=(
            asdict(cfg), 1000.0, handoff, {}, queue.Queue(), evt_q, S._shared_flags(mp),
            {**ARMED, "parent_timeout_s": 0.3, "proto": S.STREAM_PROTO}), daemon=True)
        t.start()
        t.join(20)
        assert not t.is_alive()
        events = []
        while not evt_q.empty():
            events.append(evt_q.get())
        assert events[0][0] == "hello" and events[0][1]["ok"]
        kinds = [e[0] for e in events]
        assert "fault" in kinds and "halted" in kinds
        f = next(e for e in events if e[0] == "fault")
        assert f[1] == "parent_lost"
    finally:
        sh.release()
        sim.stop()
