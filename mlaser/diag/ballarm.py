"""Ball-bar test pattern ("球杆仪" Ball Arm mf702, command id 0x426b).

MainApp FUN_004bbd20 opens the parameter grid FUN_005ee240(0x1f) over GraphParam (O):
``GRP.BallArmGuideLineLength`` pd1100 (100 mm), ``GRP.BallArmGuideLineDir`` pd1106
{horizontal, vertical}, ``GRP.BallArmCircleRadius`` pd1101 (50 mm), ``GRP.BallArmCircleTimes``
pd1102 (2), ``GRP.BallArmCircleDir`` pd1103 {clockwise, anticlockwise}; on OK it calls
CADModule slot +0xcc (FUN_100f2c60 -> FUN_100c5bf0(radius, times, circle dir, guide length,
guide dir)), which replaces the drawing with two graphics:

* the guide line from (0, 0) to the circle start;
* one arc of ``times`` full turns.

Geometry (FUN_100c5bf0, constants pi / 2pi / pi/2 / 3 at 0x101212f8, 0x1013dc50, 0x1013dc58,
0x1011da80, O):

==========  ============  ====================  ================  ==========================
guide dir   circle dir    line end              centre            start angle, sweep
==========  ============  ====================  ================  ==========================
horizontal  clockwise     ( L, 0)               ( L+R, 0)         pi,   -n*2pi
horizontal  anticlock.    (-L, 0)               (-L-R, 0)         0,    +n*2pi
vertical    clockwise     (0,  L)               (0,  L+R)         3pi/2, -n*2pi
vertical    anticlock.    (0, -L)               (0, -L-R)         pi/2, +n*2pi
==========  ============  ====================  ================  ==========================

The rebuild stores the n turns as n consecutive full-circle :class:`~mlaser.model.Arc`
segments of one contour.
"""
from __future__ import annotations

import math

from mlaser.model import Arc, Contour, Document, Line, Part

from .paramset import ParamSet

KEYS = ("GRP.BallArmGuideLineLength", "GRP.BallArmGuideLineDir", "GRP.BallArmCircleRadius",
        "GRP.BallArmCircleTimes", "GRP.BallArmCircleDir")


def params(settings=None) -> ParamSet:
    return ParamSet(KEYS, settings)


def geometry(length: float, guide_dir: int, radius: float, times: int, circle_dir: int):
    """(line_end, centre, start_angle, sweep) per FUN_100c5bf0."""
    L, R, n = float(length), float(radius), max(1, int(times))
    if guide_dir == 0:
        if circle_dir == 0:
            return (L, 0.0), (L + R, 0.0), math.pi, -n * 2 * math.pi
        return (-L, 0.0), (-L - R, 0.0), 0.0, n * 2 * math.pi
    if circle_dir == 0:
        return (0.0, L), (0.0, L + R), 1.5 * math.pi, -n * 2 * math.pi
    return (0.0, -L), (0.0, -L - R), 0.5 * math.pi, n * 2 * math.pi


def make_parts(p: ParamSet) -> list[Part]:
    end, c, a0, sweep = geometry(p["GRP.BallArmGuideLineLength"], int(p["GRP.BallArmGuideLineDir"]),
                                 p["GRP.BallArmCircleRadius"], int(p["GRP.BallArmCircleTimes"]),
                                 int(p["GRP.BallArmCircleDir"]))
    r = float(p["GRP.BallArmCircleRadius"])
    start = (c[0] + r * math.cos(a0), c[1] + r * math.sin(a0))
    start = (round(start[0], 9), round(start[1], 9))
    line = Part([Contour([Line((0.0, 0.0), end)], closed=False)], name="BallArm guide line")
    ccw = sweep > 0
    turns = int(round(abs(sweep) / (2 * math.pi)))
    arcs = [Arc(start, start, c, ccw) for _ in range(turns)]
    circle = Part([Contour(arcs, closed=True)], name=f"BallArm circle x{turns}")
    return [line, circle]


def make_document(p: ParamSet, layers=None) -> Document:
    return Document(parts=make_parts(p), layers=dict(layers or {}))
