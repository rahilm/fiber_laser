"""Raster engraving as one document object (architecture.md §4.3 item 7, P2-3).

Before this module a bitmap engraving reached the document as raster G-code read back by
``mlaser.io.gcode.parse_gcode``: one :class:`~mlaser.model.Part` per burn run (an 800 x 600
dithered picture becomes 184,627 parts).  :class:`RasterEngraving` keeps the same burn runs in
numpy arrays and a single Part carries it:

* ``Part.extra['raster']`` is the :class:`RasterEngraving` (local coordinates, read-only arrays);
* ``Part.contours`` is one closed *frame* contour around the engraving (5 lines: the bbox
  rectangle with an extra vertex at a quarter of the bottom edge).  Everything that works on
  contours (bbox, selection, rubber band, snap, docops move / rotate / scale / mirror / reverse,
  nesting, array copies) sees and moves that frame; the raster's placement is read back from the
  frame's corners (:func:`placement`): identity while the frame is untouched, else the affine map
  of the frame.  A frame edited into anything that is no longer a parallelogram (node edit,
  explode, kerf ...) is refused with :class:`RasterFrameError`.  The extra vertex makes the
  corner assignment unambiguous after a reversal or a moved start point.

Toolpath (:func:`raster_motion_items`, called once from ``build_toolpath``): the MotionItems are
generated straight from the arrays and are *exactly* those that the per-segment Parts give with
``CamParams(sort_mode=0)`` (document order): per burn run ``Marker, [Rapid], [LayerChange],
Follow(True), Pierce, LaserOn, [Delay], CutLine, [Delay], LaserOff, Follow(False)``.  The only
difference is the marker's (part, contour) pair: (raster part index, run index) instead of
(run index, 0).  The raster is always cut in its own (serpentine) order, whatever the sort mode,
like the fly-cut scan parts; raster parts come after the ordinary contours and before the scan
parts.  A craft the fast path does not reproduce (kerf, gaps, micro-joints, cooling points, leads
on open contours, runs shorter than 1 µm that ``parse_gcode`` would close) falls back to the
generic per-contour pipeline on the expanded runs (identical by construction, but slow).

Saving: :func:`expand_document` turns every raster part back into today's per-segment Parts
(``nc#<i>``, default craft copied from the raster part), so ``dumps_chf(expand_document(doc))``
is byte-identical to the .chf of today's document.  Saving the document *without* expanding
writes the frame (one closed 5-line graph) and loses the engraving.  :func:`raster_from_parts`
packs such per-segment Parts (a .nc opened with ``parse_gcode`` or a .chf saved that way) back
into one raster object.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Optional, Sequence

import numpy as np

from ..model import Contour, Craft, Document, LayerParams, Line, Part, Point
from ..toolpath.items import (
    CutArc,
    CutLine,
    Delay,
    Follow,
    LaserOff,
    LaserOn,
    LayerChange,
    Marker,
    MotionItem,
    Pierce,
    Rapid,
)

if TYPE_CHECKING:  # pragma: no cover
    from .toolpath import CamParams

__all__ = [
    "RASTER_KEY",
    "FRAME_KEY",
    "RasterEngraving",
    "RasterFrameError",
    "Placement",
    "f3_values",
    "make_raster_part",
    "raster_of_part",
    "raster_part_indices",
    "placement",
    "frame_contour",
    "expand_parts",
    "expand_document",
    "raster_from_parts",
    "raster_motion_items",
    "emit_raster_parts",
    "segment_arrays",
]

RASTER_KEY = "raster"  # Part.extra key of the RasterEngraving
FRAME_KEY = "raster_frame"  # Contour.extra flag of the frame contour
CLOSE_TOL = 1e-3  # parse_gcode: a run is a closed contour when |end - start| < 1e-3
MIN_LEN = 1e-6  # prepare_contour drops contours shorter than this
_Q = 0.25  # frame marker vertex: a quarter along the bottom edge


class RasterFrameError(ValueError):
    """The frame contour of a raster part was edited into something that is not an affine
    image of the original frame (node edit, explode, kerf ...)."""


def f3_values(v) -> np.ndarray:
    """``float(format_f3(x))`` for float32 values, vectorised: the value the raster G-code text
    holds and ``parse_gcode`` reads back.  ``|x| * 1000`` is exact in float64 for a float32 x (24
    + 10 bits), so ``floor(|x| * 1000 + 0.5)`` is .NET's round half away from zero of the exact
    binary value, and ``m / 1000`` is the correctly rounded value of the decimal text; negative
    values that round to zero keep the sign (``-0.000`` -> -0.0)."""
    x = np.asarray(v, dtype=np.float32).astype(np.float64)
    m = np.floor(np.abs(x) * 1000.0 + 0.5)
    return np.copysign(m / 1000.0, x)


def _ro(a, dtype) -> np.ndarray:
    out = np.array(a, dtype=dtype, copy=True).reshape(-1)
    out.flags.writeable = False
    return out


@dataclass(eq=False)
class RasterEngraving:
    """Burn runs of a raster engraving in local coordinates (mm), in cutting order.

    Run ``i`` burns from ``(x0[i], row_y[row[i]])`` to ``(x1[i], row_y[row[i]])`` with S value
    ``power[i]``; laser off in between (overscan, white runs and row changes are rapids).  The
    values are those of the raster G-code text (``f3_values``), so the toolpath matches the
    per-segment document bit for bit.  ``rho`` is the line density (lines and pixels per mm, the
    pitch is 1/rho; None when unknown, e.g. packed from foreign G-code), ``overscan`` the side
    travel of every row, ``direction`` the scan pattern (``"serpentine"``: the first row runs +X,
    then alternating), ``params`` the generator's settings (RasterParams as a dict, informative).
    ``frame`` is the local (x_min, y_min, x_max, y_max) of the frame contour.

    The arrays are read-only and shared by ``copy.deepcopy`` (undo snapshots and array copies
    stay cheap); the placement lives in the part's frame contour.
    """

    x0: np.ndarray
    x1: np.ndarray
    row: np.ndarray
    row_y: np.ndarray
    power: np.ndarray
    rho: Optional[int] = None
    overscan: float = 5.0
    direction: str = "serpentine"
    params: dict = field(default_factory=dict)
    frame: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        self.x0 = _ro(self.x0, np.float64)
        self.x1 = _ro(self.x1, np.float64)
        self.row = _ro(self.row, np.int32)
        self.row_y = _ro(self.row_y, np.float64)
        self.power = _ro(self.power, np.int32)
        n = len(self.x0)
        if not (len(self.x1) == len(self.row) == len(self.power) == n):
            raise ValueError("x0, x1, row and power must have the same length")
        if n and (int(self.row.min()) < 0 or int(self.row.max()) >= len(self.row_y)):
            raise ValueError("row index out of range")
        if self.frame == (0.0, 0.0, 0.0, 0.0) and n:
            self.frame = self.default_frame()

    @classmethod
    def from_segments(cls, x0, x1, y, power=None, **kw) -> "RasterEngraving":
        """Pack runs given with one y per run: consecutive runs with the same y form a row."""
        y = np.asarray(y, dtype=np.float64).reshape(-1)
        n = len(y)
        if n:
            new = np.empty(n, dtype=bool)
            new[0] = True
            bits = y.view(np.int64)  # bitwise: -0.0 and 0.0 stay apart
            new[1:] = bits[1:] != bits[:-1]
            row = np.cumsum(new) - 1
            row_y = y[new]
        else:
            row, row_y = np.zeros(0, np.int32), np.zeros(0)
        p = np.ones(n, np.int32) if power is None else np.broadcast_to(np.asarray(power), (n,))
        return cls(x0, x1, row, row_y, p, **kw)

    # ------------------------------------------------------------------ queries
    def __len__(self) -> int:
        return len(self.x0)

    @property
    def pitch(self) -> Optional[float]:
        return 1.0 / self.rho if self.rho else None

    @property
    def y(self) -> np.ndarray:
        """y of every run."""
        return self.row_y[self.row]

    def default_frame(self) -> tuple[float, float, float, float]:
        """Local bbox of the runs; a single row gets one pitch (or 1 mm) of height so that the
        frame stays a proper rectangle."""
        xa = float(min(self.x0.min(), self.x1.min()))
        xb = float(max(self.x0.max(), self.x1.max()))
        ys = self.y
        ya, yb = float(ys.min()), float(ys.max())
        if yb - ya <= 1e-9:
            yb = ya + (self.pitch or 1.0)
        if xb - xa <= 1e-9:
            xb = xa + (self.pitch or 1.0)
        return (xa, ya, xb, yb)

    def __deepcopy__(self, memo) -> "RasterEngraving":
        new = copy.copy(self)  # read-only arrays are shared
        new.params = copy.deepcopy(self.params, memo)
        memo[id(self)] = new
        return new

    def __getstate__(self) -> dict:
        return dict(self.__dict__)

    def __setstate__(self, st: dict) -> None:
        self.__dict__.update(st)
        for k in ("x0", "x1", "row", "row_y", "power"):
            a = getattr(self, k)
            if isinstance(a, np.ndarray):
                a.flags.writeable = False

    def nbytes(self) -> int:
        return sum(int(getattr(self, k).nbytes) for k in ("x0", "x1", "row", "row_y", "power"))


# ---------------------------------------------------------------------- frame and placement
def frame_contour(r: RasterEngraving) -> Contour:
    """The closed frame contour (local coordinates): P0 -> Q -> P1 -> P2 -> P3 -> P0 with the
    rectangle corners P0 = (xa, ya), P1 = (xb, ya), P2 = (xb, yb), P3 = (xa, yb) and the marker
    vertex Q a quarter along P0 -> P1."""
    xa, ya, xb, yb = r.frame
    q = (xa + (xb - xa) * _Q, ya)
    pts = [(xa, ya), q, (xb, ya), (xb, yb), (xa, yb)]
    segs = [Line(pts[i], pts[(i + 1) % 5]) for i in range(5)]
    return Contour(segs, closed=True, extra={FRAME_KEY: True})


def make_raster_part(r: RasterEngraving, craft: Optional[Craft] = None, name: str = "raster") -> Part:
    """One Part holding the raster (``extra['raster']``) and its frame contour."""
    if len(r) == 0:
        raise ValueError("raster engraving without burn runs")
    part = Part(contours=[frame_contour(r)], craft=copy.deepcopy(craft) if craft else Craft(), name=name)
    part.extra[RASTER_KEY] = r
    return part


def raster_of_part(part: Part) -> Optional[RasterEngraving]:
    r = part.extra.get(RASTER_KEY) if isinstance(part.extra, dict) else None
    return r if isinstance(r, RasterEngraving) else None


def raster_part_indices(doc: Document) -> set[int]:
    return {i for i, p in enumerate(doc.parts) if raster_of_part(p) is not None}


@dataclass(frozen=True)
class Placement:
    """Affine map local -> document: ``(a*x + b*y + tx, c*x + d*y + ty)``; ``translate`` is set
    when the map is a pure translation (then ``(x + tx, y + ty)`` is used)."""

    a: float
    b: float
    c: float
    d: float
    tx: float
    ty: float
    translate: bool = False

    def apply(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.translate:
            return x + self.tx, y + self.ty
        return self.a * x + self.b * y + self.tx, self.c * x + self.d * y + self.ty

    def point(self, p: Point) -> Point:
        x, y = self.apply(np.array([p[0]]), np.array([p[1]]))
        return (float(x[0]), float(y[0]))


def _corners(c: Contour) -> tuple[Point, Point, Point, Point, Point]:
    """(P0, Q, P1, P2, P3) of a (possibly transformed, reversed or restarted) frame contour."""
    segs = c.segments
    if len(segs) != 5 or not all(isinstance(s, Line) for s in segs):
        raise RasterFrameError("raster frame edited: expected 5 straight edges")
    v = [tuple(s.start) for s in segs]
    size = max(math.dist(v[i], v[j]) for i in range(5) for j in range(i + 1, 5))
    if size <= 0:
        raise RasterFrameError("raster frame collapsed")
    for i in range(5):
        if math.dist(segs[i].end, segs[(i + 1) % 5].start) > 1e-9 * max(1.0, size):
            raise RasterFrameError("raster frame edited: edges not connected")

    def bend(k: int) -> float:
        a, b, d = v[k - 1], v[k], v[(k + 1) % 5]
        return abs((b[0] - a[0]) * (d[1] - b[1]) - (b[1] - a[1]) * (d[0] - b[0]))

    k = min(range(5), key=bend)
    prev, nxt = v[k - 1], v[(k + 1) % 5]
    if math.dist(prev, v[k]) <= math.dist(v[k], nxt):
        p0, p1, p2, p3 = prev, nxt, v[(k + 2) % 5], v[(k + 3) % 5]
    else:
        p0, p1, p2, p3 = nxt, prev, v[(k - 2) % 5], v[(k - 3) % 5]
    return p0, v[k], p1, p2, p3  # type: ignore[return-value]


def placement(part: Part) -> Optional[Placement]:
    """Placement of the part's raster from its frame contour: None while the frame is exactly
    the original one (the arrays are used as they are), else the affine map.  Raises
    :class:`RasterFrameError` when the frame is no longer an affine image of the original."""
    r = raster_of_part(part)
    if r is None:
        raise ValueError("not a raster part")
    frames = [c for c in part.contours if c.segments]
    if len(frames) != 1:
        raise RasterFrameError("raster frame edited: expected exactly one frame contour")
    P0, Q, P1, P2, P3 = _corners(frames[0])
    xa, ya, xb, yb = r.frame
    if P0 == (xa, ya) and P1 == (xb, ya) and P3 == (xa, yb) and P2 == (xb, yb):
        return None
    w, h = xb - xa, yb - ya
    ux, uy = (P1[0] - P0[0]) / w, (P1[1] - P0[1]) / w  # image of the local x unit
    vx, vy = (P3[0] - P0[0]) / h, (P3[1] - P0[1]) / h  # image of the local y unit
    size = max(math.dist(P0, P2), math.dist(P1, P3))
    tol = 1e-6 * max(1.0, size)
    if (
        math.dist(P2, (P1[0] + P3[0] - P0[0], P1[1] + P3[1] - P0[1])) > tol
        or math.dist(Q, (P0[0] + (P1[0] - P0[0]) * _Q, P0[1] + (P1[1] - P0[1]) * _Q)) > tol
    ):
        raise RasterFrameError("raster frame edited: no longer a parallelogram")
    if abs(ux * vy - uy * vx) < 1e-12:
        raise RasterFrameError("raster frame collapsed")
    if max(abs(ux - 1.0), abs(uy), abs(vx), abs(vy - 1.0)) <= 1e-12:
        return Placement(1.0, 0.0, 0.0, 1.0, P0[0] - xa, P0[1] - ya, translate=True)
    return Placement(ux, vx, uy, vy, P0[0] - ux * xa - vx * ya, P0[1] - uy * xa - vy * ya)


def segment_arrays(part: Part) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(sx, sy, ex, ey) of every run in document coordinates (placement applied)."""
    r = raster_of_part(part)
    if r is None:
        raise ValueError("not a raster part")
    y = r.y
    pl = placement(part)
    if pl is None:
        return r.x0, y, r.x1, y
    sx, sy = pl.apply(r.x0, y)
    ex, ey = pl.apply(r.x1, y)
    return sx, sy, ex, ey


