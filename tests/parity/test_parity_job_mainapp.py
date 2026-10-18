"""MainApp.exe job builder (FUN_00462630 and the process sequences) vs the rebuild's compiler.

The harness maps MainApp.exe under Wine and calls the job builder's compute functions with
objects it builds itself (docs/parity.md, "Cutting-job pipeline: MainApp job builder").  Skips
without Wine, the built harness or the copy of the original install.
"""
import pytest

from parity_helpers import PARITY_DIR, assert_no_new_discrepancies, run_area


def _results():
    if not (PARITY_DIR / "build" / "job_mainapp_harness.exe").exists():
        pytest.skip("job_mainapp_harness.exe not built (parity/build.sh)")
    if not (PARITY_DIR / "orig" / "MainApp.exe").exists():
        pytest.skip("copy of the original install (parity/orig) missing")
    return run_area("cmp_job_mainapp")


def test_job_mainapp_no_new_discrepancies():
    assert_no_new_discrepancies(_results())


def test_job_mainapp_sequences_match():
    """Gas, laser, delay, pierce (1..5 stages, gradual, focus gradual, smooth), start cut and end cut
    (lift / short-hop rules), bolt drill and the job loop's sync-on + start-cut composition match
    item by item (JB-04/05/06 fixed)."""
    res = [r for r in _results() if r.area == "MainApp.job_builder.sequences"]
    assert len(res) >= 30
    assert all(r.status == "match" for r in res), [(r.case, r.note) for r in res if r.status != "match"]


def test_job_mainapp_duty_freq_match():
    """Per-sample duty/frequency (speed curves, slow start/end overrides, micro-joint gaps with the
    FUN_00469950 inclusive rule) and the speeds MainApp hands to CAD 0x224/0x228 agree on every cut
    run."""
    res = [r for r in _results() if r.area == "MainApp.job_builder.FUN_00462630.duty_freq"]
    assert len(res) >= 5
    assert all(r.status == "match" for r in res), [(r.case, r.metric) for r in res if r.status != "match"]


def test_job_mainapp_pulses_and_power_identical():
    """One step per sample including sample 0, FUN_00473bd0's truncation + carry and +8 = 0xFFFF
    (JB-01/JB-02 fixed): identical deltas, end position and power word on every cut run."""
    res = [r for r in _results() if r.area in ("MainApp.job_builder.FUN_00462630.pulses",
                                               "MainApp.job_builder.FUN_00462630.power")]
    assert len(res) >= 16
    for r in res:
        assert r.status == "match", (r.area, r.case, r.metric, r.note)
        if r.area.endswith(".pulses"):
            assert r.metric["max_dxy"] == 0 and r.metric["max_pos_lag"] == 0, (r.case, r.metric)
            assert r.metric["items_orig"] == r.metric["steps_rebuild"], (r.case, r.metric)


def test_job_mainapp_on_cad_samples():
    """On CADModule's own samples (agent Q's dumps, when present) the duty of contours without micro
    joints is identical, the pulses are identical item by item and the micro-joint gap samples
    follow FUN_00469950's inclusive rule."""
    allr = [r for r in _results() if ".cad_samples" in r.area]
    res = {r.area: r for r in allr if not r.area.endswith(".gaps")}
    if any(r.status == "skip" for r in res.values()) or not res:
        pytest.skip("CADModule sample dumps (out/job/cad_samples.json) not available")
    assert res["MainApp.job_builder.FUN_00462630.cad_samples.duty"].status == "match"
    r = res["MainApp.job_builder.FUN_00462630.cad_samples.pulses"]
    assert r.status == "match" and r.metric["max_dxy"] == 0 and r.metric["count_mismatch"] == 0, r.metric
    gaps = [r for r in allr if r.area.endswith(".gaps")]
    assert all(r.status == "match" for r in gaps), [(r.case, r.metric) for r in gaps if r.status != "match"]
