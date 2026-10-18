"""Step programs for the test functions (burn-in, interferometer, light test).

The original builds these as process-flow item lists and runs them through the NC module
(VM +0x68 "run program"; e.g. the interferometer program FUN_0045fdf0, the burn-in flow in
FUN_005aaa60).  The rebuild runs them step by step through the public
:class:`~mlaser.ui.facade.ControllerFacade` so they work with the card adapter and with the demo
controller: :class:`ProgramRunner.tick` is called from a Qt timer (or a loop in tests) and
advances when the current step is finished.

Limitation (requested from agent H): the facade's ``move_to(x, y)`` has no speed argument, so a
step's speed is only honoured when the controller offers ``move_to(x, y, speed=...)``.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

BUSY_MODES = ("process", "dry_run", "frame", "origin", "jog")


@dataclass
class Move:
    x: float
    y: float
    speed: float = 0.0          # mm/s, 0 = controller default
    label: str = ""


@dataclass
class MoveZ:
    z: float                    # absolute Z (mm); executed as a relative jog step
    label: str = ""


@dataclass
class Dwell:
    seconds: float
    label: str = ""


@dataclass
class DryRun:
    document: object
    label: str = ""


@dataclass
class Laser:
    on: bool
    power_pct: float = 0.0
    duration_s: float = 0.0     # >0: one timed shot (UiController.laser_test)
    label: str = ""


@dataclass
class Call:
    fn: Callable[[], None]
    label: str = ""


Step = object


@dataclass
class ProgramRunner:
    ctl: object
    steps: list = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    pos_tol: float = 0.05
    timeout_s: float = 600.0
    on_step: Optional[Callable[[int, object], None]] = None
    log: Callable[[str], None] = lambda m: None

    def __post_init__(self):
        self.index = -1
        self.state = "idle"          # idle | running | done | stopped | error
        self._t0 = 0.0
        self._target = None
        self._started = False

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self.index = -1
        self.state = "running"
        self._next()

    def stop(self) -> None:
        if self.state == "running":
            self.state = "stopped"
            try:
                self.ctl.stop()
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def current(self):
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    # ------------------------------------------------------------------ engine
    def _st(self):
        return self.ctl.state()

    def _next(self) -> None:
        self.index += 1
        if self.index >= len(self.steps):
            self.state = "done"
            return
        step = self.steps[self.index]
        self._t0 = self.clock()
        self._started = False
        if self.on_step is not None:
            try:
                self.on_step(self.index, step)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._begin(step)
        except Exception as e:  # noqa: BLE001 - surface and stop
            self.log(f"program step {self.index}: {e}")
            self.state = "error"

    def _begin(self, step) -> None:
        ctl = self.ctl
        if isinstance(step, Move):
            self._target = (step.x, step.y)
            try:
                if step.speed > 0:
                    ctl.move_to(step.x, step.y, speed=step.speed)
                else:
                    ctl.move_to(step.x, step.y)
            except TypeError:
                ctl.move_to(step.x, step.y)
        elif isinstance(step, MoveZ):
            z = self._st().z
            d = step.z - z
            self._target = step.z
            if abs(d) > 1e-6:
                ctl.jog_step("Z", 1 if d > 0 else -1, abs(d))
        elif isinstance(step, DryRun):
            ctl.dry_run(step.document)
        elif isinstance(step, Laser):
            if step.duration_s > 0 and hasattr(ctl, "laser_test"):
                ctl.laser_test(step.duration_s, step.power_pct or None)
            else:
                if step.power_pct:
                    ctl.set_laser_power(step.power_pct)
                ctl.set_output("laser", step.on)
        elif isinstance(step, Call):
            step.fn()

    def _finished(self, step) -> bool:
        st = self._st()
        busy = st.mode in BUSY_MODES
        if busy:
            self._started = True
        if isinstance(step, Move):
            near = math.dist((st.x, st.y), self._target) <= self.pos_tol
            return near and not busy
        if isinstance(step, MoveZ):
            return abs(st.z - self._target) <= self.pos_tol and not busy
        if isinstance(step, DryRun):
            return self._started and not busy or (not busy and self.clock() - self._t0 > 2.0)
        if isinstance(step, Dwell):
            return self.clock() - self._t0 >= step.seconds
        if isinstance(step, Laser) and step.duration_s > 0:
            return self.clock() - self._t0 >= step.duration_s and not busy
        return True

    def tick(self) -> bool:
        """Advance; returns True while the program is running."""
        if self.state != "running":
            return False
        step = self.current
        if step is None:
            self.state = "done"
            return False
        if self._finished(step):
            self._next()
        elif self.clock() - self._t0 > self.timeout_s and not isinstance(step, Dwell):
            self.log(f"program step {self.index} timed out")
            self.state = "error"
        return self.state == "running"

    def run_blocking(self, max_s: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                     period: float = 0.01) -> str:
        """Run to the end (tests / scripting)."""
        if self.state == "idle":
            self.start()
        t_end = time.monotonic() + max_s
        while self.tick() and time.monotonic() < t_end:
            sleep(period)
        return self.state
