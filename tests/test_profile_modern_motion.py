"""Optional "modern-ruckig" parity profile (mlaser.parity_ports.profiles.modern_motion).

Modern planner, NOT validated on hardware: these tests check the planner's own contract (limits,
path, rest at the ends, corners) and that selecting it leaves the default profile untouched.
"""
from __future__ import annotations

import importlib.util
import logging
import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("ruckig")

from mlaser import parity_ports as PP  # noqa: E402
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Line, Part  # noqa: E402
from mlaser.motion.interp import MotionConfig, plan_path  # noqa: E402
from mlaser.nc import Machine, compile_job  # noqa: E402
from mlaser.parity_ports.cadmodule import planner as faithful  # noqa: E402
from mlaser.parity_ports.cadmodule.planner import PathPoint, PlannerParams  # noqa: E402
from mlaser.parity_ports.profiles import modern_motion as MM  # noqa: E402
from mlaser.toolpath import items as I  # noqa: E402

NAME = "modern-ruckig"
DT = 1e-3


# --------------------------------------------------------------------------- shapes
def _poly(pts, feed=100.0, closed=True):
    pts = list(pts) + ([pts[0]] if closed else [])
    return [I.CutLine(pts[i], pts[i + 1], feed) for i in range(len(pts) - 1)]


def _circle(r, feed=100.0):
    return [I.CutArc((r, 0.0), (-r, 0.0), (0.0, 0.0), True, feed),
            I.CutArc((-r, 0.0), (r, 0.0), (0.0, 0.0), True, feed)]


def _slot(feed=100.0):
    """Two lines joined by two half circles (r = 5), tangent everywhere."""
    return [I.CutLine((0.0, 0.0), (40.0, 0.0), feed),
            I.CutArc((40.0, 0.0), (40.0, 10.0), (40.0, 5.0), True, feed),
            I.CutLine((40.0, 10.0), (0.0, 10.0), feed),
            I.CutArc((0.0, 10.0), (0.0, 0.0), (0.0, 5.0), True, feed)]


def _star(n=5, ro=30.0, ri=12.0, feed=150.0):
    pts = []
    for k in range(2 * n):
        r = ro if k % 2 == 0 else ri
        a = math.pi * k / n
        pts.append((r * math.cos(a), r * math.sin(a)))
    return _poly(pts, feed)


SHAPES = {
    "rect 80x40 @100": _poly([(0, 0), (80, 0), (80, 40), (0, 40)]),
    "triangle @100": _poly([(0, 0), (50, 0), (50, 30)]),
    "circle r10 @100": _circle(10.0),
    "circle r2 @100": _circle(2.0),
    "slot 40x10 @100": _slot(),
    "star 5 @150": _star(),
    "zigzag @200": _poly([(10.0 * i, 5.0 * (i % 2)) for i in range(12)], 200.0, closed=False),
    "line 300 @200": _poly([(0, 0), (300, 0)], 200.0, closed=False),
}


def _plan(moves, profile, cfg=None):
    cfg = cfg or MotionConfig()
    with PP.use_profile(profile):
        return plan_path(moves, cfg, return_geom=True)


def _limits(cfg=None):
    cfg = cfg or MotionConfig()
    prm = cfg.cut.clamped()
    return prm.max_acc, prm.jerk()


# --------------------------------------------------------------------------- selection
def test_selected_by_name_labelled_and_never_default(caplog):
    assert PP.DEFAULT == "original" and PP.active_profile() is PP.ORIGINAL
    p = PP.get_profile(NAME)
    assert p is MM.MODERN_RUCKIG
    assert "NOT validated on hardware" in p.description and "NOT validated on hardware" in p.warning
    assert "NOT validated on hardware" in (MM.__doc__ or "")
    assert p.smoother is PP.ORIGINAL.smoother and p.sampling is PP.ORIGINAL.sampling
    assert p.motion.target("plan_velocity").endswith("modern_motion:plan_velocity")
    with caplog.at_level(logging.WARNING, logger="mlaser.parity_ports"):
        with PP.use_profile(NAME):
            assert PP.active_profile() is p
    assert any("NOT validated on hardware" in r.getMessage() for r in caplog.records)
    assert PP.active_profile() is PP.ORIGINAL


