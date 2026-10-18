"""Geometry conversion for nesting: model.Part contours <-> shapely, and part transforms."""
from __future__ import annotations

import copy
import math
from typing import Iterable

import shapely
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union

from ..model import Arc, Contour, Line, Part, Point
from .model import FIT_RECT, NestSheet, Placement


def _ring(c: Contour, tol: float) -> list[Point]:
    pts = list(c.points(tol))
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) > 1e-9:
            out.append(p)
    return out


def parts_geometry(parts: Iterable[Part], tol: float = 0.05):
    """Footprint of a set of parts as a (Multi)Polygon with holes.

    Closed contours are nested by containment (even-odd: depth 1 outer, 2 hole, 3 island
    inside a hole, ...), which matches the CContourTopTree depth convention used by
    cam.ordering. Open contours contribute a thin (0.05 mm) strip so they are never
    overlapped.
    """
    rings, opens = [], []
    for p in parts:
        for c in p.contours:
            if not c.segments:
                continue
            pts = _ring(c, tol)
            if c.closed and len(pts) >= 3:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.area > 1e-9:
                    rings.append(poly)
            elif len(pts) >= 2:
                opens.append(LineString(pts).buffer(0.05))
    rings.sort(key=lambda g: -g.area)
    # even-odd composition: symmetric difference of all closed rings
    geom = None
    for r in rings:
        geom = r if geom is None else geom.symmetric_difference(r)
    if opens:
        geom = unary_union(([geom] if geom is not None else []) + opens)
    if geom is None:
        return Polygon()
    geom = geom.buffer(0)
    return geom


def fit_geometry(geom, fit: int):
    if fit == FIT_RECT and not geom.is_empty:
        return box(*geom.bounds)
    return geom


def sheet_geometry(sh: NestSheet, margin: float):
    """Usable region of a sheet: rectangle (or remnant outline with defects) shrunk by the
    edge margin. Defects (holes of the outline) are grown by the margin."""
    if sh.outline:
        g = parts_geometry(sh.outline)
    else:
        x0, y0 = sh.origin
        g = box(x0, y0, x0 + sh.width, y0 + sh.height)
    full = g
    if margin > 0:
        g = g.buffer(-margin, join_style="mitre")
    return full, g


def transform_geom(geom, pl: Placement):
    g = geom
    if pl.mirror:
        g = affinity.scale(g, -1.0, 1.0, origin=(0, 0))
    if pl.angle:
        g = affinity.rotate(g, pl.angle, origin=(0, 0))
    return affinity.translate(g, pl.x, pl.y)


def oriented(geom, angle: float, mirror: bool):
    return transform_geom(geom, Placement(-1, 0.0, 0.0, angle, mirror))


# ------------------------------------------------------------------ model.Part transforms
def _tp(p: Point, pl: Placement) -> Point:
    return pl.apply(p)


def _tseg(s, pl: Placement):
    if isinstance(s, Line):
        return Line(_tp(s.start, pl), _tp(s.end, pl))
    if isinstance(s, Arc):
        return Arc(_tp(s.start, pl), _tp(s.end, pl), _tp(s.center, pl), s.ccw != pl.mirror)
    return s


def transform_part(part: Part, pl: Placement) -> Part:
    """Deep copy of ``part`` with the placement applied (arcs keep their direction sense:
    a mirror flips ccw so the drawn shape is the mirror image)."""
    q = copy.deepcopy(part)
    q.contours = [Contour([_tseg(s, pl) for s in c.segments], c.closed, copy.deepcopy(c.extra))
                  for c in part.contours]
    if q.craft.cool_point is not None:
        q.craft.cool_point = _tp(q.craft.cool_point, pl)
    q.selected = False
    return q


def net_area(geom) -> float:
    return float(geom.area) if geom is not None else 0.0


def as_polygons(geom) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


__all__ = ["parts_geometry", "fit_geometry", "sheet_geometry", "transform_geom", "oriented",
           "transform_part", "net_area", "as_polygons", "shapely"]
