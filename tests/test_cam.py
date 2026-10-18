import math
import os
import re

import pytest

from mlaser.geom.contour import is_ccw, point_in_contour, rotate_start, seg_tangent, signed_area, sub_path
from mlaser.cam.items import CutArc, CutLine, LaserOff, LaserOn, Pierce, Rapid
from mlaser.cam.kerf import compensate, kerf_sign
from mlaser.cam.leads import contour_leads, lead_in, lead_out, side_flag
from mlaser.cam.microjoint import auto_joints, joint_intervals, split_by_joints
from mlaser.cam.ordering import build_tree, choose_start, group_nested, order_nodes
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.io.gcode import contours_to_gcode, parse_gcode, toolpath_to_gcode
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Line, Part

ORIG = "/home/dev/scratch/mlaser/extracted/Mlaser-v0.0.1.51_SP1/Mlaser-v0.0.1.51_SP1"


def rect(x0, y0, x1, y1, ccw=True):
    p = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if not ccw:
        p = p[::-1]
    return Contour([Line(a, b) for a, b in zip(p, p[1:] + p[:1])])


def circle(cx, cy, r, ccw=True):
    p = (cx + r, cy)
    return Contour([Arc(p, p, (cx, cy), ccw)])


def connected(segs, tol=1e-6):
    return all(math.dist(a.end, b.start) < tol for a, b in zip(segs, segs[1:]))


# ---------------------------------------------------------------- leads
@pytest.mark.parametrize("kind", [1, 2, 3])
@pytest.mark.parametrize("angle", [30.0, 90.0, 135.0])
def test_lead_in_tangent_and_side(kind, angle):
    P, d = (0.0, 0.0), (1.0, 0.0)
    lp = LeadParams(kind=kind, angle_deg=angle, length=3.0, radius=1.0)
    segs = lead_in(P, d, lp, flag=False)
    assert segs and connected(segs)
    assert math.dist(segs[-1].end, P) < 1e-9
    start = segs[0].start
    assert start[1] < 0            # flag 0 -> lead on the right of +x travel
    if kind in (2, 3) and isinstance(segs[-1], Arc):
        t = seg_tangent(segs[-1], 1.0)
        assert t[0] == pytest.approx(1.0, abs=1e-6) and t[1] == pytest.approx(0.0, abs=1e-6)
        assert not segs[-1].ccw     # cw when flag == 0
    mirrored = lead_in(P, d, lp, flag=True)
    assert mirrored[0].start[1] == pytest.approx(-start[1])


def test_lead_types_geometry():
    P, d = (0.0, 0.0), (1.0, 0.0)
    l1 = lead_in(P, d, LeadParams(1, 90, 2, 1), False)
    assert l1[0].start == pytest.approx((0, -2))
    l2 = lead_in(P, d, LeadParams(2, 90, 2, 1), False)   # quarter circle radius len
    assert l2[0].radius == pytest.approx(2.0) and l2[0].start == pytest.approx((-2, -2))
    l3 = lead_in(P, d, LeadParams(3, 90, 3, 1), False)   # line then R1 arc
    assert isinstance(l3[0], Line) and isinstance(l3[1], Arc) and l3[1].radius == pytest.approx(1)
    assert sum(s.length() for s in l3) > 2.0
    # type 3 with radius too big -> type 2
    assert len(lead_in(P, d, LeadParams(3, 90, 1.0, 5.0), False)) == 1
    lo = lead_out(P, d, LeadParams(2, 90, 2, 1), False)
    assert lo[0].start == P and lo[0].end == pytest.approx((2, -2))
    assert lead_in(P, d, LeadParams(1, 90, 0.005, 1), False) == []


def test_side_rule_scrap_side():
    outer = rect(0, 0, 50, 50, ccw=True)
    hole = rect(20, 20, 30, 30, ccw=False)
    lp = LeadParams(1, 90, 2, 1)
    fo = side_flag(1, True)
    fh = side_flag(2, False)
    li, _ = contour_leads(rotate_start(outer, 25), lp, LeadParams(0), fo)
    assert not point_in_contour(li[0].start, outer)      # outer lead starts outside
    hi, _ = contour_leads(rotate_start(hole, 5), lp, LeadParams(0), fh)
    assert point_in_contour(hi[0].start, hole)           # hole lead starts inside the hole
    # orientation of the drawing does not matter
    hole_ccw = rect(20, 20, 30, 30, ccw=True)
    hi2, _ = contour_leads(rotate_start(hole_ccw, 5), lp, LeadParams(0), side_flag(2, True))
    assert point_in_contour(hi2[0].start, hole_ccw)


