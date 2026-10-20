"""Flat faces of Onshape parts: face choice, flattening to 2-D loops, thickness (Qt-free).

Input is the JSON of Onshape's ``GET /parts/.../partid/{pid}/bodydetails`` (schema
``BTExportModelBodiesResponse``; lengths in metres).  Output is in millimetres.

Face choice (:func:`choose_face`):

* the planar faces are grouped into **sides** by their outward normal; a side's area is the sum
  of its planar faces (a pocket floor or a boss top faces the same way as the plate face around
  it, so a pocketed side and the plain side opposite have the same area);
* the side with the largest area wins; when the two largest sides tie (within 1 %), the side
  that carries more features wins: planar faces of that side that are not on its outermost
  plane (pocket floors, boss tops, counterbore floors).  Through-holes have no planar face, so
  they count for neither side;
* a plain symmetric plate uses its +Z (Top) face; for other directions the order is
  +Z, -Y, +X, -Z, +Y, -X (Onshape's Top, Front, Right planes first);
* the face drawn is the largest planar face of the chosen side;
* a per-part override names a face id (and is ignored, with a note, when the face is gone).

Flattening (:func:`flatten`): the face is drawn as seen from outside, X along the part's X axis
(Y when the face normal is the part's X axis), the lower-left of the outline at 0,0.  Lines,
arcs and circles stay exact; ellipses and B-splines are evaluated exactly and written as
polylines with a 0.02 mm chord tolerance; any other edge (or a spline that cannot be evaluated)
needs the tessellated edge from ``/tessellatededges`` (:attr:`FlatFace.need_tessellation`).

Thickness (:func:`plane_thickness`, :func:`volume_thickness`): the distance between the largest
planar face and the largest planar face opposite it; and volume / area of the largest planar
face (from ``/massproperties``).  The first ignores pockets, the second drifts on pocketed plates
and bosses, so a disagreement is shown, not hidden.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

Vec = tuple[float, float, float]
P2 = tuple[float, float]

M2MM = 1000.0
CHORD_TOL_MM = 0.02
CLOSE_TOL_MM = 0.01           # loop closure: gap between consecutive edges
TIE_REL = 0.01                # two sides tie when their areas differ by less than 1 %
PARALLEL = 1.0 - 1e-6

PREFERENCE: tuple[Vec, ...] = ((0, 0, 1), (0, -1, 0), (1, 0, 0), (0, 0, -1), (0, 1, 0), (-1, 0, 0))
DIR_NAMES = {(0, 0, 1): "Top (+Z)", (0, 0, -1): "Bottom (-Z)", (0, -1, 0): "Front (-Y)",
             (0, 1, 0): "Back (+Y)", (1, 0, 0): "Right (+X)", (-1, 0, 0): "Left (-X)"}


class GeometryError(ValueError):
    """The part cannot be flattened (the message says why)."""


class NoFlatFace(GeometryError):
    """The part has no planar face (a shaft, a mesh body)."""


# ============================================================================== vectors
def vec(d: Any) -> Vec:
    if isinstance(d, dict):
        return (float(d.get("x", 0.0)), float(d.get("y", 0.0)), float(d.get("z", 0.0)))
    if isinstance(d, (list, tuple)) and len(d) >= 3:
        return (float(d[0]), float(d[1]), float(d[2]))
    raise GeometryError(f"not a vector: {d!r}")


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec) -> Vec:
    n = norm(a)
    if n < 1e-15:
        raise GeometryError("zero-length direction")
    return (a[0] / n, a[1] / n, a[2] / n)


def direction_name(n: Vec) -> str:
    for d, name in DIR_NAMES.items():
        if dot(n, d) > 0.9999:
            return name
    return f"Tilted ({n[0]:+.2f}, {n[1]:+.2f}, {n[2]:+.2f})"


# ============================================================================== model
@dataclass
class PlanarFace:
    id: str
    origin: Vec                  # a point on the plane (m)
    normal: Vec                  # outward unit normal
    area: float                  # m²
    loops: list[dict]
    box: Optional[tuple[Vec, Vec]] = None

    @property
    def offset(self) -> float:
        return dot(self.origin, self.normal)


@dataclass
class Side:
    normal: Vec
    faces: list[PlanarFace]

    @property
    def area(self) -> float:
        return sum(f.area for f in self.faces)

    @property
    def main(self) -> PlanarFace:
        return max(self.faces, key=lambda f: f.area)

    @property
    def features(self) -> int:
        """Planar faces of this side below its outermost plane (pocket floors, boss tops and
        counterbore floors; the plate face itself does not count)."""
        top = max(f.offset for f in self.faces)
        return sum(1 for f in self.faces if top - f.offset > 1e-7)


@dataclass
class Body:
    id: str
    faces: list[dict]
    edges: dict[str, dict]
    vertices: dict[str, Vec]
    material: str = ""                        # BTPartMaterial name
    planar: list[PlanarFace] = field(default_factory=list)


def find_body(resp: dict, part_id: str = "") -> Body:
    """The body of ``part_id`` in a bodydetails response (the only solid one when no id fits)."""
    bodies = [b for b in (resp or {}).get("bodies") or [] if isinstance(b, dict)]
    if not bodies:
        raise NoFlatFace("Onshape returned no body for this part")
    pick = next((b for b in bodies if part_id and b.get("id") == part_id), None)
    if pick is None:
        solids = [b for b in bodies if str(b.get("type", "SOLID")).upper() in ("SOLID", "")]
        pick = solids[0] if solids else bodies[0]
    props = pick.get("properties") or {}
    mat = props.get("material") if isinstance(props, dict) else None
    body = Body(id=str(pick.get("id", "")), faces=list(pick.get("faces") or []),
                edges={str(e.get("id")): e for e in pick.get("edges") or [] if isinstance(e, dict)},
                vertices={str(v.get("id")): vec(v.get("point")) for v in pick.get("vertices") or []
                          if isinstance(v, dict) and v.get("point") is not None},
                material=str(mat.get("name") or "") if isinstance(mat, dict) else "")
    body.planar = planar_faces(body)
    return body


def _outward(face: dict, surf: dict) -> Vec:
    """Outward normal of a planar face: ``directionOrientedWithFace`` when given, else
    ``normal`` flipped when ``isOrientedWithFace`` (or the face's ``orientation``) is false."""
    d = surf.get("directionOrientedWithFace")
    if d:
        return unit(vec(d))
    n = unit(vec(surf.get("normal") or surf.get("direction")))
    flip = surf.get("isOrientedWithFace")
    if flip is None:
        flip = face.get("orientation", True)
    return n if flip else mul(n, -1.0)


def planar_faces(body: Body) -> list[PlanarFace]:
    out = []
    for f in body.faces:
        s = f.get("surface") or {}
        if str(s.get("type", "")).upper() != "PLANE":
            continue
        try:
            n = _outward(f, s)
            o = vec(s.get("origin"))
        except GeometryError:
            continue
        box = None
        b = f.get("box") or {}
        if b.get("minCorner") and b.get("maxCorner"):
            box = (vec(b["minCorner"]), vec(b["maxCorner"]))
        out.append(PlanarFace(str(f.get("id", "")), o, n, float(f.get("area") or 0.0), list(f.get("loops") or []), box))
    return out


def sides(body: Body) -> list[Side]:
    out: list[Side] = []
    for f in body.planar:
        for s in out:
            if dot(s.normal, f.normal) > PARALLEL:
                s.faces.append(f)
                break
        else:
            out.append(Side(f.normal, [f]))
    out.sort(key=lambda s: -s.area)
    return out


def _pref_rank(n: Vec) -> int:
    for i, d in enumerate(PREFERENCE):
        if dot(n, d) > 0.9999:
            return i
    return len(PREFERENCE)


@dataclass
class FaceChoice:
    face: PlanarFace
    reason: str                  # "auto: largest side" / "auto: features" / "auto: top" / "saved" ...
    opposite: Optional[PlanarFace] = None
    note: str = ""


def choose_face(body: Body, override: str = "") -> FaceChoice:
    """The face to flatten (see the module docstring)."""
    ss = sides(body)
    if not ss:
        raise NoFlatFace("the part has no flat face: export a STEP or draw the DXF by hand")
    note = ""
    if override:
        hit = next((f for f in body.planar if f.id == override), None)
        if hit is not None:
            return FaceChoice(hit, "saved", opposite_face(body, hit))
        note = "the saved face is no longer on the part: the automatic choice is used"
    best = ss[0]
    reason = "auto: largest flat side"
    ties = [s for s in ss if best.area - s.area <= TIE_REL * best.area]
    if len(ties) > 1:
        feats = max(s.features for s in ties)
        ties = [s for s in ties if s.features == feats]
        if len(ties) == 1:
            best, reason = ties[0], "auto: the side with pockets / bosses"
        else:
            best = min(ties, key=lambda s: _pref_rank(s.normal))
            reason = "auto: symmetric, " + direction_name(best.normal)
    face = best.main
    return FaceChoice(face, reason, opposite_face(body, face), note)


def opposite_face(body: Body, face: PlanarFace) -> Optional[PlanarFace]:
    opp = [f for f in body.planar if dot(f.normal, face.normal) < -PARALLEL]
    return max(opp, key=lambda f: f.area) if opp else None


def plane_thickness(body: Body) -> Optional[float]:
    """Distance (mm) between the largest planar face and the largest planar face opposite it."""
    if not body.planar:
        return None
    big = max(body.planar, key=lambda f: f.area)
    opp = opposite_face(body, big)
    if opp is None:
        return None
    return abs(dot(sub(big.origin, opp.origin), big.normal)) * M2MM


def volume_thickness(body: Body, volume_m3: Optional[float]) -> Optional[float]:
    """Volume ÷ the area of the largest planar face (mm)."""
    if not body.planar or not volume_m3 or volume_m3 <= 0:
        return None
    big = max(body.planar, key=lambda f: f.area)
    return volume_m3 / big.area * M2MM if big.area > 0 else None


def mass_volume(resp: Any, part_id: str = "") -> Optional[float]:
    """Volume (m³) from a ``BTMassPropertiesBulkInfo`` response (``bodies[pid].volume[0]``)."""
    bodies = (resp or {}).get("bodies") if isinstance(resp, dict) else None
    if not isinstance(bodies, dict) or not bodies:
        return None
    b = bodies.get(part_id) if part_id in bodies else next(iter(bodies.values()))
    vol = (b or {}).get("volume")
    if isinstance(vol, list) and vol:
        return float(vol[0])
    if isinstance(vol, (int, float)):
        return float(vol)
    return None


# ============================================================================== 2-D primitives
@dataclass
class Prim:
    kind: str                         # line | arc | circle | poly
    pts: list[P2]                     # line: [a, b]; poly: points in traversal order
    center: P2 = (0.0, 0.0)
    radius: float = 0.0
    a0: float = 0.0                   # arc: start / end angle (degrees, counter-clockwise)
    a1: float = 0.0
    reversed: bool = False            # arc: traversed clockwise (a1 -> a0)

    def start(self) -> P2:
        if self.kind == "arc":
            return _polar(self.center, self.radius, self.a1 if self.reversed else self.a0)
        if self.kind == "circle":
            return (self.center[0] + self.radius, self.center[1])
        return self.pts[0]

    def end(self) -> P2:
        if self.kind == "arc":
            return _polar(self.center, self.radius, self.a0 if self.reversed else self.a1)
        if self.kind == "circle":
            return (self.center[0] + self.radius, self.center[1])
        return self.pts[-1]

    def flip(self) -> None:
        if self.kind == "arc":
            self.reversed = not self.reversed
        elif self.kind in ("line", "poly"):
            self.pts.reverse()

    def translate(self, dx: float, dy: float) -> None:
        self.pts = [(x + dx, y + dy) for x, y in self.pts]
        self.center = (self.center[0] + dx, self.center[1] + dy)

    def sample(self, tol: float = CHORD_TOL_MM) -> list[P2]:
        """Points in traversal order (for areas and boxes)."""
        if self.kind in ("line", "poly"):
            return list(self.pts)
        if self.kind == "circle":
            a0, sweep = 0.0, 360.0
        else:
            a0, sweep = self.a0, (self.a1 - self.a0) % 360.0 or 360.0
        n = max(8, int(math.ceil(math.radians(sweep) / _arc_step(self.radius, tol))))
        pts = [_polar(self.center, self.radius, a0 + sweep * i / n) for i in range(n + 1)]
        return pts[::-1] if (self.kind == "arc" and self.reversed) else pts

    def bbox(self) -> tuple[float, float, float, float]:
        if self.kind == "circle":
            c, r = self.center, self.radius
            return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        if self.kind == "arc":
            pts = [self.start(), self.end()]
            sweep = (self.a1 - self.a0) % 360.0 or 360.0
            for q in (0.0, 90.0, 180.0, 270.0):
                if (q - self.a0) % 360.0 <= sweep:
                    pts.append(_polar(self.center, self.radius, q))
        else:
            pts = self.pts
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


def _polar(c: P2, r: float, deg: float) -> P2:
    a = math.radians(deg)
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def _arc_step(r: float, tol: float) -> float:
    if r <= tol:
        return math.pi / 4
    return max(1e-4, 2.0 * math.acos(max(-1.0, 1.0 - tol / r)))


def _d2(a: P2, b: P2) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Loop:
    prims: list[Prim]
    outer: bool
    closed: bool = True
    gap: float = 0.0

    def points(self) -> list[P2]:
        out: list[P2] = []
        for p in self.prims:
            s = p.sample()
            out.extend(s if not out else s[1:])
        return out

    def bbox(self) -> tuple[float, float, float, float]:
        bs = [p.bbox() for p in self.prims]
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    def area(self) -> float:
        pts = self.points()
        return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                       for i in range(len(pts)))) / 2.0 if len(pts) > 2 else 0.0


