"""Interactive drawing tools as Qt-free state machines (ICreateGlyph, vtable 0x1012038c).

ICreateGlyph slots (observed on every CCreate* vtable): [5] left click with the snapped
model point, [6] mouse move (rubber band), [7] "finished?" (state == 2 for two-click tools,
3 for three-click tools, never for polyline/spline), [8] the same as [5] for typed
coordinates, [3]/[4] draw the preview (yellow 0.9/0.9/0 stippled 0xAAAA lines, 0x10084000),
[2] create the graph.  A click within ``tol`` of the previous point in both x and y is
ignored (tool+0x38).  The polyline / curve are ended from the view context menu: mv2 Finish
Draw, mv3 End Draw (cancel), mv4 Enclose (close), mv5 Line / mv6 Arc mode.

Every tool here exposes::

    click(p)            left button (model point, already snapped)
    hover(p)            mouse move
    finish() -> bool    right button / Enter (mv2); True if a result is ready
    key(name) -> bool   'c' enclose (mv4), 'a' arc (mv6), 'l' line (mv5), 'backspace'
    done                True when result() is final
    result()            list[Contour] (empty for measure / pick tools)
    preview()           list of polylines to draw
    snap_points()       extra snap candidates (e.g. the polyline start for closing)
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

from mlaser.model import Contour, Line, Point

from . import primitives as P


def _pts(c: Optional[Contour]) -> list[list[Point]]:
    if c is None or not c.segments:
        return []
    pts = list(c.points(0.05))
    if c.closed and pts and math.dist(pts[0], pts[-1]) > 1e-12:
        pts.append(pts[0])
    if len(pts) == 1:
        pts = pts * 2
    return [pts]


class Tool:
    name = "tool"
    clicks = 1                      # clicks needed (0 = open-ended)

    def __init__(self, tol: float = 0.01):
        self.tol = tol
        self.points: list[Point] = []
        self.cursor: Optional[Point] = None
        self.done = False
        self._result: list[Contour] = []

    # -- input
    def _accept(self, p: Point) -> bool:
        if self.points:
            q = self.points[-1]
            if abs(p[0] - q[0]) < self.tol and abs(p[1] - q[1]) < self.tol:
                return False
        return True

    def click(self, p: Point) -> None:
        if self.done:
            return
        p = (float(p[0]), float(p[1]))
        if not self._accept(p):
            return
        self.points.append(p)
        self.cursor = p
        if self.clicks and len(self.points) >= self.clicks:
            res = self.build(self.points)
            if res is None:
                self.points.pop()           # degenerate input: wait for another click
                return
            self._result = res
            self.done = True

    def hover(self, p: Point) -> None:
        self.cursor = (float(p[0]), float(p[1]))

    def finish(self) -> bool:
        return self.done

    def key(self, name: str) -> bool:
        if name == "backspace" and self.points and not self.done:
            self.points.pop()
            return True
        return False

    # -- output
    def build(self, pts: Sequence[Point]) -> Optional[list[Contour]]:
        raise NotImplementedError

    def result(self) -> list[Contour]:
        return list(self._result)

    def preview(self) -> list[list[Point]]:
        if self.done:
            return []
        pts = list(self.points)
        if self.cursor is not None and pts:
            try:
                res = self.build(pts + [self.cursor]) if len(pts) + 1 >= (self.clicks or 2) else None
            except (ValueError, ZeroDivisionError):
                res = None
            if res:
                return [pl for c in res for pl in _pts(c)]
            return [pts + [self.cursor]]
        return []

    def snap_points(self):
        return []

    def status(self) -> str:
        return f"{self.name}: {len(self.points)}/{self.clicks or '-'}"


class _Simple(Tool):
    fn: Callable = None  # type: ignore

    def build(self, pts):
        c = type(self).fn(*pts[: self.clicks])
        return None if c is None else [c]


class PointTool(_Simple):
    name, clicks = "point", 1
    fn = staticmethod(P.point)

    def preview(self):
        return [[self.cursor, self.cursor]] if self.cursor and not self.done else []


class LineTool(_Simple):
    name, clicks = "line", 2
    fn = staticmethod(P.line)


class RectTool(_Simple):
    name, clicks = "rect", 2
    fn = staticmethod(P.rect)

    def _accept(self, p):
        # CCreateRect needs both |dx| and |dy| >= tol (0x100817b0)
        if self.points:
            q = self.points[-1]
            return abs(p[0] - q[0]) >= self.tol and abs(p[1] - q[1]) >= self.tol
        return True


class CircleTool(Tool):
    name, clicks = "circle", 2

    def build(self, pts):
        r = math.dist(pts[0], pts[1])
        return None if r < P.MIN_RADIUS else [P.circle(pts[0], r)]


class Circle3Tool(Tool):
    name, clicks = "circle3", 3

    def build(self, pts):
        if len(pts) < 3:
            return [P.line(pts[0], pts[1])]
        c = P.circle_3pt(*pts[:3])
        return None if c is None else [c]


class Arc3Tool(Tool):
    name, clicks = "arc3", 3

    def build(self, pts):
        if len(pts) < 3:
            return [P.line(pts[0], pts[1])]
        c = P.arc_3pt(*pts[:3])
        return None if c is None else [c]


class ScanArcTool(Tool):
    """CCreateArcORA: centre, start point, end angle."""
    name, clicks = "scan_arc", 3

    def build(self, pts):
        if len(pts) < 3:
            r = math.dist(pts[0], pts[1])
            return None if r < P.MIN_RADIUS else [P.circle(pts[0], r)]
        c = P.arc_center(*pts[:3])
        return None if c is None else [c]


class EllipseTool(Tool):
    name, clicks = "ellipse", 3

    def build(self, pts):
        c, m = pts[0], pts[1]
        a = math.dist(c, m)
        if a < P.MIN_RADIUS:
            return None
        if len(pts) < 3:
            return [P.line(c, m)]
        ratio = math.dist(c, pts[2]) / a
        if ratio < 1e-6:
            return None
        e = P.ellipse(c, m, ratio)
        return None if e is None else [e]


class RoundRectTool(Tool):
    name, clicks = "round_rect", 3

    def build(self, pts):
        if len(pts) < 3:
            return [P.rect(pts[0], pts[1])]
        r = P.round_rect_radius(*pts[:3])
        if r < P.MIN_RADIUS:
            return None
        return [P.round_rect(pts[0], pts[1], r)]


class PolygonTool(Tool):
    name, clicks = "polygon", 2

    def __init__(self, tol=0.01, sides: int = 6):
        super().__init__(tol)
        self.sides = sides

    def build(self, pts):
        if math.dist(pts[0], pts[1]) < 1e-6:
            return None
        return [P.polygon(pts[0], pts[1], self.sides)]

    def key(self, name):
        if name in ("+", "="):
            self.sides = min(360, self.sides + 1)
            return True
        if name == "-":
            self.sides = max(3, self.sides - 1)
            return True
        return super().key(name)


class StarTool(Tool):
    name, clicks = "star", 3

    def __init__(self, tol=0.01, points: int = 6):
        super().__init__(tol)
        self.n = points

    def build(self, pts):
        c, v = pts[0], pts[1]
        if math.dist(c, v) < 1e-6:
            return None
        inner = None if len(pts) < 3 else math.dist(c, pts[2])
        if inner is not None and inner < P.MIN_RADIUS:
            return None
        s = P.star(c, v, self.n, inner)
        return None if s is None else [s]

    key = PolygonTool.key

    @property
    def sides(self):
        return self.n

    @sides.setter
    def sides(self, v):
        self.n = v


class PolylineTool(Tool):
    """CCreateLwploy: vertices until Finish (mv2) or Enclose (mv4); arc mode (mv6) adds
    tangent-continuous bulge arcs."""
    name, clicks = "pline", 0

    def __init__(self, tol=0.01):
        super().__init__(tol)
        self.bulges: list[float] = []
        self.arc_mode = False
        self.closed = False

    def click(self, p):
        if self.done:
            return
        p = (float(p[0]), float(p[1]))
        if not self._accept(p):
            return
        if self.points:
            self.bulges.append(self._bulge_to(p))
        self.points.append(p)
        self.cursor = p

    def _bulge_to(self, p) -> float:
        if not self.arc_mode or len(self.points) < 2:
            return 0.0
        a, b = self.points[-2], self.points[-1]
        prev = P.bulge_arc(a, b, self.bulges[-1])
        if hasattr(prev, "center"):
            c = prev.center
            r = (b[0] - c[0], b[1] - c[1])
            t = (-r[1], r[0]) if prev.ccw else (r[1], -r[0])
        else:
            t = (b[0] - a[0], b[1] - a[1])
        return P.tangent_bulge(t, b, p)

    def key(self, name):
        if name == "a":
            self.arc_mode = True
            return True
        if name == "l":
            self.arc_mode = False
            return True
        if name == "c":
            if len(self.points) >= 3 or (len(self.points) >= 2 and any(self.bulges)):
                self.closed = True
                self.bulges.append(0.0)
                self.done = True
            return True
        if name == "backspace" and self.points:
            self.points.pop()
            if self.bulges:
                self.bulges.pop()
            return True
        return False

    def finish(self):
        if len(self.points) >= 2:
            self.done = True
        return self.done

    def result(self):
        if not self.done:
            return []
        pts = list(self.points)
        bul = list(self.bulges)
        if not self.closed and len(pts) >= 3 and math.dist(pts[0], pts[-1]) < 1e-9:
            pts.pop()
            self.closed = True
        return [P.polyline(pts, bul, self.closed)]

    def preview(self):
        if self.done or not self.points:
            return []
        pts = list(self.points)
        bul = list(self.bulges)
        if self.cursor is not None and math.dist(self.cursor, pts[-1]) > 1e-12:
            bul.append(self._bulge_to(self.cursor))
            pts.append(self.cursor)
        if len(pts) < 2:
            return []
        return _pts(P.polyline(pts, bul, False))

    def snap_points(self):
        return [(self.points[0], "end")] if len(self.points) >= 2 else []


class CurveTool(Tool):
    """CCreateSpline: fit points until Finish; Enclose closes the curve."""
    name, clicks = "curve", 0

    def __init__(self, tol=0.01):
        super().__init__(tol)
        self.closed = False

    def click(self, p):
        if self.done:
            return
        p = (float(p[0]), float(p[1]))
        if self._accept(p):
            self.points.append(p)
            self.cursor = p

    def key(self, name):
        if name == "c" and len(self.points) >= 3:
            self.closed = True
            self.done = True
            return True
        return super().key(name)

    def finish(self):
        if len(self.points) >= 2:
            self.done = True
        return self.done

    def result(self):
        if not self.done:
            return []
        c = P.spline(self.points, self.closed)
        return [] if c is None else [c]

    def preview(self):
        if self.done or not self.points:
            return []
        pts = list(self.points)
        if self.cursor is not None and math.dist(self.cursor, pts[-1]) > 1e-9:
            pts.append(self.cursor)
        c = P.spline(pts) if len(pts) >= 2 else None
        return _pts(c)


class PlaceTool(Tool):
    """Place ready-made contours (text, QR code) with one click; ``template`` has its anchor
    (bottom-left) at the origin."""
    name, clicks = "place", 1

    def __init__(self, template: Sequence[Contour], tol=0.01, name: str = "place"):
        super().__init__(tol)
        self.template = list(template)
        self.name = name

    def _moved(self, p):
        from mlaser.model import Arc
        out = []
        for c in self.template:
            segs = []
            for s in c.segments:
                f = lambda q: (q[0] + p[0], q[1] + p[1])  # noqa: E731
                segs.append(Line(f(s.start), f(s.end)) if isinstance(s, Line) else Arc(f(s.start), f(s.end), f(s.center), s.ccw))
            out.append(Contour(segs, c.closed, dict(c.extra)))
        return out

    def build(self, pts):
        return self._moved(pts[0])

    def preview(self):
        if self.done or self.cursor is None:
            return []
        return [pl for c in self._moved(self.cursor) for pl in _pts(c)]


class MeasureTool(Tool):
    """View mode 8 (0x100e9da0): two snapped clicks; ``measurement`` = (|dx|, |dy|, length)."""
    name, clicks = "measure", 2

    def __init__(self, tol=0.01):
        super().__init__(tol)
        self.measurement: Optional[tuple[float, float, float]] = None

    def _accept(self, p):
        return True

    def build(self, pts):
        from .arrange import measure
        self.measurement = measure(pts[0], pts[1])
        return []

    def preview(self):
        if self.done or not self.points or self.cursor is None:
            return []
        return [[self.points[0], self.cursor]]


class PickTool(Tool):
    """One click handed to ``action(point) -> bool`` (split mode 12, trim 16, extend 17).
    Stays active for further picks until cancelled (the view mode persists in the original
    until another mode is chosen)."""
    clicks = 0

    def __init__(self, action: Callable[[Point], bool], name: str, tol=0.01):
        super().__init__(tol)
        self.action = action
        self.name = name
        self.hits = 0

    def click(self, p):
        if self.action((float(p[0]), float(p[1]))):
            self.hits += 1

    def preview(self):
        return []

    def finish(self):
        self.done = True
        return False


TOOLS = {
    "draw.point": PointTool, "draw.line": LineTool, "draw.circle": CircleTool,
    "draw.circle3": Circle3Tool, "draw.arc3": Arc3Tool, "draw.scan_arc": ScanArcTool,
    "draw.ellipse": EllipseTool, "draw.rect": RectTool, "draw.round_rect": RoundRectTool,
    "draw.polygon": PolygonTool, "draw.star": StarTool, "draw.pline": PolylineTool,
    "draw.curve": CurveTool,
}

# ribbon command id of each draw tool (observed, ribbon builder FUN_004a2290 at 0x4a25f3..)
COMMAND_IDS = {
    "draw.point": 0x334a, "draw.line": 0x334b, "draw.circle": 0x334c, "draw.circle3": 0x334d,
    "draw.arc3": 0x334e, "draw.scan_arc": 0x334f, "draw.ellipse": 0x3350, "draw.rect": 0x3351,
    "draw.round_rect": 0x3352, "draw.polygon": 0x3353, "draw.star": 0x3354, "draw.pline": 0x3355,
    "draw.curve": 0x3356, "draw.text": 0x3357, "draw.trim": 0x348f, "draw.extend": 0x3490,
    "draw.advert_text": 0x3440, "draw.adv_text": 0x34ed, "draw.qr": 0x3502,
    "tool.group": 0x33fe, "tool.group_select_all": 0x33ff, "tool.group_explode": 0x3400,
    "tool.group_explode_all": 0x3401, "select.similar": 0x331d, "select.inner": 0x331e,
    "select.outer": 0x331f, "geom.merge": 0x3412, "geom.explode": 0x3413, "geom.split": 0x341c,
    "xform.size": 0x3390, "xform.align": 0x33c2, "tool.measure": 0x3552,
}