# ---------------------------------------------------------------------- expansion / packing
def expand_parts(part: Part, first_index: int = 0) -> list[Part]:
    """Today's per-segment Parts of a raster part (what ``parse_gcode`` of the raster G-code
    gives): one open (closed when shorter than 1e-3) one-line contour per run, named
    ``nc#<i>``, each with a copy of the raster part's craft."""
    r = raster_of_part(part)
    if r is None:
        return [part]
    sx, sy, ex, ey = (a.tolist() for a in segment_arrays(part))
    closed = (np.abs(r.x1 - r.x0) < CLOSE_TOL).tolist()
    default = part.craft == Craft()
    out = []
    for i in range(len(sx)):
        c = Contour([Line((sx[i], sy[i]), (ex[i], ey[i]))], closed[i])
        out.append(
            Part(
                contours=[c],
                craft=Craft() if default else copy.deepcopy(part.craft),
                name=f"nc#{first_index + i}",
            )
        )
    return out


def expand_document(doc: Document) -> Document:
    """A Document with every raster part replaced by its per-segment Parts (for .chf / DXF /
    G-code export).  The other parts, the layers and ``extra`` are shared with ``doc``."""
    if not raster_part_indices(doc):
        return doc
    parts: list[Part] = []
    for p in doc.parts:
        parts += expand_parts(p, len(parts)) if raster_of_part(p) is not None else [p]
    return Document(parts=parts, layers=doc.layers, version=doc.version, extra=doc.extra)


