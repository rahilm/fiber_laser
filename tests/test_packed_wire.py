"""Packed wire storage (mlaser/nc/packed.py): the same items and frames as plain lists."""
from __future__ import annotations

import multiprocessing as mp
import pickle
from pathlib import Path

import pytest

from mlaser.nc import compiler as C
from mlaser.nc.packed import PackedWire, WireBuilder
from mlaser.protocol import frames as F

hyp = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

DATA = Path(__file__).parent / "data"




def _compile(doc, packed: bool):
    old = C.PACK_WIRE
    C.PACK_WIRE = packed
    try:
        return C.compile_job(doc)
    finally:
        C.PACK_WIRE = old


@pytest.fixture(scope="module")
def jobs():
    from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part
    def rect(x, y, w, h):
        p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)
    parts = [Part([Contour([Arc((x + 8.0, 5.0), (x + 8.0, 5.0), (x + 5.0, 5.0), True)], True), rect(x, 0, 10, 10)],
                  Craft(lead_in=LeadParams(kind=1, length=1.0))) for x in (0.0, 15.0, 30.0)]
    doc = Document(parts=parts)
    return _compile(doc, False), _compile(doc, True)


def test_compiled_packed_wire_is_identical(jobs):
    plain, packed = jobs
    assert isinstance(packed.wire, PackedWire) and isinstance(plain.wire, list)
    assert len(packed.wire) == len(plain.wire)
    assert packed.wire.tolist() == plain.wire
    assert packed.wire == plain.wire
    assert packed.groups() == plain.groups()
    assert packed.frames() == plain.frames()
    assert packed.frames(77) == plain.frames(77)
    assert list(packed.iter_frames(5)) == plain.frames(5)
    assert packed.markers == plain.markers and packed.cut_blocks == plain.cut_blocks
    assert packed.step_deltas() == plain.step_deltas() and packed.wire_bytes() == plain.wire_bytes()
    # sequence behaviour the streamer / resume code relies on
    n = len(plain.wire)
    for i in (0, 1, n // 2, n - 1, -1, -n):
        assert packed.wire[i] == plain.wire[i]
    assert packed.wire[10:250] == plain.wire[10:250] and packed.wire[n - 5:] == plain.wire[n - 5:]
    assert packed.wire[::7] == plain.wire[::7]
    with pytest.raises(IndexError):
        packed.wire[n]
    # runs: identical dwell steps stored once
    assert len(packed.wire.cnt) < len(plain.wire)
    assert packed.wire.nbytes < 40 * len(plain.wire)       # vs ~280 bytes per list item


def test_settle_dwell_is_one_run(jobs):
    _plain, packed = jobs
    pw = packed.wire
    assert int(pw.cnt.max()) >= 200          # the 200 ms JumpAddTime settle: one item x 200


@st.composite
def item_lists(draw):
    items, starts = [], []
    for i in range(draw(st.integers(0, 400))):
        if draw(st.booleans()) or not items:
            starts.append(i)
        if items and draw(st.integers(0, 3)) == 0:
            items.append(list(items[-1]))                      # repeats -> runs
        else:
            items.append([draw(st.sampled_from([F.OP_STEP | 0x80000, 0x10270f, 0x67, 0xbb9]))]
                         + draw(st.lists(st.integers(0, 0xFFFFFFFF), max_size=draw(st.integers(0, 9)))))
    return items, starts


@settings(max_examples=150, deadline=None)
@given(item_lists(), st.sampled_from([0x4B0, 64, 40, 2000]), st.integers(0, 2**32 + 5))
def test_frames_match_build_fifo_frames(data, max_bytes, seq):
    items, starts = data
    pw = PackedWire.from_items(items, starts)
    assert pw.tolist() == items
    st_ = sorted(set(starts)) + [len(items)]
    groups = [items[a:b] for a, b in zip(st_, st_[1:]) if b > a]
    assert pw.groups() == groups
    assert pw.frames(seq, max_bytes) == F.build_fifo_frames(groups, seq, max_bytes)


def test_builder_falls_back_to_lists_for_odd_items():
    b = WireBuilder()
    rec = b.starts()
    rec.append(0); b.append([1, 2])
    rec.append(1); b.append([3, 2 ** 70])                      # not a 64-bit int
    b.append([4])
    wire, starts = b.finish()
    assert wire == [[1, 2], [3, 2 ** 70], [4]] and starts == [0, 1]


def _child(handle_bytes, q):
    shared = pickle.loads(handle_bytes)
    job = C.Job.from_shared(shared)
    q.put((job.frames(), len(job.wire), job.steps, job.markers))
    job.wire.close()


def test_shared_memory_hand_off(jobs):
    plain, packed = jobs
    shared = packed.to_shared()
    try:
        blob = pickle.dumps(shared)
        assert len(blob) < 100_000                   # metadata only, not the items
        same = C.Job.from_shared(pickle.loads(blob), copy=True)
        assert same.wire == plain.wire and same.frames() == plain.frames()
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        p = ctx.Process(target=_child, args=(blob, q))
        p.start()
        frames, n, steps, markers = q.get(timeout=60)
        p.join(30)
        assert frames == plain.frames() and n == len(plain.wire) and steps == plain.steps
        assert markers == plain.markers
    finally:
        shared.release()
    # a plain-list Job can be shared too (packed on the way)
    s2 = plain.to_shared()
    try:
        assert C.Job.from_shared(s2, copy=True).frames() == plain.frames()
    finally:
        s2.release()
