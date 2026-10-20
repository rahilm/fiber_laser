"""The controller's start gate: the last check before anything moves the machine (docs/SAFETY.md §5a).

Every motion start goes through :meth:`StartGatePart._start_gate` right before the card gets
the first command: Start (also a planned job from the JobService, the queue's sheets and the
procedures), Dry run, Frame, laser test, Resume, Backward/Forward and the absolute moves.  The
gate refuses when

1. **a Stop or a halt happened since the request** (``generation``): every Stop, halt, fault,
   disarm, connect and disconnect advances :meth:`start_generation`; a caller that takes time
   between the operator's request and the start (a plan compiling in the JobService worker, a
   compile in ``start()``) passes the generation it read at the request;
2. the controller is not connected, a fault is latched, a job worker runs, a jog / homing / move
   is in progress, or the card - **re-read at gate time** - is not idle (machine state 0, X/Y
   velocity 0) or reports an alarm;
3. **the head is not where the job starts**: the job's start position (the head position the
   wire was compiled from: its steps are relative) differs from the position read back from the
   card by more than ``start_position_tol_mm``.  A plan made from a stale head position (a jog
   during the compile, a move while paused before Backward/Forward) is refused, never run
   offset;
4. a breakpoint is pending and the start is a job that is not the resume of it (start a new
   job only after Resume or Stop; plain moves are allowed while paused, Resume rapids back);
5. an interlock holds that would trip in the started state (door / gas for a cutting job,
   follower and laser alarm while armed), or the job can fire the beam and the machine is not
   armed.

A refusal is logged, audited (``start_refused``) and published as ``last_preflight`` (a report
with the ``start_gate`` error), so the UI shows it like a pre-flight refusal.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from ...machine import preflight as PF
from ...machine.audit import audit
from .. import alarms as A
from ..client import ControllerError
from .connection import AXES

# a servo at rest reads back within a few counts of its target: 0.05 mm is well above that and
# far below anything an operator would notice as an offset cut
START_POSITION_TOL_MM = 0.05


class StartGatePart:
    start_position_tol_mm: float = START_POSITION_TOL_MM

    # ------------------------------------------------------------------ generation
    def start_generation(self) -> int:
        """A counter that changes on every Stop, halt (any, from any path), fault, disarm,
        connect and disconnect (ControllerFacade.start_generation plus the supervisor's halt
        count).  Read it when the operator asks for a start and pass it to :meth:`start_job`
        (``generation=``): a start whose generation is stale is refused."""
        return super().start_generation() + int(self._sup.halt_count)  # type: ignore[misc]

    # ------------------------------------------------------------------ the gate
    def _gate_refuse(self, action: str, reason: str) -> bool:
        rep = PF.PreflightReport(action)
        rep.add("start_gate", PF.ERROR, reason)
        self.last_preflight = rep
        self._log(f"{action} refused: {reason}", logging.WARNING)
        audit("start_refused", logging.WARNING, mode=action, reason=reason)
        return False

    def _card_idle_head(self):
        """(status, head (x, y) in mm) re-read from the card now."""
        st = self._mc.read_status()
        p = st.pulses_per_mm or self.position_ppm
        return st, (st.axes[AXES["X"]].position / p, st.axes[AXES["Y"]].position / p)

    def _start_gate(self, action: str, *, job=None, generation: Optional[int] = None,
                    emits: bool = False, resume: bool = False, job_like: bool = True) -> bool:
        """True when ``action`` may start now (see the module doc); logs and publishes the
        refusal otherwise.  ``job``: the compiled job (its ``start_mm`` must be the head);
        ``resume``: the start continues the pending breakpoint; ``job_like``: False for a plain
        move (interlocks gated as for an idle machine)."""
        if generation is not None and generation != self.start_generation():
            return self._gate_refuse(action, "Stop or a halt since the request (press Start again)")
        if self._mc is None or not self._s.connected:
            return self._gate_refuse(action, "hardware is not connected")
        if self.fsm.faulted:
            return self._gate_refuse(action, f"a fault is latched ({self.fsm.fault}); reset it first")
        if self._busy():
            return self._gate_refuse(action, "a job is already running")
        if self._jog_active or self._jogging is not None:
            return self._gate_refuse(action, "a jog is in progress (release it first)")
        if self._homing:
            return self._gate_refuse(action, "Homing is in progress")
        if self.breakpoint is not None and not resume and job_like:
            return self._gate_refuse(action, "a job is paused at a breakpoint: Resume or Stop it first")
        try:
            st, head = self._card_idle_head()
        except (ControllerError, OSError) as e:
            return self._gate_refuse(action, f"the card does not answer ({e})")
        ms = st.machine_state()
        if ms != 0:
            what = {1: "homing", 2: "jogging / moving", 4: "running a program"}.get(ms, f"busy (state {ms})")
            return self._gate_refuse(action, f"the card is {what}: wait until it is idle")
        ppm = st.pulses_per_mm or self.position_ppm
        if any(abs(st.axes[AXES[a]].velocity) / ppm > 1e-3 for a in ("X", "Y")):
            return self._gate_refuse(action, "the axes are still moving: wait until they stop")
        alarms = A.from_status(st)
        if alarms:
            return self._gate_refuse(action, "card alarm active: " + ", ".join(a.text_en for a in alarms[:4])
                                     + " (Clear Alarm first)")
        if job is not None:
            sx, sy = getattr(job, "start_mm", head)
            d = math.dist((float(sx), float(sy)), head)
            if not d <= self.start_position_tol_mm:
                return self._gate_refuse(
                    action, f"the head is at ({head[0]:.2f}, {head[1]:.2f}) mm but the job starts at "
                    f"({sx:.2f}, {sy:.2f}) mm (it moved since the job was planned): start again")
        armed = bool(self.fsm.armed)
        try:
            zf = self._read_follower(self._mc)
        except Exception:  # noqa: BLE001 - the monitor's last state is used
            zf = None
        cond = dict(self.interlocks.active)
        cond.update(self.interlocks.conditions(st, zf, follower_present=bool(self._zf_type()),
                                               job_active=job_like, emitting=emits, armed=armed))
        hold = [msg for src, msg in cond.items()
                if self.interlocks.gated(src, job_active=job_like, emitting=emits, armed=armed)]
        if hold:
            return self._gate_refuse(action, "interlock active: " + "; ".join(hold))
        if emits and not (armed and self.laser_enabled):
            return self._gate_refuse(action, "the job fires the laser and the machine is not armed")
        with self._slock:
            self._s.x, self._s.y = head
        return True


__all__ = ["StartGatePart", "START_POSITION_TOL_MM"]
