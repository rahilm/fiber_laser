"""SVG import through svgelements (MIT), with our own conversion to model contours.

The original does not parse SVG itself: MainApp runs SVG2GCode.exe (juicy-gcode, default
flavor, 96 dpi, biarc fitting) and imports the resulting G-code (see io/gcode.py), so there is
no parity constraint here (docs/review/libraries.md, "SVG import").

Document parsing is svgelements' (``SVG.parse``): ``<style>`` CSS (type, class, id selectors)
and ``style=``, ``<use>`` (also of elements in ``<defs>``), nested transforms, units
(px at 96 dpi, mm, cm, in, pt, pc), the root viewBox / preserveAspectRatio, ``display:none``
(skipped by the parser), rounded ``rect`` (rx / ry), circle, ellipse, line, polyline, polygon,
path (all commands).  Before parsing, the tree is normalised (:func:`_normalise`):

* a ``<use>`` of a ``<symbol>`` and every nested ``<svg>`` become a ``<g>`` with the explicit
  viewport transform (x / y, width / height, viewBox, preserveAspectRatio), which svgelements
  1.9.6 gets wrong (it ignores the use's width / height on a symbol and applies a nested svg's
  x / y twice);
* ``<symbol>`` (rendered only through ``<use>``), ``<mask>``, ``<marker>`` and ``<pattern>``
  content is removed: svgelements would otherwise emit it as geometry.  ``<clipPath>`` and
  ``<defs>`` content is not rendered by svgelements (clip paths are not applied: the
  clipped element is imported whole).

Elements with ``visibility:hidden`` / ``collapse`` (inherited, overridable) are skipped.

Conversion (ours): user units are px at 96 dpi -> mm (25.4/96, like juicy-gcode's ``--dpi
96``), Y flipped about the document height (SVG y-down -> machine y-up).  Elliptical arcs
that are circular after the full transform become model Arcs (a closed run of co-circular arcs
covering 360 degrees, e.g. a ``<circle>``, is one full-circle Arc); other arcs (ellipses,
non-uniform scale or skew) and cubic / quadratic Beziers are flattened to Lines with chord error
<= ``tol`` (arcs: parameter step sqrt(8 tol / sigma_max); Beziers: adaptive subdivision until
the control polygon is within ``tol`` of the chord).  Closed subpaths (``Z``, closed shapes)
are contours; open pieces are chained with ``join_segments(join_tol)``.
"""
from __future__ import annotations

import copy
import io
import math
import re
import xml.etree.ElementTree as ET
from typing import Optional

from ..model import Arc, Contour, Document, Line, Part, Point, Segment
from ..geom.curves import join_segments
from . import limits as LIM

PX_MM = 25.4 / 96.0
_UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "px": PX_MM, "": PX_MM}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK = "{http://www.w3.org/1999/xlink}href"
_DROP = {"symbol", "mask", "marker", "pattern"}

Affine = tuple[float, float, float, float, float, float]   # (a, b, c, d, e, f) as SVG matrix()


def _len_mm(s: Optional[str]) -> Optional[float]:
    """A length attribute in mm (None for a percentage or nothing)."""
    if not s:
        return None
    m = re.match(r"\s*([-+]?[\d.eE+-]+)\s*([a-z%]*)", s)
    if not m or m.group(2) == "%":
        return None
    return float(m.group(1)) * _UNITS.get(m.group(2), PX_MM)


# ----------------------------------------------------------------------- tree normalisation
def _tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def _len_px(s: Optional[str], ref: float, default: Optional[float]) -> Optional[float]:
    """A length in user units (px); percentages of ``ref``."""
    if s is None or not s.strip():
        return default
    m = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-z%]*)", s)
    if not m:
        return default
    v, u = float(m.group(1)), m.group(2)
    if u == "%":
        return v / 100.0 * ref
    return v * _UNITS.get(u, PX_MM) / PX_MM


