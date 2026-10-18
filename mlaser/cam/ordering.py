"""Cutting order, containment tree and start-point selection.

Evidence (CADModule.dll; details and tags in docs/cam.md section 8):

* Containment tree CContourTopTree (doc+0x9d20), depth assigned by 0x10022270
  (outermost = 1).
* Sort dispatcher 0x1003f010(mode, small_first), called by the ribbon handler
  (MainApp FUN_004b6190 -> CCADModule vtable +0xa0 -> 0x100c1850):
  1 left->right, 2 right->left (CSSort band sort 0x1003e8f0 -> 0x100429e0),
  3 bottom->top, 4 top->bottom (0x1003ef90, same band sort), 5 "local optimal"
  (0x1003e690 -> 0x1003d300 cheapest insertion, then 0x1003c480 bbox-container-last),
  6 inside->outside / 7 outside->inside (0x1003efd0 -> 0x1003ec40 -> CRingSort
  0x1003f690, a gift-wrap spiral; mode 6 is the reversed spiral). Mode 0 and any other
  value leave the order unchanged.  Direction is done by normalising coordinates
  (0x10040030): modes > 2 swap x/y, even modes negate x.
* Inner before outer: siblings are sorted within each tree level and the tree
  is flattened post-order (0x1003c7b0) -> children (holes and parts inside
  holes) are cut before their parent.
* Small first (0x1003cd10, pd466): an item is small if both its width and
  height are below max(0.15 * group extent, 0.35 * average item size) in that
  axis; small items are cut first.
* Band sort (0x10042420): band width = average item size (floor
  max(0.01, extent/2/n)); bands processed serpentine; a band is reversed if its
  last item is closer to the previous exit point (0x10040210; 0x1003fd80 only picks the
  direction of open contours, which the rebuild never reverses).
* Selected layer first/last (types 10/11) and the manual-sort tool (type 9) reorder the
  graph list itself (stable partition / move-to-slot); see :func:`set_layer_priority`,
  :func:`manual_pick` and :func:`set_manual_order`, which reorder ``Document.parts`` and are
  used with mode 0 (keep order).
* Start point (0x10071f80), closed contours only: mode 0 "long side first"
  (middle of the first line *glyph* longer than 2*lead length, else of the longest
  glyph; a polyline glyph starts in the middle of its longest piece, which can be an
  arc; circle: its start), mode 1 "vertex first" (first vertex with
  |cross(t_in, t_out)| > 0.01745), mode 2 percentage of length, 3 unchanged.

Statement-level port of mode 5 as the DXF import runs it (FUN_100b57c0, small first):
:func:`import_sort` / :func:`local_optimal_elems` / :func:`containers_last_faithful`
(docs/cam.md §1, §8.4); the ribbon sort (:func:`order_nodes` mode 5) keeps its documented
simplifications (lead entry/exit points, no open-contour reversal).

Rebuild extensions (not in the original, tagged in docs): mode 20 "shortest path"
(precedence-constrained nearest neighbour + 2-opt + or-opt on the real entry/exit
points), mode 21 "grid" (serpentine cells), and :func:`plan_rapids` which routes rapid
moves around holes that are already cut (the original has no rapid avoidance; it only has
the "short move without lift" / leap-frog options in the motion layer).
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from ..model import Arc, Contour, Line, Point  # noqa: F401
from ..geom.contour import contour_bbox, point_at, point_in_contour, polyline, seg_tangent  # noqa: F401
from ..parity_ports import dispatch

# sort modes (0x1003f010 numbering; 20+ are rebuild extensions)
MODE_KEEP = 0
MODE_LEFT_RIGHT, MODE_RIGHT_LEFT, MODE_BOTTOM_TOP, MODE_TOP_BOTTOM = 1, 2, 3, 4
MODE_LOCAL = 5
MODE_RING_IN = 6        # mf202 inside -> outside (reversed CRingSort spiral)
MODE_RING_OUT = 7       # mf203 outside -> inside (CRingSort spiral)
MODE_SHORTEST = 20      # rebuild: precedence-constrained NN + 2-opt + or-opt
MODE_GRID = 21          # rebuild: serpentine grid cells
# GRP.SortType (pd465) enum index -> mode = index + 1 (index 7 "small first" -> 8, ignored)
SORT_TYPE_ENUM = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7}


@dataclass
class Node:
    contour: Contour
    key: object = None                 # caller payload (e.g. (part_index, contour_index))
    depth: int = 1
    parent: Optional["Node"] = None
    children: list["Node"] = field(default_factory=list)
    bbox: tuple = (0, 0, 0, 0)
    area: float = 0.0
    entry: Point = (0.0, 0.0)          # where cutting of this contour starts (lead start)
    exit: Point = (0.0, 0.0)
    layer: Optional[int] = None        # process layer (by-layer modes); falls back to contour.extra['sort_layer']

    @property
    def size(self):
        return (self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1])


def build_tree(contours: list[Contour], keys: Optional[list] = None) -> list[Node]:
    """Return all nodes with parent/children/depth filled (open contours can be
    children but never parents)."""
    keys = keys if keys is not None else list(range(len(contours)))
    nodes = []
    for c, k in zip(contours, keys):
        bb = contour_bbox(c) if c.segments else (0, 0, 0, 0)
        n = Node(c, k, bbox=bb, area=(bb[2] - bb[0]) * (bb[3] - bb[1]))
        n.entry = c.segments[0].start if c.segments else (0, 0)
        n.exit = c.segments[-1].end if c.segments else (0, 0)
        nodes.append(n)
    polys = [polyline(n.contour) if n.contour.closed else None for n in nodes]
    order = sorted(range(len(nodes)), key=lambda i: nodes[i].area)
    for ii, i in enumerate(order):
        n = nodes[i]
        probe = _probe_point(n.contour)
        for j in order[ii + 1:]:
            m = nodes[j]
            if polys[j] is None or m.area <= n.area:
                continue
            b, mb = n.bbox, m.bbox
            if b[0] < mb[0] - 1e-9 or b[1] < mb[1] - 1e-9 or b[2] > mb[2] + 1e-9 or b[3] > mb[3] + 1e-9:
                continue
            if point_in_contour(probe, m.contour, polys[j]):
                n.parent = m
                m.children.append(n)
                break
    for n in nodes:
        d, p = 1, n.parent
        while p is not None:
            d += 1
            p = p.parent
        n.depth = d
    return nodes


def _probe_point(c: Contour) -> Point:
    s = c.segments[0]
    if isinstance(s, Line):
        return ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2)
    return s.point_at(0.5)


def group_nested(items: list[tuple[object, Contour]]):
    """Group (key, contour) pairs into parts: each odd-depth contour with its
    even-depth direct children (holes).  Returns [(key, [outer, holes...])]."""
    nodes = build_tree([c for _, c in items], [k for k, _ in items])
    out = []
    for n in nodes:
        if n.depth % 2 == 1:
            holes = [ch.contour for ch in n.children if ch.depth % 2 == 0]
            out.append((n.key, [n.contour] + holes))
    return out


# ---------------------------------------------------------------------------
def _norm(p: Point, mode: int) -> Point:
    x, y = p
    if mode > 2:
        x, y = y, x
    if mode % 2 == 0:
        x = -x
    return (x, y)


def _small_split(nodes: list[Node]):
    if len(nodes) < 2:
        return [], nodes
    gx0 = min(n.bbox[0] for n in nodes); gy0 = min(n.bbox[1] for n in nodes)
    gx1 = max(n.bbox[2] for n in nodes); gy1 = max(n.bbox[3] for n in nodes)
    aw = sum(n.size[0] for n in nodes) / len(nodes)
    ah = sum(n.size[1] for n in nodes) / len(nodes)
    tw = max(0.15 * (gx1 - gx0), 0.35 * aw)
    th = max(0.15 * (gy1 - gy0), 0.35 * ah)
    small = [n for n in nodes if n.size[0] < tw and n.size[1] < th]
    large = [n for n in nodes if not (n.size[0] < tw and n.size[1] < th)]
    return small, large


def _band_sort(nodes: list[Node], mode: int, pos: Point) -> list[Node]:
    if not nodes:
        return []
    cen = {id(n): _norm(((n.bbox[0] + n.bbox[2]) / 2, (n.bbox[1] + n.bbox[3]) / 2), mode) for n in nodes}
    sizes = [abs(_norm(n.size, mode)[0]) for n in nodes]
    us = [cen[id(n)][0] for n in nodes]
    extent = max(us) - min(us)
    bw = max(sum(sizes) / len(sizes), max(0.01, extent / 2 / len(nodes)))
    u0 = min(us)
    bands: dict[int, list[Node]] = {}
    for n in nodes:
        bands.setdefault(int((cen[id(n)][0] - u0) / bw), []).append(n)
    out: list[Node] = []
    cur = pos
    for b in sorted(bands):
        band = sorted(bands[b], key=lambda n: (round(cen[id(n)][1] / (0.1 * bw)), cen[id(n)][0]))
        if math.dist(cur, band[-1].entry) < math.dist(cur, band[0].entry):
            band.reverse()
        out += band
        cur = band[-1].exit
    return out


def _nearest(nodes: list[Node], pos: Point) -> list[Node]:
    rest = list(nodes)
    out = []
    cur = pos
    while rest:
        k = min(range(len(rest)), key=lambda i: math.dist(cur, rest[i].entry))
        n = rest.pop(k)
        out.append(n)
        cur = n.exit
    return out


def sort_level(nodes: list[Node], mode: int, pos: Point, small_first: bool) -> list[Node]:
    groups = list(_small_split(nodes)) if small_first else [[], nodes]
    out = []
    for g in groups:
        if not g:
            continue
        if mode in (1, 2, 3, 4):
            s = _band_sort(g, mode, pos)
        elif mode == MODE_KEEP:
            s = list(g)
        elif mode in (MODE_RING_IN, MODE_RING_OUT):
            s = ring_sort(g, inside_out=(mode == MODE_RING_IN))
        elif mode == MODE_GRID:
            s = _grid_sort(g, pos)
        elif mode == MODE_LOCAL:
            s = _local_optimal(g, pos)
        else:
            s = _nearest(g, pos)
        out += s
        pos = s[-1].exit
    if mode == MODE_LOCAL:
        out = _containers_last(out)
    return out


def order_nodes(nodes: list[Node], mode: int = 5, small_first: bool = False,
                start: Point = (0.0, 0.0)) -> list[Node]:
    """Inner-before-outer cutting order (post-order over sorted levels).

    Modes: see the module constants. In mode 5 small-first is handled by the level sort
    as in the original (small items first, the rest inserted behind them)."""
    if mode == MODE_SHORTEST:
        return shortest_path(nodes, start, small_first)
    if mode == MODE_LOCAL:
        # the statement-level port (0x1003d300 etc.), on the nodes' entry / exit points; the
        # set of flipped open contours is dropped here (``toolpath_sort`` returns it)
        return toolpath_sort(nodes, small_first, LOCAL_SEED)[0]
    roots = [n for n in nodes if n.parent is None]
    out: list[Node] = []
    pos = start

    rank = {id(n): i for i, n in enumerate(nodes)}

    def visit(level: list[Node]):
        nonlocal pos
        if mode not in (1, 2, 3, 4):        # "incoming order" = caller's (document) order
            level = sorted(level, key=lambda n: rank.get(id(n), 0))
        for n in sort_level(level, mode, pos, small_first):
            if n.children:
                visit(n.children)
            out.append(n)
            pos = n.exit

    visit(roots)
    return out


# --------------------------------------------------------------------------- mode 5
LOCAL_SEED = 0           # the original seeds rand() with time(); fixed here for repeatability


def _seg_dist(p: Point, a: Point, b: Point) -> float:
    """0x1003b800: distance from p to segment a->b (|p-b| for a degenerate segment)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-10:
        return math.dist(p, b)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _path_len(seq: Sequence[Node], pos: Optional[Point] = None) -> float:
    d = math.dist(pos, seq[0].entry) if (pos is not None and seq) else 0.0
    return d + sum(math.dist(a.exit, b.entry) for a, b in zip(seq, seq[1:]))


