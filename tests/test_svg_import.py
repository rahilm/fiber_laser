"""SVG import through svgelements (``mlaser.io.svg_import``): one small inline SVG per feature."""
from __future__ import annotations

import math

import pytest

pytest.importorskip("svgelements")

from mlaser.io.svg_import import PX_MM, import_svg, parse_path  # noqa: E402
from mlaser.model import Arc, Line  # noqa: E402

HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">')


def load(tmp_path, body, head=HEAD, **kw):
    p = tmp_path / "t.svg"
    p.write_text(head + body + "</svg>")
    return [c for part in import_svg(str(p), **kw).parts for c in part.contours]


def bbox(c):
    pts = list(c.points(0.001))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def full_circle(c):
    assert len(c.segments) == 1 and isinstance(c.segments[0], Arc)
    a = c.segments[0]
    assert a.start == pytest.approx(a.end)
    return a


# ----------------------------------------------------------------- use / symbol / defs
def test_use_of_defs_element_and_symbol_viewbox(tmp_path):
    cs = load(tmp_path, """
      <defs><circle id="c" r="2"/><rect id="never" width="50" height="50"/></defs>
      <symbol id="s" viewBox="0 0 10 10"><rect width="10" height="10"/></symbol>
      <use href="#c" x="20" y="30"/>
      <use xlink:href="#c" transform="translate(40,30)"/>
      <use href="#s" x="60" y="0" width="20" height="20"/>""")
    assert len(cs) == 3                                   # defs / symbol content not cut directly
    a, b = full_circle(cs[0]), full_circle(cs[1])
    assert a.center == pytest.approx((20, 70)) and a.radius == pytest.approx(2)
    assert b.center == pytest.approx((40, 70))
    assert bbox(cs[2]) == pytest.approx((60, 80, 80, 100))   # symbol scaled 2x by the use size


def test_use_of_nested_svg_and_nested_svg_viewport(tmp_path):
    cs = load(tmp_path, """
      <svg x="10" y="10" width="20" height="20" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>
      <svg x="50" y="0" width="40" height="20" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>""")
    assert bbox(cs[0]) == pytest.approx((10, 70, 30, 90))
    # preserveAspectRatio xMidYMid meet: scale 2, centred in the 40 wide viewport
    assert bbox(cs[1]) == pytest.approx((60, 80, 80, 100))


# ----------------------------------------------------------------- CSS
def test_css_classes_and_style_attribute(tmp_path):
    cs = load(tmp_path, """
      <style>.gone { display: none } rect.ghost { visibility: hidden } #byid { display:none }</style>
      <rect class="gone" width="5" height="5"/>
      <rect class="ghost" width="6" height="6"/>
      <circle class="ghost" cx="50" cy="50" r="3"/>
      <rect id="byid" width="7" height="7"/>
      <rect style="display:none" width="8" height="8"/>
      <rect style="fill:red;stroke:black" x="1" y="1" width="9" height="9"/>""")
    assert len(cs) == 2                                    # the styled rect and the circle
    lengths = sorted(round(c.length(), 6) for c in cs)
    assert lengths == pytest.approx(sorted([36.0, 2 * math.pi * 3]))   # rect.ghost only matches rects


# ----------------------------------------------------------------- visibility / display
def test_display_none_and_visibility_hidden_skipped(tmp_path):
    cs = load(tmp_path, """
      <g display="none"><rect width="5" height="5"/></g>
      <g visibility="hidden"><rect width="6" height="6"/>
         <rect visibility="visible" x="10" width="7" height="7"/></g>
      <rect style="visibility:collapse" width="8" height="8"/>
      <rect x="20" width="9" height="9"/>""")
    assert sorted(round(c.length(), 6) for c in cs) == [28.0, 36.0]


def test_clip_path_and_mask_content_not_cut(tmp_path):
    cs = load(tmp_path, """
      <clipPath id="cp"><rect width="5" height="5"/></clipPath>
      <mask id="m"><rect width="6" height="6"/></mask>
      <marker id="mk"><path d="M0 0 L3 3"/></marker>
      <pattern id="pt" width="2" height="2"><rect width="1" height="1"/></pattern>
      <rect x="10" y="10" width="20" height="20" clip-path="url(#cp)" mask="url(#m)"/>""")
    assert len(cs) == 1 and cs[0].length() == pytest.approx(80)


