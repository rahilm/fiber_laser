"""mlaser.cad geometry: primitives, trim/extend/split/merge/explode, arrange, snap, QR, text."""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mlaser.cad import arrange as AR
from mlaser.cad import edit as ED
from mlaser.cad import primitives as P
from mlaser.cad.intersect import contour_crossings, intersections
from mlaser.cad.snap import snap
from mlaser.model import Arc, Contour, Document, Line, Part


def approx(a, b, eps=1e-6):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def area(c: Contour) -> float:
    pts = list(c.points(0.01))
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                     for i in range(len(pts)))


# ------------------------------------------------------------------ primitives
def test_rect_vertex_order_follows_clicks():
    c = P.rect((10, 20), (0, 0))
    assert c.closed and len(c.segments) == 4
    assert [s.start for s in c.segments] == [(10, 20), (0, 20), (0, 0), (10, 0)]


def test_round_rect_radius_rule_and_shape():
    r = P.round_rect_radius((0, 0), (40, 20), (40, 25))
    assert r == pytest.approx(5.0)
    assert P.round_rect_radius((0, 0), (40, 20), (100, 100)) == pytest.approx(10.0)  # 0.5*min(w,h)
    c = P.round_rect((0, 0), (40, 20), 5)
    assert len(c.segments) == 8 and c.closed
    assert c.length() == pytest.approx(2 * (30 + 10) + 2 * math.pi * 5)


def test_circle_three_point_and_arc():
    c = P.circle_3pt((10, 0), (0, 10), (-10, 0))
    a = c.segments[0]
    assert approx(a.center, (0, 0)) and a.radius == pytest.approx(10)
    arc = P.arc_3pt((10, 0), (0, 10), (-10, 0)).segments[0]
    assert arc.ccw and arc.sweep() == pytest.approx(math.pi)
    arc2 = P.arc_3pt((-10, 0), (0, 10), (10, 0)).segments[0]
    assert not arc2.ccw and arc2.sweep() == pytest.approx(-math.pi)
    assert P.circle_3pt((0, 0), (1, 1), (2, 2)) is None


def test_scan_arc_ccw_from_start_to_end_angle():
    c = P.arc_center((0, 0), (5, 0), (0, 100)).segments[0]
    assert c.radius == pytest.approx(5) and approx(c.end, (0, 5)) and c.sweep() == pytest.approx(math.pi / 2)


def test_polygon_and_star():
    hexa = P.polygon((0, 0), (10, 0), 6)
    assert len(hexa.segments) == 6
    assert all(math.dist((0, 0), s.start) == pytest.approx(10) for s in hexa.segments)
    r = P.star_default_inner((0, 0), (10, 0), 6)
    assert r == pytest.approx(10 * math.cos(2 * math.pi / 6) / math.cos(math.pi / 6))
    st = P.star((0, 0), (10, 0), 6)
    assert len(st.segments) == 12
    assert math.dist((0, 0), st.segments[1].start) == pytest.approx(r)
    st2 = P.star((0, 0), (10, 0), 5, inner=3)
    assert math.dist((0, 0), st2.segments[1].start) == pytest.approx(3)
    assert P.polygon((0, 0), (1, 0), 1).segments.__len__() == 3     # clamped to >= 3


def test_ellipse_and_spline_and_point():
    e = P.ellipse((0, 0), (20, 0), 0.5)
    xs = [p[0] for p in e.points()]
    ys = [p[1] for p in e.points()]
    assert max(xs) == pytest.approx(20, abs=1e-6) and max(ys) == pytest.approx(10, abs=0.02)
    s = P.spline([(0, 0), (10, 10), (20, 0)])
    pts = list(s.points())
    assert approx(pts[0], (0, 0)) and approx(pts[-1], (20, 0))
    assert any(math.dist(p, (10, 10)) < 1e-6 for p in pts)   # interpolating
    pt = P.point((3, 4))
    assert pt.extra["point"] and pt.segments[0].start == (3.0, 4.0)


def test_polyline_bulge_arc():
    c = P.polyline([(0, 0), (10, 0), (10, 10)], [0.0, math.tan(math.pi / 8)])
    a = c.segments[1]
    assert isinstance(a, Arc) and a.ccw
    assert a.sweep() == pytest.approx(math.pi / 2)
    b = P.tangent_bulge((1, 0), (10, 0), (10, 10))
    assert b == pytest.approx(math.tan(math.pi / 4))     # semicircle-free quarter? tangent arc


# ------------------------------------------------------------------ intersections / trim / extend
def test_intersections_line_arc():
    hits = intersections(Line((-20, 0), (20, 0)), Arc((10, 0), (10, 0), (0, 0), True))
    assert sorted(round(p[0], 6) for _, _, p in hits) == [-10, 10]


