"""Fly cut (mlaser.cam.flycut): geometric checks of the scan paths and laser switching."""
import math
from pathlib import Path

import pytest
from vendor_samples import requires, vendor_file

from mlaser.cam import flycut as F
from mlaser.toolpath.items import CutArc, CutLine, LaserOff, LaserOn, Pierce, Rapid
from mlaser.model import Arc, Contour, Document, Line, Part

SAMPLE = Path(__file__).parent / "data" / "chf" / "sample_v9.chf"      # tools/make_test_chf.py


def circle(x, y, r):
    p = (x + r, y)
    return Contour([Arc(p, p, (x, y), True)], closed=True)


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[k], pts[(k + 1) % 4]) for k in range(4)], closed=True)


def continuous(c: Contour, tol=1e-6):
    return all(math.dist(a.end, b.start) <= tol for a, b in zip(c.segments, c.segments[1:]))


EXACT = F.CircleFlyParams(lag_deg=0, lag_max=0, overlap_deg=0, overlap_max=0)


# --------------------------------------------------------------------------- circles
def test_circle_grid_one_path_exact_coverage():
    cs = [circle(x, y, 3) for y in (0, 20) for x in (0, 10, 20, 30)]
    prm = F.CircleFlyParams(sort_type=3, lag_deg=0, lag_max=0, overlap_deg=0, overlap_max=0)
    paths, used = F.circle_fly(cs, prm)
    assert used == list(range(8)) and len(paths) == 1
    p = paths[0]
    assert continuous(p.contour) and not p.contour.closed and p.initial_on
    on = [s for s, flag in p.spans() if flag]
    off = [s for s, flag in p.spans() if not flag]
    # every laser-on span is a full circle of a source circle, each circle exactly once
    assert len(on) == 8 and all(isinstance(s, Arc) for s in on)
    centres = sorted((round(s.center[0], 6), round(s.center[1], 6)) for s in on)
    assert centres == sorted((float(x), float(y)) for y in (0, 20) for x in (0, 10, 20, 30))
    assert all(abs(s.length() - 2 * math.pi * 3) < 1e-6 for s in on)
    assert sum(b - a for a, b in p.on_intervals()) == pytest.approx(8 * 2 * math.pi * 3, abs=1e-6)
    # linkers are beam-off and stay outside every circle (they run between the holes)
    assert off
    for s in [s for s in off if isinstance(s, Arc)]:      # off arcs: overtravel on a cut circle
        assert any(math.dist(s.center, a.center) < 1e-9 and abs(s.radius - a.radius) < 1e-9 for a in on)
    for s in [s for s in off if isinstance(s, Line)]:
        m = ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
        assert all(math.dist(m, c.segments[0].center) >= 3 - 0.05 for c in cs)


def test_circle_tangent_entry_on_collinear_row():
    cs = [circle(x, 0, 2) for x in (0, 10, 20)]
    p = F.circle_fly(cs, EXACT)[0][0]
    arcs = [s for s in p.contour.segments if isinstance(s, Arc)]
    for a in arcs:   # start point on the right of travel (+x): tangent along +x (CCW)
        assert a.start[1] == pytest.approx(-2.0) and a.start[0] == pytest.approx(a.center[0])
        assert a.ccw
    # linker leaves and enters tangentially
    segs = p.contour.segments
    i = segs.index(arcs[0])
    first_link = segs[i + 1]
    d = (first_link.end[0] - first_link.start[0], first_link.end[1] - first_link.start[1])
    assert d[0] > 0 and abs(d[1]) < 0.3 * abs(d[0])


def test_circle_observed_switch_offsets():
    r = 5.0
    cs = [circle(x, 0, r) for x in (0, 20, 40)]
    prm = F.CircleFlyParams()
    p = F.circle_fly(cs, prm)[0][0]
    L = p.length()
    # arc ranges along the path
    acc, ranges = 0.0, []
    for s in p.contour.segments:
        if isinstance(s, Arc):
            ranges.append((acc, acc + s.length()))
        acc += s.length()
    a = min(6 * math.pi / 180 * r, 1.2)
    b = min(10 * math.pi / 180 * r, 3.0)
    expect = [(ranges[0][1] + a + b) / L, (ranges[1][0] - a + b) / L, (ranges[1][1] + a + b) / L,
              (ranges[2][0] - a + b) / L]
    assert p.switches == pytest.approx(expect)


