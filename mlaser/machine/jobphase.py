"""The phase of the job lifecycle: one owner, one lock, one transition table (deep review
2026-11, arch A1).

Before, "what is the job doing" was a set of flags on the controller (``_job_starting``,
``_job_live``, ``_paused`` with two meanings, ``_pausing``, ``_job_mode``), written from five
threads mostly without a lock, and ``MachineState.mode`` was computed from them plus its own
previous value.  :class:`JobLifecycle` holds the phase instead; every change goes through
:meth:`JobLifecycle.go`, which takes its lock, checks :data:`TRANSITIONS` and never waits on the
card (no I/O under the lock).  :func:`derive_mode` computes the displayed mode from the phase,
the breakpoint, the card's own state and the connection, never from the previous mode.

The phases (``AT_BREAKPOINT`` is IDLE with a breakpoint kept, reported by
``UiController.job_phase()``; the breakpoint itself stays the job runner's data):

=============  ==============================================================================
IDLE           no job; the worker (if any) has ended and every end-of-job step is done
STARTING       ``_run_job``: from the safety start to the worker thread running
RUNNING        the worker streams the job
HOLDING        ``pause_mode="fifo"``: the FIFO is stopped, the worker keeps the job
PAUSING        Hold to a breakpoint: cancelled and halted, the breakpoint is being taken
STOPPING       Stop of a running (or starting) job: halt, then the worker is joined
=============  ==============================================================================

Two facts are kept next to the phase (under the same lock): ``starting``, True from the start
of ``_run_job`` until its worker thread was started (or the start failed), also when a Stop
moved the phase to STOPPING in that window (the Stop waits for it,
:meth:`JobLifecycle.wait_started`, and then joins the worker); and ``worker``, True from the
start until the worker's ``finally`` has ended the stream (the job's mode is shown until then).

Safety behaviour is unchanged by this module: the supervisor's guard is the last word on
what reaches the card, independent of the phase; Stop and Hold set their phase and flags
inline and never queue; the worker's ``finally`` still halts.
"""
from __future__ import annotations

import enum
import threading
import time
from typing import Iterable, NamedTuple, Optional


