"""DXF repair (mlaser.services.repair): detection and fixes."""
from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest

from mlaser.io.dxf_import import import_dxf
from mlaser.model import Contour, Craft, Document, Line, Part
from mlaser.services.repair import RepairOptions, analyze, open_contours_summary, preview, repair

DXF = Path(__file__).parent / "data" / "dxf"


def poly(pts, closed=True, layer=0, name=""):
    n = len(pts)
    segs = [Line(pts[i], pts[(i + 1) % n]) for i in range(n if closed else n - 1)]
    return Part([Contour(segs, closed)], Craft(layer=layer), name=name, extra={"layer": "0"})


def square_with_gap(x=0.0, gap=0.2):
    return poly([(x, 0), (x + 50, 0), (x + 50, 50), (x, 50), (x, gap)], closed=False)


def test_clean_drawing_has_no_findings():
    doc = import_dxf(str(DXF / "gear.dxf"))
    rep = analyze(doc)
    assert rep.ok and rep.summary() == "no problems found"


def test_open_contour_gap_reported_and_closed():
    doc = Document(parts=[square_with_gap(gap=0.2)])
    rep = analyze(doc)
    (f,) = rep.of("open_contour")
    assert math.isclose(f.value, 0.2, abs_tol=1e-9) and f.fixable and "gap 0.200 mm" in f.message
    done = repair(doc)
    assert done["closed"] == 1 and done["open_left"] == 0
    assert doc.parts[0].contours[0].closed
    assert analyze(doc).ok


def test_gap_larger_than_the_tolerance_is_left_open():
    doc = Document(parts=[square_with_gap(gap=3.0)])
    (f,) = analyze(doc).of("open_contour")
    assert not f.fixable
    done = repair(doc)
    assert done["closed"] == 0 and done["open_left"] == 1
    assert repair(doc, RepairOptions(close_gap=5.0))["closed"] == 1


def test_open_pieces_are_joined_with_the_tolerance():
    # a rectangle drawn as four separate lines with 0.05 mm gaps (the customer's exploded DXF)
    pts = [(0, 0), (40, 0), (40, 20), (0, 20)]
    parts = []
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        parts.append(Part([Contour([Line((a[0] + 0.05, a[1]), b)], False)], Craft(layer=1), extra={"layer": "0"}))
    doc = Document(parts=parts)
    assert len(analyze(doc).of("open_contour")) == 4
    done = repair(doc)
    assert done["joined"] == 3 and done["closed"] == 1 and done["open_left"] == 0
    assert len(doc.parts) == 1 and doc.parts[0].craft.layer == 1


def test_different_layers_are_not_joined():
    a = Part([Contour([Line((0, 0), (10, 0))], False)], Craft(layer=1))
    b = Part([Contour([Line((10, 0), (10, 10))], False)], Craft(layer=2))
    doc = Document(parts=[a, b])
    repair(doc)
    assert sum(len(p.contours) for p in doc.parts) == 2


def test_duplicates_found_and_removed():
    sq = poly([(0, 0), (10, 0), (10, 10), (0, 10)])
    doc = Document(parts=[sq, copy.deepcopy(sq), poly([(50, 0), (60, 0), (60, 10), (50, 10)])])
    rep = analyze(doc)
    (f,) = rep.of("duplicate")
    assert f.part == 1 and "cut twice" in f.message
    done = repair(doc)
    assert done["duplicates"] == 1 and len(doc.parts) == 2


def test_overlapping_open_lines_merged():
    doc = Document(parts=[poly([(0, 0), (10, 0)], closed=False), poly([(5, 0), (20, 0)], closed=False)])
    (f,) = analyze(doc).of("overlap")
    assert math.isclose(f.value, 5.0, abs_tol=1e-6)
    done = repair(doc, RepairOptions(fix_open=False))
    assert done["overlaps"] == 1
    total = sum(c.length() for p in doc.parts for c in p.contours)
    assert math.isclose(total, 20.0, abs_tol=1e-6)


