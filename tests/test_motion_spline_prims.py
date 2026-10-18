"""Spline primitives in CContoutSmooth (parity JC-07, agent W).

Reference values are the original's (CADModule.dll under Wine, harness work/V/h/probe.c:
the analyser table of a spline piece and the smoother's nodes vt[5] for the spline_ellipse
sample with the dialog lead and start 0.125)."""
from __future__ import annotations

import math

import pytest

from mlaser.cam.items import CutLine
from mlaser.cam.toolpath import CamParams, prepare_contour
from mlaser.formats import chf as C
from mlaser.formats.scio import ScReader, ScWriter
from mlaser.geom.curves import glyph_segments
from mlaser.model import GLYPH_SPLINE, Contour, Craft, Glyph, LeadParams
from mlaser.motion import curvetable as CT
from mlaser.motion.interp import lead_in_index
from mlaser.motion.smooth import ContourSmoother

QUARTER = [(40, 67.5), (31.715729, 67.5), (25, 64.142136), (25, 60)]   # glyph 1 (6-decimal .chf)
KNOTS = [0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0]


def test_bezier_span_is_on_evaluatebezier():
    # FUN_10057270: the same point as the basis path up to rounding, derivatives of a cubic
    cur = CT._Nurbs(QUARTER, KNOTS)
    for t in (0.0, 0.3, 1.0, 1.7, 2.0):
        (x, y), d1, d2 = cur.evaluate(t, 2)
        s = t / 2.0
        b = [(1 - s) ** 3, 3 * s * (1 - s) ** 2, 3 * s * s * (1 - s), s ** 3]
        assert x == pytest.approx(sum(bi * p[0] for bi, p in zip(b, QUARTER)), abs=1e-12)
        assert y == pytest.approx(sum(bi * p[1] for bi, p in zip(b, QUARTER)), abs=1e-12)
        db = [-3 * (1 - s) ** 2, 3 * (1 - s) ** 2 - 6 * s * (1 - s), 6 * s * (1 - s) - 3 * s * s, 3 * s * s]
        assert d1[0] == pytest.approx(sum(bi * p[0] for bi, p in zip(db, QUARTER)) / 2.0, abs=1e-9)


def test_spline_piece_table_is_bit_identical():
    # FUN_100f92b0(piece, glyph spline, glyph length, 0): sampled with tol min(L, 1) = 1 mm
    t = CT.CurveTable(QUARTER, KNOTS, ref=18.1649038299004)
    assert len(t.recs) == 47
    orig = {
        1: [0.043478260869565216, 39.461998167944024, 67.49526576354071, 0.5380296268060476,
            0.006585839385340249, -0.9998446892446874, -0.01762377340964325, 0.0005799338073920665,
            -0.03290122517786878, 30.25521843744142],
        23: [1, 29.393398374999997, 65.303301, 10.923856505441165, 0.012063647343922687,
             -0.8944271992375123, -0.44721357902476483, 0.02998155744449111, -0.059963117650257663,
             14.92638457708632],
        46: [2, 25, 60, 18.16843370708, 0.02590411584819951, 0, -1, 0.26094753303166957, 0,
             3.8387111909228864],
    }
    for k, v in orig.items():
        r = t.recs[k]
        assert [r.u, r.x, r.y, r.s, r.f, r.tx, r.ty, r.kx, r.ky, r.R] == v
    # a blend keeps the 0.05 mm sampling (ref <= 0)
    assert CT.CurveTable(QUARTER, KNOTS).sample_tol == CT.SAMPLE_TOL


ORIG_NODES = [(10000, 0, 1), (10000, 1.4943432321114158, 1), (0.006907830287430664, 2.9886864642228317, 1), (0.007971286064497977, 3.000387442244441, 1), (0.05, 3.0067052092804567, 1), (19.432650379089417, 3.236626990151992, 1), (25.21300075135677, 6.445862012777065, 1), (29.280703795039948, 9.96080282105706, 1), (25.411874769073357, 11.711347277634465, 1), (26.433065962658464, 11.770589464277, 1), (27.09789236649941, 12.45805040883696, 1), (28.336452478200258, 15.24072936401589, 1), (23.694149592219038, 18.67411948000096, 1), (17.627457802609683, 21.78593708522058, 1), (10.747257817003351, 24.888243685000305, 1), (5.104548722602942, 28.071486836233973, 1), (3.1882235828696674, 29.879779354983967, 1), (3.316416245873457, 29.939015072523524, 1), (3.399834001896185, 30.62637963664553, 1), (6.331912993328892, 33.316247360544835, 1), (12.344830738806511, 36.345784329325554, 1), (19.432650379089623, 39.57338829531004, 1), (25.21300075135681, 42.78262331793512, 1), (29.280703795039855, 46.29756412621513, 1), (25.41187476980286, 48.04810858279253, 1), (26.433065963290776, 48.10735076943507, 1), (27.097892365769813, 48.79481171399501, 1), (28.336452478200368, 51.57749066917391, 1), (23.694149592219112, 55.01088078515899, 1), (17.627457802609612, 58.1226983903786, 1), (10.74725781700339, 61.22500499015828, 1), (5.104548722602954, 64.40824814139197, 1), (3.1882235828614003, 66.21654066014197, 1), (3.316416245862974, 66.27577637768152, 1), (3.3998340019013527, 66.96314094180354, 1), (6.331912993328896, 69.65300866570286, 1), (12.344830738806486, 72.68254563448356, 1), (12.440088522403304, 75.36288555361158, 1), (0.06379481869877768, 75.6443110900265, 1), (143.8956312718332, 76.19169346297045, 1), (10000, 77.40885726178182, 1), (10000, 78.62602106059317, 1)]


