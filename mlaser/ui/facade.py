"""Abstract machine-control interface used by the UI.

The UI never talks to sockets directly. Every button on the machine control panel
(original lang keys ``mp*``, button bitmaps 508-543 in MainApp.exe RT_BITMAP) calls one
method here. :class:`DemoController` (now :mod:`mlaser.machine.demo`, re-exported here) is a
dependency-free simulation so the UI runs without hardware; the real implementation is
:class:`mlaser.controller.ui_adapter.UiController` (see :func:`make_controller`).

Original behaviour referenced (observed in lang.txt, MainApp strings):
* status texts ``mp7`` "MCC <Ready>", ``mp8`` "<Origin>", ``mp9`` "<Jog>", ``mp10`` "<Stop>",
  ``mp11`` "<Process>", ``mp22`` "MCC <Offline>"; FTC ``mp12..17``/``mp23``; laser ``mp24``/``mp25``.
* jog: ``mp31`` Step / ``mp32`` Fast, step lengths ``mp90..mp99`` (0.05 .. 200 mm).
* outputs: ``mp41`` Follow, ``mp42`` Gas (``mp43..48`` Air/O2/N2/High*), ``mp49`` Shutter,
  ``mp50`` RedLight, ``mp51`` Laser (+ ``mp53`` LaserPwr %).
* process: ``mp54`` Start, ``mp55`` Pause, ``mp56`` Stop, ``mp57`` Dry Cut, ``mp58`` Frame,
  ``mp59`` Sim, ``mp60`` BreakPt, ``mp61`` Backward, ``mp62`` Forward, ``mp64`` BreakCon.
* homing: ``mf15`` Go Origin, ``mf16`` Only X, ``mf17`` Only Y, ``mf18``/``mp30`` Z follower,
  ``mf222`` XYZ, ``mf482`` ZF first then XY.
"""
from __future__ import annotations

from .._compat import warn_moved
from ..machine import api as _api
from ..machine.demo import DemoController  # noqa: F401 - moved, re-exported

_MOVED = ("OUTPUTS", "MachineState", "ControllerFacade")


def __getattr__(name: str):
    # OUTPUTS, MachineState and ControllerFacade moved to mlaser.machine.api (Wave 1)
    if name in _MOVED:
        warn_moved(f"{__name__}.{name}", f"mlaser.machine.api.{name}")
        return getattr(_api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def make_controller(prefer_real: bool = False) -> _api.ControllerFacade:
    """The real controller adapter when ``prefer_real``, else the demo.

    With ``prefer_real`` a failure to build the real controller **raises** (it used to fall back
    to the demo silently, so ``--real`` could end up on a fake machine without saying so)."""
    if prefer_real:
        from mlaser.controller import ui_adapter
        return ui_adapter.UiController()
    return DemoController()