@dataclass
class FlatFace:
    face_id: str
    label: str                      # "Top (+Z) 120 x 80 mm"
    reason: str
    loops: list[Loop] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    need_tessellation: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    exact_edges: int = 0
    evaluated_edges: int = 0
    tessellated_edges: int = 0

    @property
    def outer(self) -> Optional[Loop]:
        return next((lp for lp in self.loops if lp.outer), None)

    @property
    def holes(self) -> list[Loop]:
        return [lp for lp in self.loops if not lp.outer]

    @property
    def net_area(self) -> float:
        o = self.outer
        return max(0.0, (o.area() if o else 0.0) - sum(h.area() for h in self.holes))

    @property
    def ok(self) -> bool:
        return not self.need_tessellation and bool(self.loops) and all(lp.closed for lp in self.loops)


# ============================================================================== projection
@dataclass
class Frame:
    origin: Vec
    u: Vec
    v: Vec
    n: Vec

    def p2(self, p: Vec) -> P2:
        d = sub(p, self.origin)
        return (dot(d, self.u) * M2MM, dot(d, self.v) * M2MM)


def face_frame(face: PlanarFace) -> Frame:
    """X along the part's X axis; Y when the face looks along X (a side face).  Seen from
    outside: v = n × u."""
    n = face.normal
    axis: Vec = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = unit(sub(axis, mul(n, dot(axis, n))))
    return Frame(face.origin, u, cross(n, u), n)


