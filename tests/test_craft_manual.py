"""Geometry of the manual craft tools (mlaser.cam.manual, agent G phase 3)."""
from __future__ import annotations

import math

import pytest

from mlaser.cam import manual as M
from mlaser.model import Arc, Contour, Craft, Document, Line, Part


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def circle(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def test_project_line_and_arc():
    u, q, d = M.project_on_segment(Line((0, 0), (10, 0)), (3, 2))
    assert u == pytest.approx(0.3) and q == pytest.approx((3, 0)) and d == pytest.approx(2)
    a = Arc((5, 0), (5, 0), (0, 0), True)
    u, q, d = M.project_on_segment(a, (0, 7))
    assert u == pytest.approx(0.25) and q[1] == pytest.approx(5) and d == pytest.approx(2)
    cw = Arc((0, 5), (5, 0), (0, 0), False)          # quarter, clockwise
    u, q, d = M.project_on_segment(cw, (3, 3))
    assert u == pytest.approx(0.5)


def test_nearest_and_hit_test():
    doc = Document(parts=[Part([rect(0, 0, 10, 10)]), Part([circle(30, 5, 5)])])
    s, q, d, i = M.nearest_on_contour(doc.parts[0].contours[0], (10.5, 4))
    assert i == 1 and s == pytest.approx(14) and d == pytest.approx(0.5)
    h = M.hit_test(doc, (30, 10.2), tol=0.5)
    assert h is not None and (h.part, h.contour) == (1, 0)
    assert h.dist == pytest.approx(0.25 * 2 * math.pi * 5, rel=1e-6)
    assert M.hit_test(doc, (20, 20), tol=0.5) is None


def test_contour_craft_override():
    single = Part([rect(0, 0, 10, 10)])
    assert M.contour_craft(single, 0, create=True) is single.craft
    multi = Part([rect(0, 0, 10, 10), rect(2, 2, 2, 2)], Craft(layer=3))
    ov = M.contour_craft(multi, 1, create=True)
    assert ov is not multi.craft and ov.layer == 3
    assert multi.contours[1].extra["craft"] is ov
    assert M.contour_craft(multi, 0) is multi.craft


def test_start_point_fraction():
    p = Part([rect(0, 0, 10, 10)])
    assert M.set_start_at(p, 0, 15) == pytest.approx(0.375)
    assert M.start_distance(p.contours[0], p.craft) == pytest.approx(15)
    o = Part([Contour([Line((0, 0), (10, 0))], False)])
    assert M.set_start_at(o, 0, 5) == 0.0


def test_toggle_joint_add_remove_reject():
    c = rect(0, 0, 10, 10)
    cr = Craft()
    assert M.toggle_joint(cr, c, 5, 1.0) == "added"
    assert M.toggle_joint(cr, c, 5.6, 1.0) == "rejected"      # overlaps
    assert M.toggle_joint(cr, c, 39.9, 1.0) == "added"        # wraps across the start
    assert len(cr.micro_joints) == 2
    assert M.toggle_joint(cr, c, 5.3, 1.0) == "removed"
    assert [j[0] for j in cr.micro_joints] == [pytest.approx(39.9)]


def test_explode_joints_closed_and_open():
    c = rect(0, 0, 10, 10)
    cr = Craft(micro_joints=[(5, 1.0), (25, 1.0)])
    parts = M.explode_joints(c, cr)
    assert len(parts) == 2 and all(not p.closed for p in parts)
    assert sum(p.length() for p in parts) == pytest.approx(38)
    assert parts[0].segments[0].start == pytest.approx((5.5, 0))
    o = Contour([Line((0, 0), (10, 0))], False)
    parts = M.explode_joints(o, Craft(micro_joints=[(5, 2.0)]))
    assert [p.length() for p in parts] == [pytest.approx(4), pytest.approx(4)]
    assert M.explode_joints(o, Craft()) == [o]


def test_cool_point_place_moves_existing():
    c = rect(0, 0, 10, 10)
    cr = Craft()
    assert M.place_cool_point(cr, c, 10, 0.1) == "added"
    assert M.place_cool_point(cr, c, 10.3, 0.1) == "moved"      # within max(tol, 0.5)
    assert cr.extra["cool_points"] == [pytest.approx(10.3)]
    assert M.place_cool_point(cr, c, 20, 0.1) == "added"
    assert len(cr.extra["cool_points"]) == 2
    assert M.snap_to_vertex(c, (10.2, 0.1), 0.3) == pytest.approx(10)
    assert M.snap_to_vertex(c, (5, 0.1), 0.3) is None
    assert M.toggle_cool_point(cr, c, 20.1, 0.5) == "removed"


def test_split_contour():
    c = rect(0, 0, 10, 10)
    a, b = M.split_contour_at(c, 15, tol=0.1)
    assert not a.closed and not b.closed
    assert a.segments[0].start == pytest.approx((0, 0)) and a.segments[-1].end == pytest.approx((10, 5))
    assert a.length() == pytest.approx(15) and b.length() == pytest.approx(25)
    assert M.split_contour_at(c, 0.2, tol=0.1) == [c]         # inside the margin max(3tol,0.2)/L
    assert M.split_margin(1.0, 1.0) == 0.3
    a, b = M.split_contour_at(Contour([Line((0, 0), (10, 0))], False), 4)
    assert a.length() == pytest.approx(4) and b.length() == pytest.approx(6)


def test_gap_seal_overcut():
    cr = Craft()
    assert M.set_gap_seal(cr, rect(0, 0, 5, 5), M.GAP, 0.8) == -0.8
    assert M.set_gap_seal(cr, rect(0, 0, 5, 5), M.OVERCUT, 0.8) == 0.8
    assert M.set_gap_seal(cr, Contour([Line((0, 0), (1, 0))], False), M.OVERCUT, 0.8) == 0.0
    assert M.set_gap_seal(cr, None, M.SEAL, 0.8) == 0.0


def test_manual_lead_line_from_click():
    from mlaser.cam.toolpath import build_toolpath
    p = Part([rect(0, 0, 10, 10)])
    lp = M.manual_lead(p, 0, (5, -3), 5, depth=1)
    assert lp.kind == 1 and lp.length == pytest.approx(3) and abs(lp.angle_deg) == pytest.approx(90)
    cuts = [m for m in build_toolpath(Document(parts=[p])) if type(m).__name__ == "CutLine"]
    assert cuts[0].start == pytest.approx((5, -3)) and cuts[0].end == pytest.approx((5, 0))
    # oblique, from inside a hole-like position, on a CW contour
    q = Part([rect(0, 0, 10, 10)])
    M.manual_lead(q, 0, (2, 4), 5, depth=1)
    cuts = [m for m in build_toolpath(Document(parts=[q])) if type(m).__name__ == "CutLine"]
    assert cuts[0].start == pytest.approx((2, 4))
    assert M.manual_lead(Part([Contour([Line((0, 0), (1, 0))], False)]), 0, (0, 1), 0.5) is None


def test_bridge_two_contours_and_same_contour():
    from mlaser.geom.curves import signed_area
    c = M.bridge_two(rect(0, 0, 10, 10), (10, 5), rect(20, 0, 10, 10), (20, 5), 2)
    assert c.closed and signed_area(c) == pytest.approx(220)
    two = M.bridge_same(rect(0, 0, 30, 10), (10, 0), (10, 10), 2)
    assert sorted(round(signed_area(k), 6) for k in two) == [90, 190]
    l1, l2 = M.bridge_lines((0, 0), (10, 0), 2)
    assert l1 == (pytest.approx((-2, 1)), pytest.approx((12, 1)))
    assert M.bridge_two(rect(0, 0, 10, 10), (10, 5), rect(20, 0, 10, 10), (10, 5), 2) is None
