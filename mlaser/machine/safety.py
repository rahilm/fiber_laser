"""Safety state machine and the outgoing-command policy (docs/SAFETY.md §2-§3).

Pure logic, no I/O: :class:`SafetyFSM` holds the state and :func:`classify_request` /
:func:`verdict` decide whether a card request may leave the PC.  The transport owner that
applies them is :class:`mlaser.controller.supervisor.SafetySupervisor`; the streaming child
process applies the same policy to its own socket (:mod:`mlaser.controller.stream`).

States (``SafetyState``)::

    OFFLINE ──connect──> DISARMED ──arm──> ARMED ──start──> RUNNING <──> PAUSED
       ^                   ^  ^   <──disarm──┘                │            │
       │                   │  └──────── job end (disarmed) ───┘            │
       │                   └──reset (operator, link up, no alarm)── FAULT <┘ (latched, from
       └──────────────── disconnect / close ────────────────────────────── any connected state)

* A connection always starts **disarmed**; arming is an explicit operator action and is
  refused while a fault is latched, the link is down, or the controller runs in dry-run mode
  (``--laser-off``).
* ``RUNNING``/``PAUSED`` remember whether the job was started armed; a laser-off job (frame,
  dry run, backward/forward) runs disarmed.
* ``FAULT`` is latched: motion (start, resume, jog, home, FIFO start) and emission are refused
  until :meth:`SafetyFSM.reset` (an operator action; the supervisor checks the link and the
  card alarms first).  After a reset the machine is disarmed again.

Command classes (:class:`Cmd`): READ and HALT always pass; IO (non-process outputs such as the
red light) needs a connection; MOTION is refused in FAULT/OFFLINE; EMISSION (step items with a
duty, PWM duty > 0, DA > 0, process outputs on, laser sync on, laser source emission) needs
``armed`` in ARMED/RUNNING; CONFIG (parameter blocks, firmware, register 100) is refused while
a job is active.
"""
from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from ..protocol import frames as F


class SafetyState(str, enum.Enum):
    OFFLINE = "offline"
    DISARMED = "disarmed"          # CONNECTED_DISARMED
    ARMED = "armed"
    RUNNING = "running"
    PAUSED = "paused"
    FAULT = "fault"

    def __str__(self) -> str:      # f-strings and logs show the value
        return self.value


CONNECTED_DISARMED = SafetyState.DISARMED
JOB_STATES = (SafetyState.RUNNING, SafetyState.PAUSED)
CONNECTED_STATES = (SafetyState.DISARMED, SafetyState.ARMED, SafetyState.RUNNING,
                    SafetyState.PAUSED, SafetyState.FAULT)


class Cmd(enum.IntFlag):
    NONE = 0
    READ = 1
    HALT = 2          # stop motion, PWM 0, outputs off, FIFO stop/clear, follower stop, reset
    IO = 4            # outputs that are not process outputs (red light, dedust, lube ...)
    MOTION = 8        # jog, move, home, FIFO start, laser-off steps, Z/follower moves, zero
    EMISSION = 16     # anything that can make the beam or the process gas come on
    CONFIG = 32       # register 100, parameter blocks, fast map, firmware/byte downloads


class SafetyRefused(RuntimeError):
    """A command was refused by the safety policy (``reason`` says why)."""

    def __init__(self, reason: str, cls: Cmd = Cmd.NONE):
        super().__init__(reason)
        self.reason = reason
        self.cls = cls


# ============================================================================ classification
_ZERO_SUBS_HALT = (F.SYS_PWM, F.SYS_PWM_BUS, F.SYS_DA, F.SYS_LASER_SYNC)
SYS_RESET = 5            # [9999, 5, 0, 0] alarm reset / VM start (client.SYS_RESET)
OP_HOME = 2
CMD_FOLLOWER_CANCEL = 101
FOLLOW_STOP = 5          # [118, 5, 0]


