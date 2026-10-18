"""CContoutSmooth corner blending before velocity planning.

The port lives in :mod:`mlaser.parity_ports.cadmodule.smooth` (docs/motion.md §9);
``smooth_path_points`` dispatches through the active ParityProfile (``smoother`` slot).
"""
from ..parity_ports import export_ports

__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.smooth",
                                 smooth_path_points=("smoother", "smooth_path_points"))
           if not n.startswith("_")]
