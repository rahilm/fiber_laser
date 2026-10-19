"""Guided focus (autofocus head) homing.

Homes the motorised focus the way ``A240827_1`` does for the configured ``AF.AFType``
(:meth:`mlaser.controller.machine_ops.MachineOps.home_af`): network AF head (types 1/2, AF reg
101 := 2), EtherCAT bridge (3), an IO pulse (4) or a card axis (5: ``[2, 0x10, 0]``, 6: the
``AF.AFAxis`` axis), then waits for the card axis to report homed (types 5/6).  A dry run uses
a card-axis focus on the simulator.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .engine import Procedure, Step, StepFailed

AF_TYPES = {0: "wizard.focus.type_none", 1: "wizard.focus.type_net", 2: "wizard.focus.type_net",
            3: "wizard.focus.type_ec", 4: "wizard.focus.type_io", 5: "wizard.focus.type_axis",
            6: "wizard.focus.type_axis"}
HOME_TIMEOUT_S = 60.0


class FocusHomingProcedure(Procedure):
    id = "focus_home"
    title = "wizard.focus.title"
    min_mode = "operator"

    def __init__(self, machine: Any, values: Optional[dict] = None, timeout_s: float = HOME_TIMEOUT_S,
                 home_fn: Optional[Callable[[], bool]] = None, **kw: Any) -> None:
        """``home_fn``: the application's ``MachineOps.home_af`` (a private MachineOps is built
        for a dry run or without one)."""
        self.values = dict(values or {})
        self.home_fn = home_fn
        self.timeout_s = timeout_s
        super().__init__(machine, **kw)
        if self.dry_run:
            self.values.update({"AF.AFType": 5})

    def build_steps(self) -> list[Step]:
        return [
            Step("intro", "wizard.focus.intro_title", "wizard.focus.intro_text", "info",
                 safety=("wizard.focus.safety_head", "wizard.safety.no_laser", "wizard.safety.abort")),
            Step("check", "wizard.check_title", "wizard.focus.check_text", "action", run=_check, auto=True),
            Step("home", "wizard.focus.home_title", "wizard.focus.home_text", "action", run=_home,
                 motion=True, safety=("wizard.safety.motion",), action_label="wizard.focus.home_button"),
            Step("done", "wizard.focus.done_title", "wizard.focus.done_text", "info"),
        ]

    @property
    def af_type(self) -> int:
        try:
            return int(float(self.values.get("AF.AFType", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def axis(self) -> Optional[int]:
        if self.af_type == 5:
            return 4
        if self.af_type == 6:
            return int(float(self.values.get("AF.AFAxis", 4) or 4))
        return None


def _check(p: FocusHomingProcedure) -> dict:
    p.require_connected()
    t = p.af_type
    if t == 0:
        raise StepFailed("wizard.focus.error.none")
    if t not in AF_TYPES:
        raise StepFailed("wizard.focus.error.unsupported", type=t)
    return {"type": t, "type_key": AF_TYPES[t]}


def _home(p: FocusHomingProcedure) -> dict:
    from mlaser.controller.machine_ops import MachineOps

    p.operator_action("focus home")
    fn = p.home_fn if (p.home_fn is not None and not p.dry_run) else MachineOps(p.machine, p.values, log=p.log).home_af
    if not fn():
        raise StepFailed("wizard.focus.error.refused")
    ax = p.axis()
    if ax is None:
        return {"homed": None}
    card = p.machine.card()
    if card is None:
        raise StepFailed("wizard.error.not_connected")

    def homed() -> bool:
        axes = card.read_status().axes
        return ax < len(axes) and axes[ax].homed and not axes[ax].busy
    p.sleep(0.1)
    p.wait(homed, p.timeout_s, "focus axis homing", poll_s=0.05)
    return {"homed": True, "axis": ax}


__all__ = ["FocusHomingProcedure", "AF_TYPES"]
