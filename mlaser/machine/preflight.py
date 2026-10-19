"""Pre-flight check before Start, Frame, Dry run, Resume and absolute moves (docs/SAFETY.md §5).

:func:`check` returns a :class:`PreflightReport`: blocking **errors** and **warnings**.  The
controller refuses the action while the report has errors; the UI shows the report in one
confirmation dialog (warnings must be acknowledged).

Checks:

* ``fault`` latched / card ``alarms`` active (error);
* ``not_homed``: X and Y must report homed (axis status bit 15) when the policy requires it;
* ``not_armed``: a job that fires the laser needs the machine armed;
* ``layer_undefined``: every layer the parts use must have parameters (in the document, or
  from the machine's layer table).  An undefined layer used to default silently to 100 %
  power at 5000 Hz; it is an **error** for a cut and a warning for laser-off runs;
* ``open_contour`` (warning), ``zero_length`` (warning), ``bad_geometry`` (NaN / infinite
  coordinates, error);
* ``out_of_travel``: the extent of the compiled job (exact: every step item from the start
  position, including leads, kerf and rapids) or the frame / move target must lie inside the
  travel limits.  Limits come from the card's soft limits read back at connect (axis
  parameter words 1/2), else from the machine parameters (``MAC.SoftLimitMaxLen``), else they
  are unknown (warning).

Pure functions, no I/O; the numbers come from the caller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

ERROR, WARNING = "error", "warning"
ACTIONS = ("start", "dry_run", "frame", "resume", "move", "laser_test")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str             # "error" | "warning"
    message: str
    where: Optional[tuple] = None    # (part index, contour index) or a point

    @property
    def blocking(self) -> bool:
        return self.severity == ERROR


@dataclass(frozen=True)
class TravelLimits:
    x: tuple[float, float]
    y: tuple[float, float]
    source: str = "machine parameters"      # "card soft limits" | "machine parameters"

    def contains(self, x: float, y: float, tol: float = 1e-6) -> bool:
        return (self.x[0] - tol <= x <= self.x[1] + tol) and (self.y[0] - tol <= y <= self.y[1] + tol)

    def describe(self) -> str:
        return (f"X {self.x[0]:g}..{self.x[1]:g} mm, Y {self.y[0]:g}..{self.y[1]:g} mm "
                f"({self.source})")


@dataclass
class PreflightReport:
    action: str
    issues: list[Issue] = field(default_factory=list)
    extent: Optional[tuple[float, float, float, float]] = None    # x0, y0, x1, y1 (mm)
    travel: Optional[TravelLimits] = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}

    def add(self, code: str, severity: str, message: str, where=None) -> None:
        self.issues.append(Issue(code, severity, message, where))

    def summary(self) -> str:
        if not self.issues:
            return f"pre-flight {self.action}: OK"
        head = f"pre-flight {self.action}: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        return head + "".join(f"\n  {i.severity.upper()}: {i.message}" for i in self.issues)


class PreflightError(RuntimeError):
    """Raised (or logged) when an action is refused by the pre-flight check."""

    def __init__(self, report: PreflightReport):
        super().__init__(report.summary())
        self.report = report


# ============================================================================ helpers
def travel_from_machine(machine) -> Optional[TravelLimits]:
    """Stroke from the machine parameters: ``[0, MAC.SoftLimitMaxLen]`` (X) and
    ``[0, MAC_1.SoftLimitMaxLen]`` (Y); the axis offset of the original (cfg +0xbcd8) is 0 in
    the rebuild (UiController._jog_distance)."""
    if machine is None or not hasattr(machine, "fparam"):
        return None
    try:
        sx = float(machine.fparam("MAC.SoftLimitMaxLen", 0.0))
        sy = float(machine.fparam("MAC_1.SoftLimitMaxLen", 0.0))
    except Exception:  # noqa: BLE001
        return None
    if sx <= 0 or sy <= 0:
        return None
    return TravelLimits((0.0, sx), (0.0, sy), "machine parameters")


def travel_from_card(soft_limits: Optional[dict], x_axis: int = 0, y_axis: int = 1) -> Optional[TravelLimits]:
    """``soft_limits``: axis -> (lo_mm, hi_mm) decoded from the card's axis parameter block."""
    if not soft_limits:
        return None
    lx, ly = soft_limits.get(x_axis), soft_limits.get(y_axis)
    if not lx or not ly or lx[1] <= lx[0] or ly[1] <= ly[0]:
        return None
    return TravelLimits((float(lx[0]), float(lx[1])), (float(ly[0]), float(ly[1])), "card soft limits")


