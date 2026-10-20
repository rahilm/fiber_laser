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


# ----------------------------------------------------------------------------- crtmath
from mlaser.parity_ports import crtmath as CRT  # noqa: E402
from mlaser.parity_ports.cadmodule import curvetable as CT  # noqa: E402
from mlaser.parity_ports.cadmodule import smooth as SM  # noqa: E402
from mlaser.parity_ports.profiles import native_smooth as NS  # noqa: E402


def _hx(v):
    if isinstance(v, float):
        return v.hex()
    if isinstance(v, (list, tuple)):
        return [_hx(x) for x in v]
    return v


any_float = st.one_of(st.floats(allow_nan=True, allow_infinity=True, allow_subnormal=True),
                      st.floats(-2.0, 2.0), st.floats(-1e-300, 1e-300),
                      st.sampled_from([0.0, -0.0, 1.0, -1.0, 0.5, 2.0 ** 512, 2.0 ** -460, 5e-324,
                                       1.0000000000000002, 0.9999999999999999]))


@needs_kernel
@settings(max_examples=3000, deadline=None)
@given(x=any_float, y=any_float)
def test_crtmath_matches_the_port(x, y):
    assert _hx(K.crt_hypot(x, y)) == _hx(CRT.hypot(x, y))
    assert _hx(K.crt_atan2(y, x)) == _hx(CRT.atan2(y, x))
    assert _hx(K.crt_atan(x)) == _hx(CRT.atan(x))
    assert _hx(K.crt_acos(x)) == _hx(CRT.acos(x))
    assert _hx(NS.hypot(x, y)) == _hx(CRT.hypot(x, y)) and _hx(NS.acos(x)) == _hx(CRT.acos(x))


@needs_kernel
def test_crtmath_wrappers_keep_ints_on_the_port():
    assert NS.hypot(3, 0) == 3 and type(NS.hypot(3, 0)) is int      # the port returns the int
    assert NS.atan2(1, 1) == CRT.atan2(1, 1) and NS.atan(2) == CRT.atan(2)
    with pytest.raises(TypeError):
        K.crt_hypot(3, 4.0)


# ----------------------------------------------------------------------------- CurveTable
@st.composite
def table_v(draw):
    n = draw(st.integers(4, 9))
    x = y = 0.0
    ctrl = []
    for _ in range(n):
        x += draw(st.floats(-3.0, 3.0))
        y += draw(st.one_of(st.floats(-3.0, 3.0), st.just(0.0)))
        ctrl.append((x, y))
    kind = draw(st.integers(0, 3))
    if kind == 0 or n == 4:
        knots = SM._clamped_uniform(n, 3)
    elif kind == 1:
        inner = sorted(draw(st.lists(st.floats(0.0, 1.0), min_size=n - 4, max_size=n - 4)))
        knots = [0.0] * 4 + inner + [1.0] * 4
    elif kind == 2:
        knots = [0.0, 0.0, 0.0, 0.0] + [0.5] * (n - 4) + [1.0] * 4      # repeated interior knots
    else:
        knots = [0.0] * 4 + [float(i) for i in range(1, n - 3)] + [float(n - 3)] * 4
    if n == 4 and draw(st.booleans()):
        knots = [0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0]               # a DXF spline glyph
    return ctrl, knots, draw(st.sampled_from([0.0, 0.3, 18.16]))


frac_v = st.one_of(st.floats(-0.2, 1.2), st.sampled_from([0.0, 1.0, 0.5, float("nan")]))


def _records(t):
    if isinstance(t, NS.NativeCurveTable):
        return _hx([list(v) for v in t._t.records()])
    return _hx([[getattr(r, f) for f in CT._Rec.__slots__] for r in t.recs])


def _outcome(fn, *a):
    try:
        return ("ok", _hx(fn(*a)))
    except Exception as e:  # noqa: BLE001 - the port's exception is the behaviour
        return (type(e).__name__,)


@needs_kernel
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=table_v(), fs=st.lists(frac_v, min_size=1, max_size=12),
       win=st.tuples(st.floats(0.0, 0.4), st.floats(0.6, 1.0)))
def test_curve_table_matches_the_port(case, fs, win):
    ctrl, knots, ref = case
    a = _outcome(CT.CurveTable, ctrl, knots, 0.0, 1.0, ref)
    b = _outcome(NS.NativeCurveTable, ctrl, knots, 0.0, 1.0, ref)
    if a[0] != "ok":
        assert b[0] == a[0]
        return
    pa, pb = CT.CurveTable(ctrl, knots, 0.0, 1.0, ref), NS.NativeCurveTable(ctrl, knots, 0.0, 1.0, ref)
    assert _records(pa) == _records(pb)
    assert _hx((pa.total, pa.length, pa.sample_tol)) == _hx((pb.total, pb.length, pb.sample_tol))
    # the lazily materialised records are the port's
    assert _hx([[getattr(r, f) for f in CT._Rec.__slots__] for r in pb.recs]) == _records(pa)
    for u0, u1 in ((0.0, 1.0), win):
        wa, wb = pa.windowed(u0, u1), pb.windowed(u0, u1)
        assert type(wb) is type(pb)
        assert _outcome(wa.nodes) == _outcome(wb.nodes)
        for f in fs:
            for name in ("point_f", "tangent_f", "radius_at", "point_at_fraction"):
                assert _outcome(getattr(wa, name), f) == _outcome(getattr(wb, name), f), name
            assert _outcome(wa.point, f * wa.length) == _outcome(wb.point, f * wb.length)
            assert _outcome(wa.tangent, f * wa.length) == _outcome(wb.tangent, f * wb.length)
            assert wa._search(f * wa.total) == wb._search(f * wb.total)