def test_zero_length_and_tiny_segments():
    pts = [(0, 0), (20, 0), (20, 0.004), (20, 10), (0, 10)]
    p = poly(pts)
    p.contours[0].segments.insert(1, Line((20, 0), (20, 0)))          # zero length
    z = Part([Contour([Line((5, 5), (5, 5))], False)], Craft(layer=0))  # zero-length contour
    doc = Document(parts=[p, z])
    rep = analyze(doc)
    c = rep.counts()
    assert c["zero_length"] == 2 and c["tiny_segment"] == 1
    done = repair(doc)
    assert done["zero_length"] == 2 and done["tiny_segments"] == 1
    (part,) = doc.parts
    segs = part.contours[0].segments
    assert len(segs) == 4 and all(s.length() > 0.01 for s in segs)
    for a, b in zip(segs, segs[1:] + segs[:1]):
        assert math.dist(a.end, b.start) < 1e-9                       # still connected
    assert analyze(doc).ok


def test_tiny_objects_removed():
    doc = Document(parts=[poly([(0, 0), (10, 0), (10, 10)]), poly([(50, 50), (50.05, 50), (50.05, 50.05)])])
    (f,) = analyze(doc).of("tiny_object")
    assert f.part == 1
    assert repair(doc)["tiny_objects"] == 1 and len(doc.parts) == 1


def test_self_intersection_reported_not_fixed():
    bow = poly([(0, 0), (10, 10), (10, 0), (0, 10)])
    doc = Document(parts=[bow])
    (f,) = analyze(doc).of("self_intersection")
    assert not f.fixable and f.where == pytest.approx((5.0, 5.0))
    before = copy.deepcopy(doc)
    repair(doc)
    assert doc.parts[0].contours[0].segments == before.parts[0].contours[0].segments


def test_text_not_converted_is_reported_and_converted(tmp_path):
    doc = import_dxf(str(DXF / "text_points.dxf"), text_outlines=False)
    rep = analyze(doc)
    (f,) = rep.of("text")
    assert "HELLO" in f.message and f.fixable
    done = repair(doc, RepairOptions(fix_open=False))
    assert done["text"] == 1
    ref = import_dxf(str(DXF / "text_points.dxf"))
    want = next(p for p in ref.parts if p.extra.get("text"))
    got = next(p for p in doc.parts if p.extra.get("text"))
    assert len(got.contours) == len(want.contours)
    # placed as the import places it, relative to the other geometry
    def rel(d, part):
        ln = next(p for p in d.parts if p.extra.get("dxf_type") == "LINE")
        x, y = ln.contours[0].segments[0].start
        b = part.bbox()
        return (b[0] - x, b[1] - y, b[2] - x, b[3] - y)
    assert rel(doc, got) == pytest.approx(rel(ref, want), abs=1e-6)
    assert not analyze(doc).of("text")


def test_preview_does_not_change_the_document_and_shows_closing_lines():
    doc = Document(parts=[square_with_gap(gap=0.3)])
    before = copy.deepcopy(doc)
    done, lines = preview(doc)
    assert doc == before and done["closed"] == 1
    assert len(lines) == 1
    assert math.dist(lines[0][0], lines[0][1]) == pytest.approx(0.3)


def test_points_are_not_problems():
    doc = import_dxf(str(DXF / "text_points.dxf"))
    assert not analyze(doc).of("zero_length")


def test_open_contours_summary():
    doc = Document(parts=[square_with_gap(0), square_with_gap(100), poly([(0, 0), (1, 0), (1, 1)])])
    assert open_contours_summary(doc) == (2, 2)


@pytest.mark.parametrize("name", ["basic_r2000", "mix", "inserts2", "spline_ellipse", "extrusion"])
def test_samples_repair_is_idempotent(name):
    doc = import_dxf(str(DXF / f"{name}.dxf"))
    repair(doc)
    snap = copy.deepcopy(doc)
    again = repair(doc)
    assert not any(v for k, v in again.items() if k != "open_left")
    assert doc == snap
