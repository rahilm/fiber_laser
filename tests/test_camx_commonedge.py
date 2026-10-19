"""Geometric checks for mlaser.cam.commonedge (CShareEdge; docs/re/cam.md section 12)."""
import math

from mlaser.cam import commonedge as CE
from mlaser.cam.toolpath import build_toolpath
from mlaser.toolpath.items import CutLine
from mlaser.model import Contour, Document, Line, Part


def rect(x, y, w, h, ccw=True):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if not ccw:
        pts = pts[::-1]
    return Contour([Line(a, b) for a, b in zip(pts, pts[1:] + pts[:1])], True)


def edges_of(parts):
    """Multiset of unit-length grid edges covered by the cut (rounded)."""
    out = {}
    for p in parts:
        for c in p.contours:
            for s in c.segments:
                L = s.length()
                n = max(1, round(L))
                for k in range(n):
                    a = (s.start[0] + (s.end[0] - s.start[0]) * (k + 0.5) / n,
                         s.start[1] + (s.end[1] - s.start[1]) * (k + 0.5) / n)
                    key = (round(a[0], 3), round(a[1], 3))
                    out[key] = out.get(key, 0) + 1
    return out


def test_find_shared_edge_two_squares():
    a, b = rect(0, 0, 10, 10), rect(10, 0, 10, 10)
    es = CE.find_shared_edges([a, b])
    assert len(es) == 1
    e = es[0]
    assert abs(e.length - 10) < 1e-9
    assert {round(e.p0[0], 6), round(e.p1[0], 6)} == {10.0}
    # tolerance: a 0.03 gap still shares, 0.2 does not
    assert len(CE.find_shared_edges([a, rect(10.03, 0, 10, 10)])) == 1
    assert CE.find_shared_edges([a, rect(10.2, 0, 10, 10)]) == []


def test_partial_overlap_and_min_len():
    a, b = rect(0, 0, 10, 10), rect(10, 5, 10, 10)
    es = CE.find_shared_edges([a, b])
    assert len(es) == 1 and abs(es[0].length - 5) < 1e-9
    c = rect(10, 9.95, 10, 10)            # overlap 0.05 < 0.1 minimum
    assert CE.find_shared_edges([a, c]) == []


def test_share_edge_cut_once_normal():
    parts = [Part([rect(0, 0, 10, 10)]), Part([rect(10, 0, 10, 10)])]
    before = CE.cut_length(parts)
    out = CE.share_edge(parts)
    assert abs(CE.cut_length(out) - (before - 10)) < 1e-6
    cov = edges_of(out)
    assert all(v == 1 for v in cov.values())
    assert len(cov) == 70                  # 2x1 grid of 10 mm cells, every unit edge once
    shared = [p for p in out if p.extra["common_edge"].get("shared")]
    assert len(shared) == 1 and abs(shared[0].contours[0].length() - 10) < 1e-9
    assert all(not c.closed for p in out for c in p.contours)


def test_share_edge_c_type():
    parts = [Part([rect(0, 0, 10, 10)]), Part([rect(10, 0, 10, 10)]), Part([rect(20, 0, 10, 10)])]
    out = CE.share_edge(parts, c_type=True)
    assert len(out) == 3
    assert abs(CE.cut_length(out) - (120 - 20)) < 1e-6
    assert all(v == 1 for v in edges_of(out).values())
    # first part keeps its full closed outline (it cuts the shared edge), others become C's
    first = [p for p in out if p.extra["common_edge"]["source"] == 0][0]
    assert first.contours[0].closed and abs(first.contours[0].length() - 40) < 1e-9
    later = [p for p in out if p.extra["common_edge"]["source"] == 1][0]
    assert not later.contours[0].closed and abs(later.contours[0].length() - 30) < 1e-9


def test_unrelated_parts_unchanged_and_toolpath():
    far = Part([rect(100, 100, 5, 5)])
    parts = [Part([rect(0, 0, 10, 10)]), Part([rect(0, 10, 10, 10, ccw=False)]), far]
    out = CE.share_edge(parts)
    assert out[0] is far
    doc = Document(parts=out)
    items = build_toolpath(doc)
    cut = sum(math.dist(i.start, i.end) for i in items if isinstance(i, CutLine))
    assert abs(cut - (20 + 70)) < 1e-6


def test_rect_grid_detect():
    parts = [Part([rect(c * 10, r * 5, 10, 5)]) for r in range(3) for c in range(4)]
    g = CE.detect_rect_grid(parts)
    assert g and (g.rows, g.cols) == (3, 4) and abs(g.w - 10) < 1e-9 and abs(g.h - 5) < 1e-9
    assert CE.detect_rect_grid(parts[:-1]) is None          # incomplete grid
    odd = parts[:-1] + [Part([rect(30, 10, 10, 6)])]
    assert CE.detect_rect_grid(odd) is None                 # different size


def _grid_edge_check(paths, g):
    cov = edges_of([Part(paths)])
    # number of unit edges in a rows x cols grid of w x h cells
    n = (g.rows + 1) * g.cols * round(g.w) + (g.cols + 1) * g.rows * round(g.h)
    assert len(cov) == n
    assert all(v == 1 for v in cov.values())


def test_rect_share_edge_kinds():
    parts = [Part([rect(c * 4, r * 3, 4, 3)]) for r in range(3) for c in range(4)]
    g = CE.detect_rect_grid(parts)
    total = (g.rows + 1) * g.cols * g.w + (g.cols + 1) * g.rows * g.h
    for kind in (0, 1, 2):
        paths = CE.rect_share_edge(g, kind)
        assert abs(sum(p.length() for p in paths) - total) < 1e-6, kind
        _grid_edge_check(paths, g)
    assert len(CE.rect_share_edge(g, 0)) == (g.rows + 1) + (g.cols + 1)
    assert len(CE.rect_share_edge(g, 1)) == g.rows * g.cols
    assert len(CE.rect_share_edge(g, 2)) == g.rows + g.cols - 1


def test_rect_share_edge_overcut():
    parts = [Part([rect(c * 4, r * 3, 4, 3)]) for r in range(2) for c in range(2)]
    g = CE.detect_rect_grid(parts)
    base = CE.rect_share_edge(g, 1)
    ov = CE.rect_share_edge(g, 1, overcut=0.5)
    # first path: only extended by 0.5; others: extended 0.5 and start trimmed 0.35
    assert abs(ov[0].length() - (base[0].length() + 0.5)) < 1e-9
    assert abs(ov[1].length() - (base[1].length() + 0.5 - 0.35)) < 1e-9
    assert CE.rect_share_edge(g, 1, overcut=0.05)[1].length() == base[1].length()
