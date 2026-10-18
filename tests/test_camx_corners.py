"""Geometric checks for mlaser.cam.corners (CArcRound, CUnloadAngle, CAlphaRound, CCoolPoint,
kerf Bezier join; docs/cam.md section 13)."""
import math

import pytest

from mlaser.cam import corners as K
from mlaser.cam.geometry import is_ccw, seg_tangent
from mlaser.model import Arc, Contour, Line


def poly(pts, closed=True):
    segs = [Line(a, b) for a, b in zip(pts, pts[1:])]
    if closed:
        segs.append(Line(pts[-1], pts[0]))
    return Contour(segs, closed)


def square(s=10.0):
    return poly([(0, 0), (s, 0), (s, s), (0, s)])


def connected(c, tol=1e-6):
    segs = c.segments
    ok = all(math.dist(a.end, b.start) < tol for a, b in zip(segs, segs[1:]))
    return ok and (not c.closed or math.dist(segs[-1].end, segs[0].start) < tol)


def test_corner_angles():
    cs = K.corners(square())
    assert len(cs) == 4
    assert all(abs(c.angle_deg - 90) < 1e-9 and c.turn > 0 for c in cs)
    tri = poly([(0, 0), (10, 0), (0, 3)])
    a = sorted(c.angle_deg for c in K.corners(tri))
    assert abs(sum(a) - 180) < 1e-6


def test_fillet_square_tangent_radius():
    r = 2.0
    c = K.fillet(square(), r)
    arcs = [s for s in c.segments if isinstance(s, Arc)]
    assert len(arcs) == 4 and connected(c)
    for i, s in enumerate(c.segments):
        if isinstance(s, Arc):
            assert abs(s.radius - r) < 1e-9
            assert abs(abs(s.sweep()) - math.pi / 2) < 1e-9
            prev = c.segments[i - 1]
            nxt = c.segments[(i + 1) % len(c.segments)]
            # tangent continuity at both ends
            t0, t1 = seg_tangent(prev, 1.0), seg_tangent(s, 0.0)
            assert math.dist(t0, t1) < 1e-9
            t2, t3 = seg_tangent(s, 1.0), seg_tangent(nxt, 0.0)
            assert math.dist(t2, t3) < 1e-9
    # perimeter = 4*(s - 2r) + 2*pi*r
    assert abs(c.length() - (4 * (10 - 2 * r) + 2 * math.pi * r)) < 1e-9
    assert is_ccw(c)


def test_fillet_cw_and_triangle():
    cw = poly([(0, 0), (0, 10), (10, 10), (10, 0)])
    c = K.fillet(cw, 1.0)
    assert connected(c) and not is_ccw(c)
    assert all(not s.ccw for s in c.segments if isinstance(s, Arc))
    tri = poly([(0, 0), (20, 0), (0, 10)])
    c2 = K.fillet(tri, 1.0)
    for s in c2.segments:
        if isinstance(s, Arc):
            assert abs(s.radius - 1.0) < 1e-9
    assert connected(c2)


def test_fillet_angle_window():
    # L-shape: five 90 deg convex corners and one 270 deg (reflex, interior 90 as well by cos)
    tri = poly([(0, 0), (20, 0), (0, 10)])     # angles ~26.6, ~63.4, 90
    only_sharp = K.fillet(tri, 0.5, 0, 45)
    assert sum(isinstance(s, Arc) for s in only_sharp.segments) == 1
    none = K.fillet(tri, 0.5, 100, 180)
    assert none is tri


def test_fillet_too_large_skips():
    c = K.fillet(square(2.0), 5.0)
    assert not any(isinstance(s, Arc) for s in c.segments)