# ----------------------------------------------------------------- rounded rect
def test_rounded_rect_circular_corners_are_arcs(tmp_path):
    (c,) = load(tmp_path, '<rect x="10" y="10" width="20" height="10" rx="2"/>')
    arcs = [s for s in c.segments if isinstance(s, Arc)]
    assert len(arcs) == 4 and all(a.radius == pytest.approx(2) for a in arcs)
    assert c.length() == pytest.approx(2 * (16 + 6) + 2 * math.pi * 2)
    assert bbox(c) == pytest.approx((10, 80, 30, 90))


def test_rounded_rect_elliptic_corners_flattened(tmp_path):
    (c,) = load(tmp_path, '<rect x="0" y="0" width="40" height="20" rx="8" ry="4"/>', tol=0.005)
    assert all(isinstance(s, Line) for s in c.segments)
    # corner (top-left in SVG) quarter ellipse centre (8, 4) -> machine (8, 96); points on it
    for s in c.segments:
        x, y = s.end
        if x < 8 and y > 96:
            assert ((x - 8) / 8) ** 2 + ((y - 96) / 4) ** 2 == pytest.approx(1, abs=1e-6)


# ----------------------------------------------------------------- transforms
def test_nested_transforms_keep_circle_arcs(tmp_path):
    cs = load(tmp_path, """
      <g transform="translate(50,50)"><g transform="rotate(90)"><g transform="scale(2)">
        <circle cx="5" cy="0" r="3"/>
        <path d="M 0 10 A 2 2 0 0 1 4 10"/>
      </g></g></g>""")
    circle = full_circle(cs[0])
    # (5,0) *2 -> (10,0), rotate 90 -> (0,10), +50 -> (50,60), flip -> (50,40)
    assert circle.center == pytest.approx((50, 40)) and circle.radius == pytest.approx(6)
    (arc,) = cs[1].segments if not cs[1].closed else ()
    assert isinstance(arc, Arc) and arc.radius == pytest.approx(4)
    # path (0,10)->(4,10) scaled (0,20)->(8,20), rotated (-20,0)->(-20,8), +50 & flip
    assert arc.start == pytest.approx((30, 50)) and arc.end == pytest.approx((30, 42))
    mid = arc.point_at(0.5)
    assert math.dist(mid, arc.center) == pytest.approx(4)


def test_non_uniform_and_skew_transforms_flatten_within_tol(tmp_path):
    tol = 0.01
    cs = load(tmp_path, """
      <circle transform="translate(30,30) scale(3,1)" r="5"/>
      <circle transform="translate(70,70) skewX(30)" r="5"/>""", tol=tol)
    e, k = cs
    assert all(isinstance(s, Line) for s in e.segments + k.segments)
    for s in e.segments:                                    # ellipse a=15, b=5 around (30, 70)
        x, y = s.end
        assert ((x - 30) / 15) ** 2 + ((y - 70) / 5) ** 2 == pytest.approx(1, abs=1e-9)
        mx, my = (s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2
        # chord midpoint inside the ellipse by at most tol (radial approximation)
        rr = math.hypot((mx - 30) / 15, (my - 70) / 5)
        assert rr <= 1 + 1e-12 and (1 - rr) * 5 <= tol + 1e-9
    t = math.tan(math.radians(30))                          # skewX: x' = x + y tan30
    for s in k.segments:
        x, y = s.end[0] - 70, -(s.end[1] - 30)
        assert (x - y * t) ** 2 + y ** 2 == pytest.approx(25, abs=1e-6)


def test_cubic_and_quadratic_chord_error_bounded(tmp_path):
    tol = 0.02
    (c,) = load(tmp_path, '<path d="M 10 50 C 10 0 90 0 90 50 Q 50 100 10 50 Z"/>', tol=tol)

    def cubic(t):
        a, b, cc, d = (10, 50), (10, 0), (90, 0), (90, 50)
        m = 1 - t
        return (m ** 3 * a[0] + 3 * m * m * t * b[0] + 3 * m * t * t * cc[0] + t ** 3 * d[0],
                m ** 3 * a[1] + 3 * m * m * t * b[1] + 3 * m * t * t * cc[1] + t ** 3 * d[1])

    def quad(t):
        a, b, d = (90, 50), (50, 100), (10, 50)
        m = 1 - t
        return (m * m * a[0] + 2 * m * t * b[0] + t * t * d[0], m * m * a[1] + 2 * m * t * b[1] + t * t * d[1])

    segs = [((s.start[0], 100 - s.start[1]), (s.end[0], 100 - s.end[1])) for s in c.segments]

    def dist_to_poly(p):
        best = math.inf
        for a, b in segs:
            vx, vy = b[0] - a[0], b[1] - a[1]
            L2 = vx * vx + vy * vy
            u = 0 if L2 == 0 else max(0, min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2))
            best = min(best, math.dist(p, (a[0] + vx * u, a[1] + vy * u)))
        return best
    worst = max(dist_to_poly(f(i / 400)) for f in (cubic, quad) for i in range(401))
    assert worst <= tol
    assert len(c.segments) < 200


