"""Fast drop-ins for the mode-5 sort kernels of :mod:`.ordering` (same output, bit for bit).

The statement-level ports in :mod:`.ordering` are O(n^2) in pure Python: the greedy nearest
neighbour over the open contours and the cheapest-insertion scan of 0x1003d300
(:func:`~.ordering.local_optimal_elems`, 25 M ``approx_hypot`` calls on a 5,000-part scan) and
the container chain of 0x1003c480 (:func:`~.ordering.containers_last_faithful`).  The versions
here find the same answer with less work and keep the reference's float formulas and
first-match tie-breaks:

* numpy and a grid index are used **only as filters** that provably keep every element the
  reference could pick; the decision among the survivors is made with the reference's own
  scalar functions (:func:`~.ordering.approx_hypot`, :func:`~.ordering.point_seg_dist`,
  :func:`~.ordering._strictly_contains`) on the same Python floats, in the reference's order;
* nearest neighbour: a uniform grid over the element points, searched in Chebyshev rings until
  every unsearched point is provably farther (``approx_hypot(dx, dy) >= max(|dx|, |dy|) - 1e-8``);
  ties keep the lowest index, as the reference's strict ``<`` scan does;
* cheapest insertion: the midpoint pre-filter of the reference evaluated on numpy arrays of
  the route's gaps; the exact running-minimum scan then runs over the few gaps that can win
  (see :meth:`_Route.cheapest_pos` for why the result is the reference's);
* containers last: an STRtree over the bboxes lists each slot's strict containers once; the
  chain walk follows positions instead of rescanning the level.

The reference functions stay in :mod:`.ordering` (profile ``"original-reference"``,
:mod:`mlaser.parity_ports.profiles.reference`); tests/test_ordering_fast.py compares both on
hypothesis-generated inputs, and the goldens (tests/golden/orders.json) and the parity run pin
the default profile, which uses these.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

from .._registry import port
from .ordering import (LOCAL_ACCEPT, SORT_APPROX_EPS, SORT_BIG, SortElem, MsvcRand,  # noqa: F401
                       _split_small, _strictly_contains, approx_hypot, point_seg_dist)

Node = Any

__port__ = {
    "source": "CADModule",
    "functions": {
        "0x1003d300": "local_optimal_elems (filtered drop-in of ordering.local_optimal_elems)",
        "0x1003c480": "containers_last_faithful (indexed drop-in)",
        "FUN_100b57c0": "import_sort (calls the drop-ins)",
    },
    "parity": ["dxf", "import_bytes", "lead", "jobcad"],
    "reference": "mlaser.parity_ports.cadmodule.ordering",
}

# relative / absolute slack of the numpy filters (np.hypot may differ from math.hypot by an ulp)
_REL = 1e-9
_ABS = 1e-12


# --------------------------------------------------------------------------- nearest neighbour
def _approx_np(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """:func:`approx_hypot` on arrays (a filter value; the decision uses the scalar one)."""
    ax, ay = np.abs(dx), np.abs(dy)
    M = np.maximum(ax, ay)
    m = np.minimum(ax, ay)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(M > 0, m / np.where(M > 0, M, 1.0), 0.0)
    t = r * 0.5 * r
    out = M + t * M * (t * 0.5 * (t - 1.0) + 1.0)
    return np.where((ax < SORT_APPROX_EPS) & (ay < SORT_APPROX_EPS), 0.0, out)


class _OpenNN:
    """The greedy step of 0x1003d300 over the open contours: from ``cur``, the unused element
    with the smallest ``approx_hypot`` to its start or end (end only if strictly closer), the
    lowest index on ties, nothing when no distance is below ``SORT_BIG``."""

    def __init__(self, opens: list[SortElem]):
        self.S = [tuple(e.start) for e in opens]
        self.E = [tuple(e.exit) for e in opens]
        n = len(opens)
        self.used = bytearray(n)
        self.alive = n
        pts = self.S + self.E
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.buckets: dict[tuple[int, int], list[int]] = {}
        self._np: Optional[tuple[np.ndarray, np.ndarray]] = None
        self.grid = all(map(math.isfinite, xs)) and all(map(math.isfinite, ys))
        if not self.grid:
            return
        self.x0, self.y0 = min(xs), min(ys)
        W, H = max(xs) - self.x0, max(ys) - self.y0
        h = math.sqrt(W * H / max(n, 1)) if W > 0 and H > 0 else 0.0
        h = max(h, max(W, H) / max(n, 1), 1e-6)
        if not math.isfinite(h):
            h = 1.0
        self.h = h
        self.scale = max(abs(self.x0), abs(self.y0), abs(W), abs(H), 1.0)
        for j in range(n):
            for p in (self.S[j], self.E[j]):
                c = self._cell(p)
                b = self.buckets.get(c)
                if b is None:
                    self.buckets[c] = [j]
                elif b[-1] != j:
                    b.append(j)

    def _cell(self, p) -> tuple[int, int]:
        return (int(math.floor((p[0] - self.x0) / self.h)), int(math.floor((p[1] - self.y0) / self.h)))

    def take(self, j: int) -> None:
        self.used[j] = 1
        self.alive -= 1

    def _dist(self, j: int, cur) -> tuple[float, bool]:
        s, e = self.S[j], self.E[j]
        d1 = approx_hypot(s[0] - cur[0], s[1] - cur[1])
        d2 = approx_hypot(e[0] - cur[0], e[1] - cur[1])
        r = d2 < d1
        if r:
            d1 = d2
        return d1, r

    def query(self, cur) -> tuple[int, bool]:
        if self.alive <= 0:
            return -1, False
        if not (self.grid and math.isfinite(cur[0]) and math.isfinite(cur[1])):
            return self._brute(cur)
        cx, cy = self._cell(cur)
        best, bj, brev = math.inf, -1, False
        used, buckets = self.used, self.buckets
        scanned = 0
        limit = 4 * self.alive + 64
        R = 0
        while True:
            if R == 0:
                ring = [(cx, cy)]
            else:
                ring = [(cx + i, cy - R) for i in range(-R, R + 1)]
                ring += [(cx + i, cy + R) for i in range(-R, R + 1)]
                ring += [(cx - R, cy + i) for i in range(-R + 1, R)]
                ring += [(cx + R, cy + i) for i in range(-R + 1, R)]
            scanned += len(ring)
            for c in ring:
                b = buckets.get(c)
                if not b:
                    continue
                dead = 0
                for j in b:
                    if used[j]:
                        dead += 1
                        continue
                    d, r = self._dist(j, cur)
                    if d < best or (d == best and j < bj):
                        best, bj, brev = d, j, r
                if dead > 8 and dead * 2 > len(b):
                    b[:] = [j for j in b if not used[j]]
            # every point outside rings 0..R has max(|dx|, |dy|) >= R*h (cell rounding aside)
            bound = R * self.h * (1.0 - 1e-9) - 1e-9 * self.scale - SORT_APPROX_EPS
            if bj >= 0 and best < bound:
                break
            R += 1
            if scanned > limit:
                return self._brute(cur)
        if not best < SORT_BIG:
            return -1, False
        return bj, brev

    def _brute(self, cur) -> tuple[int, bool]:
        if self._np is None:
            self._np = (np.array(self.S, dtype=float).reshape(-1, 2), np.array(self.E, dtype=float).reshape(-1, 2))
        S, E = self._np
        idx = np.flatnonzero(np.frombuffer(bytes(self.used), dtype=np.uint8) == 0)
        if idx.size == 0:
            return -1, False
        d1 = _approx_np(S[idx, 0] - cur[0], S[idx, 1] - cur[1])
        d2 = _approx_np(E[idx, 0] - cur[0], E[idx, 1] - cur[1])
        d = np.minimum(d1, d2)
        ok = np.isfinite(d)
        if ok.all():
            dmin = float(d.min())
            cand = idx[d <= dmin * (1 + _REL) + _ABS]
        else:           # non-finite coordinates: decide exactly over everything
            cand = idx
        best, bj, brev = math.inf, -1, False
        for j in cand.tolist():                       # ascending index: first minimum kept
            dj, r = self._dist(j, cur)
            if dj < best:
                best, bj, brev = dj, j, r
        if not best < SORT_BIG:
            return -1, False
        return bj, brev


# --------------------------------------------------------------------------- cheapest insertion
class _Route:
    """The route of 0x1003d300 as a Python list plus numpy arrays of its element starts and
    exits and of its gaps' midpoint pre-filter terms (gap k = route[k-1].exit -> route[k].start,
    stored at index k-1)."""

    def __init__(self):
        self.items: list[SortElem] = []
        cap = 64
        self.P = np.zeros((4, cap))           # rows: start x, start y, exit x, exit y
        self.G = np.zeros((5, cap))           # rows per gap: mx, my, |bx - mx|, |by - my|, |b - a| / 2

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, k):
        return self.items[k]

    def _grow(self, need: int) -> None:
        if need > self.P.shape[1]:
            cap = max(need, 2 * self.P.shape[1])
            P = np.zeros((4, cap)); P[:, :self.P.shape[1]] = self.P; self.P = P
            G = np.zeros((5, cap)); G[:, :self.G.shape[1]] = self.G; self.G = G

    def _gap(self, k: int) -> None:
        a = self.items[k - 1].exit
        b = self.items[k].start
        mx, my = (b[0] + a[0]) * 0.5, (b[1] + a[1]) * 0.5
        hx, hy = abs(b[0] - mx), abs(b[1] - my)
        self.G[:, k - 1] = (mx, my, hx, hy, math.hypot(hx, hy) * (1 + _REL) + _ABS)

    def insert(self, pos: int, e: SortElem) -> None:
        m = len(self.items)
        self._grow(m + 1)
        self.items.insert(pos, e)
        s, x = e.start, e.exit
        if pos < m:
            self.P[:, pos + 1:m + 1] = self.P[:, pos:m].copy()
        self.P[:, pos] = (s[0], s[1], x[0], x[1])
        # gaps (gap k at index k-1): old gaps k >= pos+1 move to k+1, old gap k = pos is split
        # into the new gaps pos and pos+1
        if pos <= m - 2:
            self.G[:, pos + 1:m] = self.G[:, pos:m - 1].copy()
        for k in (pos, pos + 1):
            if 1 <= k <= m:
                self._gap(k)

    def append(self, e: SortElem) -> None:
        self.insert(len(self.items), e)

    def cheapest_pos(self, p, best: float, floor: int) -> int:
        """``ordering._cheapest_pos(route, p, best, floor)``, bit for bit.

        The reference walks k = len-1 .. 1 and accepts gap k when its pre-filter terms
        ``|p - m| - |b - m|`` (x and y) and its distance ``d_k`` are all below the current
        ``best``, which then becomes ``d_k``; the result is the last accepted k (else the end).

        Only gaps of S = {both terms below the initial best} can be accepted.  Let ``dmin`` be
        the smallest distance over S and k* the highest k reaching it.  Every gap accepted before
        k* has a larger distance, so ``best > dmin`` when the walk reaches k*; if both terms of
        k* are <= dmin, k* is accepted, and no later gap can be (a smaller distance does not
        exist).  So the answer is k*, found from the few gaps that can hold the minimum: the
        terms are lower bounds of the distance, ``|p - m| + |b - a| / 2`` an upper bound.  When
        k*'s terms exceed dmin (rounding at a segment end), S is walked exactly as the
        reference does."""
        items = self.items
        m = len(items)
        pos = m
        if m >= 2:
            G = self.G[:, :m - 1]
            px, py = float(p[0]), float(p[1])
            lx = np.abs(px - G[0]) - G[2]
            ly = np.abs(py - G[1]) - G[3]
            sel = np.flatnonzero((lx < best) & (ly < best))
            if sel.size > 8:
                lb = np.maximum(lx[sel], ly[sel])
                ub = np.hypot(px - G[0, sel], py - G[1, sel]) + G[4, sel]
                U = float(ub.min()) if np.isfinite(ub).all() else math.inf
                ks = sel[~(lb > U * (1 + _REL) + _ABS)] + 1
                dn = _seg_dist_np(px, py, self.P[2:4, ks - 1].T, self.P[0:2, ks].T)
                tk = ks[~(dn > float(dn.min()) * (1 + _REL) + _ABS)].tolist() if np.isfinite(dn).all() else None
                res = self._fast_pick(p, tk, best) if tk else None
                if res is None:
                    pos = self._scan(p, (sel + 1).tolist(), best)
                else:
                    pos = res
            elif sel.size:
                pos = self._scan(p, (sel + 1).tolist(), best)
        return max(pos, floor)

    def _terms(self, p, k: int):
        a, b = self.items[k - 1].exit, self.items[k].start
        mx, my = (b[0] + a[0]) * 0.5, (b[1] + a[1]) * 0.5
        return abs(p[0] - mx) - abs(b[0] - mx), abs(p[1] - my) - abs(b[1] - my), a, b

    def _fast_pick(self, p, tk: list[int], best0: float) -> Optional[int]:
        """k* of :meth:`cheapest_pos` from the candidate gaps ``tk`` (all gaps that can hold
        the minimal distance), ``len(route)`` when no gap beats ``best0``, None when k*'s
        terms do not allow the shortcut."""
        rows = []
        for k in tk:
            lx, ly, a, b = self._terms(p, k)
            rows.append((k, lx, ly, point_seg_dist(p, a, b)))
        dmin = min(r[3] for r in rows)
        if not dmin < best0:
            return len(self.items)          # nothing in S beats the initial best
        k, lx, ly, _d = max(r for r in rows if r[3] == dmin)
        if lx <= dmin and ly <= dmin:
            return k
        return None

    def _scan(self, p, ks: list[int], best: float) -> int:
        pos = len(self.items)
        for k in sorted(ks, reverse=True):
            lx, ly, a, b = self._terms(p, k)
            if lx < best and ly < best:
                d = point_seg_dist(p, a, b)
                if d < best:
                    best, pos = d, k
        return pos


def _seg_dist_np(px: float, py: float, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """:func:`point_seg_dist` on arrays of segments a -> b (a filter value)."""
    ex, ey = B[:, 0] - A[:, 0], B[:, 1] - A[:, 1]
    L = np.hypot(ex, ey)
    X, Y = B[:, 0] - px, B[:, 1] - py
    with np.errstate(divide="ignore", invalid="ignore"):
        proj = (Y * ey + X * ex) / L
        perp = np.abs(Y * ex - X * ey) / L
    dB = np.hypot(X, Y)
    dA = np.hypot(A[:, 0] - px, A[:, 1] - py)
    out = np.where(L < 1e-5, dB, np.where(L < proj, dA, np.where(proj < 0.0, dB, perp)))
    return np.where(np.isfinite(out), out, -np.inf)       # NaN / inf: never filtered out


# --------------------------------------------------------------------------- 0x1003d300
@port("0x1003d300", source="CADModule", parity=("dxf", "import_bytes"),
      note="drop-in of ordering.local_optimal_elems: grid / numpy filters, reference decisions")
def local_optimal_elems(elems: list[SortElem], small_first: bool, fix_first: bool = False,
                        seed: int = 0) -> list[SortElem]:
    """:func:`.ordering.local_optimal_elems` (0x1003d300), the same result with an indexed
    nearest-neighbour search and a filtered cheapest-insertion scan (module docstring)."""
    n = len(elems)
    if n < 4:
        return [e.copy() for e in elems]
    work = [e.copy() for e in (elems[1:] if fix_first else elems)]
    small: list[SortElem] = []
    if small_first:
        small, work = _split_small(work)
    route = _Route()
    floor = 0
    if fix_first:
        route.append(elems[0].copy())
        floor = 1
    if small:
        route.append(small[0].copy())
    for k in range(1, len(small)):
        p = small[k].start
        best = point_seg_dist(p, route[0].exit, route[-1].start)
        route.insert(route.cheapest_pos(p, best, floor), small[k].copy())
    floor += len(small)
    rng = MsvcRand(seed)
    picks: list[SortElem] = []
    opens: list[SortElem] = []
    for e in work:
        if not e.closed:
            opens.append(e)
        else:
            picks.insert(rng.rand() % (len(picks) + 1), e)
    if opens:
        nn = _OpenNN(opens)
        start_i = 0
        if not len(route):
            route.append(opens[0].copy())
            nn.take(0)
            start_i = 1
        for _ in range(start_i, len(opens)):
            pick, rev = nn.query(route[-1].exit)
            if pick < 0:
                break                       # the reference finds nothing again (same cur)
            e = opens[pick].copy()
            if rev and not e.closed:
                e.start, e.end = e.end, e.start
                e.reversed = not e.reversed
            route.append(e)
            nn.take(pick)
    k0 = 0
    if not len(route) and picks:
        route.append(picks[0].copy())
        k0 = 1
    for e in picks[k0:]:
        p = e.start
        best = point_seg_dist(p, route[-1].exit, route[0].start)
        route.insert(route.cheapest_pos(p, best, floor), e.copy())
    seq = route.items

    def d(a: SortElem, b: SortElem) -> float:
        return math.hypot(b.start[0] - a.start[0], b.start[1] - a.start[1])

    longest = d(seq[-1], seq[0])
    total = longest
    old = 0.0
    kmax = -1
    for i in range(n - 1, 0, -1):
        e2 = d(seq[i - 1], seq[i])
        if longest < e2:
            kmax, longest = i, e2
        total += e2
        old += d(elems[i - 1], elems[i])
    if kmax >= 0 and not fix_first and not small:
        seq = seq[kmax:] + seq[:kmax]
    if total - longest < old * LOCAL_ACCEPT:
        return seq
    return [e.copy() for e in elems]


# --------------------------------------------------------------------------- 0x1003c480
def _containers(seq: list) -> Optional[list[list[int]]]:
    """For every slot the slots whose bbox strictly contains its bbox; None when there are none."""
    n = len(seq)
    B = np.array([tuple(x.bbox) for x in seq], dtype=float).reshape(n, 4)
    if not np.isfinite(B).all():
        full = [[j for j in range(n) if j != i and _strictly_contains(seq[j].bbox, seq[i].bbox)]
                for i in range(n)]
        return full if any(full) else None
    import shapely
    g = 1e-6 + 1e-12 * np.abs(B).max(axis=1)
    boxes = shapely.box(B[:, 0] - g, B[:, 1] - g, B[:, 2] + g, B[:, 3] + g)
    tree = shapely.STRtree(boxes)
    qi, ti = tree.query(boxes)
    sel = qi != ti
    qi, ti = qi[sel], ti[sel]
    # superset filter on arrays, then the reference's own test on the survivors
    keep = (B[ti, 0] < B[qi, 0]) & (B[ti, 1] < B[qi, 1]) & (B[ti, 2] > B[qi, 2]) & (B[ti, 3] > B[qi, 3])
    qi, ti = qi[keep], ti[keep]
    if qi.size == 0:
        return None
    out: list[list[int]] = [[] for _ in range(n)]
    found = False
    for i, j in zip(qi.tolist(), ti.tolist()):
        if _strictly_contains(seq[j].bbox, seq[i].bbox):
            out[i].append(j)
            found = True
    return out if found else None


@port("0x1003c480", source="CADModule", parity=("dxf", "import_bytes"),
      note="drop-in of ordering.containers_last_faithful: indexed container chain")
def containers_last_faithful(seq: list) -> list:
    """:func:`.ordering.containers_last_faithful` (0x1003c480): for c = n-1 .. 1 the chain of
    strict bbox containers of slot c, each found at the highest position below the previous
    one, ends at the slot moved to c.  The containers of every item are listed once (STRtree),
    the walk follows the items' current positions."""
    seq = list(seq)
    n = len(seq)
    if n < 2:
        return seq
    cont = _containers(seq)
    if cont is None:
        return seq
    order = list(range(n))            # position -> original slot
    pos = list(range(n))              # original slot -> position
    for c in range(n - 1, 0, -1):
        cand = order[c]
        L = c
        while True:
            hit = -1
            for j in cont[cand]:
                pj = pos[j]
                if hit < pj < L:
                    hit = pj
            if hit < 0:
                break
            L = hit
            cand = order[hit]
        if L != c:
            o = order.pop(L)
            order.insert(c, o)
            for q in range(L, c + 1):
                pos[order[q]] = q
    return [seq[o] for o in order]


