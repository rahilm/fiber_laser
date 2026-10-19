"""Points along a toolpath for simple path followers (the demo controller, previews)."""
from __future__ import annotations

import math

from mlaser.model import Arc


def path_points(items, arc_step_mm: float = 0.5) -> list[tuple[float, float, bool]]:
    """(x, y, laser) along a toolpath (Rapid, CutLine, CutArc), for simple path followers."""
    out: list[tuple[float, float, bool]] = []
    for m in items:
        name = type(m).__name__
        if name == "Rapid":
            out.append((float(m.end[0]), float(m.end[1]), False))
        elif name == "CutLine":
            if not out or math.dist(out[-1][:2], m.start) > 1e-9:
                out.append((float(m.start[0]), float(m.start[1]), False))
            out.append((float(m.end[0]), float(m.end[1]), bool(getattr(m, "laser", True))))
        elif name == "CutArc":
            a = Arc(tuple(m.start), tuple(m.end), tuple(m.center), bool(m.ccw))
            n = max(2, min(720, int(a.length() / arc_step_mm)))
            on = bool(getattr(m, "laser", True))
            for k in range(1, n + 1):
                x, y = a.point_at(k / n)
                out.append((x, y, on))
    return out