def _viewport_transform(x: float, y: float, w: float, h: float, vb: Optional[list[float]],
                        par: Optional[str]) -> str:
    """translate(x, y) + the viewBox -> viewport mapping (SVG 1.1 §7.8)."""
    t = f"translate({x!r},{y!r})"
    if not vb or len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0:
        return t
    sx, sy = w / vb[2], h / vb[3]
    p = (par or "xMidYMid meet").split()
    align = p[0] if p else "xMidYMid"
    if align != "none":
        s = max(sx, sy) if len(p) > 1 and p[1] == "slice" else min(sx, sy)
        tx = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}.get(align[:4], 0.5) * (w - vb[2] * s)
        ty = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}.get(align[4:], 0.5) * (h - vb[3] * s)
        sx = sy = s
    else:
        tx = ty = 0.0
    return t + f" translate({tx!r},{ty!r}) scale({sx!r},{sy!r}) translate({-vb[0]!r},{-vb[1]!r})"


def _vb(el: ET.Element) -> Optional[list[float]]:
    v = [float(x) for x in _NUM.findall(el.get("viewBox", ""))]
    return v if len(v) == 4 else None


def _as_group(el: ET.Element, transform: str, keep: tuple[str, ...]) -> ET.Element:
    g = ET.Element(f"{{{_SVG_NS}}}g")
    for k in keep:
        if el.get(k) is not None:
            g.set(k, el.get(k))  # type: ignore[arg-type]
    own = el.get("transform")
    g.set("transform", f"{own} {transform}" if own else transform)
    return g


_LENGTH_ATTRS = ("width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry")
_ABS_LEN = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(mm|cm|in|pt|pc)\s*")
_PRESENTATION = ("class", "style", "display", "visibility", "fill", "stroke", "color")


def _normalise(root: ET.Element) -> None:
    """See the module doc: expand use -> symbol / svg, nested svg -> g, drop non-rendered
    content."""
    ids = {el.get("id"): el for el in root.iter() if el.get("id")}
    vb = _vb(root)
    root_w = vb[2] if vb else (_len_px(root.get("width"), 1000.0, None) or 1000.0)
    root_h = vb[3] if vb else (_len_px(root.get("height"), 1000.0, None) or 1000.0)
    budget = [20000]                    # expansions (guards self-referencing symbols)
    # untrusted input: the elements in the file plus every element a <use> copies
    # (mlaser.io.limits.SVG_MAX_ELEMENTS); a <use> of a large symbol counts its whole subtree
    elements = LIM.Budget(LIM.SVG_MAX_ELEMENTS, "elements", LIM.SVG_TOO_MANY)
    elements.add(sum(1 for _ in root.iter()))

    def subtree(ref: ET.Element) -> int:
        # not cached: a symbol grows when the <use>s inside it are expanded; counting costs no
        # more than the copy it guards, and the budget stops it at the limit
        return sum(1 for _ in ref.iter()) - 1

    def expand_uses(parent: ET.Element, vw: float, vh: float, depth: int) -> None:
        if depth > 64:
            return
        for i, el in enumerate(list(parent)):
            tag = _tag(el)
            if tag == "use":
                href = el.get("href") or el.get(_XLINK) or ""
                ref = ids.get(href[1:]) if href.startswith("#") else None
                if ref is None or _tag(ref) not in ("symbol", "svg") or budget[0] <= 0:
                    continue
                budget[0] -= 1
                elements.add(subtree(ref) + 1)
                x = _len_px(el.get("x"), vw, 0.0) or 0.0
                y = _len_px(el.get("y"), vh, 0.0) or 0.0
                if _tag(ref) == "svg":
                    x += _len_px(ref.get("x"), vw, 0.0) or 0.0
                    y += _len_px(ref.get("y"), vh, 0.0) or 0.0
                w = _len_px(el.get("width") or ref.get("width"), vw, vw) or vw
                h = _len_px(el.get("height") or ref.get("height"), vh, vh) or vh
                sub_vb = _vb(ref)
                g = _as_group(el, _viewport_transform(x, y, w, h, sub_vb, ref.get("preserveAspectRatio")),
                              _PRESENTATION)
                for ch in ref:
                    g.append(copy.deepcopy(ch))
                parent.remove(el)
                parent.insert(i, g)
                expand_uses(g, sub_vb[2] if sub_vb else w, sub_vb[3] if sub_vb else h, depth + 1)
                continue
            if tag == "svg":
                sub_vb = _vb(el)
                w = _len_px(el.get("width"), vw, vw) or vw
                h = _len_px(el.get("height"), vh, vh) or vh
                expand_uses(el, sub_vb[2] if sub_vb else w, sub_vb[3] if sub_vb else h, depth + 1)
                continue
            expand_uses(el, vw, vh, depth + 1)

    def nested_svgs(parent: ET.Element, vw: float, vh: float) -> None:
        for i, el in enumerate(list(parent)):
            if _tag(el) != "svg":
                nested_svgs(el, vw, vh)
                continue
            x = _len_px(el.get("x"), vw, 0.0) or 0.0
            y = _len_px(el.get("y"), vh, 0.0) or 0.0
            w = _len_px(el.get("width"), vw, vw) or vw
            h = _len_px(el.get("height"), vh, vh) or vh
            sub_vb = _vb(el)
            g = _as_group(el, _viewport_transform(x, y, w, h, sub_vb, el.get("preserveAspectRatio")),
                          _PRESENTATION + ("id",))
            for ch in list(el):
                g.append(ch)
            parent.remove(el)
            parent.insert(i, g)
            nested_svgs(g, sub_vb[2] if sub_vb else w, sub_vb[3] if sub_vb else h)

    # absolute units -> px numbers here: svgelements converts mm with 0.0393701 in/mm
    # (5.4e-7 relative error); 1 px = 25.4/96 mm exactly
    for el in root.iter():
        for k in _LENGTH_ATTRS:
            v = el.get(k)
            if v is not None:
                m = _ABS_LEN.fullmatch(v)
                if m:
                    el.set(k, repr(float(m.group(1)) * _UNITS[m.group(2)] / PX_MM))
    expand_uses(root, root_w, root_h, 0)
    nested_svgs(root, root_w, root_h)
    # non-rendered containers (symbols were expanded above)
    for parent in list(root.iter()):
        for el in list(parent):
            if _tag(el) in _DROP:
                parent.remove(el)


