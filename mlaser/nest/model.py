"""Nesting data model: parts, sheets, parameters and results.

The fields mirror what the original passes to its nesting engines (see docs/re/nest.md for the
evidence; SmartNest.dll ``NAddPartInfo``/``NAddSheetInfo``/``NDefineAngleList`` and AutoNest.dll
``Nest_SetPartPara``/``Nest_SetNestPara``). Units are millimetres and degrees.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..model import Part, Point

# Fit ("type of fit") of a part outline used by the nester.
FIT_SHAPE = 0        # true shape (outer contour with holes)
FIT_RECT = 1         # bounding rectangle only (fast; like "rectangular nesting")

# Gravity / start corner: which way parts are pushed (lexicographic placement score).
GRAVITY_LEFT = "left"      # fill columns from the left (minimise x, then y)
GRAVITY_BOTTOM = "bottom"  # fill rows from the bottom (minimise y, then x)
GRAVITY_RIGHT = "right"
GRAVITY_TOP = "top"
GRAVITY_AUTO = "auto"      # AutoNest direction 3 "自动(根据排样效果)": try both, keep the better

# Nesting engine (NestParams.engine). The raster engine is the default and the fallback.
ENGINE_RASTER = "raster"   # own FFT raster bottom-left fill (engine.Nester), no extra dependency
ENGINE_SPYRROW = "spyrrow"  # sparrow strip packing via spyrrow (optional extra ``nest``), sparrow_engine

# Sheet grain direction (NestParams.grain) for parts marked NestPart.extra['grain'].
GRAIN_NONE, GRAIN_X, GRAIN_Y = "", "x", "y"
# Skeleton cut-up (NestParams.scrap_cut, nest.skeleton).
SCRAP_OFF, SCRAP_GRID, SCRAP_GAPS = "off", "grid", "gaps"


@dataclass
class NestPart:
    """One part definition (the original's part list row: name ``np_PartName``,
    count ``np_PartNum``)."""
    parts: list[Part]                     # geometry (one or more model.Part; holes included)
    name: str = ""
    quantity: int = 1                     # required count (min)
    max_quantity: Optional[int] = None    # fill up to this count when space remains (None = quantity)
    priority: int = 0                     # higher = placed earlier
    angles: Optional[list[float]] = None  # allowed rotations (deg); None = NestParams.angles()
    mirror: bool = False                  # mirrored copies allowed
    fit: int = FIT_SHAPE
    id: int = -1                          # assigned by NestJob.add_part
    extra: dict = field(default_factory=dict)


@dataclass
class NestSheet:
    """A sheet (plate) or remnant. Rectangular by default; ``outline`` (a list of model.Part,
    e.g. imported from DXF) gives an arbitrary remnant shape whose holes/inner contours are
    defects that cannot be used."""
    width: float = 3000.0
    height: float = 1500.0
    quantity: int = 1
    name: str = ""
    origin: Point = (0.0, 0.0)            # lower-left of the rectangle in sheet coordinates
    outline: Optional[list[Part]] = None
    is_remnant: bool = False
    margin: Optional[float] = None        # overrides NestParams.margin for this sheet
    id: int = -1
    extra: dict = field(default_factory=dict)


@dataclass
class NestParams:
    """Engine parameters (NestParam section of ManuPara.xml / ``pd_Nest_*`` dialog).
    Defaults are the original's NP.* defaults (docs/re/nest.md)."""
    spacing: float = 3.0                  # NP.PartSpace (pd_Nest_PartSpace)
    margin: float = 5.0                   # NP.EdgeStock (pd_Nest_EdgeStock)
    rotation_step: float = 90.0           # NP.PartRoateStepAngle_deg: angles 0, step, ... < 360
    allow_mirror: bool = False            # the original always passes mirror 'N'
    part_in_part: bool = False            # NP.IsNestPartInner (pd_Nest_NestPartInner)
    common_line: bool = False             # NP.EnableShareEdge: spacing forced to 0 (kerf here)
    kerf: float = 0.0                     # spacing used when common_line is on (rebuild)
    share_edge_min_len: float = 20.0      # NP.ShareEdgeMinLen (pd_Nest_ShareEdgeMinLen)
    accuracy: float = 0.5                 # NP.NestAccuracy: curve flattening tolerance here
    only_selected: bool = False           # NP.OnlyNestSelectedPart
    gravity: str = GRAVITY_LEFT
    resolution: Optional[float] = None    # raster cell (mm); None = automatic
    time_limit_s: float = 30.0
    sort_by: str = "area"                 # placement order inside a priority: area | bbox | length
    compact: bool = True                  # exact slide after raster placement
    fill_sheets: bool = True              # use max_quantity to fill leftover space
    engine: str = ENGINE_RASTER           # ENGINE_RASTER | ENGINE_SPYRROW (rebuild option)
    # ---- rebuild options (docs/user/cutting-strategy.md); every default is "off"
    grain: str = GRAIN_NONE               # sheet grain: parts with NestPart.extra['grain'] keep their
                                          # local X axis along it (angles 0/180 or 90/270)
    scrap_cut: str = SCRAP_OFF            # skeleton cut-up after the parts: off | grid | gaps
    scrap_pitch: float = 500.0            # largest scrap piece (mm): the cut-up line pitch
    scrap_clearance: float = 0.0          # extra distance kept from the parts' kerf (0 = end in it)
    scrap_min_length: float = 1.0         # shorter cut-up lines are dropped (mm)
    remnant_cut: bool = False             # straight separation cut that frees the leftover strip
    remnant_min_width: float = 100.0      # only when the strip is at least this wide (mm)
    strategy_kerf: float = 0.2            # beam width assumed by the cut-up / separation lines (mm)
    lead_fix: bool = False                # sheets sent to the queue: lead collision check and fix
    tip_check: bool = False               # sheets sent to the queue: tip-avoiding cut order

    def angles(self) -> list[float]:
        step = self.rotation_step
        if not step or step <= 0 or step >= 360:
            return [0.0]
        n = max(1, int(round(360.0 / step)))
        return [round(i * step, 6) for i in range(n) if i * step < 360 - 1e-9]

    def part_angles(self, np_: "NestPart") -> list[float]:
        """Allowed rotations of one part: its own list, else 0/180 (grain X) or 90/270 (grain Y)
        for a part marked ``extra['grain']``, else :meth:`angles`."""
        if np_.angles:
            return list(np_.angles)
        if np_.extra.get("grain") and self.grain in (GRAIN_X, GRAIN_Y):
            return [0.0, 180.0] if self.grain == GRAIN_X else [90.0, 270.0]
        return self.angles()

    def effective_spacing(self) -> float:
        return max(self.kerf, 0.0) if self.common_line else max(self.spacing, 0.0)


