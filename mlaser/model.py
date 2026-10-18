"""Core document model shared by every subsystem.

Units are millimetres, angles radians unless a field says otherwise.
Geometry is stored as contours of Line/Arc segments; arcs use start/end/centre
plus direction so they round-trip both the .chf format and G-code (G2/G3).
Subsystems may extend these classes (add fields with defaults) but must not
rename or remove existing fields without updating every user.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Iterator, Optional, Union

Point = tuple[float, float]

# Stable identities (Wave 2 services, docs/dev/ARCHITECTURE.md "services"): every Part and Contour
# gets a process-unique ``uid`` when it is created.  ``copy.deepcopy`` keeps it (an undo snapshot
# is the same object in an earlier state); a copy that becomes a *second* object in a document
# (paste, array) gets a fresh one from :func:`ensure_unique_ids`.  ``uid`` takes no part in
# equality or repr, so comparisons and file formats are unchanged.
_ids = itertools.count(1)


def new_id() -> int:
    """A new process-unique object id (thread safe: ``next`` on a count is atomic)."""
    return next(_ids)


@dataclass
class Line:
    start: Point
    end: Point

    def length(self) -> float:
        return math.dist(self.start, self.end)

    def reversed(self) -> "Line":
        return Line(self.end, self.start)


@dataclass
class Arc:
    start: Point
    end: Point
    center: Point
    ccw: bool = True

    @property
    def radius(self) -> float:
        return math.dist(self.center, self.start)

    def sweep(self) -> float:
        """Signed sweep angle (positive = CCW). Full circle when start == end."""
        a0 = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        a1 = math.atan2(self.end[1] - self.center[1], self.end[0] - self.center[0])
        d = a1 - a0
        if self.ccw:
            while d <= 1e-12:
                d += 2 * math.pi
        else:
            while d >= -1e-12:
                d -= 2 * math.pi
        return d

    def length(self) -> float:
        return abs(self.sweep()) * self.radius

    def reversed(self) -> "Arc":
        return Arc(self.end, self.start, self.center, not self.ccw)

    def point_at(self, t: float) -> Point:
        a0 = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        a = a0 + self.sweep() * t
        r = self.radius
        return (self.center[0] + r * math.cos(a), self.center[1] + r * math.sin(a))


Segment = Union[Line, Arc]



class GlyphKind(IntEnum):
    """Source glyph kinds (IEditableGlyph type +8, CADModule glyph factory FUN_10062860;
    docs/cam.md §0).  The values are the .chf ``####Gly`` type numbers."""
    POINT = 1
    LINE = 2            # "segment"
    ARC = 3
    CIRCLE = 4
    ELLIPSE = 5         # ellipse arc
    LWPOLY = 6          # lwpolyline with bulges
    SPLINE = 7


GLYPH_POINT, GLYPH_LINE, GLYPH_ARC, GLYPH_CIRCLE, GLYPH_ELLIPSE, GLYPH_LWPOLY, GLYPH_SPLINE = GlyphKind


@dataclass
class Glyph:
    """One source primitive as the original's CAD kernel keeps it (a ``####Gly`` record in .chf).

    ``Contour.segments`` stays the flattened Line/Arc geometry every subsystem uses; a contour
    may additionally carry the glyphs it was made from (``Contour.extra['glyphs']``, see
    :meth:`Contour.set_source_glyphs`) so that writers can reproduce the original primitive kinds
    (one lwpolyline with bulges, a spline, a circle) instead of the flattened pieces.

    ``data`` uses the .chf field names (docs/formats.md §2.8): point ``p``; line ``start``/``end``;
    arc ``center``/``radius``/``a0``/``a1`` (radians, CCW from a0 to a1); circle ``center``/``radius``
    (starts at angle 0); ellipse ``center``/``axis``/``ratio``/``t0``/``t1``; lwpoly ``flag``
    (1 = closed) and ``verts`` = [((x, y), bulge)]; spline ``ctrl``/``knots`` (cubic,
    len(knots) == len(ctrl) + 4) and the two opaque ints ``u0``/``u1``.
    ``dir`` is -1 when the contour traverses the glyph backwards.
    """
    kind: int
    data: dict = field(default_factory=dict)
    dir: int = 1


@dataclass
class LeadParams:
    """Lead-in / lead-out ("GuideCurve Para" in .chf)."""
    kind: int = 0            # 0 none, 1 line, 2 arc, 3 line+arc (see docs/cam.md)
    angle_deg: float = 90.0
    length: float = 2.0
    radius: float = 1.0
    at_corner: bool = False