def _doc():
    s = (60.0, 20.0)
    parts = [Part([Contour([Line((0, 0), (80, 0)), Line((80, 0), (80, 40)), Line((80, 40), (0, 40)),
                            Line((0, 40), (0, 0))], True)],
                  Craft(layer=1, lead_in=LeadParams(kind=1, length=2))),
             Part([Contour([Arc(s, s, (50.0, 20.0), True)], True)], Craft(layer=2))]
    return Document(parts, {1: LayerParams(1, speed=80, power_pct=60, pierce={"time_s": 0.05}),
                            2: LayerParams(2, speed=40, power_pct=30)})


def test_compile_job_with_the_modern_profile():
    m = Machine()
    ref = compile_job(_doc(), None, m)
    with PP.use_profile(NAME):
        job = compile_job(_doc(), None, m)
    assert job.wire and job.steps > 0
    d = job.step_deltas()
    sx, sy = sum(a[0] for a in d), sum(a[1] for a in d)
    assert (job.start_pulses[0] + sx, job.start_pulses[1] + sy) == job.end_pulses
    assert all(-127 <= a[0] <= 127 and -127 <= a[1] <= 127 for a in d)
    assert job.laser_on_length_mm == pytest.approx(ref.laser_on_length_mm, rel=1e-3)
    assert all(abs(a - b) <= 1 for a, b in zip(job.end_pulses, ref.end_pulses))  # truncation carry
    assert job.frames()


def test_default_profile_output_is_unchanged():
    """Selecting the modern profile and leaving it restores the bit-exact default: same wire,
    same planner output as the faithful port, and the motion golden records are the same as
    before (recomputed on this platform: the frozen goldens themselves are checked by
    ``-m golden``, bit-exact on the reference platform, docs/dev/TESTING.md)."""
    G = _golden_tools()
    motion_before = G.gen_motion({})
    before = compile_job(_doc(), None, Machine())
    with PP.use_profile(NAME):
        compile_job(_doc(), None, Machine())
    after = compile_job(_doc(), None, Machine())
    assert after.wire == before.wire
    for moves in SHAPES.values():
        plan, _ = _plan(moves, "original")
        cfg = MotionConfig()
        pts, _g = PP.ORIGINAL.smoother.smooth_path_points(moves, cfg, lead_index=None)
        prm = PlannerParams(**cfg.cut.__dict__)
        prm.max_vel = max(prm.max_vel, max(m.feed for m in moves))
        assert plan.sample(1.0) == faithful.plan_velocity(pts, prm).sample(1.0)
    motion_after = G.gen_motion({})
    diffs = [k for k in sorted(motion_before) if G._same(motion_before[k], motion_after.get(k, {}), True)]
    assert not diffs and motion_before.keys() == motion_after.keys(), diffs[:5]


