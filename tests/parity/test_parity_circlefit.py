"""CircleFitDLL.dll CCircleFit::mainFit vs mlaser.cam.cleanup.fit_arcs."""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_circlefit_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_circlefit"))


def test_circlefit_circles_and_simple_arcs_match():
    res = {r.case.split(" (")[0]: r for r in run_area("cmp_circlefit")}
    for name in ("circle R10, 72 pts", "half circle R20 open, 60 pts", "line+arc+line",
                 "rounded rectangle 20x10 r2"):
        assert res[name].status == "match", res[name]