def _cross_doc():
    h = Part([Contour([Line((0, 0), (100, 0))], False)], name="h")
    v1 = Part([Contour([Line((30, -10), (30, 10))], False)], name="v1")
    v2 = Part([Contour([Line((60, -10), (60, 10))], False)], name="v2")
    return [h, v1, v2]


def test_trim_removes_bracketed_piece():
    parts = _cross_doc()
    assert ED.trim_at(parts, (45, 0.1), 1.0)
    hs = [p for p in parts if p.name.startswith("h")]
    lens = sorted(round(p.contours[0].length(), 6) for p in hs)
    assert lens == [30, 40]
    parts = _cross_doc()
    assert ED.trim_at(parts, (80, 0), 1.0)
    h = [p for p in parts if p.name.startswith("h")]
    assert len(h) == 1 and h[0].contours[0].length() == pytest.approx(60)
    assert not ED.trim_at(_cross_doc(), (500, 500), 1.0)


def test_trim_closed_contour():
    sq = Part([P.rect((0, 0), (20, 20))], name="sq")
    cut = Part([Contour([Line((10, -5), (10, 25))], False)], name="cut")
    parts = [sq, cut]
    assert ED.trim_at(parts, (20, 10), 0.5)          # right half of the square goes
    s = [p for p in parts if p.name.startswith("sq")][0].contours[0]
    assert not s.closed and s.length() == pytest.approx(40)


def test_extend_line_and_arc():
    parts = [Part([Contour([Line((0, 0), (10, 0))], False)], name="a"),
             Part([Contour([Line((30, -10), (30, 10))], False)], name="wall")]
    assert ED.extend_at(parts, (9, 0), 1.0)
    assert approx(parts[0].contours[0].segments[-1].end, (30, 0))
    arc = Contour([Arc((10, 0), (0, 10), (0, 0), True)], False)
    parts = [Part([arc]), Part([Contour([Line((-20, 0), (-5, 0))], False)])]
    assert ED.extend_at(parts, (1, 10), 1.0)
    assert approx(parts[0].contours[0].segments[0].end, (-10, 0))


def test_split_open_and_closed():
    parts = [Part([Contour([Line((0, 0), (10, 0))], False)], name="l")]
    assert ED.split_at(parts, (4, 0.2), 1.0)
    assert sorted(round(p.contours[0].length(), 6) for p in parts) == [4, 6]
    parts = [Part([P.rect((0, 0), (10, 10))])]
    assert ED.split_at(parts, (10, 5), 1.0)
    c = parts[0].contours[0]
    assert not c.closed and approx(c.segments[0].start, (10, 5)) and c.length() == pytest.approx(40)


def test_crossings_self_intersection():
    bow = Contour([Line((0, 0), (10, 10)), Line((10, 10), (10, 0)), Line((10, 0), (0, 10))], False)
    xs = contour_crossings(bow, [])
    assert len(xs) == 2


def test_explode_and_merge_roundtrip():
    sq = Part([P.rect((0, 0), (10, 10))], name="sq")
    pieces = ED.explode_parts([sq])
    assert len(pieces) == 4 and all(not p.contours[0].closed for p in pieces)
    merged = ED.merge_parts(pieces, 0.1)
    assert len(merged) == 1 and merged[0].contours[0].closed
    assert merged[0].contours[0].length() == pytest.approx(40)
    circ = Part([P.circle((0, 0), 5)])
    assert len(ED.explode_parts([circ])) == 1
    # gap within / outside tolerance
    a = Contour([Line((0, 0), (10, 0))], False)
    b = Contour([Line((10.05, 0), (20, 0))], False)
    assert len(ED.join_contours([a, b], 0.1)) == 1
    assert len(ED.join_contours([a, b], 0.01)) == 2
    c = Contour([Line((0, 5), (20, 5.0))], False)
    rev = Contour([Line((20, 0), (10.05, 0))], False)       # reversed direction joins too
    assert len(ED.join_contours([a, rev, c], 0.1)) == 2


# ------------------------------------------------------------------ arrange
def _doc_two():
    return Document(parts=[Part([P.rect((0, 0), (10, 10))], selected=True),
                           Part([P.rect((20, 5), (40, 30))], selected=True)])


def test_align_modes():
    d = _doc_two()
    assert AR.align(d.parts, 0) == 1
    assert d.parts[1].bbox()[0] == pytest.approx(0)
    d = _doc_two()
    AR.align(d.parts, 3)
    assert d.parts[0].bbox()[3] == pytest.approx(30)
    d = _doc_two()
    AR.align(d.parts, 6)
    b0, b1 = d.parts[0].bbox(), d.parts[1].bbox()
    assert (b0[0] + b0[2]) / 2 == pytest.approx((b1[0] + b1[2]) / 2)
    assert AR.align(d.parts[:1], 0) == 0                     # needs >= 2