def _outputs_class(sub: int, mask: int, value: int, guarded: int) -> Cmd:
    """``[sub, mask, value]`` of sub 2 (outputs 1..10) or sub 13 (outputs 11..26) against the
    guarded port mask (bit n-1 = output n, :func:`mlaser.protocol.frames.output_bit`)."""
    on = F.ports_of_sub_mask(sub, int(mask) & int(value))
    if on & guarded:
        return Cmd.EMISSION
    if on:
        return Cmd.IO
    return Cmd.HALT


def classify_sys(sub: int, p: Sequence[int], guarded_outputs: int) -> Cmd:
    """A system command ``[9999, sub, p...]`` (mailbox) or FIFO sys item (params ``[sub, p...]``)."""
    p = list(p) + [0, 0, 0, 0]
    if sub in (F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT):
        return _outputs_class(sub, p[0], p[1], guarded_outputs)
    if sub in (F.SYS_PWM, F.SYS_PWM_BUS):
        # [3, freq, duty, enable]: any duty counts (the enable word's meaning is INFERRED)
        return Cmd.EMISSION if p[1] & 0xFFFFFFFF else Cmd.HALT
    if sub == F.SYS_DA:
        return Cmd.EMISSION if p[1] & 0xFFFFFFFF else Cmd.HALT
    if sub == F.SYS_LASER_SYNC:
        return Cmd.EMISSION if p[0] & 0xFFFFFFFF else Cmd.HALT
    if sub == SYS_RESET:
        return Cmd.HALT
    if sub in (F.SYS_ZERO, F.SYS_AXIS_CFG):
        return Cmd.MOTION
    return Cmd.CONFIG


def classify_mailbox(words: Sequence[int], guarded_outputs: int = 0) -> Cmd:
    """An immediate command written to register 101."""
    if not words:
        return Cmd.CONFIG
    op, p = int(words[0]) & 0xFFFFFFFF, list(words[1:])
    if op == F.OP_SYS:
        return classify_sys(p[0] if p else 0, p[1:], guarded_outputs)
    if op == F.OP_STOP_AXES:
        return Cmd.HALT
    if op == CMD_FOLLOWER_CANCEL and not p:
        return Cmd.HALT
    if op == F.OP_FOLLOW:
        sub = p[0] if p else 0
        if sub == FOLLOW_STOP or (sub == 4 and not (p[1:2] or [0])[0]):
            return Cmd.HALT
        return Cmd.MOTION
    return Cmd.MOTION       # jog 3, move 5, home 2, Z 103, follower 107/117, unknown ops


def classify_fifo_items(items: Sequence[tuple[int, Sequence[int]]], guarded_outputs: int = 0) -> Cmd:
    cls = Cmd.NONE
    for op, params in items:
        if op == F.OP_STEP:
            if len(params) >= 1 and (int(params[0]) >> 16) & 0xFF:
                return Cmd.EMISSION | cls | Cmd.MOTION
            cls |= Cmd.MOTION
        elif op == F.OP_SYS and params:
            c = classify_sys(int(params[0]), params[1:], guarded_outputs)
            if c & Cmd.EMISSION:
                return Cmd.EMISSION | cls | Cmd.MOTION
            cls |= Cmd.MOTION if c & Cmd.CONFIG else c
        else:
            cls |= Cmd.MOTION
    return cls or Cmd.MOTION


def classify_request(req: F.Request, guarded_outputs: int = 0) -> Cmd:
    """Command class of one outgoing card request (:class:`mlaser.protocol.frames.Request`)."""
    if req.func == F.FUNC_READ:
        return Cmd.READ
    if req.func != F.FUNC_WRITE:
        return Cmd.CONFIG                       # 0x26 byte downloads (firmware, follower table)
    a = req.addr
    if a == F.REG_MAILBOX:
        return classify_mailbox(req.values, guarded_outputs)
    if a == F.REG_FIFO_CTRL:
        v = req.values[0] if req.values else 0
        if v in (F.FIFO_CLEAR, F.FIFO_STOP):
            return Cmd.HALT
        # FIFO start, and any value the card might take as one (fails closed)
        return Cmd.MOTION if v == F.FIFO_START else Cmd.MOTION | Cmd.CONFIG
    if a == F.REG_FIFO_DATA:
        try:
            items = F.parse_fifo_items(req.values[1:])
        except F.FrameError:
            return Cmd.EMISSION | Cmd.MOTION    # unparsable: treat as the worst case
        return classify_fifo_items(items, guarded_outputs)
    return Cmd.CONFIG


