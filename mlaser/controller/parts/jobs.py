"""Job runner of the UI controller: compile, pre-flight, the worker, pause / resume / stop.

Safety behaviour (docs/SAFETY.md §3, §6):

* every action that moves the machine runs the pre-flight check first (:meth:`preflight`);
* the worker's ``finally`` halts the card (``SafetySupervisor.safe_halt``) on every exit other
  than a verified, complete run: cancel, stop, pause, a stream fault, a refused command, any
  exception;
* a fault during a tracked job keeps a breakpoint at the executed wire index, so after the
  reset the operator continues with Resume (laser-off rapid back, re-pierce, rest of the wire);
* :meth:`stop` never blocks the caller: it flags the cancel, then a background thread sends the
  halt first (halt channel) and only then waits for the worker;
* :meth:`pause` sets the hold (shared with the streaming child) **before** the program stop goes
  out, so the streamer never mistakes the pause for an unexpected idle.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ...machine import events as EV
from ...machine.audit import audit
from ...machine.safety import SafetyState, wire_emits
from ...protocol import frames as F
from ..client import ControllerError
from ..supervisor import CommandRefused
from .motion import CANCEL_CMD


class JobRunnerPart:
    # ------------------------------------------------------------------ compile / pre-flight
    def _compile(self, document, selected_only: bool, dry_run: bool):
        from ...model import Document
        from ...nc import compile_job
        doc = document
        if selected_only:
            doc = Document(parts=[p for p in document.parts if p.selected], layers=document.layers,
                           version=document.version, extra=document.extra)
        doc = self.place(doc)
        return compile_job(doc, self.cam_params, self.machine, start=(self._s.x, self._s.y),
                           dry_run=dry_run or not self.laser_enabled, layers=self.layers)

    def place(self, document):
        """Job placement for the active coordinate system (MainApp FUN_0045cc20, see
        controller.coords): a shallow copy with ``extra["job_offset"]``; the edge-seek rotation
        (``extra["rotated"]``...) is centred on the placed origin.  No-op when ``coords`` is None."""
        if self.coords is None or not getattr(document, "parts", None):
            return document
        from ...model import Document
        head = (self._s.x, self._s.y)
        off = self.coords.job_offset(document.bbox(), head)
        extra = dict(document.extra)
        extra["job_offset"] = off
        if extra.get("rotated") and extra.get("rotate_center_is_origin", True):
            extra["rotate_center"] = self.coords.job_origin(head)
        return Document(parts=document.parts, layers=document.layers, version=document.version,
                        extra=extra)

    def travel_limits(self):
        """Card soft limits read back at connect, else the machine parameters."""
        from ...machine import preflight as PF
        sc = self.scaling
        lim = PF.travel_from_card(getattr(sc, "soft_limits", None), getattr(sc, "x_axis", 0),
                                  getattr(sc, "y_axis", 1)) if sc is not None else None
        return lim or PF.travel_from_machine(self.machine)

    def job_emits(self, job) -> bool:
        return (not getattr(job, "dry_run", False)) and wire_emits(job.wire, self.sup.guarded_outputs)

    def _refuse(self, rep) -> bool:
        refused = super()._refuse(rep)
        if refused:
            audit("preflight", logging.WARNING, action=rep.action, errors=[i.code for i in rep.errors])
        return refused

    # ------------------------------------------------------------------ start
    def start(self, document, selected_only: bool = False) -> None:
        if self.breakpoint is not None and not self._busy():
            self.resume(document=document)       # refused when it is not the paused job's document
            return
        self._start_doc(document, selected_only, False, "process")

    def dry_run(self, document) -> None:
        self._start_doc(document, False, True, "dry_run")

    def _fresh_head(self) -> tuple[float, float]:
        """The head position read from the card now (the poll's cached one when the read
        fails); also stored as the state's position, which compiles and placements use."""
        x, y = self._current_xy()
        with self._slock:
            self._s.x, self._s.y = x, y
        return x, y

    def _start_doc(self, document, selected_only, dry, mode) -> None:
        gen = self.start_generation()               # the request: a Stop from now on cancels it
        if not self._motion_ok("start"):
            return
        if self._busy():
            self._log("a job is already running")
            return
        if self.active_alarms:
            self._log("mp74: cannot start while alarms are active (Clear Alarm first)")
            return
        if not getattr(document, "parts", None) or (selected_only and not any(p.selected for p in document.parts)):
            self._log("mp74: graphic data is empty, cannot start")
            return
        if not self._start_gate(mode if mode == "dry_run" else "start", generation=gen):
            return                                  # not idle (jog, move, coasting): no compile
        for fn in list(self.job_start_hooks):
            try:
                fn(document)
            except Exception as e:  # noqa: BLE001 - a hook must not stop the job
                self._log(f"job start hook failed: {e}")
        self._fresh_head()                          # compile from where the head is now
        try:
            job = self._compile(document, selected_only, dry)
        except Exception as e:  # noqa: BLE001 - surface CAM errors to the user
            self._log(f"compile failed: {e}", logging.WARNING)
            return
        if not job.wire:
            self._log("mp74: nothing to cut")
            return
        emits = self.job_emits(job)
        action = "start" if emits else "dry_run"
        rep = self.preflight(action, document, selected_only, job=job, emits=emits)
        if self._refuse(rep):
            return
        if not self._start_gate(action, job=job, generation=gen, emits=emits):
            return
        if job.dry_run and mode == "process":
            self._log("laser disabled: running the job as a dry run")
        self.document = document
        self._run_job(job, mode, job.summary(), emits=emits, generation=gen)

    def start_job(self, plan, generation: Optional[int] = None) -> bool:
        """Run a :class:`mlaser.services.jobs.PlannedJob` (the JobSubmitter protocol: the plan
        the canvas shows, compiled off the GUI thread) without recompiling it: connected, idle,
        no alarms, the job-start hooks with the unplaced document, the pre-flight on the plan,
        then the start gate (``generation`` = :meth:`start_generation` read when the operator
        asked for the start; a Stop or a halt since then refuses it, as does a head that moved
        away from the plan's start).  Returns True when the job started."""
        frame = plan.mode == "frame"
        action = "frame" if frame else "start"
        if not self._motion_ok(action):
            return False
        if self._busy():
            self._log("a job is already running")
            return False
        job = plan.job
        if frame:
            if self._refuse(self.preflight("frame", job=job, emits=False)):
                return False
            if not self._start_gate("frame", job=job, generation=generation):
                return False
            x0, y0, x1, y1 = plan.document.bbox()
            return self._run_job(job, "frame", f"frame {x1 - x0:.1f} x {y1 - y0:.1f} mm", track=False,
                                 generation=generation)
        if self.active_alarms:
            self._log("mp74: cannot start while alarms are active (Clear Alarm first)")
            return False
        src = plan.source if plan.source is not None else plan.document
        for hook in list(self.job_start_hooks):
            try:
                hook(src)
            except Exception as e:  # noqa: BLE001 - a hook must not stop the job
                self._log(f"job start hook failed: {e}")
        if not job.wire:
            self._log("mp74: nothing to cut")
            return False
        emits = self.job_emits(job)
        action = "start" if emits else "dry_run"
        if self._refuse(self.preflight(action, src, plan.selected_only, job=job, emits=emits)):
            return False
        if not self._start_gate(action, job=job, generation=generation, emits=emits):
            return False
        if job.dry_run and plan.mode == "process":
            self._log("laser disabled: running the job as a dry run")
        self.document = src
        return self._run_job(job, plan.mode, job.summary(), emits=emits, generation=generation)

    def frame(self, document) -> None:
        """mp58 Frame: trace the job bounding box with the laser off."""
        gen = self.start_generation()
        if not self._motion_ok("frame") or self._busy():
            return
        if not self._start_gate("frame", generation=gen):
            return
        head = self._fresh_head()
        if hasattr(document, "bbox"):
            from ...nc.compiler import placed_document
            bb = placed_document(self.place(document)).bbox()
        else:
            bb = (0, 0, 0, 0)
        if bb == (0, 0, 0, 0):
            self._log("mp78: graphic data is empty, cannot go frame")
            return
        from ...nc import compile_items, frame_items
        job = compile_items(frame_items(bb, self.machine.frame_speed), self.machine, head, dry_run=True)
        if self._refuse(self.preflight("frame", job=job, emits=False)):
            return
        if not self._start_gate("frame", job=job, generation=gen):
            return
        self._run_job(job, "frame", f"frame {bb[2] - bb[0]:.1f} x {bb[3] - bb[1]:.1f} mm", track=False,
                      generation=gen)

    # ------------------------------------------------------------------ streaming
    def _halt_params(self):
        from ..supervisor import HaltParams
        return HaltParams(decel=self._decel_counts(), pwm_freq=int(self.machine.pt_laser_freq),
                          outputs_mask=self._process_outputs_mask(), follower=True)

    def _make_streamer(self, emits: bool):
        from dataclasses import asdict

        from ...protocol import frames as PF
        from .. import stream as S
        kw = dict(max_buffer_ms=self.max_buffer_ms, pwm_freq=int(self.machine.pt_laser_freq))
        if self._use_process():
            policy = dict(armed=bool(emits and self.fsm.armed), guarded=self.sup.guarded_outputs,
                          halt=asdict(self._halt_params()), link_deadline_ms=self.sup.link_deadline_ms,
                          halt_timeout_ms=self.sup.halt_timeout_ms, parent_timeout_s=self.child_parent_timeout_s,
                          follower_regs=(PF.REG_ONBZF, PF.N_ONBZF) if (self._zf_type() and self.interlocks.cfg.follower) else None)
            ps = S.ProcessStreamer(self.cfg, self.position_ppm, policy=policy, stall_s=self.child_stall_s, **kw)
            ps.on_status = self._stream_status
            ps.on_alive = self._child_alive
            ps.on_fault = lambda kind, why: self.raise_fault(f"streaming child: {kind}: {why}"
                                                             if kind != "link_lost" else f"link lost: {why}")
            return ps
        js = S.JobStreamer(self._mc, **kw)
        js.on_status = self._stream_status
        return js

    def _child_alive(self) -> None:
        self.sup.child_seen = time.monotonic()

    def _use_process(self) -> bool:
        if self.stream_process is not None:
            return bool(self.stream_process)
        return self.sim is None

    def _run_job(self, job, mode: str, what: str, track: bool = True, base_job=None,
                 offset: int = 0, after: Optional[Callable[[bool], None]] = None,
                 emits: Optional[bool] = None, resume: bool = False,
                 generation: Optional[int] = None) -> bool:
        """Stream ``job`` in a worker thread.  ``base_job``/``offset`` map the streaming wire index
        to the breakpoint job (resume jobs); ``track=False`` for frame/laser-test/aux moves.
        Returns False when the safety state refused the start (or a Stop came after the
        request ``generation``: the gate checked it, this closes the window up to the worker)."""
        # the job's cancel flag exists before the last generation check: a Stop that advances
        # the generation after the check sets this flag (stop() always sets the current one)
        self._cancel = threading.Event()
        if generation is not None and generation != self.start_generation():
            self._gate_refuse(mode, "Stop or a halt since the request (press Start again)")
            return False
        if emits is None:
            emits = self.job_emits(job)
        why = self.fsm.job_started(bool(emits))
        if why:
            self._log(why, logging.WARNING)
            audit("start_refused", logging.WARNING, mode=mode, reason=why)
            return False
        self.sup.clear_latch(f"{mode} start")
        if base_job is None and track:
            self.job = job
        self._bp_job = (base_job or job) if track else None
        self._run_job_obj = job
        self._run_offset = offset
        self._exec_index = -1
        self._program_ran = True             # VM +0x38 (FUN_10055840, event 9 "start manu")
        self._paused = False
        self._pausing = False
        self._stop_reason = ""
        self._job_mode = mode
        self._job_emits = bool(emits)
        with self._slock:
            self._s.mode, self._s.progress, self._s.job_index = mode, 0.0, -1
            self._s.outputs["laser"] = False     # updated from the executed step's laser byte
        self._log(f"{mode}: {what}")
        audit("job_start", mode=mode, what=what[:160], emits=bool(emits), armed=self.fsm.armed,
              items=len(job.wire), track=track)
        streamer = self._make_streamer(bool(emits))
        self._streamer = streamer
        if hasattr(streamer, "on_event"):
            streamer.on_event = self._stream_event
        markers = (base_job or job).markers
        f0 = (self.sup.fifo_frames, self.sup.fifo_emitting)
        self.events.publish(EV.JobStarted(mode, what, resume=resume, tracked=track))
        last_pub = [0.0]

        def progress(frac, idx):
            self._exec_index = idx
            bidx = idx + offset
            k = -1
            for j, (wi, _p, _c) in enumerate(markers):
                if wi <= bidx:
                    k = j
            w = job.wire[idx] if 0 <= idx < len(job.wire) else None
            on = bool(w and (w[0] & 0xFFFF) == F.OP_STEP and (w[1] >> 16) & 0xFF)
            with self._slock:
                self._s.progress, self._s.job_index = frac, k
                self._s.outputs["laser"] = on and frac < 1.0
            now = time.monotonic()
            if now - last_pub[0] >= 0.1 or frac >= 1.0:
                last_pub[0] = now
                self.events.publish(EV.JobProgress(mode, frac, k))

        def work():
            t0 = time.monotonic()
            ok = False
            reason = "done"
            try:
                # the host-side preparation (wire copy, timing, frames) runs while the poller
                # still talks to the card: once ``streaming`` is set the streaming link deadline
                # counts from the card's last reply (supervisor.deadline_s)
                if hasattr(streamer, "prepare"):
                    streamer.prepare(job)
                # process streamer: the parent's link watch starts with the child's first
                # message (0 = not seen yet; the child's start-up is bounded by
                # ProcessStreamer.startup_s, and the card has nothing of this job before it)
                self.sup.child_seen = 0.0
                self.sup.streaming = "process" if self._use_process() else "thread"
                streamer.run_job(job, progress, self._cancel, lambda: self._paused)
                if self._cancel.is_set():
                    reason = self._stop_reason or ("paused" if self._pausing else "stopped")
                    self._log(f"{mode} stopped" if reason != "paused" else f"{mode} paused")
                else:
                    ok = True
                    self._log(f"done: {mode} in {time.monotonic() - t0:.1f} s")
                    if track:
                        self.breakpoint = None
            except CommandRefused as e:
                reason = f"refused: {e.reason}"
                if not self._cancel.is_set() and not self._pausing:
                    self.raise_fault(f"{mode}: a command was refused ({e.reason})")
            except ControllerError as e:            # StreamFault, link errors, timeouts
                reason = f"fault: {e}"
                if self._cancel.is_set() and not self.fsm.faulted:
                    reason = self._stop_reason or "stopped"
                else:
                    self.raise_fault(f"{mode} aborted: {e}")
            except BaseException as e:  # noqa: BLE001 - every exit path ends in a safe state
                reason = f"error: {type(e).__name__}: {e}"
                logging.getLogger("mlaser.controller").exception("job worker")
                self.raise_fault(f"{mode}: unexpected error {type(e).__name__}: {e}")
            finally:
                if self.sup.streaming == "process" and self._mc is not None and self.sup.child_seen:
                    # the child owned the link during the job and relayed the card's status
                    # until child_seen; the parent's own connection last heard the card before
                    # the job, so without this the idle deadline (1.5 s) counted the whole job
                    # as silence and a stop / hold right after it read as a lost link
                    self._mc.last_ok = max(self._mc.last_ok, self.sup.child_seen)
                self.sup.streaming = None
                if not ok:
                    self.sup.safe_halt(f"{mode} ended: {reason}", pause=self._pausing)
                self._exec_index = getattr(streamer, "exec_index", self._exec_index)
                b = getattr(streamer, "buf", None)
                self.stream_stats = b if isinstance(b, dict) else (b.__dict__.copy() if b else {})
                frames = self.sup.fifo_frames - f0[0]
                audit("fifo", mode=mode, frames=frames, emitting=self.sup.fifo_emitting - f0[1],
                      ok=ok, reason=reason[:160])
                if (not ok and track and self._bp_job is not None and self.fsm.faulted
                        and self.breakpoint is None and not self._pausing):
                    # keep the place: after the reset, Resume continues here with a re-pierce
                    idx = max(0, min(len(self._bp_job.wire), self._exec_index + offset))
                    try:
                        self._set_breakpoint(self._bp_job, idx, f"fault: {self.fsm.fault}")
                    except Exception:  # noqa: BLE001
                        logging.getLogger("mlaser.controller").exception("breakpoint after fault")
                with self._slock:
                    self._s.outputs["laser"] = False
                    if self._s.mode in (mode, "pause") and self.breakpoint is None:
                        self._s.mode = "ready"
                self._job_mode = ""
                if after is not None:
                    try:
                        after(ok)
                    except Exception as e:  # noqa: BLE001
                        self._log(f"{mode}: {e}")
                if not self.fsm.faulted and not self._pausing:
                    if self.breakpoint is not None:
                        self.fsm.job_paused()
                    else:
                        self.fsm.job_ended(reason)
                self.events.publish(EV.JobFinished(mode, ok, reason, resume=resume, tracked=track,
                                                   duration_s=time.monotonic() - t0))

        self._worker = threading.Thread(target=work, name="mlaser-job", daemon=True)
        self._worker.start()
        return True

    def _stream_event(self, kind, *a) -> None:
        if kind == "underrun":
            self._log(f"FIFO watchdog: only {a[1]:.0f} ms of motion buffered at wire {a[0]} - "
                      "laser off and FIFO stopped (FAULT, continue from the breakpoint)", logging.ERROR)

    def wait_job(self, timeout: float = 60.0) -> bool:
        """Block until the running job's worker ends (tests / scripting)."""
        if self._worker is None:
            return True
        self._worker.join(timeout)
        return not self._worker.is_alive()

    # ------------------------------------------------------------------ fault
    def raise_fault(self, reason: str) -> None:
        """Latch a FAULT: halt the card at once (halt channel), cancel the job (the worker keeps a
        breakpoint), publish the state change.  Callable from any thread."""
        first = self.fsm.raise_fault(reason)
        self._invalidate_starts("fault")
        self._log(f"FAULT: {reason}" if first else f"(fault already latched) {reason}", logging.ERROR)
        audit("fault", logging.ERROR, reason=reason, first=first, job=self._busy())
        if self._busy():
            st = self._streamer
            if st is not None and hasattr(st, "set_cancel"):
                st.set_cancel()
            self._cancel.set()
        self._jogging = None
        self._jog_active = False
        if self.sup.connected:
            self.sup.safe_halt(f"fault: {reason}")

    def reset_fault(self) -> bool:
        """Operator reset of a latched FAULT: link must answer, the card alarms are cleared
        (``[9999, 5, 0, 0]``) and must stay clear, no interlock may hold.  The machine is
        disarmed afterwards; a breakpoint is kept for Resume, and with it the job stays active
        (FSM PAUSED, :meth:`job_active`): the document stays locked until Resume or Stop."""
        if not self.fsm.faulted and not self.fsm.fault:
            return True
        if self._busy():
            self._log("fault reset refused: the job worker is still stopping")
            return False
        if self.sup.connected:
            try:
                self._mc.reset_alarms()
                st = self._mc.read_status()
            except (ControllerError, OSError) as e:
                self._log(f"fault reset refused: the card does not answer ({e})", logging.WARNING)
                return False
            from .. import alarms as A
            left = A.from_status(st)
            if left:
                self._log("fault reset refused: card alarm still active: "
                          + ", ".join(a.text_en for a in left), logging.WARNING)
                return False
            # the interlocks as they are now, follower words included (a fresh read): without
            # them the follower alarm used to drop out of the active set and the reset passed
            # while it still held (timing-dependent: the evaluation is rate-limited)
            self._zf_read_t = 0.0
            zf = self._read_follower(self._mc)
            self._apply_status(st, zf)
            self.interlocks.evaluate(st, zf, follower_present=bool(self._zf_type()),
                                     job_active=self.job_active(), emitting=False,
                                     armed=self.fsm.armed, force=True)
        why = self.interlocks.blocking_arm()
        if why:
            self._log(f"fault reset refused: interlock active: {why}", logging.WARNING)
            return False
        reason = self.fsm.fault
        self.fsm.reset(paused=self.breakpoint is not None)
        self.sup.clear_latch("fault reset")
        audit("fault_reset", reason=reason)
        self._log("fault reset: the machine is disarmed" + (" (breakpoint kept: Resume continues there)"
                                                            if self.breakpoint is not None else ""))
        if self.breakpoint is not None:
            with self._slock:
                self._s.mode = "pause"
        return True

    def safe_halt(self, reason: str = "") -> None:
        """Stop motion, PWM 0, process outputs off, follower stop, FIFO stop + clear (each
        command independent), and stop the worker without waiting for it."""
        self._invalidate_starts("safe_halt")
        if self.sup.connected:
            self.sup.safe_halt(reason or "safe_halt")
        self._jogging = None
        self._jog_active = False
        if self._busy():
            self._stop_reason = reason or "safe_halt"
            st = self._streamer
            if st is not None and hasattr(st, "set_cancel"):
                st.set_cancel()
            self._cancel.set()

    # ------------------------------------------------------------------ pause / resume / stop
    def pause(self) -> None:
        """Pause.  ``pause_mode="breakpoint"`` (original, MainApp FUN_005b9a60): stop the program
        ("vm stop manu": decelerated stop, PWM and process outputs off, follower stop), keep the
        executed wire index as the breakpoint and write AutosaveParam.  ``"fifo"``: 103 := 3 and
        keep streaming (INFERRED phase-1 behaviour, exact continuation).  The hold is set before
        the stop is sent."""
        if not self._busy() or self._paused:
            return
        self._paused = True                          # hold first (shared with the child)
        st = self._streamer
        if st is not None and hasattr(st, "set_hold"):
            st.set_hold(True)
        if self.pause_mode == "fifo":
            hc = self.sup._halt_channel()
            self._try("pause", hc.fifo_stop) if hc is not None else None
            self._s.mode = "pause"
            self.fsm.job_paused()
            self._log("paused (FIFO stop)")
            return
        self._pause_to_breakpoint()

    def _pause_to_breakpoint(self, blocking: bool = False) -> None:
        """The breakpoint pause: cancel the stream, halt (pause halt), keep the executed wire
        index as the breakpoint once the worker ended (``blocking``: wait for it here)."""
        st = self._streamer
        bp_job, offset = self._bp_job, self._run_offset
        self._pausing = True
        self._stop_reason = "paused"
        # cancel the stream BEFORE latching the halt: a request already in flight is otherwise
        # refused by the latch while _cancel is still clear, which the worker took for a fault
        # (Hold sometimes latched FAULT under load; Wave 3 Run-track finding)
        self._cancel.set()
        if st is not None and hasattr(st, "set_cancel"):
            st.set_cancel()
        self.sup.safe_halt("pause", pause=True)

        def finish():
            if self._worker is not None:
                self._worker.join(timeout=10.0)
            if bp_job is None:                    # frame / laser test: a plain stop
                self._paused = self._pausing = False
                hc = self.sup._halt_channel()
                if hc is not None:
                    self._try("stop", hc.fifo_clear)
                with self._slock:
                    self._s.mode = "ready"
                if not self.fsm.faulted:
                    self.fsm.job_ended("stopped")
                self._log("stopped")
                return
            idx = max(0, min(len(bp_job.wire), self._exec_index + offset))
            self._set_breakpoint(bp_job, idx, "send pause manu cmd")
            self._pausing = False
            if not self.fsm.faulted:
                self.fsm.job_paused()

        w = self._worker
        if w is not None and w is not threading.current_thread():
            w.join(timeout=10.0 if blocking else 0.5)
        if w is None or not w.is_alive():
            finish()
        else:
            threading.Thread(target=finish, name="mlaser-pause", daemon=True).start()

    def _set_breakpoint(self, job, idx: int, why: str) -> None:
        from ...nc.resume import Breakpoint, BreakpointStore, document_key, position_mm
        hc = self.sup._halt_channel()
        if hc is not None:
            self._try("pause", hc.fifo_clear)
        x, y = position_mm(job, idx)
        try:
            key = document_key(self.document)
        except Exception:  # noqa: BLE001 - unknown: Resume then cannot compare (logged)
            key = ""
        self.breakpoint = Breakpoint(job, idx, x, y, doc_key=key)
        self._paused = False
        with self._slock:
            self._s.mode = "pause"
            self._s.outputs["laser"] = False
        if self.autosave_dir:
            try:
                if self._store is None:
                    self._store = BreakpointStore(self.autosave_dir)
                self._store.save_job_point(job, idx, (self._s.x, self._s.y))
            except OSError as e:
                self._log(f"AutosaveParam not written: {e}")
        self._log(f"paused ({why}): breakpoint at wire {idx} ({x:.3f}, {y:.3f})")
        audit("breakpoint", index=idx, x=round(x, 3), y=round(y, 3), why=why[:120])
        self.events.publish(EV.BreakpointChanged(self.breakpoint))

    def resume(self, mode: str = "exact", document=None) -> None:
        """Continue from the breakpoint: laser-off rapid from the head back to it, the
        contour's start-cut (re-pierce) there, then the rest of the wire.  Refused while a
        fault is latched; it passes the pre-flight (travel, homed, alarms, arming when it fires)
        and the start gate.  ``document``: the document open now - refused when it is not the
        one the paused job was started from (open it again, or Stop to drop the breakpoint).

        ``pause_mode="fifo"``: the FIFO hold is not released in place (the beam would restart
        mid-contour with no checks); the hold becomes a breakpoint first (the stream is
        cancelled and the card halted) and the resume runs from it like any other."""
        if self._busy() and self._paused:
            if self.fsm.faulted:
                self._log(f"resume refused: a fault is latched ({self.fsm.fault})")
                return
            if self.pause_mode == "fifo" and not self._pausing:
                self._log("resume from the FIFO hold: continuing through a breakpoint (re-pierce)")
                self._pause_to_breakpoint(blocking=True)
                if self._busy() or self.breakpoint is None:
                    self._log("resume refused: the held job did not stop in time", logging.WARNING)
                    return
            else:
                return                             # a breakpoint pause is still being taken
        gen = self.start_generation()
        bp = self.breakpoint
        if bp is None or self._busy() or not self._motion_ok("resume"):
            return
        if self.active_alarms:
            self._log("cannot resume while alarms are active (Clear Alarm first)")
            return
        if document is not None and getattr(bp, "doc_key", ""):
            from ...nc.resume import document_key
            if document_key(document) != bp.doc_key:
                self._gate_refuse("resume", "the open document is not the one the paused job was "
                                  "started from: open that document again, or Stop to abandon the breakpoint")
                return
        if not self._start_gate("resume", generation=gen, resume=True):
            return
        from ...nc.resume import resume_job
        head = self._fresh_head()                   # the laser-off rapid back starts here
        rj = resume_job(bp.job, bp.index, head, mode=mode, doc=self.document)
        emits = self.job_emits(rj)
        if self._refuse(self.preflight("resume", self.document, job=rj, emits=emits)):
            return
        if not self._start_gate("resume", job=rj, generation=gen, emits=emits, resume=True):
            return
        self.breakpoint = None
        self.events.publish(EV.BreakpointChanged(None))
        if not self._run_job(rj, bp.mode, f"resume from wire {bp.index}", base_job=bp.job,
                             offset=-rj.resume_offset, emits=emits, resume=True, generation=gen):
            self.breakpoint = bp                   # refused by the safety state: keep it
            self.events.publish(EV.BreakpointChanged(bp))

    def resume_saved(self, document, autosave_dir: Optional[str] = None, mode: str = "contour") -> bool:
        """BreakCon after a restart: compile ``document`` and continue from the newest
        AutosaveParam record (rebuild index convention, see mlaser.nc.resume)."""
        from ...nc.resume import Breakpoint, BreakpointStore, document_key, position_mm
        d = autosave_dir or self.autosave_dir
        if not d or not self._ok():
            return False
        store = BreakpointStore(d)
        self.document = document
        job = self._compile(document, False, False)
        idx = store.wire_index(job)
        if idx is None:
            self._log("no breakpoint record found")
            return False
        x, y = position_mm(job, idx)
        self.job = job
        self.breakpoint = Breakpoint(job, idx, x, y, doc_key=document_key(document))
        self.events.publish(EV.BreakpointChanged(self.breakpoint))
        self.resume(mode)
        return True

    def stop(self, wait: bool = False) -> threading.Event:
        """停止 (mp56, CManuPanel::OnStopBtn FUN_005ba0d0, O); never blocks the caller (returns
        an Event set when the stop is complete; ``wait=True`` waits for it).

        * Job running or paused: the cancel is flagged at once, then (background thread) the halt
          goes out first through the halt channel - the program stop ("vm stop manu":
          decelerated stop, PWM off ``[9999, 3|17, LC.PtLaserFreq, 0, 0]``, process outputs off,
          follower stop), FIFO stop and clear - and only then the worker is joined.
        * No job (job state 0): CNCModule +0xe8 ``[101]`` (follower cancel, FUN_1002c460) when
          the follower link is up (NC +0x34; the on-board follower shares the card link), then
          CNCModule +0x58 with no program on the card = "vm stop jog" (:meth:`_vm_stop_jog`:
          ``[1, 0x1f, 2, decel, 200000]``, 103 := 3, 103 := 1).  A job paused at a breakpoint
          (state 11) skips the ``[101]``.
        """
        done = threading.Event()
        # first: every start requested before this Stop is stale (a plan still compiling, a
        # start between its gate and its worker: _run_job checks it after making its cancel
        # flag, which is set just below)
        self._invalidate_starts("stop")
        running = self._busy()
        self._stop_reason = "stopped by operator"
        self._cancel.set()
        if running:
            self._paused = True                      # hold: no FIFO activity from the streamer
            st = self._streamer
            if st is not None and hasattr(st, "set_cancel"):
                st.set_cancel()
        self._jogging = None
        self._jog_active = False
        audit("stop", running=running, breakpoint=self.breakpoint is not None)

        def do_stop():
            try:
                if self.sup.connected:
                    if running:
                        self.sup.safe_halt("stop")
                    else:
                        if self.breakpoint is None and self._s.connected:
                            self._try("stop", self._mc.command, [CANCEL_CMD])
                        self._vm_stop_jog()
                w = self._worker
                if w is not None and w is not threading.current_thread():
                    w.join(timeout=10.0)
                self._paused = False
                bp = self.breakpoint
                had_bp = bp is not None
                self.breakpoint = None
                if had_bp and not running:      # a paused / faulted job is abandoned
                    self.events.publish(EV.JobFinished(getattr(bp, "mode", "process"), False, "stopped"))
                if had_bp or running:
                    self.events.publish(EV.BreakpointChanged(None))
                with self._slock:
                    self._s.outputs["laser"] = False
                    self._s.mode = "ready" if self._s.connected else "offline"
                if self.fsm.state in (SafetyState.RUNNING, SafetyState.PAUSED):
                    self.fsm.job_ended("stopped")
                self._log("stopped")
            except Exception:  # noqa: BLE001 - a stop must always complete
                logging.getLogger("mlaser.controller").exception("stop")
                self.sup.safe_halt("stop (after an error)")
            finally:
                done.set()

        threading.Thread(target=do_stop, name="mlaser-stop", daemon=True).start()
        if wait:
            done.wait(15.0)
        return done

    def _along(self, distance: float) -> None:
        """Backward/Forward along the path from the breakpoint.  The move replays the path's
        step deltas from the breakpoint position; it starts with a laser-off rapid from the head
        (read back from the card) to the breakpoint, so a jog or a move while paused never
        shifts it, and the pre-flight checks the move's whole extent from the head, not only
        its end.  The start gate checks the head is still where the move starts."""
        gen = self.start_generation()
        bp = self.breakpoint
        if bp is None:
            self._log("mp61/mp62: Backward/Forward only while a job is paused")
            return
        if self._busy() or not self._motion_ok("Backward/Forward"):
            return
        if not self._start_gate("move", generation=gen, resume=True):
            return
        from ...nc.resume import Breakpoint, along_path
        # from the head as it is (a laser-off rapid back to the breakpoint first when the head
        # is elsewhere): the pre-flight below then checks the extent that is really travelled
        seg, new = along_path(bp.job, bp.index, distance, self._fresh_head())
        if not seg.wire:
            self._log("Backward/Forward: at the end of the path")
            return
        if self._refuse(self.preflight("move", job=seg, target=seg.end_mm)):
            return
        if not self._start_gate("move", job=seg, generation=gen, resume=True):
            return
        keep = bp

        def after(ok: bool) -> None:
            idx = new if ok else keep.index
            x, y = seg.end_mm if ok else (keep.x, keep.y)
            self.breakpoint = Breakpoint(keep.job, idx, x, y, keep.mode, keep.doc_key)
            with self._slock:
                self._s.mode = "pause"
            self.events.publish(EV.BreakpointChanged(self.breakpoint))

        self.breakpoint = None
        if not self._run_job(seg, "pause", f"{'backward' if distance < 0 else 'forward'} "
                             f"{abs(distance):.2f} mm along the path (laser off)", track=False,
                             after=after, emits=False, generation=gen):
            self.breakpoint = keep

    def step_backward(self, length_mm: float) -> None:
        """mp61 Backward along the path (laser off) by ``length_mm`` (mp65) while paused."""
        self._along(-abs(float(length_mm)))

    def step_forward(self, length_mm: float) -> None:
        """mp62 Forward along the path (laser off) while paused."""
        self._along(abs(float(length_mm)))

    def clear_alarms(self) -> None:
        """gp90 Clear Alarm: ``[9999, 5, 0, 0]`` (FUN_100315f0) + local list.  A latched FAULT
        stays latched (reset it with :meth:`reset_fault`)."""
        if self._mc is not None and self._s.connected:
            self._safe("clear alarm", self._mc.reset_alarms)
        self._s.alarms.clear()
        self._log("mf21: alarms cleared ([9999, 5, 0, 0])")


__all__ = ["JobRunnerPart"]