def test_unload_loop():
    r = 1.0
    c = K.unload_loop(square(), r)
    assert connected(c)
    arcs = [s for s in c.segments if isinstance(s, Arc)]
    assert len(arcs) == 4
    for a in arcs:
        assert abs(a.radius - r) < 1e-9
        # centred on an original vertex, sweeping 270 deg the long way round
        assert min(math.dist(a.center, v) for v in [(0, 0), (10, 0), (10, 10), (0, 10)]) < 1e-9
        assert abs(abs(a.sweep()) - 1.5 * math.pi) < 1e-9
        # midpoint lies outside the square (outside the corner wedge)
        m = a.point_at(0.5)
        assert not (0 < m[0] < 10 and 0 < m[1] < 10)
    assert abs(c.length() - (4 * 8 + 4 * 1.5 * math.pi)) < 1e-9


def test_alpha_loop_outside():
    c = K.alpha_corners(square(), max_angle=100, min_edge=1, lead=2, mode=1)
    assert connected(c)
    # each original vertex is visited twice now (loop starts and ends there)
    pts = [s.start for s in c.segments]
    for v in [(10, 0), (10, 10), (0, 10)]:
        assert sum(math.dist(p, v) < 1e-9 for p in pts) >= 2 or v == (0, 0)
    # loop leaves along t_in and comes back along t_out (tangent continuity)
    cs = K.corners(square())
    cn = cs[0]          # vertex (10, 0): t_in (1,0), t_out (0,1)
    loop = K.alpha_loop(cn, 2.0)
    d0 = (loop[0].end[0] - loop[0].start[0], loop[0].end[1] - loop[0].start[1])
    assert d0[0] > 0 and abs(d0[1]) < 0.2 * abs(d0[0])
    dl = (loop[-1].end[0] - loop[-1].start[0], loop[-1].end[1] - loop[-1].start[1])
    assert dl[1] > 0 and abs(dl[0]) < 0.2 * abs(dl[1])
    # the loop stays outside the square
    assert all(not (0 < s.end[0] < 10 and 0 < s.end[1] < 10) for s in loop)
    # inner mode on a convex square changes nothing; corner at the start is skipped
    assert K.alpha_corners(square(), 100, 1, 2, mode=2) is not None
    assert len(K.alpha_corners(square(), 100, 1, 2, mode=2).segments) == 4
    c3 = K.alpha_corners(square(), 100, 1, 2, mode=3)
    n_loops = len(c3.segments) - 4
    assert n_loops == 3 * 24     # vertex (0,0) is the start point: skipped


def test_alpha_auto_depth():
    hole = square()
    outer = K.alpha_corners(hole, 100, 1, 2, mode=0, depth=1)
    inner = K.alpha_corners(hole, 100, 1, 2, mode=0, depth=2)
    assert len(outer.segments) > 4 and len(inner.segments) == 4


def test_auto_cool_points():
    tri = poly([(0, 0), (20, 0), (0, 10)])
    pts = K.auto_cool_points(tri, max_angle=45)
    L0 = 20.0
    assert pts == [pytest.approx(L0)]
    sq = K.auto_cool_points(square(), max_angle=90)
    assert sq == pytest.approx([0.0, 10.0, 20.0, 30.0])
    assert K.auto_cool_points(square(), 60) == []
    assert K.auto_cool_points(square(), 60, lead_point=True, start_dist=5.0) == [5.0]


def test_bezier_join():
    a, b = (0.0, 0.0), (1.0, 1.0)
    segs = K.bezier_corner_join(a, (1.0, 0.0), b, (0.0, 1.0))
    assert segs[0].start == a and segs[-1].end == b
    t0 = (segs[0].end[0] - a[0], segs[0].end[1] - a[1])
    assert abs(t0[1]) < 0.05 * abs(t0[0])   # leaves A along ta (cubic: O(t^2) deviation)
    # symmetric corner -> one tangent arc; asymmetric -> Bezier lines
    j = K.kerf_corner_join(a, (1.0, 0.0), b, (0.0, 1.0))
    assert len(j) == 1 and isinstance(j[0], Arc) and abs(j[0].radius - 1.0) < 1e-9
    j2 = K.kerf_corner_join(a, (1.0, 0.0), (2.0, 1.0), (0.0, 1.0))
    assert len(j2) > 1 and all(isinstance(s, Line) for s in j2)
