"""Breakpoint resume and Backward/Forward along the path (docs/protocol.md §12).

Original behaviour (observed, MainApp.exe / NCModule.dll):

* **Pause is a stop.**  The Pause button (MainApp ``FUN_005b9a60`` "Sys Pause") sets the job state
  app+0x484c = 11 and calls CNCModule +0x58 = "send stop manu cmd" (VM event 10 -> the program
  stop of ``FUN_100571f0``: decelerated axis stop, PWM off, process outputs off, follower stop).
  The FIFO contents are not resumed; "BreakCon"/Start continues *from the breakpoint*.
* **Breakpoint record**: while machining (state 2) ``FUN_004693c0`` writes
  ``File/AutosaveParam1.ini`` / ``AutosaveParam2.ini`` alternately (global counter DAT_009f2354,
  even -> 1, odd -> 2): counter, X*1000, Y*1000 (µm, rounded, ``FUN_00457b80`` position), and two
  ints from NC vtable +0x278 (= VM +0x138 ``FUN_10039820`` -> VM+0xec/+0xf0 = **RO[23], RO[24]**,
  the card's breakpoint indices).  ``ManuContour.dat`` holds the list of graphs done/to resume.
* **Backward / Forward** (``mp61``/``mp62``, length ``mp65`` "BF Len") move the head along the
  cut path with the laser off while paused.  Their wire sequence was not recovered (U); the
  rebuild replays the path's own step deltas (reversed for Backward) with the duty byte cleared.

Rebuild conventions (INFERRED, documented): the breakpoint is the executed *wire index* of the
job.  In AutosaveParam, ``graph_index`` = ordinal of the job marker (contour) containing it and
``path_index`` = wire items since that marker, so ``wire = markers[graph][0] + path``.
"""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..protocol import frames as F
from ..formats.scfile import AutosaveParam, newest_autosave


def _is_step(w) -> bool:
    return bool(w) and (w[0] & 0xFFFF) == F.OP_STEP


def _ppm(job) -> tuple[float, float]:
    m = job.machine
    return float(m.pulses_per_mm_x), float(m.pulses_per_mm_y)


def pulse_positions(job) -> list[tuple[int, int]]:
    """Absolute pulse position *before* each wire item (len = len(wire) + 1)."""
    x, y = job.start_pulses
    out = [(x, y)]
    for w in job.wire:
        if _is_step(w):
            dx, dy = F.decode_step(w[1:3])[:2]
            x += dx
            y += dy
        out.append((x, y))
    return out


def position_mm(job, index: int, _pos: Optional[list] = None) -> tuple[float, float]:
    """Machine position (mm) before wire item ``index``."""
    pos = _pos or pulse_positions(job)
    index = max(0, min(index, len(pos) - 1))
    px, py = _ppm(job)
    return pos[index][0] / px, pos[index][1] / py


def marker_at(job, index: int) -> int:
    """Ordinal of the last marker at or before ``index`` (-1 if none)."""
    k = -1
    for j, (wi, _p, _c) in enumerate(job.markers):
        if wi <= index:
            k = j
        else:
            break
    return k


def contour_start(job, index: int) -> int:
    """Wire index where the contour containing ``index`` begins (its marker), else 0."""
    k = marker_at(job, index)
    return job.markers[k][0] if k >= 0 else 0


def to_graph_path(job, index: int) -> tuple[int, int]:
    """Wire index -> (graph_index, path_index) for AutosaveParam (rebuild convention)."""
    k = marker_at(job, index)
    base = job.markers[k][0] if k >= 0 else 0
    return k, index - base


def from_graph_path(job, graph: int, path: int) -> int:
    """(graph_index, path_index) -> wire index (inverse of :func:`to_graph_path`)."""
    base = job.markers[graph][0] if 0 <= graph < len(job.markers) else 0
    return max(0, min(len(job.wire), base + path))


# ----------------------------------------------------------------------------- along path
def offset_index(job, index: int, distance_mm: float) -> int:
    """Wire index reached by moving ``distance_mm`` along the step path from ``index``
    (negative = backward).  Stops at the job ends."""
    px, py = _ppm(job)
    left = abs(distance_mm)
    i = index
    if distance_mm < 0:
        while i > 0 and left > 1e-9:
            i -= 1
            w = job.wire[i]
            if _is_step(w):
                dx, dy = F.decode_step(w[1:3])[:2]
                left -= math.hypot(dx / px, dy / py)
        return i
    n = len(job.wire)
    while i < n and left > 1e-9:
        w = job.wire[i]
        if _is_step(w):
            dx, dy = F.decode_step(w[1:3])[:2]
            left -= math.hypot(dx / px, dy / py)
        i += 1
    return i


