"""Machine-control interface: what the application needs from a motion controller.

* :class:`MachineAPI` is the typed interface (a :class:`typing.Protocol`) every front end talks
  to.  :class:`mlaser.controller.ui_adapter.UiController` (card client, simulator) and
  :class:`mlaser.machine.demo.DemoController` (no hardware) implement **all** of it, so the UI
  calls methods directly instead of probing with ``hasattr`` (``tests/safety`` checks both).
* :class:`ControllerFacade` is the shared base class: event bus (:mod:`.events`), log and alarm
  listeners on top of it, the safety state machine (:mod:`.safety`) and the pre-flight check
  (:mod:`.preflight`).
* :class:`MachineState` is the state snapshot ``state()`` returns, :data:`OUTPUTS` the named
  process outputs.

Moved here from ``mlaser.ui.facade`` in Wave 1 so the controller package no longer imports the
UI (docs/dev/ARCHITECTURE.md).

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

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from . import events as EV
from . import preflight as PF
from .safety import SafetyFSM, SafetyState, SafetyStatus

OUTPUTS = ("follow", "gas", "shutter", "red_light", "laser", "dedust", "lube")

log = logging.getLogger("mlaser.machine")


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
    safety: str = "offline"   # machine.safety.SafetyState value
    armed: bool = False
    fault: str = ""           # latched fault text ("" = none)
    link_ok: bool = False     # last status read succeeded in time


@runtime_checkable
class MachineAPI(Protocol):
    """The complete controller interface (typed).  Every implementation provides every member;
    a feature a controller cannot do is a logged no-op, never a missing attribute."""

    events: EV.EventBus
    laser_enabled: bool              # False: dry-run mode (--laser-off), arming impossible
    active_alarms: list              # controller.alarms.Alarm (card) or text
    breakpoint: Any                  # nc.resume.Breakpoint while paused at a breakpoint
    connect_hooks: list              # fn(controller) after a successful connect
    job_start_hooks: list            # fn(document) before a job compiles
    coords: Any                      # controller.coords.CoordSystems or None
    autosave_dir: Optional[str]
    stream_process: Optional[bool]
    scaling: Any                     # controller.client.CardScaling read from the card, or None
    last_preflight: Optional[PF.PreflightReport]   # report of the last checked action

    # -- listeners (sugar over ``events``)
    def add_log_listener(self, fn: Callable[[str], None]) -> None: ...
    def add_alarm_listener(self, fn: Callable[[list], None]) -> None: ...

    # -- connection and state
    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> bool: ...
    def disconnect(self) -> None: ...
    def state(self) -> MachineState: ...
    def snapshot(self) -> MachineState: ...
    def is_simulator(self) -> bool: ...
    def card(self) -> Any: ...       # supervised card handle (None without a card)
    def endpoint(self) -> Optional[tuple[str, int]]: ...   # card (host, port), None without one

    # -- safety
    def safety(self) -> SafetyStatus: ...
    def arm(self) -> bool: ...
    def disarm(self, reason: str = "") -> None: ...
    def reset_fault(self) -> bool: ...
    def safe_halt(self, reason: str = "") -> None: ...
    def operator_action(self, what: str = "") -> None: ...
    def heartbeat_status(self) -> dict: ...
    def heartbeat_hold(self, hold: bool) -> None: ...
    def preflight(self, action: str, document: Any = None, selected_only: bool = False,
                  target: Optional[tuple[float, float]] = None) -> PF.PreflightReport: ...

    # -- motion
    def home(self, axes: str = "XY") -> None: ...
    def startup_homing(self, confirm: Optional[Callable[[str], bool]] = None,
                       normal_exit: bool = True) -> bool: ...
    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None: ...
    def jog_stop(self, axis: Optional[str] = None) -> None: ...
    def jog_keepalive(self) -> None: ...
    def jog_step(self, axis: str, direction: int, step_mm: float, fast: bool = False) -> None: ...
    def move_to(self, x: float, y: float) -> None: ...
    def move_by(self, dx: float, dy: float, speed: Optional[float] = None,
                acc: Optional[float] = None) -> None: ...
    def return_zero(self, confirm: Optional[Callable[[str], bool]] = None,
                    target: Optional[tuple[float, float]] = None) -> Optional[tuple[float, float]]: ...
    def set_origin(self) -> None: ...
    def go_origin_point(self) -> None: ...

    # -- IO and laser
    def set_output(self, name: str, on: bool) -> None: ...
    def set_gas_type(self, gas_type: int) -> None: ...
    def set_laser_power(self, pct: float) -> None: ...
    def laser_press(self) -> None: ...
    def laser_release(self) -> None: ...
    def laser_off(self) -> None: ...
    def laser_test(self, duration_s: float = 0.1, power_pct: Optional[float] = None) -> None: ...

    # -- jobs
    def place(self, document: Any) -> Any: ...
    def start(self, document: Any, selected_only: bool = False) -> None: ...
    def dry_run(self, document: Any) -> None: ...
    def frame(self, document: Any) -> None: ...
    def pause(self) -> None: ...
    def resume(self, mode: str = "exact") -> None: ...
    def stop(self, wait: bool = False) -> threading.Event: ...
    def step_backward(self, length_mm: float) -> None: ...
    def step_forward(self, length_mm: float) -> None: ...
    def resume_saved(self, document: Any, autosave_dir: Optional[str] = None,
                     mode: str = "contour") -> bool: ...
    def wait_job(self, timeout: float = 60.0) -> bool: ...
    def job_active(self) -> bool: ...
    def clear_alarms(self) -> None: ...


def api_members() -> list[str]:
    """Names of every :class:`MachineAPI` member (methods and attributes)."""
    names = set(getattr(MachineAPI, "__annotations__", {}))
    names |= {n for n, v in vars(MachineAPI).items() if callable(v) and not n.startswith("_")}
    return sorted(names)


def _done_event() -> threading.Event:
    ev = threading.Event()
    ev.set()
    return ev


class ControllerFacade(ABC):
    """Shared base of the controllers (MCC + FTC + laser): events, listeners, safety FSM."""

    laser_enabled: bool = True
    autosave_dir: Optional[str] = None
    stream_process: Optional[bool] = None
    scaling: Any = None

    def __init__(self) -> None:
        self.events = EV.EventBus()
        self.active_alarms: list = []
        self.breakpoint = None
        self.connect_hooks: list = []
        self.job_start_hooks: list = []
        self.coords = None
        self.require_homed = True            # pre-flight policy (simulator: off, see connect_simulator)
        self.fsm = SafetyFSM(self._on_safety_change)

    # -- events ------------------------------------------------------------
    def add_log_listener(self, fn: Callable[[str], None]) -> None:
        self.events.subscribe(EV.Log, lambda e: fn(e.message))

    def add_alarm_listener(self, fn: Callable[[list], None]) -> None:
        """``fn(active_alarms)`` whenever the active alarm list changes."""
        self.events.subscribe(EV.AlarmRaised, lambda e: fn(list(e.active)))
        self.events.subscribe(EV.AlarmCleared, lambda e: fn(list(e.active)))

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, "%s", msg)
        self.events.publish(EV.Log(msg, level))

    def _on_safety_change(self, old: SafetyState, new: SafetyState, st: SafetyStatus,
                          reason: str) -> None:
        if old != new:
            self.events.publish(EV.StateChanged(old.value, new.value, reason, st.fault))
        if reason == "armed by operator":
            self.events.publish(EV.Armed())
        elif not st.armed and reason.startswith(("disarm", "fault", "disconnect", "connected")) \
                and old in (SafetyState.ARMED, SafetyState.RUNNING, SafetyState.PAUSED):
            self.events.publish(EV.Disarmed(reason))

    # -- safety ------------------------------------------------------------
    def safety(self) -> SafetyStatus:
        st = self.fsm.status()
        if st.simulator != self.is_simulator() or st.dry_run != (not self.laser_enabled):
            self.fsm.simulator = self.is_simulator()
            self.fsm.dry_run = not self.laser_enabled
            st = self.fsm.status()
        return st

    def is_simulator(self) -> bool:
        return False

    def card(self) -> Any:
        return None

    def endpoint(self) -> Optional[tuple[str, int]]:
        """The card's (host, port); None for a controller without a card."""
        return None

    def operator_action(self, what: str = "") -> None:
        """The operator started a new action (a wizard step that moves): clears the halt latch
        of the supervisor (docs/SAFETY.md §4; a latched FAULT still refuses motion).  Called
        only from a button press, never from a worker on its own."""

    def heartbeat_status(self) -> dict:
        """The "PC alive" output (docs/SAFETY.md §11): output number (0 = off), frequency,
        toggles sent so far, held (test) and active."""
        return {"output": 0, "hz": 0.0, "toggles": 0, "hold": False, "active": False}

    def heartbeat_hold(self, hold: bool) -> None:
        """Commissioning test of the watchdog relay: stop toggling (output off) while held.
        Stopping the heartbeat only ever drops the relay, so it is always allowed."""

    def arm(self) -> bool:
        self.fsm.dry_run = not self.laser_enabled
        why = self.fsm.arm()
        if why:
            self._log(why, logging.WARNING)
            return False
        self._log("ARMED: laser emission enabled", logging.WARNING)
        return True

    def disarm(self, reason: str = "") -> None:
        if self.job_active() and self.fsm.armed:
            self.stop()
        if self.fsm.disarm(reason or "disarmed by operator"):
            self._log("disarmed: laser emission disabled")

    def reset_fault(self) -> bool:
        why = self.fsm.reset()
        if why:
            self._log(why, logging.WARNING)
            return False
        self._log("fault reset: machine disarmed")
        return True

    def raise_fault(self, reason: str) -> None:
        """Latch a FAULT and bring the machine to a safe state."""
        first = self.fsm.raise_fault(reason)
        self._log(f"FAULT: {reason}" if first else f"(fault already latched) {reason}", logging.ERROR)
        self.safe_halt(reason)

    def safe_halt(self, reason: str = "") -> None:
        """Default: stop (subclasses with hardware send the halt sequence)."""
        self.stop()

    def job_active(self) -> bool:
        return self.fsm.state in (SafetyState.RUNNING, SafetyState.PAUSED)

    def snapshot(self) -> MachineState:
        import copy
        return copy.deepcopy(self.state())

    def _fill_safety(self, s: MachineState) -> None:
        st = self.safety()
        s.safety, s.armed, s.fault = st.state.value, st.armed, st.fault

    # -- pre-flight --------------------------------------------------------
    def travel_limits(self) -> Optional[PF.TravelLimits]:
        return PF.travel_from_machine(getattr(self, "machine", None))

    def homed_xy(self) -> Optional[bool]:
        return bool(self.state().homed)

    def layer_table(self):
        return getattr(self, "layers", None)

    def preflight(self, action: str, document: Any = None, selected_only: bool = False,
                  target: Optional[tuple[float, float]] = None, job: Any = None,
                  extent: Any = None, emits: Optional[bool] = None) -> PF.PreflightReport:
        st = self.safety()
        if emits is None:
            emits = action in ("start", "resume", "laser_test") and self.laser_enabled
        s = self.state()
        return PF.check(action, document=document, job=job, extent=extent, target=target,
                        travel=self.travel_limits(), homed=self.homed_xy(),
                        require_homed=self.require_homed, armed=st.armed, emits=bool(emits),
                        layer_table=self.layer_table(), alarms=self.active_alarms, fault=st.fault,
                        selected_only=selected_only,
                        head=(s.x, s.y) if action in ("start", "dry_run", "resume") else None)

    def _refuse(self, rep: PF.PreflightReport) -> bool:
        """Log and publish a failed report; True when the action must not run."""
        self.last_preflight = rep
        if rep.ok:
            if rep.warnings:
                self._log(rep.summary())
            return False
        self._log(rep.summary(), logging.WARNING)
        return True

    last_preflight: Optional[PF.PreflightReport] = None

    # -- connection --------------------------------------------------------
    @abstractmethod
    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> bool: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def state(self) -> MachineState: ...

    # -- motion ------------------------------------------------------------
    @abstractmethod
    def home(self, axes: str = "XY") -> None:
        """Go origin. ``axes`` any of 'X','Y','Z','XY','XYZ' (mf15/16/17/18/222)."""
    def startup_homing(self, confirm: Optional[Callable[[str], bool]] = None,
                       normal_exit: bool = True) -> bool:
        """Start-up go-origin prompt (mp119, mf156); returns True when homing started."""
        ask = confirm or (lambda _k: True)
        if normal_exit and not self.state().homed and ask("mp119") and ask("mf156"):
            self.home("XY")
            return True
        return False
    @abstractmethod
    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None: ...
    @abstractmethod
    def jog_stop(self, axis: Optional[str] = None) -> None: ...
    def jog_keepalive(self) -> None:
        """A continuous-jog button is still held (hold-to-jog dead-man)."""
    @abstractmethod
    def jog_step(self, axis: str, direction: int, step_mm: float, fast: bool = False) -> None: ...
    @abstractmethod
    def move_to(self, x: float, y: float) -> None:
        """mp111 'Position to'."""
    def move_by(self, dx: float, dy: float, speed: Optional[float] = None,
                acc: Optional[float] = None) -> None:
        s = self.state()
        self.move_to(s.x + dx, s.y + dy)
    def return_zero(self, confirm: Optional[Callable[[str], bool]] = None,
                    target: Optional[tuple[float, float]] = None) -> Optional[tuple[float, float]]:
        """mp29 'Home': move to ``target`` or (0, 0) (mp79 prompt for the zero)."""
        t = (0.0, 0.0) if target is None else (float(target[0]), float(target[1]))
        if t == (0.0, 0.0) and confirm is not None and not confirm("mp79"):
            return None
        self.move_to(*t)
        return t
    def set_origin(self) -> None:
        """mf514 'Set Origin' / mp163 SetManualOrigin."""
    def go_origin_point(self) -> None:
        """mp164 ReturnManualOrigin."""
        self.move_to(0.0, 0.0)

    # -- IO ----------------------------------------------------------------
    @abstractmethod
    def set_output(self, name: str, on: bool) -> None: ...
    def set_gas_type(self, gas_type: int) -> None:
        self.state().gas_type = gas_type
    def set_laser_power(self, pct: float) -> None:
        self.state().laser_power_pct = pct
    def laser_press(self) -> None:
        self.set_output("laser", True)
    def laser_release(self) -> None:
        self.set_output("laser", False)
    def laser_off(self) -> None:
        self.state().outputs["laser"] = False
    def laser_test(self, duration_s: float = 0.1, power_pct: Optional[float] = None) -> None:
        self._log("laser test: not available on this controller")

    # -- jobs --------------------------------------------------------------
    def place(self, document: Any) -> Any:
        return document
    @abstractmethod
    def start(self, document, selected_only: bool = False) -> None: ...
    @abstractmethod
    def pause(self) -> None: ...
    @abstractmethod
    def resume(self, mode: str = "exact") -> None: ...
    @abstractmethod
    def stop(self, wait: bool = False) -> threading.Event: ...
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
    def resume_saved(self, document: Any, autosave_dir: Optional[str] = None,
                     mode: str = "contour") -> bool:
        self._log("mp64: breakpoint resume needs the card controller")
        return False
    def wait_job(self, timeout: float = 60.0) -> bool:
        return True
    @abstractmethod
    def clear_alarms(self) -> None: ...


__all__ = ["OUTPUTS", "MachineState", "MachineAPI", "ControllerFacade", "api_members"]