def wire_emits(wire: Sequence[Sequence[int]], guarded_outputs: int = 0) -> bool:
    """True when a compiled job wire (list of FIFO items) can turn the beam or gas on."""
    items = []
    for w in wire:
        if not w:
            continue
        items.append((int(w[0]) & 0xFFFF, list(w[1:])))
        if len(items) >= 256:
            if classify_fifo_items(items, guarded_outputs) & Cmd.EMISSION:
                return True
            items = []
    return bool(items) and bool(classify_fifo_items(items, guarded_outputs) & Cmd.EMISSION)


def verdict(cls: Cmd, state: SafetyState, armed: bool) -> Optional[str]:
    """None when a command of class ``cls`` may be sent in ``state``, else the refusal reason."""
    if cls & Cmd.EMISSION:
        if state == SafetyState.FAULT:
            return "emission refused: a fault is latched"
        if not armed or state not in (SafetyState.ARMED, SafetyState.RUNNING):
            return f"emission refused: not armed (state {state.value})"
    if cls & Cmd.MOTION:
        if state == SafetyState.FAULT:
            return "motion refused: a fault is latched (reset it first)"
        if state == SafetyState.OFFLINE:
            return "motion refused: not connected"
    if cls & Cmd.CONFIG and state in JOB_STATES:
        return "configuration write refused while a job is active"
    if cls & Cmd.IO and state == SafetyState.OFFLINE:
        return "output refused: not connected"
    return None


# ============================================================================ state machine
@dataclass(frozen=True)
class SafetyStatus:
    """Snapshot for the UI (banner) and the event bus."""
    state: SafetyState = SafetyState.OFFLINE
    armed: bool = False
    fault: str = ""
    fault_t: float = 0.0
    simulator: bool = False
    dry_run: bool = False          # laser output disabled (--laser-off): arming impossible

    @property
    def banner(self) -> str:
        """FAULT > ARMED > DRY-RUN > SIMULATOR > DISARMED / OFFLINE."""
        if self.state == SafetyState.FAULT:
            return "FAULT"
        if self.armed:
            return "ARMED"
        if self.dry_run:
            return "DRY-RUN"
        if self.simulator:
            return "SIMULATOR"
        return "OFFLINE" if self.state == SafetyState.OFFLINE else "DISARMED"


