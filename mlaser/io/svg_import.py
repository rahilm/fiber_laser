"""Basic SVG import.

The original does not parse SVG itself: MainApp runs SVG2GCode.exe (juicy-gcode,
default flavor, 96 dpi, biarc fitting) and imports the resulting G-code (see
io/gcode.py).  This module parses SVG directly instead:

* elements: path (M L H V C S Q T A Z, absolute + relative), line, polyline,
  polygon, rect (no rounded corners), circle, ellipse; nested <g>.
* transforms: matrix/translate/scale/rotate/skewX/skewY (composed).
* units: user units are px at 96 dpi -> mm (25.4/96), like juicy-gcode's
  default ``--dpi 96``; width/height in mm/cm/in with a viewBox are honoured.
* Y axis flipped (SVG y-down -> machine y-up) about the document height.
Curves (Bezier, elliptical arcs, ellipses) are flattened to Lines within
``tol``; circles (uniform transform) stay true Arcs.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

from ..model import Arc, Contour, Document, Line, Part
from .geom import join_segments

PX_MM = 25.4 / 96.0
_UNITS = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6, "px": PX_MM, "": PX_MM}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _len_mm(s: str | None):
    if not s:
        return None
    m = re.match(r"\s*([-+]?[\d.eE+-]+)\s*([a-z%]*)", s)
    if not m or m.group(2) == "%":
        return None
    return float(m.group(1)) * _UNITS.get(m.group(2), PX_MM)


def _mul(a, b):
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (a0 * b0 + a2 * b1, a1 * b0 + a3 * b1, a0 * b2 + a2 * b3, a1 * b2 + a3 * b3,
            a0 * b4 + a2 * b5 + a4, a1 * b4 + a3 * b5 + a5)


def _parse_transform(s: str | None):
    m = (1, 0, 0, 1, 0, 0)
    if not s:
        return m
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s):
        v = [float(x) for x in _NUM.findall(args)]
        if name == "matrix" and len(v) == 6:
            t = tuple(v)
        elif name == "translate":
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0])
            r = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = _mul(_mul((1, 0, 0, 1, v[1], v[2]), r), (1, 0, 0, 1, -v[1], -v[2]))
            else:
                t = r
        elif name == "skewX":
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY":
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mul(m, t)
    return m


def _ap(m, p):
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def _bez(pts, tol):
    """Flatten a cubic/quadratic Bezier by uniform subdivision sized from control polygon."""
    L = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    n = max(2, min(500, int(math.ceil(math.sqrt(L / max(tol, 1e-6)) * 1.5))))
    out = []
    for i in range(1, n + 1):
        t = i / n
        if len(pts) == 4:
            a, b, c, d = pts
            mt = 1 - t
            out.append((mt ** 3 * a[0] + 3 * mt * mt * t * b[0] + 3 * mt * t * t * c[0] + t ** 3 * d[0],
                        mt ** 3 * a[1] + 3 * mt * mt * t * b[1] + 3 * mt * t * t * c[1] + t ** 3 * d[1]))
        else:
            a, b, c = pts
            mt = 1 - t
            out.append((mt * mt * a[0] + 2 * mt * t * b[0] + t * t * c[0],
                        mt * mt * a[1] + 2 * mt * t * b[1] + t * t * c[1]))
    return out


def _svg_arc(p0, rx, ry, phi, large, sweep, p1, tol):
    """Endpoint -> centre parameterisation (SVG spec F.6.5), flattened."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    rx, ry = abs(rx), abs(ry)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1 / rx) ** 2 + (y1 / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    co = math.sqrt(max(num / den, 0)) if den else 0
    if large == sweep:
        co = -co
    cx1, cy1 = co * rx * y1 / ry, -co * ry * x1 / rx
    cx = cp * cx1 - sp * cy1 + (p0[0] + p1[0]) / 2
    cy = sp * cx1 + cp * cy1 + (p0[1] + p1[1]) / 2

    def ang(u, v):
        a = math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])
        return a
    t1 = ang((1, 0), ((x1 - cx1) / rx, (y1 - cy1) / ry))
    dt = ang(((x1 - cx1) / rx, (y1 - cy1) / ry), ((-x1 - cx1) / rx, (-y1 - cy1) / ry))
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    r = max(rx, ry)
    step = 2 * math.acos(max(min(1 - tol / r, 1.0), -1.0)) if r > tol else math.pi / 4
    n = max(2, int(math.ceil(abs(dt) / max(step, 1e-3))))
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        out.append((cx + rx * math.cos(t) * cp - ry * math.sin(t) * sp,
                    cy + rx * math.cos(t) * sp + ry * math.sin(t) * cp))
    out[-1] = p1
    return out


