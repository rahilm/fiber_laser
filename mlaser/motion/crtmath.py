"""msvcr100 (Wine) acos / atan / atan2 / hypot, bit for bit.

The port lives in :mod:`mlaser.parity_ports.crtmath`; this module re-exports it.
"""
from ..parity_ports import export_ports
from ..parity_ports.crtmath import *  # noqa: F401,F403 (public names, for type checkers)

# every name of the port (private ones too); the entry points become profile dispatchers
__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.crtmath")
           if not n.startswith("_")]