def _ellipse_moves():
    """spline_ellipse.dxf #0 as the parity harness feeds it: four single-span quarter splines,
    written to and read back from a .chf body (6 decimals), lead line 3 mm at 90 deg, start 0.125."""
    quarters = [
        [(55.0, 60.0), (55.0, 64.14213562373095), (48.2842712474619, 67.5), (40.0, 67.5)],
        [(40.0, 67.5), (31.715728752538098, 67.5), (25.0, 64.14213562373095), (25.0, 60.0)],
        [(25.0, 60.0), (25.0, 55.85786437626905), (31.715728752538098, 52.5), (40.0, 52.5)],
        [(40.0, 52.5), (48.284271247461895, 52.5), (55.0, 55.85786437626905), (55.0, 60.0)],
    ]
    glys = [Glyph(GLYPH_SPLINE, {"u0": 0, "u1": 4, "ctrl": c4, "knots": list(KNOTS)}) for c4 in quarters]
    ct = Contour([s for g in glys for s in glyph_segments(g)], True)
    ct.set_source_glyphs(glys)
    line3 = LeadParams(1, 90.0, 3.0, 2.0)
    cr = Craft(lead_in=line3, lead_out=LeadParams(1, 90.0, 3.0, 2.0), extra={"start_param": 0.125})
    c = C.chf_from_contour(ct)
    C._apply_craft(c, cr)
    c.flag88 = 1
    w = ScWriter()
    C._write_contour(w, c, 9)
    w.eof()
    rc = C._read_contour(ScReader(w.getvalue()), 9)
    ct2, cr2 = C.contour_from_chf(rc), C.craft_from_chf(rc, 1)
    pc = prepare_contour(ct2, cr2, 1, CamParams())
    moves = []
    for (seg, on), ref in zip(pc.pieces, pc.curves):
        mv = CutLine(seg.start, seg.end, 100.0, 1, on)
        if ref is not None:
            mv.curve = ref
        moves.append(mv)
    for mv in moves[:pc.lead_in_count]:
        mv.lead = "in"
    return moves


def test_spline_primitives_give_the_original_nodes():
    moves = _ellipse_moves()
    assert sum(1 for m in moves if getattr(m, "curve", None) is not None) > 100
    sm = ContourSmoother().load(moves, 0.05, True, 1.0, 1.0, lead_in_index(moves)).run()
    kinds = [p.kind for p in sm.prims]
    assert kinds == ["line", "spline", "spline", "spline", "spline", "spline", "line"]
    # windows (fractions of each glyph) as FUN_1006f2d0 cuts them
    assert [p._f[0] for p in sm.prims[2:5]] == [0.0, 0.0, 0.0]
    assert [p._f[1] for p in sm.prims[1:5]] == [1.0, 1.0, 1.0, 1.0]
    assert sm.prims[1]._f[0] == sm.prims[5]._f[1]
    assert len(sm.pieces) == 13
    nodes = [(n.radius, n.s, n.factor) for n in sm.nodes()]
    assert nodes == [tuple(float(x) for x in n) for n in ORIG_NODES]


def test_sub_path_keeps_segment_ends_exactly():
    # parity JC-13 (polyline start 0.3): the last piece ends at the stored start point itself,
    # not at a + ((acc + sl) - acc)/sl of it
    from mlaser.geom.contour import rotate_start, sub_path
    from mlaser.model import Line
    c = Contour([Line((0.0, 0.0), (40.0, 0.0)), Line((40.0, 0.0), (40.0, 20.0)),
                 Line((40.0, 20.0), (0.0, 20.0)), Line((0.0, 20.0), (0.0, 0.0))], True)
    body = rotate_start(c, 38.137167)
    L = body.length()
    segs = sub_path(body, 0.0, L)
    assert segs[-1].end == body.segments[-1].end == (38.137167, 0.0)
    assert segs[0].start == body.segments[0].start