# --------------------------------------------------------------------------- FUN_100b57c0
@port("FUN_100b57c0", "0x1003f010", "0x1003e690", source="CADModule", parity=("dxf", "import_bytes"),
      note="ordering.import_sort with the drop-in kernels")
def import_sort(nodes: list[Node], small_first: bool = True, seed: int = 0,
                ends=None) -> tuple[list[Node], set]:
    """:func:`.ordering.import_sort` (FUN_100b57c0, mode 5 per tree level, containers last,
    post-order) running the drop-in kernels of this module."""
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
        if len(children) < 4:      # local_optimal_elems keeps fewer than 4 elements unchanged
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


# --------------------------------------------------------------------------- ring sort
def _ring_kernel() -> Any:
    """The C kernel's ring_order when the default selection chose the kernel, else None."""
    from ..profiles import native
    k = native._K
    if k is None or not hasattr(k, "ring_order") or not native.native_selected():
        return None
    return k


@port("0x1003f690", source="CADModule", status="observed",
      note="drop-in of ordering.ring_sort: the same scan and decisions in C")
def ring_sort(nodes: list, inside_out: bool = False) -> list:
    """:func:`.ordering.ring_sort` (CRingSort, modes 6 / 7) with the same picks in the same
    order, on the C kernel (``_ring.c``): the same sequential scan, each candidate decided with
    the reference's double arithmetic; ``|w|`` is libm's hypot where every decision is more than
    1e-13 from its threshold, else CPython's ``math.hypot`` (called back), as in the reference.
    The reference (the Python port) runs when the default selection did not choose the kernel
    (``MLASER_NATIVE``), and for coordinates that are not floats."""
    from . import ordering as R
    k = _ring_kernel()
    if k is None or len(nodes) < 2:
        return R.ring_sort(nodes, inside_out)
    P = [((x.bbox[0] + x.bbox[2]) / 2, (x.bbox[1] + x.bbox[3]) / 2) for x in nodes]
    x0 = min(x.bbox[0] for x in nodes); y0 = min(x.bbox[1] for x in nodes)
    x1 = max(x.bbox[2] for x in nodes); y1 = max(x.bbox[3] for x in nodes)
    n = len(nodes)
    if R.RING_TOL_FAITHFUL:
        tol = 0.1 * (x1 - x0) * (y1 - y0) / n
    else:
        tol = 0.1 * math.sqrt(max((x1 - x0) * (y1 - y0), 1e-12) / n)
    rx, ry = x0 - 1.0, y0
    vals = [v for p in P for v in p]
    if not all(type(v) is float for v in vals) or type(tol) is not float or not all(
            type(v) is float or (type(v) is int and -(1 << 53) < v < (1 << 53)) for v in (rx, ry)):
        return R.ring_sort(nodes, inside_out)
    from array import array
    out = k.ring_order(array("d", vals), tol, rx, ry, math.hypot, False)
    seq = [nodes[i] for i in out]
    return seq[::-1] if inside_out else seq


