import math

import numpy as np
import pytest

from mlaser.cam.items import CutArc, CutLine, Delay, Rapid
from mlaser.motion.interp import (MotionConfig, arc_interp, corner_radius, estimate_time,
                                  plan_path, seg_interp, seg_interp_time, simulate)
from mlaser.motion.planner import PathPoint, PlannerParams, curvature_speed, plan_velocity
from mlaser.motion.profile import accel_distance, scurve_profile, trapezoid_profile

A, J = 2000.0, 32000.0


def _check_profile(p, vcap):
    T = p.duration
    ts = np.linspace(0, T, 3001)
    st = [p.state(t) for t in ts]
    pos = [s for s, _ in st]
    vel = [v for _, v in st]
    assert all(b >= a - 1e-9 for a, b in zip(pos, pos[1:]))
    assert pos[0] == pytest.approx(0, abs=1e-12)
    assert pos[-1] == pytest.approx(p.L, abs=1e-6)
    assert vel[0] == pytest.approx(p.vs, abs=1e-6)
    assert vel[-1] == pytest.approx(p.ve, abs=1e-6)
    assert max(vel) <= vcap + 1e-6
    acc = np.diff(vel) / np.diff(ts)
    assert np.max(np.abs(acc)) <= p.A * 1.01 + 1e-6
    # position is the integral of velocity
    assert np.trapezoid(vel, ts) == pytest.approx(p.L, rel=1e-3)


@pytest.mark.parametrize("L,vs,vm,ve", [(100, 0, 200, 0), (5, 0, 200, 0), (10, 100, 200, 20),
                                        (10, 20, 200, 100), (300, 50, 150, 50), (0.5, 0, 200, 0)])
def test_scurve_feasible_profiles(L, vs, vm, ve):
    p = scurve_profile(L, vs, vm, ve, A, J)
    assert p.feasible
    _check_profile(p, vm)


def test_scurve_infeasible_lowers_acc():
    p = scurve_profile(1.0, 0.0, 200.0, 50.0, A, J)   # cannot reach 50 mm/s in 1 mm
    assert not p.feasible
    assert p.A > A or p.J != J
    assert sum(p.s) == pytest.approx(1.0)
    assert p.state(p.duration)[1] == pytest.approx(50.0)


def test_scurve_long_move_time():
    # long move: accel phase time = vmax/A + A/J ; cruise covers the rest
    p = scurve_profile(1000, 0, 200, 0, A, J)
    ta = 200 / A + A / J
    da = accel_distance(0, 200, A, J)
    assert da == pytest.approx(200 * ta / 2)
    assert p.duration == pytest.approx(2 * ta + (1000 - 2 * da) / 200, rel=1e-9)


def test_trapezoid():
    p = trapezoid_profile(100, 0, 100, 0, 1000)
    assert p.duration == pytest.approx(2 * 0.1 + (100 - 10) / 100)
    p = trapezoid_profile(1, 0, 100, 0, 1000)   # triangle
    assert p.vmax == pytest.approx(math.sqrt(1000 * 1))


def test_seg_interp_output_format():
    s = seg_interp(100, 200, 0, 0, A, J, period_ms=1.0)
    assert s[0] == 0.0 and s[-1] == 1.0
    assert all(b >= a for a, b in zip(s, s[1:]))
    t_ms = seg_interp_time(100, 200, 0, 0, A, J)
    assert len(s) == pytest.approx(t_ms / 1.0, abs=3)
    # max step = vmax * period / L
    assert max(np.diff(s)) <= 200 * 0.001 / 100 + 1e-9
    assert seg_interp(0.00001, 200, 0, 0, A, J) == []       # too short (return 1)
    assert seg_interp(10, 200, 0, 0, 1.0, J) == []          # acc < 5 rejected


@pytest.mark.parametrize("mode", [1, 2])
def test_arc_interp(mode):
    s = arc_interp(0, math.pi, 10, 100, 0, 0, 2000, 2000, 30000, 30000, 1.0, mode)
    assert s[0] == pytest.approx(0) and s[-1] == pytest.approx(1)
    assert all(b >= a - 1e-12 for a, b in zip(s, s[1:]))
    # never faster than vmax
    L = math.pi * 10
    assert max(np.diff(s)) * L / 0.001 <= 100 * 1.01


