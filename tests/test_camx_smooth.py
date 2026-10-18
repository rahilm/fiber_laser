"""CContoutSmooth port (mlaser.motion.smooth): geometric checks."""
import math

import pytest

from mlaser.toolpath.items import CutArc, CutLine
from mlaser.model import Arc, Line
from mlaser.motion.planner import PlannerParams, plan_velocity
from mlaser.motion.smooth import (R_LINE, ContourSmoother, SmoothParams, smooth_nodes,
                                  smooth_path_points)


def _poly(pts):
    return [Line(a, b) for a, b in zip(pts, pts[1:])]


def _dist_to_polyline(p, pts):
    best = math.inf
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        best = min(best, math.dist(p, (a[0] + dx * t, a[1] + dy * t)))
    return best


def _samples(sm, n=4000):
    L = sm.length
    return [sm.point_at(L * k / n) for k in range(n + 1)]


def test_param_defaults_and_clamps():
    p = SmoothParams()
    assert (p.lead_tol, p.arc_limit_deg, p.line_ext, p.arc_ext, p.shallow) == (0.001, 37, 4.0, 2.2, 1.2)
    c = p.clamped()
    assert c.arc_limit_deg == 30                       # ctor 37 is outside [7, 30]
    c = SmoothParams(-1, 1, 1.0, 1.0, 0.5).clamped()
    assert (c.lead_tol, c.arc_limit_deg, c.line_ext, c.arc_ext, c.shallow) == (0.0, 7, 2.0, 2.2, 1.0)
    c = SmoothParams(1, 99, 99, 99, 99).clamped()
    assert (c.lead_tol, c.arc_limit_deg, c.line_ext, c.arc_ext, c.shallow) == (0.1, 30, 8.0, 4.0, 8.0)


def test_straight_line_huge_radius():
    sm = ContourSmoother().load([Line((0, 0), (30, 0))]).run()
    nodes = sm.nodes()
    assert all(n.radius == R_LINE for n in nodes)
    assert sm.length == pytest.approx(30.0)
    assert nodes[0].s == 0 and nodes[-1].s == pytest.approx(30.0)


def test_collinear_lines_are_merged():
    sm = ContourSmoother().load(_poly([(0, 0), (10, 0), (25, 0)])).run()
    assert [p.kind for p in sm.pieces] == ["line"]
    assert all(n.radius == R_LINE for n in sm.nodes())


def test_full_circle_radius():
    sm = ContourSmoother().load([Arc((5, 0), (5, 0), (0, 0), True)]).run()
    assert all(n.radius == pytest.approx(5.0) for n in sm.nodes())
    assert sm.length == pytest.approx(2 * math.pi * 5)


def test_two_semicircles_merge_into_one_arc():
    # FUN_100099d0 merges co-circular arcs with the same direction, whatever the total sweep
    segs = [Arc((5, 0), (-5, 0), (0, 0), True), Arc((-5, 0), (5, 0), (0, 0), True)]
    sm = ContourSmoother().load(segs).run()
    assert [p.kind for p in sm.pieces] == ["arc"]
    assert sm.length == pytest.approx(2 * math.pi * 5)
    assert all(abs(n.radius - 5.0) < 1e-9 for n in sm.nodes())


def test_arc_arc_blend_floor():
    # two tangent arcs on different circles: the blend's nodes are floored at min(rA, rB)
    segs = [Arc((5, 0), (-5, 0), (0, 0), True), Arc((-5, 0), (-15, 0), (-10, 0), False)]
    sm = ContourSmoother().load(segs).run()
    kinds = [p.kind for p in sm.pieces]
    assert kinds == ["arc", "blend", "arc"]
    off = sm.pieces[0].length
    nodes = sm.nodes()
    blend_nodes = [n for n in nodes if off + 1e-9 < n.s <= off + sm.pieces[1].length + 1e-9]
    assert blend_nodes and all(n.radius >= 5.0 - 1e-9 for n in blend_nodes)