def raster_from_parts(parts: Sequence[Part], **kw) -> Optional[RasterEngraving]:
    """Pack per-segment Parts (one open horizontal one-line contour each, default craft) into a
    RasterEngraving, or None when any part does not qualify."""
    x0, x1, ys = [], [], []
    for p in parts:
        if len(p.contours) != 1 or p.craft != Craft():
            return None
        c = p.contours[0]
        if len(c.segments) != 1 or not isinstance(c.segments[0], Line) or c.extra:
            return None
        s = c.segments[0]
        if s.start[1] != s.end[1] or c.closed != (math.dist(s.start, s.end) < CLOSE_TOL):
            return None
        x0.append(s.start[0])
        x1.append(s.end[0])
        ys.append(s.start[1])
    if not x0:
        return None
    return RasterEngraving.from_segments(x0, x1, ys, **kw)


# ---------------------------------------------------------------------- toolpath
def _plain(craft: Craft, prm: "CamParams") -> bool:
    """True when ``prepare_contour`` turns every run into exactly its own line (no kerf, gap,
    joints, cooling points or leads on open contours)."""
    if craft.kerf_comp is not None and craft.kerf_comp >= 0 and craft.kerf_width:
        return False
    if float(craft.extra.get("overcut", 0.0) or 0.0) < -0.05:
        return False
    if craft.micro_joints or craft.cool_point is not None or craft.extra.get("cool_points"):
        return False
    if prm.leads_on_open and (craft.lead_in.kind != 0 or craft.lead_out.kind != 0):
        return False
    return True


