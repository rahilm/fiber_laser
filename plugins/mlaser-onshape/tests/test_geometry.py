"""Face choice, flattening and the DXF that comes out (read back with ezdxf)."""
import math

import dxfcheck as DC
import models as MD
import pytest

from mlaser_onshape import geometry as G
from mlaser_onshape.dxfout import safe_name, write_dxf


def flat_of(builder, pid="P", override="", tess=True):
    body = G.find_body(builder.response(pid), pid)
    ch = G.choose_face(body, override)
    fl = G.flatten(body, ch)
    if fl.need_tessellation and tess:
        fl = G.flatten(body, ch, G.tessellation_map(builder.tessellation(pid)))
    return body, ch, fl


def dxf_of(tmp_path, builder, name="p", **kw):
    body, ch, fl = flat_of(builder, **kw)
    path = write_dxf(fl, tmp_path / f"{name}.dxf")
    doc, ents = DC.read(path)
    return fl, ents


def test_plate_with_holes_dxf(tmp_path):
    b, top, bot = MD.plate_with_holes()
    fl, ents = dxf_of(tmp_path, b)
    assert fl.face_id == top and "Top (+Z)" in fl.label             # plain symmetric plate: +Z
    x0, y0, x1, y1 = DC.bbox(ents)
    assert (round(x0, 6), round(y0, 6)) == (0, 0)
    assert x1 == pytest.approx(100.0) and y1 == pytest.approx(60.0)
    circles = [e for e in ents if e.dxftype() == "CIRCLE"]
    assert len(circles) == 3
    assert sorted(round(c.dxf.radius, 6) for c in circles) == [2.0, 4.0, 4.0]
    assert {(round(c.dxf.center.x, 6), round(c.dxf.center.y, 6)) for c in circles} == {(20, 30), (80, 30), (50, 15)}
    assert len([e for e in ents if e.dxftype() == "LINE"]) == 4
    assert len(DC.chains(ents)) == 4                                 # outline + 3 holes, all closed
    assert fl.exact_edges == 7 and fl.tessellated_edges == 0


def test_slot_arcs_are_exact_and_origin_moves_to_lower_left(tmp_path):
    b, top, bot = MD.plate_with_slot()                               # drawn at (10, 5) in the part
    fl, ents = dxf_of(tmp_path, b)
    x0, y0, x1, y1 = DC.bbox(ents)
    assert (x0, y0) == pytest.approx((0, 0), abs=1e-9) and (x1, y1) == pytest.approx((120, 50))
    arcs = [e for e in ents if e.dxftype() == "ARC"]
    assert len(arcs) == 2 and all(a.dxf.radius == pytest.approx(5.0) for a in arcs)
    assert sorted(round(a.dxf.center.x, 6) for a in arcs) == [40.0, 80.0]
    assert all(a.dxf.center.y == pytest.approx(25.0) for a in arcs)
    # each arc is the outer half of the slot (sweep 180, on the side away from the slot centre)
    for a in arcs:
        sweep = (a.dxf.end_angle - a.dxf.start_angle) % 360
        assert sweep == pytest.approx(180.0)
        mid = math.radians(a.dxf.start_angle + 90)
        mx = a.dxf.center.x + 5 * math.cos(mid)
        assert (mx > 80) if a.dxf.center.x > 60 else (mx < 40)
    assert len(DC.chains(ents)) == 2


def test_orientation_from_face_flag_when_no_oriented_direction():
    b, top, bot = MD.plate_with_slot(mirror_flags=True)               # bottom: normal + orientation=false
    body = G.find_body(b.response("P"), "P")
    f = next(f for f in body.planar if f.id == bot)
    assert f.normal == pytest.approx((0, 0, -1))
    assert G.plane_thickness(body) == pytest.approx(3.0)