def _local_optimal(nodes: list[Node], pos: Point) -> list[Node]:
    """Mode 5 (0x1003d300, fix_first = 0): open contours appended by nearest neighbour,
    closed contours added by cheapest insertion in shuffled order, the resulting cycle cut
    at its longest edge, and the result accepted only if it beats the incoming order.

    Rebuild differences (I): distances use the real entry/exit (lead start / lead end)
    instead of the contour start; open contours are never reversed; the shuffle is seeded."""
    n = len(nodes)
    if n < 4:
        return list(nodes)
    route: list[Node] = []
    opens = [x for x in nodes if not x.contour.closed]
    closed = [x for x in nodes if x.contour.closed]
    rest = list(opens)
    while rest:
        if not route:
            route.append(rest.pop(0))
            continue
        cur = route[-1].exit
        k = min(range(len(rest)), key=lambda i: min(math.dist(cur, rest[i].entry), math.dist(cur, rest[i].exit)))
        route.append(rest.pop(k))
    rng = random.Random(LOCAL_SEED)
    order = list(closed)
    rng.shuffle(order)
    for x in order:
        p = x.entry
        if not route:
            route.append(x)
            continue
        best = _seg_dist(p, route[-1].exit, route[0].entry)
        best_pos = len(route)
        for k in range(len(route) - 1, 0, -1):
            a, b = route[k - 1].exit, route[k].entry
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if abs(p[0] - mx) - abs(b[0] - mx) >= best or abs(p[1] - my) - abs(b[1] - my) >= best:
                continue
            d = _seg_dist(p, a, b)
            if d < best:
                best, best_pos = d, k
        route.insert(best_pos, x)
    # cut the cycle at its longest edge
    edges = [math.dist(route[-1].exit, route[0].entry)] + [
        math.dist(route[k - 1].exit, route[k].entry) for k in range(1, len(route))]
    cyc = sum(edges)
    kmax = max(range(len(edges)), key=lambda k: edges[k])
    if kmax > 0:
        route = route[kmax:] + route[:kmax]
    old = _path_len(nodes)
    if cyc - edges[kmax] < LOCAL_ACCEPT * old:
        return route
    return list(nodes)


