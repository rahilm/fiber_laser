"""Optional "modern" DXF import (mlaser.io.dxf_modern, profile "modern-dxf")."""
from __future__ import annotations

import math

import pytest

ezdxf = pytest.importorskip("ezdxf")
pytest.importorskip("uharfbuzz")

from ezdxf.enums import TextEntityAlignment  # noqa: E402

from mlaser.formats.chf import dumps_chf, loads_chf  # noqa: E402
from mlaser.geom import curves as geom  # noqa: E402
from mlaser.io.dxf_import import import_dxf  # noqa: E402
from mlaser.model import Arc  # noqa: E402
from mlaser.parity_ports import get_profile, use_profile  # noqa: E402


def _save(d, tmp_path, name="t.dxf"):
    p = str(tmp_path / name)
    d.saveas(p)
    return p


def _bbox(contours):
    lo, hi = geom.segments_bbox([s for c in contours for s in c.segments])
    return lo[0], lo[1], hi[0], hi[1]


def _parts(doc, dxf_type):
    return [p for p in doc.parts if p.extra.get("dxf_type") == dxf_type]


def _modern(p):
    return import_dxf(p, placement=None, mode="modern")


def test_hatch_boundaries_become_cut_contours(tmp_path):
    d = ezdxf.new("R2010")
    m = d.modelspace()
    h = m.add_hatch()
    h.paths.add_polyline_path([(20, 0), (30, 0), (30, 10)], is_closed=True)
    ep = h.paths.add_edge_path()
    ep.add_line((40, 0), (50, 0))
    ep.add_arc((50, 5), 5, -90, 90)
    ep.add_line((50, 10), (40, 10))
    ep.add_line((40, 10), (40, 0))
    p = _save(d, tmp_path)
    assert not _parts(import_dxf(p, placement=None), "HATCH")      # faithful: dropped
    hs = _parts(_modern(p), "HATCH")
    assert len(hs) == 2 and all(c.closed for pt in hs for c in pt.contours)
    boxes = sorted(_bbox(pt.contours) for pt in hs)
    assert boxes[0] == pytest.approx((20, 0, 30, 10))
    assert boxes[1] == pytest.approx((40, 0, 55, 10))
    edge = [pt for pt in hs if _bbox(pt.contours)[0] > 35][0]
    assert any(isinstance(s, Arc) for s in edge.contours[0].segments)   # the arc edge stays an arc


def test_associative_hatch_is_not_cut_twice(tmp_path):
    d = ezdxf.new("R2010")
    m = d.modelspace()
    lw = m.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    h = m.add_hatch()
    bp = h.paths.add_polyline_path([(0, 0), (10, 0), (10, 10), (0, 10)], is_closed=True)
    bp.source_boundary_objects = [lw.dxf.handle]
    h.dxf.associative = 1
    doc = _modern(_save(d, tmp_path))
    assert len(doc.parts) == 1 and doc.parts[0].extra["dxf_type"] == "LWPOLYLINE"


def test_dimension_geometry_and_text(tmp_path):
    d = ezdxf.new("R2010", setup=True)
    m = d.modelspace()
    m.add_linear_dim(base=(0, 20), p1=(0, 0), p2=(10, 0)).render()
    p = _save(d, tmp_path)
    assert not import_dxf(p, placement=None).parts                 # faithful: DIMENSION skipped
    doc = _modern(p)
    geo = [pt for pt in doc.parts if pt.craft.layer != 10]
    txt = [pt for pt in doc.parts if pt.craft.layer == 10]
    assert geo and txt
    x0, _y0, x1, y1 = _bbox([c for pt in geo for c in pt.contours])
    assert x0 == pytest.approx(0, abs=0.5) and x1 == pytest.approx(10, abs=0.5) and y1 >= 20
    assert txt[0].extra["text"].isdigit()                         # the measurement (EZDXF style units)
    tx0, ty0, tx1, _ty1 = _bbox(txt[0].contours)
    assert tx0 < 5 < tx1 and ty0 > 19                              # above the dimension line
    assert not any(p.extra.get("layer", "").lower() == "defpoints" for p in doc.parts)


def test_attrib_text_outlines(tmp_path):
    d = ezdxf.new("R2010")
    b = d.blocks.new("TAGGED")
    b.add_circle((0, 0), 1)
    b.add_attdef("NAME", (0, -3), dxfattribs={"height": 2.0})
    ins = d.modelspace().add_blockref("TAGGED", (50, 50))
    ins.add_auto_attribs({"NAME": "P-17"})
    doc = _modern(_save(d, tmp_path))
    at = _parts(doc, "ATTRIB")
    assert len(at) == 1 and at[0].extra["text"] == "P-17" and at[0].craft.layer == 10
    _x0, y0, _x1, y1 = _bbox(at[0].contours)
    assert y1 - y0 == pytest.approx(2.0, abs=0.3)                 # cap height (plus the hyphen/digits)
    assert y0 == pytest.approx(47.0, abs=0.05)
    assert "P-17" in [t["text"] for t in doc.extra["texts"]]


def test_minsert_grid(tmp_path):
    d = ezdxf.new("R2010")
    b = d.blocks.new("C")
    b.add_circle((0, 0), 1)
    mi = d.modelspace().add_blockref("C", (200, 0))
    mi.grid(size=(2, 3), spacing=(5, 7))           # 2 rows 5 apart, 3 columns 7 apart
    p = _save(d, tmp_path)
    assert len(import_dxf(p, placement=None).parts) == 1           # faithful: one copy
    doc = _modern(p)
    centres = sorted(tuple(round(v, 6) for v in c.segments[0].center) for pt in doc.parts for c in pt.contours)
    assert centres == sorted((200.0 + 7 * j, 0.0 + 5 * i) for i in range(2) for j in range(3))


