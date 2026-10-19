"""Connection, status poller, alarms, interlocks and link events of the UI controller."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from ...devices import laser as _laser
from ...machine import events as EV
from ...machine import lifecycle
from ...machine.api import MachineState
from ...machine.audit import audit
from ...machine.safety import SafetyState
from .. import alarms as A
from ..client import ControllerError

log = logging.getLogger("mlaser.controller.ui_adapter")

AXES = {"X": 0, "Y": 1, "Z": 2, "W": 3}



# streaming link deadline while the in-process simulator is the card (connect_simulator)
SIM_LINK_DEADLINE_MS = 1000.0

class ConnectionPart:
    # ------------------------------------------------------------------ connection
    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> bool:
        """Open the socket, read RO[0..1] (checkMCStatus FUN_1004ed60), write 101 := [9999, 5, 0, 0]
        (or the full ``MotionController.startup`` when ``card_params`` is set), read the card
        parameters (FUN_10039f20) and adopt the scaling.  The connection always starts
        **disarmed** (or in FAULT when a fault is still latched)."""
        self.disconnect(quiet=True, keep_sim=True)
        self._invalidate_starts("connect")
        # the streaming link deadline: the configured one for a real card, the simulator's own
        # (see connect_simulator) while self.sim runs in this process
        if self.sim is None:
            self.sup.link_deadline_ms = self._card_link_deadline_ms()
        if host:
            self.cfg.host = host
        if port:
            self.cfg.port = int(port)
        mc = self.sup.open(self.cfg, self.machine.pulses_per_mm_x)
        self.sup.guarded_outputs = self._guarded_outputs_mask()
        self.sup.heartbeat_output = int(self.heartbeat_output or 0)
        self.sup.heartbeat_hz = float(self.heartbeat_hz)
        try:
            if self.card_params is not None:
                cfgw, ver = mc.startup(self.card_params)
            else:
                cfgw, ver = mc.connect()
        except (ControllerError, OSError) as e:
            self._log(f"mf166: cannot connect to {self.cfg.host}:{self.cfg.port} ({e})", logging.WARNING)
            self.sup.close()
            return False
        self.version = ver
        with self._slock:
            s = self._s
            s.connected, s.mode, s.laser, s.link_ok = True, "ready", "ready", True
            s.ftc = "ready" if self._zf_type() else "offline"
        self.fsm.simulator = self.sim is not None
        self.fsm.dry_run = not self.laser_enabled
        self.fsm.connected()
        self.sup.link_ok = True
        self._log(f"mf165: connected to MCC {self.cfg.host}:{self.cfg.port} ({self.cfg.transport}), "
                  f"version {ver} (V{ver // 100}.{ver % 100:02d}), config 0x{cfgw:08x}; "
                  f"{'SIMULATOR, ' if self.sim is not None else ''}"
                  f"{'DRY-RUN (laser disabled)' if not self.laser_enabled else 'DISARMED'}")
        audit("connect", host=self.cfg.host, port=self.cfg.port, transport=self.cfg.transport,
              version=ver, simulator=self.sim is not None, dry_run=not self.laser_enabled)
        if ver and ver < 20177:
            self._log(f"warning: firmware version {ver} < MinHardwareVer 20177")
        self._read_scaling()
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="mlaser-poll", daemon=True)
        self._poll_thread.start()
        self.sup.start_monitor()
        lifecycle.register(self)
        _laser.set_emission_guard(self._laser_source_guard)
        for fn in list(self.connect_hooks):
            try:
                fn(self)
            except Exception as e:  # noqa: BLE001
                self._log(f"connect hook failed: {e}")
        return True

    def _laser_source_guard(self, on: bool) -> None:
        """Laser source emission on (devices.laser) needs the machine armed."""
        if on and not (self.fsm.armed and self.fsm.state in (SafetyState.ARMED, SafetyState.RUNNING)):
            audit("command", logging.WARNING, cls="EMISSION", target="laser source", refused="not armed")
            raise _laser.LaserError("laser source emission refused: the machine is not armed")

    def _read_scaling(self) -> None:
        try:
            sc = self._mc.read_scaling(AXES["X"], AXES["Y"])
        except (ControllerError, OSError) as e:
            self._log(f"card parameters not readable ({e}); keeping Machine scaling")
            return
        self.scaling = sc
        if sc.soft_limits:
            self._log("card soft limits: " + ", ".join(
                f"axis {a} {lo:g}..{hi:g} mm" for a, (lo, hi) in sorted(sc.soft_limits.items())))
        if sc.ppm <= 0:
            self._log("Hardware restore failed: the card reports 0 counts/mm (gp97); keeping "
                      "Machine scaling")
            return
        self._mc.pulses_per_mm = sc.position_ppm
        if self.sup._halt is not None:
            self.sup._halt.pulses_per_mm = sc.position_ppm
        self._log(f"card parameters: {sc.summary()}")
        if self.adopt_card_scaling:
            sx, sy = sc.step_ppm(sc.x_axis), sc.step_ppm(sc.y_axis)
            if sx > 0 and sy > 0 and sc.period_us > 0:
                self.machine.pulses_per_mm_x, self.machine.pulses_per_mm_y = sx, sy
                self.machine.period_ms = sc.period_ms
                if self.sim is not None:
                    self.sim.period_ms = sc.period_ms

    def connect_simulator(self, time_scale: float = 1.0, require_homed: bool = False,
                          link_deadline_ms: Optional[float] = None, **sim_kw) -> bool:
        """Start an in-process :class:`CardSimulator` on localhost UDP and connect to it.  The
        simulator's parameter image matches ``machine`` (period, pulses/mm).  The pre-flight
        homing requirement is off for the simulator unless ``require_homed``.

        The streaming link deadline defaults to ``SIM_LINK_DEADLINE_MS`` (1 s) here: the simulated
        card shares this Python process (and its GIL) with the streamer, so on a loaded PC a
        150 ms gap measures thread scheduling, not the link, and faulted demo jobs spuriously.  A
        real card keeps the configured deadline (150 ms default); pass ``link_deadline_ms`` to
        test link-loss behaviour at the real value."""
        from ..simulator import CardSimulator
        self.disconnect(quiet=True)
        self._card_link_deadline_ms()                       # remember the configured value
        self.sup.link_deadline_ms = float(link_deadline_ms if link_deadline_ms is not None
                                          else SIM_LINK_DEADLINE_MS)
        m = self.machine
        sim_kw.setdefault("ppm", m.pulses_per_mm_x)          # position counts = step pulses
        sim_kw.setdefault("step_ppm", m.pulses_per_mm_x)
        self.sim = CardSimulator("127.0.0.1", 0, period_ms=m.period_ms,
                                 time_scale=time_scale, **sim_kw).start()
        self.require_homed = bool(require_homed)
        self._log(f"simulator started on 127.0.0.1:{self.sim.port} (UDP)")
        self.cfg.transport = "udp"
        return self.connect("127.0.0.1", self.sim.port)

    def _card_link_deadline_ms(self) -> float:
        """The deadline configured for a real card (captured once, before any simulator)."""
        v = getattr(self, "_card_deadline_ms", None)
        if v is None:
            v = self._card_deadline_ms = float(self.sup.link_deadline_ms)
        return v

    def is_simulator(self) -> bool:
        return self.sim is not None

    def disconnect(self, quiet: bool = False, keep_sim: bool = False) -> None:
        """Halt the card first (program stop, PWM 0, process outputs off, FIFO stop/clear), then
        stop the job worker, the poller and close the sockets."""
        self._invalidate_starts("disconnect")
        if self.sup.connected:
            self.sup.safe_halt("disconnect")
        self._stop_reason = "disconnect"
        self._cancel.set()
        st = self._streamer
        if st is not None and hasattr(st, "set_cancel"):
            st.set_cancel()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=5.0)
        self._poll_stop.set()
        if self._poll_thread is not None and self._poll_thread is not threading.current_thread():
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        was = self.sup.connected
        self.sup.close()
        lifecycle.unregister(self)
        if _laser.emission_guard() == self._laser_source_guard:
            _laser.set_emission_guard(None)
        if self.sim is not None and not keep_sim:
            self.sim.stop()
            self.sim = None
        with self._slock:
            self._s = MachineState(x=self._s.x, y=self._s.y)
        self.breakpoint = None
        self._jogging = None
        self._jog_active = False
        self.fsm.offline("disconnected")
        if was:
            audit("disconnect")
        if not quiet:
            self._log("disconnected")

    def state(self) -> MachineState:
        s = self._s
        self._fill_safety(s)
        s.link_ok = self.sup.link_ok and s.connected
        return s

    def snapshot(self) -> MachineState:
        """Thread-safe copy of the current state."""
        import copy
        with self._slock:
            self._fill_safety(self._s)
            return copy.deepcopy(self._s)

    def card(self):
        """The supervised card handle (None while disconnected)."""
        return self.sup.card if self.sup.connected else None

    def endpoint(self) -> Optional[tuple[str, int]]:
        return (str(self.cfg.host), int(self.cfg.port))

    def operator_action(self, what: str = "") -> None:
        """A new operator action (wizard step): clears the halt latch, nothing else (a latched
        FAULT still refuses motion in the guard)."""
        self.sup.clear_latch(what or "operator action")

    def heartbeat_status(self) -> dict:
        s = self.sup
        return {"output": int(s.heartbeat_output or 0), "hz": float(s.heartbeat_hz),
                "toggles": int(s.heartbeat_toggles), "hold": bool(s.heartbeat_held),
                "active": bool(s.heartbeat_output) and s.connected and not s.heartbeat_held}

    def heartbeat_hold(self, hold: bool) -> None:
        self.sup.heartbeat_held = bool(hold)
        audit("heartbeat_hold", hold=bool(hold))

    # ------------------------------------------------------------------ link events
    def _on_link_lost(self, why: str) -> None:
        with self._slock:
            self._s.link_ok = False
        job = self._busy()
        self.events.publish(EV.LinkLost(why, job))
        self.raise_fault(f"link lost: {why}")

    def _on_link_restored(self) -> None:
        with self._slock:
            self._s.link_ok = True
        self._log("link restored (the fault stays latched until it is reset)")
        self.events.publish(EV.LinkRestored())

    # ------------------------------------------------------------------ polling
    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            mc = self._mc
            if mc is None:
                break
            if self.sup.streaming:
                # a job streams: the streamer owns the link and relays the status
                self._poll_stop.wait(self.poll_s)
                self._deadman_tick()
                continue
            try:
                # FUN_1004eab0: fast read once the fast-status map is written (startup)
                st = mc.poll() if getattr(mc, "fast_len", 0) > 0 else mc.read_status()
                zf = self._read_follower(mc)
                self._errors = 0
            except (ControllerError, OSError) as e:
                self._errors += 1
                if self._errors == 3:
                    self._log(f"mf166: status polling failed ({e})", logging.WARNING)
                    with self._slock:
                        self._s.mode = "offline"
                self._poll_stop.wait(max(self.poll_s, 0.2))
                continue
            self._apply_status(st, zf)
            self._deadman_tick()
            self._poll_stop.wait(self.poll_s)

    def _read_follower(self, mc) -> Optional[list]:
        """Follower status words (10000 x 18) for the interlock, when an on-board FTC exists."""
        if not (self._zf_type() and self.interlocks.cfg.follower):
            return None
        now = time.monotonic()
        if now - self._zf_read_t < self.interlocks.cfg.period_s:
            return self._zf_words
        self._zf_read_t = now
        from ...protocol import frames as F
        try:
            self._zf_words = mc.read_regs(F.REG_ONBZF, F.N_ONBZF)
        except ControllerError:
            self._zf_words = None
        return self._zf_words

    def _stream_status(self, st, zf=None) -> None:
        """Status relayed by the streamer (in-thread or child process) during a job."""
        if zf is None and self.sup.streaming == "thread" and self._mc is not None:
            zf = self._read_follower(self._mc)
        self._apply_status(st, zf)

    def _apply_status(self, st, zf=None) -> None:
        ppm = st.pulses_per_mm or self.position_ppm
        new_alarms = A.from_status(st)
        raised, cleared = A.diff(self.active_alarms, new_alarms)
        homing_done = False
        with self._slock:
            s = self._s
            s.connected = True
            s.x = st.axes[AXES["X"]].position / ppm
            s.y = st.axes[AXES["Y"]].position / ppm
            s.z = st.axes[AXES["Z"]].position / ppm
            vx, vy = st.axes[AXES["X"]].velocity / ppm, st.axes[AXES["Y"]].velocity / ppm
            s.speed = (vx * vx + vy * vy) ** 0.5          # FUN_1002bb20
            self._homed_mask = st.homed_mask()            # NC +0x224 (FUN_10039590)
            ms = st.machine_state()
            for name, n in self.output_map.items():
                if name in s.outputs and n:
                    try:
                        s.outputs[name] = st.output(n)
                    except ValueError:
                        pass
            if raised or cleared:
                now = time.time()
                for a in raised:
                    s.alarms.append((a.text_en, now))
                if cleared:
                    gone = {a.text_en for a in cleared}
                    s.alarms[:] = [x for x in s.alarms if x[0] not in gone]
            if self._homing and ms != 1:
                self._homing = False
                homing_done = True
                # MainApp FUN_00598280: done when the homed mask (status bit 15, FUN_10039590)
                # covers the homed axes
                want = self._home_mask & 0xFFFF
                s.homed = not new_alarms and (st.homed_mask() & want) == want
            elif not self._homing and (self._homed_mask & 3) == 3:
                s.homed = True
            if self._busy():
                s.mode = "pause" if self._paused else (self._job_mode or "process")
            elif self.breakpoint is not None:
                s.mode = "pause"
            elif ms == 1:
                s.mode = "origin"
            elif ms == 2:
                s.mode = "jog"
            elif ms == 4:
                s.mode = "process"
            elif s.mode != "offline":
                s.mode = "ready"
        self.active_alarms = new_alarms
        for a in raised:
            self._log(f"card alarm: {a.text_en} (code {a.code})", logging.WARNING)
            audit("alarm", logging.WARNING, status="raised", code=a.code, text=a.text_en)
        for a in cleared:
            self._log(f"card alarm cleared: {a.text_en}")
            audit("alarm", status="cleared", code=a.code, text=a.text_en)
        if raised:
            self.events.publish(EV.AlarmRaised(tuple(raised), tuple(new_alarms)))
        if cleared:
            self.events.publish(EV.AlarmCleared(tuple(cleared), tuple(new_alarms)))
        if homing_done:
            self._log("mp28: go origin done" if not new_alarms else "go origin ended with alarms")
            self.events.publish(EV.HomingDone(not new_alarms))
        if raised and self._busy() and not self._paused:
            # MainApp FUN_00598280: any alarm during a job -> "Sys Pause" (FUN_005b9a60).  The
            # rebuild latches a FAULT (halt + breakpoint); reset, then Resume continues there.
            self.raise_fault("card alarm during the job: " + ", ".join(f"{a.text_en} ({a.code})" for a in raised))
        trips = self.interlocks.evaluate(
            st, zf, follower_present=bool(self._zf_type()), job_active=self.job_active(),
            emitting=self._job_emits and self._busy(), armed=self.fsm.armed)
        for t in trips:
            audit("interlock", logging.ERROR, source=t.source, message=t.message)
            self.raise_fault(f"interlock {t.source}: {t.message}")

    def _deadman_tick(self) -> None:
        """Hold-to-jog dead-man: a continuous jog without a keep-alive for ``jog_deadman_s``
        (UI hung, release lost, pendant silent) is stopped."""
        ax = self._jogging
        if ax is None or self.jog_deadman_s is None or not self._jog_active:
            return
        if time.monotonic() - self._jog_alive > self.jog_deadman_s:
            self._jogging = None
            self._jog_active = False
            self._log(f"jog {ax}: no keep-alive for {self.jog_deadman_s:.2f} s (dead-man) - stopping",
                      logging.WARNING)
            audit("deadman", axis=ax)
            try:
                self._vm_stop_jog()
            except Exception:  # noqa: BLE001
                self.sup.safe_halt("jog dead-man")


__all__ = ["ConnectionPart", "AXES"]
