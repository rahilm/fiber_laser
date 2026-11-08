"""Nesting engine: no overlaps, inside sheet, quantities, holes / part-in-part, speed."""
import time

import pytest
import shapely

from mlaser.model import Arc, Contour, Line, Part
from mlaser.nest import (FIT_RECT, GRAVITY_BOTTOM, NestJob, NestParams, NestPart, NestSheet,
                         apply_result, nest, stats)
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


#: seconds :func:`_reference_work` takes on the development host (Linux x86-64, Python 3.12)
REFERENCE_WORK_S = 0.035


def _reference_work() -> float:
    """Seconds a fixed mix of what nesting does (shapely buffers / unions / predicates and
    plain Python geometry) takes now: the speed of this runner under its current load."""
    t = time.perf_counter()
    polys = [shapely.box(x * 7.0, (x % 13) * 5.0, x * 7.0 + 6.0, (x % 13) * 5.0 + 4.0).buffer(0.5)
             for x in range(300)]
    u = shapely.union_all(polys)
    hits = sum(1 for p in polys if u.intersects(p) and not p.disjoint(u))
    acc = 0.0
    for i in range(60_000):
        acc += (i % 97) * 0.5 - (i % 31) * 0.25
    assert hits == len(polys) and acc
    return time.perf_counter() - t


def _budget(seconds: float) -> float:
    """``seconds`` (the bound on the development host) scaled by how much slower this runner
    is right now (a shared Windows runner was 6x slower: 5.6 s for a 0.85 s nest), never less.
    A real slowdown of the nesting (e.g. trying all 10,000 sheet copies) is still far over."""
    slow = min(_reference_work() for _ in range(3)) / REFERENCE_WORK_S
    return seconds * max(1.0, slow)


def placed_geoms(job, lay):
    return [(pl, parts_geometry([transform_part(p, pl) for p in job.part(pl.part_id).parts]))
            for pl in lay.placements]


def check_valid(job, res, spacing):
    for lay in res.layouts:
        sh = job.sheet(lay.sheet_id)
        _, usable = sheet_geometry(sh, job.params.margin)
        gs = placed_geoms(job, lay)
        for _, g in gs:
            assert usable.buffer(1e-6).contains(g)
        for i in range(len(gs)):
            for j in range(i + 1, len(gs)):
                d = gs[i][1].distance(gs[j][1])
                assert d >= spacing - 1e-4, (i, j, d)
                assert gs[i][1].intersection(gs[j][1]).area < 1e-6


def mixed_job(**kw):
    kw.setdefault("part_in_part", True)
    job = NestJob(params=NestParams(spacing=3, margin=5, rotation_step=90, **kw))
    job.add_part(NestPart([Part([rect(0, 0, 300, 200), circle(150, 100, 70)])], name="plate", quantity=6))
    job.add_part(NestPart([Part([circle(0, 0, 30)])], name="disc", quantity=20))
    job.add_part(NestPart([Part([lshape(200, 150, 40)])], name="L", quantity=10, mirror=True))
    job.add_sheet(NestSheet(1200, 800, quantity=2))
    return job


def test_no_overlap_inside_sheet_quantities():
    job = mixed_job()
    res = nest(job)
    assert res.unplaced == {}
    assert res.placed == {0: 6, 1: 20, 2: 10}
    assert sum(len(l.placements) for l in res.layouts) == 36
    check_valid(job, res, 3)
    st = stats(job)
    assert 0 < st["utilisation"] < 1


def test_part_in_part_uses_holes():
    job = mixed_job()
    nest(job)
    lay = job.result.layouts[0]
    holes = [shapely.Point(pl.apply((150, 100))) for pl in lay.placements if pl.part_id == 0]
    discs = [shapely.Point(pl.apply((0, 0))) for pl in lay.placements if pl.part_id == 1]
    inside = sum(1 for d in discs for h in holes if d.distance(h) < 70)
    assert inside >= 1
    # disabled: no disc may sit in a hole
    job2 = mixed_job(part_in_part=False)
    nest(job2)
    for lay in job2.result.layouts:
        holes = [shapely.Point(pl.apply((150, 100))) for pl in lay.placements if pl.part_id == 0]
        discs = [shapely.Point(pl.apply((0, 0))) for pl in lay.placements if pl.part_id == 1]
        assert not any(d.distance(h) < 70 for d in discs for h in holes)
    check_valid(job2, job2.result, 3)


def test_overflow_to_second_sheet_and_unplaced():
    job = NestJob(params=NestParams(spacing=2, margin=0))
    job.add_part(NestPart([Part([rect(0, 0, 90, 90)])], quantity=5))
    job.add_part(NestPart([Part([rect(0, 0, 500, 500)])], quantity=1))   # never fits
    job.add_sheet(NestSheet(200, 200, quantity=3))
    res = nest(job)
    assert res.placed[0] == 5
    assert res.unplaced == {1: 1}
    assert len(res.layouts) == 2 and len(res.layouts[0].placements) == 4
    check_valid(job, res, 2)


