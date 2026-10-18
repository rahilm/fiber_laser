"""Dynamic capture (object snap) for the drawing tools: CDynCaptureEng (vtable 0x1011f87c).

Observed (0x10054420 -> IGraph+0x90 -> CEditable*::Capture +0x88):

* a graph is only tested when the cursor lies inside its bbox grown by the tolerance
  (CGlyContour 0x10062f10);
* each element offers characteristic points tested with a square window
  (|dx| <= tol and |dy| < tol): a segment its start, end and midpoint plus a 4th point
  (CEditableSegment 0x10084550, +0x100..+0x130), an arc its start, end, then mid / centre
  points (CEditableArc 0x10086f30), a circle its centre and quadrant points
  (CEditableCircle 0x10091810);
* the tolerance is the view's pick distance (CCADModule+0x270 = k * pixel size, set by
  0x100e9210).

The original returns the first hit in drawing order; the rebuild returns the nearest one
(same result whenever only one point is inside the window).  No grid snap exists in the
original (no grid is drawn and no grid parameter is registered).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from mlaser.model import Arc, Contour, Line, Part, Point

from .intersect import is_full_circle

KINDS = ("end", "mid", "center", "quadrant")


def characteristic_points(c: Contour) -> list[tuple[Point, str]]:
    out: list[tuple[Point, str]] = []
    for s in c.segments:
        if isinstance(s, Line):
            out.append((s.start, "end"))
            out.append((s.end, "end"))
            if s.length() > 1e-9:
                out.append((((s.start[0] + s.end[0]) / 2, (s.start[1] + s.end[1]) / 2), "mid"))
        elif is_full_circle(s):
            cx, cy = s.center
            r = s.radius
            out.append((s.center, "center"))
            out += [((cx + r, cy), "quadrant"), ((cx, cy + r), "quadrant"),
                    ((cx - r, cy), "quadrant"), ((cx, cy - r), "quadrant")]
        else:
            out.append((s.start, "end"))
            out.append((s.end, "end"))
            out.append((s.point_at(0.5), "mid"))
            out.append((s.center, "center"))
    return out


def snap(parts: Sequence[Part], p: Point, tol: float,
         extra: Iterable[tuple[Point, str]] = ()) -> Optional[tuple[Point, str]]:
    """Nearest characteristic point inside the square window of half-size ``tol``."""
    best, best_d = None, math.inf

    def test(q, kind):
        nonlocal best, best_d
        if abs(q[0] - p[0]) <= tol and abs(q[1] - p[1]) <= tol:
            d = math.dist(q, p)
            if d < best_d:
                best, best_d = (tuple(q), kind), d

    for q, kind in extra:
        test(q, kind)
    for part in parts:
        if not part.contours:
            continue
        for c in part.contours:
            for q, kind in characteristic_points(c):
                test(q, kind)
    return best