# ============================================================================== B-splines
def _find_span(n: int, p: int, t: float, U: list[float]) -> int:
    if t >= U[n + 1]:
        return n
    if t <= U[p]:
        return p
    lo, hi = p, n + 1
    mid = (lo + hi) // 2
    while t < U[mid] or t >= U[mid + 1]:
        if t < U[mid]:
            hi = mid
        else:
            lo = mid
        mid = (lo + hi) // 2
    return mid


class BSpline:
    """A (rational) B-spline from ``BTSplineDescription``: ``controlPoints`` flat (x, y, z per
    point, plus the weight w as a fourth value when ``isRational``), ``knots`` the full knot
    vector, ``degree``."""

    def __init__(self, d: dict) -> None:
        self.p = int(d.get("degree") or 0)
        self.U = [float(k) for k in d.get("knots") or []]
        cp = [float(c) for c in d.get("controlPoints") or []]
        n = len(self.U) - self.p - 1
        if self.p < 1 or n < self.p + 1 or not cp:
            raise GeometryError("spline without degree, knots or control points")
        stride = len(cp) // n
        if stride * n != len(cp) or stride not in (3, 4):
            raise GeometryError(f"spline: {len(cp)} control values for {n} control points")
        if bool(d.get("isRational")) and stride != 4:
            raise GeometryError("rational spline without weights")
        self.n = n
        self.P = [tuple(cp[i * stride:i * stride + 3]) for i in range(n)]
        self.W = [cp[i * stride + 3] if stride == 4 else 1.0 for i in range(n)]
        if any(w <= 0 for w in self.W):
            raise GeometryError("spline with a non-positive weight")
        self.t0, self.t1 = self.U[self.p], self.U[n]
        if not self.t1 > self.t0:
            raise GeometryError("spline with an empty parameter range")

    def __call__(self, t: float) -> Vec:
        p, U = self.p, self.U
        t = min(max(t, self.t0), self.t1)
        k = _find_span(self.n - 1, p, t, U)
        d = []
        for j in range(p + 1):
            i = k - p + j
            w = self.W[i]
            x, y, z = self.P[i]
            d.append([x * w, y * w, z * w, w])
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                i = k - p + j
                den = U[i + p - r + 1] - U[i]
                a = 0.0 if den == 0 else (t - U[i]) / den
                d[j] = [(1 - a) * d[j - 1][m] + a * d[j][m] for m in range(4)]
        x, y, z, w = d[p]
        return (x / w, y / w, z / w)

    def closest(self, q: Vec) -> float:
        N = 400
        ts = [self.t0 + (self.t1 - self.t0) * i / N for i in range(N + 1)]
        best = min(ts, key=lambda t: norm(sub(self(t), q)))
        h = (self.t1 - self.t0) / N
        lo, hi = max(self.t0, best - h), min(self.t1, best + h)
        for _ in range(60):                       # golden section on the bracket
            m1, m2 = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
            if norm(sub(self(m1), q)) < norm(sub(self(m2), q)):
                hi = m2
            else:
                lo = m1
        return (lo + hi) / 2


