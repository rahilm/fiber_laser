"""Look-ahead velocity planning (CVelocityPlanning).

The port lives in the parity fence, :mod:`mlaser.parity_ports.cadmodule.planner`; this module
is its domain-facing name.  ``plan_velocity`` dispatches through the active ParityProfile
(``motion`` slot); the types (``PathPoint``, ``PlannerParams``, ``Plan``, ...) are the port's.
"""
from ..parity_ports import export_ports

__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.planner",
                                 plan_velocity=("motion", "plan_velocity"))
           if not n.startswith("_")]