# ---------------------------------------------------------------- kerf
def test_kerf_square_and_circle():
    sq = rect(0, 0, 10, 10)
    grown = compensate(sq, 1, 0.1, depth=1)
    assert signed_area(grown) == pytest.approx(10.2 ** 2, rel=1e-6)
    shrunk = compensate(sq, 2, 0.1, depth=1)
    assert signed_area(shrunk) == pytest.approx(9.8 ** 2, rel=1e-6)
    cw = compensate(rect(0, 0, 10, 10, ccw=False), 1, 0.1, depth=1)
    assert abs(signed_area(cw)) == pytest.approx(10.2 ** 2, rel=1e-6)
    # hole (depth 2): path moves into the hole (scrap side) for mode 1
    assert compensate(circle(0, 0, 5), 1, 0.1, depth=2).segments[0].radius == pytest.approx(4.9)
    assert compensate(circle(0, 0, 5, ccw=False), 1, 0.1, depth=2).segments[0].radius == pytest.approx(4.9)
    assert compensate(circle(0, 0, 5), 1, 0.1, depth=1).segments[0].radius == pytest.approx(5.1)
    # gate: too small for the width
    assert compensate(rect(0, 0, 0.2, 0.2), 1, 0.1) is None
    assert kerf_sign(-1, 1, True) == 0


def test_kerf_preserves_arcs_and_concave():
    rr = Contour([Line((1, 0), (9, 0)), Arc((9, 0), (10, 1), (9, 1), True), Line((10, 1), (10, 9)),
                  Arc((10, 9), (9, 10), (9, 9), True), Line((9, 10), (1, 10)),
                  Arc((1, 10), (0, 9), (1, 9), True), Line((0, 9), (0, 1)), Arc((0, 1), (1, 0), (1, 1), True)])
    g = compensate(rr, 1, 0.2)
    assert sum(isinstance(s, Arc) for s in g.segments) == 4
    assert connected(g.segments + g.segments[:1])
    L = Contour([Line(a, b) for a, b in zip(
        [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)],
        [(10, 0), (10, 5), (5, 5), (5, 10), (0, 10), (0, 0)])])
    assert signed_area(compensate(L, 1, 0.5)) == pytest.approx(96.0, rel=1e-6)
    assert signed_area(compensate(L, 2, 0.5)) == pytest.approx(56.0, rel=1e-6)


# ---------------------------------------------------------------- micro-joints
def test_microjoints():
    sq = rect(0, 0, 10, 10)
    iv = joint_intervals(40, [10.0, (20.0, 1.0), 39.9], 0.5)
    assert (9.75, 10.25) in iv and (19.5, 20.5) in iv
    assert any(a == 0.0 for a, _ in iv) and any(b == 40 for _, b in iv)   # wrapped around the start
    pieces = split_by_joints(sq, joint_intervals(40, [5.0, 25.0], 0.5))
    off = sum(s.length() for s, on in pieces if not on)
    on = sum(s.length() for s, on in pieces if on)
    assert off == pytest.approx(1.0) and on == pytest.approx(39.0)
    assert connected([s for s, _ in pieces])
    js = auto_joints(sq, count=3, joint_len=0.5)
    assert [p for p, _ in js] == pytest.approx([10, 20, 30])
    assert auto_joints(rect(0, 0, 1, 1), count=3) == []


