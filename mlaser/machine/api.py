"""Machine-control interface: what the application needs from a motion controller.

:class:`ControllerFacade` is the abstract controller (MCC + FTC + laser) every front end talks
to, :class:`MachineState` the state snapshot it returns and :data:`OUTPUTS` the named process
outputs.  Implementations: :class:`mlaser.controller.ui_adapter.UiController` (card client,
simulator) and :class:`mlaser.ui.facade.DemoController` (no hardware).  Moved here from
``mlaser.ui.facade`` in Wave 1 so the controller package no longer imports the UI
(docs/dev/ARCHITECTURE.md); the typed MachineAPI and event bus of docs/ROADMAP.md (Wave 2)
grow from this module.

The UI never talks to sockets directly. Every button on the machine control panel
(original lang keys ``mp*``, button bitmaps 508-543 in MainApp.exe RT_BITMAP) calls one
method here.

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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

OUTPUTS = ("follow", "gas", "shutter", "red_light", "laser", "dedust", "lube")


@dataclass
class MachineState:
    connected: bool = False
    homed: bool = False
    mode: str = "offline"     # offline|ready|origin|jog|process|pause|stop|frame|dry_run
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 0.0
    speed: float = 0.0
    ftc: str = "offline"      # offline|ready|follow|drill|jog_up|jog_down|estop
    laser: str = "offline"    # offline|ready
    outputs: dict = field(default_factory=lambda: {k: False for k in OUTPUTS})
    gas_type: int = 0         # 0 air, 1 O2, 2 N2, 3..5 high-pressure variants (mp43..mp48)
    laser_power_pct: float = 10.0
    alarms: list = field(default_factory=list)   # list[(lang_key_or_text, timestamp)]
    progress: float = 0.0     # 0..1 for a running job
    job_index: int = -1       # current contour index of a running job


class ControllerFacade(ABC):
    """What the UI needs from a motion controller (MCC + FTC + laser)."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[str], None]] = []

    # -- events ------------------------------------------------------------
    def add_log_listener(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def _log(self, msg: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(msg)
            except Exception:  # pragma: no cover - UI callback errors must not kill control
                pass

    # -- connection --------------------------------------------------------
    @abstractmethod
    def connect(self, host: str = "10.1.1.168", port: int = 502) -> bool: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def state(self) -> MachineState: ...

    # -- motion ------------------------------------------------------------
    @abstractmethod
    def home(self, axes: str = "XY") -> None:
        """Go origin. ``axes`` any of 'X','Y','Z','XY','XYZ' (mf15/16/17/18/222)."""
    @abstractmethod
    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None: ...
    @abstractmethod
    def jog_stop(self, axis: Optional[str] = None) -> None: ...
    @abstractmethod
    def jog_step(self, axis: str, direction: int, step_mm: float) -> None: ...
    @abstractmethod
    def move_to(self, x: float, y: float) -> None:
        """mp111 'Position to'."""

    # -- IO ----------------------------------------------------------------
    @abstractmethod
    def set_output(self, name: str, on: bool) -> None: ...
    def set_gas_type(self, gas_type: int) -> None:
        self.state().gas_type = gas_type
    def set_laser_power(self, pct: float) -> None:
        self.state().laser_power_pct = pct

    # -- jobs --------------------------------------------------------------
    @abstractmethod
    def start(self, document, selected_only: bool = False) -> None: ...
    @abstractmethod
    def pause(self) -> None: ...
    @abstractmethod
    def resume(self) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @abstractmethod
    def frame(self, document) -> None:
        """mp58 'Frame': trace the job bounding box with the head (laser off)."""
    @abstractmethod
    def dry_run(self, document) -> None:
        """mp57 'Dry Cut': run the toolpath without laser."""
    def step_backward(self, length_mm: float) -> None:
        """mp61 Backward along the path while paused (mp65 BF Len)."""
    def step_forward(self, length_mm: float) -> None:
        """mp62 Forward along the path while paused."""
    @abstractmethod
    def clear_alarms(self) -> None: ...
    def set_origin(self) -> None:
        """mf514 'Set Origin' / mp163 SetManualOrigin."""
    def go_origin_point(self) -> None:
        """mp164 ReturnManualOrigin."""
