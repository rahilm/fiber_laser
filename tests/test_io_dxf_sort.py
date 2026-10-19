"""Import sort (FUN_100b57c0 -> 0x1003f010 mode 5, small first), INSERT-level ellipses, glyph-based
start points and TrueType text glyphs (docs/re/cam.md §1 / §8.4, docs/re/parity.md D-12 / D-16 / D-19)."""
from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from mlaser.cam import ordering  # noqa: E402
from mlaser.cam.ordering import (MsvcRand, SortElem, approx_hypot, choose_start,  # noqa: E402
                                 containers_last_faithful, contour_glyphs, local_optimal_elems,
                                 point_seg_dist)
from mlaser.formats.chf import dumps_chf, loads_chf  # noqa: E402
from mlaser.io.dxf_import import import_dxf  # noqa: E402
from mlaser.model import GLYPH_LINE, GLYPH_LWPOLY, GLYPH_SPLINE  # noqa: E402

PARITY = Path(os.environ.get("MLASER_WORKSPACE") or Path(__file__).resolve().parents[1].parent) / "parity"
SAMPLES = ["basic_r2000", "dxfTemplate", "extrusion", "gear", "inserts", "inserts2", "mix",
           "r12_polyline", "spline_ellipse", "spline_rational_acad", "visibility"]


def _save(doc, tmp_path, name="t.dxf"):
    p = tmp_path / name
    doc.saveas(p)
    return str(p)


# --------------------------------------------------------------------------- helpers of 0x1003d300
def test_msvc_rand_sequence():
    r = MsvcRand(1)
    assert [r.rand() for _ in range(3)] == [41, 18467, 6334]     # msvcrt srand(1)


def test_approx_hypot_and_point_segment_distance():
    assert approx_hypot(1e-9, -1e-9) == 0.0
    assert approx_hypot(3.0, 0.0) == 3.0
    assert approx_hypot(3.0, 4.0) == pytest.approx(5.0113, abs=1e-4)   # approximation, not exact
    assert point_seg_dist((5, 3), (0, 0), (10, 0)) == pytest.approx(3.0)
    assert point_seg_dist((-4, 3), (0, 0), (10, 0)) == pytest.approx(5.0)   # beyond a -> |a - p|
    assert point_seg_dist((14, 3), (0, 0), (10, 0)) == pytest.approx(5.0)   # before b -> |b - p|
    assert point_seg_dist((1, 1), (0, 0), (0, 1e-7)) == pytest.approx(math.hypot(1, 1 - 1e-7))


def _el(i, x, y, closed=True, end=None, size=10.0):
    return SortElem(i, (x, y, x + size, y + size), closed, (x, y), end if end else (0.0, 0.0))


def test_fewer_than_four_unchanged_and_rejection():
    es = [_el(0, 0, 0), _el(1, 100, 0), _el(2, 50, 0)]
    assert [e.index for e in local_optimal_elems(es, True)] == [0, 1, 2]
    # already short incoming order: 0.95 rule keeps it
    es = [_el(i, 20.0 * i, 0) for i in range(5)]
    assert [e.index for e in local_optimal_elems(es, False)] == [0, 1, 2, 3, 4]


def test_open_contour_nearest_neighbour_reverses():
    # three open lines in a row, the middle one drawn backwards; a far closed square first
    es = [_el(0, 500, 500),
          SortElem(1, (0, 0, 10, 1), False, (0, 0), (10, 0)),
          SortElem(2, (30, 0, 40, 1), False, (40, 0), (30, 0)),
          SortElem(3, (60, 0, 70, 1), False, (60, 0), (70, 0)),
          SortElem(4, (1000, 0, 1010, 10), True, (1000, 0))]
    out = local_optimal_elems(es, False)
    order = [e.index for e in out]
    assert order.index(1) + 1 == order.index(2) and order.index(2) + 1 == order.index(3)
    two = next(e for e in out if e.index == 2)
    assert two.reversed and two.start == (30, 0) and two.end == (40, 0)


def test_containers_last_follows_the_container_chain():
    class B:
        def __init__(self, name, bbox):
            self.name, self.bbox = name, bbox
    a = B("big", (0, 0, 100, 100))
    b = B("mid", (10, 10, 50, 50))
    c = B("small", (20, 20, 30, 30))
    d = B("other", (200, 0, 210, 10))
    out = containers_last_faithful([a, b, c, d])
    names = [x.name for x in out]
    assert names.index("small") < names.index("mid") < names.index("big")


# --------------------------------------------------------------------------- import
def test_mirrored_insert_ellipse_keeps_ccw(tmp_path):
    """DxfParse transforms the ELLIPSE entity (FUN_10008e60: parameters -> [-t1, -t0]) before
    CADModule cuts it into Bezier pieces, so a mirrored full ellipse still runs CCW from its
    major-axis end (inserts2.dxf)."""
    d = ezdxf.new("R2000")
    blk = d.blocks.new("E")
    blk.add_ellipse((0, 0), major_axis=(6, 0), ratio=0.5)
    d.modelspace().add_blockref("E", (60, 50), dxfattribs={"xscale": -1})
    doc = import_dxf(_save(d, tmp_path), placement=None)
    g = doc.parts[0].contours[0].source_glyphs()
    p0, p1 = g[0].data["ctrl"][0], g[0].data["ctrl"][1]
    assert p0 == pytest.approx((54, 50))          # major axis mirrored to (-6, 0)
    assert p1[1] < p0[1]                          # first quarter goes down: CCW


