"""What the operator screen and the LAN monitor show: one :class:`RunStatus` per refresh.

Qt-free.  :func:`run_status` turns the controller's state (``MachineState``), the safety status
(``SafetyStatus``), the active alarms, the planned job and the queue runner into the figures of
docs/review/ux.md §4.4: the state (``READY`` / ``RUNNING`` / ``HOLD`` / ``FAULT`` / ``ALARM`` /
``OFFLINE``) with its reason, progress, elapsed time and ETA, part n/N and contour n/N from the
job markers, and the live process values of the layer being cut.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

GAS_NAMES = {0: "Air", 1: "O2", 2: "N2", 3: "Air HP", 4: "O2 HP", 5: "N2 HP"}
KINDS = ("offline", "ready", "running", "hold", "fault", "alarm")
ICONS = {"offline": "○", "ready": "●", "running": "▶", "hold": "⏸", "fault": "⚠", "alarm": "⚠"}


@dataclass
class RunStatus:
    kind: str = "offline"                 # one of KINDS
    reason: str = ""                      # i18n id or text (see ``reason_args``)
    reason_args: dict = field(default_factory=dict)
    mode: str = ""                        # process | dry_run | frame (while running / held)
    armed: bool = False
    dry_run: bool = False                 # laser disabled (--laser-off)
    simulator: bool = False
    progress: float = 0.0                 # 0..1
    elapsed_s: float = 0.0
    eta_s: Optional[float] = None         # remaining seconds, None = unknown
    contour: int = 0                      # 1-based index of the contour being cut, 0 = unknown
    contours: int = 0
    part: int = 0                         # 1-based ordinal of the part being cut
    parts: int = 0
    part_index: int = -1                  # index into the placed document's parts
    done_parts: tuple = ()                # part indices already cut
    speed: float = 0.0                    # mm/s
    power_pct: Optional[float] = None
    gas: str = ""
    gas_bar: Optional[float] = None
    height_mm: Optional[float] = None     # layer cut height
    z: float = 0.0
    x: float = 0.0
    y: float = 0.0
    breakpoint: bool = False
    alarms: list = field(default_factory=list)   # (code, text-or-alarm) as given

    @property
    def icon(self) -> str:
        return ICONS.get(self.kind, "")

    @property
    def running(self) -> bool:
        return self.kind == "running"


def marker_position(plan: Any, k: int) -> tuple[int, int, int, int, int, tuple]:
    """``(contour, contours, part ordinal, parts, part index, done parts)`` for marker ``k``
    of ``plan.job.markers`` (``(wire index, part, contour)`` per contour)."""
    markers = list(getattr(getattr(plan, "job", None), "markers", None) or [])
    if not markers:
        return 0, 0, 0, 0, -1, ()
    order: list[int] = []
    seen: set = set()
    for _w, p, _c in markers:
        if p not in seen:
            seen.add(p)
            order.append(p)
    if k < 0:
        return 0, len(markers), 0, len(order), -1, ()
    k = min(k, len(markers) - 1)
    part = markers[k][1]
    ordinal = order.index(part) + 1
    done = tuple(order[:ordinal - 1])
    return k + 1, len(markers), ordinal, len(order), part, done


def estimate_remaining(progress: float, elapsed_s: float, estimate_s: float) -> Optional[float]:
    """Remaining time: the planner's estimate at the start, the measured rate as the job
    advances (blended over the first 20 %)."""
    p = max(0.0, min(1.0, progress))
    est = estimate_s * (1.0 - p) if estimate_s > 0 else None
    if p <= 0.01 or elapsed_s <= 1.0:
        return est
    rate = elapsed_s * (1.0 - p) / p
    if est is None:
        return rate
    w = min(1.0, p / 0.2)
    return (1.0 - w) * est + w * rate


def run_status(ms: Any, st: Any, alarms: list, plan: Any = None, elapsed_s: float = 0.0,
               breakpoint: Any = None) -> RunStatus:
    """The operator view of one moment.  ``ms``: ``MachineState`` (or None), ``st``:
    ``SafetyStatus`` (or None), ``plan``: the PlannedJob being run (or the preview)."""
    r = RunStatus()
    if st is not None:
        r.armed = bool(getattr(st, "armed", False))
        r.dry_run = bool(getattr(st, "dry_run", False))
        r.simulator = bool(getattr(st, "simulator", False))
    r.alarms = list(alarms or [])
    r.breakpoint = breakpoint is not None
    connected = bool(getattr(ms, "connected", False)) if ms is not None else False
    mode = getattr(ms, "mode", "offline") if ms is not None else "offline"
    if ms is not None:
        r.x, r.y, r.z = float(ms.x), float(ms.y), float(ms.z)
        r.speed = float(getattr(ms, "speed", 0.0) or 0.0)
    banner = getattr(st, "banner", "") if st is not None else ""
    running = mode in ("process", "dry_run", "frame")
    held = mode == "pause" or r.breakpoint
    if banner == "FAULT":
        r.kind, r.reason = "fault", "run.reason.fault"
        r.reason_args = {"fault": getattr(st, "fault", "")}
    elif r.alarms:
        r.kind, r.reason = "alarm", "run.reason.alarm"
        a = r.alarms[0]
        r.reason_args = {"code": getattr(a, "code", ""), "text": getattr(a, "text_en", str(a))}
    elif not connected:
        r.kind, r.reason = "offline", "run.reason.offline"
    elif held:
        r.kind = "hold"
        r.reason = "run.reason.breakpoint" if r.breakpoint else "run.reason.hold"
        if breakpoint is not None:
            r.reason_args = {"x": float(getattr(breakpoint, "x", 0.0)), "y": float(getattr(breakpoint, "y", 0.0))}
    elif running:
        r.kind = "running"
        r.reason = {"process": "run.reason.cutting", "dry_run": "run.reason.dry_run",
                    "frame": "run.reason.frame"}[mode]
    else:
        r.kind = "ready"
        r.reason = ("run.reason.laser_off" if r.dry_run else
                    "run.reason.armed" if r.armed else "run.reason.disarmed")
    if running or held:
        r.mode = mode if running else ("process" if breakpoint is None else getattr(breakpoint, "mode", "process"))
        r.progress = float(getattr(ms, "progress", 0.0) or 0.0) if ms is not None else 0.0
        r.elapsed_s = max(0.0, elapsed_s)
        est = 0.0
        if plan is not None:
            try:
                est = float((plan.estimate or {}).get("total", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError):
                est = 0.0
        r.eta_s = estimate_remaining(r.progress, r.elapsed_s, est)
        k = int(getattr(ms, "job_index", -1)) if ms is not None else -1
        (r.contour, r.contours, r.part, r.parts, r.part_index, r.done_parts) = marker_position(plan, k)
    if plan is not None and r.part_index >= 0:
        _layer_values(r, plan)
    elif ms is not None:
        r.power_pct = float(getattr(ms, "laser_power_pct", 0.0) or 0.0) if running else None
        r.gas = GAS_NAMES.get(int(getattr(ms, "gas_type", 0) or 0), "") if running else ""
    return r


def _layer_values(r: RunStatus, plan: Any) -> None:
    doc: Any = getattr(plan, "document", None)
    try:
        part = doc.parts[r.part_index]
        lp = doc.layers.get(part.craft.layer)
    except (AttributeError, IndexError, KeyError, TypeError):
        return
    if lp is None:
        return
    r.power_pct = float(lp.power_pct)
    r.gas = GAS_NAMES.get(int(lp.gas), str(lp.gas))
    r.gas_bar = float(lp.gas_pressure) if lp.gas_pressure else None
    r.height_mm = float(lp.cut_height)


def fmt_duration(s: Optional[float]) -> str:
    """``m:ss`` / ``h:mm:ss``; ``-`` when unknown."""
    if s is None or not math.isfinite(s) or s < 0:
        return "-"
    s = int(round(s))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


__all__ = ["RunStatus", "run_status", "marker_position", "estimate_remaining", "fmt_duration",
           "GAS_NAMES", "KINDS", "ICONS"]