def test_circle_overlap_rejected_and_groups_split():
    cs = [circle(0, 0, 3), circle(4, 0, 3), circle(10, 0, 3), circle(200, 0, 3), circle(210, 0, 3)]
    paths, used = F.circle_fly(cs, F.CircleFlyParams(max_fly_len=80))
    assert 1 not in used                      # overlaps circle 0
    assert len(paths) == 2
    assert sorted(len(p.sources) for p in paths) == [2, 2]
    assert F.circle_fly([circle(0, 0, 1)])[0] == []          # needs >= 2 circles


def test_circle_sorting_band_mode():
    cs = [circle(x, y, 1) for x, y in ((30, 0), (0, 0), (10, 10), (20, 0))]
    p = F.circle_fly(cs, F.CircleFlyParams(sort_type=1))[0][0]
    xs = [s.center[0] for s in p.contour.segments if isinstance(s, Arc)]
    assert xs[0] == 0                         # left -> right band sort


def test_detect_circle_from_two_halves():
    c = Contour([Arc((1, 0), (-1, 0), (0, 0), True), Arc((-1, 0), (1, 0), (0, 0), True)])
    assert F.detect_circle(c) == pytest.approx((0, 0, 1))
    assert F.detect_circle(rect(0, 0, 1, 1)) is None
    assert F.detect_circle(Contour([Arc((1, 0), (0, 1), (0, 0), True)])) is None


# --------------------------------------------------------------------------- lines
def _on_segments(p):
    return [s for s, on in p.spans() if on]


def _point_on_edge(pt, edges, tol=1e-6):
    for e in edges:
        v = (e.end[0] - e.start[0], e.end[1] - e.start[1])
        L = math.hypot(*v)
        t = ((pt[0] - e.start[0]) * v[0] + (pt[1] - e.start[1]) * v[1]) / (L * L)
        q = (e.start[0] + v[0] * t, e.start[1] + v[1] * t)
        if -1e-9 <= t <= 1 + 1e-9 and math.dist(pt, q) <= tol:
            return True
    return False


def test_line_fly_rect_grid_covers_every_edge_once():
    rs = [rect(i * 15, j * 15, 10, 10) for i in range(3) for j in range(2)]
    paths, used = F.line_fly(rs)
    assert used == list(range(6))
    edges = [s for r in rs for s in r.segments]
    total_on = sum(b - a for p in paths for a, b in p.on_intervals())
    assert total_on == pytest.approx(6 * 40.0, abs=1e-6)
    for p in paths:
        assert continuous(p.contour)
        for s in _on_segments(p):            # laser-on only on part edges
            for u in (0.0, 0.25, 0.5, 0.75, 1.0):
                q = (s.start[0] + (s.end[0] - s.start[0]) * u, s.start[1] + (s.end[1] - s.start[1]) * u)
                assert _point_on_edge(q, edges)
        for s, on in p.spans():               # laser-off linkers never lie on an edge
            if not on and s.length() > 1e-3:
                m = ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
                assert not _point_on_edge(m, edges, 1e-3)
    # every edge midpoint is cut
    on = [s for p in paths for s in _on_segments(p)]
    for e in edges:
        m = ((e.start[0] + e.end[0]) / 2, (e.start[1] + e.end[1]) / 2)
        assert _point_on_edge(m, on)


def test_line_fly_collinear_rows_are_single_passes():
    rs = [rect(i * 15, 0, 10, 10) for i in range(4)]
    p = F.line_fly(rs)[0][0]
    # the bottom edges y=0 are flown in one pass with straight laser-off gap linkers
    segs = p.spans()
    first_row = []
    for s, on in segs:
        if abs(s.start[1]) < 1e-9 and abs(s.end[1]) < 1e-9:
            first_row.append((s, on))
        elif first_row:
            break
    assert [on for _, on in first_row] == [True, False, True, False, True, False, True]
    assert all(isinstance(s, Line) for s, _ in first_row)


def test_line_fly_start_corner():
    rs = [rect(i * 15, j * 15, 10, 10) for i in range(2) for j in range(2)]
    for corner, pt in ((0, (0, 0)), (1, (25, 0)), (2, (0, 25)), (3, (25, 25))):
        p = F.line_fly(rs, F.LineFlyParams(start_corner=corner))[0][0]
        assert p.contour.segments[0].start == pytest.approx(pt)


def test_line_fly_shared_edge_cut_once():
    rs = [rect(0, 0, 10, 10), rect(10, 0, 10, 10)]          # common edge x = 10
    paths, _ = F.line_fly(rs)
    total_on = sum(b - a for p in paths for a, b in p.on_intervals())
    assert total_on == pytest.approx(70.0, abs=1e-6)          # 80 - shared 10


def test_line_fly_rejects_arcs():
    paths, used = F.line_fly([circle(0, 0, 5), rect(20, 0, 10, 10)])
    assert used == [1]


