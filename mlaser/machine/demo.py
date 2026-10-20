"""``DemoController``: the no-hardware controller (``mlaser --fake``, UI tests).

It integrates motion from wall-clock time in :meth:`state` (infinite acceleration) and
implements the whole :class:`~mlaser.machine.api.MachineAPI`, including the safety state
machine, arming and the pre-flight check, so the UI behaves the same with and without a card
(architecture review §1.4: demo mode used to silently lose features).  Moved from
``mlaser.ui.facade`` (which re-exports it).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from . import api as _api
from . import events as EV
from .safety import SafetyState


class DemoController(_api.ControllerFacade):
    """No-hardware simulation: integrates motion in :meth:`state` from wall-clock time."""

    TABLE = (0.0, 0.0, 3000.0, 1500.0)
    JOG_SLOW, JOG_FAST, RAPID = 20.0, 200.0, 400.0

    def __init__(self, clock: Callable[[], float] = time.monotonic, laser_enabled: bool = True) -> None:
        super().__init__()
        self._clock = clock
        self._laser_enabled = bool(laser_enabled)
        self.machine = None
        self.layers = None
        self._require_homed = False      # nothing to home: the demo table is always referenced
        self._s = _api.MachineState()
        self._t = clock()
        self._jog: dict[str, tuple[int, float]] = {}
        self._path: list[tuple[float, float]] = []
        self._path_len = 0.0
        self._path_pos = 0.0
        self._path_speed = 0.0
        self._resume_mode = "process"
        self._job_t0 = 0.0
        self.halts: list[str] = []       # safe_halt reasons (tests)
        self._laser_until = 0.0

    def is_simulator(self) -> bool:
        return True

    def travel_limits(self):
        from .preflight import TravelLimits
        x0, y0, x1, y1 = self.TABLE
        return TravelLimits((x0, x1), (y0, y1), "demo table")

    # -- internal ----------------------------------------------------------
    def _advance(self) -> None:
        now = self._clock()
        dt, self._t = max(0.0, now - self._t), now
        s = self._s
        for axis, (direction, v) in self._jog.items():
            setattr(s, axis.lower(), getattr(s, axis.lower()) + direction * v * dt)
        if self._path and s.mode in ("process", "frame", "dry_run", "origin"):
            self._path_pos = min(self._path_len, self._path_pos + self._path_speed * dt)
            s.x, s.y = self._point_at(self._path_pos)
            s.progress = self._path_pos / self._path_len if self._path_len else 1.0
            s.speed = self._path_speed
            if self._path_pos >= self._path_len:
                mode = s.mode
                if mode == "origin":
                    s.homed = True
                    self.events.publish(EV.HomingDone(True))
                self._log(f"done: {mode}")
                self._path = []
                s.mode, s.speed = "ready", 0.0
                s.outputs["laser"] = False
                if mode != "origin":
                    self._finish(mode, True, "done")
        elif not self._jog and s.mode not in ("pause",):
            s.speed = 0.0
        x0, y0, x1, y1 = self.TABLE
        s.x, s.y = min(max(s.x, x0), x1), min(max(s.y, y0), y1)
        if self._laser_until and now >= self._laser_until:
            self._laser_until = 0.0
            s.outputs["laser"] = False

    def _finish(self, mode: str, ok: bool, reason: str) -> None:
        self.fsm.job_ended(reason)
        self.events.publish(EV.JobFinished(mode, ok, reason, duration_s=self._clock() - self._job_t0))

    def _point_at(self, d: float) -> tuple[float, float]:
        acc = 0.0
        for a, b in zip(self._path, self._path[1:]):
            L = math.dist(a, b)
            if acc + L >= d and L > 0:
                t = (d - acc) / L
                return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            acc += L
        return self._path[-1]

    def _run(self, pts: list[tuple[float, float]], speed: float, mode: str, emits: bool = False) -> bool:
        s = self._s
        if mode != "origin":
            why = self.fsm.job_started(emits)
            if why:
                self._log(why)
                return False
        self._path = [(s.x, s.y)] + list(pts)
        self._path_len = sum(math.dist(a, b) for a, b in zip(self._path, self._path[1:]))
        self._path_pos = 0.0
        self._path_speed = speed
        s.mode, s.progress = mode, 0.0
        self._job_t0 = self._clock()
        self._log(f"{mode}: {len(pts)} points, {self._path_len:.1f} mm")
        if mode != "origin":
            self.events.publish(EV.JobStarted(mode, f"{len(pts)} points", tracked=mode != "frame"))
        return True

    def _require(self) -> bool:
        if not self._s.connected:
            self._log("gp0: hardware is not connected")
            return False
        return True

    def _motion_ok(self, what: str) -> bool:
        if not self._require():
            return False
        if self.fsm.faulted:
            self._log(f"{what} refused: a fault is latched ({self.fsm.fault})")
            return False
        return True

    @staticmethod
    def _doc_points(document, selected_only=False) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for p in getattr(document, "parts", []):
            if selected_only and not p.selected:
                continue
            for c in p.contours:
                pts.extend(c.points(0.5))
        return pts

    # -- API ---------------------------------------------------------------
    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> bool:
        self._invalidate_starts("connect")
        s = self._s
        s.connected, s.mode, s.ftc, s.laser, s.link_ok = True, "ready", "ready", "ready", True
        self.fsm.simulator = True
        self._sync_dry_run()
        self.fsm.connected()
        self._log(f"mf0: demo controller connected ({host or '10.1.1.168'}:{port or 502})")
        for fn in list(self.connect_hooks):
            try:
                fn(self)
            except Exception as e:  # noqa: BLE001
                self._log(f"connect hook failed: {e}")
        return True

    def disconnect(self) -> None:
        self._invalidate_starts("disconnect")
        if self.job_active():
            self.safe_halt("disconnect")
        self._s = _api.MachineState(x=self._s.x, y=self._s.y)
        self.fsm.offline("disconnected")
        self._log("demo controller disconnected")

    def state(self) -> _api.MachineState:
        self._advance()
        self._fill_safety(self._s)
        return self._s

    def home(self, axes: str = "XY") -> None:
        if not self._motion_ok("Homing"):
            return
        self._advance()
        self._run([(0.0 if "X" in axes else self._s.x, 0.0 if "Y" in axes else self._s.y)], self.RAPID, "origin")
        if "Z" in axes:
            self._s.z = 0.0

    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None:
        if not self._motion_ok("jog") or self.job_active():
            return
        self._advance()
        self._jog[axis.upper()] = (1 if direction > 0 else -1, self.JOG_FAST if fast else self.JOG_SLOW)
        self._s.mode = "jog"

    def jog_stop(self, axis: Optional[str] = None) -> None:
        self._advance()
        if axis is None:
            self._jog.clear()
        else:
            self._jog.pop(axis.upper(), None)
        if not self._jog and self._s.mode == "jog":
            self._s.mode = "ready"

    def jog_step(self, axis: str, direction: int, step_mm: float, fast: bool = False) -> None:
        if not self._motion_ok("jog step") or self.job_active():
            return
        self._advance()
        a = axis.lower()
        setattr(self._s, a, getattr(self._s, a) + (1 if direction > 0 else -1) * step_mm)

    def move_to(self, x: float, y: float) -> None:
        if not self._motion_ok("move") or self.job_active():
            return
        if self._refuse(self.preflight("move", target=(x, y))):
            return
        self._advance()
        self._run([(x, y)], self.RAPID, "dry_run")

    def set_output(self, name: str, on: bool) -> None:
        if name not in self._s.outputs:
            raise KeyError(name)
        if on and name in ("laser", "shutter") and not self.fsm.armed:
            self._log(f"output {name} refused: not armed")
            return
        self._s.outputs[name] = bool(on)
        self._log(f"output {name} = {'on' if on else 'off'}")

    def start(self, document, selected_only: bool = False) -> None:
        if not self._motion_ok("start"):
            return
        if self._s.mode == "pause" and self._path:
            self.resume()
            return
        pts = self._doc_points(document, selected_only)
        if not pts:
            self._log("mp74: graphic data is empty, cannot start")
            return
        self._advance()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        emits = self.laser_enabled
        rep = self.preflight("start" if emits else "dry_run", document, selected_only,
                             extent=(min(xs), min(ys), max(xs), max(ys)), emits=emits)
        if self._refuse(rep):
            return
        speed = 100.0
        layers = getattr(document, "layers", {}) or {}
        if layers:
            speed = max(1.0, min(lp.speed for lp in layers.values()))
        if self._run(pts, speed, "process", emits=emits):
            self._s.outputs["laser"] = emits

    def start_job(self, job, generation: Optional[int] = None) -> bool:
        """Run a :class:`mlaser.services.jobs.PlannedJob` (the JobSubmitter protocol): pre-flight
        on the plan's extent, then follow its toolpath (planned order, leads, rapids).  A Stop
        since the request (``generation``) refuses it.  Returns True when it started."""
        if generation is not None and generation != self.start_generation():
            self._log("start refused: Stop since the request (press Start again)", logging.WARNING)
            return False
        if not self._motion_ok("frame" if job.mode == "frame" else "start") or self.job_active():
            return False
        from ..toolpath.points import path_points
        pts = [(x, y) for x, y, _on in path_points(job.toolpath)]
        if not pts:
            self._log("mp74: nothing to cut")
            return False
        self._advance()
        emits = job.mode == "process" and self.laser_enabled and not job.dry_run
        action = "frame" if job.mode == "frame" else ("start" if emits else "dry_run")
        doc = job.source if job.source is not None else job.document
        rep = self.preflight(action, doc if job.mode != "frame" else None, job.selected_only,
                             extent=job.cut_bounds if job.mode != "frame" else job.bounds, emits=emits)
        if self._refuse(rep):
            return False
        if job.mode == "process":
            layers = getattr(job.document, "layers", {}) or {}
            speed = max(1.0, min(lp.speed for lp in layers.values())) if layers else 100.0
        else:
            speed = self.RAPID / 2
        started = self._run(pts, speed, job.mode, emits=emits)
        if started and emits:
            self._s.outputs["laser"] = True
        return bool(started)

    def pause(self) -> None:
        self._advance()
        if self._s.mode in ("process", "dry_run", "frame"):
            self._resume_mode = self._s.mode
            self._s.mode = "pause"
            self._s.outputs["laser"] = False
            self.fsm.job_paused()
            self._log("paused")

    def resume(self, mode: str = "exact", document=None) -> None:
        self._advance()
        if self._s.mode == "pause":
            if self.fsm.faulted:
                self._log(f"resume refused: a fault is latched ({self.fsm.fault})")
                return
            self._s.mode = self._resume_mode
            self.fsm.job_resumed()
            self._log("resumed")

    def stop(self, wait: bool = False) -> threading.Event:
        self._invalidate_starts("stop")
        self._advance()
        was = self.job_active() and self._s.mode in ("process", "dry_run", "frame", "pause")
        mode = self._resume_mode if self._s.mode == "pause" else self._s.mode
        self._path, self._jog = [], {}
        self._s.mode, self._s.speed = ("ready" if self._s.connected else "offline"), 0.0
        self._s.outputs["laser"] = False
        self._log("stopped")
        if was:
            self._finish(mode, False, "stopped")
        done = threading.Event()
        done.set()
        return done

    def safe_halt(self, reason: str = "") -> None:
        self.halts.append(reason)
        self.stop()

    def frame(self, document) -> None:
        if not self._motion_ok("frame") or self.job_active():
            return
        bb = document.bbox() if hasattr(document, "bbox") else (0, 0, 0, 0)
        if bb == (0, 0, 0, 0):
            self._log("mp78: graphic data is empty, cannot go frame")
            return
        if self._refuse(self.preflight("frame", extent=bb, emits=False)):
            return
        x0, y0, x1, y1 = bb
        self._advance()
        self._run([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], self.RAPID / 2, "frame")

    def dry_run(self, document) -> None:
        if not self._motion_ok("dry run") or self.job_active():
            return
        pts = self._doc_points(document)
        if not pts:
            self._log("mp74: graphic data is empty")
            return
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if self._refuse(self.preflight("dry_run", document, extent=(min(xs), min(ys), max(xs), max(ys)),
                                       emits=False)):
            return
        self._advance()
        self._run(pts, self.RAPID / 2, "dry_run")

    def step_backward(self, length_mm: float) -> None:
        self._advance()
        if self._path and self._s.mode == "pause":
            self._path_pos = max(0.0, self._path_pos - length_mm)
            self._s.x, self._s.y = self._point_at(self._path_pos)

    def step_forward(self, length_mm: float) -> None:
        self._advance()
        if self._path and self._s.mode == "pause":
            self._path_pos = min(self._path_len, self._path_pos + length_mm)
            self._s.x, self._s.y = self._point_at(self._path_pos)

    def laser_test(self, duration_s: float = 0.1, power_pct: Optional[float] = None) -> None:
        """Fire in place for ``duration_s`` (needs the machine armed)."""
        if not self._motion_ok("laser test") or self.job_active():
            return
        if not self.fsm.armed:
            self._log("laser test refused: the machine is not armed")
            return
        if power_pct is not None:
            self._s.laser_power_pct = power_pct
        self._s.outputs["laser"] = True
        self._laser_until = self._clock() + max(0.0, duration_s)
        self._log(f"laser test {duration_s * 1000:.0f} ms")

    def wait_job(self, timeout: float = 60.0) -> bool:
        return not self._path

    def job_active(self) -> bool:
        return self.fsm.state in (SafetyState.RUNNING, SafetyState.PAUSED)

    def clear_alarms(self) -> None:
        self._s.alarms.clear()
        self.active_alarms = []
        self._log("mf21: alarms cleared")


__all__ = ["DemoController"]
