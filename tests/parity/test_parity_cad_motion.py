"""CADModule.dll's motion copy (segInterp / segInterp_time copies, CVelocityPlanning) vs
mlaser.motion.  This is the main motion reference: MainApp uses CADModule's copy, not
MotionCtrl.dll (docs/motion.md §1, docs/parity.md)."""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_cad_motion_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_cad_motion"))


def test_cad_planner_cases_and_fuzz_match_exactly():
    res = [r for r in run_area("cmp_cad_motion") if r.area == "CADModule.CVelocityPlanning"]
    assert len(res) >= 18
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]
    fuzz = next(r for r in res if r.case.startswith("fuzz"))
    assert fuzz.metric["paths"] >= 150 and fuzz.metric["differ"] == 0


def test_cad_scurve_fuzz_and_version():
    res = run_area("cmp_cad_motion")
    fuzz = next(r for r in res if r.area == "CADModule.segInterp" and r.case.startswith("fuzz"))
    assert fuzz.status == "match", fuzz.metric
    ver = next(r for r in res if r.area == "CADModule.CVelocityPlanning.version")
    assert ver.status == "match", ver.metric
