"""CAM geometry fixes found by the CADModule job harness (docs/re/parity.md §9.Q1, JC-02..JC-11)
and the faithful mode-5 sort in the toolpath."""
import math

import pytest

from mlaser.cam import ordering as O
from mlaser.cam.geometry import CurveRef, SplineCurve
from mlaser.geom.contour import stored_length
from mlaser.toolpath.items import CutLine
from mlaser.cam.leads import start_tangents
from mlaser.cam.toolpath import CamParams, build_toolpath, prepare_contour
from mlaser.model import GLYPH_LWPOLY, GLYPH_SPLINE, Arc, Contour, Craft, Document, Glyph, LeadParams, Line, Part


def rect(x0=0.0, y0=0.0, w=60.0, h=40.0):
    p = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)])


LINE3 = LeadParams(1, 90.0, 3.0, 2.0)


def test_jc05_lead_out_leaves_along_the_departing_edge():
    pc = prepare_contour(rect(), Craft(lead_in=LINE3, lead_out=LeadParams(1, 90.0, 3.0, 2.0)), 1, CamParams())
    assert pc.pieces[0][0].start == pytest.approx((0.0, -3.0))
    assert pc.pieces[-1][0].end == pytest.approx((0.0, -3.0))     # O: (0, -3), not (-3, 0)


def test_jc05_vertex_start_inside_the_contour():
    # start at the vertex (60, 0): lead-in along the next edge, lead-out along the edge before
    d_in, d_out = start_tangents(rect(), 60.0 / 200.0)
    assert d_in == pytest.approx((0.0, 1.0)) and d_out == pytest.approx((1.0, 0.0))


def test_jc02_gap_dropped_with_a_lead_out():
    cr = Craft(lead_in=LINE3, lead_out=LeadParams(1, 90.0, 3.0, 2.0), extra={"overcut": -1.0})
    pc = prepare_contour(rect(), cr, 1, CamParams())
    assert sum(s.length() for s, _ in pc.pieces) == pytest.approx(206.0)
    cr.lead_out = LeadParams(0)
    pc = prepare_contour(rect(), cr, 1, CamParams())
    assert sum(s.length() for s, _ in pc.pieces) == pytest.approx(202.0)   # gap kept without lead-out


def test_jc03_kerf_child_keeps_the_start_fraction():
    pc = prepare_contour(rect(), Craft(kerf_comp=1, kerf_width=0.2, lead_in=LINE3), 1, CamParams())
    lens = [round(s.length(), 6) for s, _ in pc.pieces]
    assert lens == [3.0, 60.4, 40.4, 60.4, 40.4]
    assert pc.pieces[1][0].start == pytest.approx((-0.2, -0.2))


def test_jc04_tangent_kerf_joins_keep_their_own_ends():
    a0 = -1.570796      # 6-decimal angle as in a .chf
    arc = Arc((40 + 10 * math.cos(a0), 10 + 10 * math.sin(a0)), (40 + 10 * math.cos(-a0), 10 + 10 * math.sin(-a0)),
              (40.0, 10.0), True)
    c = Contour([Line((0, 0), (40, 0)), arc, Line((40, 20), (0, 20)), Line((0, 20), (0, 0))])
    pc = prepare_contour(c, Craft(kerf_comp=0, kerf_width=0.3), 1, CamParams())
    lens = [s.length() for s, _ in pc.pieces]
    assert lens[0] == pytest.approx(39.7, abs=1e-12)
    assert lens[1] == pytest.approx(9.7 * 3.141592, abs=1e-9)


def test_jc10_per_joint_widths_from_the_craft():
    cr = Craft(micro_joints=[50.0], extra={"micro_joint_widths": [0.6]})
    pc = prepare_contour(rect(), cr, 1, CamParams())
    off = [s.length() for s, on in pc.pieces if not on]
    assert sum(off) == pytest.approx(0.6)


