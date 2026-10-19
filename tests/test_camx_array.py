"""Array / fill operations (mlaser.cam.array), see docs/re/cam.md §14."""
import math

from mlaser.cam.array import (ArrayParams, FillParams, apply_rect_array, circular_array,
                              fill_circle_centres, fill_circles, fill_sheet_array, linear_fill,
                              parts_bbox, rect_array)
from mlaser.model import Arc, Contour, Document, Line, Part


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def square_part(x=0, y=0, w=10, h=5):
    return Part(contours=[rect(x, y, w, h)])


def test_rect_array_spacing_mode_positions_and_serpentine():
    p = square_part(0, 0, 10, 5)
    prm = ArrayParams(rows=2, cols=3, offset_type=1, row_gap=2, col_gap=1, row_dir=1, col_dir=1)
    out = rect_array([p], prm)
    assert len(out) == 5                       # 2*3 - original
    origins = [q.bbox()[:2] for q in out]
    # row 0: cols 1, 2 ; row 1 (odd): reversed col order 2, 1, 0
    assert origins == [(11, 0), (22, 0), (22, 7), (11, 7), (0, 7)]
    cells = [q.extra["array_cell"] for q in out]
    assert cells == [(0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]


def test_rect_array_offset_mode_and_negative_directions():
    p = square_part(0, 0, 10, 5)
    prm = ArrayParams(rows=2, cols=2, offset_type=0, row_gap=20, col_gap=30, row_dir=0, col_dir=0)
    out = rect_array([p], prm)
    bbs = sorted(q.bbox()[:2] for q in out)
    assert bbs == sorted([(-30, 0), (-30, -20), (0, -20)])
    # originals untouched, copies are independent
    assert p.bbox() == (0, 0, 10, 5)


def test_apply_rect_array_uses_selection():
    a, b = square_part(0, 0), square_part(100, 100)
    a.selected = True
    doc = Document(parts=[a, b])
    n = apply_rect_array(doc, ArrayParams(rows=1, cols=4, col_gap=5))
    assert n == 3 and len(doc.parts) == 5
    xs = sorted(q.bbox()[0] for q in doc.parts[2:])
    assert xs == [15, 30, 45]


def test_circular_array_rotation():
    p = Part(contours=[Contour([Line((10, 0), (12, 0))], False)])
    out = circular_array([p], 4, (0, 0))
    starts = [q.contours[0].segments[0].start for q in out]
    exp = [(0, 10), (-10, 0), (0, -10)]
    for s, e in zip(starts, exp):
        assert math.dist(s, e) < 1e-9
    # partial sweep: last copy on the end angle
    out = circular_array([p], 3, (0, 0), total_angle_deg=90)
    assert math.dist(out[-1].contours[0].segments[0].start, (0, 10)) < 1e-9


def test_fill_sheet_array_counts_and_inside():
    p = square_part(-50, -50, 10, 5)
    sheet = (0, 0, 100, 50)
    out = fill_sheet_array([p], sheet, gap=2, margin=3)
    # cols: (94+2)/(12) = 8 ; rows: (44+2)/7 = 6
    assert len(out) == 48
    x0, y0, x1, y1 = parts_bbox(out)
    assert x0 >= 3 - 1e-9 and y0 >= 3 - 1e-9 and x1 <= 97 + 1e-9 and y1 <= 47 + 1e-9
    # no overlaps: all bbox origins distinct and >= pitch apart in one axis
    orgs = {tuple(round(v, 6) for v in q.bbox()[:2]) for q in out}
    assert len(orgs) == 48


def _inside_ok(centres, outer_box, r, stock, holes=()):
    x0, y0, x1, y1 = outer_box
    for cx, cy in centres:
        assert x0 + r + 0.5 * stock - 1e-6 <= cx <= x1 - r - 0.5 * stock + 1e-6
        assert y0 + r + 0.5 * stock - 1e-6 <= cy <= y1 - r - 0.5 * stock + 1e-6
        for hx0, hy0, hx1, hy1 in holes:
            # circle (with clearance) does not touch the hole box
            dx = max(hx0 - cx, 0, cx - hx1)
            dy = max(hy0 - cy, 0, cy - hy1)
            assert math.hypot(dx, dy) >= r + 0.5 * stock - 1e-6


def test_fill_circles_rows_and_columns():
    outer = rect(0, 0, 100, 60)
    for ft in (0, 1):
        prm = FillParams(fill_type=ft, radius=4, stock=6, space=4)
        cs = fill_circle_centres(outer, [], prm)
        assert len(cs) > 10
        _inside_ok(cs, (0, 0, 100, 60), 4, 6)
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                assert math.dist(a, b) >= 2 * 4 + 0.7 * 4 - 1e-6
    parts = fill_circles(outer, [], FillParams(radius=2, stock=2, space=2), layer=3)
    seg = parts[0].contours[0].segments[0]
    assert isinstance(seg, Arc) and seg.start == seg.end and abs(seg.radius - 2) < 1e-9
    assert parts[0].craft.layer == 3


def test_fill_circles_avoid_hole_and_along_edge():
    outer = rect(0, 0, 100, 100)
    hole = rect(40, 40, 20, 20)
    prm = FillParams(fill_type=1, radius=3, stock=4, space=3)
    cs = fill_circle_centres(outer, [hole], prm)
    _inside_ok(cs, (0, 0, 100, 100), 3, 4, holes=[(40, 40, 60, 60)])
    cs2 = fill_circle_centres(outer, [hole], FillParams(fill_type=2, radius=3, stock=4, space=3))
    assert len(cs2) > 10
    _inside_ok(cs2, (0, 0, 100, 100), 3, 4, holes=[(40, 40, 60, 60)])


def test_linear_fill_spacing_and_margin():
    outer = rect(0, 0, 50, 20)
    parts = linear_fill(outer, [], FillParams(fill_type=3, space=2, stock=1, angle_deg=0))
    ys = sorted({round(p.contours[0].segments[0].start[1], 6) for p in parts})
    assert len(ys) == 9
    assert all(abs((b - a) - 2) < 1e-6 for a, b in zip(ys, ys[1:]))
    for p in parts:
        s = p.contours[0].segments[0]
        assert abs(abs(s.end[0] - s.start[0]) - 48) < 1e-6
    # S order: consecutive lines alternate direction
    d = [math.copysign(1, p.contours[0].segments[0].end[0] - p.contours[0].segments[0].start[0]) for p in parts]
    assert all(a != b for a, b in zip(d, d[1:]))
    rot = linear_fill(outer, [], FillParams(fill_type=3, space=2, stock=1, angle_deg=45))
    s = rot[len(rot) // 2].contours[0].segments[0]
    ang = math.degrees(math.atan2(s.end[1] - s.start[1], s.end[0] - s.start[0])) % 180
    assert abs(ang - 45) < 1e-6
