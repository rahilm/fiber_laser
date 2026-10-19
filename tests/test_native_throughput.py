"""Review 2 throughput pieces of the native kernel against their Python references, randomised.

* the packed C sink (``_emitter.Sink`` behind ``WireBuilder(native=True)``) against the Python
  ``WireBuilder``, and the kernel writing its steps into it against the port's list wire;
* ``NativeEmitter.to_path`` (a rapid in one call) and flat ``array('d')`` sample buffers;

Everything is compared exactly (``float.hex``, PackedWire arrays and dtypes).
docs/dev/NATIVE_KERNEL.md, docs/dev/PERFORMANCE.md.
"""
from __future__ import annotations

import math
import random
from array import array
from types import SimpleNamespace

import numpy as np
import pytest

import mlaser.parity_ports.profiles.native as N
from mlaser._native import _emitter as K
from mlaser.nc.packed import PackedWire, WireBuilder
from mlaser.parity_ports.mainapp import sampling as S

needs_kernel = pytest.mark.skipif(K is None, reason="mlaser._native not built (no C compiler?)")
hyp = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402


def _machine(ppx=1000.0, ppy=1000.0, max_step=127, period=1.0):
    return SimpleNamespace(pulses_per_mm_x=ppx, pulses_per_mm_y=ppy, max_step=max_step, period_ms=period)


def _arrays(w):
    if isinstance(w, PackedWire):
        return ("packed", w.words.dtype.str, w.words.tolist(), w.woff.tolist(), w.cnt.tolist(), w.flag.tolist())
    return ("plain", [list(x) for x in w])


# ----------------------------------------------------------------------------- the packed sink
word_v = st.one_of(st.integers(0, 5), st.integers(0, 0xFFFFFFFF), st.integers(-2**63, 2**63 - 1),
                   st.booleans(), st.sampled_from([0xFFFFFFFF, 2**32, -1]))
item_v = st.lists(word_v, min_size=0, max_size=5)
odd_v = st.sampled_from([[1.5], ["x"], [2**64], [-(2**63) - 1], [None]])


@needs_kernel
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(st.one_of(
    st.tuples(st.just("item"), item_v),
    st.tuples(st.just("again"), st.just(None)),
    st.tuples(st.just("start"), st.integers(-1, 2)),
    st.tuples(st.just("odd"), odd_v)), max_size=60))
def test_sink_is_the_python_builder(ops):
    """Same items, runs, flags, dtypes, plain fallback and starts as the Python WireBuilder."""
    outs = []
    for native in (False, True):
        b = WireBuilder(native=native)
        assert (b.native_sink is not None) == native
        rec = b.starts()
        last = [0]
        lens = []
        for kind, v in ops:
            if kind == "item":
                last = v
                b.append(list(v))
            elif kind == "again":
                b.append(list(last))
            elif kind == "start":
                rec.append(len(b) + v)          # only len(b) itself marks a start
            else:
                b.append(list(v))
            lens.append(len(b))
        wire, starts = b.finish()
        outs.append((_arrays(wire), starts, lens))
    assert outs[0] == outs[1]


@needs_kernel
def test_sink_finish_closes_it():
    s = K.Sink()
    s.append([1, 2, 3])
    s.finish()
    with pytest.raises(RuntimeError):
        s.append([1])
    with pytest.raises(RuntimeError):
        s.finish()


# ----------------------------------------------------------------------------- kernel + sink
def _stream(seed):
    """A random emit / to / dwell / raw / group stream (the compiler's calls)."""
    r = random.Random(seed)
    ops = []
    x = y = 0.0
    for _ in range(r.randint(1, 12)):
        k = r.random()
        if k < 0.45:
            pts, ss, s = [], [], 0.0
            for _ in range(r.randint(0, 60)):
                x += r.uniform(-0.04, 0.04) * r.choice((1, 1, 10))
                y += r.uniform(-0.04, 0.04)
                s += r.uniform(0, 0.06)
                pts.append((x, y))
                ss.append(s)
            kw = {}
            if ss and r.random() < 0.4:
                kw["gaps"] = [(r.uniform(0, s), r.uniform(0, s + 1))]
                kw["micro_pct"] = r.choice((0, 0, 30))
            ops.append(("emit", r.random() < 0.8, r.randint(1, 100), r.randint(1, 30000), pts, ss, kw))
        elif k < 0.6:
            pts = []
            for _ in range(r.randint(0, 40)):
                x += r.uniform(-0.3, 0.3)
                y += r.uniform(-0.3, 0.3)
                pts.append((x, y))
            ops.append(("rapid", pts))
        elif k < 0.75:
            ops.append(("dwell", r.choice((0.0, 0.001, 0.0105, 0.2)), r.random() < 0.5))
        elif k < 0.9:
            ops.append(("raw", [r.randint(0, 2**32 - 1) for _ in range(r.randint(1, 4))]))
        else:
            ops.append(("group", [[3000 | (8 << 16), r.randint(0, 255), 0], [9999, 5, 6, 7]]))
    return ops


