"""Golden values observed from the original DLLs (parity harness, Wine).

The numbers below were produced by MotionCtrl.dll (exports segInterp, segInterp_time, arcInterp,
CVelocityPlanning vt[1]/[2]/[4]) and by CADModule.dll's v1.3.23 planner copy (factory
0x1010d140, called directly by the debug harness in work/motionK/h) for the inputs given here.
They let the rebuild keep exact parity without Wine.  See docs/motion.md §2-§4 and docs/parity.md
D-01..D-05.
"""
import math

import pytest

from mlaser.motion.interp import arc_interp, seg_interp, seg_interp_time
from mlaser.motion.planner import PathPoint, PlannerParams, plan_velocity


def _line(L, n, feed):
    return [PathPoint(L * i / (n - 1), 1e9, feed, 1e9, 1.0) for i in range(n)]


def _square(side=50.0, corner_R=0.05, feed=150.0):
    pts, s = [], 0.0
    for k in range(4):
        pts.append(PathPoint(s, corner_R if k else 1e9, feed, 1e9, 1.0))
        pts.append(PathPoint(s + side / 2, 1e9, feed, 1e9, 1.0))
        s += side
    pts.append(PathPoint(s, 1e9, feed, 1e9, 1.0))
    return pts


def _circle(R, n, feed):
    L = 2 * math.pi * R
    return [PathPoint(L * i / n, R, feed, 1e9, 1.0) for i in range(n + 1)]


def _mixed():
    return [PathPoint(0, 1e9, 200, 1e9, 1), PathPoint(40, 1e9, 200, 1e9, 1),
            PathPoint(41, 1.0, 200, 1e9, 1), PathPoint(42, 1.0, 200, 1e9, 1),
            PathPoint(60, 1e9, 200, 1e9, 1), PathPoint(70, 1e9, 50, 1e9, 1),
            PathPoint(80, 1e9, 50, 1e9, 0.5), PathPoint(120, 1e9, 200, 1e9, 1)]


MC = dict(start_slow_vel=10.0)          # MotionCtrl.dll parameter defaults (p7 = 10)

# name: (use_curvature, params, points, DLL total time [s], DLL sample count, DLL last sample [mm])
PLANS = {
    "line 100mm feed 100": (1, PlannerParams(**MC), _line(100, 2, 100), 1.1336306209562124, 1134,
                            99.99999906372815),
    "line 3mm": (1, PlannerParams(**MC), _line(3, 2, 200), 0.14423034713726487, 145, 2.9999999348150728),
    "square 50 R0.05": (1, PlannerParams(**MC), _square(), 1.9211498968365766, 1922, 199.99999998473155),
    "square 50 R0.5 acc 5000 T 0.06": (1, PlannerParams(max_acc=5000, acc_time=0.06, max_vel=300, **MC),
                                       _square(corner_R=0.5, feed=300), 0.9826984126984126, 983,
                                       199.99999053689018),
    "circle R5 (D-04)": (1, PlannerParams(**MC), _circle(5.0, 24, 200), 0.4567513500618037, 457,
                         31.415924973681964),
    "circle R1 p0.1 (D-04)": (1, PlannerParams(precision=0.1, **MC), _circle(1.0, 24, 200),
                              0.2698470624843496, 270, 6.283183663515796),
    "circle R20 p0.02 (D-04)": (1, PlannerParams(precision=0.02, **MC), _circle(20.0, 36, 200),
                                0.7908185307179586, 791, 125.6637032187412),
    "mixed (D-04)": (1, PlannerParams(**MC), _mixed(), 1.1782733310448328, 1179, 119.99999989109054),
    "start slow 5mm @10": (1, PlannerParams(start_slow_len=5.0, start_slow_vel=10.0), _line(50, 3, 150),
                           0.9681830375546269, 969, 49.99999997220042),
    "use_curvature=0 1e9 ends (D-05)": (0, PlannerParams(**MC), [
        PathPoint(0, 1e9, 200, 1e9, 1), PathPoint(20, 1e9, 200, 30, 1), PathPoint(40, 1e9, 200, 1e9, 1),
        PathPoint(60, 1e9, 200, 80, 1), PathPoint(100, 1e9, 200, 1e9, 1)], 0.303831124765535, 304,
        59.93351363319121),
    "use_curvature=0 realistic (D-05)": (0, PlannerParams(**MC), [
        PathPoint(0, 1e9, 200, 0, 1), PathPoint(20, 1e9, 200, 30, 1), PathPoint(40, 1e9, 200, 150, 1),
        PathPoint(60, 1e9, 200, 80, 1), PathPoint(100, 1e9, 200, 0, 1)], 0.9145845506780101, 915,
        99.99999893471642),
    "min_vel 20 square": (1, PlannerParams(min_vel=20.0, **MC), _square(), 1.8502904044858706, 1851,
                          199.99999988897326),
    # CADModule copy only (ten parameters: slow start p6/p7 and slow end p8/p9)
    "CAD slow start+end": (1, PlannerParams(start_slow_len=5.0, start_slow_vel=10.0, end_slow_len=3.0,
                                            end_slow_vel=20.0), _line(50, 3, 150),
                           1.1719040156320755, 1172, 49.99999818747344),
    "CAD path shorter than slow start": (1, PlannerParams(start_slow_len=10.0, start_slow_vel=30.0),
                                         [PathPoint(0, 1e9, 50), PathPoint(1.7307302137669103, 181.66295961089932, 50),
                                          PathPoint(2.209071438833827, 10000, 50)],
                                         0.15895179421293018, 159, 2.2090689095905915),
    "CAD R2 arc between lines": (1, PlannerParams(max_acc=3000, acc_time=0.1, max_vel=250),
                                 [PathPoint(i * 0.4, 2.0 if 10 < i < 30 else 1e9, 250) for i in range(60)],
                                 0.34883228116164644, 349, 23.599994234855544),
}


