"""App-level parity: files and card frames produced by the ORIGINAL MainApp under Wine.

These read captures made by /home/dev/scratch/mlaser/parity/run_app.sh (MainApp driven with
xdotool inside a loopback-only network namespace); they skip when no capture exists.
"""
from parity_helpers import assert_no_new_discrepancies, run_area


def test_chf_written_by_original_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_chf", needs_wine=False))


def test_chf_written_by_original_round_trips_byte_identical():
    """loads_chf -> dumps_chf reproduces every .chf the original wrote, byte for byte, and every
    file loads to the same shape as the DXF, except where the original itself changed the curve
    (known discrepancies, e.g. D-08b: CADModule's quadratic -> cubic spline conversion)."""
    import pytest
    res = run_area("cmp_chf", needs_wine=False)
    rt = [r for r in res if r.area == "formats.chf round trip (original file)"]
    if not rt:
        pytest.skip("no app-level captures")
    assert all(r.status == "match" for r in rt), [(r.case, r.metric) for r in rt if r.status != "match"]
    import known_diffs          # compare/ is on sys.path (run_area)
    lookup = known_diffs.lookup
    loads = [r for r in res if r.area == "formats.chf.loads_chf(original file)"]
    bad = [(r.case, r.metric) for r in loads if r.status != "match" and lookup(r) is None]
    assert not bad, bad


def test_card_traffic_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_app_frames", needs_wine=False))


def test_lead_no_new_discrepancies():
    assert_no_new_discrepancies(run_area("cmp_lead", needs_wine=False))