# ---------------------------------------------------------------- ordering / start
def test_tree_and_inner_before_outer():
    outer = rect(0, 0, 100, 100)
    h1 = circle(20, 20, 5, ccw=False)
    h2 = circle(80, 80, 5, ccw=False)
    island = circle(80, 80, 2)          # part inside a hole
    other = rect(200, 0, 250, 50)
    nodes = build_tree([outer, h1, h2, island, other])
    depth = {n.key: n.depth for n in nodes}
    assert depth == {0: 1, 1: 2, 2: 2, 3: 3, 4: 1}
    for mode in (1, 2, 3, 4, 5):
        order = [n.key for n in order_nodes(nodes, mode)]
        assert order.index(1) < order.index(0) and order.index(2) < order.index(0)
        assert order.index(3) < order.index(2)
    lr = [n.key for n in order_nodes(nodes, 1) if n.depth == 1]
    rl = [n.key for n in order_nodes(nodes, 2) if n.depth == 1]
    assert lr == [0, 4] and rl == [4, 0]
    groups = group_nested([(i, c) for i, c in enumerate([outer, h1, h2, island, other])])
    assert sorted(len(cs) for _, cs in groups) == [1, 1, 3]


def test_start_point_modes():
    c = rect(0, 0, 10, 4)
    assert choose_start(c, 0, lead_len=2.0) == pytest.approx(5.0)       # middle of first long line
    assert choose_start(c, 0, lead_len=3.0) == pytest.approx(5.0)       # 10 > 6
    assert choose_start(c, 0, lead_len=6.0) == pytest.approx(5.0)       # none > 12 -> longest (first)
    assert choose_start(c, 1) == pytest.approx(0.0)                     # first vertex
    assert choose_start(c, 2, percent=50) == pytest.approx(14.0)
    assert choose_start(c, 3) == 0.0
    r = rotate_start(c, 5.0)
    assert r.segments[0].start == pytest.approx((5, 0)) and r.length() == pytest.approx(28)


# ---------------------------------------------------------------- toolpath
def _doc():
    outer = rect(0, 0, 100, 50)
    hole = circle(25, 25, 5)
    craft = Craft(kerf_comp=1, kerf_width=0.1, lead_in=LeadParams(2, 90, 3, 1), micro_joints=[50.0])
    craft.extra["overcut"] = 0.0
    return Document(parts=[Part([outer, hole], craft)], layers={1: LayerParams(speed=80)})


def test_build_toolpath_stream():
    items = build_toolpath(_doc(), CamParams())
    kinds = [type(i).__name__ for i in items]
    assert kinds.count("Pierce") == 2 and kinds.count("LaserOn") == 2 and kinds.count("LaserOff") == 2
    cuts = [i for i in items if isinstance(i, (CutLine, CutArc))]
    # first contour cut is the hole (inner before outer)
    first = cuts[0]
    assert math.dist(first.start, (25, 25)) < 5
    # moves are continuous within each contour
    rap = [i for i in items if isinstance(i, Rapid)]
    assert len(rap) == 2
    for a, b in zip(items, items[1:]):
        if isinstance(a, (CutLine, CutArc)) and isinstance(b, (CutLine, CutArc)):
            assert math.dist(a.end, b.start) < 1e-6
    assert any(not c.laser for c in cuts)      # micro-joint gap
    assert all(c.feed == 80 for c in cuts)
    # pierce happens at the start of the lead-in
    p = [i for i in items if isinstance(i, Pierce)][1]
    nxt = items[items.index(p) + 2]
    assert isinstance(nxt, (CutLine, CutArc)) and nxt.start == pytest.approx(p.point)


def test_overcut_and_gap():
    c = rect(0, 0, 10, 10)
    for oc, exp in ((2.0, 42.0), (-2.0, 38.0)):
        craft = Craft(lead_in=LeadParams(0), lead_out=LeadParams(0))
        craft.extra["overcut"] = oc
        doc = Document(parts=[Part([c], craft)])
        items = build_toolpath(doc, CamParams(start_mode=3))
        L = sum(math.dist(i.start, i.end) for i in items if isinstance(i, CutLine))
        assert L == pytest.approx(exp)


def test_gcode_export_roundtrip():
    items = build_toolpath(_doc(), CamParams())
    text = toolpath_to_gcode(items)
    assert text.startswith("G17\r\nG90\r\nG0 Z1\r\nG0 X0 Y0\r\n") and text.endswith("G0 Z1\r\n")
    for ln in text.split("\r\n"):
        for m in re.finditer(r"[XYIJ](-?\d+\.\d+)", ln):
            assert len(m.group(1).split(".")[1]) == 4
    doc = parse_gcode(contours_to_gcode([rect(0, 0, 10, 10), circle(5, 5, 2)]))
    assert len(doc.parts) == 2
    assert all(p.contours[0].closed for p in doc.parts)
    assert doc.parts[1].contours[0].length() == pytest.approx(2 * math.pi * 2, rel=1e-6)


