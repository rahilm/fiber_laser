"""Coordinate systems (mf307): Float, Tool (work) origins 1..6, Manual; set / return origin.

Recovered from MainApp.exe (O = observed, I = inferred); docs/re/protocol.md §17.

* Ids 0xcf6d..0xcf74 (FUN_004b1130 -> FUN_00482950): ``MSC.CoordSysType`` (+0x42a0) :=
  id − 0xcf6d: 0 Float, 1..6 Tool 1..6, 7 Manual.  0xcf75 Set origin (FUN_004b1200), 0xcf76
  Return to origin (FUN_004b1270); both are disabled for Float (FUN_004b1150).
* Storage: tool origins ``+0x42c0 + 16·i`` (machine X/Y mm), serialised in ManuPara.xml as
  ``MSC.ToolCoordPtStr`` = ``"x1,y1,...,x6,y6"`` (writer FUN_005cd770, reader in FUN_004d90e0 at
  0x4dbb9a: split on ",", used only with exactly 12 tokens, otherwise all zero).  Mark points use
  the same layout in ``MSC.MarkPtStr``.  Nothing in softPara.ini / AutosaveParam.
* Set tool origin (FUN_005c9d50): position = NC +0x1a4 (axis RO k2 / ppm), tool[n−1] := (X, Y),
  log mp131 "Tool Coord %d Origin Point, (%.3f, %.3f)".
* Return to tool origin (FUN_005ca160): Δ = tool − current; speed 100 mm/s when the origin is
  (0, 0) (|x|, |y| ≤ 0.001), else MC.XFastMoveSpeed; acc MC.XFastMoveAcc; relative move
  ``[5, mask, v, a, 10a, dx, dy, 0, 0]`` through the panel step 9 (FUN_005bea00).
* Manual: set = NC +0x1a8 FUN_100314b0 = mailbox ``[9999, 8, axis mask]`` (the card zeroes its
  coordinates at the current head position), mark points reset; return = move to (0, 0) at
  min(XFastMoveSpeed, 100).
* Job placement (FUN_0045cc20): origin = tool[n−1] for types 1..6, else the head position;
  the drawing is translated by ``origin − anchor`` where the anchor is the dock point of the
  drawing's bounding box (FUN_004d2750, ``MSC.DockPtType`` 1..9 as a 3×3 grid: X %3 == 1 min,
  2 centre, 0 max; Y 1..3 max, 4..6 centre, 7..9 min; 0 = no translation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Point = tuple[float, float]

FLOAT, MANUAL = 0, 7
N_TOOLS = 6
NAMES = {0: "mf300", 1: "mf301", 2: "mf302", 3: "mf303", 4: "mf304", 5: "mf305", 6: "mf306",
         7: "mf513"}
COMMAND_IDS = {0xcf6d + i: i for i in range(8)}      # FUN_004b1130


def parse_points(text: str, n: int = N_TOOLS) -> list[Point]:
    """``MSC.ToolCoordPtStr`` / ``MSC.MarkPtStr`` reader (0x4dbb9a)."""
    toks = (text or "").split(",")
    pts = [(0.0, 0.0)] * n
    if len(toks) != 2 * n:
        return pts
    vals = []
    for t in toks:
        try:
            vals.append(float(t) if t.strip() else 0.0)
        except ValueError:
            vals.append(0.0)
    return [(vals[2 * i], vals[2 * i + 1]) for i in range(n)]


def format_points(pts: list[Point]) -> str:
    """FUN_005cd770 / FUN_005cd520: comma separated, 17 significant digits (lexical_cast)."""
    return ",".join("%.17g,%.17g" % (x, y) for x, y in pts)


def dock_anchor(bbox: tuple[float, float, float, float], dock_type: int) -> Optional[Point]:
    """FUN_004d2750: the drawing point that is placed on the origin (None for type 0)."""
    if not dock_type or dock_type < 1 or dock_type > 9:
        return None
    x0, y0, x1, y1 = bbox
    col = dock_type % 3
    x = x0 if col == 1 else ((x0 + x1) / 2 if col == 2 else x1)
    y = y1 if dock_type <= 3 else ((y0 + y1) / 2 if dock_type <= 6 else y0)
    return (x, y)


@dataclass
class CoordSystems:
    kind: int = FLOAT                                    # MSC.CoordSysType
    tools: list = field(default_factory=lambda: [(0.0, 0.0)] * N_TOOLS)
    marks: list = field(default_factory=lambda: [(0.0, 0.0)] * N_TOOLS)
    dock_type: int = 7                                   # MSC.DockPtType
    move_speed: float = 100.0                            # MC.XFastMoveSpeed
    move_acc: float = 4000.0                             # MC.XFastMoveAcc

    # ---- persistence (ManuPara.xml keys)
    def to_values(self) -> dict:
        return {"MSC.CoordSysType": self.kind, "MSC.ToolCoordPtStr": format_points(self.tools),
                "MSC.MarkPtStr": format_points(self.marks), "MSC.DockPtType": self.dock_type}

    @classmethod
    def from_values(cls, vals: Optional[dict]) -> "CoordSystems":
        vals = vals or {}
        c = cls()
        try:
            c.kind = int(float(vals.get("MSC.CoordSysType", 0) or 0))
        except (TypeError, ValueError):
            c.kind = FLOAT
        c.tools = parse_points(str(vals.get("MSC.ToolCoordPtStr", "") or ""))
        c.marks = parse_points(str(vals.get("MSC.MarkPtStr", "") or ""))
        for attr, key in (("dock_type", "MSC.DockPtType"), ("move_speed", "MC.XFastMoveSpeed"),
                          ("move_acc", "MC.XFastMoveAcc")):
            if vals.get(key) not in (None, ""):
                cur = getattr(c, attr)
                setattr(c, attr, type(cur)(float(vals[key])))
        return c

    # ---- operations
    def select(self, kind: int) -> None:
        if not 0 <= kind <= 7:
            raise ValueError(kind)
        self.kind = kind

    @property
    def can_set_or_return(self) -> bool:
        """FUN_004b1150: enabled only for types 1..7."""
        return 1 <= self.kind <= 7

    def set_tool_origin(self, pos: Point) -> int:
        """FUN_005c9d50: tool[n−1] := current position; returns n."""
        if not 1 <= self.kind <= N_TOOLS:
            raise ValueError("set tool origin needs a tool coordinate system (1..6)")
        self.tools[self.kind - 1] = (float(pos[0]), float(pos[1]))
        return self.kind

    def return_target(self) -> tuple[Point, float]:
        """Target and speed of the return move (FUN_005ca160 / FUN_005ca730)."""
        if self.kind == MANUAL:
            return (0.0, 0.0), min(self.move_speed, 100.0)
        if 1 <= self.kind <= N_TOOLS:
            t = self.tools[self.kind - 1]
            sp = 100.0 if abs(t[0]) <= 0.001 and abs(t[1]) <= 0.001 else self.move_speed
            return t, sp
        raise ValueError("return to origin is disabled for the float coordinate system")

    def job_origin(self, head: Point) -> Point:
        """FUN_0045cc20: tool origin for 1..6, the head position for Float / Manual."""
        if 1 <= self.kind <= N_TOOLS:
            return self.tools[self.kind - 1]
        return (float(head[0]), float(head[1]))

    def job_offset(self, bbox, head: Point) -> Point:
        """Translation applied to the drawing (``doc.extra["job_offset"]``)."""
        a = dock_anchor(bbox, self.dock_type)
        if a is None:
            return (0.0, 0.0)
        o = self.job_origin(head)
        return (o[0] - a[0], o[1] - a[1])