def test_size_lock_and_anchor():
    assert AR.size_factors(10, 20, 20, 20, True) == (2.0, 2.0)
    assert AR.size_factors(10, 20, 10, 10, True) == (0.5, 0.5)
    assert AR.size_factors(10, 20, 20, 10, False) == (2.0, 0.5)
    parts = [Part([P.rect((0, 0), (10, 20))])]
    AR.resize(parts, 20, 0, True, 5)
    assert parts[0].bbox() == pytest.approx((-5, -10, 15, 30))
    parts = [Part([P.rect((0, 0), (10, 20))])]
    AR.resize(parts, 20, 20, False, 7)          # bottom-left stays
    assert parts[0].bbox() == pytest.approx((0, 0, 20, 20))
    assert AR.preset_factor(0x3394, (0, 0, 10, 10)) == pytest.approx(0.75)
    assert AR.preset_factor(0x3399, (0, 0, 50, 20)) == pytest.approx(2.0)


def test_groups():
    d = _doc_two()
    g = AR.group(d)
    assert len(d.parts) == 1 and AR.is_group(g) and len(g.contours) == 2
    for p in d.parts:
        p.selected = False
    assert AR.select_groups(d) == 1
    assert AR.explode_group(d, True) == 1 and len(d.parts) == 2
    assert not any(AR.is_group(p) for p in d.parts)


def test_select_similar():
    a = Part([P.rect((0, 0), (10, 5))], selected=True)
    b = Part([P.rect((100, 100), (110, 105))])               # translated copy
    c = Part([P.rect((0, 0), (5, 10))])                      # rotated by 90 deg: same edges
    e = Part([P.rect((0, 0), (10, 6))])
    d = Document(parts=[a, b, c, e])
    assert AR.select_similar(d) == 2
    assert [p.selected for p in d.parts] == [True, True, True, False]


def test_select_by_topology_depth():
    outer = Part([P.rect((0, 0), (100, 100))])
    hole = Part([P.circle((50, 50), 10)])
    island = Part([P.circle((50, 50), 3)])
    free = Part([P.rect((200, 0), (210, 10))])
    d = Document(parts=[outer, hole, island, free])
    assert AR.select_by_topology(d, 1) == 2
    assert [p.selected for p in d.parts] == [True, False, False, True]
    AR.select_by_topology(d, 2)
    assert [p.selected for p in d.parts] == [False, True, False, False]


def test_measure_and_auto_step():
    assert AR.measure((0, 0), (3, -4)) == (3, 4, 5)
    assert AR.auto_step(100) == 2.0
    assert AR.auto_step(1000) == 20.0
    assert AR.auto_step(10) == pytest.approx(0.2)
    assert AR.auto_step(40) == pytest.approx(0.8)


# ------------------------------------------------------------------ snap
def test_snap_points():
    parts = [Part([P.rect((0, 0), (10, 10))]), Part([P.circle((50, 50), 5)])]
    assert snap(parts, (0.3, -0.2), 0.5) == ((0, 0), "end")
    assert snap(parts, (5.1, 0.1), 0.5) == ((5.0, 0.0), "mid")
    assert snap(parts, (50.2, 49.9), 0.5) == ((50, 50), "center")
    assert snap(parts, (55.1, 50), 0.5)[1] == "quadrant"
    assert snap(parts, (30, 30), 0.5) is None


# ------------------------------------------------------------------ qr / text
def test_qr_scan_lines():
    from mlaser.cad.qr import qr_matrix, qr_scan_lines
    m = qr_matrix("https://example.com")
    n = len(m)
    lines = qr_scan_lines("https://example.com", 25.0, 4, (10, 20))
    assert lines and all(len(c.segments) == 1 and not c.closed for c in lines)
    ys = sorted({round(c.segments[0].start[1], 6) for c in lines})
    assert ys[0] == pytest.approx(20 + 0.125) and ys[1] - ys[0] == pytest.approx(0.25)
    xs = [x for c in lines for x in (c.segments[0].start[0], c.segments[0].end[0])]
    assert min(xs) >= 10 - 1e-9 and max(xs) <= 35 + 1e-9
    # finder pattern: the top-left module row starts with 7 dark modules
    assert all(m[0][:7]) and n >= 21


def test_qr_squares():
    from mlaser.cad.qr import qr_squares
    cs = qr_squares("hello", 21.0)
    assert cs and all(c.closed for c in cs)


def test_text_contours():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mlaser.cad.text import resolve_family, text_contours
    fam = resolve_family(None)
    cs = text_contours("HI", fam, 10.0, (5, 5))
    assert len(cs) >= 2 and all(c.closed for c in cs)
    xs = [p[0] for c in cs for p in c.points()]
    ys = [p[1] for c in cs for p in c.points()]
    assert min(xs) == pytest.approx(5) and min(ys) == pytest.approx(5)
    assert 3 < max(ys) - min(ys) <= 10.0
    assert text_contours("", fam, 10) == []
