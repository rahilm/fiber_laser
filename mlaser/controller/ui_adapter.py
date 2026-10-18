"""Binds the :class:`mlaser.machine.api.MachineAPI` to the real card client.

``UiController`` = :class:`~mlaser.controller.supervisor.SafetySupervisor` (the only holder of
the card transport: the guarded :class:`~mlaser.controller.client.MotionController` and the halt
channel, docs/SAFETY.md) + :func:`mlaser.nc.compile_job` (document -> FIFO job) +
:mod:`mlaser.controller.stream` (in a worker thread, or in a separate process for a real card)
+ :mod:`mlaser.nc.resume` (breakpoints), with a status-polling thread that keeps a
:class:`MachineState` current (RO block 1000.., axis RO block 2000.., FUN_100521b0 /
FUN_100523c0), decodes the alarm words (RO[6]/RO[7], :mod:`mlaser.controller.alarms`) and runs
the interlock monitor (:mod:`mlaser.controller.interlocks`).

The class is assembled from four parts (``controller/parts``): connection and poller, motion,
IO, job runner.  Notifications go out on the event bus ``events`` (:mod:`mlaser.machine.events`);
a GUI installs a dispatcher on it to receive them on its own thread (the Qt signal bridge that
used to live here moved to ``mlaser.ui.qtbridge``).

Behaviour taken from the original (docs/protocol.md §10-§13):

* connect: read RO[0..1], write 101 := [9999, 5, 0, 0] (VM start, FUN_100571f0), optionally the
  full start-up with parameter download (``card_params``, docs/protocol.md §3a), then read the
  card's parameter blocks
  ("hardware restore", FUN_10039f20) for the interpolation period, the axis scaling and the
  card soft limits;
* Go Origin: mailbox ``[2, axis_mask, 0]`` (FUN_10032da0), mask from the homing mode;
* Clear Alarm: ``[9999, 5, 0, 0]`` (FUN_100315f0);
* Pause = program stop with a breakpoint (MainApp FUN_005b9a60 "Sys Pause" -> "send stop manu
  cmd"), Start/Resume continues from the breakpoint (``pause_mode="breakpoint"``, default).
  ``pause_mode="fifo"`` keeps the phase-1 behaviour (103 := 3 / 103 := 2, exact continuation);
* an alarm during a job pauses it (FUN_00598280 -> FUN_005b9a60); the rebuild latches a FAULT
  with a breakpoint (reset, then Resume);
* Backward/Forward while paused move along the path with the laser off.

Safety additions (docs/SAFETY.md): connect disarmed, arming for every emission, pre-flight
before Start / Frame / Resume / moves, ``safe_halt`` on every exit path, a latched FAULT on link
loss, stream faults, card alarms during a job, interlocks and swallowed safety failures.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from ..machine.api import ControllerFacade, MachineState
from ..machine.safety import SafetyState
from .client import CardScaling, ControllerError, MotionController, TransportConfig
from .interlocks import InterlockConfig, InterlockMonitor
from .parts.connection import AXES, ConnectionPart
from .parts.io import IOPart
from .parts.jobs import JobRunnerPart
from .parts.motion import CANCEL_CMD, HOME_MODES, MotionPart  # noqa: F401 - re-exported
from .supervisor import CommandRefused, SafetySupervisor
from .stream import DEFAULT_MAX_BUFFER_MS

log = logging.getLogger(__name__)

# Digital output numbers for the UI's named outputs.  The real numbers are machine
# configuration (HardPara.xml DO table, pd255..282); these defaults are placeholders (INFERRED).
DEFAULT_OUTPUT_MAP = {"gas": 1, "shutter": 2, "red_light": 3, "dedust": 4, "lube": 5}


class UiController(ConnectionPart, MotionPart, IOPart, JobRunnerPart, ControllerFacade):
    """Real-card implementation of :class:`~mlaser.machine.api.MachineAPI` (see module doc)."""

    def __init__(self, host: str = "10.1.1.168", port: int = 502, ini_path: Optional[str] = None,
                 machine=None, cam_params=None, layers=None, poll_s: float = 0.05,
                 laser_enabled: bool = True, output_map: Optional[dict] = None,
                 pause_mode: str = "breakpoint", stream_process: Optional[bool] = None,
                 adopt_card_scaling: bool = True, autosave_dir: Optional[str] = None,
                 pause_on_alarm: bool = True, dat_dir: Optional[str] = None,
                 jump_add_time: Optional[str] = None, *,
                 max_buffer_ms: float = DEFAULT_MAX_BUFFER_MS, link_deadline_ms: float = 150.0,
                 idle_link_deadline_ms: float = 1500.0, halt_timeout_ms: int = 40,
                 heartbeat_output: int = 0, heartbeat_hz: float = 5.0,
                 interlocks: Optional[InterlockConfig] = None, jog_deadman_s: Optional[float] = 0.5,
                 require_homed: bool = True):
        super().__init__()
        from ..nc import Machine
        self.cfg = TransportConfig.from_ini(ini_path) if ini_path else TransportConfig(host=host, port=port)
        if machine is None and dat_dir:
            machine = Machine.from_dat_dir(dat_dir, ini_path, jump_add_time)
        self.machine = machine or Machine()
        self.document = None                 # last document started (for resume_items)
        self.cam_params = cam_params
        self.layers = layers                 # MachineParams / dict of LayerParams (optional)
        self.poll_s = poll_s
        self.laser_enabled = laser_enabled   # False: dry-run mode, arming impossible
        if output_map is None:
            pc = getattr(self.machine, "process", None)
            try:
                output_map = pc.output_map() if pc is not None else None
            except Exception:  # noqa: BLE001
                output_map = None
        self.output_map = dict(DEFAULT_OUTPUT_MAP if output_map is None else output_map)
        self.pause_mode = pause_mode
        self.stream_process = stream_process  # None = auto: process for a real card
        self.adopt_card_scaling = adopt_card_scaling
        self.autosave_dir = autosave_dir
        self.pause_on_alarm = pause_on_alarm  # kept for compatibility: an alarm during a job always faults
        # startup.CardParameters: when set, connect() runs the original's full start-up
        # (parameter download + fast-status map, MotionController.startup); None = connect only
        self.card_params = None
        # --- safety configuration (docs/SAFETY.md)
        self.max_buffer_ms = float(max_buffer_ms)          # motion in the card FIFO, prefill included
        self.heartbeat_output = int(heartbeat_output)      # "PC alive" DO (0 = off)
        self.heartbeat_hz = float(heartbeat_hz)
        self.jog_deadman_s = jog_deadman_s                 # hold-to-jog keep-alive timeout
        self.require_homed = require_homed
        self.child_parent_timeout_s = 0.3                  # streaming child: parent heartbeat
        self.child_stall_s = 0.5                           # parent: child silent -> fault
        self.interlocks = InterlockMonitor(interlocks)
        self.sup = SafetySupervisor(self.fsm, halt_timeout_ms=halt_timeout_ms,
                                    link_deadline_ms=link_deadline_ms,
                                    idle_link_deadline_ms=idle_link_deadline_ms,
                                    heartbeat_output=heartbeat_output, heartbeat_hz=heartbeat_hz,
                                    on_link_lost=self._on_link_lost,
                                    on_link_restored=self._on_link_restored,
                                    halt_params=self._halt_params)
        self.sup.guarded_fn = self._guarded_outputs_mask
        self.sim = None                      # in-process CardSimulator when connect_simulator() is used
        self.version = 0
        self.scaling: Optional[CardScaling] = None
        self.job = None                      # last compiled mlaser.nc.Job (whole document)
        self.stream_stats: dict = {}
        self._s = MachineState()
        self._slock = threading.Lock()
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._paused = False
        self._pausing = False
        self._stop_reason = ""
        self._job_mode = ""
        self._job_emits = False
        self._run_job_obj = None             # job currently streaming (may be a resume job)
        self._run_offset = 0                 # wire index in self._bp_job = streaming index + offset
        self._bp_job = None                  # job the breakpoint indices refer to
        self._exec_index = -1
        self._streamer = None
        self._homing = False
        self._home_mask = 0
        self._homed_mask = 0                 # status bit 15 of the axes (FUN_10039590)
        self._program_ran = False            # VM +0x38: set when a program starts (FUN_10055840)
        self._gate_open = False              # cfg +0x4a51/+0x4a52: laser gate (shutter) open
        self._laser_on = False               # panel laser button held (CManuPanel +0x1ed5)
        self._timed_shot = False             # DAT_100b4f50: timed point shot running
        self._power_dirty = False
        self._jogging: Optional[str] = None  # continuous jog in progress (dead-man)
        self._jog_alive = 0.0
        self._zf_words: Optional[list] = None
        self._zf_read_t = 0.0
        self.laser_type: Optional[int] = None  # SP.m_iEnableLaserType override (MachineOps)
        self._errors = 0
        self._store = None

    # ------------------------------------------------------------------ helpers
    @property
    def _mc(self) -> Optional[MotionController]:
        """The supervisor's guarded main channel (internal use)."""
        return self.sup.mc

    @property
    def mc(self):
        """Deprecated alias of :meth:`card` for scripts and tests: the supervised card handle
        (no socket access); None while disconnected."""
        return self.card()

    @property
    def signals(self):
        """Removed: subscribe to ``events`` (mlaser.machine.events) instead."""
        return None

    @property
    def active_alarms(self) -> list:
        return self._active_alarms

    @active_alarms.setter
    def active_alarms(self, v: list) -> None:
        self._active_alarms = list(v)

    def _ok(self) -> bool:
        if self._mc is None or not self._s.connected:
            self._log("gp0: hardware is not connected")
            return False
        return True

    def _safe(self, what: str, fn, *a, **kw):
        """Run a card call; a failure is logged (a refused command says why)."""
        try:
            return fn(*a, **kw)
        except NotImplementedError as e:
            self._log(f"{what}: not available - {e}")
        except CommandRefused as e:
            self._log(f"{what} refused: {e.reason}", logging.WARNING)
        except (ControllerError, OSError, ValueError) as e:
            self._log(f"{what} failed: {e}", logging.WARNING)
        return None

    def _try(self, what: str, fn, *a, **kw) -> bool:
        """Like :meth:`_safe` but returns whether the call succeeded."""
        try:
            fn(*a, **kw)
            return True
        except CommandRefused as e:
            self._log(f"{what} refused: {e.reason}", logging.WARNING)
        except (ControllerError, OSError, ValueError, AttributeError) as e:
            self._log(f"{what} failed: {e}", logging.WARNING)
        return False

    @property
    def ppm(self) -> tuple[float, float]:
        """Step-item pulses per mm (Machine), X and Y."""
        return (self.machine.pulses_per_mm_x, self.machine.pulses_per_mm_y)

    @property
    def position_ppm(self) -> float:
        """Divisor of the card's position/velocity counts (config +0xbc28)."""
        return self._mc.pulses_per_mm if self._mc is not None else self.machine.pulses_per_mm_x

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def job_active(self) -> bool:
        return self._busy() or self.fsm.state in (SafetyState.RUNNING, SafetyState.PAUSED)

    # ------------------------------------------------------------------ safety
    def arm(self) -> bool:
        """Operator arming: emission commands are allowed from now on (until a disarm, a fault,
        a disconnect).  Refused in dry-run mode, while a fault is latched or an interlock holds."""
        if not self._s.connected:
            self._log("arming refused: not connected")
            return False
        why = self.interlocks.blocking_arm()
        if why:
            self._log(f"arming refused: interlock active: {why}", logging.WARNING)
            return False
        if not super().arm():
            return False
        from ..machine.audit import audit
        audit("arm", logging.WARNING)
        self._on_armed()
        return True

    def disarm(self, reason: str = "") -> None:
        """Drop the arming.  A running job that fires the laser is stopped first."""
        from ..machine.audit import audit
        if self._busy() and self._job_emits:
            self.stop()
        was = self.fsm.armed
        self.fsm.disarm(reason or "disarmed by operator")
        if was:
            audit("disarm", reason=reason or "operator")
            self._log("disarmed: laser emission disabled")


__all__ = ["UiController", "DEFAULT_OUTPUT_MAP", "AXES", "CANCEL_CMD", "HOME_MODES"]