def test_mirrored_and_nonuniform_inserts(tmp_path):
    d = ezdxf.new("R2010")
    b = d.blocks.new("A")
    b.add_arc((0, 0), 2, 0, 90)                     # quarter arc in +x/+y
    b.add_line((2, 0), (5, 0))
    m = d.modelspace()
    m.add_blockref("A", (100, 0), dxfattribs={"xscale": -1, "yscale": 1})      # mirrored
    m.add_blockref("A", (0, 100), dxfattribs={"xscale": 1, "yscale": 3})       # non-uniform
    doc = _modern(_save(d, tmp_path))
    mir = [pt for pt in doc.parts if _bbox(pt.contours)[1] < 50]
    x0, y0, x1, y1 = _bbox([c for pt in mir for c in pt.contours])
    assert (x0, y0, x1, y1) == pytest.approx((95, 0, 100, 2))
    assert any(isinstance(s, Arc) for pt in mir for c in pt.contours for s in c.segments)
    nu = [pt for pt in doc.parts if _bbox(pt.contours)[1] >= 50]
    x0, y0, x1, y1 = _bbox([c for pt in nu for c in pt.contours])
    assert (x0, y0, x1, y1) == pytest.approx((0, 100, 5, 106), abs=0.02)        # radius 2 -> 2 x 6


def test_nested_insert_layer_inheritance(tmp_path):
    d = ezdxf.new("R2010")
    inner = d.blocks.new("IN")
    inner.add_circle((0, 0), 1)                     # layer "0": takes the INSERT's layer
    inner.add_line((5, 0), (6, 0), dxfattribs={"layer": "FIXED"})
    outer = d.blocks.new("OUT")
    outer.add_blockref("IN", (10, 0), dxfattribs={"rotation": 90})
    d.modelspace().add_blockref("OUT", (0, 0), dxfattribs={"layer": "CUT", "xscale": 2, "yscale": 2})
    doc = _modern(_save(d, tmp_path))
    layers = sorted(p.extra["layer"] for p in doc.parts)
    assert layers == ["CUT", "FIXED"]
    circ = [p for p in doc.parts if p.extra["layer"] == "CUT"][0].contours[0].segments[0]
    assert circ.center == pytest.approx((20, 0)) and math.dist(circ.start, circ.center) == pytest.approx(2)
    assert "FIXED" in doc.extra["layers"] and "CUT" in doc.extra["layers"]


def test_text_cap_height_alignment_and_cjk(tmp_path):
    d = ezdxf.new("R2010")
    m = d.modelspace()
    m.add_text("H", dxfattribs={"height": 5}).set_placement((10, 10))
    m.add_text("HHH", dxfattribs={"height": 5}).set_placement((50, 10), align=TextEntityAlignment.MIDDLE_CENTER)
    m.add_text("中文", dxfattribs={"height": 4}).set_placement((0, 50))
    m.add_mtext("HI\\PIH", dxfattribs={"char_height": 2.0, "insert": (0, -20)})
    doc = _modern(_save(d, tmp_path))
    by = {p.extra["text"]: p for p in doc.parts}
    x0, y0, x1, y1 = _bbox(by["H"].contours)
    assert (y0, y1) == pytest.approx((10, 15), abs=1e-6)            # cap height = text height
    x0, y0, x1, y1 = _bbox(by["HHH"].contours)
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx((50, 10), abs=1e-6)
    assert len(by["中文"].contours) >= 4
    mt = [p for p in doc.parts if p.extra["dxf_type"] == "MTEXT"]
    assert len(mt) == 1
    _x0, y0, _x1, y1 = _bbox(mt[0].contours)
    assert y1 == pytest.approx(-20, abs=0.3) and y1 - y0 > 4           # two lines below the insert
    rec = loads_chf(dumps_chf(doc))                                   # .chf round trip
    assert len(rec.parts) == len(doc.parts)


def test_default_unchanged_and_profile_selection(tmp_path):
    d = ezdxf.new("R2010")
    m = d.modelspace()
    m.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
    h = m.add_hatch()
    h.paths.add_polyline_path([(20, 0), (30, 0), (30, 10)], is_closed=True)
    p = _save(d, tmp_path)
    base = dumps_chf(import_dxf(p))
    assert dumps_chf(import_dxf(p, mode="faithful")) == base
    modern = dumps_chf(import_dxf(p, mode="modern"))
    assert modern != base
    import mlaser.parity_ports.profiles.modern_dxf  # noqa: F401 - registers the profile
    assert get_profile("modern-dxf").dxf.collect.__module__ == "mlaser.io.dxf_modern"
    with use_profile("modern-dxf"):
        assert dumps_chf(import_dxf(p)) == modern
    assert dumps_chf(import_dxf(p)) == base
    with pytest.raises(ValueError):
        import_dxf(p, mode="nope")


def test_spline_and_ellipse_flattened_within_tolerance(tmp_path):
    d = ezdxf.new("R2010")
    m = d.modelspace()
    m.add_ellipse((0, 0), major_axis=(10, 0), ratio=0.5)
    doc = import_dxf(_save(d, tmp_path), placement=None, mode="modern", flatten_tol=0.01)
    c = doc.parts[0].contours[0]
    assert c.closed
    for s in c.segments:
        mx, my = (s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2
        # chord midpoint inside the ellipse by at most the tolerance
        assert 1 - ((mx / 10) ** 2 + (my / 5) ** 2) ** 0.5 < 0.01 / 5 + 1e-9
    assert _bbox([c]) == pytest.approx((-10, -5, 10, 5), abs=1e-6)