def _apply(em, emit, ops):
    for op in ops:
        if op[0] == "emit":
            _, laser, duty, freq, pts, ss, kw = op
            em.laser, em.duty, em.freq = laser, duty, freq
            emit(em, pts, ss, **kw)
        elif op[0] == "rapid":
            if hasattr(em, "to_path"):
                em.to_path(op[1], False)
            else:
                for x, y in op[1]:
                    em.to(x, y, False)
        elif op[0] == "dwell":
            em.dwell(op[1], op[2])
        elif op[0] == "raw":
            em.raw(op[1])
        else:
            em.group(op[1])
    return (em.pos[0].hex(), em.pos[1].hex(), float(em.carry[0]).hex(), float(em.carry[1]).hex(),
            tuple(em.cur), em.steps, em.overspeed, float(em.on_len).hex())


@needs_kernel
@pytest.mark.parametrize("seed", range(40))
def test_kernel_into_the_sink_matches_the_port(seed):
    r = random.Random(seed * 7 + 1)
    m = _machine(r.choice((1000.0, 2500.0 / 3, 409.6, 100.0)), r.choice((1000.0, 1234.5678)),
                 r.choice((127, 40, 127.0)))
    carry = (r.uniform(-0.99, 0.99), r.uniform(-0.99, 0.99))
    ops = _stream(seed)
    ref = S.Emitter(m, (0.0, 0.0), r.random() < 0.1, carry)
    a = _apply(ref, S.emit_samples, ops)
    before = dict(N.STATS)
    em = N.NativeEmitter(m, (0.0, 0.0), ref.dry, carry)
    b = WireBuilder(native=True)
    em.wire, em.starts = b, b.starts()
    assert _apply(em, N.emit_samples, ops) == a
    assert N.STATS["emit_port"] == before["emit_port"] and N.STATS["dwell_port"] == before["dwell_port"]
    wire, plain = b.finish()
    assert plain is None and _arrays(wire) == _arrays(PackedWire.from_items(ref.wire, ref.starts))


@needs_kernel
def test_kernel_undoes_its_sink_output_when_it_hands_a_call_to_the_port():
    """A step beyond int8 (max_step 200) raises the port's ValueError; the kernel must leave the
    sink exactly as before the call (also the run count of a merged last run)."""
    m = _machine(1000.0, 1000.0, max_step=200)
    outs = []
    for native in (False, True):
        em = (N.NativeEmitter if native else S.Emitter)(m, (0.0, 0.0), False)
        b = WireBuilder(native=native)
        em.wire, em.starts = b, b.starts()
        em.laser = True
        em.dwell(0.005, True)                    # a run of 5 equal steps
        with pytest.raises(ValueError):
            (N.emit_samples if native else S.emit_samples)(
                em, [(0.0, 0.0), (0.001, 0.0), (0.15, 0.0)], [0.0, 0.001, 0.15])
        em.dwell(0.003, True)
        outs.append(_arrays(b.finish()[0]))
    assert outs[0] == outs[1]


@needs_kernel
def test_flat_sample_buffers_are_the_tuples():
    r = random.Random(5)
    pts = [(r.uniform(-1, 1), r.uniform(-1, 1)) for _ in range(300)]
    ss = sorted(r.uniform(0, 30) for _ in range(300))
    from mlaser.nc.process import SpeedCurve
    kw = dict(gaps=[(3.0, 4.5)], dcurve=SpeedCurve(100.0, 80, [(0, 10), (50, 90)], True),
              fcurve=SpeedCurve(100.0, 5000, [(0, 1000), (60, 9000)], True),
              up=(2.0, 0, 11, 1500, True), down=(1.0, 0, 13, 1700, True), micro_pct=40)
    outs = []
    for flat in (False, True):
        em = N.NativeEmitter(_machine(), (0.0, 0.0), False)
        em.laser = True
        before = N.STATS["emit_port"]
        if flat:
            N.emit_samples(em, array("d", [v for p in pts for v in p]), array("d", ss), **kw)
        else:
            N.emit_samples(em, pts, ss, **kw)
        assert N.STATS["emit_port"] == before
        outs.append(([list(w) for w in em.wire], em.starts, em.steps, float(em.carry[0]).hex(),
                     float(em.on_len).hex()))
    assert outs[0] == outs[1]
    ref = S.Emitter(_machine(), (0.0, 0.0), False)
    ref.laser = True
    S.emit_samples(ref, pts, ss, **kw)
    assert outs[0][0] == ref.wire


@needs_kernel
def test_selfcheck_detects_a_wrong_sink(monkeypatch):
    class BadSink:
        def __init__(self):
            self._k = K.Sink()

        def __len__(self):
            return len(self._k)

        def append(self, item):
            self._k.append(item)

        def mark_start(self, i):
            pass                                  # loses the group starts of raw items

        def finish(self):
            return self._k.finish()

        @property
        def pending(self):
            return self._k.pending

    class Kern:
        Sink = BadSink

        def __getattr__(self, name):
            return getattr(K, name)

    monkeypatch.setattr(N, "_K", Kern())
    # the steps go to the real sink only when it is the kernel's own type: the bad one makes the
    # kernel decline, which the self-check reports as well
    assert N.selfcheck() != ""
