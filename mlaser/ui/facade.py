"""Abstract machine-control interface used by the UI.

The UI never talks to sockets directly. Every button on the machine control panel
(original lang keys ``mp*``, button bitmaps 508-543 in MainApp.exe RT_BITMAP) calls one
method here. :class:`DemoController` is a dependency-free simulation so the UI runs
without hardware; ``mlaser.controller`` is expected to provide a real implementation
(see :func:`make_controller`).

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

import math
import time
from typing import Callable, Optional

from .._compat import warn_moved
from ..machine import api as _api

_MOVED = ("OUTPUTS", "MachineState", "ControllerFacade")


def __getattr__(name: str):
    # OUTPUTS, MachineState and ControllerFacade moved to mlaser.machine.api (Wave 1)
    if name in _MOVED:
        warn_moved(f"{__name__}.{name}", f"mlaser.machine.api.{name}")
        return getattr(_api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class DemoController(_api.ControllerFacade):
    """No-hardware simulation: integrates motion in :meth:`state` from wall-clock time."""

    TABLE = (0.0, 0.0, 3000.0, 1500.0)
    JOG_SLOW, JOG_FAST, RAPID = 20.0, 200.0, 400.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._clock = clock
        self._s = _api.MachineState()
        self._t = clock()
        self._jog: dict[str, tuple[int, float]] = {}
        self._path: list[tuple[float, float]] = []
        self._path_len = 0.0
        self._path_pos = 0.0
        self._path_speed = 0.0
        self._resume_mode = "process"

    # -- internal ----------------------------------------------------------
    def _advance(self) -> None:
        now = self._clock()
        dt, self._t = max(0.0, now - self._t), now
        s = self._s
        for axis, (direction, v) in self._jog.items():
            setattr(s, axis.lower(), getattr(s, axis.lower()) + direction * v * dt)
        if self._path and s.mode in ("process", "frame", "dry_run", "origin"):
            self._path_pos = min(self._path_len, self._path_pos + self._path_speed * dt)
            s.x, s.y = self._point_at(self._path_pos)
            s.progress = self._path_pos / self._path_len if self._path_len else 1.0
            s.speed = self._path_speed
            if self._path_pos >= self._path_len:
                if s.mode == "origin":
                    s.homed = True
                self._log(f"done: {s.mode}")
                self._path = []
                s.mode, s.speed = "ready", 0.0
                s.outputs["laser"] = False
        elif not self._jog and s.mode not in ("pause",):
            s.speed = 0.0
        x0, y0, x1, y1 = self.TABLE
        s.x, s.y = min(max(s.x, x0), x1), min(max(s.y, y0), y1)

    def _point_at(self, d: float) -> tuple[float, float]:
        acc = 0.0
        for a, b in zip(self._path, self._path[1:]):
            L = math.dist(a, b)
            if acc + L >= d and L > 0:
                t = (d - acc) / L
                return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            acc += L
        return self._path[-1]

    def _run(self, pts: list[tuple[float, float]], speed: float, mode: str) -> None:
        s = self._s
        self._path = [(s.x, s.y)] + list(pts)
        self._path_len = sum(math.dist(a, b) for a, b in zip(self._path, self._path[1:]))
        self._path_pos = 0.0
        self._path_speed = speed
        s.mode, s.progress = mode, 0.0
        self._log(f"{mode}: {len(pts)} points, {self._path_len:.1f} mm")

    def _require(self) -> bool:
        if not self._s.connected:
            self._log("gp0: hardware is not connected")
            return False
        return True

    @staticmethod
    def _doc_points(document, selected_only=False) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for p in getattr(document, "parts", []):
            if selected_only and not p.selected:
                continue
            for c in p.contours:
                pts.extend(c.points(0.5))
        return pts

    # -- API ---------------------------------------------------------------
    def connect(self, host: str = "10.1.1.168", port: int = 502) -> bool:
        s = self._s
        s.connected, s.mode, s.ftc, s.laser = True, "ready", "ready", "ready"
        self._log(f"mf0: demo controller connected ({host}:{port})")
        return True

    def disconnect(self) -> None:
        self._s = _api.MachineState(x=self._s.x, y=self._s.y)
        self._log("demo controller disconnected")

    def state(self) -> _api.MachineState:
        self._advance()
        return self._s

    def home(self, axes: str = "XY") -> None:
        if not self._require():
            return
        self._advance()
        self._run([(0.0 if "X" in axes else self._s.x, 0.0 if "Y" in axes else self._s.y)], self.RAPID, "origin")
        if "Z" in axes:
            self._s.z = 0.0

    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None:
        if not self._require():
            return
        self._advance()
        self._jog[axis.upper()] = (1 if direction > 0 else -1, self.JOG_FAST if fast else self.JOG_SLOW)
        self._s.mode = "jog"

    def jog_stop(self, axis: Optional[str] = None) -> None:
        self._advance()
        if axis is None:
            self._jog.clear()
        else:
            self._jog.pop(axis.upper(), None)
        if not self._jog and self._s.mode == "jog":
            self._s.mode = "ready"

    def jog_step(self, axis: str, direction: int, step_mm: float) -> None:
        if not self._require():
            return
        self._advance()
        a = axis.lower()
        setattr(self._s, a, getattr(self._s, a) + (1 if direction > 0 else -1) * step_mm)

    def move_to(self, x: float, y: float) -> None:
        if self._require():
            self._advance()
            self._run([(x, y)], self.RAPID, "dry_run")

    def set_output(self, name: str, on: bool) -> None:
        if name not in self._s.outputs:
            raise KeyError(name)
        self._s.outputs[name] = bool(on)
        self._log(f"output {name} = {'on' if on else 'off'}")

    def start(self, document, selected_only: bool = False) -> None:
        if not self._require():
            return
        pts = self._doc_points(document, selected_only)
        if not pts:
            self._log("mp74: graphic data is empty, cannot start")
            return
        self._advance()
        speed = 100.0
        layers = getattr(document, "layers", {}) or {}
        if layers:
            speed = max(1.0, min(lp.speed for lp in layers.values()))
        self._s.outputs["laser"] = True
        self._run(pts, speed, "process")

    def pause(self) -> None:
        self._advance()
        if self._s.mode in ("process", "dry_run", "frame"):
            self._resume_mode = self._s.mode
            self._s.mode = "pause"
            self._s.outputs["laser"] = False
            self._log("paused")

    def resume(self) -> None:
        self._advance()
        if self._s.mode == "pause":
            self._s.mode = self._resume_mode
            self._log("resumed")

    def stop(self) -> None:
        self._advance()
        self._path, self._jog = [], {}
        self._s.mode, self._s.speed = ("ready" if self._s.connected else "offline"), 0.0
        self._s.outputs["laser"] = False
        self._log("stopped")

    def frame(self, document) -> None:
        if not self._require():
            return
        bb = document.bbox() if hasattr(document, "bbox") else (0, 0, 0, 0)
        if bb == (0, 0, 0, 0):
            self._log("mp78: graphic data is empty, cannot go frame")
            return
        x0, y0, x1, y1 = bb
        self._advance()
        self._run([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], self.RAPID / 2, "frame")

    def dry_run(self, document) -> None:
        if not self._require():
            return
        pts = self._doc_points(document)
        if not pts:
            self._log("mp74: graphic data is empty")
            return
        self._advance()
        self._run(pts, self.RAPID / 2, "dry_run")

    def step_backward(self, length_mm: float) -> None:
        self._advance()
        if self._path and self._s.mode == "pause":
            self._path_pos = max(0.0, self._path_pos - length_mm)
            self._s.x, self._s.y = self._point_at(self._path_pos)

    def step_forward(self, length_mm: float) -> None:
        self._advance()
        if self._path and self._s.mode == "pause":
            self._path_pos = min(self._path_len, self._path_pos + length_mm)
            self._s.x, self._s.y = self._point_at(self._path_pos)

    def clear_alarms(self) -> None:
        self._s.alarms.clear()
        self._log("mf21: alarms cleared")


def make_controller(prefer_real: bool = False) -> _api.ControllerFacade:
    """Return the real controller adapter if available and requested, else the demo.

    ``mlaser.controller`` is being written separately; it is expected to expose a
    ``ControllerFacade`` implementation named ``UiController`` (to be bound later).
    """
    if prefer_real:
        try:  # pragma: no cover - depends on the controller package
            from mlaser.controller import ui_adapter  # type: ignore
            return ui_adapter.UiController()
        except Exception:
            pass
    return DemoController()
