"""App level: the rebuild's DXF import -> .chf is byte-identical to MainApp's import -> Save As
(uninitialised ints skipped, docs/parity.md D-12), and the lead command on the rebuild's own
import picks the original's start points (D-19)."""
import pytest

from parity_helpers import run_area


def test_rebuild_import_bytes_match_mainapp():
    res = [r for r in run_area("cmp_chf", needs_wine=False)
           if r.area == "rebuild DXF->.chf bytes vs MainApp DXF->.chf"]
    if not res:
        pytest.skip("no app-level captures")
    import known_diffs
    bad = [(r.case, r.metric) for r in res if r.status != "match" and known_diffs.lookup(r) is None]
    assert not bad, bad
    # everything except the TEXT sample (D-16, hinted GDI outlines) is byte-identical
    assert sum(r.status == "match" for r in res) >= len(res) - 1


def test_lead_on_rebuild_import_matches_original_start_points():
    res = [r for r in run_area("cmp_lead", needs_wine=False) if r.case.startswith("import_dxf(")]
    if not res:
        pytest.skip("no app-level lead capture")
    assert res[0].status == "match", res[0].metric
    starts = [row["rebuild"] for row in res[0].metric["per_contour"] if row["rebuild_lead"]]
    assert 0.374474 in starts          # closed LWPOLYLINE: middle of its 34.8 mm arc