# The faithful sort modes and the start point are ports in the parity fence
# (mlaser.parity_ports.cadmodule.ordering); the entry points below dispatch through the active
# ParityProfile (``ordering`` slot), helpers and constants are the port's.
from ..parity_ports.cadmodule.ordering import (  # noqa: E402,F401
    SORT_APPROX_EPS, SORT_SEG_EPS, SORT_SMALL_FRAC, SORT_SMALL_AVG, SORT_BIG, SortElem,
    MsvcRand, approx_hypot, point_seg_dist, _split_small, _cheapest_pos, _strictly_contains,
    RING_TOL_FAITHFUL, _walk_to_dist, _GLY_SEGMENT, _GLY_ARC, _GLY_CIRCLE, _GLY_LWPOLY,
    _GLY_SPLINE, _source_glyph_groups, contour_glyph_list, contour_glyphs, LENGTH_TIE_EPS,
    _long_side_start, LOCAL_ACCEPT)

import_sort = dispatch("ordering", "import_sort")
local_optimal_elems = dispatch("ordering", "local_optimal_elems")
containers_last_faithful = dispatch("ordering", "containers_last_faithful")
ring_sort = dispatch("ordering", "ring_sort")
choose_start = dispatch("ordering", "choose_start")
def toolpath_sort(nodes: list[Node], small_first: bool = False, seed: int = 0) -> tuple[list[Node], set]:
    """Mode 5 of the cutting order (dispatcher 0x1003f010 -> 0x1003e690 / 0x1003d300 /
    0x1003c480 / 0x1003c7b0), the same faithful port as :func:`import_sort`, with the element
    points the original's graphs report once their crafts are applied: the node's ``entry``
    (lead-in start, or the start point; kerf child) and, for open contours, its ``exit``
    (``build_toolpath`` sets both from the prepared contour).  Returns (order, ids of the open
    contours the sort flipped); the caller reverses those (vtable +0xb0 = 0x10073980) and
    prepares their leads again."""
    return import_sort(nodes, small_first, seed, ends=lambda n: (tuple(n.entry), tuple(n.exit)))