def along_path(job, index: int, distance_mm: float):
    """(laser-off :class:`Job` that moves along the path, new wire index).

    Backward replays the step deltas between the new and the current index in reverse with the
    sign flipped; Forward replays them as they are.  All duty/power fields are cleared, so the
    beam stays off; non-step items (process, IO) are skipped.
    """
    from .compiler import Job
    new = offset_index(job, index, distance_mm)
    lo, hi = (new, index) if new < index else (index, new)
    seg = [w for w in job.wire[lo:hi] if _is_step(w)]
    wire = []
    order = reversed(seg) if new < index else seg
    sign = -1 if new < index else 1
    for w in order:
        dx, dy, freq, _duty, _p = F.decode_step(w[1:3])
        wire.append(F.encode_step(sign * dx, sign * dy, freq, 0, 0))
    pos = pulse_positions(job)
    m = job.machine
    sx, sy = pos[max(0, min(index, len(pos) - 1))]
    ex, ey = pos[max(0, min(new, len(pos) - 1))]
    px, py = _ppm(job)
    j = Job(wire=wire, machine=m, start_mm=(sx / px, sy / py), start_pulses=(sx, sy),
            end_pulses=(ex, ey), end_mm=(ex / px, ey / py), steps=len(wire),
            duration_s=len(wire) * m.period_ms / 1000.0, dry_run=True)
    return j, new


# ----------------------------------------------------------------------------- resume
_STATE_SUBS = {F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT, F.SYS_PWM, F.SYS_PWM_BUS, F.SYS_DA,
               getattr(F, "SYS_LASER_SYNC", 18)}


def _state_items(job, upto: int) -> list[list[int]]:
    """System items (outputs, PWM, DA, laser sync) that set state before ``upto``: the last one
    per (sub-command, channel/mask) so the machine is in the same state at the breakpoint."""
    last: dict = {}
    for w in job.wire[:upto]:
        if w and (w[0] & 0xFFFF) == F.OP_SYS and len(w) > 1 and w[1] in _STATE_SUBS:
            key = (w[1], w[2] if w[1] in (F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT, F.SYS_DA) and len(w) > 2 else 0)
            last[key] = list(w)
    return list(last.values())


def _preamble(job, start: int, index: int) -> list[list[int]]:
    """Non-motion items of the contour's pre-cut block (pierce/follow process items and
    zero-delta dwells between the contour marker and the first moving step), replayed when
    resuming inside a contour so the head is lowered and the beam pierces again."""
    out = []
    for w in job.wire[start:index]:
        if _is_step(w):
            dx, dy = F.decode_step(w[1:3])[:2]
            if dx or dy:
                break
            out.append(list(w))
        else:
            out.append(list(w))
    return out


def _rapid_to_pulses(cur: tuple[float, float], target: tuple[float, float],
                     target_pulses: tuple[int, int], m) -> tuple[list[list[int]], tuple[int, int]]:
    """Laser-off rapid wire from ``cur`` (mm) that ends exactly on ``target_pulses``.

    The rapid is compiled on its own, with a fresh pulse remainder (``carry = (0, 0)``) and,
    like every sampled move, no closing step at the exact end (the last sample is the last tick
    t <= T, docs/nc.md §1.1).  Inside one job the remainder carries that fraction into the next
    move; here the next move is the rest of the *original* job, whose deltas were quantised
    with a different remainder history and are replayed as they are.  So the rapid alone can
    stop one pulse short of (or past) the restart point, and everything after it would be
    shifted by that pulse.  The residual is therefore closed with a laser-off correction step
    right after the rapid's last moving step (before its settle dwell).
    """
    from ..toolpath import items as I
    from .compiler import compile_items
    ppm = (float(m.pulses_per_mm_x), float(m.pulses_per_mm_y))
    start = (round(cur[0] * ppm[0]), round(cur[1] * ppm[1]))
    wire: list[list[int]] = []
    end = start
    try:
        rj = compile_items([I.Rapid(cur, target)], m, cur, dry_run=True)
        wire, start, end = [list(w) for w in rj.wire], tuple(rj.start_pulses), tuple(rj.end_pulses)  # type: ignore[assignment]
    except ValueError:
        # sub-micron rapids: the S-curve degenerates (NaN samples); the correction steps
        # below cover the whole (few-pulse) distance
        pass
    rx, ry = target_pulses[0] - end[0], target_pulses[1] - end[1]
    if rx or ry:
        at, freq = len(wire), 0
        for i in range(len(wire) - 1, -1, -1):
            if _is_step(wire[i]):
                dx, dy, f, _d, _p = F.decode_step(wire[i][1:3])
                if dx or dy:
                    at, freq = i + 1, f
                    break
        n = max(1, math.ceil(max(abs(rx), abs(ry)) / m.max_step))
        fix, px, py = [], 0, 0
        for k in range(1, n + 1):
            ex, ey = round(rx * k / n), round(ry * k / n)
            fix.append(F.encode_step(ex - px, ey - py, freq, 0, 0))
            px, py = ex, ey
        wire[at:at] = fix
    return wire, start


