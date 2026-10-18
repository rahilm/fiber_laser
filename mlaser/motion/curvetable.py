"""Curve pieces of CContoutSmooth (sample table, curvature nodes, arc length -> XY).

The port lives in :mod:`mlaser.parity_ports.cadmodule.curvetable`; this module re-exports it.
"""
from ..parity_ports import export_ports
from ..parity_ports.cadmodule.curvetable import *  # noqa: F401,F403 (public names, for type checkers)

# every name of the port (private ones too); the entry points become profile dispatchers
__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.curvetable")
           if not n.startswith("_")]