def _containers_last(seq: list[Node]) -> list[Node]:
    """0x1003c480: for c = n-1 .. 1, the lowest j < c whose bbox strictly contains slot c's
    bbox is moved to position c (a sibling whose box surrounds others is cut after them)."""
    seq = list(seq)
    for c in range(len(seq) - 1, 0, -1):
        for j in range(c):
            if _strictly_contains(seq[j].bbox, seq[c].bbox):
                seq.insert(c, seq.pop(j))
                break
    return seq


# --------------------------------------------------------------------------- grid (rebuild)
def _grid_sort(nodes: list[Node], pos: Point) -> list[Node]:
    """Rebuild extension: cells of 2x the average item size, rows bottom->top, cells
    serpentine, nearest neighbour inside a cell."""
    if len(nodes) < 2:
        return list(nodes)
    aw = sum(max(x.size[0], 1e-3) for x in nodes) / len(nodes)
    ah = sum(max(x.size[1], 1e-3) for x in nodes) / len(nodes)
    cw, ch = 2 * aw, 2 * ah
    x0 = min(x.bbox[0] for x in nodes); y0 = min(x.bbox[1] for x in nodes)
    cells: dict[tuple[int, int], list[Node]] = {}
    for x in nodes:
        c = ((x.bbox[0] + x.bbox[2]) / 2, (x.bbox[1] + x.bbox[3]) / 2)
        cells.setdefault((int((c[1] - y0) / ch), int((c[0] - x0) / cw)), []).append(x)
    out: list[Node] = []
    rows = sorted({r for r, _ in cells})
    for k, r in enumerate(rows):
        cols = sorted(c for rr, c in cells if rr == r)
        if k % 2:
            cols.reverse()
        for c in cols:
            s = _nearest(cells[(r, c)], pos)
            out += s
            pos = s[-1].exit
    return out