def test_max_quantity_fills_sheet():
    job = NestJob(params=NestParams(spacing=0, margin=0, rotation_step=0, resolution=1.0))
    job.add_part(NestPart([Part([rect(0, 0, 50, 50)])], quantity=2, max_quantity=100))
    job.add_sheet(NestSheet(220, 110))
    res = nest(job)
    # 4 x 2 squares of 50 (the raster needs ~1 cell of slack per part; zero spacing)
    assert res.placed[0] == 8
    check_valid(job, res, 0)


def test_remnant_sheet_with_defect_and_bottom_gravity():
    outline = [Part([rect(0, 0, 400, 300), rect(150, 100, 100, 100)])]   # defect hole in the middle
    job = NestJob(params=NestParams(spacing=2, margin=3, gravity=GRAVITY_BOTTOM))
    job.add_part(NestPart([Part([rect(0, 0, 60, 40)])], quantity=25, fit=FIT_RECT))
    job.add_sheet(NestSheet(outline=outline, is_remnant=True))
    res = nest(job)
    check_valid(job, res, 2)
    defect = shapely.box(150, 100, 250, 200)
    for _, g in placed_geoms(job, res.layouts[0]):
        assert g.distance(defect) >= 3 - 1e-6


def test_apply_result_document():
    job = mixed_job()
    nest(job)
    doc = apply_result(job, layout_index=0, include_sheet=True)
    assert doc.parts[0].extra["nest_sheet"] == 0
    nested = [p for p in doc.parts if "nest" in p.extra]
    assert len(nested) == len(job.result.layouts[0].placements)
    pl = job.result.layouts[0].placements[0]
    src = job.part(pl.part_id).parts[0].contours[0].segments[0].start
    assert nested[0].contours[0].segments[0].start == pytest.approx(pl.apply(src))
    # mirrored arcs keep a consistent shape (circle area preserved)
    for p in nested:
        assert parts_geometry([p]).area == pytest.approx(parts_geometry(job.part(p.extra["nest"]["part_id"]).parts).area, rel=1e-6)


@pytest.mark.flaky(reruns=1)            # a timing bound on a shared runner (one rerun, like the other timing tests)
def test_hundred_parts_fast():
    job = NestJob(params=NestParams(spacing=3, margin=5, rotation_step=90))
    for k in range(10):
        job.add_part(NestPart([Part([lshape(80 + 10 * k, 60 + 5 * k, 20)])], quantity=6))
    job.add_part(NestPart([Part([circle(0, 0, 25), circle(0, 0, 10)])], quantity=40))
    job.add_sheet(NestSheet(2500, 1250, quantity=2))
    budget = _budget(10.0)
    t = time.monotonic()
    res = nest(job)
    assert time.monotonic() - t < budget
    assert sum(res.placed.values()) == 100
    check_valid(job, res, 3)


def test_common_line_parts_touch_and_share_edges():
    from mlaser.cam.commonedge import find_shared_edges
    job = NestJob(params=NestParams(margin=0, common_line=True, rotation_step=0, share_edge_min_len=20))
    job.add_part(NestPart([Part([rect(0, 0, 100, 50)])], quantity=6))
    job.add_sheet(NestSheet(400, 200))
    res = nest(job)
    assert res.placed[0] == 6
    check_valid(job, res, 0)
    doc_parts = [transform_part(p, pl) for pl in res.layouts[0].placements for p in job.part(0).parts]
    shared = find_shared_edges([c for p in doc_parts for c in p.contours], min_len=20)
    assert len(shared) >= 5          # 6 rectangles in a block share at least 5 edges
    doc = apply_result(job, layout_index=0)
    cut = sum(c.length() for p in doc.parts for c in p.contours)
    assert cut < 6 * 300 - 4 * 50    # common edges are cut once


@pytest.mark.flaky(reruns=1)            # a timing bound on a shared runner (one rerun, like the other timing tests)
def test_auto_direction_and_unlimited_sheet_copies():
    from mlaser.nest import GRAVITY_AUTO
    job = NestJob(params=NestParams(spacing=2, margin=2, gravity=GRAVITY_AUTO))
    job.add_part(NestPart([Part([rect(0, 0, 150, 40)])], quantity=8))
    job.add_part(NestPart([Part([rect(0, 0, 900, 900)])], quantity=1))   # never fits
    job.add_sheet(NestSheet(300, 600, quantity=10000))
    budget = _budget(5.0)
    t = time.monotonic()
    res = nest(job)
    assert time.monotonic() - t < budget
    assert res.placed[0] == 8 and res.unplaced == {1: 1}
    assert len(res.layouts) == 1 and res.gravity in ("left", "bottom")
    check_valid(job, res, 2)
