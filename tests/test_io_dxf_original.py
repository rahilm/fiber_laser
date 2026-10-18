"""DXF import as the original builds it (DxfParse + CADModule; docs/cam.md §1, docs/parity.md D-07..D-16).

Goldens marked "orig" come from .chf files the original MainApp wrote for the parity samples
(parity/out/app_files/*.chf) or from the decompiled constants they reflect.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from mlaser.formats.chf import dumps_chf, loads_chf  # noqa: E402
from mlaser.io import geom  # noqa: E402
from mlaser.io.dxf_import import import_dxf  # noqa: E402
from mlaser.model import (GLYPH_ARC, GLYPH_CIRCLE, GLYPH_LINE, GLYPH_LWPOLY, GLYPH_POINT,  # noqa: E402
                          GLYPH_SPLINE, Line)

PARITY = Path("/home/dev/scratch/mlaser/parity")


def _save(doc, tmp_path, name="t.dxf"):
    p = tmp_path / name
    doc.saveas(p)
    return str(p)


def _glyphs(doc):
    return [[(g.dir, g.kind) for g in (p.contours[0].source_glyphs() or [])] for p in doc.parts]


def test_placement_layer_and_glyph_kinds(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    for a, b in [((0, 0), (100, 0)), ((100, 0), (100, 50)), ((100, 50), (0, 50)), ((0, 50), (0, 0))]:
        m.add_line(a, b)
    m.add_circle((20, 25), 8)
    m.add_lwpolyline([(120, 0, 0), (150, 0, 0.5), (150, 30, 0), (120, 30, -0.3)], format="xyb", close=True)
    doc = import_dxf(_save(d, tmp_path))
    x0, y0, _x1, _y1 = doc.bbox()
    assert (x0, y0) == pytest.approx((20.0, 0.0))                   # orig: bbox lower-left at (20, 0)
    assert all(p.craft.layer == 0 for p in doc.parts)               # orig: layer 0
    kinds = _glyphs(doc)
    assert kinds == [[(1, GLYPH_CIRCLE)], [(1, GLYPH_LINE)] * 4, [(1, GLYPH_LWPOLY)]]   # inner first
    circle = doc.parts[0].contours[0]
    assert circle.extra["depth"] == 2 and doc.parts[1].contours[0].extra["depth"] == 1
    raw = doc.parts[2].contours[0].source_glyphs()[0].data
    assert raw["flag"] == 1 and [b for _p, b in raw["verts"]] == [0.0, 0.5, 0.0, -0.3]
    # .chf: one lwpoly glyph, circle glyph, closing tolerance 0.1, depth in +0x88
    back = loads_chf(dumps_chf(doc))
    recs = [p.extra["chf"] for p in back.parts]
    assert [[g.kind for g in r.glys] for r in recs] == [[4], [2, 2, 2, 2], [6]]
    assert [r.tol for r in recs] == [0.1, 0.1, 0.1] and [r.flag88 for r in recs] == [2, 1, 1]
    assert recs[2].bbox_max[0] == pytest.approx(177.5)             # exact arc extreme (orig 177.5)
    assert recs[2].length == pytest.approx(126.542361, abs=1e-6)  # orig 126.542361
    keep = import_dxf(_save(d, tmp_path), placement=None)
    assert keep.bbox()[:2] == pytest.approx((0.0, 0.0))


def test_mirrored_circle_and_arc(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    m.add_circle((30, 10), 4, dxfattribs={"extrusion": (0, 0, -1)})
    m.add_arc((10, 10), 5, 0, 90, dxfattribs={"extrusion": (0, 0, -1)})
    doc = import_dxf(_save(d, tmp_path), placement=None)
    c = doc.parts[0].contours[0]
    g = c.source_glyphs()[0]
    assert g.kind == GLYPH_CIRCLE and g.dir == 1 and g.data["center"] == pytest.approx((-30, 10))
    assert c.segments[0].ccw and c.segments[0].start == pytest.approx((-26, 10))   # starts at angle 0
    a = doc.parts[1].contours[0].source_glyphs()[0].data
    assert (a["a0"], a["a1"]) == pytest.approx((math.pi / 2, math.pi))       # orig 1.570796 / 3.141593


def test_tiny_arc_becomes_line(tmp_path):
    d = ezdxf.new("R2000")
    d.modelspace().add_arc((0, 0), 50, 0, 0.5)          # 0.44 mm, sagitta 5e-4 (FUN_100032e0)
    d.modelspace().add_arc((100, 0), 50, 0, 40)
    doc = import_dxf(_save(d, tmp_path), placement=None)
    assert _glyphs(doc) == [[(1, GLYPH_LINE)], [(1, GLYPH_ARC)]]


def test_splines_and_ellipses(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    m.add_open_spline([(0, 0), (10, 20), (30, -10), (40, 15), (60, 0)], degree=3)
    m.add_rational_spline([(70, 0), (70, 20), (90, 20)], weights=[1, math.sqrt(0.5), 1], degree=2)
    m.add_ellipse((20, 60), major_axis=(15, 0), ratio=0.5)
    m.add_ellipse((60, 60), major_axis=(0, 10), ratio=0.4, start_param=0.3, end_param=2.5)
    doc = import_dxf(_save(d, tmp_path), placement=None, sort_mode=0)     # document order
    sp, rat, full, part = (p.contours[0] for p in doc.parts)
    g = sp.source_glyphs()[0]
    assert g.kind == GLYPH_SPLINE and g.data["knots"] == [0, 0, 0, 0, 1, 2, 2, 2, 2]
    # weights written before the control points (ezdxf order) are dropped (D-08), then the
    # quadratic becomes a cubic with k = 1.882 / 3 (orig ctrl (90, 12.546667) after +20)
    q = rat.source_glyphs()[0].data
    assert q["ctrl"][1] == pytest.approx((70, 12.546667), abs=1e-6)
    assert q["knots"] == [0, 0, 0, 0, 1, 1, 1, 1]
    # ellipse: 4 Bezier quarters, kappa = 4/3 tan(pi/8), knots end at 2 (orig)
    gs = full.source_glyphs()
    assert len(gs) == 4 and full.closed
    assert gs[0].data["ctrl"][1] == pytest.approx((35, 64.142136), abs=1e-6)
    assert gs[0].data["knots"] == [0, 0, 0, 0, 2, 2, 2, 2] and gs[0].data["u1"] == 4
    ps = part.source_glyphs()
    assert len(ps) == 2 and ps[0].data["knots"][-1] == 0.5
    assert ps[1].data["ctrl"][1] == pytest.approx((56, 56.84571), abs=1e-5)   # orig (76, 56.84571)


def test_spline_weights_in_autocad_order(tmp_path):
    src = PARITY / "samples" / "spline_rational_acad.dxf"
    if not src.exists():
        pytest.skip("parity samples not generated")
    doc = import_dxf(str(src))
    g = doc.parts[0].contours[0].source_glyphs()[0]
    assert g.data["ctrl"][1] == pytest.approx((20, 11.024501), abs=1e-6)       # orig, weight honoured


def test_insert_nonuniform_keeps_arcs_circular(tmp_path):
    d = ezdxf.new("R2000")
    b = d.blocks.new("PART")
    b.add_line((0, 0), (10, 0)); b.add_line((10, 0), (10, 5))
    b.add_arc((5, 5), 5, 0, 180); b.add_line((0, 5), (0, 0))
    m = d.modelspace()
    m.add_blockref("PART", (0, 0))
    m.add_blockref("PART", (30, 10), dxfattribs={"xscale": 2, "yscale": 0.5, "rotation": 30})
    m.add_blockref("PART", (80, 0), dxfattribs={"xscale": -1})
    doc = import_dxf(_save(d, tmp_path), placement=None)
    kinds = _glyphs(doc)
    # orig inserts.chf: closed [L L A L]; open [L L L] (L3 prepended by the merge pass); lone arc;
    # mirrored copy with the arc traversed backwards
    assert kinds[0] == [(1, 2), (1, 2), (1, 3), (1, 2)]
    assert kinds[1] == [(1, 2), (1, 2), (1, 2)] and not doc.parts[1].contours[0].closed
    assert kinds[2] == [(1, 3)]
    assert kinds[3] == [(1, 2), (1, 2), (-1, 3), (1, 2)] and doc.parts[3].contours[0].closed
    arc = doc.parts[2].contours[0].source_glyphs()[0].data
    assert arc["radius"] == pytest.approx(5.0)                                 # not scaled (D-07)
    assert arc["center"] == pytest.approx((37.410254, 17.165064), abs=1e-6)   # orig - 22.196699
    assert (arc["a0"], arc["a1"]) == pytest.approx((math.pi / 6, 7 * math.pi / 6))


def test_paper_space_kept_invisible_skipped(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    m.add_line((0, 0), (10, 0))
    m.add_line((0, 5), (10, 5), dxfattribs={"invisible": 1})
    d.layout("Layout1").add_line((0, 10), (10, 10))
    doc = import_dxf(_save(d, tmp_path), placement=None)
    starts = sorted(p.contours[0].segments[0].start for p in doc.parts)
    assert starts == [(0.0, 0.0), (0.0, 10.0)]                                # D-09


def test_point_graph_and_text_group(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    m.add_text("HELLO", dxfattribs={"insert": (0, 0), "height": 5})
    m.add_point((10, 10))
    m.add_line((0, 20), (30, 20))
    doc = import_dxf(_save(d, tmp_path), placement=None)
    pt = doc.parts[0]
    assert pt.extra["dxf_type"] == "POINT" and _glyphs(doc)[0] == [(1, GLYPH_POINT)]
    assert pt.contours[0].segments == [Line((10.0, 10.0), (10.0, 10.0))]
    assert doc.extra["texts"][0]["text"] == "HELLO" and doc.extra["points"] == [(10.0, 10.0)]
    text = [p for p in doc.parts if p.extra.get("dxf_type") == "TEXT"]
    if not text:
        pytest.skip("no Qt font engine")
    t = text[0]
    assert t.craft.layer == 10 and len(t.contours) >= 5 and all(c.closed for c in t.contours)
    ys = [y for c in t.contours for s in c.segments for y in (s.start[1], s.end[1])]
    assert max(ys) - min(ys) == pytest.approx(5.0)                    # outline height = text height
    chf = loads_chf(dumps_chf(doc))
    assert chf.parts[-1].extra["chf_type"] == 9 and chf.parts[-1].extra["chf"].layer == 10
    assert [p.extra["chf_type"] for p in chf.parts[:2]] == [8, 8]


def test_flattening_matches_original_lengths():
    # (ctrl, knots, length the original stored in its .chf)
    cases = [
        ([(20, 0), (30, 20), (50, -10), (60, 15), (80, 0)], [0, 0, 0, 0, 1, 2, 2, 2, 2], 68.6944),
        ([(90, 0), (90, 12.546667), (97.453333, 20), (110, 20)], [0, 0, 0, 0, 1, 1, 1, 1], 32.0927),
        ([(820.535818, 200.26007), (876.747316, 88.551084), (978.745288, -114.149258),
          (1065.965731, 235.764232), (1199.539848, 164.543849), (1260.045683, 132.282744)],
         [0, 0, 0, 0, 251.176365, 455.769377, 625.187226, 625.187226, 625.187226, 625.187226], 650.5932),
    ]
    for ctrl, knots, L in cases:
        pts = geom.flatten_bspline(ctrl, knots)
        assert sum(math.dist(a, b) for a, b in zip(pts, pts[1:])) == pytest.approx(L, abs=2e-4)
        exact = geom.bspline_length(ctrl, knots)
        dev = max(_dist_to_curve(p, ctrl, knots) for p in _mids(pts))
        assert dev < 0.02 and abs(exact - L) < 0.01


def _mids(pts):
    return [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(pts, pts[1:])]


def _dist_to_curve(p, ctrl, knots, n=400):
    lo, hi = knots[3], knots[-4]
    f = lambda u: math.dist(p, geom.bspline_point(ctrl, knots, min(u, hi * (1 - 1e-12))))  # noqa: E731
    us = [lo + (hi - lo) * i / n for i in range(n + 1)]
    i = min(range(n + 1), key=lambda k: f(us[k]))
    a, b = us[max(i - 1, 0)], us[min(i + 1, n)]
    for _ in range(60):                                   # golden-section refinement
        m1, m2 = a + (b - a) * 0.382, a + (b - a) * 0.618
        if f(m1) < f(m2):
            b = m2
        else:
            a = m1
    return f((a + b) / 2)


def test_source_glyphs_invalidated_by_edits(tmp_path):
    d = ezdxf.new("R2000")
    d.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10)])
    doc = import_dxf(_save(d, tmp_path))
    c = doc.parts[0].contours[0]
    assert c.source_glyphs()
    c.segments = [type(s)(*(tuple(v) for v in vars(s).values())) for s in c.segments]   # same geometry
    assert c.source_glyphs()
    c.segments[0] = Line(c.segments[0].start, (25.0, 1.0))
    assert c.source_glyphs() is None
    rec = loads_chf(dumps_chf(doc)).parts[0].extra["chf"]
    assert [g.kind for g in rec.glys] == [2, 2]            # rebuilt from the edited segments


@pytest.mark.parametrize("name", ["basic_r2000", "inserts", "gear", "r12_polyline", "visibility",
                                  "dxfTemplate", "spline_rational_acad"])
def test_same_file_as_mainapp(name):
    """Byte comparison with the .chf the original MainApp wrote (minus the uninitialised +0x354 int)."""
    orig = PARITY / "out" / "app_files" / f"{name}.chf"
    dxf = PARITY / "samples" / f"{name}.dxf"
    if not (orig.exists() and dxf.exists()):
        pytest.skip("parity outputs not available")
    a = orig.read_bytes().decode("gbk").split("\r\n")
    b = dumps_chf(import_dxf(str(dxf))).decode("gbk").split("\r\n")
    assert len(a) == len(b)
    # +0x354 (uninitialised in the original) is the int two lines before "<Vision Feature>";
    # a spline glyph's first int (u0, CEditableSpline +0xc4, discarded by the loader) is garbage too
    garbage = {i - 2 for i, s in enumerate(a) if s == "<Vision Feature>"}
    garbage |= {i + 1 for i in range(2, len(a) - 1) if a[i] == "7" and a[i - 2].startswith("####Gly")}
    diff = [(i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y and i not in garbage]
    assert diff == []


def test_minsert_grid_ignored_and_mirrored_ellipse(tmp_path):
    d = ezdxf.new("R2000")
    b = d.blocks.new("P")
    b.add_line((0, 0), (10, 0))
    m = d.modelspace()
    m.add_blockref("P", (0, 0)).grid(size=(2, 2), spacing=(25, 30))      # DxfParse: one copy (inserts2.dxf)
    m.add_ellipse((80, 60), major_axis=(8, 0), ratio=0.6, start_param=0.2, end_param=2.0,
                  dxfattribs={"extrusion": (0, 0, -1)})
    sp = m.add_spline(degree=3)
    sp.control_points = [(140, 0), (143, 8), (147, 8), (150, 0), (153, -8), (157, -8), (160, 0)]
    sp.knots = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 2]
    doc = import_dxf(_save(d, tmp_path), placement=None)
    assert len(doc.parts) == 3
    # orig mix.chf (minus the (20, 10) placement): one Bezier piece over [-2.0, -0.2], because the
    # pi/2 cut is taken with truncation toward zero
    e = doc.parts[1].contours[0].source_glyphs()
    assert len(e) == 1
    got = [v for p in e[0].data["ctrl"] for v in p]
    assert got == pytest.approx([76.670825, 55.635372, 81.35606, 54.348833,
                                 86.816872, 56.01646, 87.840533, 59.046387], abs=1e-6)
    s = doc.parts[2].contours[0].source_glyphs()                        # a triple knot does not split
    assert len(s) == 1 and len(s[0].data["ctrl"]) == 7


def test_per_contour_craft_written_per_group_element():
    from mlaser.model import Contour, Craft, Document, LeadParams, Part
    tri = lambda o: Contour([Line((o, 0), (o + 10, 0)), Line((o + 10, 0), (o, 10)), Line((o, 10), (o, 0))], True)  # noqa: E731
    p = Part([tri(0), tri(20)], Craft(layer=2))
    p.contours[1].extra["craft"] = Craft(layer=3, lead_in=LeadParams(kind=1, length=3, radius=2))
    raw = dumps_chf(Document([p]))
    back = loads_chf(raw)
    elems = back.parts[0].extra["chf"].elems
    assert [(e.layer, e.lead_in.kind, e.lead_in.length) for e in elems] == [(2, 0, 2.0), (3, 1, 3.0)]
    assert back.parts[0].contours[1].extra["craft"].lead_in.length == 3.0
    assert "craft" not in back.parts[0].contours[0].extra
    assert dumps_chf(back) == raw                                   # unchanged: byte-identical
    back.parts[0].contours[1].extra["craft"].lead_in.length = 5.0   # in-place edit is detected
    back.parts[0].craft.kerf_width = 0.2
    elems = loads_chf(dumps_chf(back)).parts[0].extra["chf"].elems
    assert [(e.kerf_width, e.lead_in.length) for e in elems] == [(0.2, 2.0), (0.0, 5.0)]