def ring_selfcheck(k: Any) -> str:
    """Known-answer check of the C ring sort ``k.ring_order`` against the reference (part of the
    kernel self-check, profiles/native.py): a 9 x 7 grid of equal parts (collinear rows, the
    near-collinear rule), a jittered line and duplicates (the tolerance box); "" or what differs."""
    from array import array
    from . import ordering as R

    class _N:
        def __init__(self, b: tuple) -> None:
            self.bbox = b

    x = 0x5DEECE66D
    sets = []
    grid = [_N((c * 30.0, r * 30.0, c * 30.0 + 24.0, r * 30.0 + 24.0)) for r in range(7) for c in range(9)]
    sets.append(grid)
    line = []
    for i in range(40):
        x = (x * 0x5DEECE66D + 11) & ((1 << 48) - 1)
        j = x / float(1 << 48) * 0.02 - 0.01
        line.append(_N((i * 3.0 + j, i * 0.3, i * 3.0 + j + 1.0, i * 0.3 + 1.0)))
    sets.append(line)
    sets.append([_N((float(i % 3) * 10.0, 0.0, float(i % 3) * 10.0 + 2.0, 2.0)) for i in range(12)])
    for nodes in sets:
        P = [((n.bbox[0] + n.bbox[2]) / 2, (n.bbox[1] + n.bbox[3]) / 2) for n in nodes]
        x0 = min(n.bbox[0] for n in nodes)
        y0 = min(n.bbox[1] for n in nodes)
        x1 = max(n.bbox[2] for n in nodes)
        y1 = max(n.bbox[3] for n in nodes)
        tol = 0.1 * math.sqrt(max((x1 - x0) * (y1 - y0), 1e-12) / len(nodes))
        want = [id(n) for n in R.ring_sort(nodes)] if not R.RING_TOL_FAITHFUL else None
        if want is None:
            return ""
        flat = array("d", [v for p in P for v in p])
        for exact in (False, True):
            got = [id(nodes[i]) for i in k.ring_order(flat, tol, x0 - 1.0, y0, math.hypot, exact)]
            if got != want:
                return f"ring sort differs from the reference ({len(nodes)} nodes)"
    return ""