# --------------------------------------------------------------------------- shortest (rebuild)
def _descendant_ok(seq: list[Node]) -> bool:
    seen = set()
    for x in seq:
        if any(id(ch) not in seen for ch in x.children):
            return False
        seen.add(id(x))
    return True


def shortest_path(nodes: list[Node], start: Point = (0.0, 0.0), small_first: bool = False,
                  time_limit: float = 2.0) -> list[Node]:
    """Rebuild extension (mode 20): global order over all contours with the only constraint
    that every contour comes after its children (holes / parts in holes). Greedy nearest
    available contour, then 2-opt (segment reversal, asymmetric entry/exit costs) and or-opt
    (single moves) that keep the constraint, until no improvement or ``time_limit``."""
    if not nodes:
        return []
    t0 = time.monotonic()
    groups = list(_small_split([x for x in nodes if x.parent is None])) if small_first else None
    # greedy
    remaining = {id(x): len(x.children) for x in nodes}
    avail = [x for x in nodes if not x.children]
    if groups is not None and groups[0]:
        small_ids = set()
        stack = list(groups[0])
        while stack:
            y = stack.pop()
            small_ids.add(id(y))
            stack.extend(y.children)
    else:
        small_ids = None
    out: list[Node] = []
    pos = start
    while avail:
        cand = avail
        if small_ids is not None:
            sm = [x for x in avail if id(x) in small_ids]
            cand = sm or avail
        k = min(range(len(cand)), key=lambda i: math.dist(pos, cand[i].entry))
        x = cand[k]
        avail.remove(x)
        out.append(x)
        pos = x.exit
        if x.parent is not None:
            remaining[id(x.parent)] -= 1
            if remaining[id(x.parent)] == 0:
                avail.append(x.parent)
    if small_ids is not None:
        return out          # keep the small-first partition intact (no global moves)
    out = _two_opt(out, start, t0, time_limit)
    out = _or_opt(out, start, t0, time_limit)
    return out


def _arrays(seq: list[Node]):
    E = np.array([x.entry for x in seq], float)
    X = np.array([x.exit for x in seq], float)
    return E, X


def _two_opt(seq: list[Node], start: Point, t0: float, limit: float) -> list[Node]:
    n = len(seq)
    if n < 3:
        return seq
    improved = True
    while improved and time.monotonic() - t0 < limit:
        improved = False
        E, X = _arrays(seq)
        where = {id(x): k for k, x in enumerate(seq)}
        ppos = np.array([where.get(id(x.parent), n) if x.parent is not None else n for x in seq])
        # m[i] = min parent position over k >= i: reversal [i, j] valid iff j < m[i]
        m = np.minimum.accumulate(ppos[::-1])[::-1]
        F = np.hypot(*(E[1:] - X[:-1]).T)            # e(k, k+1)
        B = np.hypot(*(E[:-1] - X[1:]).T)            # e(k+1, k)
        cF = np.concatenate([[0.0], np.cumsum(F)])
        cB = np.concatenate([[0.0], np.cumsum(B)])
        for i in range(n - 1):
            jmax = min(n - 1, int(m[i]) - 1)
            if jmax <= i:
                continue
            js = np.arange(i + 1, jmax + 1)
            prev = np.array(start, float) if i == 0 else X[i - 1]
            e_prev_i = math.dist(prev, E[i])
            e_prev_j = np.hypot(*(E[js] - prev).T)
            nxt_ok = js + 1 < n
            nj = np.minimum(js + 1, n - 1)
            e_i_next = np.where(nxt_ok, np.hypot(*(E[nj] - X[i]).T), 0.0)
            e_j_next = np.where(nxt_ok, np.hypot(*(E[nj] - X[js]).T), 0.0)
            inner = (cB[js] - cB[i]) - (cF[js] - cF[i])
            delta = e_prev_j + e_i_next - e_prev_i - e_j_next + inner
            k = int(np.argmin(delta))
            if delta[k] < -1e-9:
                j = int(js[k])
                seq[i:j + 1] = seq[i:j + 1][::-1]
                improved = True
                break
    return seq


