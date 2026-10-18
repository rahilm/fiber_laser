"""1-D motion profiles (S-curve solver, trapezoid, accel-only; MotionCtrl / CADModule).

The port lives in :mod:`mlaser.parity_ports.cadmodule.profile`; ``make_profile`` and
``scurve_profile`` dispatch through the active ParityProfile (``motion`` slot).
"""
from ..parity_ports import export_ports

__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.profile",
                                 make_profile=("motion", "make_profile"), scurve_profile=("motion", "scurve_profile"))
           if not n.startswith("_")]