def test_arc_interp_centripetal_limit():
    # per-axis limit: v^2/R*max(|sin|,|cos|) <= a  ->  v <= sqrt(R*a*sqrt(2))
    R, a = 1.0, 2000.0
    s = arc_interp(0, 2 * math.pi, R, 500, 0, 0, a, a, 30000, 30000, 1.0, 1)
    v = np.diff(s) * 2 * math.pi * R / 0.001
    assert max(v) <= math.sqrt(R * a * math.sqrt(2)) * 1.02
    assert max(v) > math.sqrt(R * a) * 0.9


def test_curvature_speed_continuous_and_monotone():
    q = 0.5 / 0.125
    for x in (1.6, 6.0, 12.0):
        assert curvature_speed(x - 1e-9, q, 0.05) == pytest.approx(curvature_speed(x + 1e-9, q, 0.05), rel=1e-6)
    rs = np.linspace(0.01, 100, 500)
    vs = [curvature_speed(r, q, 0.05) for r in rs]
    assert all(b >= a for a, b in zip(vs, vs[1:]))
    assert curvature_speed(1.0, q, 0.05) == pytest.approx(32.0)   # 8 q R


def test_jerk_derivation():
    p = PlannerParams()
    # 200 <= 2000*0.125/2 = 125 is false -> J = 2A/T
    assert p.jerk() == pytest.approx(2 * 2000 / 0.125)
    p = PlannerParams(max_vel=100)
    assert p.jerk() == pytest.approx(4 * 100 / 0.125 ** 2)


def test_planner_corner_slowdown():
    sq = [CutLine((0, 0), (100, 0), 150), CutLine((100, 0), (100, 100), 150)]
    plan = plan_path(sq, MotionConfig())
    s_corner = 100.0
    # find the time closest to the corner
    ts = np.linspace(0, plan.duration, 4000)
    sv = [plan.state_at_time(t) for t in ts]
    v_at = min((abs(s - s_corner), v) for s, v in sv)[1]
    assert v_at < 30
    assert max(v for _, v in sv) <= 150 + 1e-6
    # CContoutSmooth (on by default, as in the original) cuts the corner by a 0.05 mm blend
    assert 199.9 < plan.length < 200
    samples = plan.sample(1.0)
    assert samples[0] == 0 and samples[-1] == pytest.approx(plan.length, abs=1e-5)
    assert all(b >= a - 1e-9 for a, b in zip(samples, samples[1:]))


def test_planner_tangent_join_no_slowdown():
    path = [CutLine((0, 0), (50, 0), 100), CutArc((50, 0), (50, 100), (50, 50), True, 100),
            CutLine((50, 100), (0, 100), 100)]
    plan = plan_path(path, MotionConfig())
    s, v = plan.state_at_time(plan.duration / 2)
    assert v == pytest.approx(100, rel=0.02)


def test_corner_radius():
    assert corner_radius(0.0, 0.05) > 1e6
    r90 = corner_radius(math.pi / 2, 0.05)
    c = math.cos(math.pi / 4)
    assert r90 == pytest.approx(0.05 * c / (1 - c))


def test_start_slow():
    pts = [PathPoint(0.0), PathPoint(20.0), PathPoint(100.0)]
    for p in pts:
        p.feed = 200
    plan = plan_velocity(pts, PlannerParams(start_slow_len=10, start_slow_vel=10))
    ts = np.linspace(0, plan.duration, 4000)
    for t in ts:
        s, v = plan.state_at_time(t)
        if s < 9.5:
            assert v <= 10 * 1.07 + 1e-6


def test_estimate_and_simulate_agree():
    items = [Rapid((0, 0), (10, 0)), CutLine((10, 0), (60, 0), 100), CutLine((60, 0), (60, 50), 100),
             Delay(0.5), Rapid((60, 50), (0, 0))]
    cfg = MotionConfig(jump_add_time_ms=200)
    est = estimate_time(items, cfg)
    sim = simulate(items, cfg, period_ms=1.0)
    assert sim[-1].t == pytest.approx(est["total"], abs=0.01)
    assert est["cut_length"] == pytest.approx(100, abs=0.1)     # one smoothed corner
    assert est["dwell"] == pytest.approx(0.5)
    assert est["rapid"] > 2 * 0.2

