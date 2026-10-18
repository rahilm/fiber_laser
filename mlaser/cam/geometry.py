"""Moved: the contour helpers are :mod:`mlaser.geom.contour`, the exact spline curves
:mod:`mlaser.parity_ports.cadmodule.spline` (Wave 1, docs/dev/ARCHITECTURE.md).

``SplineCurve`` and ``CurveRef`` are re-exported here (the profile's ``spline`` slot); the
contour helpers are forwarded with a DeprecationWarning.
"""
from __future__ import annotations

from .._compat import warn_moved
from ..geom import contour as _contour
from ..parity_ports.cadmodule.spline import CurveRef, SplineCurve  # noqa: F401

__all__ = ["SplineCurve", "CurveRef"] + list(_contour.__all__)


def __getattr__(name: str):
    if hasattr(_contour, name):
        warn_moved(f"{__name__}.{name}", f"mlaser.geom.contour.{name}")
        return getattr(_contour, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
