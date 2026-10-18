"""Arrangement and selection helpers: align, size, groups, similar / inner / outer, measure.

Evidence (MainApp handlers -> ICADModule vtable -> CADModule worker):

* align (mf70, cmds 0x33c2..0x33c9, FUN_004b28c0 -> +0x138 -> 0x100c2a80): needs >= 2
  selected graphs; reference = bbox of the selection; mode 0 left, 1 right, 2 h-centre,
  3 top, 4 bottom, 5 v-centre, 6 centre; the main button 0x33c2 maps to mode 0.  [O]
* size (mf591, cmd 0x3390 size dialog FUN_005f6c10 -> +0x12c -> 0x100b9e50(anchor, sx, sy)):
  width/height in mm, "ratio lock" GRP.ZoomRatioIsLock (+0xb3e0), anchor +0xb400 in 1..9,
  default 5.  With the lock on, the changed dimension's factor is used for both.  Presets
  0x3391..0x3396 = 10/30/50/75/200/500 %, 0x3397..0x339a = 10/50/100/200 mm for the larger
  bbox side (FUN_004b20a0).  [O]
* groups (mf80..mf83, cmds 0x33fe..0x3401 FUN_004b2bf0): group +0x164 (0x100c4020,
  COpGroupGraphCmd -> CGlyGroup), explode selected +0x13c(1) / all +0x13c(0) (0x100bb940),
  select all groups = select mode 5.  In the rebuild a group is one :class:`Part` holding the
  member contours, flagged ``extra['group'] = True`` (a CGlyGroup is one selectable graph).
* select (FUN_004b1e80 -> +0xbc -> 0x100b59d0): mode 7 "similar" needs exactly one selected
  graph and selects every graph whose IGraph::IsSimilar(+0x58, tol 0.02 = 0x1011dae0) holds:
  equal length within tol, same element count, elements pairwise similar in the same or in
  reversed order (CGlyContour 0x10063540).  Mode 9 selects graphs whose contour-tree depth equals the sub
  mode (tree 0x100ad520 / 0x10022270): 0x331e (mf651 "选择外模") depth 1, 0x331f (mf652
  "选择内模") depth 2.  [O]
* measure (mf116, cmd 0x3552 -> view mode 8, clicks 0x100e9da0 with snapping, result shown
  as mv17 "X: Y: Length:" with |dx|, |dy| and distance; inch variant A250917_6).  [O]
"""
from __future__ import annotations

import copy
import math
from typing import Optional, Sequence

from mlaser.model import Arc, Contour, Document, Line, Part, Point

from .edit import _clone_part

ALIGN_MODES = ("left", "right", "hcenter", "top", "bottom", "vcenter", "center")
SIMILAR_TOL = 0.02
SIZE_PRESETS_PCT = {0x3391: 10, 0x3392: 30, 0x3393: 50, 0x3394: 75, 0x3395: 200, 0x3396: 500}
SIZE_PRESETS_MM = {0x3397: 10, 0x3398: 50, 0x3399: 100, 0x339a: 200}


def bbox(parts: Sequence[Part]) -> tuple[float, float, float, float]:
    bs = [p.bbox() for p in parts if p.contours]
    if not bs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def _map_parts(parts, f, flip=False):
    for p in parts:
        for c in p.contours:
            segs = []
            for s in c.segments:
                if isinstance(s, Line):
                    segs.append(Line(f(s.start), f(s.end)))
                else:
                    segs.append(Arc(f(s.start), f(s.end), f(s.center), (not s.ccw) if flip else s.ccw))
            c.segments = segs


def translate(parts: Sequence[Part], dx: float, dy: float) -> None:
    _map_parts(parts, lambda p: (p[0] + dx, p[1] + dy))


# ------------------------------------------------------------------ align
def align(parts: Sequence[Part], mode: int) -> int:
    """0x100c2a80; returns the number of graphs moved."""
    parts = [p for p in parts if p.contours]
    if len(parts) < 2 or not 0 <= mode < 7:
        return 0
    X0, Y0, X1, Y1 = bbox(parts)
    moved = 0
    for p in parts:
        x0, y0, x1, y1 = p.bbox()
        dx = dy = 0.0
        if mode == 0:
            dx = X0 - x0
        elif mode == 1:
            dx = X1 - x1
        elif mode in (2, 6):
            dx = (X0 + X1) / 2 - (x0 + x1) / 2
        if mode == 3:
            dy = Y1 - y1
        elif mode == 4:
            dy = Y0 - y0
        elif mode in (5, 6):
            dy = (Y0 + Y1) / 2 - (y0 + y1) / 2
        if abs(dx) > 1e-9 or abs(dy) > 1e-9:
            translate([p], dx, dy)
            moved += 1
    return moved