@pytest.mark.skipif(not os.path.exists(f"{ORIG}/SVG/Output.nc"), reason="original not available")
def test_parse_original_output_nc():
    with open(f"{ORIG}/SVG/Output.nc") as f:
        doc = parse_gcode(f.read())
    assert len(doc.parts) == 7
    assert all(p.contours[0].closed for p in doc.parts)
    x0, y0, x1, y1 = doc.bbox()
    assert x0 == pytest.approx(5.3854, abs=0.01) and 26 < x1 < 28


# ---------------------------------------------------------------- import
def test_dxf_import(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from mlaser.io.dxf_import import import_dxf
    d = ezdxf.new()
    m = d.modelspace()
    # square from loose lines, drawn in shuffled directions
    m.add_line((0, 0), (10, 0)); m.add_line((10, 10), (10, 0)); m.add_line((10, 10), (0, 10.01))
    m.add_line((0, 0), (0, 10))
    m.add_circle((30, 5), 3)
    m.add_lwpolyline([(50, 0, 0), (60, 0, 1), (60, 10, 0), (50, 10, 1)], format="xyb", close=True)
    m.add_arc((80, 0), 5, 0, 180)
    m.add_line((75, 0), (85, 0))
    m.add_spline_control_frame([(100, 0), (105, 5), (110, 0), (105, -5)]).closed = False
    m.add_ellipse((130, 0), major_axis=(5, 0), ratio=0.5)
    m.add_text("HELLO").dxf.insert = (0, 50)
    blk = d.blocks.new("B")
    blk.add_circle((0, 0), 1)
    m.add_blockref("B", (200, 0), dxfattribs={"xscale": 2, "yscale": 2})
    c2 = m.add_circle((300, 0), 1); c2.dxf.invisible = 1
    p = tmp_path / "t.dxf"
    d.saveas(p)
    doc = import_dxf(str(p), join_tol=0.05, placement=None)       # keep DXF coordinates
    cs = [c for part in doc.parts if part.extra.get("dxf_type") != "TEXT" for c in part.contours]
    closed = [c for c in cs if c.closed]
    assert len(closed) == 6        # square, circle, lwpoly, arc+line, ellipse, block circle
    sq = min(closed, key=lambda c: math.dist(c.segments[0].start, (0, 0)))
    assert sq.length() == pytest.approx(40, abs=0.05)
    lw = [c for c in closed if any(isinstance(s, Arc) for s in c.segments) and len(c.segments) == 4][0]
    assert lw.length() == pytest.approx(20 + 2 * math.pi * 5, rel=1e-6)
    blk_c = [c for c in closed if len(c.segments) == 1 and math.dist(c.segments[0].center, (200, 0)) < 1e-9]
    assert blk_c and blk_c[0].segments[0].radius == pytest.approx(2)
    assert doc.extra["texts"][0]["text"] == "HELLO"
    assert not any(isinstance(s, Arc) and math.dist(s.center, (300, 0)) < 1e-9 for c in cs for s in c.segments)


def test_svg_import(tmp_path):
    from mlaser.io.svg_import import import_svg
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50">
      <rect x="10" y="10" width="20" height="10"/>
      <circle cx="50" cy="25" r="5"/>
      <g transform="translate(60,0)"><path d="M0 0 L10 0 L10 10 Z"/></g>
      <path d="M 70 30 a 5 5 0 0 1 10 0 c 0 5 -10 5 -10 0 z"/>
    </svg>"""
    p = tmp_path / "t.svg"
    p.write_text(svg)
    doc = import_svg(str(p))
    cs = [c for part in doc.parts for c in part.contours]
    assert len(cs) == 4 and all(c.closed for c in cs)
    r = cs[0]
    assert r.length() == pytest.approx(60)
    assert min(y for s in r.segments for _, y in (s.start, s.end)) == pytest.approx(30)   # y flipped: 50 - 20
    circ = [c for c in cs if len(c.segments) == 1][0]
    assert circ.segments[0].radius == pytest.approx(5) and circ.segments[0].center == pytest.approx((50, 25))
    tri = cs[2]
    assert tri.segments[0].start == pytest.approx((60, 50))