@pytest.mark.parametrize("tol", [0.05, 0.1, 0.2])
def test_square_corner_apex_deviation_equals_tol(tol):
    sm = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), tol=tol).run()
    pts = _samples(sm, 20000)
    apex = min(math.dist(p, (10, 0)) for p in pts)
    assert apex == pytest.approx(tol, rel=1e-3)
    # control legs: d = 2 tol / sin(45 deg), extent 4 d on lines
    d = 2 * tol / math.sin(math.radians(45))
    blend = sm.pieces[1].g
    assert blend.ctrl[1] == pytest.approx((10 - d, 0.0))
    assert blend.ctrl[0] == pytest.approx((10 - 4 * d, 0.0))
    assert blend.ctrl[2] == pytest.approx((10, 0))


def test_corner_radius_scales_with_tol():
    def apex_r(tol):
        ns = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), tol=tol).run().nodes()
        return min(n.radius for n in ns)
    r1, r2 = apex_r(0.05), apex_r(0.1)
    assert 0 < r1 < 1 and r2 == pytest.approx(2 * r1, rel=0.02)


def test_tol_is_clamped():
    a = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), tol=5.0).run()
    b = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), tol=0.3).run()
    assert a.length == pytest.approx(b.length)
    c = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), tol=0.0).run()
    assert min(math.dist(p, (10, 0)) for p in _samples(c, 20000)) == pytest.approx(0.01, rel=1e-2)