def parse_path(d: str, tol: float = 0.01):
    """Return subpaths as (points list, closed) in SVG user units (untransformed)."""
    toks = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUM.pattern, d)
    subs = []
    pts: list = []
    cur = (0.0, 0.0)
    start = cur
    cmd = None
    last_c = None
    i = 0

    def num():
        nonlocal i
        v = float(toks[i]); i += 1
        return v

    def flag():
        # arc flags may be written without separators ("a1 1 0 01..."): handle first char
        nonlocal i
        t = toks[i]
        if t in ("0", "1"):
            i += 1
            return int(t)
        v = t[0]
        toks[i] = t[1:]
        return int(v)

    while i < len(toks):
        t = toks[i]
        if re.match(r"[A-Za-z]", t):
            cmd = t
            i += 1
            if cmd in "Zz":
                if pts:
                    subs.append((pts, True))
                pts = []
                cur = start
                last_c = None
                continue
        if cmd is None:
            i += 1
            continue
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)
        if C == "M":
            if pts:
                subs.append((pts, False))
            cur = (ox + num(), oy + num())
            start = cur
            pts = [cur]
            cmd = "l" if rel else "L"
            last_c = None
        elif C == "L":
            cur = (ox + num(), oy + num()); pts.append(cur); last_c = None
        elif C == "H":
            cur = ((cur[0] if rel else 0) + num(), cur[1]); pts.append(cur); last_c = None
        elif C == "V":
            cur = (cur[0], (cur[1] if rel else 0) + num()); pts.append(cur); last_c = None
        elif C == "C":
            c1 = (ox + num(), oy + num()); c2 = (ox + num(), oy + num()); e = (ox + num(), oy + num())
            pts += _bez([cur, c1, c2, e], tol); cur = e; last_c = ("C", c2)
        elif C == "S":
            c1 = (2 * cur[0] - last_c[1][0], 2 * cur[1] - last_c[1][1]) if last_c and last_c[0] == "C" else cur
            c2 = (ox + num(), oy + num()); e = (ox + num(), oy + num())
            pts += _bez([cur, c1, c2, e], tol); cur = e; last_c = ("C", c2)
        elif C == "Q":
            c1 = (ox + num(), oy + num()); e = (ox + num(), oy + num())
            pts += _bez([cur, c1, e], tol); cur = e; last_c = ("Q", c1)
        elif C == "T":
            c1 = (2 * cur[0] - last_c[1][0], 2 * cur[1] - last_c[1][1]) if last_c and last_c[0] == "Q" else cur
            e = (ox + num(), oy + num())
            pts += _bez([cur, c1, e], tol); cur = e; last_c = ("Q", c1)
        elif C == "A":
            rx, ry, phi = num(), num(), math.radians(num())
            la, sw = flag(), flag()
            e = (ox + num(), oy + num())
            pts += _svg_arc(cur, rx, ry, phi, la, sw, e, tol); cur = e; last_c = None
        else:
            i += 1
    if pts and len(pts) > 1:
        subs.append((pts, False))
    return subs


def import_svg(path: str, tol: float = 0.01, join_tol: float = 0.05) -> Document:
    root = ET.parse(path).getroot()
    ns = lambda tag: tag.split("}")[-1]
    W = _len_mm(root.get("width"))
    H = _len_mm(root.get("height"))
    vb = [float(x) for x in _NUM.findall(root.get("viewBox", ""))]
    base = (PX_MM, 0, 0, PX_MM, 0, 0)
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        sx = (W / vb[2]) if W else PX_MM
        sy = (H / vb[3]) if H else sx
        base = (sx, 0, 0, sy, -vb[0] * sx, -vb[1] * sy)
        if H is None:
            H = vb[3] * sy
    if H is None:
        H = 0.0
    flip = (1, 0, 0, -1, 0, H)
    world = _mul(flip, base)
    tol_u = tol / max(abs(world[0]), 1e-9)
    contours: list[Contour] = []
    loose = []

    def emit(pts, closed, m):
        P = [_ap(m, p) for p in pts]
        segs = [Line(a, b) for a, b in zip(P, P[1:]) if math.dist(a, b) > 1e-9]
        if closed and len(P) > 2 and math.dist(P[-1], P[0]) > 1e-9:
            segs.append(Line(P[-1], P[0]))
        if not segs:
            return
        if closed:
            contours.append(Contour(segs, True))
        else:
            loose.extend(segs)

    def walk(el, m):
        m = _mul(m, _parse_transform(el.get("transform")))
        tag = ns(el.tag)
        g = lambda k, d=0.0: float(_NUM.findall(el.get(k, str(d)))[0]) if _NUM.findall(el.get(k, str(d))) else d
        if tag in ("defs", "clipPath", "mask", "symbol", "metadata", "title", "desc", "style"):
            return
        if tag == "path":
            for pts, closed in parse_path(el.get("d", ""), tol_u):
                emit(pts, closed, m)
        elif tag == "line":
            emit([(g("x1"), g("y1")), (g("x2"), g("y2"))], False, m)
        elif tag in ("polyline", "polygon"):
            v = [float(x) for x in _NUM.findall(el.get("points", ""))]
            emit(list(zip(v[0::2], v[1::2])), tag == "polygon", m)
        elif tag == "rect":
            x, y, w, h = g("x"), g("y"), g("width"), g("height")
            if w > 0 and h > 0:
                emit([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], True, m)
        elif tag in ("circle", "ellipse"):
            cx, cy = g("cx"), g("cy")
            rx = g("r") if tag == "circle" else g("rx")
            ry = g("r") if tag == "circle" else g("ry")
            sx, sy = math.hypot(m[0], m[1]), math.hypot(m[2], m[3])
            uniform = abs(sx - sy) < 1e-9 and abs(m[0] * m[2] + m[1] * m[3]) < 1e-9
            if rx > 0 and ry > 0 and abs(rx - ry) < 1e-12 and uniform:
                c = _ap(m, (cx, cy))
                p0 = _ap(m, (cx + rx, cy))
                det = m[0] * m[3] - m[1] * m[2]
                contours.append(Contour([Arc(p0, p0, c, ccw=det > 0)], True))
            elif rx > 0 and ry > 0:
                n = max(16, int(2 * math.pi * max(rx, ry) / max(math.sqrt(8 * max(rx, ry) * tol_u), 1e-6)))
                emit([(cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n))
                      for k in range(n)], True, m)
        for ch in el:
            walk(ch, m)

    walk(root, world)
    contours += join_segments(loose, join_tol)
    doc = Document(extra={"source": path})
    for i, c in enumerate(contours):
        doc.parts.append(Part(contours=[c], name=f"svg#{i}"))
    return doc
