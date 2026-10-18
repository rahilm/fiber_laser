"""Cutting-job pipeline, NCModule half: the original FIFO packer FUN_10043c20 (called by address
with ManuItems we build, harness/nc_job_harness.c) vs the rebuild's encoders
(frames.encode_step, process.ManuEncoder, frames.build_fifo_frames = Job.frames).
Skips without Wine; see docs/parity.md "Cutting-job pipeline"."""
from parity_helpers import assert_no_new_discrepancies, run_area


def _res():
    return run_area("cmp_job_nc")


def test_job_nc_no_new_discrepancies():
    assert_no_new_discrepancies(_res())


def test_motion_steps_and_frames_word_identical():
    res = [r for r in _res() if r.case.startswith("motion:") and r.status != "skip"]
    assert len(res) >= 6
    assert all(r.status == "match" for r in res), [(r.area, r.case, r.metric) for r in res if r.status != "match"]


def test_system_commands_and_follower_items_word_identical():
    names = ("class 1: laser/DA/outputs/delays/sync", "class 1 sub 6: crash protect on",
             "follower classes 6-0x13, ZF type 1", "follower classes, crash protect + peak DA (IO laser, DA 2)",
             "follower classes, dry run", "3001 marker rules (0xe, class 3, motion <-> process)")
    res = [r for r in _res() if r.area == "NCModule.FUN_10043c20 items" and r.case in names]
    assert len(res) == len(names)
    assert all(r.status == "match" for r in res), [(r.case, r.note) for r in res if r.status != "match"]


def test_pierce_sequences_without_focus_axis_identical():
    res = [r for r in _res() if r.area == "NCModule.FUN_10043c20 items"
           and r.case.startswith("pierce + start/end cut") and "AF type" not in r.case]
    assert len(res) >= 8
    assert all(r.status == "match" for r in res), [(r.case, r.note) for r in res if r.status != "match"]


def test_mainapp_manuitems_pack_identically():
    """Agent R's dumps of the ORIGINAL MainApp builders' ManuItems, packed by both encoders."""
    res = [r for r in _res() if r.area == "NCModule.FUN_10043c20 items" and r.case.startswith("MainApp ManuItems")]
    if not res:
        import pytest
        pytest.skip("no out/job/mainapp_manu_*.json (run cmp_job_mainapp first)")
    assert all(r.status == "match" for r in res), [(r.case, r.note) for r in res if r.status != "match"]


def test_compile_job_word_and_frame_identical():
    """Full compile_job streams: every ManuItem packs word-identically (no 3001 at program start,
    JN-01) and Job.frames has the original's frame boundaries (size test once per ManuItem, JN-08)."""
    res = [r for r in _res() if r.area == "NCModule.FUN_10043c20 compile_job items"]
    assert len(res) >= 3
    for r in res:
        assert r.metric["reencode_equals_job_wire"] and r.metric["unrepresented_wire_items"] == 0, r.metric
        assert r.metric["items_differing"] == 0 and r.status == "match", (r.case, r.note)
    fr = [r for r in _res() if r.area == "NCModule.FUN_10043c20 compile_job frames"]
    assert len(fr) >= 3
    for r in fr:
        assert r.status == "match" and r.metric["job_frames_equal_per_item_packing"], (r.case, r.metric, r.note)


def test_focus_zf0_and_unhandled_classes_identical():
    """Focus axis (AF 5/6, +50 ms wait, axis 0x10, start position), ZF type 0 fall-through,
    classes 2 / 0x14.. as steps and a first peak of 0 % (JN-02..JN-07 fixed)."""
    res = [r for r in _res() if r.area in ("NCModule.FUN_10043c20 items", "NCModule.FUN_10043c20 frames")
           and r.status != "skip" and not r.case.startswith("batches")]
    assert len(res) >= 40
    assert all(r.status == "match" for r in res), [(r.area, r.case, r.note) for r in res if r.status != "match"]