@pytest.mark.parametrize("pts", [
    [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
    [(0, 0), (5, 5), (10, 0), (15, 5), (20, 0)],
    [(0, 0), (20, 0), (21, 8), (40, 8)],
])
def test_deviation_within_tol_and_continuity(pts):
    tol = 0.1
    sm = ContourSmoother().load(_poly(pts), tol=tol).run()
    n = 6000
    sam = _samples(sm, n)
    ds = sm.length / n
    for a, b in zip(sam, sam[1:]):
        assert math.dist(a, b) <= ds * 1.05 + 1e-9
    assert max(_dist_to_polyline(p, pts) for p in sam) <= tol + 1e-6
    assert sam[0] == pytest.approx(pts[0]) and sam[-1] == pytest.approx(pts[-1])
    assert sm.length < sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def test_cusp_gives_full_stop():
    sm = ContourSmoother().load(_poly([(0, 0), (10, 0), (0, 0.5)])).run()
    assert any(p.sentinel for p in sm.pieces)
    nodes = sm.nodes()
    assert min(n.radius for n in nodes) == 0.0
    s_join = [n.s for n in nodes if n.radius == 0.0]
    assert s_join[0] == pytest.approx(10.0, abs=1e-6)


def test_lead_joint_uses_lead_tol():
    segs = _poly([(-5, 0), (0, 0), (0, 10)])
    sm = ContourSmoother(SmoothParams(lead_tol=0.002)).load(segs, tol=0.1, lead_index=1).run()
    apex = min(math.dist(p, (0, 0)) for p in _samples(sm, 40000))
    assert apex == pytest.approx(0.002, rel=2e-2)


def test_flag_off_no_blending():
    sm = ContourSmoother().load(_poly([(0, 0), (10, 0), (10, 10)]), flag=False).run()
    assert [p.kind for p in sm.pieces] == ["line", "line"]
    assert sm.length == pytest.approx(20.0)


def test_small_fillet_replaced_by_blended_corner():
    # 60 deg turn with a tiny fillet: FUN_1000a3c0 restores the sharp corner (dot >= 0.05)
    r, tol = 0.02, 0.05
    t = r * math.tan(math.radians(30))
    u = (math.cos(math.radians(60)), math.sin(math.radians(60)))
    p1 = (10 + t * u[0], t * u[1])
    segs = [Line((0, 0), (10 - t, 0)), Arc((10 - t, 0), p1, (10 - t, r), True),
            Line(p1, (10 + 10 * u[0], 10 * u[1]))]
    sm = ContourSmoother().load(segs, tol=tol).run()
    assert "arc" not in [p.kind for p in sm.pieces]
    # shallow-corner law: V = L0 (1 - (1 - 1.2/8) * ratio), apex deviation = tol * V / L0
    ratio = (math.acos(-0.5) - math.pi / 2) / (math.acos(-0.984375) - math.pi / 2)
    expect = tol * (1 - (1 - 0.15) * ratio)
    assert min(math.dist(p, (10, 0)) for p in _samples(sm, 20000)) == pytest.approx(expect, rel=1e-2)


def test_square_fillet_kept():
    # 90 deg turn: dot = 0 < 0.05, the fillet arc is kept and blended on both sides
    r = 0.02
    segs = [Line((0, 0), (10 - r, 0)), Arc((10 - r, 0), (10, r), (10 - r, r), True), Line((10, r), (10, 10))]
    sm = ContourSmoother().load(segs, tol=0.05).run()
    assert [p.kind for p in sm.prims] == ["line", "arc", "line"]


def test_large_arc_kept():
    segs = [Line((0, 0), (10, 0)), Arc((10, 0), (15, 5), (10, 5), True), Line((15, 5), (15, 20))]
    sm = ContourSmoother().load(segs, tol=0.05).run()
    assert [p.kind for p in sm.pieces] == ["line", "blend", "arc", "blend", "line"]
    assert any(abs(n.radius - 5) < 1e-9 for n in sm.nodes())


def test_arc2seg_factor_on_arc_to_line_blend():
    segs = [Arc((0, -5), (5, 0), (0, 0), True), Line((5, 0), (5, 10))]
    sm = ContourSmoother().load(segs, kx=0.5, ky=0.5).run()
    blend = [p for p in sm.pieces if p.kind == "blend"][0]
    assert blend.factor == pytest.approx(0.5, abs=0.02)
    assert min(n.factor for n in sm.nodes()) == blend.factor
    sm1 = ContourSmoother().load(segs).run()
    assert min(n.factor for n in sm1.nodes()) == 1.0


def test_short_line_run_fitted_as_spline():
    r, n = 10.0, 120
    pts = [(r * math.cos(2 * math.pi * k / n * 0.5), r * math.sin(2 * math.pi * k / n * 0.5)) for k in range(n + 1)]
    sm = ContourSmoother().load(_poly(pts)).run()
    assert [p.kind for p in sm.pieces] == ["spline"]
    for p in _samples(sm, 2000):
        assert abs(math.hypot(*p) - r) < 0.3
    mid = [nd.radius for nd in sm.nodes()[3:-3]]
    assert all(abs(R - r) / r < 0.2 for R in mid)


def test_smooth_nodes_monotone():
    ns = smooth_nodes(_poly([(0, 0), (10, 0), (10, 10), (20, 15)]), tol=0.05)
    ss = [s for s, _, _ in ns]
    assert ss == sorted(ss) and ss[0] == 0.0


def test_planner_integration_square():
    sq = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]
    moves = [CutLine(a, b, 100.0) for a, b in zip(sq, sq[1:])]
    pts, geom = smooth_path_points(moves)
    plan = plan_velocity(pts, PlannerParams(max_vel=100.0))
    assert plan.length == pytest.approx(geom.cum[-1], rel=1e-6)
    assert 2.0 < plan.duration < 10.0
    # slow at the corner, fast mid-edge
    vs = []
    t, T = 0.0, plan.duration
    while t < T:
        s, v = plan.state_at_time(t)
        vs.append((s, v))
        t += 0.001
    corner = [v for s, v in vs if abs(s - 50.0) < 0.3]
    mid = [v for s, v in vs if abs(s - 25.0) < 0.3]
    assert corner and mid and max(corner) < 0.3 * min(mid)
    (x, y), laser = geom.at(25.0)
    assert (x, y) == pytest.approx((25.0, 0.0)) and laser


def test_moves_with_arcs_and_laser_flags():
    moves = [CutLine((0, 0), (10, 0), 50.0), CutLine((10, 0), (11, 0), 50.0, laser=False),
             CutArc((11, 0), (16, 5), (11, 5), True, 50.0)]
    pts, geom = smooth_path_points(moves)
    assert geom.at(5.0)[1] is True
    assert geom.at(10.5)[1] is False
    assert geom.at(geom.cum[-1])[0] == pytest.approx((16, 5), abs=1e-9)