def test_jc11_ties_keep_the_first_longest_piece():
    # two equal lwpoly pieces whose lengths differ in the last bits: the first one wins
    c = rect(0, 0, 20, 10)
    g = Glyph(GLYPH_LWPOLY, {"flag": 1, "verts": [((0, 0), 0.0), ((20, 0), 0.0), ((20, 10), 0.0), ((0, 10), 0.0)]})
    c.set_source_glyphs([g])
    c.segments[2] = Line((20.0, 10.0), (1e-15, 10.0))
    c.set_source_glyphs([g])
    assert O.choose_start(c, 0, 3.0) == pytest.approx(10.0)


def test_stored_length_scales_the_start_fraction():
    c = rect()
    c.set_source_glyphs([Glyph(2, {"start": s.start, "end": s.end}) for s in c.segments])

    class Rec:
        length = 200.00005
    c.extra["chf"] = Rec()
    assert stored_length(c) == 200.00005
    pc = prepare_contour(c, Craft(extra={"start_param": 0.5}), 1, CamParams())
    # the walk is 0.5 * 200.00005 = 100.000025 mm, 2.5e-5 past the corner (60, 40)
    assert pc.pieces[0][0].start == pytest.approx((60.0 - 2.5e-5, 40.0), abs=1e-9)


def _spline_contour():
    k = 0.5522847498
    q = [((25, 0), (25, k * 25), (k * 25, 25), (0, 25)), ((0, 25), (-k * 25, 25), (-25, k * 25), (-25, 0)),
         ((-25, 0), (-25, -k * 25), (-k * 25, -25), (0, -25)), ((0, -25), (k * 25, -25), (25, -k * 25), (25, 0))]
    from mlaser.geom.curves import glyph_segments
    glys = [Glyph(GLYPH_SPLINE, {"ctrl": list(c4), "knots": [0, 0, 0, 0, 1, 1, 1, 1], "u0": 0, "u1": 4}) for c4 in q]
    c = Contour([s for g in glys for s in glyph_segments(g)])
    c.set_source_glyphs(glys)
    return c, glys


def test_jc07_start_on_the_true_curve_and_curve_refs():
    c, glys = _spline_contour()
    t = 0.125                                    # middle of the first quarter
    pc = prepare_contour(c, Craft(lead_in=LINE3, extra={"start_param": t}), 1, CamParams())
    sc = SplineCurve.from_glyph(glys[0])
    P = sc.point(sc.u_at(sc.length * 0.5))
    assert pc.pieces[1][0].start == pytest.approx(P, abs=1e-12)
    assert pc.pieces[0][0].end == pytest.approx(P, abs=1e-12)
    # body pieces carry their exact curve, leads none
    assert pc.curves[0] is None and isinstance(pc.curves[1], CurveRef)
    assert pc.curves[1].curve.point(pc.curves[1].u0) == pytest.approx(P, abs=1e-12)
    items = build_toolpath(Document(parts=[Part(contours=[c], craft=Craft(lead_in=LINE3, extra={"start_param": t}))]))
    cuts = [m for m in items if isinstance(m, CutLine)]
    assert sum(1 for m in cuts if getattr(m, "curve", None) is not None) == len(cuts) - 1


def test_toolpath_mode5_uses_the_faithful_sort_and_reverses_open_contours():
    # open lines; the incoming order is poor (accepted by the 0.95 rule) and the line drawn
    # backwards is flipped by the sort (0x1003c840) and cut reversed, its leads prepared again
    cs = [Contour([Line((0, 0), (10, 0))], closed=False), Contour([Line((40, 0), (50, 0))], closed=False),
          Contour([Line((30, 0), (20, 0))], closed=False), Contour([Line((60, 0), (70, 0))], closed=False)]
    doc = Document(parts=[Part(contours=[c]) for c in cs])
    items = build_toolpath(doc, CamParams(sort_mode=5))
    cuts = [(m.start, m.end) for m in items if isinstance(m, CutLine)]
    assert cuts == [((0, 0), (10, 0)), ((20, 0), (30, 0)), ((40, 0), (50, 0)), ((60, 0), (70, 0))]
    # the same order as the import sort on these contours
    nodes = O.build_tree(cs, list(range(4)))
    order, flipped = O.import_sort(nodes, small_first=False, seed=O.LOCAL_SEED)
    assert [n.key for n in order] == [0, 2, 1, 3] and {n.key for n in order if id(n) in flipped} == {2}
