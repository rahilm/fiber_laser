"""Common-edge (shared-edge) cutting, "共边" (mf250), "C型共边" (mf250_1) and the rectangle
grid variant "矩形共边" (A250707_0..2).  Port of CADModule CShareEdge (vtable 0x101213a4)
and its drivers; see docs/cam.md section 12.

Evidence (CADModule.dll):

* **Driver** 0x100cabb0(this, c_type, rebuild_tree).  Collects the selected type-8 contours,
  marks pairs that share an edge (0x10047310), then either
  - normal (c_type == 0): 0x10049c40 splits every contour into its unshared pieces plus one
    copy of every shared piece, re-sorts with the cutting-order dispatcher 0x1003f010 mode 3
    (bottom to top) and replaces the originals (undo record COpShareEdgeCmd);
  - C-type (c_type != 0): contours are grouped by connectivity (0x10050ed0), groups ordered with
    0x1003e430(.., 3), then 0x10049730 removes from each contour the parts already cut by an
    earlier contour: each part keeps its own shared edges towards later parts and is cut as an
    open "C".
* **Pair test** 0x10047310 / 0x10044770: the kerf child contour (+0x110) is used when there is
  one; bboxes must overlap within tol = CShareEdge+8 = 0.05 (0x1011d6d0, set in 0x100cabb0).
  A contour containing a circle (glyph type 4) or a point (type 1) never shares.
  Line/line (0x10043d50): |d2 x d1|/|d1| <= 0.1 (0x1011f0b0) and below sin(eps)*|d2|; the
  end points are projected (0x10043390: endpoint coincidence within tol, otherwise on-segment
  with perpendicular distance <= tol) and the common interval (0x100434d0) must be >= 0.1 mm
  long on both edges.  Arc/arc (0x10043ef0), lwpoly and spline variants exist (only line/line
  and concentric arc/arc are ported).
* **Interval bookkeeping** 0x100494f0: for each pair (i < j) the interval on i is tagged 1 and
  the one on j is tagged 2.  0x10049c40 emits a shared piece only for tag-2 intervals (the
  later contour's copy), and the gaps between consecutive intervals as plain pieces; pieces
  shorter than 0.1 mm are dropped.
* **Rectangle grid** 0x100c36e0 -> 0x10047530 / 0x10047d30(type, overcut): all selected
  contours must be axis-aligned rectangles (0x10043220: 4 sides equal to the bbox within 0.01,
  closed) of the same size as the first one (within 0.02, 0x1011dae0), at least 0.09
  (0x1011f6a8) wide and high, filling a complete rows x cols grid (cell index from the centre).
  The originals are replaced by:
  - type 0 (横平竖直, INFERRED label): rows+1 horizontal and cols+1 vertical full lines;
  - type 1 (S型, INFERRED label): per cell a path left?(first cell only) -> top -> right ->
    bottom(row 0 only), the column direction alternating per row (serpentine);
  - type 2 (梯形, INFERRED label): one staircase polyline per anti-diagonal, alternate
    diagonals reversed.
  Overcut (A250707_1/2): when >= 0.1 (0x1011f0b0) the path start is moved 0.7*overcut
  (0x1011f298) along its first segment and the end is extended by the overcut along the last
  segment; the original also splits each path into a short head and a tail (purpose U), not
  ported.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Craft, Document, Line, Part, Point, Segment
from .geometry import contour_bbox, sub_path

__all__ = ["SharedEdge", "find_shared_edges", "share_edge", "share_edge_document",
           "RectGrid", "detect_rect_grid", "rect_share_edge", "cut_length"]

TOL = 0.05          # CShareEdge+8 (0x1011d6d0)
PARALLEL = 0.1      # 0x1011f0b0
MIN_LEN = 0.1       # 0x1011f0b0 (0x100434d0, 0x10049c40)


@dataclass
class SharedEdge:
    """Coincident stretch between contour ``i`` and contour ``j`` (i < j).

    ``on_i`` / ``on_j`` are arc-length intervals (mm, a < b) on each contour."""
    i: int
    j: int
    on_i: tuple[float, float]
    on_j: tuple[float, float]
    p0: Point
    p1: Point

    @property
    def length(self) -> float:
        return math.dist(self.p0, self.p1)


def _bbox_overlap(a, b, tol) -> bool:
    return a[0] <= b[2] + tol and a[1] <= b[3] + tol and b[0] - tol <= a[2] and b[1] - tol <= a[3]


def _proj(p, a, d, L):
    """Parameter (mm along a->b) and perpendicular distance of p."""
    vx, vy = p[0] - a[0], p[1] - a[1]
    return (vx * d[0] + vy * d[1]), abs(vx * d[1] - vy * d[0])


def _line_overlap(s: Line, t: Line, tol: float, min_len: float):
    """Common interval of two collinear lines as (u0, u1) on s and (v0, v1) on t, in mm."""
    L1, L2 = s.length(), t.length()
    if L1 < 1e-9 or L2 < 1e-9:
        return None
    d1 = ((s.end[0] - s.start[0]) / L1, (s.end[1] - s.start[1]) / L1)
    d2 = ((t.end[0] - t.start[0]) / L2, (t.end[1] - t.start[1]) / L2)
    if abs(d1[0] * d2[1] - d1[1] * d2[0]) * L2 > PARALLEL:
        return None
    u_a, e_a = _proj(t.start, s.start, d1, L1)
    u_b, e_b = _proj(t.end, s.start, d1, L1)
    if e_a > tol or e_b > tol:
        return None
    lo, hi = max(0.0, min(u_a, u_b)), min(L1, max(u_a, u_b))
    if hi - lo < min_len:
        return None
    # parameters of the overlap end points on t
    p_lo = (s.start[0] + d1[0] * lo, s.start[1] + d1[1] * lo)
    p_hi = (s.start[0] + d1[0] * hi, s.start[1] + d1[1] * hi)
    v_lo, _ = _proj(p_lo, t.start, d2, L2)
    v_hi, _ = _proj(p_hi, t.start, d2, L2)
    v0, v1 = max(0.0, min(v_lo, v_hi)), min(L2, max(v_lo, v_hi))
    if v1 - v0 < min_len:
        return None
    return (lo, hi), (v0, v1), p_lo, p_hi


def _arc_angles(a: Arc):
    a0 = math.atan2(a.start[1] - a.center[1], a.start[0] - a.center[0])
    return a0, a.sweep()


def _arc_overlap(s: Arc, t: Arc, tol: float, min_len: float):
    """Concentric equal-radius arcs: common angular range (INFERRED port of 0x10043ef0)."""
    if math.dist(s.center, t.center) > tol or abs(s.radius - t.radius) > tol:
        return None
    r = s.radius
    a0, sw = _arc_angles(s)
    b0, tw = _arc_angles(t)
    # express t's range relative to s's start in s's direction
    sg = 1 if sw > 0 else -1

    def rel(ang):
        x = (ang - a0) * sg
        return x % (2 * math.pi)
    tb0, tb1 = rel(b0), rel(b0 + tw)
    lo, hi = sorted((tb0, tb0 + tw * sg)) if abs(tw) < 2 * math.pi - 1e-9 else (0.0, 2 * math.pi)
    if lo < 0:
        lo, hi = lo + 2 * math.pi, hi + 2 * math.pi
    olo, ohi = max(0.0, lo), min(abs(sw), hi)
    if (ohi - olo) * r < min_len:
        return None
    pl = s.point_at(olo / abs(sw))
    ph = s.point_at(ohi / abs(sw))
    L2 = t.length()

    def on_t(p):
        ang = math.atan2(p[1] - t.center[1], p[0] - t.center[0])
        x = ((ang - b0) * (1 if tw > 0 else -1)) % (2 * math.pi)
        return min(x * r, L2)
    v = sorted((on_t(pl), on_t(ph)))
    return (olo * r, ohi * r), (v[0], v[1]), pl, ph


def _eligible(c: Contour) -> bool:
    """Contours with a full circle never share (glyph type 4 check in 0x10044770)."""
    return not any(isinstance(s, Arc) and math.dist(s.start, s.end) < 1e-9 for s in c.segments)


def find_shared_edges(contours: Sequence[Contour], tol: float = TOL,
                      min_len: float = MIN_LEN) -> list[SharedEdge]:
    """All coincident edge stretches between different contours (0x10047310 / 0x10044770)."""
    boxes = [contour_bbox(c) if c.segments else (0, 0, 0, 0) for c in contours]
    starts = []
    for c in contours:
        acc, st = 0.0, []
        for s in c.segments:
            st.append(acc)
            acc += s.length()
        starts.append(st)
    out: list[SharedEdge] = []
    for i in range(len(contours)):
        ci = contours[i]
        if not ci.segments or not _eligible(ci):
            continue
        for j in range(i + 1, len(contours)):
            cj = contours[j]
            if not cj.segments or not _eligible(cj) or not _bbox_overlap(boxes[i], boxes[j], tol):
                continue
            for a, sa in enumerate(ci.segments):
                for b, sb in enumerate(cj.segments):
                    if isinstance(sa, Line) and isinstance(sb, Line):
                        r = _line_overlap(sa, sb, tol, min_len)
                    elif isinstance(sa, Arc) and isinstance(sb, Arc):
                        r = _arc_overlap(sa, sb, tol, min_len)
                    else:
                        r = None
                    if r is None:
                        continue
                    (u0, u1), (v0, v1), p0, p1 = r
                    out.append(SharedEdge(i, j, (starts[i][a] + u0, starts[i][a] + u1),
                                          (starts[j][b] + v0, starts[j][b] + v1), p0, p1))
    return out


def _merge(iv: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    iv = sorted(iv)
    out: list[list] = []
    for a, b, tag in iv:
        if out and a <= out[-1][1] + 1e-6 and out[-1][2] == tag:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, tag])
    return [tuple(x) for x in out]


def _pieces(c: Contour, cut: list[tuple[float, float]], min_len: float) -> list[Contour]:
    """Open pieces of ``c`` outside the (sorted, disjoint) intervals ``cut``."""
    L = c.length()
    if not cut:
        return [c]
    gaps = []
    if c.closed:
        for k, (a, b) in enumerate(cut):
            na = cut[(k + 1) % len(cut)][0]
            if k + 1 == len(cut):
                na += L
            gaps.append((b, na))
    else:
        prev = 0.0
        for a, b in cut:
            gaps.append((prev, a))
            prev = b
        gaps.append((prev, L))
    out = []
    for a, b in gaps:
        if b - a >= min_len:
            segs = sub_path(c, a, b) if (b <= L + 1e-9 or c.closed) else sub_path(c, a, L)
            if segs:
                out.append(Contour(segs, False))
    return out


def _part_for(src: Part, contours: list[Contour], info: dict) -> Part:
    craft = Craft(layer=src.craft.layer, kerf_comp=-1)
    craft.lead_in.kind = 0
    craft.lead_out.kind = 0
    return Part(contours=contours, craft=craft, name=src.name, extra={"common_edge": info})


def cut_length(parts: Iterable[Part]) -> float:
    return sum(c.length() for p in parts for c in p.contours)


def share_edge(parts: Sequence[Part], c_type: bool = False, tol: float = TOL,
               min_len: float = MIN_LEN, sort: bool = True) -> list[Part]:
    """Rebuild ``parts`` so that every shared edge is cut once.

    Normal mode (c_type=False, 0x10049c40): every contour is split into its unshared open
    pieces (one Part per original contour, open contours) and every shared stretch becomes its
    own Part with one open contour (the copy of the later contour, tag 2).
    C-type (c_type=True, 0x10049730): each contour loses the stretches already cut by an
    earlier contour; the result keeps one Part per original part with open "C" contours.
    Parts without shared edges are returned unchanged.  New parts get no kerf and no leads
    (the original builds fresh CGlyContours), and keep the source layer.  With ``sort`` the
    result is ordered bottom to top by bbox centre (the original's 0x1003f010 mode 3);
    build_toolpath re-orders with its own sort mode afterwards.
    """
    flat: list[tuple[int, int, Contour]] = []
    for pi, p in enumerate(parts):
        for ci, c in enumerate(p.contours):
            flat.append((pi, ci, c))
    edges = find_shared_edges([c for _, _, c in flat], tol, min_len)
    if not edges:
        return list(parts)
    iv: dict[int, list] = {}
    for e in edges:
        iv.setdefault(e.i, []).append((e.on_i[0], e.on_i[1], 1))
        iv.setdefault(e.j, []).append((e.on_j[0], e.on_j[1], 2))
    touched_parts = {flat[k][0] for k in iv}
    out: list[Part] = [p for pi, p in enumerate(parts) if pi not in touched_parts]
    new: list[Part] = []
    per_part: dict[int, list[Contour]] = {}
    for k, (pi, ci, c) in enumerate(flat):
        if pi not in touched_parts:
            continue
        ints = _merge(iv.get(k, []))
        if c_type:
            cut = [(a, b) for a, b, t in ints if t == 2]
            per_part.setdefault(pi, []).extend(_pieces(c, _union(cut), min_len) if cut else [c])
        else:
            cut = [(a, b) for a, b, _ in ints]
            pcs = _pieces(c, _union(cut), min_len) if cut else [c]
            per_part.setdefault(pi, []).extend(pcs)
            for a, b, t in ints:
                if t == 2 and b - a >= min_len:
                    segs = sub_path(c, a, b)
                    if segs:
                        new.append(_part_for(parts[pi], [Contour(segs, False)],
                                             {"shared": True, "source": pi}))
    for pi, cs in per_part.items():
        if c_type:
            new.append(_part_for(parts[pi], cs, {"c_type": True, "source": pi}))
        else:
            for c in cs:
                new.append(_part_for(parts[pi], [c], {"shared": False, "source": pi}))
    if sort:
        def key(p):
            b = p.bbox()
            return ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2)
        new.sort(key=key)
    return out + new


def _union(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def share_edge_document(doc: Document, c_type: bool = False, selected_only: bool = False,
                        **kw) -> Document:
    """Copy of ``doc`` with common-edge cutting applied (to the selected parts if asked)."""
    d = copy.deepcopy(doc)
    if selected_only:
        sel = [p for p in d.parts if p.selected]
        rest = [p for p in d.parts if not p.selected]
        d.parts = rest + share_edge(sel, c_type, **kw)
    else:
        d.parts = share_edge(d.parts, c_type, **kw)
    return d


# ---------------------------------------------------------------- rectangle grid
@dataclass
class RectGrid:
    x0: float
    y0: float
    w: float
    h: float
    rows: int
    cols: int
    members: list[int] = field(default_factory=list)

    def point(self, r: int, c: int) -> Point:
        return (self.x0 + c * self.w, self.y0 + r * self.h)


def _is_rect(c: Contour, tol: float = 0.01) -> Optional[tuple[float, float, float, float]]:
    """Axis-aligned rectangle test (0x10043220): 4 lines, closed, sides = bbox within 0.01."""
    if not c.closed or len(c.segments) != 4 or not all(isinstance(s, Line) for s in c.segments):
        return None
    x0, y0, x1, y1 = contour_bbox(c)
    w, h = x1 - x0, y1 - y0
    L = [s.length() for s in c.segments]
    if (abs(L[0] - L[2]) < tol and abs(L[1] - L[3]) < tol and
            ((abs(L[0] - w) < tol and abs(L[1] - h) < tol) or (abs(L[0] - h) < tol and abs(L[1] - w) < tol))):
        return (x0, y0, x1, y1)
    return None


def detect_rect_grid(parts: Sequence[Part], size_tol: float = 0.02,
                     min_size: float = 0.09) -> Optional[RectGrid]:
    """Equal rectangles filling a complete grid (0x10047530), else None."""
    rects = []
    for k, p in enumerate(parts):
        for c in p.contours:
            r = _is_rect(c)
            if r and r[2] - r[0] >= min_size and r[3] - r[1] >= min_size:
                rects.append((k, r))
    if len(rects) < 2:
        return None
    w = rects[0][1][2] - rects[0][1][0]
    h = rects[0][1][3] - rects[0][1][1]
    if any(abs((r[2] - r[0]) - w) >= size_tol or abs((r[3] - r[1]) - h) >= size_tol for _, r in rects):
        return None
    cx0 = min((r[0] + r[2]) / 2 for _, r in rects)
    cy0 = min((r[1] + r[3]) / 2 for _, r in rects)
    cells = {}
    for k, r in rects:
        c = round(((r[0] + r[2]) / 2 - cx0) / w)
        rr = round(((r[1] + r[3]) / 2 - cy0) / h)
        if (rr, c) in cells:
            return None
        cells[(rr, c)] = k
    rows = max(rc[0] for rc in cells) + 1
    cols = max(rc[1] for rc in cells) + 1
    if len(cells) != rows * cols:
        return None
    return RectGrid(cx0 - w / 2, cy0 - h / 2, w, h, rows, cols, [cells[k] for k in sorted(cells)])


def _poly(pts: list[Point]) -> Contour:
    return Contour([Line(a, b) for a, b in zip(pts, pts[1:]) if math.dist(a, b) > 1e-12], False)


def _overcut(pts: list[Point], ov: float, trim_start: bool) -> list[Point]:
    pts = list(pts)
    if len(pts) < 2:
        return pts
    if trim_start:
        a, b = pts[0], pts[1]
        L = math.dist(a, b)
        k = min(0.7 * ov, L - 1e-6) / L
        pts[0] = (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
    a, b = pts[-2], pts[-1]
    L = math.dist(a, b)
    pts[-1] = (b[0] + (b[0] - a[0]) / L * ov, b[1] + (b[1] - a[1]) / L * ov)
    return pts


def rect_share_edge(grid: RectGrid, kind: int = 0, overcut: float = 0.0) -> list[Contour]:
    """Cut paths for a rectangle grid (0x10047d30).  Every grid edge is cut exactly once."""
    P = grid.point
    R, C = grid.rows, grid.cols
    paths: list[list[Point]] = []
    if kind == 0:
        for r in range(R + 1):
            paths.append([P(r, 0), P(r, C)])
        for c in range(C + 1):
            paths.append([P(0, c), P(R, c)])
        overcut = 0.0          # type 0 ignores the overcut argument
    elif kind == 1:
        for r in range(R):
            rev = r % 2 == 1
            order = range(C - 1, -1, -1) if rev else range(C)
            for n, c in enumerate(order):
                pts = []
                if not rev:
                    if n == 0:
                        pts.append(P(r, c))
                    pts += [P(r + 1, c), P(r + 1, c + 1), P(r, c + 1)]
                    if r == 0:
                        pts.append(P(r, c))
                else:
                    if n == 0:
                        pts.append(P(r, c + 1))
                    pts += [P(r + 1, c + 1), P(r + 1, c), P(r, c)]
                    if r == 0:
                        pts.append(P(r, c + 1))
                paths.append(pts)
    elif kind == 2:
        for k in range(R + C - 1):
            cmax = min(k, C - 1)
            pts: list[Point] = []
            c, r = cmax, k - cmax
            while c >= 0 and r < R:
                if r == 0:
                    pts.append(P(0, c))
                if r == k - cmax:
                    pts.append(P(r, c + 1))
                pts += [P(r + 1, c + 1), P(r + 1, c)]
                if c == 0:
                    pts.append(P(r, 0))
                c -= 1
                r += 1
            # remove consecutive duplicates
            clean = [pts[0]]
            for q in pts[1:]:
                if math.dist(q, clean[-1]) > 1e-9:
                    clean.append(q)
            if k % 2 == 1:
                clean.reverse()
            paths.append(clean)
    else:
        raise ValueError(kind)
    out = []
    for n, pts in enumerate(paths):
        if overcut >= 0.1:
            pts = _overcut(pts, overcut, trim_start=n > 0)
        out.append(_poly(pts))
    return out
