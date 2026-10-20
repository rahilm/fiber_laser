"""Import check and repair of a drawing (Qt-free): the "Import review" wizard's logic.

:func:`analyze` lists what would go wrong at the machine, as :class:`Finding` s:

=====================  ===================================================  ===================
kind                   what                                                 fix (option)
=====================  ===================================================  ===================
``open_contour``       a contour whose ends do not meet, with the gap       join ends within
                       (mm) and the distance to the nearest other open end   ``join_tol``, then
                                                                            close gaps up to
                                                                            ``close_gap``
``duplicate``          a contour lying on another one (cut twice)           removed
``overlap``            open lines/arcs lying on each other (doubled length)  merged
``zero_length``        zero-length segments or contours                     removed
``tiny_segment``       segments shorter than ``tiny_segment``               merged into the
                                                                            neighbours
``tiny_object``        contours smaller than ``tiny_object`` (bbox)         removed
``self_intersection``  a closed contour that crosses itself                 not automatic
``text``               DXF TEXT/MTEXT that is not converted to outlines     converted (bundled
                                                                            font)
=====================  ===================================================  ===================

:func:`repair` applies the selected fixes **in place** (the caller wraps it in one undoable
edit, ``ctx.edit``); :func:`preview` runs it on a copy and reports what would change, plus the
lines that close gaps (for the canvas).  The fixes reuse the import cleanup of
:mod:`mlaser.cam.cleanup` (``merge_connected``, ``close_gaps``, ``remove_duplicate_contours``,
``remove_overlaps``, ``remove_small``).  The DxfParse-faithful import is not changed: this is a
separate step after it.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from mlaser.model import Contour, Document, Line, Part, Point, Segment

KINDS = ("open_contour", "duplicate", "overlap", "zero_length", "tiny_segment", "tiny_object",
         "self_intersection", "text")
#: plural labels (the UI translates them by ``repair.kind.<kind>``)
KIND_TEXT = {"open_contour": "open contours", "duplicate": "duplicate contours",
             "overlap": "overlapping lines", "zero_length": "zero-length geometry",
             "tiny_segment": "tiny segments", "tiny_object": "tiny objects",
             "self_intersection": "self-intersections", "text": "text not converted"}

__all__ = ["RepairOptions", "Finding", "RepairReport", "analyze", "repair", "preview", "KINDS",
           "KIND_TEXT", "open_contours_summary"]


@dataclass
class RepairOptions:
    join_tol: float = 0.1            # mm: join open ends this close (IGP.ConnectGate default)
    close_gap: float = 0.5           # mm: close an open contour whose own gap is at most this
    dup_gate: float = 0.1            # mm: duplicate / overlap tolerance (IGP.OverlapGate)
    tiny_segment: float = 0.01       # mm
    tiny_object: float = 0.1         # mm (IGP.MicoGraphGate)
    fix_open: bool = True
    fix_duplicates: bool = True
    fix_overlaps: bool = True
    fix_zero: bool = True
    fix_tiny_segments: bool = True
    fix_tiny_objects: bool = True
    convert_text: bool = True

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RepairOptions":
        return cls(**{k: v for k, v in dict(d or {}).items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Finding:
    kind: str
    part: int = -1                   # document part index (-1: not a part, e.g. text)
    contour: int = -1
    value: float = 0.0               # gap (mm), length (mm), count ...
    where: Optional[Point] = None    # where to look (for "zoom to")
    fixable: bool = True
    message: str = ""


@dataclass
class RepairReport:
    findings: list[Finding] = field(default_factory=list)
    contours: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings

    def of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.kind] = out.get(f.kind, 0) + 1
        return out

    @property
    def fixable(self) -> int:
        return sum(1 for f in self.findings if f.fixable)

    def summary(self) -> str:
        c = self.counts()
        if not c:
            return "no problems found"
        return ", ".join(f"{n} {KIND_TEXT[k]}" for k, n in c.items())


# ============================================================================== helpers
def _seg_len(s: Segment) -> float:
    try:
        return float(s.length())
    except Exception:  # noqa: BLE001
        return math.dist(s.start, s.end)


def _items(doc: Document) -> list[tuple[int, int, Contour]]:
    return [(pi, ci, c) for pi, p in enumerate(doc.parts) for ci, c in enumerate(p.contours) if c.segments]


def _bbox(c: Contour) -> tuple[float, float, float, float]:
    pts = list(c.points(0.05))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _is_point(c: Contour) -> bool:
    """A point graph (DXF POINT, drawn points): a deliberate zero-length object."""
    from mlaser.model import GLYPH_POINT
    g = c.glyphs
    if g and all(getattr(x, "kind", None) == GLYPH_POINT for x in g):
        return True
    return bool(dict.get(c.extra, "point")) if isinstance(c.extra, dict) else False


def _layer_key(p: Part) -> tuple:
    return (int(getattr(p.craft, "layer", 0)), str((p.extra or {}).get("layer", "")))


def _text_done(doc: Document) -> set:
    return {(str((p.extra or {}).get("text", "")), str((p.extra or {}).get("dxf_type", "")))
            for p in doc.parts if (p.extra or {}).get("text") is not None and p.contours}


def _unconverted_texts(doc: Document) -> list[dict]:
    texts = doc.extra.get("texts") or []
    done = _text_done(doc)
    out = []
    for t in texts:
        if not isinstance(t, dict) or not str(t.get("text", "")).strip():
            continue
        if (str(t.get("text", "")), str(t.get("type", ""))) not in done:
            out.append(t)
    return out


def _placed(doc: Document, p) -> Optional[Point]:
    if p is None:
        return None
    off = doc.extra.get("placement_offset") or (0.0, 0.0)
    try:
        return (float(p[0]) + float(off[0]), float(p[1]) + float(off[1]))
    except (TypeError, IndexError, ValueError):
        return None


def _self_intersection(c: Contour) -> Optional[Point]:
    from shapely.geometry import LinearRing, Polygon
    from shapely.validation import explain_validity
    pts: list[Point] = []
    for q in c.points(0.05):
        if not pts or math.dist(q, pts[-1]) > 1e-9:
            pts.append(q)
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) <= 1e-9:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    try:
        if LinearRing(pts).is_simple:
            return None
        m = re.search(r"\[([-0-9.eE+]+)\s+([-0-9.eE+]+)", explain_validity(Polygon(pts)))
        return (float(m.group(1)), float(m.group(2))) if m else pts[0]
    except Exception:  # noqa: BLE001 - degenerate ring: reported by zero_length instead
        return None


def _open_ends(items) -> list[tuple[int, Point]]:
    """(item index, point) of both ends of every open contour."""
    ends = []
    for k, (_pi, _ci, c) in enumerate(items):
        if not c.closed:
            ends.append((k, c.segments[0].start))
            ends.append((k, c.segments[-1].end))
    return ends


# ============================================================================== analyse
#: above this many open segments the overlap check (quadratic) is skipped, so the review
#: after opening a huge exploded drawing stays fast; the repair still merges overlaps
MAX_OVERLAP_SEGMENTS = 4000


def analyze(doc: Document, opts: Optional[RepairOptions] = None, max_pairs: int = 200_000) -> RepairReport:
    """Every :class:`Finding` of ``doc`` (nothing is changed).  ``max_pairs`` bounds the
    duplicate test (pairs of contours with overlapping boxes)."""
    from mlaser.cam.cleanup import remove_duplicate_contours, remove_overlaps
    o = opts or RepairOptions()
    rep = RepairReport()
    items = _items(doc)
    rep.contours = len(items)
    # ---- open contours, with the gap and the nearest other open end
    radius = max(o.join_tol, o.close_gap, 1.0)        # the nearest other end is looked for this far
    grid: dict[tuple[int, int], list[tuple[int, Point]]] = {}
    for kk, q in _open_ends(items):
        grid.setdefault((math.floor(q[0] / radius), math.floor(q[1] / radius)), []).append((kk, q))

    def nearest(k: int, p: Point) -> float:
        gx, gy = math.floor(p[0] / radius), math.floor(p[1] / radius)
        best = math.inf
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for kk, q in grid.get((gx + dx, gy + dy), ()):
                    if kk != k:
                        best = min(best, math.dist(p, q))
        return best if best <= radius else math.inf

    for k, (pi, ci, c) in enumerate(items):
        if c.closed or _is_point(c):
            continue
        a, b = c.segments[0].start, c.segments[-1].end
        gap = math.dist(a, b)
        near = min(nearest(k, a), nearest(k, b))
        fix = o.fix_open and (near <= o.join_tol or (gap <= o.close_gap and c.length() > 2 * gap))
        msg = f"part {pi + 1} contour {ci + 1} is open: gap {gap:.3f} mm"
        if near < math.inf:
            msg += f", nearest open end {near:.3f} mm"
        rep.findings.append(Finding("open_contour", pi, ci, gap, a, fix, msg))
    # ---- duplicates: a contour lying on an earlier one (bbox candidates first)
    if 1 < len(items):
        import shapely
        boxes = [_bbox(c) for _pi, _ci, c in items]
        g = o.dup_gate
        tree = shapely.STRtree(shapely.box(*zip(*[(b[0] - g, b[1] - g, b[2] + g, b[3] + g) for b in boxes])))
        qi, ti = tree.query(shapely.box(*zip(*boxes)))
        pairs = sorted({(int(j), int(i)) for i, j in zip(qi.tolist(), ti.tolist()) if j < i})
        dead: set[int] = set()
        for n, (j, i) in enumerate(pairs):
            if n > max_pairs:
                break
            if i in dead or j in dead:
                continue
            ci_, cj_ = items[i][2], items[j][2]
            if len(remove_duplicate_contours([cj_, ci_], g)) < 2:
                dead.add(i)
                pi, cc, c = items[i]
                rep.findings.append(Finding("duplicate", pi, cc, c.length(), c.segments[0].start, o.fix_duplicates,
                                            f"part {pi + 1} contour {cc + 1} lies on part {items[j][0] + 1} "
                                            f"contour {items[j][1] + 1} (cut twice)"))
    # ---- overlapping open geometry
    segs = [s for _pi, _ci, c in items if not c.closed for s in c.segments]
    if 1 < len(segs) <= MAX_OVERLAP_SEGMENTS:
        before = sum(_seg_len(s) for s in segs)
        after = sum(_seg_len(s) for s in remove_overlaps(segs, o.dup_gate))
        if before - after > max(o.dup_gate, 1e-6):
            first = next(((pi, ci, c) for pi, ci, c in items if not c.closed), None)
            rep.findings.append(Finding("overlap", first[0] if first else -1, first[1] if first else -1,
                                        before - after, first[2].segments[0].start if first else None,
                                        o.fix_overlaps, f"{before - after:.2f} mm of open lines lie on each other"))
    # ---- zero length, tiny segments, tiny objects, self-intersections
    for pi, ci, c in items:
        if _is_point(c):
            continue
        lens = [_seg_len(s) for s in c.segments]
        total = sum(lens)
        if total <= 1e-9:
            rep.findings.append(Finding("zero_length", pi, ci, 0.0, c.segments[0].start, o.fix_zero,
                                        f"part {pi + 1} contour {ci + 1} has zero length"))
            continue
        zeros = sum(1 for v in lens if v <= 1e-9)
        if zeros:
            rep.findings.append(Finding("zero_length", pi, ci, float(zeros), c.segments[0].start, o.fix_zero,
                                        f"part {pi + 1} contour {ci + 1}: {zeros} zero-length segment(s)"))
        tiny = [k for k, v in enumerate(lens) if 1e-9 < v < o.tiny_segment]
        if tiny and len(c.segments) > len(tiny) + 1:
            rep.findings.append(Finding("tiny_segment", pi, ci, float(len(tiny)), c.segments[tiny[0]].start,
                                        o.fix_tiny_segments,
                                        f"part {pi + 1} contour {ci + 1}: {len(tiny)} segment(s) shorter than "
                                        f"{o.tiny_segment:g} mm"))
        x0, y0, x1, y1 = _bbox(c)
        if max(x1 - x0, y1 - y0) < o.tiny_object:
            rep.findings.append(Finding("tiny_object", pi, ci, max(x1 - x0, y1 - y0), (x0, y0), o.fix_tiny_objects,
                                        f"part {pi + 1} contour {ci + 1} is smaller than {o.tiny_object:g} mm"))
        elif c.closed:
            hit = _self_intersection(c)
            if hit is not None:
                rep.findings.append(Finding("self_intersection", pi, ci, 0.0, hit, False,
                                            f"part {pi + 1} contour {ci + 1} crosses itself near "
                                            f"({hit[0]:.2f}, {hit[1]:.2f}): fix it in the CAD program"))
    # ---- text that is not converted to outlines
    can = _text_family_ok()
    for t in _unconverted_texts(doc):
        rep.findings.append(Finding("text", -1, -1, float(t.get("height", 0.0) or 0.0),
                                    _placed(doc, t.get("insert")), can and o.convert_text,
                                    f"{t.get('type', 'TEXT')} \"{str(t.get('text', ''))[:40]}\" is not converted "
                                    "to outlines (it would not be cut)"))
    return rep


def _text_family_ok() -> bool:
    try:
        from mlaser.io.dxf_import import _text_family
        return bool(_text_family())
    except Exception:  # noqa: BLE001
        return False


def open_contours_summary(doc: Document) -> tuple[int, int]:
    """(open contours, parts with one) of a document (the pre-flight's one-line summary)."""
    n, parts = 0, set()
    for pi, p in enumerate(doc.parts):
        for c in p.contours:
            if c.segments and not c.closed and not _is_point(c):
                n += 1
                parts.add(pi)
    return n, len(parts)


# ============================================================================== repair
def _drop_short(c: Contour, tiny: float, zero: bool, small: bool) -> tuple[Optional[Contour], int, int]:
    """Remove zero-length segments and merge segments shorter than ``tiny`` into a neighbour
    (a line neighbour is stretched; an arc keeps its centre).  Returns (contour or None when
    nothing is left, zero-length removed, tiny merged)."""
    from mlaser.cam.cleanup import _with_end, _with_start
    segs = list(c.segments)
    out: list[Segment] = []
    nz = nt = 0
    pending: Optional[Point] = None             # start of a dropped first segment
    for s in segs:
        L = _seg_len(s)
        if zero and L <= 1e-9:
            nz += 1
            continue
        if small and L < tiny and len(segs) - nz - nt > 2:
            nt += 1
            if out and isinstance(out[-1], Line):
                out[-1] = Line(out[-1].start, s.end)
            elif out:
                out[-1] = _with_end(out[-1], s.end)
            else:
                pending = s.start
            continue
        if out and math.dist(out[-1].end, s.start) > 1e-12:
            s = _with_start(s, out[-1].end)
        elif not out and pending is not None:
            s = _with_start(s, pending)
        out.append(s)
        pending = None
    if not out:
        return None, nz, nt
    if nz == 0 and nt == 0:
        return c, 0, 0
    if c.closed and math.dist(out[-1].end, out[0].start) > 1e-12:
        if isinstance(out[-1], Line) or not isinstance(out[0], Line):
            out[-1] = _with_end(out[-1], out[0].start)
        else:
            out[0] = _with_start(out[0], out[-1].end)
    q = Contour(out, c.closed, dict(c.extra))
    q.craft = c.craft
    return q, nz, nt


def _clusters(members: list, reach: float) -> list[list]:
    """``members`` ((part, contour) pairs) split into groups whose boxes, grown by ``reach``,
    touch (union-find over an STRtree query)."""
    if len(members) < 2:
        return [members] if members else []
    import shapely
    boxes = [_bbox(c) for _p, c in members]
    tree = shapely.STRtree(shapely.box(*zip(*[(b[0] - reach, b[1] - reach, b[2] + reach, b[3] + reach)
                                              for b in boxes])))
    qi, ti = tree.query(shapely.box(*zip(*boxes)))
    parent = list(range(len(members)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, j in zip(qi.tolist(), ti.tolist()):
        ri, rj = find(int(i)), find(int(j))
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    out: dict[int, list] = {}
    for k, m in enumerate(members):
        out.setdefault(find(k), []).append(m)
    return list(out.values())


def _repair_open(doc: Document, members: list, o: RepairOptions, done: dict, close_gaps, merge_connected,
                 remove_overlaps) -> None:
    owner = members[0][0]
    opened = [c for _p, c in members]
    work: list = list(opened)
    changed = False
    if o.fix_overlaps and len(opened) > 1:
        segs_in = [s for c in opened for s in c.segments]
        merged = remove_overlaps(segs_in, o.dup_gate)
        if sum(_seg_len(s) for s in segs_in) - sum(_seg_len(s) for s in merged) > max(o.dup_gate, 1e-6):
            done["overlaps"] += 1
            work, changed = list(merged), True
    if o.fix_open:
        res = merge_connected(work, o.join_tol, 1) if len(work) > 1 or changed else list(work)
        res = close_gaps(res, o.close_gap)
        n_closed = sum(1 for r in res if r.closed)
        if not changed and len(res) == len(opened) and n_closed == 0:
            return                                    # nothing to do: keep the originals
        done["joined"] += max(0, len(opened) - len(res)) if not changed else 0
        done["closed"] += n_closed
        final = res
    elif changed:
        final = merge_connected(work, 1e-6, 1)
    else:
        return
    ids = {id(c) for c in opened}
    for p in doc.parts:
        if any(id(c) in ids for c in p.contours):
            p.contours = [c for c in p.contours if id(c) not in ids]
    owner.contours.extend(final)


def repair(doc: Document, opts: Optional[RepairOptions] = None) -> dict[str, int]:
    """Apply the fixes of ``opts`` to ``doc`` in place.  Returns what was done:
    ``closed``, ``joined``, ``duplicates``, ``overlaps``, ``zero_length``, ``tiny_segments``,
    ``tiny_objects``, ``text``, and ``open_left`` (open contours that remain)."""
    from mlaser.cam.cleanup import close_gaps, merge_connected, remove_overlaps, remove_small
    o = opts or RepairOptions()
    done = {k: 0 for k in ("closed", "joined", "duplicates", "overlaps", "zero_length", "tiny_segments",
                           "tiny_objects", "text", "open_left")}
    # 1. zero-length and tiny segments
    for p in doc.parts:
        keep = []
        for c in p.contours:
            if not c.segments or _is_point(c):
                keep.append(c)
                continue
            if o.fix_zero and sum(_seg_len(s) for s in c.segments) <= 1e-9:
                done["zero_length"] += 1
                continue
            q, nz, nt = _drop_short(c, o.tiny_segment, o.fix_zero, o.fix_tiny_segments)
            done["zero_length"] += nz
            done["tiny_segments"] += nt
            if q is not None:
                keep.append(q)
        p.contours = keep
    # 2. duplicates (the later contour of a pair goes)
    if o.fix_duplicates:
        rep = analyze(doc, RepairOptions(dup_gate=o.dup_gate, fix_open=False, convert_text=False))
        dead = {(f.part, f.contour) for f in rep.of("duplicate")}
        for pi, p in enumerate(doc.parts):
            n0 = len(p.contours)
            p.contours = [c for ci, c in enumerate(p.contours) if (pi, ci) not in dead]
            done["duplicates"] += n0 - len(p.contours)
    # 3. open contours, per layer and per cluster of touching boxes: overlaps merged, ends
    #    joined within the tolerance, gaps closed (clusters keep the joining near-linear on
    #    large exploded drawings: merge_connected compares every pair it is given)
    if o.fix_open or o.fix_overlaps:
        groups: dict[tuple, list[tuple[Part, Contour]]] = {}
        for p in doc.parts:
            for c in p.contours:
                if c.segments and not c.closed and not _is_point(c):
                    groups.setdefault(_layer_key(p), []).append((p, c))
        reach = max(o.join_tol, o.dup_gate, 1e-6)
        for members in groups.values():
            for cluster in _clusters(members, reach):
                _repair_open(doc, cluster, o, done, close_gaps, merge_connected, remove_overlaps)
    # 4. tiny objects
    if o.fix_tiny_objects:
        for p in doc.parts:
            keep = []
            for c in p.contours:
                if c.segments and not _is_point(c) and not remove_small([c], o.tiny_object):
                    done["tiny_objects"] += 1
                    continue
                keep.append(c)
            p.contours = keep
    # 5. text to outlines
    if o.convert_text:
        done["text"] = _convert_texts(doc)
    doc.parts = [p for p in doc.parts if p.contours]
    doc.extra.pop("order", None)
    done["open_left"] = open_contours_summary(doc)[0]
    return done


def _convert_texts(doc: Document) -> int:
    from mlaser.io.dxf_import import TEXT_LAYER, _text_contours, _text_family, _tt_text_contours
    from mlaser.model import Craft
    fam = _text_family()
    if not fam:
        return 0
    n = 0
    for t in _unconverted_texts(doc):
        tt = dict(t)
        p = _placed(doc, t.get("insert"))
        if p is None:
            continue
        tt["insert"] = p
        try:
            cs = _tt_text_contours(tt, fam)
            if cs is None:
                cs = _text_contours(tt, fam)
        except Exception:  # noqa: BLE001 - this text stays unconverted (listed again)
            cs = []
        if cs:
            doc.parts.append(Part(contours=cs, craft=Craft(layer=TEXT_LAYER), name="text",
                                  extra={"layer": t.get("layer", ""), "dxf_type": t.get("type", "TEXT"),
                                         "text": t.get("text", "")}))
            n += 1
    return n


def preview(doc: Document, opts: Optional[RepairOptions] = None) -> tuple[dict[str, int], list[list[Point]]]:
    """What :func:`repair` would do (on a copy) and the new closing / joining lines as
    polylines (for the canvas preview)."""
    work = copy.deepcopy(doc)
    old = [(s.start, s.end) for p in doc.parts for c in p.contours for s in c.segments]
    done = repair(work, opts)
    oldset = {(round(a[0], 6), round(a[1], 6), round(b[0], 6), round(b[1], 6)) for a, b in old}
    lines: list[list[Point]] = []
    for p in work.parts:
        for c in p.contours:
            for s in c.segments:
                if isinstance(s, Line):
                    k = (round(s.start[0], 6), round(s.start[1], 6), round(s.end[0], 6), round(s.end[1], 6))
                    if k not in oldset and _seg_len(s) <= max((opts or RepairOptions()).close_gap, 1.0) * 2:
                        lines.append([s.start, s.end])
    return done, lines


def findings_where(findings: Iterable[Finding]) -> list[Point]:
    return [f.where for f in findings if f.where is not None]
