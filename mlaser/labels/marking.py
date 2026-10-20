"""Part-ID etching: single-stroke text inside each part, on a low-power marking layer.

Parts from several orders on one sheet must be sorted after cutting; etching the part id (or
order number) on each part makes that possible without labels (review 2 features P2-4).

* :data:`FONT` is a single-stroke font of this project (A-Z, 0-9, ``- _ . / # : ?`` and
  space; lower case is drawn as upper case, any other character as ``?``) on a 4 x 6 grid:
  open polylines, so the beam traces each stroke once.
* :func:`place_text` finds the largest text height (between ``min_height`` and ``max_height``)
  that fits inside the part with ``margin`` mm of clearance from the outline and from every hole
  (shapely: the part's outer polygon shrunk by ``margin`` minus its holes grown by ``margin``;
  candidate centres are the pole of inaccessibility and a grid sorted by clearance; 0° and 90°).
  A part with no room is skipped and reported, never marked across an edge.
* :func:`etch_document` adds the strokes to each part as open contours with their own
  :class:`~mlaser.model.Craft` on the marking layer (no lead-in / lead-out, no kerf).  They lie
  inside the outer contour, so the cut order (inner contours first) etches them before the part
  is cut free.  :func:`remove_marking` takes them out again.

The marking layer's parameters are not chosen here: the caller passes the layer
(``LayerParams``) built from a marking technology (the UI plugin, ``mlaser.ui.ext_labels``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from mlaser.model import Craft, Contour, LeadParams, Line, Segment

#: the colour of the marking layer the etch commands add (document data: the layer's colour)
MARK_LAYER_COLOR = "#ff00ff"

Stroke = list[tuple[float, float]]

_O = [(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]
_P = [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]


def _dot(x: float, y: float, r: float = 0.25) -> Stroke:
    return [(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r), (x - r, y - r)]


#: glyph strokes on a 4 (wide) x 6 (cap height) grid, baseline y = 0
FONT: dict[str, list[Stroke]] = {
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]],
    "B": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)]],
    "C": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "D": [[(0, 0), (0, 6), (2, 6), (4, 4), (4, 2), (2, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 6), (3, 6)], [(2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "J": [[(4, 6), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 2)], [(1, 3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [list(_O)],
    "P": [list(_P)],
    "Q": [list(_O), [(2, 2), (4, 0)]],
    "R": [list(_P), [(2, 3), (4, 0)]],
    "S": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 3), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": [list(_O), [(0, 1), (4, 5)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (2, 3)], [(3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "6": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3), (0, 3)]],
    "7": [[(0, 6), (4, 6), (1, 0)]],
    "8": [[(1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 2), (0, 1), (1, 0), (3, 0),
           (4, 1), (4, 2), (3, 3)]],
    "9": [[(0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3), (4, 3)]],
    "-": [[(0.5, 3), (3.5, 3)]],
    "_": [[(0, 0), (4, 0)]],
    ".": [_dot(2, 0.25)],
    "/": [[(0, 0), (4, 6)]],
    "#": [[(1, 0), (1, 6)], [(3, 0), (3, 6)], [(0, 2), (4, 2)], [(0, 4), (4, 4)]],
    ":": [_dot(2, 1.5), _dot(2, 4.5)],
    "?": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (2, 3), (2, 2)], _dot(2, 0.25)],
    " ": [],
}
CELL_W, CELL_H, ADVANCE = 4.0, 6.0, 6.0     # grid units: glyph width, cap height, advance


def normalise(text: str) -> str:
    """Upper case; characters the font lacks become ``?``."""
    return "".join(ch if ch in FONT else "?" for ch in str(text).upper())


def text_strokes(text: str, height: float = 6.0) -> list[Stroke]:
    """Strokes of ``text`` with cap height ``height`` mm, baseline at y = 0, starting at x = 0."""
    s = height / CELL_H
    out: list[Stroke] = []
    for i, ch in enumerate(normalise(text)):
        for st in FONT[ch]:
            out.append([((i * ADVANCE + x) * s, y * s) for x, y in st])
    return out


def text_size(text: str, height: float) -> tuple[float, float]:
    """Width and height (mm) of ``text`` at cap height ``height``."""
    n = len(normalise(text))
    return ((max(0, n) * ADVANCE - (ADVANCE - CELL_W)) * height / CELL_H if n else 0.0, height)


# ============================================================================== placement
@dataclass
class Placement:
    center: tuple[float, float]
    angle: float                         # degrees (0 or 90)
    height: float                        # cap height, mm
    strokes: list[Stroke] = field(default_factory=list)


def part_region(part: Any, margin: float) -> Any:
    """The area of ``part`` where marks may go: its largest closed contour shrunk by ``margin``,
    minus every other closed contour (holes) grown by ``margin`` and every open contour (an
    earlier mark, an open cut) grown by ``margin``.  A shapely geometry (may be empty)."""
    from shapely.geometry import LineString, Polygon
    from shapely.ops import unary_union

    polys, opens = [], []
    for c in getattr(part, "contours", []) or []:
        pts = list(c.points(0.1))
        if c.closed and len(pts) >= 3:
            p = Polygon(pts)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty:
                polys.append(p)
        elif len(pts) >= 2:
            opens.append(LineString(pts))
    if not polys:
        return Polygon()
    polys.sort(key=lambda p: p.area, reverse=True)
    outer = polys[0]
    region = outer.buffer(-margin)
    cut = [p.buffer(margin) for p in polys[1:]] + [ln.buffer(margin) for ln in opens]
    if cut:
        region = region.difference(unary_union(cut))
    return region


def _candidates(region: Any, n: int = 12) -> list[tuple[float, float]]:
    from shapely.geometry import Point
    from shapely.ops import polylabel

    geoms = list(getattr(region, "geoms", [region]))
    pts: list[tuple[float, float]] = []
    for g in sorted(geoms, key=lambda g: g.area, reverse=True):
        try:
            c = polylabel(g, tolerance=0.1)
            pts.append((c.x, c.y))
        except Exception:  # noqa: BLE001 - a degenerate polygon
            pass
        x0, y0, x1, y1 = g.bounds
        for i in range(n):
            for j in range(n):
                pts.append((x0 + (i + 0.5) * (x1 - x0) / n, y0 + (j + 0.5) * (y1 - y0) / n))
    boundary = region.boundary
    scored = [(boundary.distance(Point(p)), p) for p in pts if region.contains(Point(p))]
    scored.sort(key=lambda t: -t[0])
    return [p for _d, p in scored[:60]]


def _rect(cx: float, cy: float, w: float, h: float, angle: float) -> Any:
    from shapely.geometry import Polygon
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return Polygon([(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts])


def place_text(part: Any, text: str, min_height: float = 2.0, max_height: float = 10.0, margin: float = 1.0,
               angles: Sequence[float] = (0.0, 90.0), steps: int = 10) -> Optional[Placement]:
    """The placement of ``text`` in ``part`` with the largest cap height that fits (see the
    module docstring), or None when even ``min_height`` does not fit."""
    from shapely.prepared import prep

    text = normalise(text).strip()
    if not text:
        return None
    region = part_region(part, margin)
    if region.is_empty:
        return None
    pr = prep(region)
    cands = _candidates(region)
    if not cands:
        return None
    steps = max(1, int(steps))
    heights = [max_height - (max_height - min_height) * k / steps for k in range(steps + 1)] if max_height > min_height \
        else [min_height]
    for h in heights:
        w, hh = text_size(text, h)
        for angle in angles:
            for cx, cy in cands:
                if pr.contains(_rect(cx, cy, w, hh, angle)):
                    return Placement((cx, cy), float(angle), h, _placed(text, h, cx, cy, angle))
    return None


def _placed(text: str, h: float, cx: float, cy: float, angle: float) -> list[Stroke]:
    w, hh = text_size(text, h)
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for st in text_strokes(text, h):
        out.append([(cx + (x - w / 2) * ca - (y - hh / 2) * sa, cy + (x - w / 2) * sa + (y - hh / 2) * ca)
                    for x, y in st])
    return out


# ============================================================================== document
def marking_craft(layer: int) -> Craft:
    """The craft of a marking stroke: the marking layer, no leads, no kerf."""
    return Craft(layer=int(layer), kerf_comp=-1, kerf_width=0.0, lead_in=LeadParams(kind=0, length=0.0),
                 lead_out=LeadParams(kind=0, length=0.0))


def stroke_contours(strokes: Sequence[Stroke], layer: int) -> list[Contour]:
    out = []
    for st in strokes:
        segs: list[Segment] = [Line((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
                               for a, b in zip(st, st[1:]) if math.dist(a, b) > 1e-6]
        if segs:
            out.append(Contour(segs, closed=False, craft=marking_craft(layer)))
    return out


def is_marking(c: Any, layer: int) -> bool:
    cr = getattr(c, "craft", None)
    return not getattr(c, "closed", True) and cr is not None and int(getattr(cr, "layer", -1)) == int(layer)


def remove_marking(document: Any, layer: int, parts: Optional[Sequence[int]] = None) -> int:
    """Remove the marking strokes (open contours on ``layer``); returns how many."""
    n = 0
    for pi, p in enumerate(document.parts):
        if parts is not None and pi not in parts:
            continue
        keep = [c for c in p.contours if not is_marking(c, layer)]
        n += len(p.contours) - len(keep)
        p.contours = keep
    return n


def default_text(pi: int, part: Any) -> str:
    """The part's name (or its nest part id + copy, or its index)."""
    name = str(getattr(part, "name", "") or "")
    if name:
        return name
    nest = (getattr(part, "extra", None) or {}).get("nest")
    if isinstance(nest, dict) and "part_id" in nest:
        return f"P{nest['part_id']}"
    return f"P{pi + 1}"


