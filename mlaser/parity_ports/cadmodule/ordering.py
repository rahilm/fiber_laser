"""The original's cutting order and start point, statement by statement (moved from
``mlaser.cam.ordering``, Wave 1; docs/cam.md §8).

* mode 5 as the DXF import and the toolpath run it (FUN_100b57c0 -> dispatcher 0x1003f010):
  :func:`import_sort`, :func:`local_optimal_elems` (0x1003d300 cheapest insertion with the
  MSVC ``rand()``), :func:`containers_last_faithful` (0x1003c480);
* modes 6 / 7: :func:`ring_sort` (CRingSort 0x1003f690);
* the start point of a closed contour: :func:`choose_start` (0x10071f80) with the glyph walk.

The tree nodes (``mlaser.cam.ordering.Node``) are read by attribute only.  Module flags such as
``RING_TOL_FAITHFUL`` are read from this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from ...geom.contour import seg_tangent
from ...model import Arc, Contour, GlyphKind, Line, Point
from .._registry import port

Node = Any      # mlaser.cam.ordering.Node (contour, bbox, children, parent, entry, exit)

__port__ = {
    "source": "CADModule",
    "functions": {
        "0x1003f010": "sort dispatcher (modes 5, 6, 7 here)", "FUN_100b57c0": "import_sort (DXF import)",
        "0x1003e690": "import_sort (per level, children first)", "0x1003d300": "local_optimal_elems",
        "0x1003cd10": "_split_small", "0x1003c480": "containers_last_faithful",
        "0x1003c840": "import_sort (reverse flipped open contours)", "0x1003c7b0": "import_sort (post-order)",
        "FUN_1003d0b0": "import_sort ends()", "0x1003f690": "ring_sort (CRingSort)",
        "0x1003f4c0": "ring_sort (mode 6 reversed)", "0x10071f80": "choose_start / _long_side_start",
        "msvcrt rand": "MsvcRand",
    },
    "parity": ["dxf", "import_bytes", "lead", "jobcad"],
}

LOCAL_ACCEPT = 0.95      # new order kept only if (cycle - longest edge) < 0.95 * old length


# --------------------------------------------------------------------------- mode 5, statement-level port
# Used by the DXF import (FUN_100b57c0 runs the dispatcher with IGP.AutoSortType, default 5, and
# small_first = 1 right after chaining; docs/cam.md §1 and §8.4).  Everything below follows the
# CADModule code literally, including its oddities (cycle lengths measured start-to-start, the
# small-item closing edge from the FIRST route element's exit to the LAST one's start).
SORT_APPROX_EPS = 1e-8       # DAT_1011f528
SORT_SEG_EPS = 1e-5          # DAT_1011f530
SORT_SMALL_FRAC = 0.15       # DAT_1011f590
SORT_SMALL_AVG = 0.35        # DAT_1011f598
SORT_BIG = 999999999.0       # DAT_1011f5a8


@dataclass
class SortElem:
    """The 0x58-byte element record of the sorter (0x1003d0b0): +0 index, +8 bbox, +0x28 closed,
    +0x30 start, +0x40 end (open contours only), +0x50 reversed."""
    index: int
    bbox: tuple
    closed: bool
    start: Point
    end: Point = (0.0, 0.0)
    reversed: bool = False

    @property
    def exit(self) -> Point:
        return self.start if self.closed else self.end

    def copy(self) -> "SortElem":
        return SortElem(self.index, self.bbox, self.closed, self.start, self.end, self.reversed)


class MsvcRand:
    """msvcrt ``srand``/``rand`` (the shuffle of 0x1003d300 is seeded with ``time(NULL)``)."""

    def __init__(self, seed: int = 0):
        self.state = seed & 0xFFFFFFFF

    def rand(self) -> int:
        self.state = (self.state * 214013 + 2531011) & 0xFFFFFFFF
        return (self.state >> 16) & 0x7FFF


def approx_hypot(dx: float, dy: float) -> float:
    """0x1003b470 (also inlined in 0x1003d300): 0 when both |dx|, |dy| < 1e-8, else
    M + t M (1 + 0.5 t (t - 1)) with t = 0.5 (m/M)^2."""
    ax, ay = abs(dx), abs(dy)
    if ax < SORT_APPROX_EPS and ay < SORT_APPROX_EPS:
        return 0.0
    if ax <= ay:
        r, M = ax / ay, ay
    else:
        r, M = ay / ax, ax
    t = r * 0.5 * r
    return M + t * M * (t * 0.5 * (t - 1.0) + 1.0)


def point_seg_dist(p: Point, a: Point, b: Point) -> float:
    """0x1003b800(p, a, b): |b - p| for a segment shorter than 1e-5; projection measured from b
    along (b - a): beyond a -> |a - p|, before b -> |b - p|, else |cross| / L."""
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    X, Y = b[0] - p[0], b[1] - p[1]
    if L < SORT_SEG_EPS:
        return math.hypot(X, Y)
    proj = (Y * (b[1] - a[1]) + X * (b[0] - a[0])) / L
    if L < proj:
        return math.hypot(a[0] - p[0], a[1] - p[1])
    if proj < 0.0:
        return math.hypot(X, Y)
    return abs(Y * (b[0] - a[0]) - X * (b[1] - a[1])) / L


def _split_small(elems: list[SortElem]) -> tuple[list[SortElem], list[SortElem]]:
    """0x1003cd10: an item is small when its width is below max(0.15 group width, 0.35 average
    width) AND its height likewise.  Returns (small, large); large keeps all items when none is
    small."""
    n = len(elems)
    if n < 1:
        return [], elems
    aw = sum(e.bbox[2] - e.bbox[0] for e in elems) / n
    ah = sum(e.bbox[3] - e.bbox[1] for e in elems) / n
    gx0 = min(e.bbox[0] for e in elems); gy0 = min(e.bbox[1] for e in elems)
    gx1 = max(e.bbox[2] for e in elems); gy1 = max(e.bbox[3] for e in elems)
    tw = max((gx1 - gx0) * SORT_SMALL_FRAC, aw * SORT_SMALL_AVG)
    th = max((gy1 - gy0) * SORT_SMALL_FRAC, ah * SORT_SMALL_AVG)
    small, large = [], []
    for e in elems:
        if tw <= e.bbox[2] - e.bbox[0] or th <= e.bbox[3] - e.bbox[1]:
            large.append(e)
        else:
            small.append(e)
    if not small:
        return [], elems
    return small, large


def _cheapest_pos(route: list[SortElem], p: Point, best: float, floor: int) -> int:
    """The insertion scan of 0x1003d300: gaps route[k-1].exit -> route[k].start for
    k = len-1 .. 1, pre-filtered with the midpoint test, point-to-segment distance; the default
    position (closing gap) is the end.  Result clamped to ``floor``."""
    pos = len(route)
    for k in range(len(route) - 1, 0, -1):
        a, b = route[k - 1].exit, route[k].start
        mx, my = (b[0] + a[0]) * 0.5, (b[1] + a[1]) * 0.5
        if abs(p[0] - mx) - abs(b[0] - mx) < best and abs(p[1] - my) - abs(b[1] - my) < best:
            d = point_seg_dist(p, a, b)
            if d < best:
                best, pos = d, k
    return max(pos, floor)


@port("0x1003d300", source="CADModule", parity=("dxf", "import_bytes"))
def local_optimal_elems(elems: list[SortElem], small_first: bool, fix_first: bool = False,
                        seed: int = 0) -> list[SortElem]:
    """0x1003d300(elements, small_first, fix_first): statement-level port.

    1. fewer than 4 elements: unchanged;
    2. with ``small_first`` the small items (0x1003cd10) start the route: the first one, then
       each next by cheapest insertion, whose closing-edge distance is measured to the segment
       route[0].exit -> route[-1].start (as in the binary);
    3. large open contours: greedy nearest neighbour from the route end (approximate hypot,
       start or end, the end reverses the element);
    4. large closed contours, in a random order (each inserted at ``rand() % (len + 1)`` of the
       pick list, ``srand(seed)``), by cheapest insertion behind the small items;
    5. cycle C = |route[0].start - route[-1].start| + sum |route[i].start - route[i-1].start|
       (start to start); if an internal edge is the longest and there are no small items and
       fix_first is 0 the route is rotated to start after it;
    6. accepted only if C - longest < 0.95 * (the same start-to-start length of the incoming
       order without closing edge)."""
    n = len(elems)
    if n < 4:
        return [e.copy() for e in elems]
    work = [e.copy() for e in (elems[1:] if fix_first else elems)]
    small: list[SortElem] = []
    if small_first:
        small, work = _split_small(work)
    route: list[SortElem] = []
    floor = 0
    if fix_first:
        route.append(elems[0].copy())
        floor = 1
    if small:
        route.append(small[0].copy())
    for k in range(1, len(small)):
        p = small[k].start
        best = point_seg_dist(p, route[0].exit, route[-1].start)
        route.insert(_cheapest_pos(route, p, best, floor), small[k].copy())
    floor += len(small)
    rng = MsvcRand(seed)
    picks: list[SortElem] = []
    opens: list[SortElem] = []
    for e in work:
        if not e.closed:
            opens.append(e)
        else:
            picks.insert(rng.rand() % (len(picks) + 1), e)
    used = [False] * len(opens)
    start_i = 0
    if not route and opens:
        route.append(opens[0].copy())
        used[0] = True
        start_i = 1
    for _ in range(start_i, len(opens)):
        cur = route[-1].exit
        best, pick, rev = SORT_BIG, -1, False
        for j, e in enumerate(opens):
            if used[j]:
                continue
            d1 = approx_hypot(e.start[0] - cur[0], e.start[1] - cur[1])
            d2 = approx_hypot(e.exit[0] - cur[0], e.exit[1] - cur[1])
            r = d2 < d1
            if r:
                d1 = d2
            if d1 < best:
                best, pick, rev = d1, j, r
        if pick >= 0:
            e = opens[pick].copy()
            if rev and not e.closed:
                e.start, e.end = e.end, e.start
                e.reversed = not e.reversed
            route.append(e)
            used[pick] = True
    k0 = 0
    if not route and picks:
        route.append(picks[0].copy())
        k0 = 1
    for e in picks[k0:]:
        p = e.start
        best = point_seg_dist(p, route[-1].exit, route[0].start)
        route.insert(_cheapest_pos(route, p, best, floor), e.copy())

    def d(a: SortElem, b: SortElem) -> float:
        return math.hypot(b.start[0] - a.start[0], b.start[1] - a.start[1])

    longest = d(route[-1], route[0])
    total = longest
    old = 0.0
    kmax = -1
    for i in range(n - 1, 0, -1):
        e = d(route[i - 1], route[i])
        if longest < e:
            kmax, longest = i, e
        total += e
        old += d(elems[i - 1], elems[i])
    if kmax >= 0 and not fix_first and not small:
        route = route[kmax:] + route[:kmax]
    if total - longest < old * LOCAL_ACCEPT:
        return route
    return [e.copy() for e in elems]


@port("0x1003c480", source="CADModule", parity=("dxf", "import_bytes"))
def containers_last_faithful(seq: list) -> list:
    """0x1003c480 on one level, ``seq`` items with ``.bbox`` = (x0, y0, x1, y1): for
    c = n-1 .. 1, walk j = c-1 .. 0 and follow the chain of strict bbox containers of slot c
    (each found container becomes the item to contain); the last one found (lowest index) is
    moved to slot c, the items in between shift down."""
    seq = list(seq)
    for c in range(len(seq) - 1, 0, -1):
        cand = c
        for j in range(c - 1, -1, -1):
            if _strictly_contains(seq[j].bbox, seq[cand].bbox):
                cand = j
        if cand != c:
            seq.insert(c, seq.pop(cand))
    return seq


@port("FUN_100b57c0", "0x1003f010", "0x1003e690", source="CADModule", parity=("dxf", "import_bytes"))
def import_sort(nodes: list[Node], small_first: bool = True, seed: int = 0,
                ends=None) -> tuple[list[Node], set]:
    """The sort run by the DXF import (FUN_100b57c0 -> 0x1003f010 mode 5): per tree level,
    children first (0x1003e690), elements built from each contour (start = first point, end =
    last point for open contours, bbox), :func:`local_optimal_elems`, write-back of the new order
    and reversal of open contours whose element was flipped (0x1003c840); then
    :func:`containers_last_faithful` over the tree (0x1003c480) and the post-order flattening
    (0x1003c7b0).  ``nodes`` from :func:`build_tree` (children in document order, as
    FUN_10024480 keeps them).  Returns (order, ids of the nodes whose contour must be
    reversed).  Node bboxes must be the graph bboxes (+0x28).

    ``ends(node) -> (start, end)``: the element points (+0x30 / +0x40, vtable +0x88 / +0x8c of
    the graph, FUN_1003d0b0).  Default: the contour's first and last point, which is what a
    freshly imported contour returns.  For a contour with crafts FUN_100627a0 (+0x88) returns
    the kerf child's start, else the lead-in start (FUN_1002f630) when there is a lead, else the
    start point +0x188: the toolpath passes its prepared entry / exit points
    (:func:`toolpath_sort`)."""
    reversed_ids: set = set()
    rank = {id(n): i for i, n in enumerate(nodes)}
    roots = [n for n in nodes if n.parent is None]

    def elem(i: int, x: Node) -> SortElem:
        c = x.contour
        if ends is not None:
            s, e = ends(x)
        else:
            s = c.segments[0].start if c.segments else (0.0, 0.0)
            e = c.segments[-1].end if c.segments else s
        if id(x) in reversed_ids:
            s, e = e, s
        return SortElem(i, tuple(x.bbox), bool(c.closed), tuple(s), tuple(e) if not c.closed else (0.0, 0.0))

    def level(children: list[Node]) -> list[Node]:
        children = sorted(children, key=lambda x: rank.get(id(x), 0))   # document order
        for ch in children:
            ch.children = level(ch.children)
        if not children:
            return children
        es = [elem(i, ch) for i, ch in enumerate(children)]
        res = local_optimal_elems(es, small_first, False, seed)
        out = [children[e.index] for e in res]
        for e, x in zip(res, out):
            if e.reversed and not x.contour.closed:
                reversed_ids.symmetric_difference_update({id(x)})
        return out

    roots = level(roots)

    def last(children: list[Node]) -> list[Node]:
        for ch in children:
            ch.children = last(ch.children)
        return containers_last_faithful(children)

    roots = last(roots)
    out: list[Node] = []

    def flat(children: list[Node]):
        for ch in children:
            flat(ch.children)
            out.append(ch)

    flat(roots)
    return out, reversed_ids

def _strictly_contains(a: tuple, b: tuple) -> bool:
    return a[0] < b[0] and a[1] < b[1] and a[2] > b[2] and a[3] > b[3]

# --------------------------------------------------------------------------- modes 6 / 7
RING_TOL_FAITHFUL = False   # True: tolerance 0.1*W*H/n exactly as the binary (an area used as a length)


@port("0x1003f690", source="CADModule", status="observed")
def ring_sort(nodes: list[Node], inside_out: bool = False) -> list[Node]:
    """CRingSort 0x1003f690: gift-wrap spiral over bbox centres, starting just left of the
    group's lower-left corner and walking the hull counter-clockwise, inwards. Mode 7
    (outside -> inside) uses the spiral as is, mode 6 (inside -> outside) reversed
    (0x1003f4c0)."""
    if len(nodes) < 2:
        return list(nodes)
    P = [((x.bbox[0] + x.bbox[2]) / 2, (x.bbox[1] + x.bbox[3]) / 2) for x in nodes]
    x0 = min(x.bbox[0] for x in nodes); y0 = min(x.bbox[1] for x in nodes)
    x1 = max(x.bbox[2] for x in nodes); y1 = max(x.bbox[3] for x in nodes)
    n = len(nodes)
    if RING_TOL_FAITHFUL:
        tol = 0.1 * (x1 - x0) * (y1 - y0) / n
    else:   # I: dimensionally consistent version (0.1 * sqrt(area per item))
        tol = 0.1 * math.sqrt(max((x1 - x0) * (y1 - y0), 1e-12) / n)
    R = (x0 - 1.0, y0)
    idx = list(range(n))
    out: list[int] = []
    while idx:
        last = idx[-1]
        v = (P[last][0] - R[0], P[last][1] - R[1])
        pick = last
        if not (abs(v[0]) < tol and abs(v[1]) < tol):
            for ii in range(len(idx) - 2, -1, -1):
                i = idx[ii]
                w = (P[i][0] - R[0], P[i][1] - R[1])
                if abs(w[0]) < tol and abs(w[1]) < tol:
                    pick = i
                    break
                nw, nv = math.hypot(*w), math.hypot(*v)
                if nw < 1e-12 or nv < 1e-12:
                    continue
                sn = (v[0] * w[1] - v[1] * w[0]) / (nw * nv)
                upd = False
                if sn <= -0.1:
                    upd = True
                elif abs(sn) < 0.1 and nw < (1 - 4 * abs(sn)) * nv:
                    upd = True
                if upd:
                    v, pick = w, i
                    if abs(v[0]) < tol and abs(v[1]) < tol:
                        break
        out.append(pick)
        idx.remove(pick)
        R = P[pick]
    seq = [nodes[i] for i in out]
    return seq[::-1] if inside_out else seq

# ---------------------------------------------------------------------------
@port("0x10071f80", source="CADModule", parity=("lead", "jobcad"))
def choose_start(c: Contour, mode: int = 0, lead_len: float = 2.0, percent: float = 0.0) -> float:
    """Arc length of the start point (0x10071f80).  Open contours always start at 0."""
    if not c.closed or not c.segments or mode == 3:
        return 0.0
    if mode == 2:
        return (percent * 0.01 % 1.0) * c.length()
    if mode == 1:
        acc = 0.0
        n = len(c.segments)
        for i in range(n):
            s0, s1 = c.segments[i - 1], c.segments[i]
            t0, t1 = seg_tangent(s0, 1.0), seg_tangent(s1, 0.0)
            if abs(t0[0] * t1[1] - t0[1] * t1[0]) > 0.01745 or (t0[0] * t1[0] + t0[1] * t1[1]) < 0:
                return _walk_to_dist(c, acc)
            acc += s1.length()
        return 0.0
    # mode 0: long side first (requires lead length >= 0.1, DAT_1011f0b0)
    if lead_len < 0.1:
        return 0.0
    return _walk_to_dist(c, _long_side_start(c, lead_len))


def _walk_to_dist(c: Contour, walk: float) -> float:
    """FUN_10071f80 stores walk / +0x48 (the contour's stored length, ``geometry.stored_length``)
    as the start fraction; return that fraction as arc length of ``c`` (fraction * computed
    length), the convention of :func:`choose_start` callers (``start_param = d / length``)."""
    from ...geom.contour import stored_length
    Ls = stored_length(c)
    L = c.length()
    return walk / Ls * L if Ls > 0 else walk


# glyph kinds as in the .chf / CADModule glyph factory FUN_10062860
_GLY_SEGMENT, _GLY_ARC, _GLY_CIRCLE, _GLY_LWPOLY, _GLY_SPLINE = (
    GlyphKind.LINE, GlyphKind.ARC, GlyphKind.CIRCLE, GlyphKind.LWPOLY, GlyphKind.SPLINE)


def _source_glyph_groups(c: Contour, with_dir: bool = False) -> Optional[list[tuple]]:
    """Split ``c.segments`` by the contour's source glyphs (``Contour.source_glyphs()``, set by
    the DXF import and by ``load_chf``).  Each glyph is expanded with ``geom.curves.glyph_segments``
    and counted the way the importer builds the contour (``io.dxf_import._contour``): pieces of
    zero length are dropped (arcs and a lone point glyph are kept); the importer's end-point
    snapping moves end points but keeps the count.  None when the counts do not add up."""
    src = c.source_glyphs()
    if not src:
        return None
    from mlaser.geom.curves import glyph_segments
    from mlaser.model import GLYPH_POINT
    lone_point = len(src) == 1 and src[0].kind == GLYPH_POINT
    groups, i = [], 0
    for g in src:
        segs = glyph_segments(g)
        if not lone_point:
            segs = [s for s in segs if s.length() > 1e-12 or isinstance(s, Arc)]
        n = len(segs)
        groups.append((int(g.kind), c.segments[i:i + n], -1 if getattr(g, "dir", 1) == -1 else 1, g))
        i += n
    if i != len(c.segments) or not all(grp[1] for grp in groups):
        return None
    return groups if with_dir else [(k, segs) for k, segs, _d, _g in groups]


def contour_glyph_list(c: Contour) -> list[tuple[int, list, int, object]]:
    """:func:`contour_glyphs` as (kind, segments, dir, glyph): each glyph's traversal direction
    (``model.Glyph.dir``: 1, or -1 when the contour runs the glyph backwards) and the source
    glyph itself (None when the contour carries no source glyphs)."""
    groups = _source_glyph_groups(c, with_dir=True) if isinstance(c.extra, dict) else None
    if groups:
        return groups
    rec = c.extra.get("chf") if isinstance(c.extra, dict) else None
    glys = list(getattr(rec, "glys", None) or [])
    out = contour_glyphs(c)
    if len(glys) != len(out):
        glys = [None] * len(out)
    return [(k, segs, -1 if getattr(g, "dir", 1) == -1 else 1, g) for (k, segs), g in zip(out, glys)]


def contour_glyphs(c: Contour) -> list[tuple[int, list]]:
    """The contour as the original's glyph list: ``[(kind, segments), ...]`` in traversal
    order.  Preferred source: the contour's own source glyphs (``Contour.source_glyphs()``:
    the rebuild's DXF reader and ``load_chf`` attach them), split with
    ``geom.curves.glyph_segments`` (:func:`_source_glyph_groups`), so an LWPOLYLINE is ONE glyph of
    kind 6 whose pieces are its sub-segments, as in the original.  Then a .chf record
    (``extra['chf'].glys``).  Otherwise every segment is its own glyph: a line (2), an arc (3)
    or, for a closed full-circle arc, a circle (4)."""
    groups = _source_glyph_groups(c) if isinstance(c.extra, dict) else None
    if groups:
        return groups
    rec = c.extra.get("chf") if isinstance(c.extra, dict) else None
    glys = getattr(rec, "glys", None)
    if glys:
        try:
            from .chf import gly_segments
            groups, i = [], 0
            for g in glys:
                n = len(gly_segments(g))
                groups.append((g.kind, c.segments[i:i + n]))
                i += n
            if i == len(c.segments) and all(seg for _k, seg in groups):
                return groups
        except Exception:      # noqa: BLE001 - foreign record: fall back to per-segment glyphs
            pass
    out = []
    for s in c.segments:
        if isinstance(s, Line):
            out.append((_GLY_SEGMENT, [s]))
        elif math.dist(s.start, s.end) < 1e-9:
            out.append((_GLY_CIRCLE, [s]))
        else:
            out.append((_GLY_ARC, [s]))
    return out


# Equal lengths for the "first maximum" of FUN_10071f80 (strict '<').  The rebuild's lengths of
# geometrically equal pieces (the 24 teeth of gear.dxf, mirrored and rotated copies of the same
# 6-decimal vertices) differ in the last bits, the binary's x87 values do not order them the same
# way; within 1e-9 mm the FIRST one is kept, which is what the original picks (parity JC-11:
# gear #1 starts on tooth 1, 0.014795, not tooth 21).  I: the binary's own last-bit order.
LENGTH_TIE_EPS = 1e-9


def _long_side_start(c: Contour, lead_len: float) -> float:
    """Mode 0 of FUN_10071f80 ("自动选择起点位置(长边优先)"), O from the CADModule decomp:

    1. walk the glyphs; the first *line glyph* (kind 2) longer than 2*lead wins; otherwise
       the longest glyph (strictly longer replaces, so ties keep the first);
    2. inside the chosen glyph the start is its middle (0.5), a circle glyph its own start
       (0.0), and an lwpoly glyph (kind 6) the middle of its longest sub-segment (first one
       on ties, in traversal order; the reversed-glyph branch mirrors the search);
    3. result = length of the glyphs before + position inside the chosen glyph.

    Because a .chf/DXF polyline is one glyph, its 30 mm line does not count as a "line
    glyph": the closed LWPOLYLINE of basic_r2000.dxf starts in the middle of its 34.8 mm arc
    (start_param 0.374474, parity harness ``app_lead``)."""
    glys = contour_glyphs(c)
    if not glys:
        return 0.0
    lens = [sum(s.length() for s in segs) for _k, segs in glys]
    pick = None
    best = 0
    for i, (kind, _segs) in enumerate(glys):
        if 2 * lead_len < lens[i] and kind == _GLY_SEGMENT:
            pick = i
            break
        if lens[best] < lens[i] - LENGTH_TIE_EPS:
            best = i
    if pick is None:
        pick = best
    before = sum(lens[:pick])
    kind, segs = glys[pick]
    if kind == _GLY_CIRCLE:
        inside = 0.0
    elif kind == _GLY_LWPOLY:
        sl = [s.length() for s in segs]
        k = 0
        for j in range(1, len(sl)):
            if sl[k] < sl[j] - LENGTH_TIE_EPS:
                k = j
        inside = sum(sl[:k]) + sl[k] / 2
    else:
        inside = lens[pick] / 2
    return before + inside