def adaptive(f: Callable[[float], P2], t0: float, t1: float, tol: float = CHORD_TOL_MM,
             min_seg: int = 8, depth: int = 18) -> list[P2]:
    """Points of ``f`` on [t0, t1] with chord deviation below ``tol``."""
    out = [f(t0)]

    def rec(a: float, pa: P2, b: float, pb: P2, d: int) -> None:
        m = (a + b) / 2
        pm = f(m)
        # deviation of the midpoint (and quarter points) from the chord
        dev = _seg_dist(pm, pa, pb)
        if d > 0 and dev <= tol:
            q1, q3 = f((a + m) / 2), f((m + b) / 2)
            dev = max(dev, _seg_dist(q1, pa, pb), _seg_dist(q3, pa, pb))
        if dev > tol and d < depth:
            rec(a, pa, m, pm, d + 1)
            rec(m, pm, b, pb, d + 1)
        else:
            out.append(pb)

    ts = [t0 + (t1 - t0) * i / min_seg for i in range(min_seg + 1)]
    ps = [out[0]] + [f(t) for t in ts[1:]]
    for i in range(min_seg):
        rec(ts[i], ps[i], ts[i + 1], ps[i + 1], 1)
    return out


def _seg_dist(p: P2, a: P2, b: P2) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-24:
        return _d2(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return _d2(p, (a[0] + t * dx, a[1] + t * dy))


# ============================================================================== edges -> prims
def _edge_points(edge: dict, body: Body) -> tuple[Optional[Vec], Optional[Vec], Optional[Vec]]:
    g = edge.get("geometry") or {}
    s = vec(g["startPoint"]) if g.get("startPoint") else None
    e = vec(g["endPoint"]) if g.get("endPoint") else None
    m = vec(g["midPoint"]) if g.get("midPoint") else None
    vs = [body.vertices[v] for v in edge.get("vertices") or [] if v in body.vertices]
    if s is None and vs:
        s = vs[0]
    if e is None and vs:
        e = vs[-1]
    return s, e, m


def _is_closed_edge(edge: dict, s: Optional[Vec], e: Optional[Vec]) -> bool:
    if not edge.get("vertices") and s is None:
        return True
    return s is not None and e is not None and norm(sub(s, e)) * M2MM < 1e-4


def edge_prim(edge: dict, body: Body, fr: Frame, tess: Optional[dict[str, list[Vec]]] = None) -> tuple[Prim, str]:
    """The 2-D primitive of an edge and how it was made (exact / evaluated / tessellated).
    Raises KeyError(edge id) when the edge needs a tessellation that is not in ``tess``."""
    eid = str(edge.get("id"))
    curve = edge.get("curve") or {}
    ctype = str(curve.get("type", "")).upper()
    s, e, m = _edge_points(edge, body)
    try:
        if ctype == "LINE":
            if s is None or e is None:
                raise GeometryError("line without end points")
            return Prim("line", [fr.p2(s), fr.p2(e)]), "exact"
        if ctype == "CIRCLE":
            return _circle_prim(curve, edge, s, e, m, fr), "exact"
        if ctype == "ELLIPSE":
            return _ellipse_prim(curve, edge, s, e, m, fr), "evaluated"
        if ctype == "BCURVE":
            return _spline_prim(curve, edge, s, e, m, fr), "evaluated"
    except GeometryError:
        pass
    if tess and eid in tess and len(tess[eid]) >= 2:
        return Prim("poly", [fr.p2(p) for p in tess[eid]]), "tessellated"
    raise KeyError(eid)


def _ccw_from(a0: float, a_mid: float, a1: float) -> bool:
    """True when going counter-clockwise from a0 reaches a_mid before a1 (degrees)."""
    return (a_mid - a0) % 360.0 < (a1 - a0) % 360.0


def _circle_prim(curve: dict, edge: dict, s, e, m, fr: Frame) -> Prim:
    c3 = vec(curve.get("origin"))
    r = float(curve.get("radius") or 0.0) * M2MM
    if r <= 0:
        raise GeometryError("circle without a radius")
    nrm = curve.get("normal")
    if nrm and abs(dot(unit(vec(nrm)), fr.n)) < PARALLEL:
        raise GeometryError("circle not in the face plane")
    c = fr.p2(c3)
    if _is_closed_edge(edge, s, e):
        return Prim("circle", [], center=c, radius=r)
    s2, e2 = fr.p2(s), fr.p2(e)
    a0 = math.degrees(math.atan2(s2[1] - c[1], s2[0] - c[0]))
    a1 = math.degrees(math.atan2(e2[1] - c[1], e2[0] - c[0]))
    if m is not None:
        m2 = fr.p2(m)
        ccw = _ccw_from(a0, math.degrees(math.atan2(m2[1] - c[1], m2[0] - c[0])), a1)
    else:
        g = edge.get("geometry") or {}
        if g.get("arcIsClockwise") is not None:      # clockwise about the curve normal
            about_n = not bool(g["arcIsClockwise"])
        else:
            about_n = True                           # an edge runs counter-clockwise about its normal
        ccw = about_n == (dot(unit(vec(nrm)), fr.n) > 0) if nrm else about_n
    if ccw:
        return Prim("arc", [], center=c, radius=r, a0=a0 % 360.0, a1=a1 % 360.0)
    return Prim("arc", [], center=c, radius=r, a0=a1 % 360.0, a1=a0 % 360.0, reversed=True)


def _ellipse_prim(curve: dict, edge: dict, s, e, m, fr: Frame) -> Prim:
    c = vec(curve.get("origin"))
    N = unit(vec(curve.get("normal")))
    M = unit(vec(curve.get("majorAxis")))
    ra, rb = float(curve.get("majorRadius") or 0), float(curve.get("minorRadius") or 0)
    if ra <= 0 or rb <= 0:
        raise GeometryError("ellipse without radii")
    Mn = cross(N, M)

    def at(t: float) -> Vec:
        return add(c, add(mul(M, ra * math.cos(t)), mul(Mn, rb * math.sin(t))))

    def par(p: Vec) -> float:
        d = sub(p, c)
        return math.atan2(dot(d, Mn) / rb, dot(d, M) / ra)

    tau = 2 * math.pi
    if _is_closed_edge(edge, s, e):
        return Prim("poly", adaptive(lambda t: fr.p2(at(t)), 0.0, tau))
    if s is None or e is None:
        raise GeometryError("ellipse edge without end points")
    t0, t1 = par(s), par(e)
    forward = True                     # an edge runs counter-clockwise about its curve normal
    if m is not None:
        forward = (par(m) - t0) % tau < (t1 - t0) % tau
    if forward:
        return Prim("poly", adaptive(lambda t: fr.p2(at(t)), t0, t0 + (t1 - t0) % tau))
    pts = adaptive(lambda t: fr.p2(at(t)), t1, t1 + (t0 - t1) % tau)
    return Prim("poly", pts[::-1])


def _spline_prim(curve: dict, edge: dict, s, e, m, fr: Frame) -> Prim:
    sp = BSpline(curve)
    if _is_closed_edge(edge, s, e):
        t0, t1 = sp.t0, sp.t1
    else:
        if s is None or e is None:
            raise GeometryError("spline edge without end points")
        t0, t1 = sp.closest(s), sp.closest(e)
        tol = 0.01 / M2MM
        if norm(sub(sp(t0), s)) > tol or norm(sub(sp(t1), e)) > tol:
            raise GeometryError("the spline does not pass through the edge's end points")
        if m is not None and norm(sub(sp(sp.closest(m)), m)) > tol:
            raise GeometryError("the spline does not pass through the edge's midpoint")
    rev = t1 < t0
    a, b = (t1, t0) if rev else (t0, t1)
    pts = adaptive(lambda t: fr.p2(sp(t)), a, b)
    return Prim("poly", pts[::-1] if rev else pts)


# ============================================================================== loops
def _chain(prims: list[Prim]) -> tuple[list[Prim], bool, float]:
    """Order and orient the primitives of a loop end to start; closed?, largest gap (mm)."""
    if len(prims) == 1 and prims[0].kind == "circle":
        return prims, True, 0.0
    rest = list(prims)
    out = [rest.pop(0)]
    # orient the first so that one of the others continues it
    if rest and min(min(_d2(out[0].end(), q.start()), _d2(out[0].end(), q.end())) for q in rest) > \
            min(min(_d2(out[0].start(), q.start()), _d2(out[0].start(), q.end())) for q in rest):
        out[0].flip()
    gap = 0.0
    while rest:
        end = out[-1].end()
        best_i, best_d, best_flip = 0, float("inf"), False
        for i, q in enumerate(rest):
            for flip, pt in ((False, q.start()), (True, q.end())):
                d = _d2(end, pt)
                if d < best_d:
                    best_i, best_d, best_flip = i, d, flip
        q = rest.pop(best_i)
        if best_flip:
            q.flip()
        gap = max(gap, best_d)
        out.append(q)
    gap = max(gap, _d2(out[-1].end(), out[0].start()))
    return out, gap <= CLOSE_TOL_MM, gap


def flatten(body: Body, choice: FaceChoice, tess: Optional[dict[str, list[Vec]]] = None) -> FlatFace:
    """The chosen face as 2-D loops in mm, lower-left of the outline at 0,0 (see the module
    docstring).  Edges that need ``/tessellatededges`` are listed in ``need_tessellation``."""
    face = choice.face
    fr = face_frame(face)
    flat = FlatFace(face.id, "", choice.reason, notes=[choice.note] if choice.note else [])
    loops: list[Loop] = []
    for raw in face.loops:
        prims: list[Prim] = []
        for co in raw.get("coedges") or []:
            edge = body.edges.get(str(co.get("edgeId")))
            if edge is None:
                flat.notes.append(f"edge {co.get('edgeId')} is missing from the body details")
                continue
            try:
                prim, how = edge_prim(edge, body, fr, tess)
            except KeyError as exc:
                flat.need_tessellation.append(str(exc.args[0]))
                continue
            if how == "exact":
                flat.exact_edges += 1
            elif how == "evaluated":
                flat.evaluated_edges += 1
            else:
                flat.tessellated_edges += 1
            prims.append(prim)
        if prims:
            chained, closed, gap = _chain(prims)
            loops.append(Loop(chained, bool(raw.get("isOuter")), closed, gap))
    if flat.need_tessellation or not loops:
        if not loops and not flat.need_tessellation:
            raise GeometryError("the face has no edges")
        return flat
    if not any(lp.outer for lp in loops):         # no isOuter flag: the loop with the largest box
        big = max(loops, key=lambda lp: (lp.bbox()[2] - lp.bbox()[0]) * (lp.bbox()[3] - lp.bbox()[1]))
        big.outer = True
    outer = next(lp for lp in loops if lp.outer)
    x0, y0, x1, y1 = outer.bbox()
    for lp in loops:
        for p in lp.prims:
            p.translate(-x0, -y0)
    flat.loops = loops
    flat.width, flat.height = x1 - x0, y1 - y0
    flat.label = f"{direction_name(face.normal)} {flat.width:.0f} x {flat.height:.0f} mm"
    for lp in loops:
        if not lp.closed:
            flat.notes.append(f"a loop is open (gap {lp.gap:.3f} mm)")
    return flat


def face_list(body: Body) -> list[tuple[str, str]]:
    """(face id, "Top (+Z) 120 x 80 mm") of every planar face, largest first (the face picker)."""
    out = []
    for f in sorted(body.planar, key=lambda f: -f.area):
        try:
            fl = flatten(body, FaceChoice(f, "picked"), None)
            size = f" {fl.width:.0f} x {fl.height:.0f} mm" if fl.loops else ""
        except GeometryError:
            size = ""
        out.append((f.id, f"{direction_name(f.normal)}{size} ({f.area * 1e6:.0f} mm²)"))
    return out


def tessellation_map(resp: Any) -> dict[str, list[Vec]]:
    """edge id -> points (m) from a ``BTExportTessellatedEdgesResponse``."""
    out: dict[str, list[Vec]] = {}
    for b in (resp or {}).get("bodies") or []:
        for e in (b or {}).get("edges") or []:
            pts = [vec(v) for v in e.get("vertices") or []]
            if e.get("id") is not None and len(pts) >= 2:
                out[str(e["id"])] = pts
    return out


def iter_entities(flat: FlatFace) -> Iterable[Prim]:
    for lp in flat.loops:
        yield from lp.prims