# ----------------------------------------------------------------- units / viewBox
@pytest.mark.parametrize("head,size", [
    ('<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in" viewBox="0 0 200 100">', 50.8 / 200),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="20cm" height="10cm" viewBox="0 0 200 100">', 1.0),
    ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">', PX_MM),
    ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">', PX_MM),
])
def test_units_and_viewbox(tmp_path, head, size):
    (c,) = load(tmp_path, '<rect x="10" y="20" width="50" height="30"/>', head=head)
    H = 100 * size
    assert bbox(c) == pytest.approx((10 * size, H - 50 * size, 60 * size, H - 20 * size))


def test_units_on_elements(tmp_path):
    head = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100">'
    (c,) = load(tmp_path, '<rect x="0" y="0" width="1in" height="10"/>', head=head)
    # inside a viewBox, 1in = 96 user units: the viewBox maps 1 user unit to 1 mm
    assert bbox(c) == pytest.approx((0, 90, 96, 100))


def test_viewbox_aspect_meet_not_stretched(tmp_path):
    """The old parser stretched a viewBox to width/height separately (non-uniform); SVG's
    default preserveAspectRatio="xMidYMid meet" scales uniformly and centres."""
    head = '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 10 10">'
    (c,) = load(tmp_path, '<rect width="10" height="10"/>', head=head)
    assert bbox(c) == pytest.approx((25, 0, 75, 50), abs=1e-9)


# ----------------------------------------------------------------- misc
def test_open_paths_join_and_lines(tmp_path):
    cs = load(tmp_path, """
      <line x1="0" y1="0" x2="10" y2="0"/><line x1="10" y1="0" x2="10" y2="10"/>
      <polyline points="10,10 0,10 0,0"/>
      <polyline points="50,50 60,60"/>""")
    closed = [c for c in cs if c.closed]
    assert len(closed) == 1 and closed[0].length() == pytest.approx(40)
    assert any(not c.closed and c.length() == pytest.approx(math.sqrt(200)) for c in cs)


def test_text_and_image_ignored(tmp_path):
    cs = load(tmp_path, '<text x="1" y="1">hi</text><image href="x.png" width="5" height="5"/>'
                        '<polygon points="0,0 10,0 10,10"/>')
    assert len(cs) == 1 and cs[0].closed


def test_parse_path_compat():
    subs = parse_path("M0 0 L10 0 L10 10 Z M 20 0 a 5 5 0 0 1 10 0", tol=0.01)
    assert subs[0][1] is True and subs[0][0][:3] == [(0, 0), (10, 0), (10, 10)]
    pts, closed = subs[1]
    assert not closed and pts[0] == pytest.approx((20, 0)) and pts[-1] == pytest.approx((30, 0))
    assert all(math.dist(p, (25, 0)) == pytest.approx(5) for p in pts)
