"""Line reader/writer of every Mlaser "scFlie" text file (CSerialEng).

The port lives in :mod:`mlaser.parity_ports.cadmodule.scio`; this module re-exports it.
"""
from ..parity_ports import export_ports
from ..parity_ports.cadmodule.scio import *  # noqa: F401,F403 (public names, for type checkers)

# every name of the port (private ones too); the entry points become profile dispatchers
__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.scio")
           if not n.startswith("_")]
