"""Part recognition (mlaser.services.parts): containment, layers, blocks, identical parts,
merge / split, nest parts; on the parity sample DXFs and on synthetic drawings."""
from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest

from mlaser.io.dxf_import import import_dxf
from mlaser.model import Arc, Contour, Craft, Document, Line, Part
from mlaser.nest.model import Placement
from mlaser.nest.shapes import transform_part
from mlaser.services.parts import RecognitionRules, recognize, same_shape

DXF = Path(__file__).parent / "data" / "dxf"


def rect(x0, y0, x1, y1, layer="0", closed=True):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    segs = [Line(pts[i], pts[(i + 1) % 4]) for i in range(4 if closed else 3)]
    return Part([Contour(segs, closed)], Craft(layer=0), extra={"layer": layer})


def circle(cx, cy, r, layer="0"):
    p = (cx + r, cy)
    return Part([Contour([Arc(p, p, (cx, cy), True)], True)], Craft(layer=0), extra={"layer": layer})


def line(a, b, layer="0"):
    return Part([Contour([Line(a, b)], False)], Craft(layer=0), extra={"layer": layer})


def l_shape(dx=0.0, dy=0.0, mirror=False):
    pts = [(0, 0), (40, 0), (40, 10), (10, 10), (10, 30), (0, 30)]
    if mirror:
        pts = [(-x, y) for x, y in reversed(pts)]
    pts = [(x + dx, y + dy) for x, y in pts]
    return Part([Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)], Craft(layer=0))


# ---------------------------------------------------------------- parity sample DXFs
@pytest.mark.parametrize("name, parts, loose", [
    ("gear", 1, 0),            # the bore stays in the gear (review 2 §2.1 item 2)
    ("r12_polyline", 3, 0),
    ("inserts", 4, 2),
    ("inserts2", 7, 2),
    ("basic_r2000", 2, 1),
])
def test_parity_samples(name, parts, loose):
    doc = import_dxf(str(DXF / f"{name}.dxf"))
    before = copy.deepcopy(doc)
    ps = recognize(doc)
    assert ps.count == parts
    assert len(ps.loose) == loose
    assert doc == before                               # recognition never changes the drawing
    members = sorted(i for g in ps.groups for i in g.all_members()) + ps.loose + ps.ignored
    assert len(members) == len(set(members))          # every document part at most once


def test_gear_is_one_part_with_its_bore():
    doc = import_dxf(str(DXF / "gear.dxf"))
    assert len(doc.parts) == 2                         # the import itself: one part per contour
    ps = recognize(doc)
    (g,) = ps.groups
    assert g.name == "gear" and g.quantity == 1 and g.holes == 1
    assert sorted(g.members) == [0, 1]
    (np_,) = ps.nest_parts(doc)
    assert np_.name == "gear" and np_.quantity == 1 and len(np_.parts) == 2


def test_inserts_identical_parts_get_a_quantity():
    doc = import_dxf(str(DXF / "inserts.dxf"))
    ps = recognize(doc)
    assert sorted(g.quantity for g in ps.groups) == [2, 2]
    assert ps.summary().startswith("4 parts (2 different)")
    nps = ps.nest_parts(doc, multiplier=5)
    assert sorted(p.quantity for p in nps) == [10, 10]


# ---------------------------------------------------------------- synthetic
def test_holes_islands_and_features_by_containment():
    doc = Document(parts=[rect(0, 0, 100, 100), rect(10, 10, 50, 50), rect(20, 20, 30, 30),
                          line((60, 60), (90, 90)), rect(200, 0, 220, 20), line((300, 0), (310, 0))])
    ps = recognize(doc)
    assert [sorted(g.members) for g in ps.groups] == [[0, 1, 2, 3], [4]]
    assert ps.loose == [5]                              # an open line in no part is not a part
    pip = recognize(doc, RecognitionRules(islands="parts"))
    assert [sorted(g.members) for g in pip.groups] == [[0, 1, 3], [2], [4]]   # part in part


def test_feature_and_ignored_layers():
    doc = Document(parts=[rect(0, 0, 100, 100), rect(10, 10, 20, 20, layer="ETCH"),
                          rect(-500, -500, 500, 500, layer="BORDER"), rect(30, 30, 40, 40, layer="MARK_1"),
                          rect(200, 200, 210, 210, layer="ETCH")])
    ps = recognize(doc)
    assert ps.ignored == [2]                            # the title-block border is not a part
    assert [sorted(g.members) for g in ps.groups] == [[0, 1, 3]]
    assert ps.loose == [4]                              # an etch outside every part


def test_layer_mode_one_part_per_layer():
    doc = Document(parts=[rect(0, 0, 10, 10, "P1"), rect(50, 0, 60, 10, "P1"), rect(100, 0, 110, 10, "P2"),
                          rect(2, 2, 4, 4, "ETCH")])
    ps = recognize(doc, RecognitionRules(mode="layer", merge_identical=False))
    names = {g.name: sorted(g.members) for g in ps.groups}
    assert names == {"P1": [0, 1, 3], "P2": [2]}