# ----------------------------------------------------------------------- geometry
def _apply(m: Affine, p) -> Point:
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def _lin(m: Affine, v) -> Point:
    return (m[0] * v[0] + m[2] * v[1], m[1] * v[0] + m[3] * v[1])


def _bezier_flat(ctrl: list[Point], tol: float, out: list[Point], depth: int = 0) -> None:
    """Append the end points of a flattening of the Bezier ``ctrl`` (cubic or quadratic) with
    chord error <= tol (the curve lies in its control hull; split until the hull is flat)."""
    a, b = ctrl[0], ctrl[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L > 1e-15:
        dev = max(abs((q[0] - a[0]) * dy - (q[1] - a[1]) * dx) / L for q in ctrl[1:-1])
    else:
        dev = max(math.dist(q, a) for q in ctrl[1:-1])
    if dev <= tol or depth >= 24:
        out.append(b)
        return
    # de Casteljau split at 1/2
    left, right = [ctrl[0]], [ctrl[-1]]
    pts = list(ctrl)
    while len(pts) > 1:
        pts = [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in zip(pts, pts[1:])]
        left.append(pts[0])
        right.append(pts[-1])
    _bezier_flat(left, tol, out, depth + 1)
    _bezier_flat(right[::-1], tol, out, depth + 1)


class _Builder:
    """Collects model segments of one subpath in mm (``world``: px -> mm, it flips Y)."""

    def __init__(self, world: Affine, tol_px: float):
        self.world = world
        self.tol_px = tol_px
        self.segs: list[Segment] = []

    def mm(self, p) -> Point:
        return _apply(self.world, p)

    def line(self, p0: Point, p1: Point) -> None:
        a, b = self.mm(p0), self.mm(p1)
        if math.dist(a, b) > 1e-9:
            self.segs.append(Line(a, b))

    def polyline(self, pts: list[Point]) -> None:
        for p, q in zip(pts, pts[1:]):
            self.line(p, q)

    def bezier(self, ctrl: list[Point]) -> None:
        out = [ctrl[0]]
        _bezier_flat(ctrl, self.tol_px, out)
        out[-1] = ctrl[-1]
        self.polyline(out)

    def arc(self, p0: Point, p1: Point, c: Point, u: Point, v: Point, t0: float, sweep: float) -> None:
        """P(t) = c + u cos t + v sin t, t in [t0, t0 + sweep] (all in px, transformed)."""
        nu, nv = math.hypot(*u), math.hypot(*v)
        if nu < 1e-12 or nv < 1e-12 or abs(sweep) < 1e-15:
            self.line(p0, p1)
            return
        dot = u[0] * v[0] + u[1] * v[1]
        cross = u[0] * v[1] - u[1] * v[0]
        if abs(nu - nv) <= 1e-9 * max(nu, nv) and abs(dot) <= 1e-9 * nu * nv and abs(sweep) < 2 * math.pi - 1e-9:
            a, b = self.mm(p0), self.mm(p1)
            if math.dist(a, b) > 1e-9:
                ccw_px = (sweep * cross) > 0
                wdet = self.world[0] * self.world[3] - self.world[1] * self.world[2]
                self.segs.append(Arc(a, b, self.mm(c), ccw=ccw_px == (wdet > 0)))
            return
        # sigma_max of [u v]: |P''| <= sigma_max -> chord error <= h^2 sigma_max / 8
        s1 = nu * nu + nv * nv
        smax = math.sqrt((s1 + math.sqrt(max(s1 * s1 - 4 * cross * cross, 0.0))) / 2)
        h = math.sqrt(8 * self.tol_px / smax) if smax > 0 else math.pi / 8
        n = max(2, min(100000, int(math.ceil(abs(sweep) / h))))
        pts = [p0]
        for i in range(1, n):
            t = t0 + sweep * i / n
            pts.append((c[0] + u[0] * math.cos(t) + v[0] * math.sin(t),
                        c[1] + u[1] * math.cos(t) + v[1] * math.sin(t)))
        pts.append(p1)
        self.polyline(pts)


def _full_circle(segs: list[Segment]) -> Optional[Arc]:
    """A closed run of co-circular arcs covering 360 degrees -> one full-circle Arc."""
    if not segs or not all(isinstance(s, Arc) for s in segs):
        return None
    a0 = segs[0]
    assert isinstance(a0, Arc)
    r, c = a0.radius, a0.center
    if any(math.dist(s.center, c) > 1e-9 * max(r, 1) or abs(s.radius - r) > 1e-9 * max(r, 1)
           or s.ccw != a0.ccw for s in segs if isinstance(s, Arc)):
        return None
    if abs(sum(abs(s.sweep()) for s in segs if isinstance(s, Arc)) - 2 * math.pi) > 1e-6:
        return None
    return Arc(a0.start, a0.start, c, a0.ccw)


def _path_subpaths(path, m: Affine, world: Affine, tol_px: float):
    """Yield (segments in mm, closed) for an svgelements Path whose segments are in local
    coordinates and ``m`` is the element's full transform (local -> px)."""
    import svgelements as se

    b: Optional[_Builder] = None
    start: Optional[Point] = None
    cur: Optional[Point] = None

    def P(q) -> Point:
        return _apply(m, (q[0], q[1]))

    for seg in path:
        if isinstance(seg, se.Move):
            if b is not None and b.segs:
                yield b.segs, False
            b = _Builder(world, tol_px)
            start = cur = P(seg.end)
            continue
        if b is None:   # drawing without a Move (after Z): continue from the current point
            b = _Builder(world, tol_px)
            start = cur = P(seg.start) if seg.start is not None else (0.0, 0.0)
        if isinstance(seg, se.Close):
            if cur is not None and start is not None:
                b.line(cur, start)
            if b.segs:
                yield b.segs, True
            b = None
            cur = start
            continue
        p0 = P(seg.start)
        p1 = P(seg.end)
        if isinstance(seg, se.Line):
            b.line(p0, p1)
        elif isinstance(seg, se.CubicBezier):
            b.bezier([p0, P(seg.control1), P(seg.control2), p1])
        elif isinstance(seg, se.QuadraticBezier):
            b.bezier([p0, P(seg.control), p1])
        elif isinstance(seg, se.Arc):
            rx, ry = seg.rx, seg.ry
            if rx < 1e-12 or ry < 1e-12 or seg.center is None:
                b.line(p0, p1)
            else:
                cx, cy = seg.center[0], seg.center[1]
                ul = (seg.prx[0] - cx, seg.prx[1] - cy)
                vl = (-ul[1] * ry / rx, ul[0] * ry / rx)   # svgelements' frame: rot90(u) * ry/rx
                b.arc(p0, p1, P(seg.center), _lin(m, ul), _lin(m, vl), seg.get_start_t(), seg.sweep)
        else:
            b.line(p0, p1)
        cur = p1
    if b is not None and b.segs:
        yield b.segs, False


def _matrix(t) -> Affine:
    return (float(t.a), float(t.b), float(t.c), float(t.d), float(t.e), float(t.f))


def _hidden(el) -> bool:
    v = str(el.values.get("visibility", "visible")).strip().lower()
    return v in ("hidden", "collapse")


def parse_path(d: str, tol: float = 0.01):
    """Return subpaths of the path data ``d`` as (points list, closed), flattened within
    ``tol`` in SVG user units (untransformed, y down)."""
    import svgelements as se

    out = []
    ident: Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for segs, closed in _path_subpaths(se.Path(d), ident, ident, tol):
        pts: list[Point] = [segs[0].start]
        for s in segs:
            if isinstance(s, Arc):
                n = max(2, int(math.ceil(abs(s.sweep()) / (2 * math.acos(max(1 - tol / s.radius, -1.0))))))
                pts.extend(s.point_at(i / n) for i in range(1, n + 1))
            else:
                pts.append(s.end)
        if closed and math.dist(pts[0], pts[-1]) < 1e-12:
            pts.pop()
        out.append((pts, closed))
    return out


def _load_tree(path: str) -> tuple[bytes, ET.Element]:
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    tree = ET.parse(path)
    root = tree.getroot()
    _normalise(root)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue(), root


def import_svg(path: str, tol: float = 0.01, join_tol: float = 0.05) -> Document:
    """Import an SVG file: one Part per contour (``svg#<i>``), coordinates in mm, Y up.

    Untrusted input: more than :data:`mlaser.io.limits.SVG_MAX_ELEMENTS` elements after the
    ``<use>`` expansion, or nesting too deep to follow, raise
    :class:`~mlaser.io.limits.ImportLimitError`."""
    try:
        return _import_svg(path, tol, join_tol)
    except RecursionError:
        raise LIM.too_deep("SVG") from None


def _import_svg(path: str, tol: float, join_tol: float) -> Document:
    import svgelements as se

    data, root = _load_tree(path)
    LIM.check_cancel()
    svg = se.SVG.parse(io.BytesIO(data), reify=False, ppi=96.0)
    if root.get("height") is None and _vb(root) is None:
        H = 0.0
    else:
        H = float(svg.height) * PX_MM if svg.height is not None else 0.0
    world: Affine = (PX_MM, 0.0, 0.0, -PX_MM, 0.0, H)
    tol_px = tol / PX_MM
    contours: list[Contour] = []
    loose: list[Segment] = []
    shape_types: tuple[type, ...] = (se.Shape,)
    skip_types: tuple[type, ...] = (se.Text, se.Image)
    for n, el in enumerate(svg.elements()):
        if not n & 1023:
            LIM.check_cancel()
        if not isinstance(el, shape_types) or isinstance(el, skip_types) or _hidden(el):
            continue
        try:
            p = se.Path(el)
        except Exception:  # noqa: BLE001 - a malformed element is skipped, like the old parser
            continue
        m = _matrix(p.transform)
        for segs, closed in _path_subpaths(p, m, world, tol_px):
            if closed:
                full = _full_circle(segs)
                contours.append(Contour([full] if full else segs, True))
            else:
                loose.extend(segs)
    contours += join_segments(loose, join_tol)
    doc = Document(extra={"source": path})
    for i, c in enumerate(contours):
        doc.parts.append(Part(contours=[c], name=f"svg#{i}"))
    return doc


__all__: list[str] = ["import_svg", "parse_path", "PX_MM"]