# ------------------------------------------------------------------ size
def anchor_point(box, anchor: int) -> Point:
    """Reference point 1..9 of a bbox, row-major from the top-left (1) to the bottom-right
    (9), 5 = centre.  (The numbering of +0xb400 is INFERRED; only 1..9 and default 5 are
    observed.)"""
    x0, y0, x1, y1 = box
    anchor = min(9, max(1, int(anchor)))
    col, row = (anchor - 1) % 3, (anchor - 1) // 3
    return (x0 + (x1 - x0) * col / 2, y1 - (y1 - y0) * row / 2)


def scale_about(parts: Sequence[Part], sx: float, sy: float, anchor: Point) -> None:
    """Non-uniform scaling; arcs stay arcs only for sx == sy (otherwise they are flattened)."""
    ax, ay = anchor
    f = lambda p: (ax + (p[0] - ax) * sx, ay + (p[1] - ay) * sy)  # noqa: E731
    if abs(abs(sx) - abs(sy)) < 1e-12:
        _map_parts(parts, f, flip=(sx * sy) < 0)
        return
    for p in parts:
        for c in p.contours:
            pts = list(c.points(0.01))
            if c.closed and len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
                pts = pts[:-1]
            q = [f(v) for v in pts]
            n = len(q)
            if c.closed:
                c.segments = [Line(q[i], q[(i + 1) % n]) for i in range(n)]
            else:
                c.segments = [Line(q[i], q[i + 1]) for i in range(n - 1)]


def size_factors(cur_w: float, cur_h: float, new_w: float, new_h: float, lock: bool) -> tuple[float, float]:
    """FUN_004b20a0 (case 0x3390): factors from the dialog's width/height."""
    sx = new_w / cur_w if cur_w > 0.01 else 1.0
    sy = new_h / cur_h if cur_h > 0.01 else 1.0
    if lock:
        if abs(sx - 1.0) > 1e-9 and abs(sx) > 1e-9:
            sy = sx
        elif abs(sy - 1.0) > 1e-9 and abs(sy) > 1e-9:
            sx = sy
        else:
            sx = sy = 1.0
    return sx, sy


def resize(parts: Sequence[Part], new_w: float, new_h: float, lock: bool = True, anchor: int = 5) -> tuple[float, float]:
    parts = [p for p in parts if p.contours]
    if not parts:
        return (1.0, 1.0)
    box = bbox(parts)
    sx, sy = size_factors(box[2] - box[0], box[3] - box[1], new_w, new_h, lock)
    scale_about(parts, sx, sy, anchor_point(box, anchor))
    return sx, sy


def preset_factor(cmd_id: int, box) -> float:
    if cmd_id in SIZE_PRESETS_PCT:
        return SIZE_PRESETS_PCT[cmd_id] / 100.0
    m = max(box[2] - box[0], box[3] - box[1])
    return SIZE_PRESETS_MM[cmd_id] / m if m > 0.01 else 1.0


# ------------------------------------------------------------------ groups
def is_group(p: Part) -> bool:
    return bool(p.extra.get("group"))


def group(doc: Document) -> Optional[Part]:
    """Group the selected parts into one Part (needs >= 2); the group takes the craft of the
    first member and replaces the members at the first member's position."""
    sel = [i for i, p in enumerate(doc.parts) if p.selected and p.contours]
    if len(sel) < 2:
        return None
    members = [doc.parts[i] for i in sel]
    g = Part(contours=[copy.deepcopy(c) for p in members for c in p.contours],
             craft=copy.deepcopy(members[0].craft), name=members[0].name or "group", selected=True,
             extra={"group": True, "members": [m.name for m in members]})
    first = sel[0]
    doc.parts = [p for i, p in enumerate(doc.parts) if i not in sel]
    doc.parts.insert(first, g)
    doc.extra.pop("order", None)
    return g


def explode_group(doc: Document, selected_only: bool = True) -> int:
    """0x100bb940(flag): break groups back into one Part per contour."""
    out, n = [], 0
    for p in doc.parts:
        if is_group(p) and (p.selected or not selected_only) and len(p.contours) >= 1:
            n += 1
            for k, c in enumerate(p.contours):
                q = _clone_part(p, [c], f"#{k}")
                q.extra.pop("members", None)
                out.append(q)
        else:
            out.append(p)
    if n:
        doc.parts = out
        doc.extra.pop("order", None)
    return n