@dataclass
class EtchResult:
    marked: list[tuple[int, str, float, float]] = field(default_factory=list)   # (part, text, height, angle)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)          # (part, text, reason)
    contours: int = 0


def etch_document(document: Any, layer: int, text_of: Callable[[int, Any], str] = default_text,
                  min_height: float = 2.0, max_height: float = 10.0, margin: float = 1.0,
                  angles: Sequence[float] = (0.0, 90.0), parts: Optional[Sequence[int]] = None,
                  replace: bool = True, layer_params: Any = None) -> EtchResult:
    """Etch ``text_of(index, part)`` into every part (or the indices in ``parts``).  Earlier
    marks on ``layer`` are replaced (``replace``).  ``layer_params``: stored as
    ``document.layers[layer]`` when given (the marking technology)."""
    res = EtchResult()
    if layer_params is not None:
        document.layers[int(layer)] = layer_params
    for pi, p in enumerate(document.parts):
        if parts is not None and pi not in parts:
            continue
        extra = getattr(p, "extra", None) or {}
        if not p.contours or "nest_sheet" in extra or getattr(p.craft, "layer", 1) == 0 or \
                getattr(p, "raster", None) is not None:
            continue
        if replace:
            p.contours = [c for c in p.contours if not is_marking(c, layer)]
        text = normalise(text_of(pi, p)).strip()
        if not text:
            res.skipped.append((pi, text, "no text"))
            continue
        pl = place_text(p, text, min_height, max_height, margin, angles)
        if pl is None:
            res.skipped.append((pi, text, "no room"))
            continue
        cs = stroke_contours(pl.strokes, layer)
        p.contours = cs + list(p.contours)
        res.marked.append((pi, text, pl.height, pl.angle))
        res.contours += len(cs)
    return res


__all__ = ["FONT", "normalise", "text_strokes", "text_size", "Placement", "part_region", "place_text",
           "marking_craft", "stroke_contours", "is_marking", "remove_marking", "default_text", "EtchResult",
           "etch_document"]
