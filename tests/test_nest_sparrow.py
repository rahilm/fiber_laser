"""Optional sparrow (spyrrow) nesting engine: selection, validity, multi-sheet driver."""

import sys

import pytest

from mlaser.model import Arc, Contour, Line, Part
from mlaser.nest import (
    ENGINE_RASTER,
    ENGINE_SPYRROW,
    GRAVITY_BOTTOM,
    NestEngineUnavailable,
    Nester,
    NestJob,
    NestParams,
    NestPart,
    NestSheet,
    apply_result,
    engine_class,
    nest,
)
from mlaser.nest.shapes import parts_geometry, sheet_geometry, transform_part


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def circle(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def lshape(w, h, t):
    pts = [(0, 0), (w, 0), (w, t), (t, t), (t, h), (0, h)]
    return Contour([Line(pts[i], pts[(i + 1) % 6]) for i in range(6)], True)


def check_valid(job, res):
    sp = job.params.effective_spacing()
    for lay in res.layouts:
        _, usable = sheet_geometry(job.sheet(lay.sheet_id), job.params.margin)
        gs = [
            parts_geometry([transform_part(p, pl) for p in job.part(pl.part_id).parts])
            for pl in lay.placements
        ]
        for g in gs:
            assert usable.buffer(1e-3).contains(g)
        for i in range(len(gs)):
            for j in range(i + 1, len(gs)):
                assert gs[i].distance(gs[j]) >= sp - 1e-3, (i, j)
                assert gs[i].intersection(gs[j]).area < 1e-6


def small_job(n_rect=6, n_l=3, n_ring=2, sheets=1, w=600.0, h=400.0, **kw):
    kw.setdefault("time_limit_s", 4.0)
    job = NestJob(params=NestParams(spacing=3, margin=5, rotation_step=90, engine=ENGINE_SPYRROW, **kw))
    job.add_part(NestPart([Part([rect(0, 0, 120, 60)])], name="rect", quantity=n_rect))
    job.add_part(NestPart([Part([lshape(200, 150, 40)])], name="L", quantity=n_l))
    job.add_part(NestPart([Part([circle(0, 0, 60), circle(0, 0, 35)])], name="ring", quantity=n_ring))
    job.add_sheet(NestSheet(w, h, quantity=sheets))
    return job


# ------------------------------------------------------------------ selection (no spyrrow needed)
def test_engine_selection_default_is_raster():
    assert NestParams().engine == ENGINE_RASTER
    assert engine_class(ENGINE_RASTER) is Nester
    with pytest.raises(NestEngineUnavailable, match="Unknown nesting engine"):
        engine_class("nope")


def test_spyrrow_missing_fails_cleanly(monkeypatch):
    monkeypatch.setitem(sys.modules, "spyrrow", None)  # import spyrrow -> ImportError
    from mlaser.nest import available_engines

    assert available_engines() == [ENGINE_RASTER]
    job = small_job()
    with pytest.raises(NestEngineUnavailable, match=r"mlaser\[nest\]"):
        nest(job)
    assert job.result is None


# ------------------------------------------------------------------ the engine
def _need_spyrrow():
    pytest.importorskip("spyrrow")


def test_small_job_no_overlap():
    _need_spyrrow()
    job = small_job()
    res = nest(job)
    assert res.engine == ENGINE_SPYRROW
    assert res.unplaced == {}
    assert len(res.layouts) == 1
    assert sum(len(lay.placements) for lay in res.layouts) == 11
    check_valid(job, res)
    assert all(pl.angle % 90 == 0 and not pl.mirror for pl in res.layouts[0].placements)
    doc = apply_result(job)  # the apply step understands the placements
    assert len(doc.parts) == 11


def test_multi_sheet_overflow_and_quantities():
    _need_spyrrow()
    job = small_job(n_rect=20, n_l=6, n_ring=4, sheets=3, time_limit_s=6.0)
    res = nest(job)
    assert res.unplaced == {}
    assert res.placed == {0: 20, 1: 6, 2: 4}  # 248k mm2 of parts > 230k mm2 usable
    assert 2 <= len(res.layouts) <= 3
    check_valid(job, res)


def test_sheet_limit_leaves_unplaced():
    _need_spyrrow()
    job = small_job(n_rect=40, n_l=0, n_ring=0, sheets=1, time_limit_s=3.0)
    res = nest(job)
    assert len(res.layouts) == 1
    assert 0 < res.placed[0] < 40 and res.unplaced[0] == 40 - res.placed[0]
    check_valid(job, res)


def test_gravity_bottom_frame_and_too_large_part():
    _need_spyrrow()
    job = small_job(n_rect=5, n_l=1, n_ring=1, gravity=GRAVITY_BOTTOM, time_limit_s=3.0)
    job.add_part(NestPart([Part([rect(0, 0, 900, 50)])], name="huge", quantity=1))
    res = nest(job)
    assert res.unplaced == {3: 1}
    check_valid(job, res)
    lay = res.layouts[0]
    assert lay.remnant is not None and lay.remnant[1] > 5  # filled from the bottom


def test_angles_spacing_and_notes():
    _need_spyrrow()
    job = small_job(n_rect=8, n_l=2, n_ring=1, part_in_part=True, time_limit_s=3.0)
    job.params.spacing = 8.0
    job.parts[0].angles = [0.0]
    res = nest(job)
    check_valid(job, res)
    assert {pl.angle for lay in res.layouts for pl in lay.placements if pl.part_id == 0} == {0.0}
    assert any("part-in-part" in n for n in res.notes)


def test_fill_up_to_max_quantity():
    _need_spyrrow()
    job = small_job(n_rect=2, n_l=0, n_ring=0, time_limit_s=3.0)
    job.parts[0].max_quantity = 60
    res = nest(job)
    assert res.unplaced == {}
    assert 2 < res.placed[0] <= 60
    check_valid(job, res)
