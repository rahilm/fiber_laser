"""Mlaser ``.chf`` drawing/job files (docs/formats.md).

The codec is a byte-exact port in the parity fence, :mod:`mlaser.parity_ports.cadmodule.chf`;
this module is its domain-facing name.  ``loads_chf`` / ``dumps_chf`` / ``load_chf`` /
``save_chf`` dispatch through the active ParityProfile (``chf`` slot); the record types
(``ChfContour``, ...) and helpers are the port's.
"""
from ..parity_ports import export_ports

__all__ = [n for n in export_ports(globals(), "mlaser.parity_ports.cadmodule.chf",
                                 loads_chf=("chf", "loads_chf"), dumps_chf=("chf", "dumps_chf"), load_chf=("chf", "load_chf"), save_chf=("chf", "save_chf"))
           if not n.startswith("_")]
