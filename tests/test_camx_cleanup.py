"""Import cleanup (mlaser.cam.cleanup), see docs/cam.md §15."""
import math

from mlaser.cam.cleanup import (CleanupParams, FitParams, cleanup_contours, cleanup_document,
                                close_gaps, fit_arcs, merge_connected, remove_duplicate_contours,
                                remove_overlaps, remove_small, smooth_contour)
from mlaser.model import Arc, Contour, Document, Line, Part


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def circle(cx, cy, r):
    p = (cx + r, cy)
    return Contour([Arc(p, p, (cx, cy), True)], True)


def test_remove_small_uses_bbox_extent():
    cs = [rect(0, 0, 0.05, 0.05), rect(0, 0, 0.05, 5), circle(0, 0, 0.02)]
    out = remove_small(cs, 0.1)
    assert len(out) == 1 and out[0] is cs[1]


def test_remove_duplicate_contours():
    a = rect(0, 0, 10, 10)
    b = rect(0.03, 0.0, 10, 10)                 # within gate
    c = rect(20, 0, 10, 10)
    part_edge = Contour([Line((2, 0), (8, 0))], False)   # lies on a's bottom edge
    out = remove_duplicate_contours([a, b, c, part_edge], gate=0.1)
    assert len(out) == 2
    assert c in out


def test_remove_overlaps_lines_and_arcs():
    segs = [Line((0, 0), (10, 0)), Line((5, 0), (15, 0)), Line((15, 0), (12, 0)),
            Line((0, 1), (3, 1)), Line((20, 0), (25, 0))]
    out = remove_overlaps(segs, 0.01)
    lens = sorted(round(s.length(), 6) for s in out)
    assert lens == [3, 5, 15]
    c = (0, 0)
    arcs = [Arc((1, 0), (0, 1), c, True), Arc((math.cos(1), math.sin(1)), (-1, 0), c, True)]
    out = remove_overlaps(arcs, 0.01)
    assert len(out) == 1 and abs(out[0].length() - math.pi) < 1e-6
    # wrap-around merge
    arcs = [Arc((0, -1), (0, 1), c, True), Arc((0, 1), (0, -1), c, True)]
    out = remove_overlaps(arcs, 0.01)
    assert len(out) == 1 and abs(out[0].length() - 2 * math.pi) < 1e-6


def test_merge_connected_square_tolerance_and_closing():
    segs = [Line((0, 0), (10, 0)), Line((10.05, 0.05), (10, 10)), Line((0, 10), (10, 10)),
            Line((0.04, 10), (0, 0.04))]
    out = merge_connected(segs, 0.06, 1)
    assert len(out) == 1 and out[0].closed
    c = out[0]
    for a, b in zip(c.segments, c.segments[1:] + c.segments[:1]):
        assert a.end == b.start                 # snapped exactly
    # outside the square tolerance: not joined
    out = merge_connected([Line((0, 0), (1, 0)), Line((1.07, 0), (2, 0))], 0.06, 1)
    assert len(out) == 2


def test_merge_direction_first_prefers_straight_continuation():
    base = Line((0, 0), (10, 0))
    straight = Line((10, 0), (20, 0))
    turn = Line((10, 0), (10, 10))
    out = merge_connected([base, turn, straight], 0.01, 1)
    first = [c for c in out if c.segments[0].start == (0, 0)][0]
    assert first.segments[1].end == (20, 0)
    out = merge_connected([base, Line((10, 0), (10, 1)), Line((10, 0), (30, 0))], 0.01, 2)
    first = [c for c in out if c.segments[0].start == (0, 0)][0]
    assert first.segments[1].end == (10, 1)     # shortest first


def test_close_gaps():
    c = Contour([Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10), (0, 0.3))], False)
    out = close_gaps([c], 0.5)
    assert out[0].closed and out[0].segments[-1].end == (0, 0)
    assert not close_gaps([c], 0.1)[0].closed


def _poly_circle(cx, cy, r, n, a0=0.0, sweep=2 * math.pi):
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def _max_dev(segs, pts):
    from mlaser.cam.cleanup import _seg_dist
    return max(min(_seg_dist(p, s) for s in segs) for p in pts)


def test_fit_arcs_full_circle():
    pts = _poly_circle(5, 5, 10, 72)
    segs = fit_arcs(pts, FitParams(err=0.05), closed=True)
    assert len(segs) == 1 and isinstance(segs[0], Arc)
    assert abs(segs[0].radius - 10) < 0.05 and math.dist(segs[0].center, (5, 5)) < 0.05


def test_fit_arcs_arc_and_corner():
    arc_pts = _poly_circle(0, 0, 20, 30, 0, math.pi / 2)   # quarter circle polyline
    line_pts = [(0, 20 + k) for k in range(1, 11)]          # then a straight tail (corner? no: tangent)
    pts = arc_pts + [(-k, 20) for k in range(1, 11)]
    segs = fit_arcs(pts, FitParams(err=0.05))
    assert len(segs) < 8
    assert segs[0].start == pts[0] and math.dist(segs[-1].end, pts[-1]) < 1e-9
    for a, b in zip(segs, segs[1:]):
        assert math.dist(a.end, b.start) < 1e-9
    assert _max_dev(segs, pts) <= 0.05 + 1e-9
    # sharp corner: a square polyline stays 4 lines
    sq = [(0, 0), (5, 0), (10, 0), (10, 5), (10, 10), (0, 10), (0, 0)]
    segs = fit_arcs(sq, FitParams(err=0.02))
    assert all(isinstance(s, Line) for s in segs) and len(segs) == 4


def test_smooth_contour_keeps_shape():
    pts = _poly_circle(0, 0, 15, 40, 0, math.pi)
    segs = [Line(pts[k], pts[k + 1]) for k in range(len(pts) - 1)] + [Line(pts[-1], pts[0])]
    c = Contour(segs, True)
    s = smooth_contour(c, 0.05)
    assert s.closed and len(s.segments) < len(segs)
    assert any(isinstance(x, Arc) for x in s.segments)
    assert _max_dev(s.segments, pts) <= 0.05 + 1e-9
    assert math.dist(s.segments[-1].end, s.segments[0].start) < 1e-9


def test_cleanup_pipeline_document():
    # exploded square with a duplicate edge, a gap of 0.05 and a speck
    segs = [Line((0, 0), (10, 0)), Line((10, 0), (0, 0)), Line((10, 0.05), (10, 10)),
            Line((10, 10), (0, 10)), Line((0, 10), (0, 0))]
    doc = Document(parts=[Part(contours=[Contour([s], False)]) for s in segs] +
                   [Part(contours=[rect(50, 50, 0.05, 0.05)]), Part(contours=[rect(20, 0, 5, 5)]),
                    Part(contours=[rect(20, 0, 5, 5)])])
    res = cleanup_document(doc, CleanupParams(connect_gate=0.1))
    assert res["after"] == 2
    closed = sorted(round(p.contours[0].length(), 3) for p in doc.parts)
    assert closed == [20.0, 40.0] or closed == [20.0, 40.05]
    assert all(p.contours[0].closed for p in doc.parts)