def _generic_items(
    doc: Document, pi: int, prm: "CamParams", pos: Point, layer: Optional[int]
) -> tuple[list[MotionItem], Point, Optional[int]]:
    """The per-segment pipeline itself on the expanded runs (sort mode 0, starting at ``pos``)."""
    from .toolpath import build_toolpath

    part = doc.parts[pi]
    tmp = Document(parts=expand_parts(part), layers=doc.layers)
    sub = replace(prm, sort_mode=0, small_first=False, origin=pos, return_to_origin=False, avoid_cut=False)
    items = build_toolpath(tmp, sub)
    out: list[MotionItem] = []
    first_lc = True
    for it in items:
        if isinstance(it, Marker):
            it = Marker(it.kind, pi, it.part, it.info)
        elif isinstance(it, LayerChange) and first_lc:
            first_lc = False
            if it.layer == layer:
                continue
        out.append(it)
    for it in reversed(out):
        if isinstance(it, (CutLine, CutArc)):
            pos = tuple(it.end)  # type: ignore[assignment]
            break
    lcs = [it.layer for it in out if isinstance(it, LayerChange)]
    return out, pos, (lcs[-1] if lcs else layer)


def raster_motion_items(
    doc: Document, pi: int, prm: "CamParams", pos: Point, layer: Optional[int]
) -> tuple[list[MotionItem], Point, Optional[int]]:
    """MotionItems of raster part ``pi`` starting at ``pos`` with the current process layer
    ``layer``; returns (items, new pos, new layer).  Identical to the items of the per-segment
    Parts under sort mode 0 except for the markers' (part, contour) = (pi, run index)."""
    part = doc.parts[pi]
    r = raster_of_part(part)
    if r is None or len(r) == 0:
        return [], pos, layer
    craft = part.craft
    if not _plain(craft, prm) or bool(np.any(np.abs(r.x1 - r.x0) < CLOSE_TOL)):
        return _generic_items(doc, pi, prm, pos, layer)
    L = craft.layer
    lp: LayerParams = doc.layers.get(L, LayerParams(index=L, speed=prm.default_feed))
    feed = lp.speed or prm.default_feed
    pt = prm.pierce_time_s if prm.pierce_time_s is not None else float(lp.pierce.get("time_s", 0.0))
    stages = lp.pierce.get("stages", [])
    d0, d1 = prm.start_delay_s, prm.end_delay_s
    sx, sy, ex, ey = (a.tolist() for a in segment_arrays(part))
    items: list[MotionItem] = []
    add = items.append
    dist = math.dist
    for i in range(len(sx)):
        s = (sx[i], sy[i])
        e = (ex[i], ey[i])
        if dist(s, e) < MIN_LEN:  # prepare_contour drops it (no marker either)
            continue
        add(Marker("contour", pi, i))
        if dist(pos, s) > 1e-9:
            add(Rapid(tuple(pos), s))  # type: ignore[arg-type]
        if layer != L:
            add(LayerChange(L))
            layer = L
        add(Follow(True))
        add(Pierce(s, L, pt, list(stages)))
        add(LaserOn(L))
        if d0 > 0:
            add(Delay(d0))
        add(CutLine(s, e, feed, L, True))
        if d1 > 0:
            add(Delay(d1))
        add(LaserOff())
        add(Follow(False))
        pos = e
    return items, pos, layer


def emit_raster_parts(
    doc: Document, indices, prm: "CamParams", items: list, pos: Point, layer: Optional[int]
) -> tuple[Point, Optional[int]]:
    """``build_toolpath`` hook: append the items of the raster parts ``indices`` (document order)
    to ``items``; returns the new (pos, layer)."""
    for pi in sorted(indices):
        blk, pos, layer = raster_motion_items(doc, pi, prm, pos, layer)
        items += blk
    return pos, layer
