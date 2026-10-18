"""Generic motion items produced by the CAM pipeline (``build_toolpath``).

These are deliberately controller-agnostic: the controller/protocol layer maps
them to MCC100 FIFO wire items (see docs/cam.md, "MotionItem stream").
Coordinates are machine mm (after part placement), speeds mm/s, times seconds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

Point = tuple[float, float]


@dataclass
class Rapid:
    """Laser-off traverse ("jump", G00).  ``start`` is informative."""
    start: Point
    end: Point
    lift: bool = True          # head goes up (height follower off) before the move


@dataclass
class CutLine:
    start: Point
    end: Point
    feed: float                # mm/s
    layer: int = 1
    laser: bool = True         # False for a micro-joint gap travelled at cut speed


@dataclass
class CutArc:
    start: Point
    end: Point
    center: Point
    ccw: bool
    feed: float
    layer: int = 1
    laser: bool = True


@dataclass
class LayerChange:
    """Select process layer (power/frequency/gas/height parameter set)."""
    layer: int


@dataclass
class Pierce:
    """Pierce at the current point before the first cut of a contour."""
    point: Point
    layer: int = 1
    time_s: float = 0.0
    stages: list[dict] = field(default_factory=list)  # optional multi-stage piercing parameters


@dataclass
class LaserOn:
    layer: int = 1


@dataclass
class LaserOff:
    pass


@dataclass
class Delay:
    seconds: float


@dataclass
class IO:
    port: int
    value: bool


@dataclass
class Follow:
    """Height follower (ZF) on = cut height tracking, off = lift to safe height."""
    enable: bool


@dataclass
class Marker:
    """Non-motion bookkeeping (start/end of part/contour) for UI progress."""
    kind: str
    part: int = -1
    contour: int = -1
    info: Optional[dict] = None


MotionItem = Union[Rapid, CutLine, CutArc, LayerChange, Pierce, LaserOn, LaserOff,
                   Delay, IO, Follow, Marker]
MOVE_TYPES = (Rapid, CutLine, CutArc)