@pytest.mark.parametrize("name", list(PLANS))
def test_planner_matches_dll(name):
    uc, prm, pts, t_dll, n_dll, last_dll = PLANS[name]
    plan = plan_velocity(pts, prm, bool(uc))
    s = plan.sample()
    assert plan.duration == pytest.approx(t_dll, abs=1e-12)
    assert len(s) == n_dll
    assert s[-1] == pytest.approx(last_dll, abs=1e-9)
    assert all(b >= a - 1e-12 for a, b in zip(s, s[1:]))


# (L, vmax, vs, ve, acc, jerk, period_ms, mode): DLL sample count, DLL segInterp_time [ms]
SEGS = [
    ((10, 100, 0, 0, 1000, 10000, 1, 2), 318, 317.48231169801096),      # bisection case (D-01b)
    ((100, 200, 0, 0, 2000, 32000, 1, 2), 663, 662.5),
    ((100, 200, 50, 20, 2000, 32000, 1, 2), 621, 620.1875),
    ((2, 300, 0, 0, 3000, 50000, 1, 2), 109, 108.57093795090323),       # bisection case (D-01b)
    ((0.5, 100, 0, 80, 1000, 10000, 1, 2), 13, 12.5),
    ((50, 150, 150, 150, 1500, 20000, 1, 2), 334, 333.3333333333333),
    ((30, 120, 0, 0, 800, 6000, 0.5, 2), 1086, 542.940190914817),
    ((10, 300, 400, 0, 2000, 20000, 1, 2), 67, 66.66666666666667),
    ((1, 200, 0, 0, 2000, 32000, 1, 1), 45, 44.721359549995796),
]


@pytest.mark.parametrize("args,n_dll,t_dll", SEGS)
def test_seg_interp_matches_dll(args, n_dll, t_dll):
    s = seg_interp(*args)
    assert len(s) == n_dll and s[-1] == pytest.approx(1.0, abs=1e-15) and s[0] == 0.0
    assert seg_interp_time(*args[:6], mode=args[7]) == pytest.approx(t_dll, abs=1e-9)


# arcInterp: (a0, a1, R, vmax, vs, ve, accX, accY, jerkX, jerkY, period_ms, mode) -> DLL sample count
ARCS = [
    ((0, 1.5, 20, 100, 0, 0, 1000, 1000, 10000, 10000, 1, 2), 500),
    ((0, 1.5, 20, 100, 0, 0, 1000, 1000, 10000, 10000, 1, 1), 399),      # D-03
    ((0, 2 * math.pi, 10, 150, 0, 0, 2000, 1500, 30000, 20000, 1, 1), 564),
    ((0.3, -2.0, 50, 200, 20, 10, 2000, 2000, 30000, 30000, 1, 1), 658),
    ((0, 0.01, 2, 100, 10, 10, 1000, 1000, 10000, 10000, 1, 1), 3),
    ((0, math.pi, 1, 200, 0, 0, 3000, 3000, 50000, 50000, 1, 1), 74),
]


@pytest.mark.parametrize("args,n_dll", ARCS)
def test_arc_interp_counts_match_dll(args, n_dll):
    s = arc_interp(*args)
    assert len(s) == n_dll and s[-1] == pytest.approx(1.0, abs=1e-15)


def test_seg_interp_shorter_than_half_period_is_empty():
    # FUN_1000f730: trunc(T/period + 0.5) < 1 -> no samples.  Observed with CADModule's copy
    # (0x101078a0 / 0x10106e70): vs/ve are clipped to vmax before the 'too short' test, T = 0.466 ms.
    args = (0.0093115571680636, 20, 150, 300, 3000, 2000, 1, 2)
    assert seg_interp(*args) == []
    assert seg_interp_time(*args[:6], mode=2) == pytest.approx(0.46557785840318, abs=1e-12)