def select_groups(doc: Document) -> int:
    n = 0
    for p in doc.parts:
        p.selected = is_group(p)
        n += p.selected
    return n


# ------------------------------------------------------------------ similar
def _seg_similar(a, b, tol: float) -> bool:
    if type(a) is not type(b):
        return False
    if abs(a.length() - b.length()) > tol:
        return False
    if isinstance(a, Arc):
        return abs(a.radius - b.radius) <= tol and abs(abs(a.sweep()) - abs(b.sweep())) <= tol
    return True


def contour_similar(a: Contour, b: Contour, tol: float = SIMILAR_TOL) -> bool:
    """CGlyContour::IsSimilar (0x10063540)."""
    if abs(a.length() - b.length()) > tol or len(a.segments) != len(b.segments):
        return False
    n = len(a.segments)
    if all(_seg_similar(a.segments[i], b.segments[i], tol) for i in range(n)):
        return True
    return all(_seg_similar(a.segments[i], b.segments[n - 1 - i], tol) for i in range(n))


def part_similar(a: Part, b: Part, tol: float = SIMILAR_TOL) -> bool:
    if len(a.contours) != len(b.contours):
        return False
    if len(a.contours) == 1:
        return contour_similar(a.contours[0], b.contours[0], tol)
    used = set()
    for ca in a.contours:
        for j, cb in enumerate(b.contours):
            if j not in used and contour_similar(ca, cb, tol):
                used.add(j)
                break
        else:
            return False
    return True


def select_similar(doc: Document, tol: float = SIMILAR_TOL) -> int:
    sel = [p for p in doc.parts if p.selected]
    if len(sel) != 1:
        return 0
    ref = sel[0]
    n = 0
    for p in doc.parts:
        if p is not ref and part_similar(ref, p, tol):
            p.selected = True
            n += 1
    return n


# ------------------------------------------------------------------ inner / outer
def contour_depths(doc: Document) -> dict[tuple[int, int], int]:
    """Nesting depth of every closed contour over the whole document (1 = outermost),
    like CContourTopTree / 0x100ad520.  Open contours get depth 0."""
    from shapely.geometry import Polygon
    items = []
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            if c.closed and c.segments:
                pts = list(c.points(0.05))
                if len(pts) >= 3:
                    try:
                        poly = Polygon(pts).buffer(0)
                    except Exception:  # noqa: BLE001
                        continue
                    items.append(((pi, ci), poly, abs(poly.area)))
    depth = {}
    for key, poly, area in items:
        rp = poly.representative_point()
        d = 1
        for key2, poly2, area2 in items:
            if key2 != key and area2 > area and poly2.contains(rp):
                d += 1
        depth[key] = d
    for pi, p in enumerate(doc.parts):
        for ci, _c in enumerate(p.contours):
            depth.setdefault((pi, ci), 0)
    return depth


def select_by_topology(doc: Document, sub: int) -> int:
    """Select mode 9 (0x100b5da6): a graph is selected iff its topology depth (graph+0x88,
    written by the contour tree walk 0x10022270, 1 = outermost) equals ``sub``.  0x331e
    (mf651, 外模 "outer mould") passes 1, 0x331f (mf652, 内模 "inner mould") passes 2; the
    English captions in lang.txt are swapped.  A rebuild part is selected if one of its
    contours has that depth."""
    dep = contour_depths(doc)
    n = 0
    for pi, p in enumerate(doc.parts):
        p.selected = any(dep.get((pi, ci), 0) == sub for ci in range(len(p.contours)))
        n += p.selected
    return n


# ------------------------------------------------------------------ measure
def measure(p0: Point, p1: Point) -> tuple[float, float, float]:
    """mv17: (|dx|, |dy|, distance)."""
    return abs(p1[0] - p0[0]), abs(p1[1] - p0[1]), math.dist(p0, p1)


# ------------------------------------------------------------------ fine-tune step
def auto_step(extent: float) -> float:
    """Keyboard fine-tuning step when IGP.AutoCalcKeyboardMoveStepLength is on (FUN_004d0930):
    v = 0.02 * selection extent (width for left/right, height for up/down), normalised by
    decades to [1, 10], step = max(1, round(v)) * decade."""
    v = 0.02 * extent
    m = 1.0
    if extent >= 50.0:
        while v >= 10.0:
            v /= 10.0
            m *= 10.0
    else:
        while v < 1.0 and v > 0:
            v *= 10.0
            m /= 10.0
        if v <= 0:
            return 1.0
    k = int(round(v))
    return max(1, k) * m
