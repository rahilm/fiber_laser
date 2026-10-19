"""Guided laser power / PWM test with the safety arming flow (docs/SAFETY.md §2-§3).

The machine is armed only inside this procedure: an explicit "Arm" step (the UI asks for a
confirmation first), then short test pulses in place at the chosen duty levels
(``machine.laser_test``: zero-motion FIFO steps with the laser byte, refused unless armed), a
table for the measured power, and it is **always disarmed** at the end, on abort and on a
failure.  Refused in dry-run mode (``--laser-off``), with a latched fault or active alarms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .engine import Procedure, Step, StepFailed

MAX_PULSE_S = 2.0
DEFAULT_LEVELS = (10.0, 30.0, 50.0)


@dataclass
class PulseResult:
    duty_pct: float
    duration_ms: float
    fired: bool
    measured_w: Optional[float] = None       # entered by the operator (power meter)
    note: str = ""


@dataclass
class LaserTestParams:
    levels: list[float] = field(default_factory=lambda: list(DEFAULT_LEVELS))
    duration_ms: float = 100.0
    pause_s: float = 1.0                     # between pulses (source and meter settle)

    def validate(self) -> None:
        if not self.levels:
            raise StepFailed("wizard.laser.error.no_levels")
        for v in self.levels:
            if not 0.0 < float(v) <= 100.0:
                raise StepFailed("wizard.laser.error.level", value=v)
        if not 0.0 < self.duration_ms <= MAX_PULSE_S * 1000.0:
            raise StepFailed("wizard.laser.error.duration", max_ms=int(MAX_PULSE_S * 1000))


class LaserTestProcedure(Procedure):
    id = "laser_test"
    title = "wizard.laser.title"
    min_mode = "engineer"

    def __init__(self, machine: Any, params: Optional[LaserTestParams] = None, **kw: Any) -> None:
        self.params = params or LaserTestParams()
        self.pulses: list[PulseResult] = []
        self.armed_by_us = False
        super().__init__(machine, **kw)

    def build_steps(self) -> list[Step]:
        notes = ("wizard.laser.safety_eyewear", "wizard.laser.safety_enclosure",
                 "wizard.laser.safety_target", "wizard.laser.safety_people", "wizard.safety.abort")
        return [
            Step("intro", "wizard.laser.intro_title", "wizard.laser.intro_text", "info", safety=notes),
            Step("params", "wizard.laser.params_title", "wizard.laser.params_text", "input"),
            Step("check", "wizard.check_title", "wizard.laser.check_text", "action", run=_check, auto=True),
            Step("arm", "wizard.laser.arm_title", "wizard.laser.arm_text", "action", run=_arm,
                 safety=notes[:4], action_label="wizard.laser.arm_button"),
            Step("fire", "wizard.laser.fire_title", "wizard.laser.fire_text", "action", run=_fire,
                 motion=True, safety=("wizard.laser.safety_eyewear",), action_label="wizard.laser.fire_button"),
            Step("results", "wizard.laser.results_title", "wizard.laser.results_text", "input"),
            Step("finish", "wizard.laser.finish_title", "wizard.laser.finish_text", "action",
                 run=_disarm, auto=True),
        ]

    def disarm(self, why: str) -> None:
        try:
            if self.machine.safety().armed:
                self.machine.disarm(why)
        except Exception:  # noqa: BLE001 - the halt already switched the beam off
            pass
        self.armed_by_us = False

    def on_abort(self) -> None:
        self.disarm("laser test aborted")

    def on_finish(self) -> None:
        self.disarm("laser test finished")

    def table(self) -> list[tuple]:
        """(duty %, duration ms, fired, measured W) rows for the report / UI."""
        return [(r.duty_pct, r.duration_ms, r.fired, r.measured_w) for r in self.pulses]


def _check(p: LaserTestProcedure) -> dict:
    p.require_connected()
    p.params.validate()
    if not p.machine.laser_enabled:
        raise StepFailed("wizard.laser.error.dry_run_mode")
    if p.machine.active_alarms:
        raise StepFailed("wizard.error.alarms", count=len(p.machine.active_alarms))
    rep = p.machine.preflight("laser_test", emits=True)
    errs = [i for i in rep.errors if i.code != "not_armed"]
    if errs:
        raise StepFailed("wizard.laser.error.preflight", detail="; ".join(i.message for i in errs))
    return {"warnings": [i.code for i in rep.warnings]}


def _arm(p: LaserTestProcedure) -> bool:
    p.check_cancel()
    if not p.machine.safety().armed:
        if not p.machine.arm():
            raise StepFailed("wizard.laser.error.arm_refused")
        p.armed_by_us = True
    return True


def _fire(p: LaserTestProcedure) -> list:
    pr = p.params
    p.pulses = []
    for i, duty in enumerate(pr.levels):
        p.check_cancel()
        if not p.machine.safety().armed:
            raise StepFailed("wizard.laser.error.not_armed")
        p.operator_action(f"pulse {i + 1}")
        p.machine.laser_test(pr.duration_ms / 1000.0, float(duty))
        p.sleep(0.05)
        p.wait(lambda: not p.machine.job_active(), 10.0 + pr.duration_ms / 1000.0, "laser pulse")
        fin = p.machine.events.last(_job_finished())
        fired = bool(fin is not None and fin.mode == "process" and fin.ok)
        p.pulses.append(PulseResult(float(duty), pr.duration_ms, fired))
        p.log(f"test pulse {duty:g} % for {pr.duration_ms:g} ms: {'fired' if fired else 'not fired'}")
        if not fired:
            raise StepFailed("wizard.laser.error.pulse", duty=duty)
        if i + 1 < len(pr.levels):
            p.sleep(pr.pause_s)
    return [r.duty_pct for r in p.pulses]


def _disarm(p: LaserTestProcedure) -> bool:
    p.disarm("laser test finished")
    return True


def _job_finished():
    from mlaser.machine.events import JobFinished

    return JobFinished


__all__ = ["LaserTestProcedure", "LaserTestParams", "PulseResult", "DEFAULT_LEVELS", "MAX_PULSE_S"]