@dataclass
class Craft:
    """Per-part cutting attributes ("<Crafts>" block in .chf)."""
    layer: int = 1                       # process layer index (colour / parameter set)
    kerf_comp: int = -1                  # -1 none, 0 inner, 1 outer (see docs/formats.md)
    kerf_width: float = 0.0
    lead_in: LeadParams = field(default_factory=LeadParams)
    lead_out: LeadParams = field(default_factory=lambda: LeadParams(kind=0))
    micro_joints: list[float] = field(default_factory=list)  # arc-length positions
    cool_point: Optional[Point] = None
    extra: dict = field(default_factory=dict)  # unparsed/opaque fields preserved for round-trip


@dataclass
class Contour:
    segments: list[Segment] = field(default_factory=list)
    closed: bool = True
    extra: dict = field(default_factory=dict)  # format-specific data (e.g. .chf contour record)
    uid: int = field(default_factory=new_id, compare=False, repr=False)

    def points(self, tol: float = 0.05) -> Iterator[Point]:
        """Flatten to a polyline (for display / geometry ops)."""
        if not self.segments:
            return
        yield self.segments[0].start
        for s in self.segments:
            if isinstance(s, Line):
                yield s.end
            else:
                n = max(2, int(math.ceil(s.length() / max(tol, 1e-6) ** 0.5 / 2)))
                n = min(n, 720)
                for i in range(1, n + 1):
                    yield s.point_at(i / n)

    def length(self) -> float:
        return sum(s.length() for s in self.segments)

    def start_point(self) -> Point:
        return self.segments[0].start

    def set_source_glyphs(self, glyphs: list) -> None:
        """Attach the source glyphs of the current ``segments`` (see :class:`Glyph`)."""
        import copy
        self.extra["glyphs"] = list(glyphs)
        self.extra["glyph_segments"] = copy.deepcopy(self.segments)

    def source_glyphs(self) -> Optional[list]:
        """The attached source glyphs, or None when there are none or ``segments`` has been
        changed since they were attached (the glyphs no longer describe the geometry)."""
        g = self.extra.get("glyphs")
        if g and self.extra.get("glyph_segments") == self.segments:
            return g
        return None


@dataclass
class Part:
    """One drawable object ("graph" / "Group elem" in .chf)."""
    contours: list[Contour] = field(default_factory=list)
    craft: Craft = field(default_factory=Craft)
    name: str = ""
    selected: bool = False
    extra: dict = field(default_factory=dict)
    uid: int = field(default_factory=new_id, compare=False, repr=False)

    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for c in self.contours:
            for x, y in c.points():
                xs.append(x); ys.append(y)
        if not xs:
            return (0, 0, 0, 0)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class LayerParams:
    """Cutting process parameters for one layer (speed, power, gas, height...)."""
    index: int = 1
    color: str = "#ffffff"
    speed: float = 100.0          # mm/s
    power_pct: float = 100.0
    frequency_hz: float = 5000.0
    duty_pct: float = 100.0
    gas: int = 0
    gas_pressure: float = 0.0
    cut_height: float = 1.0
    pierce: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Document:
    parts: list[Part] = field(default_factory=list)
    layers: dict[int, LayerParams] = field(default_factory=dict)
    version: int = 9
    extra: dict = field(default_factory=dict)

    def bbox(self):
        bs = [p.bbox() for p in self.parts if p.contours]
        if not bs:
            return (0, 0, 0, 0)
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def ensure_unique_ids(doc: "Document", keep: Iterable = ()) -> int:
    """Give every Part and Contour of ``doc`` a uid no other object in it has.

    Objects in ``keep`` (the objects that held their uid before an edit) keep it; any other
    object whose uid is already taken (a deep copy of a part that is still in the document)
    gets a new one.  Returns the number of ids reassigned."""
    kept = {id(o) for o in keep}
    n = 0
    for attr in ("parts", "contours"):
        objs = list(doc.parts) if attr == "parts" else [c for p in doc.parts for c in p.contours]
        seen: set[int] = set()
        for o in objs:                       # the kept objects claim their uid first
            if id(o) in kept:
                seen.add(o.uid)
        for o in objs:
            if id(o) in kept:
                continue
            if o.uid in seen:
                o.uid = new_id()
                n += 1
            seen.add(o.uid)
    return n