def test_import_sort_reverses_open_contour_and_sets_lead_flag(tmp_path):
    d = ezdxf.new("R2000")
    m = d.modelspace()
    m.add_line((0, 0), (100, 100))                  # a poor document order (far jumps),
    m.add_circle((290, 300), 40)                    # so the local-optimal route is accepted
    m.add_line((300, 100), (200, 0))                # drawn backwards
    m.add_circle((-160, 300), 40)
    m.add_line((400, 0), (500, 100))
    doc = import_dxf(_save(d, tmp_path), placement=None)
    rec = [p for p in doc.parts if p.extra.get("dxf_type") == "LINE"
           and p.contours[0].source_glyphs()[0].data["start"] == (300, 100)]
    assert rec and rec[0].contours[0].segments[0].start == pytest.approx((200, 0))
    assert rec[0].craft.lead_in.at_corner          # +0x1f8 toggled by the reverse
    g = rec[0].contours[0].source_glyphs()[0]
    assert g.dir == -1
    back = loads_chf(dumps_chf(doc))
    assert len(back.parts) == 5
    assert back.parts[doc.parts.index(rec[0])].extra["chf"].lead_corner
    keep = import_dxf(_save(d, tmp_path), placement=None, sort_mode=0)
    assert [p.contours[0].segments[0].start for p in keep.parts] == [(0, 0), (330, 300), (300, 100),
                                                                    (-120, 300), (400, 0)]


@pytest.mark.parametrize("name", SAMPLES)
def test_same_bytes_as_mainapp_all_samples(name):
    """D-12 closed: rebuild DXF -> .chf equals MainApp's import -> Save As on every sample except
    TEXT (D-16), skipping only the uninitialised ints (contour +0x354, spline u0, Bezier-piece u1)."""
    orig = PARITY / "out" / "app_files" / f"{name}.chf"
    dxf = PARITY / "samples" / f"{name}.dxf"
    if not (orig.exists() and dxf.exists()):
        pytest.skip("parity outputs not available")
    import sys
    sys.path.insert(0, str(PARITY / "compare"))
    try:
        from parity_lib import chf_line_diff
    finally:
        sys.path.pop(0)
    raw = orig.read_bytes()
    diffs = None
    for seed in range(0, 40):          # the original shuffles with srand(time()) (0x1003d300)
        diffs = chf_line_diff(raw, dumps_chf(import_dxf(str(dxf), sort_seed=seed)))
        if not diffs:
            break
    assert diffs == []


# --------------------------------------------------------------------------- start point on own import
def test_contour_glyphs_from_source_glyphs_and_long_side_start(tmp_path):
    """FUN_10071f80 mode 0 on the rebuild's own import: the closed LWPOLYLINE is ONE glyph, so it
    starts in the middle of its longest piece (an arc), as the original does on basic_r2000;
    a duplicate vertex (zero-length piece dropped by the importer) does not shift the split."""
    d = ezdxf.new("R2000")
    d.modelspace().add_lwpolyline([(0, 0, 0), (30, 0, 0), (30, 0, 0.6), (40, 20, 0), (0, 20, 0)],
                                  format="xyb", close=True)
    doc = import_dxf(_save(d, tmp_path), placement=None)
    c = doc.parts[0].contours[0]
    groups = contour_glyphs(c)
    assert [k for k, _s in groups] == [GLYPH_LWPOLY] and len(groups[0][1]) == len(c.segments)
    lens = [s.length() for s in c.segments]
    k = max(range(len(lens)), key=lambda i: lens[i])
    assert choose_start(c, 0, 3.0) == pytest.approx(sum(lens[:k]) + lens[k] / 2)
    # without source glyphs every segment is its own glyph: the first line > 2*lead wins
    c2 = type(c)(list(c.segments), True, {})
    assert choose_start(c2, 0, 3.0) == pytest.approx(lens[0] / 2)


# --------------------------------------------------------------------------- TEXT glyphs
def test_text_outline_glyph_kinds(tmp_path):
    from mlaser.io import dxf_import
    fam = dxf_import._text_family()
    if not fam or dxf_import._tt_font(fam, cff=True) is None:
        pytest.skip("no outline font for the text family")
    d = ezdxf.new("R2000")
    d.modelspace().add_text("HO", dxfattribs={"height": 5.0, "insert": (0, 0)})
    doc = import_dxf(_save(d, tmp_path), placement=None)
    grp = [p for p in doc.parts if p.extra.get("dxf_type") == "TEXT"][0]
    kinds = [{g.kind for g in c.source_glyphs()} for c in grp.contours]
    assert {GLYPH_LINE} in kinds                              # H: straight records only
    assert any(GLYPH_SPLINE in k for k in kinds)              # O: cubic spline records
    sp = next(g for c in grp.contours for g in c.source_glyphs() if g.kind == GLYPH_SPLINE)
    n = len(sp.data["ctrl"])
    assert (n - 1) % 3 == 0 and sp.data["u1"] == 3
    assert sp.data["knots"][:4] == [0.0] * 4 and sp.data["knots"][-4:] == [1.0] * 4
    assert len(sp.data["knots"]) == n + 4
    assert all(c.extra["close_tol"] == 0.01 for c in grp.contours)
    ys = [y for c in grp.contours for s in c.segments for y in (s.start[1], s.end[1])]
    assert max(ys) - min(ys) == pytest.approx(5.0, abs=1e-6)  # outline height = text height
    rec = loads_chf(dumps_chf(doc))
    assert any(g.kind == 7 for p in rec.parts for c in p.contours for g in c.extra["chf"].glys)
    assert ordering is not None