@needs_kernel
def test_curve_table_falls_back_to_the_port():
    ints = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)]
    t = NS.NativeCurveTable(ints, SM._clamped_uniform(5, 3))
    assert type(t) is CT.CurveTable              # an int control point: the port's own table
    before = NS.STATS["table_port"]
    NS.NativeCurveTable([(0.0, 0.0)] * 5, SM._clamped_uniform(5, 3))
    assert NS.STATS["table_port"] == before      # all points equal: C handles it (no records)


# ----------------------------------------------------------------------------- smoother
class _Mv:
    def __init__(self, a, b, c=None, ccw=True, feed=100.0, laser=True):
        self.start, self.end, self.center, self.ccw = a, b, c, ccw
        self.feed, self.laser = feed, laser


def _chain(r: random.Random):
    pts = [(0.0, 0.0)]
    mv = []
    for _ in range(r.randint(1, 10)):
        a = pts[-1]
        k = r.random()
        if k < 0.6:
            L = r.choice((r.uniform(0.01, 0.4), r.uniform(0.5, 3.0), r.uniform(3.0, 30.0)))
            ang = r.uniform(0, 2 * math.pi)
            b = (a[0] + L * math.cos(ang), a[1] + L * math.sin(ang))
            mv.append(_Mv(a, b, feed=r.choice((50.0, 100.0, 100.0, 300.0)), laser=r.random() < 0.9))
        else:
            rad = r.uniform(0.3, 12.0)
            ang = r.uniform(0, 2 * math.pi)
            c = (a[0] - rad * math.cos(ang), a[1] - rad * math.sin(ang))
            sw = r.uniform(0.2, 6.2)
            ccw = r.random() < 0.5
            e = ang + (sw if ccw else -sw)
            b = (c[0] + rad * math.cos(e), c[1] + rad * math.sin(e))
            mv.append(_Mv(a, b, c, ccw, feed=r.choice((60.0, 100.0))))
        pts.append(mv[-1].end)
    if r.random() < 0.3:      # a closed contour (full circle)
        c = (pts[-1][0] - 4.0, pts[-1][1])
        mv.append(_Mv(pts[-1], pts[-1], c, True))
    return mv


@needs_kernel
@pytest.mark.parametrize("seed", range(60))
def test_smoother_matches_the_port(seed):
    from mlaser.motion.interp import MotionConfig
    from mlaser.parity_ports.cadmodule.planner import PlannerParams, plan_velocity
    r = random.Random(seed)
    mv = _chain(r)
    cfg = MotionConfig(corner_tol=r.choice((0.05, 0.01, 0.2)), arc2seg_k=r.choice(((1.0, 1.0), (0.9, 0.7))))
    li = r.choice((None, 1)) if len(mv) > 1 else None
    a = _outcome(SM.smooth_path_points, mv, cfg, None, li)
    b = _outcome(NS.native_smooth_path_points, mv, cfg, None, li)
    assert a[0] == b[0]
    if a[0] != "ok":
        return
    pa, ga = SM.smooth_path_points(mv, cfg, None, li)
    pb, gb = NS.native_smooth_path_points(mv, cfg, None, li)
    assert _hx([(p.s, p.radius, p.feed, p.factor) for p in pa]) == _hx([(p.s, p.radius, p.feed, p.factor) for p in pb])
    assert _hx(ga.cum) == _hx(gb.cum)
    plan = plan_velocity(pa, PlannerParams())
    for period in (1.0, 0.5):
        ss = plan.sample(period)
        sb = gb.sample_plan(plan, period)
        assert _hx(list(sb)) == _hx(ss)
    ss = plan.sample(1.0)
    extra = [-1.0, 0.0, ga.cum[-1], ga.cum[-1] + 1.0] + [r.uniform(0, ga.cum[-1]) for _ in range(20)]
    for s_list in (ss, extra):
        want = [c for s in s_list for c in ga.at(s)[0]]
        got = gb.points(array("d", s_list))
        assert type(got) is array and _hx(list(got)) == _hx(want)
        assert _hx([c for s in s_list for c in gb.at(s)[0]]) == _hx(want)
        assert [ga.at(s)[1] for s in s_list] == [gb.at(s)[1] for s in s_list]