def test_file_and_none_modes():
    doc = Document(parts=[rect(0, 0, 10, 10), rect(2, 2, 4, 4), rect(50, 0, 60, 10)])
    one = recognize(doc, RecognitionRules(mode="file"), base="plate")
    assert [(g.name, g.members) for g in one.groups] == [("plate", [0, 1, 2])]
    each = recognize(doc, RecognitionRules(mode="none", merge_identical=False))
    assert len(each.groups) == 3


def test_rotated_copies_merge_mirrored_ones_do_not():
    doc = Document()
    for k in range(4):
        doc.parts.append(transform_part(l_shape(), Placement(-1, 100 * k, 0, angle=37 * k)))
    doc.parts.append(l_shape(500, 0, mirror=True))
    ps = recognize(doc)
    assert sorted(g.quantity for g in ps.groups) == [1, 4]
    assert not same_shape(doc, [0], [4])
    assert same_shape(doc, [0], [3])


def test_gear_copies_rotated_merge():
    gear = import_dxf(str(DXF / "gear.dxf"))
    doc = Document()
    for k in range(6):
        doc.parts += [transform_part(p, Placement(-1, 150 * k, 0, angle=13 * k)) for p in gear.parts]
    ps = recognize(doc)
    (g,) = ps.groups
    assert g.quantity == 6 and g.holes == 1


def test_parts_that_differ_in_a_hole_stay_apart():
    a = Document(parts=[rect(0, 0, 50, 50), circle(25, 25, 5), rect(100, 0, 150, 50), circle(125, 25, 6)])
    ps = recognize(a)
    assert sorted(g.quantity for g in ps.groups) == [1, 1]


def test_merge_and_split():
    doc = Document(parts=[rect(0, 0, 10, 10), rect(50, 0, 60, 10), rect(100, 0, 130, 10)])
    ps = recognize(doc)
    assert sorted(g.quantity for g in ps.groups) == [1, 2]
    k = next(i for i, g in enumerate(ps.groups) if g.quantity == 2)
    new = ps.split(k)
    assert [g.quantity for g in new] == [1, 1] and len(ps.groups) == 3
    m = ps.merge([0, 1, 2])
    assert m.quantity == 1 and sorted(m.members) == [0, 1, 2] and len(ps.groups) == 1
    (single,) = ps.split(0)[:1]
    assert len(ps.groups) == 3 and single.quantity == 1


def test_nest_parts_carry_order_refs_and_are_copies():
    doc = Document(parts=[rect(0, 0, 10, 10)])
    ps = recognize(doc, base="tab")
    (np_,) = ps.nest_parts(doc, multiplier=3, extra={"order": {"customer": "A", "order": "1"}})
    assert np_.quantity == 3 and np_.extra["order"]["customer"] == "A"
    np_.parts[0].contours.clear()
    assert doc.parts[0].contours                        # the drawing keeps its geometry


def test_block_mode(tmp_path):
    import ezdxf
    d = ezdxf.new()
    blk = d.blocks.new("TAB")
    blk.add_lwpolyline([(0, 0), (30, 0), (30, 20), (0, 20)], close=True)
    blk.add_circle((15, 10), 4)
    other = d.blocks.new("WASHER")
    other.add_circle((0, 0), 10)
    other.add_circle((0, 0), 4)
    msp = d.modelspace()
    for k in range(3):
        msp.add_blockref("TAB", (100 * k, 0), dxfattribs={"rotation": 30 * k})
    msp.add_blockref("WASHER", (0, 200))
    msp.add_lwpolyline([(500, 500), (520, 500), (520, 520)], close=True)      # outside every block
    p = tmp_path / "blocks.dxf"
    d.saveas(p)
    doc = import_dxf(str(p))
    ps = recognize(doc, RecognitionRules(mode="block"))
    by = {g.name: g.quantity for g in ps.groups}
    assert by["TAB"] == 3 and by["WASHER"] == 1
    assert ps.count == 5                                # + the triangle outside the blocks
    nested = recognize(doc)
    assert nested.count == 5                            # containment finds the same parts here


def test_rules_from_dict_and_bad_mode():
    r = RecognitionRules.from_dict({"mode": "layer", "feature_layers": "ETCH, SCRIBE", "x": 1})
    assert r.mode == "layer" and r.feature_layers == ("ETCH", "SCRIBE")
    with pytest.raises(ValueError):
        RecognitionRules(mode="magic")
    assert RecognitionRules.from_dict(r.to_dict()) == r


def test_open_only_drawing_has_no_parts():
    doc = import_dxf(str(DXF / "visibility.dxf"))
    ps = recognize(doc)
    assert ps.count == 0 and len(ps.loose) == 2
    assert math.isclose(ps.count, 0)
