"""msvcr100 (Wine) acos / atan / atan2 / hypot, bit for bit.

The port lives in :mod:`mlaser.parity_ports.crtmath`; this module re-exports it.
"""
from ..parity_ports import export_ports

__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.crtmath")
           if not n.startswith("_")]
