"""Laser interferometer test ("干涉仪" mf180, command id 17000) and pitch compensation data.

The ribbon command (FUN_004bbc60) toggles the docked interferometer panel (cfg+0x40b8 =
visible, control bar at frame+0xcb94).  The panel edits the SoftParam block (O, ManuPara.xml):

=========================  ======  =========================================  =========
name                       lang    meaning                                    default
=========================  ======  =========================================  =========
SP.InterfereAxisIndex      pd455   axis {X, Y}                                X
SP.InterfereSpeed          pd456   move speed (mm/s)                          100
SP.InterfereLoopTimes      pd457   loop times                                 1
SP.InterfereStayTime       pd458   stay time at each point (s)                4
SP.InterfereTotalLength    pd459   move range (mm)                            3000
SP.InterfereStepLength     pd460   interval (mm)                              150
SP.InterfereIsFirstPtGap.. pd461   first-point gap adjust                     on
SP.InterfereGapAdjust      pd462   gap size (mm)                              5
=========================  ======  =========================================  =========

and has Simulate (FUN_00564610 / FUN_005c2c40: build + draw) and Start (FUN_005647c0: build +
VM +0x68 run).  Both build the program with FUN_0045fdf0(axis, home direction of that axis
(MAC.GoOriginalDirection cfg+0xbc6c+0xa0*card axis), range, step, first-gap flag, gap, stay,
speed, loops) (O):

* refuse unless speed >= 1, step >= 0.001 and step <= range;
* ``N = int(range / step)``; nominal points ``p_i = i * step``, i = 0..N;
* optional compensation of the targets: MP.CompensateType (cfg+0x4cd4) 2 = subtract the
  interpolated forward / backward error of the pitch table, 1 = a linear factor
  (cfg+0x40f8 X / 0x4100 Y); 0 = none;
* targets are rounded to whole position counts (1/ppm), multiplied by ``s = +1`` when the
  axis homes in the negative direction (home direction 0) else -1 (move away from home), and
  added to the current position;
* for each loop: move to ``p_0 - s*gap`` (backlash take-up; in the first loop only when the
  first-point gap adjust is on, otherwise p_0 is the current position and is skipped) with a
  stay, then p_0..p_N with a stay at each point, overshoot to ``p_N + s*gap`` with a stay,
  then p_N..p_0 with a stay at each point.  Moves use the interferometer speed; stays are
  delay items of ``stay * 1000`` ms (FUN_00464540 with ``1000 * stay``).

Pitch compensation measurement import (dialog ``cp*`` keys, file types ``*.rtl;*.ren;*.pos;
*.csv`` cp5): the rebuild reads CSV / whitespace tables of ``position, forward measured,
backward measured`` and computes the deviations and the mean backlash (cp8..cp16).  The
binary Renishaw formats and the ``File/PithCompensate.pcf`` record layout are U.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Optional

from .paramset import ParamSet
from .program import Dwell, Move

KEYS = ("SP.InterfereAxisIndex", "SP.InterfereSpeed", "SP.InterfereLoopTime", "SP.InterfereStayTime",
        "SP.InterfereTotalLength", "SP.InterfereStepLength", "SP.InterfereIsFirstPtGapAdjust",
        "SP.InterfereGapAdjust")


def params(settings=None) -> ParamSet:
    return ParamSet(KEYS, settings)


@dataclass
class PitchTable:
    """Measured pitch errors of one axis (cp7 position, cp8/cp10 measured, cp9/cp11 errors)."""
    positions: list[float] = field(default_factory=list)
    forward: list[float] = field(default_factory=list)     # measured positive direction
    backward: list[float] = field(default_factory=list)    # measured negative direction

    def errors(self) -> tuple[list[float], list[float]]:
        fe = [m - p for p, m in zip(self.positions, self.forward)]
        be = [m - p for p, m in zip(self.positions, self.backward)]
        return fe, be

    def backlash(self) -> list[float]:
        """cp12 per point: backward - forward measured."""
        return [b - f for f, b in zip(self.forward, self.backward)]

    def mean_backlash(self) -> float:
        """cp15 "Avg.Backlash (mm): %.4f"."""
        bl = self.backlash()
        return sum(bl) / len(bl) if bl else 0.0

    def error_at(self, x: float, forward: bool = True) -> float:
        """Linear interpolation of the error (FUN_0045fdf0 compensation, type 2)."""
        fe, be = self.errors()
        e = fe if forward else be
        if not self.positions:
            return 0.0
        ax = abs(x)
        pts = sorted(zip(self.positions, e))
        if ax <= pts[0][0]:
            return pts[0][1]
        for (p0, e0), (p1, e1) in zip(pts, pts[1:]):
            if p0 <= ax <= p1:
                t = (ax - p0) / (p1 - p0) if p1 > p0 else 0.0
                return e0 + (e1 - e0) * t
        return pts[-1][1]

    @classmethod
    def parse(cls, text: str) -> "PitchTable":
        """``position, forward, backward`` rows (comma, semicolon, tab or space separated);
        non-numeric lines (headers) are skipped."""
        t = cls()
        for row in csv.reader(io.StringIO(text.replace(";", ",").replace("\t", ","))):
            vals = []
            for cell in (c for part in row for c in part.split()):
                try:
                    vals.append(float(cell))
                except ValueError:
                    vals = []
                    break
            if len(vals) >= 2:
                t.positions.append(vals[0])
                t.forward.append(vals[1])
                t.backward.append(vals[2] if len(vals) > 2 else vals[1])
        return t

    def to_csv(self) -> str:
        fe, be = self.errors()
        out = ["position,forward_measured,backward_measured,forward_error,backward_error,backlash"]
        for i, p in enumerate(self.positions):
            out.append(f"{p:.4f},{self.forward[i]:.4f},{self.backward[i]:.4f},{fe[i]:.4f},{be[i]:.4f},"
                       f"{self.backward[i] - self.forward[i]:.4f}")
        return "\n".join(out) + "\n"


def nominal_points(total: float, step: float) -> list[float]:
    n = int(math.floor(total / step + 1e-9))
    return [i * step for i in range(n + 1)]


def build_program(p: ParamSet, start: tuple[float, float], home_dir: int = 0, ppm: float = 1000.0,
                  table: Optional[PitchTable] = None, comp_type: int = 0, factor: float = 0.0) -> list:
    """FUN_0045fdf0 -> list of :class:`Move` / :class:`Dwell` steps (machine coordinates)."""
    axis = int(p["SP.InterfereAxisIndex"])
    speed = float(p["SP.InterfereSpeed"])
    loops = max(1, int(p["SP.InterfereLoopTime"]))
    stay = float(p["SP.InterfereStayTime"])
    total = float(p["SP.InterfereTotalLength"])
    step = float(p["SP.InterfereStepLength"])
    first_gap = bool(p["SP.InterfereIsFirstPtGapAdjust"])
    gap = float(p["SP.InterfereGapAdjust"])
    if speed < 1.0 or step < 0.001 or step > total:
        raise ValueError("interferometer: speed >= 1, interval >= 0.001 and interval <= range required")
    nom = nominal_points(total, step)
    fwd = list(nom)
    bwd = list(nom)
    if comp_type == 2 and table is not None:
        fwd = [x - table.error_at(x, True) for x in nom]
        bwd = [x - table.error_at(x, False) for x in nom]
    elif comp_type == 1 and factor:
        fwd = [x * (1.0 + factor) for x in nom]
        bwd = list(fwd)
    s = 1.0 if home_dir == 0 else -1.0
    q = 1.0 / ppm if ppm > 0 else 0.0

    def target(off: float) -> tuple[float, float]:
        v = round(off / q) * q if q else off
        x, y = start
        return (x + s * v, y) if axis == 0 else (x, y + s * v)

    def move(off, label=""):
        x, y = target(off)
        return Move(x, y, speed, label)

    steps: list = []
    for loop in range(loops):
        first = 1
        if loop > 0 or first_gap:
            steps.append(move(fwd[0] - gap, "gap"))
            first = 0
            if stay > 0:                               # "if (items) dwell" after the gap move
                steps.append(Dwell(stay, "gap"))
        for i in range(first, len(fwd)):
            steps.append(move(fwd[i], f"+{i}"))
            if stay > 0:
                steps.append(Dwell(stay, f"+{i}"))
        steps.append(move(bwd[-1] + gap, "overshoot"))
        if stay > 0:
            steps.append(Dwell(stay, "overshoot"))
        for i in range(len(bwd) - 1, -1, -1):
            steps.append(move(bwd[i], f"-{i}"))
            if stay > 0:
                steps.append(Dwell(stay, f"-{i}"))
    return steps


def program_path(steps: list) -> list[tuple[float, float]]:
    """Points of the moves (for the Simulate button)."""
    return [(s.x, s.y) for s in steps if isinstance(s, Move)]
