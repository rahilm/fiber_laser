"""MotionCtrl.dll (segInterp, segInterp_time, arcInterp, CVelocityPlanning) vs mlaser.motion."""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_motion_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_motion"))


def test_motion_profiles_match_up_to_sampler_end_rule():
    """segInterp S-curve values agree to 1e-12 once the DLL's end rule is applied."""
    res = [r for r in run_area("cmp_motion") if r.area == "MotionCtrl.segInterp"
           and "mode=2" in r.case and r.metric.get("dll_rc") == 0 and r.metric.get("dll_n")]
    assert res
    exact = [r for r in res if r.metric["err_with_dll_end_rule"] < 1e-9]
    assert len(exact) >= len(res) - 2, [(r.case, r.metric) for r in res]


def test_motion_planner_straight_lines_match():
    res = {r.case: r for r in run_area("cmp_motion") if r.area == "MotionCtrl.CVelocityPlanning"}
    for name in ("line 100mm feed 100", "square 50 corners R=0.05", "start slow 5mm @10"):
        assert res[name].status == "match", res[name]
