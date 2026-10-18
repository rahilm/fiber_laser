"""DxfParseDllvc100.dll (newDxfFileParse) vs mlaser.io.dxf_import.import_dxf."""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_dxf_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_dxf"))


def test_dxf_basic_entities_match():
    res = {r.case: r for r in run_area("cmp_dxf")}
    for name in ("basic_r2000.dxf", "r12_polyline.dxf", "extrusion.dxf", "gear.dxf"):
        assert res[name].status == "match", res[name]