class SafetyFSM:
    """Thread-safe state holder.  ``on_change(old, new, status, reason)`` is called after every
    transition (outside the lock)."""

    def __init__(self, on_change: Optional[Callable[[SafetyState, SafetyState, SafetyStatus, str], None]] = None):
        self._lock = threading.RLock()
        self._state = SafetyState.OFFLINE
        self._armed = False
        self._fault = ""
        self._fault_t = 0.0
        self.simulator = False
        self.dry_run = False
        self.on_change = on_change
        self.transitions: list[tuple[float, str, str, str]] = []

    # ------------------------------------------------------------------ queries
    @property
    def state(self) -> SafetyState:
        return self._state

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def fault(self) -> str:
        return self._fault

    @property
    def faulted(self) -> bool:
        return self._state == SafetyState.FAULT

    def status(self) -> SafetyStatus:
        with self._lock:
            return SafetyStatus(self._state, self._armed, self._fault, self._fault_t,
                                self.simulator, self.dry_run)

    def check(self, cls: Cmd) -> Optional[str]:
        with self._lock:
            return verdict(cls, self._state, self._armed)

    # ------------------------------------------------------------------ transitions
    def _go(self, new: SafetyState, reason: str = "") -> bool:
        with self._lock:
            old = self._state
            if old == new:
                return False
            self._state = new
            self.transitions.append((time.time(), old.value, new.value, reason))
            del self.transitions[:-100]
            st = self.status()
        if self.on_change is not None:
            self.on_change(old, new, st, reason)
        return True

    def connected(self) -> None:
        """Link up: DISARMED, or FAULT when a fault is still latched."""
        with self._lock:
            self._armed = False
            new = SafetyState.FAULT if self._fault else SafetyState.DISARMED
        self._go(new, "connected")

    def offline(self, reason: str = "disconnected") -> None:
        with self._lock:
            self._armed = False
        self._go(SafetyState.OFFLINE, reason)

    def arm(self) -> Optional[str]:
        """Returns None when armed, else the refusal reason."""
        with self._lock:
            if self.dry_run:
                return "arming refused: laser output is disabled (dry-run mode)"
            if self._state == SafetyState.FAULT:
                return f"arming refused: fault latched ({self._fault})"
            if self._state == SafetyState.OFFLINE:
                return "arming refused: not connected"
            if self._state == SafetyState.RUNNING:
                return "arming refused: a job is running"
            if self._armed:
                return None
            self._armed = True
            st = self._state
        if st == SafetyState.PAUSED:          # paused at a breakpoint: arm for the resume
            if self.on_change is not None:
                self.on_change(st, st, self.status(), "armed by operator")
        else:
            self._go(SafetyState.ARMED, "armed by operator")
        return None

    def disarm(self, reason: str = "disarmed by operator") -> bool:
        """Drop the arming.  In a job state the caller must halt first (the FSM only records)."""
        with self._lock:
            was = self._armed
            self._armed = False
            st = self._state
        if st == SafetyState.ARMED:
            self._go(SafetyState.DISARMED, reason)
        elif was and self.on_change is not None:
            s = self.status()
            self.on_change(st, st, s, reason)
        return was

    def job_started(self, emits: bool) -> Optional[str]:
        """A job (or a laser test / frame / move along the path) starts streaming."""
        with self._lock:
            if self._state == SafetyState.FAULT:
                return f"start refused: fault latched ({self._fault})"
            if self._state == SafetyState.OFFLINE:
                return "start refused: not connected"
            if self._state in JOB_STATES and self._state != SafetyState.PAUSED:
                return "start refused: a job is already running"
            if emits and not self._armed:
                return "start refused: the job fires the laser and the machine is not armed"
        self._go(SafetyState.RUNNING, "job started")
        return None

    def job_paused(self) -> None:
        if self._state == SafetyState.RUNNING:
            self._go(SafetyState.PAUSED, "paused")

    def job_resumed(self) -> None:
        if self._state == SafetyState.PAUSED:
            self._go(SafetyState.RUNNING, "resumed")

    def job_ended(self, reason: str = "job ended") -> None:
        with self._lock:
            if self._state not in JOB_STATES:
                return
            new = SafetyState.ARMED if self._armed else SafetyState.DISARMED
        self._go(new, reason)

    def raise_fault(self, reason: str) -> bool:
        """Latch a fault (first reason wins; later ones are only logged by the caller).  Returns
        True when this call latched it."""
        with self._lock:
            first = not self._fault
            if first:
                self._fault, self._fault_t = reason, time.time()
            self._armed = False
            st = self._state
        if st == SafetyState.OFFLINE:
            return first             # remembered; FAULT is entered on the next connect
        if not self._go(SafetyState.FAULT, reason) and first and self.on_change is not None:
            self.on_change(st, st, self.status(), reason)
        return first

    def reset(self, paused: bool = False) -> Optional[str]:
        """Operator reset of a latched fault (the caller has checked link and alarms).
        ``paused``: a breakpoint of the faulted job is kept, so the job stays active (PAUSED,
        disarmed) until it is resumed or stopped."""
        with self._lock:
            if not self._fault and self._state != SafetyState.FAULT:
                return None
            self._fault, self._fault_t = "", 0.0
            self._armed = False
            new = (SafetyState.OFFLINE if self._state == SafetyState.OFFLINE
                   else SafetyState.PAUSED if paused else SafetyState.DISARMED)
        self._go(new, "fault reset by operator")
        return None


__all__ = ["SafetyState", "CONNECTED_DISARMED", "Cmd", "SafetyRefused", "classify_request",
           "classify_mailbox", "classify_sys", "classify_fifo_items", "wire_emits", "verdict",
           "SafetyStatus", "SafetyFSM", "JOB_STATES", "CONNECTED_STATES"]
