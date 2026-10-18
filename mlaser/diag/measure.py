"""Data measurement ("数据测定" mf229, command id 0x3a9b).

Observed: the command (FUN_004b8ef0) only toggles the byte cfg+0x4327 (cleared at start-up,
FUN_0047dba4; the ribbon check state follows it, FUN_004b8f20).  No reader of that byte exists
in MainApp, NCModule, CADModule, ParaModule or ControlModule (objdump search for
``[reg+0x4327]``), so in this build the switch has no further effect (O).

Rebuild (I): while the switch is on, a :class:`DataRecorder` samples the machine state (time,
X, Y, Z, path speed, laser output, follower values when available) at the UI poll rate so the
measurement the button name promises is available; it can be saved as CSV.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from typing import Callable, Optional

COLUMNS = ("t_s", "x_mm", "y_mm", "z_mm", "speed_mm_s", "laser", "mode")


@dataclass
class Sample:
    t: float
    x: float
    y: float
    z: float
    speed: float
    laser: bool
    mode: str
    extra: Optional[dict] = None


class DataRecorder:
    def __init__(self, max_samples: int = 200000, clock: Callable[[], float] = time.monotonic):
        self.samples: list[Sample] = []
        self.max_samples = max_samples
        self.clock = clock
        self.t0: Optional[float] = None
        self.enabled = False

    def start(self) -> None:
        self.samples.clear()
        self.t0 = self.clock()
        self.enabled = True

    def stop(self) -> None:
        self.enabled = False

    def sample(self, state, extra: Optional[dict] = None) -> Optional[Sample]:
        if not self.enabled:
            return None
        if self.t0 is None:
            self.t0 = self.clock()
        s = Sample(self.clock() - self.t0, float(state.x), float(state.y), float(state.z),
                   float(state.speed), bool(state.outputs.get("laser", False)), str(state.mode), extra)
        self.samples.append(s)
        if len(self.samples) > self.max_samples:
            del self.samples[: len(self.samples) - self.max_samples]
        return s

    def summary(self) -> dict:
        if not self.samples:
            return dict(n=0)
        sp = [s.speed for s in self.samples]
        return dict(n=len(self.samples), duration_s=self.samples[-1].t, max_speed=max(sp),
                    mean_speed=sum(sp) / len(sp),
                    x_range=(min(s.x for s in self.samples), max(s.x for s in self.samples)),
                    y_range=(min(s.y for s in self.samples), max(s.y for s in self.samples)))

    def save_csv(self, path: str) -> int:
        keys = sorted({k for s in self.samples if s.extra for k in s.extra})
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(list(COLUMNS) + keys)
            for s in self.samples:
                w.writerow([f"{s.t:.3f}", f"{s.x:.4f}", f"{s.y:.4f}", f"{s.z:.4f}", f"{s.speed:.3f}",
                            int(s.laser), s.mode] + [(s.extra or {}).get(k, "") for k in keys])
        return len(self.samples)
