"""NCModule.dll CExtModbus frame codec (FUN_10028190 / FUN_10029a90) vs mlaser.controller.frames."""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_nc_codec_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_nc"))


def test_nc_encoder_byte_identical():
    res = [r for r in run_area("cmp_nc") if r.area == "NCModule.CExtModbus.encode"]
    assert len(res) >= 30
    assert all(r.status == "match" for r in res), [r for r in res if r.status != "match"]
