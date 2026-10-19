"""The parity fence: bit-exact ports of the original program, selected through ``ParityProfile``.

Everything here reproduces a function of Mlaser v0.0.1.51_SP1 operation by operation, checked
against the original under Wine (docs/parity.md) and frozen by the goldens (tests/golden/).
Rules (docs/dev/ARCHITECTURE.md, enforced by import-linter):

* a port changes only together with a Wine parity re-run;
* new behaviour is a new :class:`~.profile.ParityProfile`, never an edit of a port;
* ``parity_ports`` imports only :mod:`mlaser.geom`, :mod:`mlaser.protocol` and
  :mod:`mlaser.model` (plus the standard library and numpy);
* entry points carry ``@port(...)`` (:mod:`._registry`), every module a ``__port__`` record.

Layout, by original binary:

=====================  ==================================================================
``cadmodule``          CADModule.dll: velocity planner, motion profiles, segInterp /
                       arcInterp, CContoutSmooth + curve table, exact spline glyphs, sort
                       dispatcher (import sort, local optimal, ring sort, start point),
                       the .chf codec and its CSerialEng text format
``mainapp``            MainApp.exe: per-period sample loop and pulse quantisation, the GP.*
                       layer parameter view
``ncmodule``           NCModule.dll: ManuItem -> FIFO item encoder
``circlefit``          CircleFitDLL: CCircleFit::mainFit (polyline -> lines and arcs)
``dxfparse``           DxfParseDllvc100 + CADModule import: DXF entities -> glyphs -> contours
``crtmath``            msvcr100 (Wine) acos / atan / atan2 / hypot, bit for bit
=====================  ==================================================================

The MCC100 frame codec (``mlaser.protocol.frames``) and the splineAnalyer sample table
(``mlaser.geom.curves.flatten_bspline_params``) are bit-exact too but live in the foundation
packages, since the ports build on them; they carry ``__port__`` records as well.
"""
from ._registry import PortRecord, port, registered  # noqa: F401
from .profile import (DEFAULT, INTERFACES, ORIGINAL, ORIGINAL_PYTHON, PROFILES, Implementation,  # noqa: F401
                      ParityProfile, active_profile, dispatch, get_profile, register_profile,
                      export_ports, set_profile, use_profile)


def kernel_status():  # -> profiles.native.KernelStatus
    """Which emitter loop the default profile runs: the C kernel (``mlaser._native``) or the
    Python port, and why (``MLASER_NATIVE``, the self-check); docs/dev/NATIVE_KERNEL.md."""
    from .profiles.native import kernel_status as _ks
    return _ks()