def _or_opt(seq: list[Node], start: Point, t0: float, limit: float) -> list[Node]:
    n = len(seq)
    if n < 3:
        return seq
    improved = True
    while improved and time.monotonic() - t0 < limit:
        improved = False
        for k in range(n):
            x = seq[k]
            prev = start if k == 0 else seq[k - 1].exit
            nxt = seq[k + 1].entry if k + 1 < n else None
            gain = math.dist(prev, x.entry) + (math.dist(x.exit, nxt) if nxt else 0.0)
            gain -= math.dist(prev, nxt) if nxt else 0.0
            rest = seq[:k] + seq[k + 1:]
            where = {id(y): i for i, y in enumerate(rest)}
            lo = max([where[id(c)] + 1 for c in x.children] + [0])
            hi = where[id(x.parent)] if x.parent is not None and id(x.parent) in where else len(rest)
            best, best_t = gain - 1e-9, None
            for t in range(lo, hi + 1):            # insert before rest[t]
                if t == k:
                    continue
                a = start if t == 0 else rest[t - 1].exit
                b = rest[t].entry if t < len(rest) else None
                cost = math.dist(a, x.entry) + (math.dist(x.exit, b) - math.dist(a, b) if b else 0.0)
                if cost < best:
                    best, best_t = cost, t
            if best_t is not None:
                rest.insert(best_t, x)
                seq = rest
                improved = True
                break
    return seq


def rapid_length(order: Sequence[Node], start: Point = (0.0, 0.0), back_to_start: bool = False) -> float:
    """Total rapid (travel) length of a cutting order: start -> entry, exit -> next entry."""
    if not order:
        return 0.0
    d = _path_len(order, start)
    if back_to_start:
        d += math.dist(order[-1].exit, start)
    return d


# --------------------------------------------------------------------------- rapid avoidance (rebuild)
def route_rapid(p0: Point, p1: Point, obstacles, clearance: float = 1.0):
    """Shortest polyline from p0 to p1 that does not cross the interior of ``obstacles``
    (shapely geometries grown by ``clearance``), via a visibility graph over the obstacle
    outline vertices. Obstacles containing p0 or p1 are ignored. Returns [p0, ..., p1]."""
    from shapely.geometry import LineString, Point as SPoint
    from shapely.ops import unary_union
    line = LineString([p0, p1])
    obs = []
    for g in obstacles:
        tol = 0.25 if clearance > 0 else 0.0
        gg = g.simplify(tol).buffer(clearance + tol, quad_segs=2) if clearance > 0 else g
        if gg.contains(SPoint(p0)) or gg.contains(SPoint(p1)):
            # an end point lies on/near this opening (e.g. the exit of the hole just cut):
            # still keep the rapid out of the opening itself, without clearance
            gg = g.simplify(0.1).buffer(-0.05, quad_segs=2)
            if gg.is_empty or gg.contains(SPoint(p0)) or gg.contains(SPoint(p1)):
                continue
        obs.append(gg)
    if not obs:
        return [p0, p1]
    blk = unary_union(obs)
    core = blk.buffer(-1e-6)
    if not line.intersects(core):
        return [p0, p1]
    # only obstacles connected to the straight path matter (keeps the graph small)
    polys = list(getattr(blk, "geoms", [blk]))
    rel = [g for g in polys if g.envelope.buffer(math.dist(p0, p1)).intersects(line)]
    verts = [p0, p1]
    for g in rel:
        for ring in [g.exterior] + list(g.interiors):
            verts += [(x + 0.0, y + 0.0) for x, y in list(ring.coords)[:-1]]
    n = len(verts)
    import heapq
    dist = [math.inf] * n
    prev = [-1] * n
    dist[0] = 0.0
    seen = [False] * n
    h = [(0.0, 0)]
    vis_cache: dict = {}

    def visible(a: int, b: int) -> bool:
        k = (a, b) if a < b else (b, a)
        if k not in vis_cache:
            vis_cache[k] = not LineString([verts[a], verts[b]]).intersects(core)
        return vis_cache[k]

    while h:
        d, u = heapq.heappop(h)
        if seen[u]:
            continue
        seen[u] = True
        if u == 1:
            break
        for v in range(n):
            if seen[v] or v == u:
                continue
            nd = d + math.dist(verts[u], verts[v])
            if nd < dist[v] and visible(u, v):
                dist[v], prev[v] = nd, u
                heapq.heappush(h, (nd, v))
    if prev[1] < 0:
        return [p0, p1]
    path = [1]
    while path[-1] != 0:
        path.append(prev[path[-1]])
    return [verts[i] for i in reversed(path)]