class JobPhase(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    HOLDING = "holding"
    PAUSING = "pausing"
    STOPPING = "stopping"
    AT_BREAKPOINT = "at_breakpoint"      # reported only: IDLE with a breakpoint kept


P = JobPhase

#: the phases in which a job owns the card (the worker runs or is being ended)
LIVE = frozenset({P.RUNNING, P.HOLDING, P.PAUSING, P.STOPPING})
#: the phases in which the streamer must not touch the FIFO (the hold shared with the child)
HOLD = frozenset({P.HOLDING, P.PAUSING, P.STOPPING})

#: allowed transitions (anything else is refused and logged by the caller)
TRANSITIONS: dict[JobPhase, frozenset] = {
    P.IDLE: frozenset({P.STARTING}),
    P.STARTING: frozenset({P.RUNNING, P.STOPPING, P.IDLE}),
    P.RUNNING: frozenset({P.HOLDING, P.PAUSING, P.STOPPING, P.IDLE}),
    P.HOLDING: frozenset({P.PAUSING, P.STOPPING, P.IDLE}),
    # a Stop while the breakpoint is being taken leaves PAUSING as it is (the pause finishes,
    # then the Stop drops the breakpoint); the pause's end goes to IDLE
    P.PAUSING: frozenset({P.IDLE}),
    P.STOPPING: frozenset({P.IDLE}),
}


class PhaseState(NamedTuple):
    """One immutable reading of the lifecycle (read without the lock: a single reference)."""
    phase: JobPhase
    mode: str               # the job's mode ("process", "dry_run", "frame", "pause"), "" idle
    starting: bool          # inside _run_job's start window
    worker: bool = False    # the worker's stream is open (started, its end not reached)

    @property
    def live(self) -> bool:
        return self.phase in LIVE

    @property
    def hold(self) -> bool:
        return self.phase in HOLD


class JobLifecycle:
    """The phase, its lock and the condition the Stop waits on."""

    def __init__(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._st = PhaseState(P.IDLE, "", False)
        self.history: list[tuple[float, str, str, str]] = []    # (time, old, new, why), last 50

    # ------------------------------------------------------------------ reading
    @property
    def state(self) -> PhaseState:
        return self._st

    @property
    def phase(self) -> JobPhase:
        return self._st.phase

    # ------------------------------------------------------------------ changing
    def go(self, new: JobPhase, why: str = "", *, frm: Optional[Iterable[JobPhase]] = None,
           mode: Optional[str] = None) -> bool:
        """``phase -> new`` when the table allows it (and the phase is one of ``frm`` when
        given); ``mode`` replaces the job's mode.  Returns False (nothing changed) otherwise."""
        with self._cv:
            st = self._st
            if frm is not None and st.phase not in frm:
                return False
            if new is not st.phase and new not in TRANSITIONS.get(st.phase, ()):
                return False
            self._set(st._replace(phase=new, mode=st.mode if mode is None else mode), why)
            return True

    def begin_start(self, mode: str, why: str = "") -> bool:
        """IDLE -> STARTING with the start window open; False when a job is not over yet."""
        with self._cv:
            st = self._st
            if st.phase is not P.IDLE or st.starting:
                return False
            self._set(PhaseState(P.STARTING, mode, True, True), why or f"{mode} start")
            return True

    def end_start(self, started: bool) -> None:
        """The start window closes: STARTING -> RUNNING (worker running) or IDLE (it failed); a
        Stop meanwhile (STOPPING) stays.  Wakes :meth:`wait_started`."""
        with self._cv:
            st = self._st
            phase = st.phase
            if phase is P.STARTING:
                phase = P.RUNNING if started else P.IDLE
            self._set(st._replace(phase=phase, starting=False, worker=st.worker and started,
                                  mode=st.mode if phase is not P.IDLE else ""),
                      "worker started" if started else "start failed")

    def worker_ended(self, why: str = "worker ended") -> JobPhase:
        """The worker ended the stream: RUNNING / HOLDING -> IDLE; PAUSING and STOPPING stay
        (the pause's or the Stop's end makes them IDLE).  Returns the phase after it."""
        with self._cv:
            st = self._st
            if st.phase in (P.RUNNING, P.HOLDING):
                self._set(PhaseState(P.IDLE, "", st.starting, False), why)
            else:
                self._set(st._replace(worker=False, mode=""), why)
            return self._st.phase

    def end_job(self, why: str, *, frm: Iterable[JobPhase]) -> bool:
        """The end of the job from one of ``frm`` (the worker's end: RUNNING / HOLDING; the
        pause's end: PAUSING; the Stop's end: STOPPING) -> IDLE, the job's mode cleared."""
        with self._cv:
            st = self._st
            if st.phase not in frm:
                return False
            # (a start window still open - a Stop that waited 10 s for it in vain - stays
            # recorded: end_start closes it; IDLE lets the next start wait for that)
            self._set(PhaseState(P.IDLE, "", st.starting, False), why)
            return True

    def reset(self, why: str) -> None:
        """IDLE whatever the phase was (a disconnect, once the worker has ended): a phase left
        behind by a thread that never finished must not refuse every later start."""
        with self._cv:
            self._set(PhaseState(P.IDLE, "", False, False), why)

    def wait_started(self, timeout: float) -> bool:
        """Block until the start window is closed (wall clock, bounded); True when it is."""
        return self.wait_for(lambda st: not st.starting, timeout)

    def wait_for(self, pred, timeout: float) -> bool:
        """Block until ``pred(state)`` holds (wall clock, bounded; tests and scripts)."""
        end = time.monotonic() + timeout
        with self._cv:
            while not pred(self._st):
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True

    def _set(self, st: PhaseState, why: str) -> None:
        old = self._st
        self._st = st
        if old.phase is not st.phase:
            self.history.append((time.time(), old.phase.value, st.phase.value, why[:80]))
            del self.history[:-50]
        self._cv.notify_all()


def derive_mode(st: PhaseState, *, connected: bool, worker_alive: bool, breakpoint: bool,
                card_state: Optional[int] = None, poll_failing: bool = False) -> str:
    """``MachineState.mode`` from the job phase, the breakpoint, the connection and the card's
    machine state (``StatusSnapshot.machine_state()``: 1 homing, 2 jog / move, 4 processing;
    None = not known since the last phase change, e.g. at the end of a job).  Never depends on
    the mode shown before (arch A1: "offline" stayed after a link fault was reset)."""
    if not connected:
        return "offline"
    if st.starting or (st.worker and worker_alive):
        return "pause" if st.hold else (st.mode or "process")
    if breakpoint or st.phase is P.PAUSING:
        # PAUSING after the worker's end: the Hold is taking the breakpoint (the FIFO is
        # cleared, the resume point stored).  Never "ready" in between: the Run screen showed
        # READY for that moment (CI, a slow runner: for more than 0.3 s)
        return "pause"
    if poll_failing:
        return "offline"            # the card stopped answering the status poll (not a job)
    if card_state == 1:
        return "origin"
    if card_state == 2:
        return "jog"
    if card_state == 4:
        return "process"
    return "ready"


__all__ = ["JobPhase", "JobLifecycle", "PhaseState", "TRANSITIONS", "LIVE", "HOLD", "derive_mode"]
