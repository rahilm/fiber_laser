"""Import cleanup: overlap/duplicate removal, joining, small-object removal, arc fitting.

Evidence (docs/cam.md §15). The import post-processor is ``CGly2ContourEng`` (CADModule
``0x10061c90``, called from the CCADModule import ``0x100f0e10`` via ``0x100b1220``) with an
option block that MainApp fills from the "IGP." import parameters (``0x004d35c0``)::

    +0x00 IGP.MegerConnectGraphType  pd367  0 none, 1 direction first, 2 length first, 3 distance first
    +0x08 IGP.ConnectGate            pd368  0.1   (join tolerance, also the contours' closing tol)
    +0x10 IGP.IsFilteOverlapGraph    pd365  1
    +0x18 IGP.OverlapGate            pd366  0.1
    +0x25 IGP.IsAutoFilterMicoGraph  pd363  1
    +0x28 IGP.MicoGraphGate          pd364  0.1
    +0x30 IGP.IsAutoSmoothGraph      pd369  0
    +0x38 IGP.SmoothAccuracy         pd370  0.05

Order in 0x10061c90 (O): overlap removal on the loose glyphs (0x10061190, tolerance
min(ConnectGate, OverlapGate), 20x20 spatial grid) -> chaining (type 0: one contour per
glyph; type 1: 0x1005f7d0; else 0x10076a20 per contour) -> small-object removal
(0x1005c150; gate MicoGraphGate if enabled, else 0.01) -> open-contour split (0x1005c2f0, U)
-> optional smoothing (0x10075820, accuracy) -> orientation / bbox refresh.

* **Small objects** (0x1005c150, O): a contour is deleted when max(bbox w, h) < gate; a
  contour made of a single point glyph is kept. (The lang label says "smallest length" but
  the test is on the bbox extent.)
* **Duplicate contours** (manual "Clear duplicate lines" mf471, vtable +0xd8 -> 0x100c0a30, O):
  contour i is removed when its sample points (decimated so consecutive kept points differ
  by >= 5*gate in x or y) all lie on another contour j whose bbox overlaps i's bbox
  expanded by the gate; closed contours must also match j's length within 10*gate.
* **Joining** (0x10076a20, O): ends match when |dx| <= tol and |dy| <= tol (a square, not a
  circle). With several candidates: type 1 takes the best tangent continuation (max dot of
  directions), type 2 the shortest candidate (I: the x87 compare is inverted in the
  pseudocode; the 1e8 initial value implies a minimum), type 3 the nearest end point.
* **Arc fitting**: CircleFitDLL ``CCircleFit::mainFit(MyPoint*, first, last, CdlFitOut*)``
  (0x10001010 -> 0x100011e0), used by Dxf2Grp ``0x1000eec0`` on runs of line glyphs with
  Ang_CircleFit = pi/10, Err_CircleFit = caller tolerance, Len_CircleFit = 10 (DLL
  defaults 0.02 / pi/10 / 100 / Rmax 5000). Ported statement by statement (O), see
  docs/cam.md §15: duplicate removal (1e-6), point tangents (0x10002420), whole-set circle
  test (0x10002af0), then a loop over runs of monotonically decreasing (fitted on a reversed
  copy, 0x10002620 / 0x100028a0) or increasing tangents whose chord turn stays <= Ang, each
  run of more than 4 points fitted by 0x100019e0 (biarc 0x100020b0 grown while the points
  are within Err, 0x10003100, then shrunk until the chord midpoints are within Err,
  0x10002eb0, radii <= Rmax, ratio <= 50 and the circles nest within Err; else the longest
  line); otherwise a straight run (chord deviation 0x10002680 <= Err, turn <= Ang).
* The CADModule smoother itself (0x10075820 -> 0x1004b8e0) converts runs of short glyphs
  into splines (CEditableSpline); its fit is U. The rebuild uses the CircleFit approach for
  ``smooth_contour`` so the result stays Line/Arc.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..model import Arc, Contour, Document, Line, Part, Point, Segment
from ..parity_ports import dispatch

__all__ = ["CleanupParams", "remove_small", "remove_duplicate_contours", "remove_overlaps",
           "merge_connected", "close_gaps", "fit_arcs", "smooth_contour", "cleanup_contours",
           "cleanup_document", "FitParams"]



@dataclass
class CleanupParams:
    """Import parameters "IGP." (ManuPara.xml defaults)."""
    remove_small: bool = True          # pd363
    small_gate: float = 0.1            # pd364  [0.01, 10]
    remove_overlap: bool = True        # pd365
    overlap_gate: float = 0.1          # pd366  [0.01, 0.5]
    merge_type: int = 1                # pd367  0 none, 1 direction, 2 length, 3 distance
    connect_gate: float = 0.1          # pd368  [0.01, 0.5]
    smooth: bool = False               # pd369
    smooth_accuracy: float = 0.05      # pd370  [0.01, 10]
    close_gap: float = 0.0             # rebuild addition: close open contours up to this gap


# --------------------------------------------------------------------------- small helpers
def _bbox_of(c: Contour):
    pts = list(c.points(0.05))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _seg_dist(p: Point, s: Segment) -> float:
    """Distance from a point to a Line or Arc segment."""
    if isinstance(s, Line):
        (x0, y0), (x1, y1) = s.start, s.end
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / L2))
        return math.hypot(p[0] - (x0 + t * dx), p[1] - (y0 + t * dy))
    r = s.radius
    ang = math.atan2(p[1] - s.center[1], p[0] - s.center[0])
    a0 = math.atan2(s.start[1] - s.center[1], s.start[0] - s.center[0])
    sw = s.sweep()
    d = (ang - a0) % (2 * math.pi) if sw > 0 else (a0 - ang) % (2 * math.pi)
    if d <= abs(sw) + 1e-12:
        return abs(math.dist(p, s.center) - r)
    return min(math.dist(p, s.start), math.dist(p, s.end))


def _contour_dist(p: Point, c: Contour) -> float:
    return min(_seg_dist(p, s) for s in c.segments)


# --------------------------------------------------------------------------- small objects
def remove_small(contours: Sequence[Contour], gate: float) -> list[Contour]:
    """0x1005c150: drop contours whose larger bbox side is below ``gate``."""
    out = []
    for c in contours:
        if not c.segments:
            continue
        x0, y0, x1, y1 = _bbox_of(c)
        if max(x1 - x0, y1 - y0) < gate:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- duplicates
def _samples(c: Contour, gate: float) -> list[Point]:
    pts = list(c.points(min(0.05, gate)))
    if not pts:
        return []
    keep = [pts[0]]
    step = 5.0 * gate                      # 0x100c0a30: decimation with DAT_1011f428 = 5
    for q in pts[1:]:
        if abs(q[0] - keep[-1][0]) >= step or abs(q[1] - keep[-1][1]) >= step:
            keep.append(q)
    if math.dist(keep[-1], pts[-1]) > 1e-9:
        keep.append(pts[-1])
    # also probe segment midpoints so a short duplicate is not judged by its ends only
    for s in c.segments:
        keep.append(s.point_at(0.5) if isinstance(s, Arc) else
                    ((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2))
    return keep


def remove_duplicate_contours(contours: Sequence[Contour], gate: float = 0.1) -> list[Contour]:
    """0x100c0a30 "Clear Duplicate Lines": remove contours lying on another contour."""
    cs = [c for c in contours if c.segments]
    alive = [True] * len(cs)
    boxes = [_bbox_of(c) for c in cs]
    lens = [c.length() for c in cs]
    for i in range(len(cs) - 1, -1, -1):
        bi = boxes[i]
        samp = None
        for j in range(len(cs) - 1, -1, -1):
            if j == i or not alive[j]:
                continue
            if cs[i].closed and abs(lens[i] - lens[j]) > 10.0 * gate:
                continue
            bj = boxes[j]
            if bj[0] > bi[2] + gate or bj[2] < bi[0] - gate or bj[1] > bi[3] + gate or bj[3] < bi[1] - gate:
                continue
            if lens[i] > lens[j] + 10.0 * gate:
                continue                    # a longer contour cannot lie on a shorter one
            samp = samp or _samples(cs[i], gate)
            if all(_contour_dist(p, cs[j]) <= gate for p in samp):
                alive[i] = False
                break
    return [c for c, a in zip(cs, alive) if a]


# --------------------------------------------------------------------------- overlaps (glyph level)
def _line_key(s: Line, tol: float):
    (x0, y0), (x1, y1) = s.start, s.end
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    if ux < -1e-12 or (abs(ux) <= 1e-12 and uy < 0):
        ux, uy = -ux, -uy
    off = -uy * x0 + ux * y0                 # signed distance of the carrier line from origin
    return ux, uy, off


def remove_overlaps(segs: Iterable[Segment], tol: float = 0.1) -> list[Segment]:
    """Glyph-level overlap removal (role of 0x10061190; details I).

    Collinear lines (direction within tol/length, carrier offset within tol) and co-circular
    arcs (centre and radius within tol) are merged so every stretch of geometry occurs once.
    Disjoint pieces stay separate; direction of the first piece of a group is kept.
    """
    lines: list[Line] = []
    arcs: list[Arc] = []
    for s in segs:
        if s.length() <= 1e-9:
            continue
        (lines if isinstance(s, Line) else arcs).append(s)
    out: list[Segment] = []
    # ---- lines
    used = [False] * len(lines)
    keys = [_line_key(s, tol) for s in lines]
    for i, s in enumerate(lines):
        if used[i]:
            continue
        ux, uy, off = keys[i]
        group = [i]
        for j in range(i + 1, len(lines)):
            if used[j]:
                continue
            vx, vy, o2 = keys[j]
            Lj = lines[j].length()
            if abs(ux * vy - uy * vx) * max(Lj, s.length()) <= tol and abs(o2 - off) <= tol:
                group.append(j)
        for j in group:
            used[j] = True
        # project to the carrier and union the intervals
        ref = s.start
        iv = []
        for j in group:
            a = (lines[j].start[0] - ref[0]) * ux + (lines[j].start[1] - ref[1]) * uy
            b = (lines[j].end[0] - ref[0]) * ux + (lines[j].end[1] - ref[1]) * uy
            iv.append((min(a, b), max(a, b)))
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        fwd = ((s.end[0] - s.start[0]) * ux + (s.end[1] - s.start[1]) * uy) > 0
        for a, b in merged:
            p = (ref[0] + ux * a, ref[1] + uy * a)
            q = (ref[0] + ux * b, ref[1] + uy * b)
            out.append(Line(p, q) if fwd else Line(q, p))
    # ---- arcs
    used = [False] * len(arcs)
    for i, s in enumerate(arcs):
        if used[i]:
            continue
        group = [i]
        for j in range(i + 1, len(arcs)):
            if not used[j] and math.dist(arcs[j].center, s.center) <= tol and abs(arcs[j].radius - s.radius) <= tol:
                group.append(j)
        for j in group:
            used[j] = True
        if len(group) == 1:
            out.append(s)
            continue
        c, r = s.center, s.radius
        iv = []
        full = False
        for j in group:
            a = arcs[j]
            sw = a.sweep()
            a0 = math.atan2(a.start[1] - c[1], a.start[0] - c[0])
            if abs(abs(sw) - 2 * math.pi) < 1e-9:
                full = True
                break
            lo = a0 if sw > 0 else a0 + sw
            lo %= 2 * math.pi
            iv.append((lo, lo + abs(sw)))
        if full:
            p0 = (c[0] + r, c[1])
            out.append(Arc(p0, p0, c, s.ccw))
            continue
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1] + 1e-9:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        # wrap-around: the last interval may run past 2pi onto the first one
        while len(merged) > 1 and merged[-1][1] - 2 * math.pi >= merged[0][0] - 1e-9:
            a, b = merged.pop()
            merged[0] = (a, max(b, merged[0][1] + 2 * math.pi))
        res = [(a, min(b, a + 2 * math.pi)) for a, b in merged]
        for a, b in res:
            if b - a >= 2 * math.pi - 1e-9:
                p0 = (c[0] + r, c[1])
                out.append(Arc(p0, p0, c, s.ccw))
                continue
            pa = (c[0] + r * math.cos(a), c[1] + r * math.sin(a))
            pb = (c[0] + r * math.cos(b), c[1] + r * math.sin(b))
            out.append(Arc(pa, pb, c, True) if s.ccw else Arc(pb, pa, c, False))
    return out


# --------------------------------------------------------------------------- joining
def _tangent_start(s: Segment) -> Point:
    if isinstance(s, Line):
        d = (s.end[0] - s.start[0], s.end[1] - s.start[1])
    else:
        rx, ry = s.start[0] - s.center[0], s.start[1] - s.center[1]
        d = (-ry, rx) if s.ccw else (ry, -rx)
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _tangent_end(s: Segment) -> Point:
    if isinstance(s, Line):
        d = (s.end[0] - s.start[0], s.end[1] - s.start[1])
    else:
        rx, ry = s.end[0] - s.center[0], s.end[1] - s.center[1]
        d = (-ry, rx) if s.ccw else (ry, -rx)
    n = math.hypot(*d) or 1.0
    return (d[0] / n, d[1] / n)


def _near(p: Point, q: Point, tol: float) -> bool:
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol     # square test (0x10076a20)


def _with_start(s: Segment, p: Point) -> Segment:
    return Line(p, s.end) if isinstance(s, Line) else Arc(p, s.end, s.center, s.ccw)


def _with_end(s: Segment, p: Point) -> Segment:
    return Line(s.start, p) if isinstance(s, Line) else Arc(s.start, p, s.center, s.ccw)


def _pieces(items) -> list[list[Segment]]:
    out = []
    for it in items:
        if isinstance(it, Contour):
            if it.segments:
                out.append(list(it.segments))
        else:
            out.append([it])
    return out


def merge_connected(items: Iterable, tol: float = 0.1, mode: int = 1) -> list[Contour]:
    """Join segments/open contours whose ends meet within ``tol`` (0x10076a20).

    ``items`` may mix Segments and Contours; closed contours pass through unchanged.
    ``mode``: 0 no joining, 1 direction first, 2 length (shortest) first, 3 distance first.
    A chain is closed when its end meets its start within ``tol`` (the new contour's
    closing tolerance is the gate, ctor 0x1006c2d0(tol)).
    """
    items = list(items)
    closed = [it for it in items if isinstance(it, Contour) and it.closed and it.segments]
    pool = _pieces([it for it in items if not (isinstance(it, Contour) and it.closed)])
    if mode == 0:
        return closed + [Contour(p, _near(p[0].start, p[-1].end, tol) and len(p) > 1) for p in pool]
    used = [False] * len(pool)
    plen = [sum(s.length() for s in p) for p in pool]
    out = list(closed)

    def candidates(pt: Point, at_end: bool):
        res = []
        for k, p in enumerate(pool):
            if used[k]:
                continue
            if _near(p[0].start, pt, tol):
                res.append((k, False))            # attach forward
            elif _near(p[-1].end, pt, tol):
                res.append((k, True))             # attach reversed
        return res

    def pick(res, pt: Point, tan: Point, forward: bool):
        if len(res) == 1 or not res:
            return res[0] if res else None
        best, bv = None, None
        for k, rev in res:
            p = pool[k]
            if mode == 1:
                if forward:
                    t = _tangent_start(p[0]) if not rev else tuple(-x for x in _tangent_end(p[-1]))
                else:
                    t = _tangent_end(p[-1]) if not rev else tuple(-x for x in _tangent_start(p[0]))
                v = -(t[0] * tan[0] + t[1] * tan[1])          # minimise -dot = maximise dot
            elif mode == 2:
                v = plen[k]
            else:
                q = (p[-1].end if rev else p[0].start)
                v = math.dist(q, pt)
            if bv is None or v < bv:
                best, bv = (k, rev), v
        return best

    for i, p in enumerate(pool):
        if used[i]:
            continue
        used[i] = True
        chain = list(p)
        # forward
        while not _near(chain[-1].end, chain[0].start, tol) or len(chain) == 1 and isinstance(chain[0], Line):
            res = [r for r in candidates(chain[-1].end, True)]
            sel = pick(res, chain[-1].end, _tangent_end(chain[-1]), True)
            if sel is None:
                break
            k, rev = sel
            used[k] = True
            segs = pool[k] if not rev else [s.reversed() for s in reversed(pool[k])]
            segs = list(segs)
            segs[0] = _with_start(segs[0], chain[-1].end)
            chain.extend(segs)
        # backward
        if not _near(chain[-1].end, chain[0].start, tol):
            while True:
                res = []
                for k, q in enumerate(pool):
                    if used[k]:
                        continue
                    if _near(q[-1].end, chain[0].start, tol):
                        res.append((k, False))
                    elif _near(q[0].start, chain[0].start, tol):
                        res.append((k, True))
                sel = pick(res, chain[0].start, _tangent_start(chain[0]), False)
                if sel is None:
                    break
                k, rev = sel
                used[k] = True
                segs = pool[k] if not rev else [s.reversed() for s in reversed(pool[k])]
                segs = list(segs)
                segs[-1] = _with_end(segs[-1], chain[0].start)
                chain = segs + chain
                if _near(chain[-1].end, chain[0].start, tol):
                    break
        is_closed = (len(chain) > 1 or isinstance(chain[0], Arc)) and _near(chain[-1].end, chain[0].start, tol)
        if is_closed:
            chain[-1] = _with_end(chain[-1], chain[0].start)
        out.append(Contour(chain, is_closed))
    return out


def close_gaps(contours: Sequence[Contour], max_gap: float) -> list[Contour]:
    """Rebuild addition: close open contours whose ends are within ``max_gap`` with a line."""
    out = []
    for c in contours:
        if not c.closed and c.segments and len(c.segments) >= 1:
            g = math.dist(c.segments[-1].end, c.segments[0].start)
            if g <= max_gap and c.length() > 2 * g:
                segs = list(c.segments)
                if g > 1e-9:
                    segs.append(Line(segs[-1].end, segs[0].start))
                out.append(Contour(segs, True, dict(c.extra)))
                continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- CircleFit port
# CCircleFit::mainFit is a port in the parity fence (mlaser.parity_ports.circlefit.fit);
# fit_arcs dispatches through the active ParityProfile (``arcfit`` slot)
from ..parity_ports.circlefit.fit import (  # noqa: E402,F401
    ERR_CIRCLEFIT, ANG_CIRCLEFIT, RMAX_CIRCLEFIT, RADIUS_RATIO, DUP_EPS, FitParams, _TWO_PI,
    _HALF_PI, _ang, _unwrap_pair, _Pts, _circle_test, _biarc_dll, _arc_err, _fit_ccw, _mainfit,
    _oriented_arc, _records_to_segments)

fit_arcs = dispatch("arcfit", "fit_arcs")


def smooth_contour(c: Contour, accuracy: float = 0.05, min_lines: int = 3,
                   fp: Optional[FitParams] = None) -> Contour:
    """Replace runs of >= ``min_lines`` consecutive Lines by fitted lines/arcs (CircleFit
    approach used by Dxf2Grp 0x1000eec0; Err = ``accuracy``). Arcs are kept as they are."""
    fp = fp or FitParams(err=accuracy)
    segs = list(c.segments)
    if not segs:
        return c
    all_lines = all(isinstance(s, Line) for s in segs)
    if all_lines and c.closed and len(segs) >= min_lines:
        pts = [segs[0].start] + [s.end for s in segs]
        new = fit_arcs(pts, fp, closed=True)
        return Contour(new, True, dict(c.extra)) if new else c
    out: list[Segment] = []
    run: list[Line] = []

    def flush():
        if len(run) >= min_lines:
            out.extend(fit_arcs([run[0].start] + [s.end for s in run], fp))
        else:
            out.extend(run)
        run.clear()
    for s in segs:
        if isinstance(s, Line):
            run.append(s)
        else:
            flush()
            out.append(s)
    flush()
    for k in range(1, len(out)):
        out[k] = _with_start(out[k], out[k - 1].end)
    if c.closed and out:
        out[-1] = _with_end(out[-1], out[0].start)
    return Contour(out, c.closed, dict(c.extra))


# --------------------------------------------------------------------------- pipeline
def cleanup_contours(contours: Sequence[Contour], prm: Optional[CleanupParams] = None) -> list[Contour]:
    """Run the 0x10061c90 pipeline on a contour list (open contours are exploded and re-joined)."""
    prm = prm or CleanupParams()
    closed = [c for c in contours if c.closed and c.segments]
    loose: list[Segment] = [s for c in contours if not c.closed for s in c.segments]
    if prm.remove_overlap:
        tol = min(prm.connect_gate, prm.overlap_gate)
        loose = remove_overlaps(loose, tol)
        closed = remove_duplicate_contours(closed, prm.overlap_gate)
    joined = merge_connected(closed + loose, prm.connect_gate, prm.merge_type)
    if prm.remove_overlap:
        joined = remove_duplicate_contours(joined, prm.overlap_gate)
    if prm.close_gap > 0:
        joined = close_gaps(joined, prm.close_gap)
    joined = remove_small(joined, prm.small_gate if prm.remove_small else 0.01)
    if prm.smooth:
        joined = [smooth_contour(c, prm.smooth_accuracy) for c in joined]
    return joined


def cleanup_document(doc: Document, prm: Optional[CleanupParams] = None,
                     only_selected: bool = False) -> dict:
    """Clean the document in place: one Part per resulting contour (craft of the first source
    part is reused). Returns counts {'before', 'after'}."""
    prm = prm or CleanupParams()
    parts = [p for p in doc.parts if p.selected] if only_selected else list(doc.parts)
    if not parts:
        return {"before": 0, "after": 0}
    keep = [p for p in doc.parts if p not in parts]
    before = sum(len(p.contours) for p in parts)
    import copy as _copy
    craft = _copy.deepcopy(parts[0].craft)
    res = cleanup_contours([c for p in parts for c in p.contours], prm)
    new = []
    for c in res:
        q = Part(contours=[c], craft=_copy.deepcopy(craft))
        new.append(q)
    doc.parts = keep + new
    doc.extra.pop("order", None)
    return {"before": before, "after": len(res)}
