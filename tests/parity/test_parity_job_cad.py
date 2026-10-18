"""Cutting-job pipeline, CADModule half: contours loaded and crafted by the original CADModule
(.chf body loader, lead setter, primitive list, CInterpMrg::Interp; harness/cad_job_harness.c)
vs cam.toolpath.prepare_contour + motion.interp.plan_path sampling.  Skips without Wine; see
docs/parity.md "Cutting-job pipeline"."""
from parity_helpers import assert_no_new_discrepancies, run_area


def _res():
    return run_area("cmp_job_cad")


def test_job_cad_no_new_discrepancies():
    assert_no_new_discrepancies(_res())


def test_plain_and_lead_geometry_matches():
    res = [r for r in _res() if r.area == "CADModule job: crafts -> primitives"
           and any(r.case.endswith(v) for v in ("/ plain", "/ lead 45deg 5", "/ micro-joints 2x0.6 + lead",
                                                "/ overcut 1.5"))]
    assert len(res) >= 20
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]


def test_circles_sample_identically():
    res = [r for r in _res() if r.area == "CADModule job: CInterpMrg samples"
           and (r.case.startswith("circle r15 / plain") or r.case.startswith("hole r8 (in rect) / plain"))]
    assert len(res) == 2          # one row per case: the Machine default pipeline (smoothing on)
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]


def test_smoothed_corners_and_lead_joints_sample_identically():
    """CContoutSmooth (corner blends, lead-in joint at lead_tol, arc merges, micro-joint
    switch list) matches the original sample for sample on the plain shapes and lead crafts."""
    want = ("rect60x40 / plain", "rect60x40 / lead line 3", "circle r15 / lead line 3",
            "polyline with arcs / lead arc 3", "hole r8 (in rect) / micro-joints 2x0.6 + lead",
            "circle r15 / overcut 1.5")
    res = [r for r in _res() if r.area == "CADModule job: CInterpMrg samples" and r.case.split(" [")[0] in want]
    assert len(res) == len(want)
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]


def test_start_rule_matches_on_the_purpose_built_shapes():
    res = [r for r in _res() if r.area == "CADModule job: start point 0x10071f80" and ".dxf" not in r.case]
    assert len(res) >= 12
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]


def test_laser_switches_match_without_micro_joints():
    res = [r for r in _res() if r.area == "CADModule job: laser switch / per-sample laser"
           and "micro-joints" not in r.case]
    assert len(res) >= 60
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]
