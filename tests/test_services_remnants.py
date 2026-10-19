"""Remnant geometry and stock (mlaser.services.remnants)."""
from __future__ import annotations

import pytest
from shapely.geometry import Polygon, box

from mlaser.nest import NestJob, NestParams, NestPart, nest
from mlaser.nest.apply import rect_part
from mlaser.services.remnants import Remnant, RemnantStore, polygon_of, remnant_geometry, rings_of


def test_staircase_left_gravity_is_the_rest_of_the_sheet():
    sheet = box(0, 0, 3000, 1500)
    used = box(0, 0, 1000, 1500)                    # a full-height column of parts
    (r,) = remnant_geometry(sheet, used, spacing=5, gravity="left")
    assert r.bounds == pytest.approx((1005, 0, 3000, 1500))
    assert r.area == pytest.approx(1995 * 1500)


def test_staircase_follows_the_part_steps():
    sheet = box(0, 0, 2000, 1000)
    used = box(0, 0, 1200, 400).union(box(0, 400, 600, 1000))   # an L of parts
    (r,) = remnant_geometry(sheet, used, spacing=0, gravity="left", min_width=10)
    # L-shaped remnant: the right side plus the upper-middle step
    assert r.area == pytest.approx(2000 * 1000 - 1200 * 400 - 600 * 600)
    assert r.contains(box(700, 500, 1990, 990))
    assert not r.intersects(box(10, 10, 1190, 390))


def test_staircase_ignores_gaps_between_parts_but_free_mode_keeps_them():
    sheet = box(0, 0, 1000, 1000)
    used = box(0, 0, 300, 300).union(box(700, 0, 1000, 300))    # a 400 mm gap between two parts
    stair = remnant_geometry(sheet, used, spacing=0, gravity="bottom", min_width=10, min_area=1)
    free = remnant_geometry(sheet, used, spacing=0, mode="free", min_width=10, min_area=1)
    gap = box(310, 10, 690, 290)
    assert any(p.contains(gap) for p in free)
    assert any(p.contains(gap) for p in stair)       # bottom gravity: the gap is open upwards
    left = remnant_geometry(sheet, used, spacing=0, gravity="left", min_width=10, min_area=1)
    assert not any(p.intersects(box(10, 10, 990, 290)) for p in left)


def test_slivers_and_small_pieces_are_dropped():
    sheet = box(0, 0, 1000, 1000)
    used = box(0, 0, 980, 1000)                     # leaves a 20 mm strip
    assert remnant_geometry(sheet, used, spacing=0, min_width=50) == []
    used = box(0, 0, 800, 1000)
    assert remnant_geometry(sheet, used, spacing=0, min_width=50, min_area=300000) == []
    assert len(remnant_geometry(sheet, used, spacing=0, min_width=50, min_area=1000)) == 1


def test_remnant_with_a_defect_hole_round_trips():
    sheet = Polygon([(0, 0), (1500, 0), (1500, 800), (0, 800)], [[(600, 300), (700, 300), (700, 400), (600, 400)]])
    (r,) = remnant_geometry(sheet, box(0, 0, 400, 800), spacing=0, min_width=10)
    rings = rings_of(r)
    assert len(rings) == 2 and rings[0][0] != rings[0][-1]
    back = polygon_of(rings)
    assert back.area == pytest.approx(r.area, rel=1e-6)
    assert back.bounds[:2] == (0.0, 0.0)            # normalised to its bounding box


def test_store_lifecycle_and_persistence(tmp_path):
    p = tmp_path / "remnants.json"
    st = RemnantStore(p)
    (r,) = st.capture(box(0, 0, 3000, 1500), box(0, 0, 2000, 1500), "S235", 3.0, spacing=5,
                      source="flange.chf")
    assert r.id == "R-0001" and r.status == "available"
    assert r.size == pytest.approx((995, 1500))
    st.reserve(r.id, "queue:abc")
    assert st.available() == []
    st.release(r.id)
    assert [x.id for x in st.available("s235", 3.0)] == [r.id]
    assert st.available("Inox", 0.0) == [] and st.available("", 2.0) == []
    st.mark_used(r.id, "queue:def")
    st2 = RemnantStore(p)                           # restart
    r2 = st2.get(r.id)
    assert r2.status == "used" and r2.used_by == "queue:def" and r2.used_at > 0
    assert st2.add([[[0, 0], [10, 0], [10, 10]]]).id == "R-0002"   # numbering continues
    assert st2.purge_used() == 1


def test_remnant_is_offered_to_the_nester():
    rem = Remnant(rings=[[[0, 0], [800, 0], [800, 300], [300, 300], [300, 600], [0, 600]]], id="R-0007",
                  material="S235", thickness_mm=3)
    sh = rem.nest_sheet()
    assert sh.is_remnant and sh.extra["remnant_id"] == "R-0007" and sh.outline
    job = NestJob(params=NestParams(spacing=2, margin=0, rotation_step=90, time_limit_s=10))
    job.add_part(NestPart([rect_part(0, 0, 200, 100, 1)], name="plate", quantity=6))
    job.add_sheet(sh)
    res = nest(job)
    assert sum(res.placed.values()) == 6
    poly = rem.polygon
    from mlaser.nest.apply import layout_parts
    from mlaser.nest.shapes import parts_geometry
    for q in layout_parts(job, res.layouts[0]):
        assert poly.buffer(1e-6).contains(parts_geometry([q]))   # every part inside the L