def test_uturn_linker_is_smooth_and_off():
    rs = [rect(0, 0, 10, 2)]                                   # two horizontal edges 2 mm apart
    p = F.line_fly(rs, F.LineFlyParams(max_link_len=80))[0][0]
    assert continuous(p.contour)
    assert all(not on for s, on in p.spans() if isinstance(s, Line) and s.length() < 0.6)


# --------------------------------------------------------------------------- switches / items
def test_split_and_switch_roundtrip():
    c = Contour([Line((0, 0), (10, 0)), Line((10, 0), (20, 0))], closed=False)
    sp = F.split_by_switches(c, True, [0.25, 0.75])
    assert [(round(s.length(), 6), on) for s, on in sp] == [(5, True), (5, False), (5, False), (5, True)]
    init, sw = F.switches_from_spans(sp)
    assert init and sw == pytest.approx([0.25, 0.75])
    p = F.ScanPath(c, [0.25, 0.75], True)
    q = F.shift_switches(p, on_shift=-1.0, off_shift=2.0)
    assert q.switches == pytest.approx([0.35, 0.70])


def test_scan_motion_items_sequence():
    p = F.circle_fly([circle(0, 0, 3), circle(10, 0, 3)], EXACT)[0][0]
    items = F.scan_motion_items(p, feed=50.0, layer=2, pos=(0, 0), pierce_time_s=0.2)
    kinds = [type(i).__name__ for i in items[:6]]
    assert kinds == ["Marker", "Rapid", "LayerChange", "Follow", "Pierce", "LaserOn"]
    assert isinstance(items[-2], LaserOff)
    moves = [i for i in items if isinstance(i, (CutLine, CutArc))]
    assert all(math.dist(a.end, b.start) < 1e-6 for a, b in zip(moves, moves[1:]))
    assert sum(1 for m in moves if isinstance(m, CutArc) and m.laser) == 2
    assert all(not m.laser for m in moves if isinstance(m, CutLine))
    from mlaser.motion import interp
    est = interp.estimate_time(items)
    assert est and all(v >= 0 for v in est.values() if isinstance(v, (int, float)))


def test_apply_fly_cut_and_chf_roundtrip(tmp_path):
    from mlaser.formats import chf
    doc = Document(parts=[Part(contours=[circle(x, 0, 2)]) for x in (0, 10, 20)]
                   + [Part(contours=[rect(0, 20, 5, 5)])])
    for p in doc.parts:
        p.selected = True
    new = F.apply_fly_cut(doc, F.SCAN_CIRCLE, EXACT)
    assert new is not None and len(doc.parts) == 2 and doc.parts[0] is new
    assert len(new.contours) == 3 and new.extra["chf_type"] == 11
    path = tmp_path / "scan.chf"
    chf.save_chf(doc, path)
    doc2 = chf.load_chf(path)
    sp = F.scan_paths_of_part(doc2.parts[0])
    assert len(sp) == 1 and sp[0].kind == F.SCAN_CIRCLE and sp[0].initial_on
    assert sum(b - a for a, b in sp[0].on_intervals()) == pytest.approx(3 * 2 * math.pi * 2, abs=1e-3)


def test_sample_engraving_scan():
    from mlaser.formats import chf
    doc = chf.load_chf(SAMPLE)
    part = doc.parts[0]
    sp = F.scan_paths_of_part(part)
    assert len(sp) == 1 and sp[0].kind == F.SCAN_ENGRAVE and not sp[0].initial_on
    iv = sp[0].on_intervals()
    # beam on exactly on the elements, one interval per element (switch fractions have 6 decimals)
    assert len(iv) == len(part.contours)
    assert all(abs((b - a) - c.length()) < 0.01 for (a, b), c in zip(iv, part.contours))


@requires("File/autosave.chf")
def test_vendor_sample_engraving_scan_parity():
    from mlaser.formats import chf
    doc = chf.load_chf(vendor_file("File/autosave.chf"))
    part = doc.parts[0]
    sp = F.scan_paths_of_part(part)
    assert len(sp) == 1 and sp[0].kind == F.SCAN_ENGRAVE and not sp[0].initial_on
    on = sum(b - a for a, b in sp[0].on_intervals())
    src = sum(c.length() for c in part.contours)
    assert on == pytest.approx(src, abs=0.1)                 # beam on exactly on the 839 elements
    assert len(sp[0].on_intervals()) == len(part.contours) - 1 or \
        len(sp[0].on_intervals()) == len(part.contours) - 1 or \
        len(sp[0].on_intervals()) == len(part.contours)
