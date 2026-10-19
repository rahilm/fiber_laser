"""Machine-level parity: card traffic of the ORIGINAL MainApp for manual machine operations
(Go Origin, jog, step, mark/return, 回零位, outputs, laser, stop, coordinate origins, laser type)
vs the rebuild's UiController / MachineOps on its CardSimulator.

Reads the capture made by the parity harness's run_app.sh machine; skips when the
harness, its capture or Wine is missing (MLASER_PARITY=0 skips explicitly).  Frame / Dry Cut /
Start are refused by the original (hardware authorisation state), so they are only checked for
"no FIFO traffic"; see docs/re/parity.md §7.  Every other action must match exactly.
"""
from parity_helpers import assert_no_new_discrepancies, run_area


def _results():
    import pytest
    res = run_area("cmp_machine", needs_wine=True)
    if all(r.status == "skip" for r in res):
        pytest.skip("no machine-level capture (run parity/run_app.sh machine)")
    return res


def test_machine_ops_no_new_discrepancies():
    assert_no_new_discrepancies(_results())


def test_refused_operations_sent_no_fifo_traffic():
    """Frame / Dry Cut / Start / BreakPt: the original never streamed anything (if it ever does,
    cmp_machine reports an error so the FIFO comparison gets written)."""
    res = [r for r in _results() if r.case in ("frame 边框", "dry run 空走", "start 开始", "breakpoint 断点定位")]
    assert res and all(r.status == "skip" for r in res), [(r.case, r.status, r.metric) for r in res]


def test_matching_operations_still_match():
    """Every compared action matches the original request for request (D-20..D-32 fixed by agent P,
    docs/re/parity.md section 7); no machine-level discrepancy is accepted."""
    res = _results()
    import known_diffs          # compare/ is on sys.path once run_area ran
    assert not [k for k in known_diffs.KNOWN if k["area"] == "MainApp machine operations"]
    bad = [(r.case, r.status, r.metric) for r in res if r.status not in ("match", "skip")]
    assert not bad, bad


def test_jog_addresses_the_axis_number():
    """Safety (D-21): every jog the original sent carries the axis number (X 0, Y 1), and the
    rebuild's jog actions matched it."""
    res = {r.case: r for r in _results()}
    jogs = [r for c, r in res.items() if c.split(" ")[0] in ("jog", "step", "fast")]
    assert jogs and all(r.status == "match" for r in jogs), [(r.case, r.status) for r in jogs]
    for r in jogs:
        first = r.metric["original"][0]
        assert first.startswith("0x40 101 x6 [3, 0,") or first.startswith("0x40 101 x6 [3, 1,"), first
