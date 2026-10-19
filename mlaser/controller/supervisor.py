"""SafetySupervisor: the only holder of the card transport (docs/SAFETY.md).

``UiController`` owns one supervisor; the supervisor owns the sockets:

* the **main channel** (:class:`MotionController`), used by the poller, the in-thread job
  streamer, motion and IO commands.  Its ``guard`` hook runs :meth:`SafetySupervisor._guard`
  on **every** outgoing request: the request is classified
  (:func:`mlaser.machine.safety.classify_request`) and refused with :class:`CommandRefused`
  unless the safety state allows it (arming for emission, no motion while a fault is latched,
  no configuration writes during a job, nothing that moves after a halt until the operator
  starts something new).  Every non-read command is written to the audit trail;
* the **halt channel**: a second socket with a short timeout (``halt_timeout_ms``, 2 sends) and
  its own lock, so a halt never waits behind a blocked transaction of the main channel.  It
  carries halts and the "PC alive" heartbeat output only.

Everything else (UI, diag, devices) gets :attr:`SafetySupervisor.card`, a :class:`CardHandle`
that exposes the card methods but not the socket, the guard or ``close``.

:meth:`safe_halt` sends, each command on its own and best effort, in the order the original
uses for a program stop (parity ``test_stop_sequence``): decelerated stop ``[1, 0x1f, 2,
decel, 200000]``, PWM off ``[9999, 3, f, 0, 0]`` and ``[9999, 17, f, 0, 0]``, process outputs off
``[9999, 2, mask, 0]`` (+ 13 for outputs 17..32), follower stop ``[118, 5, 0]``, FIFO stop
(103 := 3), FIFO clear (103 := 1).  A halt that did not go through completely is repeated by
the monitor every ``halt_retry_s`` until one succeeds (the link may come back).

The monitor thread (``monitor_s``, 50 ms) also watches the link: no successful reply for
``link_deadline_ms`` (150 ms) while a job streams, or ``idle_link_deadline_ms`` otherwise,
calls ``on_link_lost``; and it toggles the heartbeat output (``heartbeat_output``,
``heartbeat_hz``) while connected and not faulted.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..machine.audit import audit
from ..machine.safety import Cmd, SafetyFSM, SafetyState, classify_request
from ..protocol import frames as F
from .client import ControllerError, MotionController, TransportConfig

log = logging.getLogger(__name__)

REFUSED_CODE = 9101


class CommandRefused(ControllerError):
    """The safety supervisor refused an outgoing command (``reason``, ``cls``)."""

    def __init__(self, reason: str, cls: Cmd = Cmd.NONE):
        super().__init__(f"refused by the safety supervisor: {reason}", REFUSED_CODE)
        self.reason = reason
        self.cls = cls


@dataclass
class HaltParams:
    """Words of the halt sequence (UiController supplies them from the machine parameters)."""
    decel: int = 4000                 # [1, 0x1f, 2, decel, 200000] (FUN_10038de0)
    pwm_freq: int = 5000              # LC.PtLaserFreq
    outputs_mask: int = 0             # process outputs (gas valves, laser DO), bit n = output n+1
    follower: bool = True


@dataclass
class HaltResult:
    reason: str
    steps: list = field(default_factory=list)       # (name, ok, error)
    t0: float = 0.0
    t1: float = 0.0
    channel: str = "halt"

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(ok for _n, ok, _e in self.steps)

    @property
    def duration_s(self) -> float:
        return self.t1 - self.t0


def halt_steps(mc, p: HaltParams, pause: bool = False) -> list[tuple[str, Callable[[], None]]]:
    """The halt sequence as (name, thunk) pairs, original stop order first."""
    f = int(p.pwm_freq)
    steps: list[tuple[str, Callable[[], None]]] = [
        ("stop motion", lambda: mc.stop_motion(int(p.decel))),
        ("pwm off", lambda: mc.command(F.mailbox_sys(F.SYS_PWM, f, 0, 0))),
        ("pwm off (bus)", lambda: mc.command(F.mailbox_sys(F.SYS_PWM_BUS, f, 0, 0))),
    ]
    if p.outputs_mask & 0xFFFF:
        steps.append(("process outputs off",
                      lambda: mc.command(F.mailbox_sys(F.SYS_OUTPUTS, p.outputs_mask & 0xFFFF, 0))))
    if (p.outputs_mask >> 16) & 0xFFFF:
        steps.append(("process outputs off (ext)",
                      lambda: mc.command(F.mailbox_sys(F.SYS_OUTPUTS_EXT, (p.outputs_mask >> 16) & 0xFFFF, 0))))
    if p.follower:
        steps.append(("follower stop", lambda: mc.command([F.OP_FOLLOW, 5, 0])))
    if not pause:
        steps.append(("fifo stop", mc.fifo_stop))
        steps.append(("fifo clear", mc.fifo_clear))
    return steps


def run_halt(mc, p: HaltParams, reason: str, pause: bool = False, channel: str = "halt") -> HaltResult:
    """Send the halt sequence, every command independently (a failure never skips the rest)."""
    res = HaltResult(reason, t0=time.monotonic(), channel=channel)
    for name, fn in halt_steps(mc, p, pause):
        try:
            fn()
            res.steps.append((name, True, ""))
        except Exception as e:  # noqa: BLE001 - best effort, every command on its own
            res.steps.append((name, False, str(e)))
    res.t1 = time.monotonic()
    return res


class CardHandle:
    """The card methods the UI, diag and devices may use; every call goes through the guarded
    main channel.  No access to the socket, the guard, ``open``/``close`` or ``connect``."""

    ALLOWED = frozenset({
        "read_regs", "read_reg", "write_regs", "write_reg", "transact", "command", "set_outputs",
        "set_output", "set_da", "set_pwm", "jog", "jog_mm", "move", "stop", "stop_axes",
        "zero_position", "home", "home_mode", "reset_alarms", "stop_motion", "stop_manu",
        "fifo_clear", "fifo_start", "fifo_stop", "fifo_write_frame", "read_status", "poll",
        "read_system_params", "read_axis_params", "read_scaling", "write_sys_param",
        "write_axis_param", "position_mm", "pulses_per_mm", "fast_len", "fast_map", "sys_rw",
        "axis_rw", "fifo_capacity", "hello", "apply_parameters", "download_parameters",
        "write_fast_map", "read_status_fast", "read_servo_info", "set_timeout",
    })

    def __init__(self, sup: "SafetySupervisor"):
        object.__setattr__(self, "_sup", sup)

    def __getattr__(self, name: str):
        if name not in CardHandle.ALLOWED:
            raise AttributeError(f"card handle: {name!r} is not available outside the safety supervisor")
        mc = object.__getattribute__(self, "_sup")._mc
        if mc is None:
            raise ControllerError("hardware is not connected", F.ERR_TIMEOUT)
        return getattr(mc, name)

    def __setattr__(self, name, value):
        raise AttributeError("card handle is read-only")

    @property
    def cfg(self) -> TransportConfig:
        mc = object.__getattribute__(self, "_sup")._mc
        return replace(mc.cfg) if mc is not None else TransportConfig()

    def __bool__(self) -> bool:
        return object.__getattribute__(self, "_sup")._mc is not None

    def __repr__(self) -> str:
        return f"<CardHandle connected={bool(self)}>"


class SafetySupervisor:
    def __init__(self, fsm: SafetyFSM, *, halt_timeout_ms: int = 40, halt_sends: int = 2,
                 link_deadline_ms: float = 150.0, idle_link_deadline_ms: float = 1500.0,
                 monitor_s: float = 0.05, halt_retry_s: float = 0.5,
                 heartbeat_output: int = 0, heartbeat_hz: float = 5.0,
                 on_link_lost: Optional[Callable[[str], None]] = None,
                 on_link_restored: Optional[Callable[[], None]] = None,
                 halt_params: Optional[Callable[[], HaltParams]] = None):
        self.fsm = fsm
        self.halt_timeout_ms = int(halt_timeout_ms)
        self.halt_sends = max(1, int(halt_sends))
        self.link_deadline_ms = float(link_deadline_ms)
        self.idle_link_deadline_ms = float(idle_link_deadline_ms)
        self.child_deadline_ms = 500.0
        self.monitor_s = float(monitor_s)
        self.halt_retry_s = float(halt_retry_s)
        self.heartbeat_output = int(heartbeat_output)
        self.heartbeat_hz = float(heartbeat_hz)
        self.on_link_lost = on_link_lost
        self.on_link_restored = on_link_restored
        self.halt_params = halt_params or HaltParams
        self._guarded = 0                     # bit n = output n+1 needs arming to switch on
        self.guarded_fn: Optional[Callable[[], int]] = None   # live mask (owner's output map)
        self._mc: Optional[MotionController] = None
        self._halt: Optional[MotionController] = None
        self.card = CardHandle(self)
        self._latch = False                   # set by a halt: nothing moves until cleared
        self._latch_reason = ""
        self.streaming: Optional[str] = None  # None | "thread" | "process"
        self.child_seen = 0.0                 # monotonic time of the last relay from the child
        self.link_ok = False
        self._lost = False
        self.halts: list[HaltResult] = []
        self._retry: Optional[tuple[str, bool]] = None
        self._mon: Optional[threading.Thread] = None
        self._mon_stop = threading.Event()
        self._hb_state = False
        self._hb_next = 0.0
        self.heartbeat_toggles = 0
        self.heartbeat_held = False           # commissioning test: output off, no toggling
        self.fifo_frames = 0
        self.fifo_emitting = 0
        self.refused: list[tuple[float, str, str]] = []
        self._halt_lock = threading.RLock()

    @property
    def guarded_outputs(self) -> int:
        fn = self.guarded_fn
        if fn is not None:
            try:
                return int(fn())
            except Exception:  # noqa: BLE001 - fall back to the last fixed mask
                pass
        return self._guarded

    @guarded_outputs.setter
    def guarded_outputs(self, v: int) -> None:
        self._guarded = int(v)

    # ------------------------------------------------------------------ channels
    @property
    def mc(self) -> Optional[MotionController]:
        """The raw main channel (the owner's internals only)."""
        return self._mc

    @property
    def connected(self) -> bool:
        return self._mc is not None

    def open(self, cfg: TransportConfig, pulses_per_mm: float) -> MotionController:
        """Create the main channel (guarded) and the halt channel; does not send anything."""
        self.close()
        mc = MotionController(config=cfg, pulses_per_mm=pulses_per_mm)
        mc.guard = self._guard
        self._mc = mc
        hcfg = replace(cfg, timeout_ms=self.halt_timeout_ms,
                       fifo_time_ms=self.halt_timeout_ms * self.halt_sends, max_recv=1)
        self._halt = MotionController(config=hcfg, pulses_per_mm=pulses_per_mm)
        self._halt.guard = self._halt_guard
        self._latch = False
        self.link_ok = False
        self._lost = False
        return mc

    def close(self) -> None:
        self.stop_monitor()
        for m in (self._mc, self._halt):
            if m is not None:
                try:
                    m.close()
                except OSError:
                    pass
        self._mc = self._halt = None
        self.streaming = None

    def _halt_channel(self) -> Optional[MotionController]:
        h = self._halt
        if h is None:
            return self._mc
        if h.cfg.transport == "tcp" and h._sock is None:
            try:
                h.open()
            except OSError:
                return self._mc           # second TCP connection refused: main channel
        return h

    # ------------------------------------------------------------------ guard
    def clear_latch(self, why: str = "") -> None:
        """A new deliberate operator action (start, jog, move, home): motion allowed again."""
        if self._latch:
            self._latch = False
            audit("halt_latch", cleared_by=why)

    @property
    def halted(self) -> bool:
        return self._latch

    def _check(self, req: F.Request, cls: Cmd) -> Optional[str]:
        why = self.fsm.check(cls)
        if why is None and self._latch and cls & (Cmd.MOTION | Cmd.EMISSION):
            why = f"halted ({self._latch_reason}): start a new action first"
        return why

    def _guard(self, req: F.Request) -> None:
        cls = classify_request(req, self.guarded_outputs)
        if cls == Cmd.READ:
            return
        why = self._check(req, cls)
        if req.func == F.FUNC_WRITE and req.addr == F.REG_FIFO_DATA:
            self.fifo_frames += 1
            if cls & Cmd.EMISSION:
                self.fifo_emitting += 1
            if why:
                audit("command", logging.WARNING, cls=cls.name or str(int(cls)), addr=req.addr,
                      words=list(req.values[:8]), refused=why)
        else:
            audit("command", logging.WARNING if why else logging.INFO, cls=cls.name or str(int(cls)),
                  func=req.func, addr=req.addr, words=list(req.values[:16]), refused=why or None)
        if why:
            self.refused.append((time.time(), cls.name or str(int(cls)), why))
            del self.refused[:-100]
            raise CommandRefused(why, cls)

    def _halt_guard(self, req: F.Request) -> None:
        cls = classify_request(req, self.guarded_outputs)
        if cls & ~(Cmd.READ | Cmd.HALT | Cmd.IO):
            raise CommandRefused(f"the halt channel carries halts only ({cls!r})", cls)
        if cls != Cmd.READ and not (cls & Cmd.IO and self._is_heartbeat(req)):
            audit("command", cls=cls.name or str(int(cls)), func=req.func, addr=req.addr,
                  words=list(req.values[:16]), channel="halt")

    def _is_heartbeat(self, req: F.Request) -> bool:
        n = self.heartbeat_output
        if not n or req.addr != F.REG_MAILBOX or len(req.values) < 4:
            return False
        bit = 1 << ((n - 1) if n <= 16 else (n - 17))
        return req.values[1] in (F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT) and req.values[2] == bit

    # ------------------------------------------------------------------ halt
    def safe_halt(self, reason: str, pause: bool = False, latch: bool = True) -> HaltResult:
        """Bring the card to a safe state (see the module doc); idempotent, never raises."""
        if latch:
            self._latch = True
            self._latch_reason = reason
        mc = self._halt_channel()
        if mc is None:
            res = HaltResult(reason, [("connection", False, "not connected")], time.monotonic(),
                             time.monotonic(), "none")
        else:
            try:
                p = self.halt_params()
            except Exception:  # noqa: BLE001 - never let a parameter error skip the halt
                p = HaltParams()
            with self._halt_lock:      # whole sequences, never interleaved on the wire
                res = run_halt(mc, p, reason, pause, "halt" if mc is self._halt else "main")
        self.halts.append(res)
        del self.halts[:-50]
        audit("halt", logging.WARNING, reason=reason, ok=res.ok, pause=pause, channel=res.channel,
              ms=round(res.duration_s * 1000, 1),
              failed=[n for n, ok, _e in res.steps if not ok] or None)
        if not res.ok and mc is not None and not pause:
            self._retry = (reason, pause)          # the monitor repeats it until it succeeds
        elif res.ok:
            self._retry = None
        return res

    # ------------------------------------------------------------------ monitor
    def start_monitor(self) -> None:
        if self._mon is not None and self._mon.is_alive():
            return
        self._mon_stop.clear()
        self._mon = threading.Thread(target=self._monitor, name="mlaser-safety", daemon=True)
        self._mon.start()

    def stop_monitor(self) -> None:
        self._mon_stop.set()
        t = self._mon
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._mon = None
        if self.heartbeat_output and self._hb_state:
            self._heartbeat_write(False)

    def link_age_s(self) -> float:
        now = time.monotonic()
        if self.streaming == "process":
            return now - self.child_seen if self.child_seen else 0.0
        mc = self._mc
        if mc is None or not mc.last_ok:
            return 0.0
        return now - mc.last_ok

    def deadline_s(self) -> float:
        """No reply for this long = link lost.  While the child process streams it owns the link
        and detects a loss itself (``link_deadline_ms``); the parent only watches the child's
        relays (``child_deadline_ms``, longer: the parent's GIL may be busy with the GUI)."""
        if self.streaming == "process":
            return self.child_deadline_ms / 1000.0
        return (self.link_deadline_ms if self.streaming else self.idle_link_deadline_ms) / 1000.0

    def _monitor(self) -> None:
        next_retry = 0.0
        while not self._mon_stop.wait(self.monitor_s):
            try:
                self._tick_link()
                now = time.monotonic()
                if self._retry is not None and now >= next_retry:
                    next_retry = now + self.halt_retry_s
                    reason, pause = self._retry
                    self.safe_halt(f"{reason} (repeat)", pause, latch=False)
                self._tick_heartbeat(now)
            except Exception:  # noqa: BLE001 - the monitor must keep running
                log.exception("safety monitor")

    def _tick_link(self) -> None:
        mc = self._mc
        if mc is None or not mc.last_ok:
            return
        age = self.link_age_s()
        if age > self.deadline_s():
            if not self._lost:
                self._lost = True
                self.link_ok = False
                why = (f"no reply from the card for {age * 1000:.0f} ms "
                       f"({'streaming' if self.streaming else 'idle'}, deadline "
                       f"{self.deadline_s() * 1000:.0f} ms)")
                audit("link", logging.ERROR, status="lost", age_ms=round(age * 1000), streaming=self.streaming)
                if self.on_link_lost is not None:
                    self.on_link_lost(why)
        else:
            if self._lost:
                self._lost = False
                audit("link", status="restored")
                if self.on_link_restored is not None:
                    self.on_link_restored()
            self.link_ok = True

    def _tick_heartbeat(self, now: float) -> None:
        if not self.heartbeat_output or self.heartbeat_hz <= 0:
            return
        alive = (self._mc is not None and self.fsm.state not in (SafetyState.FAULT, SafetyState.OFFLINE)
                 and not self._lost and not self.heartbeat_held)
        if not alive:
            if self._hb_state:
                self._heartbeat_write(False)
            return
        if now >= self._hb_next:
            self._hb_next = now + 1.0 / (2.0 * self.heartbeat_hz)
            self._heartbeat_write(not self._hb_state)

    def _heartbeat_write(self, on: bool) -> None:
        mc = self._halt_channel()
        if mc is None:
            return
        try:
            mc.set_output(self.heartbeat_output, on)
            self._hb_state = on
            self.heartbeat_toggles += 1
        except (ControllerError, OSError, ValueError):
            pass


__all__ = ["SafetySupervisor", "CardHandle", "CommandRefused", "HaltParams", "HaltResult",
           "halt_steps", "run_halt", "REFUSED_CODE"]