def job_extent_mm(job) -> Optional[tuple[float, float, float, float]]:
    """Exact extent of a compiled job's step items from its start position (mm)."""
    m = getattr(job, "machine", None)
    px = float(getattr(m, "pulses_per_mm_x", 0.0) or 0.0)
    py = float(getattr(m, "pulses_per_mm_y", 0.0) or px)
    if px <= 0 or py <= 0:
        return None
    sx, sy = getattr(job, "start_pulses", (0, 0))
    try:
        import numpy as np
        w1 = np.fromiter((w[1] for w in job.wire if w and (w[0] & 0xFFFF) == 3000 and len(w) > 1),
                         dtype=np.int64)
        if w1.size == 0:
            return (sx / px, sy / py, sx / px, sy / py)
        dx = (w1 & 0xFF).astype(np.int64)
        dx = np.where(dx > 127, dx - 256, dx)
        dy = ((w1 >> 8) & 0xFF).astype(np.int64)
        dy = np.where(dy > 127, dy - 256, dy)
        cx = np.concatenate(([0], np.cumsum(dx))) + sx
        cy = np.concatenate(([0], np.cumsum(dy))) + sy
        return (float(cx.min()) / px, float(cy.min()) / py, float(cx.max()) / px, float(cy.max()) / py)
    except ImportError:  # pragma: no cover - numpy is a hard dependency
        x, y = sx, sy
        x0 = x1 = x
        y0 = y1 = y
        for w in job.wire:
            if w and (w[0] & 0xFFFF) == 3000:
                v = w[1]
                ddx, ddy = v & 0xFF, (v >> 8) & 0xFF
                x += ddx - 256 if ddx > 127 else ddx
                y += ddy - 256 if ddy > 127 else ddy
                x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
        return (x0 / px, y0 / py, x1 / px, y1 / py)


def used_layers(document) -> set[int]:
    return {int(getattr(getattr(p, "craft", None), "layer", 1)) for p in getattr(document, "parts", [])}


def layer_is_defined(document, layer: int, layer_table=None) -> bool:
    """A layer is defined when the document carries parameters for it that are not a UI
    placeholder (``LayerParams.extra["placeholder"]``), or the machine layer table supplies it."""
    lp = (getattr(document, "layers", None) or {}).get(layer)
    if lp is not None and not (getattr(lp, "extra", None) or {}).get("placeholder"):
        return True
    if layer_table is None:
        return False
    try:
        if hasattr(layer_table, "layer_params"):
            return layer_table.layer_params(layer) is not None
        return layer in layer_table
    except Exception:  # noqa: BLE001
        return False


def _finite(p) -> bool:
    return all(math.isfinite(float(v)) for v in p)


def check_geometry(document, rep: PreflightReport, selected_only: bool = False) -> None:
    for pi, part in enumerate(getattr(document, "parts", [])):
        if selected_only and not getattr(part, "selected", False):
            continue
        for ci, c in enumerate(getattr(part, "contours", [])):
            segs = getattr(c, "segments", [])
            bad = False
            length = 0.0
            for s in segs:
                pts = [getattr(s, "start", (0, 0)), getattr(s, "end", (0, 0))]
                if hasattr(s, "center"):
                    pts.append(s.center)
                if not all(_finite(p) for p in pts):
                    bad = True
                    break
                try:
                    length += float(s.length())
                except Exception:  # noqa: BLE001
                    length += math.dist(pts[0], pts[1])
            if bad or not math.isfinite(length):
                rep.add("bad_geometry", ERROR, f"part {pi + 1} contour {ci + 1}: NaN or infinite "
                        "coordinates", (pi, ci))
                continue
            if segs and length <= 1e-9:
                rep.add("zero_length", WARNING, f"part {pi + 1} contour {ci + 1}: zero length", (pi, ci))
            if segs and not getattr(c, "closed", True):
                rep.add("open_contour", WARNING, f"part {pi + 1} contour {ci + 1} is open", (pi, ci))