# --------------------------------------------------------------------------- kinematics
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_limits_respected_on_the_samples(shape):
    moves = SHAPES[shape]
    plan, geom = _plan(moves, NAME)
    A, J = _limits()
    vmax = max(max(m.feed for m in moves), MotionConfig().cut.max_vel)
    s = np.asarray(plan.sample(1.0))
    assert len(s) > 10
    assert np.all(np.diff(s) >= -1e-9), "the path parameter goes backwards"
    v = np.diff(s) / DT
    a = np.diff(s, 2) / DT ** 2
    j = np.diff(s, 3) / DT ** 3
    assert v.max() <= vmax * 1.001
    assert np.abs(a).max() <= A * 1.01
    assert np.abs(j).max() <= J * 1.01
    # the continuous trajectory too (each block's own polynomials)
    for b in plan.blocks:
        tt = np.linspace(0.0, b.duration, 2000)
        _p, vb, ab, jb = b._eval(tt)
        assert vb.max() <= vmax * (1 + 1e-9) and np.abs(ab).max() <= A * (1 + 1e-9)
        assert np.abs(jb).max() <= J * (1 + 1e-9)
        assert b.vs >= 0 and b.ve >= 0


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_path_followed_and_at_rest_at_both_ends(shape):
    moves = SHAPES[shape]
    plan, geom = _plan(moves, NAME)
    total = geom.cum[-1]
    assert plan.length == pytest.approx(total, abs=1e-6)
    assert plan.blocks[0].vs == 0.0 and plan.blocks[-1].ve == 0.0
    s = plan.sample(1.0)
    assert s[0] == 0.0
    A, J = _limits()
    assert s[1] - s[0] <= J * DT ** 3 / 6 * 1.01             # starts from rest (jerk ramp)
    assert total - s[-1] <= J * DT ** 3 / 6 * 1.01 + 1e-9    # ends at rest on the end point
    assert plan.state_at_time(plan.duration + 1.0) == (plan.length, 0.0)
    # every sample lies on the commanded geometry (within the smoother's corner tolerance)
    tol = MotionConfig().corner_tol + 1e-6
    for x in s[:: max(1, len(s) // 400)]:
        (px, py), _ = geom.at(x)
        assert _dist_to_moves((px, py), moves) <= tol


def _dist_to_moves(p, moves):
    best = math.inf
    for m in moves:
        if isinstance(m, I.CutArc):
            r = math.dist(m.start, m.center)
            best = min(best, abs(math.dist(p, m.center) - r))   # full circles / half arcs only
            continue
        (ax, ay), (bx, by) = m.start, m.end
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
        best = min(best, math.hypot(p[0] - ax - u * dx, p[1] - ay - u * dy))
    return best


def test_corners_slowed_down():
    moves = SHAPES["triangle @100"]
    plan, geom = _plan(moves, NAME)
    s = np.asarray(plan.sample(1.0))
    v = np.diff(s) / DT
    mid = 0.5 * (s[1:] + s[:-1])
    feed = 100.0
    assert v.max() >= 0.95 * feed                      # cruises on the straights
    for corner in (50.0, 80.0):                        # s of the two interior corners
        near = v[np.abs(mid - corner) <= 0.5]
        assert near.size and near.min() < 0.3 * feed, (corner, near.min() if near.size else None)
    # no corner speed limit on a tangent path (slot): only the r = 5 arcs, sqrt(A r) = 100
    plan2, _ = _plan(SHAPES["slot 40x10 @100"], NAME)
    v2 = np.diff(np.asarray(plan2.sample(1.0))) / DT
    n = len(v2)
    assert v2[n // 10: -n // 10].min() > 0.8 * feed


def test_curvature_limit_on_a_small_circle():
    plan, _ = _plan(SHAPES["circle r2 @100"], NAME)
    A, _J = _limits()
    v = np.diff(np.asarray(plan.sample(1.0))) / DT
    assert v.max() <= math.sqrt(A * 2.0) * 1.001       # v <= sqrt(a_max r)


def test_planner_unit_cases():
    """Direct calls: empty / degenerate input, slow start and slow end, explicit limits."""
    prm = PlannerParams(max_acc=2000.0, acc_time=0.125, max_vel=200.0)
    assert MM.plan_velocity([], prm).blocks == []
    assert MM.plan_velocity([PathPoint(0.0)], prm).blocks == []
    pts = [PathPoint(0.0, feed=200.0), PathPoint(50.0, feed=200.0), PathPoint(100.0, feed=200.0)]
    plan = MM.plan_velocity(pts, prm)
    assert plan.length == pytest.approx(100.0) and plan.blocks[0].vs == 0.0
    slow = PlannerParams(max_acc=2000.0, acc_time=0.125, max_vel=200.0, start_slow_len=10.0,
                         start_slow_vel=20.0, end_slow_len=10.0, end_slow_vel=30.0)
    p2 = MM.plan_velocity(pts, slow)
    for t in np.linspace(0.0, p2.duration, 3000):
        x, vv = p2.state_at_time(float(t))
        if x <= 10.0:
            assert vv <= 20.0 + 1e-6
        if x >= 90.0:
            assert vv <= 30.0 + 1e-6
    assert p2.duration > plan.duration
    # a point limit in the middle
    p3 = MM.plan_velocity([PathPoint(0.0, feed=200.0), PathPoint(50.0, feed=200.0, limit=25.0),
                           PathPoint(100.0, feed=200.0)], prm)
    x, vv = min((p3.state_at_time(float(t)) for t in np.linspace(0, p3.duration, 5000)),
                key=lambda xv: abs(xv[0] - 50.0))
    assert vv <= 25.0 + 1e-3
    # same Plan.sample interface as the faithful planner
    assert isinstance(plan, faithful.Plan)
    assert plan.sample(1.0)[-1] == pytest.approx(100.0, abs=1e-4)


def test_scurve_distance_matches_ruckig():
    A, J = 2000.0, 32000.0
    for v0, v1 in ((0.0, 100.0), (0.0, 5.0), (40.0, 180.0), (150.0, 10.0)):
        L = MM.scurve_distance(v0, v1, A, J)
        blk = MM._ruckig_block(L * (1 + 1e-5), v0, v1, max(v0, v1), A, J)
        assert blk is not None and blk.duration > 0
        assert blk.vmax <= max(v0, v1) * (1 + 1e-6) + 1e-6
        assert MM.reachable_speed(min(v0, v1), L, A, J, 1e9) == pytest.approx(max(v0, v1), rel=1e-6)


def _golden_tools():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("make_goldens", root / "tools" / "make_goldens.py")
    G = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(G)
    return G


def test_golden_motion_paths_under_the_modern_profile():
    """Every path x config of the motion goldens: plans, stays within v/a/j, ends at rest."""
    G = _golden_tools()
    paths = G.motion_paths()
    for cname, cfg, feed in G.motion_configs():
        prm = cfg.cut.clamped()
        A, J = prm.max_acc, prm.jerk()
        for pname, moves in paths.items():
            mv = G._with_feed(moves, feed)
            plan, geom = _plan(mv, NAME, cfg)
            assert plan.length == pytest.approx(geom.cum[-1], abs=1e-6), (pname, cname)
            s = np.asarray(plan.sample(1.0))
            if len(s) < 4:
                continue
            assert np.diff(s).max() / DT <= feed * 1.001, (pname, cname)
            assert np.abs(np.diff(s, 2)).max() / DT ** 2 <= A * 1.01, (pname, cname)
            assert np.abs(np.diff(s, 3)).max() / DT ** 3 <= J * 1.01, (pname, cname)
            assert plan.blocks[0].vs == 0.0 and plan.blocks[-1].ve == 0.0
            if cfg.cut.start_slow_len >= 0.1 and geom.cum[-1] > 10:
                n0 = int(np.searchsorted(s, cfg.cut.start_slow_len * 0.9))
                assert np.diff(s[:n0]).max(initial=0.0) / DT <= cfg.cut.start_slow_vel * 1.001


def test_community_features_only_and_clear_error_without_ruckig(monkeypatch):
    import inspect
    import sys

    src = inspect.getsource(MM)
    assert "intermediate_positions" not in src            # waypoints = Ruckig Pro / cloud API
    monkeypatch.setitem(sys.modules, "ruckig", None)      # simulate the missing extra
    with pytest.raises(MM.RuckigMissing, match="ruckig"):
        MM.plan_velocity([PathPoint(0.0, feed=100.0), PathPoint(10.0, feed=100.0)])