@dataclass
class Placement:
    """One placed copy. Transform: mirror about the part's local Y axis (x -> -x) if
    ``mirror``, then rotate by ``angle`` (deg, CCW) about the local origin, then translate by
    (x, y). The local origin is the original part coordinates."""
    part_id: int
    x: float
    y: float
    angle: float = 0.0
    mirror: bool = False

    def apply(self, p: Point) -> Point:
        px, py = p
        if self.mirror:
            px = -px
        a = math.radians(self.angle)
        c, s = math.cos(a), math.sin(a)
        return (px * c - py * s + self.x, px * s + py * c + self.y)


@dataclass
class SheetLayout:
    sheet_id: int
    placements: list[Placement] = field(default_factory=list)
    utilisation: float = 0.0              # net part area / sheet area
    used_extent: float = 0.0              # extent along the gravity direction (mm)
    remnant: Optional[tuple[float, float, float, float]] = None   # usable leftover rectangle
    extra: dict = field(default_factory=dict)   # e.g. 'name', stored 'graphs' from a project file


@dataclass
class NestResult:
    layouts: list[SheetLayout] = field(default_factory=list)
    placed: dict[int, int] = field(default_factory=dict)       # part_id -> count
    unplaced: dict[int, int] = field(default_factory=dict)     # part_id -> missing required count
    elapsed_s: float = 0.0
    resolution: float = 0.0
    gravity: str = ""                                           # direction actually used
    engine: str = ENGINE_RASTER                                 # engine that produced it
    notes: list[str] = field(default_factory=list)              # engine warnings (unsupported options)

    @property
    def utilisation(self) -> float:
        if not self.layouts:
            return 0.0
        return sum(l.utilisation for l in self.layouts) / len(self.layouts)


@dataclass
class NestJob:
    """A nest project: part list, sheet list, parameters and (optional) last result."""
    parts: list[NestPart] = field(default_factory=list)
    sheets: list[NestSheet] = field(default_factory=list)
    params: NestParams = field(default_factory=NestParams)
    result: Optional[NestResult] = None
    name: str = ""
    extra: dict = field(default_factory=dict)

    def add_part(self, np_: NestPart) -> NestPart:
        np_.id = max([p.id for p in self.parts] + [-1]) + 1
        if not np_.name:
            np_.name = (np_.parts[0].name if np_.parts and np_.parts[0].name else f"Part{np_.id + 1}")
        self.parts.append(np_)
        return np_

    def add_sheet(self, sh: NestSheet) -> NestSheet:
        sh.id = max([s.id for s in self.sheets] + [-1]) + 1
        if not sh.name:
            sh.name = f"Sheet{sh.id + 1}"
        self.sheets.append(sh)
        return sh

    def part(self, pid: int) -> NestPart:
        for p in self.parts:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def sheet(self, sid: int) -> NestSheet:
        for s in self.sheets:
            if s.id == sid:
                return s
        raise KeyError(sid)
