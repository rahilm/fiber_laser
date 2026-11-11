"""What the controller parts share, declared once for the type checker (review arch A7).

:class:`~mlaser.controller.ui_adapter.UiController` is assembled from the parts in this package
(connection and poller, motion, IO, job runner, start gate) over one ``self``: each part uses
fields that ``UiController.__init__`` sets and methods that another part or the controller
defines.  :class:`PartBase` lists them with their types, and every part derives from it, so
mypy checks each use against one declaration (a misspelt field or a wrong argument type in the
code that drives the machine is found by the checker, not on the machine).

At run time ``PartBase`` is ``object``: the parts' classes, their method resolution order and
their behaviour are exactly what they were.  The declarations below are never executed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading
    from abc import abstractmethod
    from typing import Any, Callable, Optional, Union

    from ...machine.api import ControllerFacade, MachineState
    from ...machine.jobphase import JobLifecycle
    from ...nc import Machine
    from ...nc.resume import BreakpointStore
    from ..._oslock import FileLock
    from ..client import CardScaling, MotionController, TransportConfig
    from ..clock import Clock
    from ..commands import CommandExecutor, CommandTicket
    from ..interlocks import InterlockMonitor
    from ..simulator import CardSimulator
    from ..startup import CardParameters
    from ..stream import JobStreamer, ProcessStreamer
    from ..supervisor import HaltParams, SafetySupervisor

    class PartBase(ControllerFacade):
        """The fields and methods of ``UiController`` that the parts use on each other."""

        # --- configuration (UiController.__init__)
        clock: Clock
        cfg: TransportConfig
        machine: Machine
        document: Any                        # last document started (model.Document)
        cam_params: Any
        layers: Any                          # MachineParams / dict of LayerParams, or None
        poll_s: float
        output_map: dict[str, int]
        pause_mode: str
        adopt_card_scaling: bool
        pause_on_alarm: bool
        card_params: Optional[CardParameters]
        laser_type: Optional[int]
        _max_buffer_ms: float
        _heartbeat_output: int
        _heartbeat_hz: float
        _jog_deadman_s: Optional[float]
        _child_parent_timeout_s: float
        _child_stall_s: float
        _card_deadline_ms: float
        _interlocks: InterlockMonitor

        # --- card link and status
        _sup: SafetySupervisor
        sim: Optional[CardSimulator]
        version: int
        scaling: Optional[CardScaling]
        config_word: int
        connect_error: str
        card_busy: bool
        _card_lock: Optional[FileLock]
        _s: MachineState                     # written under _slock only (review A6)
        _slock: threading.Lock
        _poll_stop: threading.Event
        _poll_thread: Optional[threading.Thread]
        _poll_failing: bool
        _errors: int
        _active_alarms: list
        _zf_words: Optional[list]
        _zf_read_t: float
        _commands: CommandExecutor

        # --- job
        job: Any                             # last compiled nc.Job (whole document)
        stream_stats: dict
        _worker: Optional[threading.Thread]
        _cancel: threading.Event
        _lc: JobLifecycle
        _stop_reason: str
        _job_emits: bool
        _run_job_obj: Any                    # nc.Job streaming now (may be a resume job)
        _run_offset: int
        _bp_job: Any                         # nc.Job the breakpoint indices refer to
        _exec_index: int
        _streamer: Optional[Union[JobStreamer, ProcessStreamer]]
        _store: Optional[BreakpointStore]

        # --- motion and IO
        _homing: bool
        _home_mask: int
        _homed_mask: int
        _program_ran: bool
        _gate_open: bool
        _laser_on: bool
        _timed_shot: bool
        _power_dirty: bool
        _jogging: Optional[str]
        _jog_alive: float
        _jog_active: bool
        _mask_warned: str

        # --- UiController's own helpers and read-only properties
        @property
        @abstractmethod
        def _mc(self) -> Optional[MotionController]: ...
        @property
        @abstractmethod
        def position_ppm(self) -> float: ...
        @property
        @abstractmethod
        def interlocks(self) -> InterlockMonitor: ...
        @property
        @abstractmethod
        def max_buffer_ms(self) -> float: ...
        @property
        @abstractmethod
        def heartbeat_output(self) -> int: ...
        @property
        @abstractmethod
        def heartbeat_hz(self) -> float: ...
        @property
        @abstractmethod
        def jog_deadman_s(self) -> Optional[float]: ...
        @property
        @abstractmethod
        def child_parent_timeout_s(self) -> float: ...
        @property
        @abstractmethod
        def child_stall_s(self) -> float: ...
        @abstractmethod
        def _busy(self) -> bool: ...
        @abstractmethod
        def _ok(self) -> bool: ...
        @abstractmethod
        def _safe(self, what: str, fn: Callable[..., Any], *a: Any, **kw: Any) -> Any: ...
        @abstractmethod
        def _try(self, what: str, fn: Callable[..., Any], *a: Any, **kw: Any) -> bool: ...
        @abstractmethod
        def _offload(self) -> bool: ...
        @abstractmethod
        def _on_ui_thread(self) -> bool: ...
        @abstractmethod
        def _ui_marshal(self, fn: Callable[..., Any]) -> Callable[..., Any]: ...
        @abstractmethod
        def submit_command(self, what: str, fn: Callable[[], Any], *,
                           drop_if_stale: bool = False) -> CommandTicket: ...

        # --- defined by one part, used by another
        # connection
        @abstractmethod
        def _apply_status(self, st: Any, zf: Optional[list] = None) -> None: ...
        @abstractmethod
        def _stream_status(self, st: Any, zf: Optional[list] = None) -> None: ...
        @abstractmethod
        def _read_follower(self, mc: Any) -> Optional[list]: ...
        @abstractmethod
        def _derive_mode(self, card_state: Optional[int] = None) -> str: ...
        # motion
        @abstractmethod
        def _motion_ok(self, what: str) -> bool: ...
        @abstractmethod
        def _zf_type(self) -> int: ...
        @abstractmethod
        def _laser_type(self) -> int: ...
        @abstractmethod
        def _current_xy(self) -> tuple[float, float]: ...
        @abstractmethod
        def _decel_counts(self) -> int: ...
        @abstractmethod
        def _vm_stop_jog(self) -> None: ...
        # IO
        @abstractmethod
        def _port_guarded(self, port: int) -> bool: ...
        @abstractmethod
        def _guarded_outputs_mask(self) -> int: ...
        @abstractmethod
        def _process_outputs_mask(self) -> int: ...
        @abstractmethod
        def _gate_off(self) -> None: ...
        @abstractmethod
        def _red_light_off(self) -> None: ...
        @abstractmethod
        def _on_armed(self) -> None: ...
        # job runner
        @abstractmethod
        def _run_job(self, job: Any, mode: str, what: str, track: bool = True, base_job: Any = None,
                     offset: int = 0, after: Optional[Callable[[bool], None]] = None,
                     emits: Optional[bool] = None, resume: bool = False,
                     generation: Optional[int] = None) -> bool: ...
        @abstractmethod
        def _held(self) -> bool: ...
        @abstractmethod
        def _fresh_head(self) -> tuple[float, float]: ...
        @abstractmethod
        def _halt_params(self) -> HaltParams: ...
        # start gate
        @abstractmethod
        def _start_gate(self, action: str, *, job: Any = None, generation: Optional[int] = None,
                        emits: bool = False, resume: bool = False, job_like: bool = True) -> bool: ...
        @abstractmethod
        def _gate_refuse(self, action: str, reason: str) -> bool: ...
else:
    PartBase = object