def resume_job(job, index: int, current_mm: Optional[tuple[float, float]] = None,
               mode: str = "exact", machine=None, doc=None):
    """A new Job that continues ``job`` from wire ``index``.

    ``mode="contour"`` restarts the whole contour containing the breakpoint (from its marker,
    including its pierce block); ``mode="exact"`` restarts at the breakpoint itself and re-runs
    the contour's pre-cut process items there (``compiler.resume_items``: gas, follower on,
    PWM, laser sync; else the contour's own pre-cut block).  The job begins with a laser-off rapid
    from ``current_mm`` (default: the breakpoint) to the restart point and replays the output /
    PWM / DA state that was in effect at that point.
    """
    m = machine or job.machine
    index = max(0, min(index, len(job.wire)))
    cstart = contour_start(job, index)
    restart = cstart if mode == "contour" else index
    pos = pulse_positions(job)
    target = position_mm(job, restart, pos)
    cur = current_mm if current_mm is not None else target
    pre = []  # type: ignore[var-annotated]
    if math.dist(cur, target) > 1e-6:
        pre, start_pulses = _rapid_to_pulses(cur, target, pos[restart], m)
        start_mm = cur
    else:
        px, py = _ppm(job)
        start_mm, start_pulses = target, pos[restart]
    state = _state_items(job, restart)
    body = []
    if mode != "contour" and restart != cstart:
        # mid-cut: a fresh start-cut sequence (gas, follower, laser on, laser sync) from the
        # compiler when it can build one (compiler.resume_items), else the contour's own
        # pre-cut process items
        try:
            from .compiler import resume_items
            body = [list(w) for w in resume_items(job, index, doc)]
        except (ImportError, AttributeError):
            body = []
        if not body:
            body = _preamble(job, cstart, index)
    wire = pre + state + body + [list(w) for w in job.wire[restart:]]
    shift = len(pre) + len(state) + len(body) - restart
    markers = [(wi + shift, p, c) for wi, p, c in job.markers if wi >= restart]
    steps = sum(1 for w in wire if _is_step(w))
    kw = dict(wire=wire, start_mm=start_mm, start_pulses=tuple(start_pulses), markers=markers,
              steps=steps, duration_s=steps * m.period_ms / 1000.0, machine=m)
    kw = {k: v for k, v in kw.items() if hasattr(job, k)}
    new = dataclasses.replace(job, **kw)
    new.resume_from = index          # informative
    new.resume_offset = shift        # new wire index = old index + shift (for index >= restart)
    return new


# ----------------------------------------------------------------------------- autosave
class BreakpointStore:
    """AutosaveParam1/2.ini writer/reader (FUN_004693c0 layout, :mod:`mlaser.formats.scfile`)."""

    def __init__(self, directory: os.PathLike | str):
        self.dir = Path(directory)
        last = newest_autosave(self.dir) if self.dir.exists() else None
        self.counter = (last.counter + 1) if last else 0

    def save(self, x_mm: float, y_mm: float, graph_index: int, path_index: int) -> Path:
        """Write the next file (counter even -> AutosaveParam1.ini, odd -> 2)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = AutosaveParam(self.counter, int(round(x_mm * 1000)), int(round(y_mm * 1000)),
                            int(graph_index), int(path_index))
        p = self.dir / ("AutosaveParam1.ini" if self.counter % 2 == 0 else "AutosaveParam2.ini")
        rec.save(p)
        self.counter += 1
        return p

    def save_job_point(self, job, index: int, xy_mm: Optional[Sequence[float]] = None) -> Path:
        g, pth = to_graph_path(job, index)
        x, y = xy_mm if xy_mm is not None else position_mm(job, index)
        return self.save(x, y, g, pth)

    def load(self) -> Optional[AutosaveParam]:
        return newest_autosave(self.dir) if self.dir.exists() else None

    def wire_index(self, job) -> Optional[int]:
        rec = self.load()
        return None if rec is None else from_graph_path(job, rec.graph_index, rec.path_index)


@dataclass
class Breakpoint:
    """A held breakpoint (what UiController keeps while paused)."""
    job: object
    index: int
    x: float
    y: float
    mode: str = "process"
