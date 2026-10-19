"""Remnant stock: the unused part of a cut sheet kept as a polygon with material and thickness.

Qt-free (docs/review/ux.md P2-3, wireframe §4.3).

* :func:`remnant_geometry` computes the leftover of a sheet after its parts are cut.  The
  default ``"staircase"`` mode is what an operator can actually separate from the skeleton: the
  shadow of every placed part towards the nesting corner (gravity) is taken as used, so the
  remnant boundary is a staircase of straight cuts, not the lace between the parts.  ``"free"``
  keeps every gap wider than ``min_width`` (for a camera or manual check).  Slivers narrower than
  ``min_width`` are removed (a morphological opening) and pieces smaller than ``min_area`` are
  dropped.
* :class:`RemnantStore` keeps the stock in ``remnants.json`` (atomic replace): id (``R-0001``),
  outline rings, material, thickness, location, status ``available`` -> ``reserved`` (sent to the
  queue on a nest) -> ``used`` (its sheet was cut), timestamps and the source job.
* :meth:`Remnant.nest_sheet` turns a remnant into a :class:`mlaser.nest.NestSheet` with its true
  outline (holes are defects the nester avoids), so remnants are offered in nesting.

Outline coordinates are millimetres with the lower-left of the bounding box at (0, 0).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

Ring = list[list[float]]
STATUSES = ("available", "reserved", "used")
GRAVITIES = ("left", "bottom", "right", "top")


# ============================================================================== geometry
def _shapely():
    import shapely
    from shapely.geometry import MultiPolygon, Polygon, box
    from shapely.ops import unary_union
    return shapely, Polygon, MultiPolygon, box, unary_union


def polygons(geom: Any) -> list:
    _, Polygon, MultiPolygon, _, _ = _shapely()
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _sweep(poly: Any, v: tuple[float, float]) -> Any:
    """The region ``poly`` covers when moved by ``t * v`` for t in [0, 1] (exact for any simple
    polygon: the polygon, its moved copy and the parallelogram every edge sweeps)."""
    shapely, Polygon, _, _, _ = _shapely()
    from shapely import affinity
    ext = Polygon(poly.exterior)
    pieces = [ext, affinity.translate(ext, v[0], v[1])]
    pts = list(ext.exterior.coords)
    for a, b in zip(pts, pts[1:]):
        quad = Polygon([a, b, (b[0] + v[0], b[1] + v[1]), (a[0] + v[0], a[1] + v[1])])
        if quad.area > 1e-9:
            pieces.append(quad if quad.is_valid else quad.buffer(0))
    return shapely.union_all(pieces)


def shadow(used: Any, sheet_bounds: tuple[float, float, float, float], gravity: str = "left",
           tol: float = 0.5) -> Any:
    """The used region as an operator separates it: every placed piece swept to the sheet edge
    it was nested against (``gravity``), so the boundary is a staircase of straight cuts."""
    _, _, _, _, unary_union = _shapely()
    x0, y0, x1, y1 = sheet_bounds
    swept = []
    for p in polygons(used):
        q = p.simplify(tol) if tol > 0 else p
        if q.is_empty:
            continue
        bx0, by0, bx1, by1 = q.bounds
        v = {"left": (x0 - bx1, 0.0), "right": (x1 - bx0, 0.0),
             "bottom": (0.0, y0 - by1), "top": (0.0, y1 - by0)}[gravity]
        swept.append(_sweep(q, v))
    return unary_union(swept) if swept else None


def remnant_geometry(sheet: Any, used: Any, spacing: float = 3.0, mode: str = "staircase",
                     gravity: str = "left", min_width: float = 50.0, min_area: float = 40000.0,
                     largest_only: bool = False) -> list:
    """Leftover polygons of ``sheet`` (a shapely polygon) after ``used`` (the cut parts) is
    taken away.  ``spacing``: kept clear around the parts (the cut-off line sits there);
    ``min_width`` / ``min_area`` (mm, mm²) drop slivers and small pieces."""
    shapely, _, _, _, unary_union = _shapely()
    if sheet is None or sheet.is_empty:
        return []
    if used is None or used.is_empty:
        free = sheet
    else:
        if mode == "staircase":
            occ = shadow(used, sheet.bounds, gravity if gravity in GRAVITIES else "left")
        else:
            occ = unary_union(polygons(used))
        occ = occ.buffer(max(spacing, 0.0), join_style="mitre") if spacing > 0 else occ
        free = sheet.difference(occ)
    if min_width > 0 and not free.is_empty:
        r = min_width / 2.0
        free = free.buffer(-r, join_style="mitre").buffer(r, join_style="mitre").intersection(sheet)
    out = [p.simplify(0.05) for p in polygons(free) if p.area >= max(min_area, 1e-6)]
    out = [p for p in out if not p.is_empty]
    out.sort(key=lambda p: -p.area)
    return out[:1] if largest_only else out


def rings_of(poly: Any, origin: Optional[tuple[float, float]] = None) -> list[Ring]:
    """Exterior then holes as ``[[x, y], ...]`` lists (closing point dropped), shifted so
    ``origin`` (default: the lower-left of the bounds) becomes (0, 0)."""
    ox, oy = origin if origin is not None else poly.bounds[:2]

    def ring(coords) -> Ring:
        pts = [[round(x - ox, 4), round(y - oy, 4)] for x, y in coords]
        return pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts
    return [ring(poly.exterior.coords)] + [ring(h.coords) for h in poly.interiors]


def polygon_of(rings: list[Ring]) -> Any:
    _, Polygon, _, _, _ = _shapely()
    if not rings or len(rings[0]) < 3:
        return Polygon()
    p = Polygon([tuple(pt) for pt in rings[0]], [[tuple(pt) for pt in h] for h in rings[1:] if len(h) >= 3])
    return p if p.is_valid else p.buffer(0)


# ============================================================================== records
@dataclass
class Remnant:
    rings: list[Ring]                         # exterior + holes, mm, bbox lower-left at (0, 0)
    material: str = ""
    thickness_mm: float = 0.0
    id: str = ""
    status: str = "available"
    location: str = ""                        # rack / shelf, free text
    created: float = 0.0
    used_at: float = 0.0
    source: str = ""                          # job / queue entry it was cut from
    used_by: str = ""                         # job / queue entry that used it
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def polygon(self) -> Any:
        return polygon_of(self.rings)

    @property
    def area_mm2(self) -> float:
        return float(self.polygon.area)

    @property
    def size(self) -> tuple[float, float]:
        if not self.rings or not self.rings[0]:
            return (0.0, 0.0)
        xs = [p[0] for p in self.rings[0]]
        ys = [p[1] for p in self.rings[0]]
        return (max(xs) - min(xs), max(ys) - min(ys))

    def label(self) -> str:
        w, h = self.size
        mat = f" {self.material}" if self.material else ""
        thk = f" {self.thickness_mm:g} mm" if self.thickness_mm else ""
        return f"{self.id} {w:.0f}x{h:.0f} mm{mat}{thk}"

    def nest_sheet(self, quantity: int = 1) -> Any:
        """A :class:`mlaser.nest.NestSheet` with this remnant's true outline."""
        from mlaser.model import Contour, Craft, Line, Part
        from mlaser.nest import NestSheet
        contours = []
        for ring in self.rings:
            pts = [(float(x), float(y)) for x, y in ring]
            if len(pts) < 3:
                continue
            segs: list = [Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            contours.append(Contour(segs, True))
        w, h = self.size
        outline = [Part(contours, Craft(layer=0), name=self.id)]
        return NestSheet(w, h, quantity, name=self.id, outline=outline, is_remnant=True,
                         extra={"remnant_id": self.id, "material": self.material,
                                "thickness_mm": self.thickness_mm})

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Remnant":
        names = {f.name for f in fields(cls)}
        r = cls(**{k: v for k, v in d.items() if k in names})
        if r.status not in STATUSES:
            r.status = "available"
        return r


class RemnantStore:
    """The remnant stock (``remnants.json``; ``path=None`` keeps it in memory)."""

    def __init__(self, path: Optional[os.PathLike | str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.items: list[Remnant] = []
        self.next_number = 1
        self.listeners: list[Callable[[], None]] = []
        if self.path is not None:
            self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        self.items = []
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Remnant.from_json(d) for d in data.get("remnants", []) if isinstance(d, dict)]
            self.next_number = int(data.get("next", 1))
        except (OSError, ValueError, TypeError):
            try:
                os.replace(self.path, str(self.path) + ".bad")
            except OSError:
                pass
            self.items = []
        nums = [int(r.id[2:]) for r in self.items if r.id.startswith("R-") and r.id[2:].isdigit()]
        self.next_number = max([self.next_number] + [n + 1 for n in nums])

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": "mlaser-remnants/1", "next": self.next_number,
                "remnants": [r.to_json() for r in self.items]}
        fd, tmp = tempfile.mkstemp(prefix=".remnants-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _changed(self) -> None:
        self.save()
        for fn in list(self.listeners):
            fn()

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    # ------------------------------------------------------------------ access
    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def get(self, rid: str) -> Remnant:
        for r in self.items:
            if r.id == rid:
                return r
        raise KeyError(rid)

    def available(self, material: str = "", thickness_mm: float = 0.0, tol: float = 0.05) -> list[Remnant]:
        """Available remnants, matching ``material`` / ``thickness_mm`` when given (largest
        first)."""
        out = []
        for r in self.items:
            if r.status != "available":
                continue
            if material and r.material and r.material.lower() != material.lower():
                continue
            if thickness_mm and r.thickness_mm and abs(r.thickness_mm - thickness_mm) > tol:
                continue
            out.append(r)
        out.sort(key=lambda r: -r.area_mm2)
        return out

    # ------------------------------------------------------------------ edits
    def add(self, rings: list[Ring], material: str = "", thickness_mm: float = 0.0, **kw: Any) -> Remnant:
        r = Remnant(rings=rings, material=material, thickness_mm=float(thickness_mm),
                    id=f"R-{self.next_number:04d}", created=self.clock(), **kw)
        self.next_number += 1
        self.items.append(r)
        self._changed()
        return r

    def add_polygon(self, poly: Any, material: str = "", thickness_mm: float = 0.0, **kw: Any) -> Remnant:
        return self.add(rings_of(poly), material, thickness_mm, **kw)

    def capture(self, sheet: Any, used: Any, material: str = "", thickness_mm: float = 0.0,
                spacing: float = 3.0, mode: str = "staircase", gravity: str = "left",
                min_width: float = 50.0, min_area: float = 40000.0, source: str = "") -> list[Remnant]:
        """Store the leftover of a cut sheet (:func:`remnant_geometry`); returns the new
        remnants (none when nothing usable is left)."""
        polys = remnant_geometry(sheet, used, spacing, mode, gravity, min_width, min_area)
        out = []
        for p in polys:
            out.append(self.add(rings_of(p), material, thickness_mm, source=source))
        return out

    def set_status(self, rid: str, status: str, by: str = "") -> Remnant:
        if status not in STATUSES:
            raise ValueError(status)
        r = self.get(rid)
        r.status = status
        if status == "used":
            r.used_at = self.clock()
            r.used_by = by or r.used_by
        elif status == "available":
            r.used_by = ""
            r.used_at = 0.0
        else:
            r.used_by = by or r.used_by
        self._changed()
        return r

    def reserve(self, rid: str, by: str = "") -> Remnant:
        return self.set_status(rid, "reserved", by)

    def release(self, rid: str) -> Remnant:
        return self.set_status(rid, "available")

    def mark_used(self, rid: str, by: str = "") -> Remnant:
        return self.set_status(rid, "used", by)

    def update(self, rid: str, **kw: Any) -> Remnant:
        r = self.get(rid)
        for k, v in kw.items():
            if k in ("material", "location", "note"):
                setattr(r, k, str(v))
            elif k == "thickness_mm":
                r.thickness_mm = float(v)
        self._changed()
        return r

    def remove(self, rid: str) -> Remnant:
        r = self.get(rid)
        self.items.remove(r)
        self._changed()
        return r

    def purge_used(self) -> int:
        n = len(self.items)
        self.items = [r for r in self.items if r.status != "used"]
        if len(self.items) != n:
            self._changed()
        return n - len(self.items)


def sheet_polygon_of_parts(parts: Iterable[Any]) -> Any:
    """A sheet outline given as model parts (a remnant or an imported sheet DXF)."""
    from mlaser.nest.shapes import parts_geometry
    return parts_geometry(list(parts))


__all__ = ["Remnant", "RemnantStore", "remnant_geometry", "shadow", "rings_of", "polygon_of",
           "polygons", "STATUSES"]