def plan_rapids(order: Sequence[Node], start: Point = (0.0, 0.0), clearance: float = 2.0,
                avoid_parts: bool = False, min_size: float = 0.0):
    """Rebuild extension: rapid paths for a cutting order that avoid the openings already
    cut (closed even-depth contours = holes whose slug may drop or tilt) and, with
    ``avoid_parts``, finished parts (closed odd-depth contours). Returns a list of polylines,
    one per node (from the previous exit to the node's entry)."""
    from shapely.geometry import Polygon
    cut = []
    out = []
    pos = start
    for x in order:
        out.append(route_rapid(pos, x.entry, cut, clearance) if cut else [pos, x.entry])
        if x.contour.closed and x.contour.segments:
            hole = x.depth % 2 == 0
            if hole or avoid_parts:
                sz = max(x.size)
                if sz >= min_size:
                    try:
                        g = Polygon(polyline(x.contour, 0.2))
                        if not g.is_valid:
                            g = g.buffer(0)
                        if g.area > 0:
                            cut.append(g)
                    except ValueError:
                        pass
        pos = x.exit
    return out


def polyline_length(pts: Sequence[Point]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


# --------------------------------------------------------------------------- document reorders
def set_layer_priority(doc, layers, last: bool = False) -> None:
    """Types 10/11 (mf610 / mf611): parts on ``layers`` move first (or last) in a stable
    partition of ``doc.parts``; nothing is sorted geometrically. Use with mode 0."""
    layers = set(layers)
    on = [p for p in doc.parts if p.craft.layer in layers]
    off = [p for p in doc.parts if p.craft.layer not in layers]
    doc.parts = off + on if last else on + off
    doc.extra.pop("order", None)


def manual_pick(doc, index: int, step: int) -> int:
    """One click of the manual-sort tool (type 9, 0x100b9a50): the hit part (only the
    unordered tail ``index >= step - 1`` can be hit) moves to slot ``step - 1``; the others
    shift right keeping their order. Returns the next step; 0 when the order is complete."""
    n = len(doc.parts)
    if step < 1 or index < step - 1 or index >= n:
        return step
    doc.parts.insert(step - 1, doc.parts.pop(index))
    doc.extra.pop("order", None)
    step += 1
    return 0 if step > n else step


def set_manual_order(doc, sequence: Sequence[int]) -> None:
    """Apply a whole click sequence (original part indices) at once: listed parts first in
    that order, the rest after in their current order. Use with mode 0."""
    seen, head = set(), []
    for i in sequence:
        if 0 <= i < len(doc.parts) and i not in seen:
            seen.add(i)
            head.append(doc.parts[i])
    doc.parts = head + [p for i, p in enumerate(doc.parts) if i not in seen]
    doc.extra.pop("order", None)


def move_first_last(doc, index: int, last: bool = False) -> None:
    """mv16 / mv15 (0x100b8bb0): move one part to the first (or last) position."""
    if 0 <= index < len(doc.parts) and len(doc.parts) >= 2:
        p = doc.parts.pop(index)
        if last:
            doc.parts.append(p)
        else:
            doc.parts.insert(0, p)
        doc.extra.pop("order", None)