# ============================================================================ check
def check(action: str, *, document=None, job=None, extent=None, target=None,
          travel: Optional[TravelLimits] = None, homed: Optional[bool] = None,
          require_homed: bool = True, armed: bool = False, emits: bool = False,
          layer_table=None, alarms: Sequence = (), fault: str = "", selected_only: bool = False,
          head: Optional[tuple[float, float]] = None) -> PreflightReport:
    """Build the report for ``action`` (one of :data:`ACTIONS`).

    ``job``: the compiled job (its exact extent is checked); ``extent``: a bounding box
    (frame); ``target``: an absolute move target; ``emits``: the job can fire the laser;
    ``homed``: X/Y homed per the card (None = unknown)."""
    rep = PreflightReport(action, travel=travel)
    if fault:
        rep.add("fault", ERROR, f"a fault is latched: {fault} (reset it first)")
    alarms = list(alarms or ())
    if alarms:
        txt = ", ".join(getattr(a, "text_en", str(a)) for a in alarms[:4])
        rep.add("alarm", ERROR, f"card alarm active: {txt} (Clear Alarm first)")
    if require_homed and action != "laser_test":
        if homed is None:
            rep.add("homed_unknown", WARNING, "homing state unknown")
        elif not homed:
            rep.add("not_homed", ERROR, "X/Y are not homed: run Go Origin first")
    if emits and not armed:
        rep.add("not_armed", ERROR, "the job fires the laser but the machine is not armed")
    if document is not None:
        missing = sorted(li for li in used_layers(document) if not layer_is_defined(document, li, layer_table))
        for li in missing:
            sev = ERROR if emits else WARNING
            rep.add("layer_undefined", sev, f"layer {li} has no cutting parameters "
                    "(set them in the layer table; it would otherwise cut at 100 % power)", (li,))
        check_geometry(document, rep, selected_only)
    if job is not None and extent is None:
        extent = job_extent_mm(job)
        st = getattr(job, "start_mm", None)
        if extent is not None and head is not None and st is not None:
            # the wire is relative steps: from a head that is not at the job's start the job
            # runs translated by (head - start), so that is the extent to check (the start
            # gate refuses such a start anyway; this keeps the report honest)
            dx, dy = float(head[0]) - float(st[0]), float(head[1]) - float(st[1])
            if dx or dy:
                extent = (extent[0] + dx, extent[1] + dy, extent[2] + dx, extent[3] + dy)
    if extent is not None:
        x0, y0, x1, y1 = extent
        if head is not None:
            x0, y0, x1, y1 = min(x0, head[0]), min(y0, head[1]), max(x1, head[0]), max(y1, head[1])
            extent = (x0, y0, x1, y1)
        rep.extent = extent
        if not all(math.isfinite(v) for v in extent):
            rep.add("bad_geometry", ERROR, "the job extent is not finite")
        elif travel is None:
            rep.add("travel_unknown", WARNING, "machine travel limits unknown: bounds not checked")
        elif not (travel.contains(x0, y0) and travel.contains(x1, y1)):
            rep.add("out_of_travel", ERROR,
                    f"job extent X {x0:.1f}..{x1:.1f}, Y {y0:.1f}..{y1:.1f} mm is outside the "
                    f"travel {travel.describe()}")
    if target is not None:
        tx, ty = target
        if not (math.isfinite(tx) and math.isfinite(ty)):
            rep.add("bad_geometry", ERROR, "move target is not finite")
        elif travel is None:
            rep.add("travel_unknown", WARNING, "machine travel limits unknown: target not checked")
        elif not travel.contains(tx, ty):
            rep.add("out_of_travel", ERROR, f"target ({tx:.1f}, {ty:.1f}) mm is outside the travel "
                    f"{travel.describe()}")
    return rep


def merge(reports: Iterable[PreflightReport]) -> PreflightReport:
    reports = list(reports)
    out = PreflightReport(reports[0].action if reports else "start")
    for r in reports:
        out.issues.extend(r.issues)
        out.extent = out.extent or r.extent
        out.travel = out.travel or r.travel
    return out


__all__ = ["ERROR", "WARNING", "Issue", "TravelLimits", "PreflightReport", "PreflightError",
           "check", "travel_from_machine", "travel_from_card", "job_extent_mm", "used_layers",
           "layer_is_defined", "check_geometry", "merge"]