def test_pocketed_plate_takes_the_pocket_side_and_mirrors(tmp_path):
    b, top, bot, floor = MD.pocketed_plate(pocket_side=-1)
    body, ch, fl = flat_of(b)
    assert ch.face.id == bot and "pockets" in ch.reason and "Bottom (-Z)" in fl.label
    assert len(fl.holes) == 2                                        # the pocket outline and the hole
    # seen from below: X stays X, Y is mirrored; the 3 mm hole at part (10, 10) is at (10, 70)
    hole = next(p for lp in fl.holes for p in lp.prims if p.kind == "circle")
    assert hole.center == pytest.approx((10.0, 70.0))
    # thickness: the faces give 6 mm, volume / largest face drifts (pocket), and disagrees
    assert G.plane_thickness(body) == pytest.approx(6.0)
    tv = G.volume_thickness(body, G.mass_volume(b.mass("P")))
    assert tv < 5.5
    b2, top2, bot2, floor2 = MD.pocketed_plate(pocket_side=+1)
    assert G.choose_face(G.find_body(b2.response("P"), "P")).face.id == top2


def test_side_face_plate_uses_y_as_x(tmp_path):
    b, top, bot = MD.side_plate()
    fl, ents = dxf_of(tmp_path, b)
    assert "Right (+X)" in fl.label
    x0, y0, x1, y1 = DC.bbox(ents)
    assert (x1 - x0, y1 - y0) == pytest.approx((90.0, 40.0))         # 90 along Y -> DXF X
    c = next(e for e in ents if e.dxftype() == "CIRCLE")
    assert (c.dxf.center.x, c.dxf.center.y) == pytest.approx((45.0, 20.0))


def test_bspline_edge_is_evaluated_exactly(tmp_path):
    b, top, bot = MD.spline_plate(r=20.0)
    fl, ents = dxf_of(tmp_path, b)
    assert fl.evaluated_edges == 1 and fl.tessellated_edges == 0
    poly = next(e for e in ents if e.dxftype() == "POLYLINE")
    pts = [(v.dxf.location.x, v.dxf.location.y) for v in poly.vertices]
    assert len(pts) > 20
    assert max(abs(math.hypot(x, y) - 20.0) for x, y in pts) < 1e-6   # a rational spline: exactly the arc
    # the chords stay within 0.02 mm of the arc
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert 20.0 - math.hypot((x0 + x1) / 2, (y0 + y1) / 2) <= 0.0201
    assert len(DC.chains(ents)) == 1


def test_ellipse_and_other_edge_use_evaluation_and_tessellation(tmp_path):
    b, top, bot = MD.ellipse_and_other_plate()
    body, ch, fl0 = flat_of(b, tess=False)
    assert len(fl0.need_tessellation) == 1 and not fl0.ok
    fl, ents = dxf_of(tmp_path, b)
    assert fl.ok and fl.tessellated_edges == 1 and fl.evaluated_edges == 1
    ell = [e for e in ents if e.dxftype() == "POLYLINE" and len(list(e.vertices)) > 10]
    pts = [(v.dxf.location.x, v.dxf.location.y) for v in ell[0].vertices]
    assert max(abs(((x - 30) / 10) ** 2 + ((y - 20) / 5) ** 2 - 1) for x, y in pts) < 1e-6
    assert len(DC.chains(ents)) == 2
    assert DC.bbox(ents)[3] == pytest.approx(45.0)


def test_no_flat_face_is_reported():
    body = G.find_body(MD.round_bar().response("P"), "P")
    with pytest.raises(G.NoFlatFace):
        G.choose_face(body)
    assert G.plane_thickness(body) is None


def test_saved_face_and_missing_saved_face():
    b, top, bot = MD.plate_with_holes()
    body = G.find_body(b.response("P"), "P")
    assert G.choose_face(body, bot).face.id == bot
    ch = G.choose_face(body, "gone")
    assert ch.face.id == top and "no longer" in ch.note
    names = G.face_list(body)
    assert names[0][1].startswith(("Top (+Z) 100 x 60", "Bottom (-Z) 100 x 60"))


def test_safe_names():
    assert safe_name("../../etc/passwd") == "etc_passwd"
    assert safe_name("CON") == "_CON"
    assert safe_name("  ") == "part"
    assert safe_name('Plate 1/8" (left)') == "Plate 1_8_ (left)"


def test_spline_rejects_bad_data():
    with pytest.raises(G.GeometryError):
        G.BSpline({"degree": 2, "knots": [0, 1], "controlPoints": [0, 0, 0]})
    with pytest.raises(G.GeometryError):
        G.BSpline({"degree": 1, "knots": [0, 0, 1, 1], "controlPoints": [0, 0, 0, 1, 1, 1], "isRational": True})
