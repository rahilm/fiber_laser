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

Behaviour taken from the original (docs/re/protocol.md §10-§13):

* connect: read RO[0..1], write 101 := [9999, 5, 0, 0] (VM start, FUN_100571f0), optionally the
  full start-up with parameter download (``card_params``, docs/re/protocol.md §3a), then read the
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
from .parts.gate import StartGatePart
from .parts.io import IOPart
from .parts.jobs import JobRunnerPart
from .parts.motion import CANCEL_CMD, HOME_MODES, MotionPart  # noqa: F401 - re-exported
from .supervisor import CommandRefused, SafetySupervisor
from .stream import DEFAULT_MAX_BUFFER_MS

log = logging.getLogger(__name__)

# Digital output numbers for the UI's named outputs.  The real numbers are machine
# configuration (HardPara.xml DO table, pd255..282); these defaults are placeholders (INFERRED).
DEFAULT_OUTPUT_MAP = {"gas": 1, "shutter": 2, "red_light": 3, "dedust": 4, "lube": 5}


class UiController(ConnectionPart, MotionPart, IOPart, JobRunnerPart, StartGatePart, ControllerFacade):
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
        self._laser_enabled = bool(laser_enabled)  # False: dry-run mode, arming impossible (read-only)
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
        # --- safety configuration (docs/SAFETY.md): read-only properties, changed only through
        # configure_safety (audited, refused during a job)
        self._max_buffer_ms = float(max_buffer_ms)         # motion in the card FIFO, prefill included
        self._heartbeat_output = int(heartbeat_output)     # "PC alive" DO (0 = off)
        self._heartbeat_hz = float(heartbeat_hz)
        self._jog_deadman_s = jog_deadman_s                # hold-to-jog keep-alive timeout
        self._require_homed = bool(require_homed)
        self._child_parent_timeout_s = 0.3                 # streaming child: parent heartbeat
        self._child_stall_s = 0.5                          # parent: child silent -> fault
        self._interlocks = InterlockMonitor(interlocks)
        self._sup = SafetySupervisor(self.fsm, halt_timeout_ms=halt_timeout_ms,
                                    link_deadline_ms=link_deadline_ms,
                                    idle_link_deadline_ms=idle_link_deadline_ms,
                                    heartbeat_output=heartbeat_output, heartbeat_hz=heartbeat_hz,
                                    on_link_lost=self._on_link_lost,
                                    on_link_restored=self._on_link_restored,
                                    halt_params=self._halt_params)
        self._sup._guarded_fn = self._guarded_outputs_mask
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
        self._jog_active = False             # a continuous jog was sent and not yet stopped
        self._zf_words: Optional[list] = None
        self._zf_read_t = 0.0
        self.laser_type: Optional[int] = None  # SP.m_iEnableLaserType override (MachineOps)
        self._errors = 0
        self._store = None

    # ------------------------------------------------------------------ helpers
    @property
    def _mc(self) -> Optional[MotionController]:
        """The supervisor's guarded main channel (internal use)."""
        return self._sup._mc

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

    # ------------------------------------------------------------------ safety configuration
    max_buffer_ms = property(lambda self: self._max_buffer_ms,
                             doc="Motion buffered in the card FIFO (ms), prefill included.")
    heartbeat_output = property(lambda self: self._heartbeat_output, doc='"PC alive" output (0 = off).')
    heartbeat_hz = property(lambda self: self._heartbeat_hz)
    jog_deadman_s = property(lambda self: self._jog_deadman_s, doc="Hold-to-jog keep-alive timeout.")
    child_parent_timeout_s = property(lambda self: self._child_parent_timeout_s)
    child_stall_s = property(lambda self: self._child_stall_s)
    interlocks = property(lambda self: self._interlocks, doc="The interlock monitor (read-only).")

    # bounds of configure_safety: a value outside them is refused, never clamped
    SAFETY_BOUNDS = {"link_deadline_ms": (20.0, 1000.0), "idle_link_deadline_ms": (200.0, 10000.0),
                     "max_buffer_ms": (100.0, 5000.0), "heartbeat_hz": (0.5, 50.0),
                     "jog_deadman_s": (0.1, 5.0)}

    def safety_config(self) -> dict:
        """The safety configuration in effect (read-only copy)."""
        from dataclasses import asdict
        return {"interlocks": asdict(self._interlocks.cfg), "heartbeat_output": self._heartbeat_output,
                "heartbeat_hz": self._heartbeat_hz, "link_deadline_ms": self._card_link_deadline_ms(),
                "idle_link_deadline_ms": self._sup.idle_link_deadline_ms,
                "max_buffer_ms": self._max_buffer_ms, "jog_deadman_s": self._jog_deadman_s,
                "require_homed": self._require_homed, "laser_enabled": self.laser_enabled}

    def configure_safety(self, *, interlocks: Optional[InterlockConfig] = None,
                         heartbeat_output: Optional[int] = None, heartbeat_hz: Optional[float] = None,
                         link_deadline_ms: Optional[float] = None,
                         idle_link_deadline_ms: Optional[float] = None,
                         max_buffer_ms: Optional[float] = None, jog_deadman_s: Optional[float] = None,
                         require_homed: Optional[bool] = None, source: str = "operator",
                         reason: str = "") -> bool:
        """Change the safety configuration (ConfigService / Options / the commissioning
        wizard).  Refused while a job is active or when a value is outside
        :attr:`SAFETY_BOUNDS` (or the heartbeat is not an output 0..26); a plugin needs the
        plugin policy.  Applied at once (the supervisor too, when connected) and audited."""
        from ..machine.audit import audit
        from ..protocol import frames as F
        if self.plugin_policy_check(source, "configure_safety", reason):
            return False
        if self.job_active():
            self._log("safety configuration refused while a job is active", logging.WARNING)
            return False
        vals = {"link_deadline_ms": link_deadline_ms, "idle_link_deadline_ms": idle_link_deadline_ms,
                "max_buffer_ms": max_buffer_ms, "heartbeat_hz": heartbeat_hz, "jog_deadman_s": jog_deadman_s}
        for k, v in vals.items():
            if v is None:
                continue
            lo, hi = self.SAFETY_BOUNDS[k]
            if not lo <= float(v) <= hi:
                self._log(f"safety configuration refused: {k} = {v} is outside {lo:g}..{hi:g}", logging.WARNING)
                return False
        if heartbeat_output is not None and not 0 <= int(heartbeat_output) <= F.N_OUTPUTS:
            self._log(f"safety configuration refused: heartbeat output {heartbeat_output} (0..26)", logging.WARNING)
            return False
        if interlocks is not None:
            self._interlocks._configure(interlocks)
        if heartbeat_output is not None:
            self._heartbeat_output = int(heartbeat_output)
            self._sup.set_heartbeat_output(self._heartbeat_output)   # old output off first
        if heartbeat_hz is not None:
            self._heartbeat_hz = float(heartbeat_hz)
            self._sup.heartbeat_hz = self._heartbeat_hz
        if link_deadline_ms is not None:
            self._card_deadline_ms = float(link_deadline_ms)
            if self.sim is None:
                self._sup.link_deadline_ms = float(link_deadline_ms)
        if idle_link_deadline_ms is not None:
            self._sup.idle_link_deadline_ms = float(idle_link_deadline_ms)
        if max_buffer_ms is not None:
            self._max_buffer_ms = float(max_buffer_ms)
        if jog_deadman_s is not None:
            self._jog_deadman_s = float(jog_deadman_s)
        if require_homed is not None:
            self._require_homed = bool(require_homed)
        audit("safety_config", logging.WARNING, source=source, reason=reason[:120] or None,
              **{k: v for k, v in dict(vals, heartbeat_output=heartbeat_output, require_homed=require_homed,
                                       interlocks=None if interlocks is None else repr(interlocks)).items()
                 if v is not None})
        return True

    # ------------------------------------------------------------------ safety
    def arm(self, reason: str = "", source: str = "operator") -> bool:
        """Arming: emission commands are allowed from now on (until a disarm, a fault, a
        disconnect).  Refused in dry-run mode, while a fault is latched or an interlock holds,
        and for a plugin unless the plugin policy allows it.  Audited with ``source`` and
        ``reason``."""
        if self.plugin_policy_check(source, "arm", reason):
            return False
        if not self._s.connected:
            self._log("arming refused: not connected")
            return False
        why = self.interlocks.blocking_arm()
        if why:
            self._log(f"arming refused: interlock active: {why}", logging.WARNING)
            return False
        if not super().arm(reason, source):
            return False
        self._on_armed()
        return True

    def disarm(self, reason: str = "") -> None:
        """Drop the arming.  A running job that fires the laser is stopped first."""
        from ..machine.audit import audit
        self._invalidate_starts("disarm")
        if self._busy() and self._job_emits:
            self.stop()
        was = self.fsm.armed
        self.fsm.disarm(reason or "disarmed by operator")
        if was:
            audit("disarm", reason=reason or "operator")
            self._log("disarmed: laser emission disabled")


__all__ = ["UiController", "DEFAULT_OUTPUT_MAP", "AXES", "CANCEL_CMD", "HOME_MODES"]