@needs_kernel
def test_sampling_declines_odd_inputs_and_gives_the_ports_values():
    from mlaser.motion.interp import MotionConfig
    mv = [_Mv((0, 0), (10, 0)), _Mv((10, 0), (10, 10)), _Mv((10, 10), (0, 10), (5, 10), True)]   # ints
    pa, ga = SM.smooth_path_points(mv, MotionConfig())
    pb, gb = NS.native_smooth_path_points(mv, MotionConfig())
    ss = [0.0, 1.5, 7.25, 30.0]
    assert _hx([c for s in ss for c in ga.at(s)[0]]) == _hx(list(gb.points(ss)))


# ----------------------------------------------------------------------------- ring sort
from mlaser.parity_ports.cadmodule import ordering as ORD  # noqa: E402
from mlaser.parity_ports.cadmodule import ordering_fast as ORDF  # noqa: E402


class _Node:
    def __init__(self, bbox):
        self.bbox = bbox


@st.composite
def ring_nodes(draw):
    kind = draw(st.integers(0, 3))
    n = draw(st.integers(2, 60))
    out = []
    for k in range(n):
        if kind == 0:        # floats anywhere
            x, y = draw(st.floats(-500, 500)), draw(st.floats(-500, 500))
        elif kind == 1:      # a grid: collinear rows / columns, the near-collinear rule
            x, y = float((k % 7) * 30), float((k // 7) * 30)
        elif kind == 2:      # duplicates and near-duplicates: the tolerance box
            x = float(draw(st.integers(0, 4))) * 10 + draw(st.sampled_from([0.0, 1e-9, -1e-9]))
            y = float(draw(st.integers(0, 4))) * 10
        else:                # a line with jitter: sin close to 0.1, nearly equal lengths
            x = k * 3.0 + draw(st.floats(-0.01, 0.01))
            y = k * 0.3 + draw(st.floats(-0.01, 0.01))
        w, h = draw(st.floats(0.0, 20.0)), draw(st.floats(0.0, 20.0))
        out.append(_Node((x - w, y - h, x + w, y + h)))
    return out


@needs_kernel
@settings(max_examples=400, deadline=None)
@given(nodes=ring_nodes(), inside_out=st.booleans(), faithful=st.booleans())
def test_ring_sort_matches_the_reference(nodes, inside_out, faithful):
    old = ORD.RING_TOL_FAITHFUL
    ORD.RING_TOL_FAITHFUL = faithful
    try:
        ref = ORD.ring_sort(nodes, inside_out)
        got = ORDF.ring_sort(nodes, inside_out)
    finally:
        ORD.RING_TOL_FAITHFUL = old
    assert [id(x) for x in got] == [id(x) for x in ref]


@needs_kernel
@settings(max_examples=200, deadline=None)
@given(nodes=ring_nodes())
def test_ring_kernel_quick_and_exact_paths_agree(nodes):
    import math
    P = [((x.bbox[0] + x.bbox[2]) / 2, (x.bbox[1] + x.bbox[3]) / 2) for x in nodes]
    flat = array("d", [v for p in P for v in p])
    x0 = min(x.bbox[0] for x in nodes)
    y0 = min(x.bbox[1] for x in nodes)
    args = (flat, 0.37, x0 - 1.0, y0, math.hypot)
    assert K.ring_order(*args, False) == K.ring_order(*args, True)


@needs_kernel
def test_ring_sort_large_grid_and_python_fallbacks():
    nodes = [_Node((float(i % 40) * 30, float(i // 40) * 30, float(i % 40) * 30 + 24, float(i // 40) * 30 + 24))
             for i in range(900)]
    assert [id(x) for x in ORDF.ring_sort(nodes)] == [id(x) for x in ORD.ring_sort(nodes)]
    ints = [_Node((i, 0, i + 2, 2)) for i in range(5)]       # int bboxes: centres are floats
    assert [id(x) for x in ORDF.ring_sort(ints, True)] == [id(x) for x in ORD.ring_sort(ints, True)]
    assert ORDF.ring_sort(nodes[:1]) == nodes[:1]


@needs_kernel
@pytest.mark.parametrize("what", ["ring", "table", "crt"])
def test_selfcheck_detects_wrong_geometry_kernels(monkeypatch, what):
    class Kern:
        def __getattr__(self, name):
            return getattr(K, name)

        def ring_order(self, *a):
            out = K.ring_order(*a)
            return out[:2][::-1] + out[2:] if what == "ring" else out

        def ct_build(self, ctrl, knots, tol):
            if what == "table":
                knots = list(knots)
                knots[4] = knots[4] * (1 + 2 ** -50)     # a last-bits difference in the table
            return K.ct_build(ctrl, knots, tol)

        def crt_acos(self, x):
            r = K.crt_acos(x)
            return math.nextafter(r, 4.0) if what == "crt" and x == 0.5 else r

    fake = Kern()
    monkeypatch.setattr(N, "_K", fake)
    monkeypatch.setattr(NS, "_K", fake)
    err = N.selfcheck()
    assert err and ("ring" in err if what == "ring" else "smoother" in err), err
