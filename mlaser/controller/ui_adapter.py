"""Binds the UI's :class:`mlaser.machine.api.ControllerFacade` to the real card client.

``UiController`` = :class:`~mlaser.controller.client.MotionController` (protocol, docs/protocol.md)
+ :func:`mlaser.nc.compile_job` (document -> FIFO job) + :mod:`mlaser.controller.stream` (in a worker
thread, or in a separate process for a real card) + :mod:`mlaser.nc.resume` (breakpoints), with a
status-polling thread that keeps a :class:`MachineState` current (RO block 1000.., axis RO
block 2000.., FUN_100521b0 / FUN_100523c0) and decodes the alarm words (RO[6]/RO[7],
:mod:`mlaser.controller.alarms`).

Behaviour taken from the original (docs/protocol.md §10-§13):

* connect: read RO[0..1], write 101 := [9999, 5, 0, 0] (VM start, FUN_100571f0), optionally the
  full start-up with parameter download (``card_params``, docs/protocol.md §3a), then read the
  card's parameter blocks
  ("hardware restore", FUN_10039f20) for the interpolation period and the axis scaling;
* Go Origin: mailbox ``[2, axis_mask, 0]`` (FUN_10032da0), mask from the homing mode;
* Clear Alarm: ``[9999, 5, 0, 0]`` (FUN_100315f0);
* Pause = program stop with a breakpoint (MainApp FUN_005b9a60 "Sys Pause" -> "send stop manu
  cmd"), Start/Resume continues from the breakpoint (``pause_mode="breakpoint"``, default).
  ``pause_mode="fifo"`` keeps the phase-1 behaviour (103 := 3 / 103 := 2, exact continuation);
* an alarm during a job pauses it (FUN_00598280 -> FUN_005b9a60);
* Backward/Forward while paused move along the path with the laser off.

Thread model: GUI, polling and job threads share the client, whose transaction lock serialises
them (as the original's global HAL lock 0x100b4ee8 does).  Log, alarm and breakpoint
notifications from worker threads reach the GUI thread through Qt signals (``signals``) when Qt
is available, otherwise listeners are called directly.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Callable, Optional

from ..machine.api import OUTPUTS, ControllerFacade, MachineState
from . import alarms as A
from ..protocol import frames as F
from .client import (HOME_X, HOME_XY, HOME_XY_W, HOME_XY_ZF, HOME_Y, HOME_ZF, CardScaling,
                     ControllerError, MotionController, TransportConfig, stop_decel)

log = logging.getLogger(__name__)

CANCEL_CMD = 101       # write(101, [101]): follower cancel (CNCModule +0xe8 = FUN_1002c460)

AXES = {"X": 0, "Y": 1, "Z": 2, "W": 3}     # card axis numbers (config +0xbbd4.., INFERRED default)

# Digital output numbers for the UI's named outputs.  The real numbers are machine
# configuration (HardPara.xml DO table, pd255..282); these defaults are placeholders (INFERRED).
DEFAULT_OUTPUT_MAP = {"gas": 1, "shutter": 2, "red_light": 3, "dedust": 4, "lube": 5}

# facade home() argument -> homing mode (cfg +0xbba8, MainApp FUN_004af3xx command handler)
HOME_MODES = {"X": HOME_X, "Y": HOME_Y, "XY": HOME_XY, "XYZ": HOME_XY_ZF, "Z": HOME_ZF,
              "ZF": HOME_ZF, "XYW": HOME_XY_W}

try:  # Qt is optional for the adapter (headless tests use it without a QApplication)
    from PySide6.QtCore import QObject, Signal

    class _Bridge(QObject):
        message = Signal(str)
        alarmsChanged = Signal(list)          # list[alarms.Alarm] currently active
        breakpointChanged = Signal(object)    # nc.resume.Breakpoint or None
        homingDone = Signal(bool)
except Exception:  # pragma: no cover - PySide6 missing
    _Bridge = None


def _qt_app_running() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication
        return QCoreApplication.instance() is not None
    except Exception:  # pragma: no cover
        return False


class UiController(ControllerFacade):
    """Real-card implementation of the UI facade (see module doc)."""

    def __init__(self, host: str = "10.1.1.168", port: int = 502, ini_path: Optional[str] = None,
                 machine=None, cam_params=None, layers=None, poll_s: float = 0.05,
                 laser_enabled: bool = True, output_map: Optional[dict] = None,
                 pause_mode: str = "breakpoint", stream_process: Optional[bool] = None,
                 adopt_card_scaling: bool = True, autosave_dir: Optional[str] = None,
                 pause_on_alarm: bool = True, dat_dir: Optional[str] = None,
                 jump_add_time: Optional[str] = None):
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
        self.laser_enabled = laser_enabled
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
        self.pause_on_alarm = pause_on_alarm
        # startup.CardParameters: when set, connect() runs the original's full start-up
        # (parameter download + fast-status map, MotionController.startup); None = connect only
        self.card_params = None
        self.mc: Optional[MotionController] = None
        self.sim = None                      # in-process CardSimulator when connect_simulator() is used
        self.version = 0
        self.scaling: Optional[CardScaling] = None
        self.job = None                      # last compiled mlaser.nc.Job (whole document)
        self.breakpoint = None               # nc.resume.Breakpoint while paused
        self.active_alarms: list = []        # list[alarms.Alarm]
        self.stream_stats: dict = {}
        self._s = MachineState()
        self._slock = threading.Lock()
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._paused = False
        self._job_mode = ""
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
        self.laser_type: Optional[int] = None  # SP.m_iEnableLaserType override (MachineOps)
        self._errors = 0
        self._store = None
        self._alarm_listeners: list[Callable[[list], None]] = []
        self.coords = None                   # controller.coords.CoordSystems (job placement), None = drawing coords
        self.job_start_hooks: list[Callable] = []   # fn(document) before a job compiles (autosave.chf)
        self.connect_hooks: list[Callable] = []     # fn(controller) after a successful connect
                                                    # (ui.ext_machine: start-up homing prompt)
        self._main = threading.main_thread()
        self._bridge = None
        if _Bridge is not None:
            try:
                self._bridge = _Bridge()
                self._bridge.message.connect(super()._log)
            except Exception:  # pragma: no cover
                self._bridge = None

    # ------------------------------------------------------------------ helpers
    @property
    def signals(self):
        """Qt signal bridge (``message``, ``alarmsChanged``, ``breakpointChanged``,
        ``homingDone``) or None without PySide6."""
        return self._bridge

    def add_alarm_listener(self, fn: Callable[[list], None]) -> None:
        """Called with the active alarm list whenever it changes (worker thread)."""
        self._alarm_listeners.append(fn)

    def _log(self, msg: str) -> None:
        log.info(msg)
        if (self._bridge is not None and threading.current_thread() is not self._main
                and _qt_app_running()):
            self._bridge.message.emit(msg)
        else:
            super()._log(msg)

    def _emit(self, name: str, *args) -> None:
        if self._bridge is not None and _qt_app_running():
            try:
                getattr(self._bridge, name).emit(*args)
            except Exception:  # noqa: BLE001
                pass

    def _ok(self) -> bool:
        if self.mc is None or not self._s.connected:
            self._log("gp0: hardware is not connected")
            return False
        return True

    def _safe(self, what: str, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except NotImplementedError as e:
            self._log(f"{what}: not available - {e}")
        except (ControllerError, OSError, ValueError) as e:
            self._log(f"{what} failed: {e}")
        return None

    @property
    def ppm(self) -> tuple[float, float]:
        """Step-item pulses per mm (Machine), X and Y."""
        return (self.machine.pulses_per_mm_x, self.machine.pulses_per_mm_y)

    @property
    def position_ppm(self) -> float:
        """Divisor of the card's position/velocity counts (config +0xbc28)."""
        return self.mc.pulses_per_mm if self.mc is not None else self.machine.pulses_per_mm_x

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _use_process(self) -> bool:
        if self.stream_process is not None:
            return bool(self.stream_process)
        return self.sim is None

    def _decel_counts(self) -> int:
        """Stop deceleration word of ``[1, 0x1f, 2, decel, 200000]`` (FUN_10054900 ->
        FUN_10038de0, O): ftol(clamp(MP.JogStopDccFactor * MC.XFastMoveAcc, 2000, FCP.MaxAcc)),
        not scaled by ppm (capture: 4000 with the defaults)."""
        m = self.machine
        return F.ftol(stop_decel(m.move_acc, m.jog_stop_factor, m.max_acc))

    def _process_outputs_mask(self) -> int:
        pc = getattr(self.machine, "process", None)
        ports = set()
        if pc is not None:
            ports.update(p for p in getattr(pc, "gas_valves", ()) if p)
            if getattr(pc, "do_laser", 0):
                ports.add(pc.do_laser)
        g = self.output_map.get("gas")
        if g:
            ports.add(g)
        m = 0
        for p in ports:
            if 1 <= p <= 16:
                m |= 1 << (p - 1)
        return m

    # ------------------------------------------------------------------ connection
    def connect(self, host: Optional[str] = None, port: Optional[int] = None) -> bool:
        """Open the socket, read RO[0..1] (checkMCStatus FUN_1004ed60), write 101 := [9999, 5, 0, 0]
        (or the full ``MotionController.startup`` when ``card_params`` is set), read the card
        parameters (FUN_10039f20) and adopt the scaling."""
        self.disconnect(quiet=True, keep_sim=True)
        if host:
            self.cfg.host = host
        if port:
            self.cfg.port = int(port)
        self.mc = MotionController(config=self.cfg, pulses_per_mm=self.machine.pulses_per_mm_x)
        try:
            if self.card_params is not None:
                cfgw, ver = self.mc.startup(self.card_params)
            else:
                cfgw, ver = self.mc.connect()
        except (ControllerError, OSError) as e:
            self._log(f"mf166: cannot connect to {self.cfg.host}:{self.cfg.port} ({e})")
            self.mc.close()
            self.mc = None
            return False
        self.version = ver
        with self._slock:
            s = self._s
            s.connected, s.mode, s.laser = True, "ready", "ready"
            s.ftc = "offline"       # follower status registers: see mlaser.devices
        self._log(f"mf165: connected to MCC {self.cfg.host}:{self.cfg.port} ({self.cfg.transport}), "
                  f"version {ver} (V{ver // 100}.{ver % 100:02d}), config 0x{cfgw:08x}")
        if ver and ver < 20177:
            self._log(f"warning: firmware version {ver} < MinHardwareVer 20177")
        self._read_scaling()
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="mlaser-poll", daemon=True)
        self._poll_thread.start()
        for fn in list(self.connect_hooks):
            try:
                fn(self)
            except Exception as e:  # noqa: BLE001
                self._log(f"connect hook failed: {e}")
        return True

    def _read_scaling(self) -> None:
        try:
            sc = self.mc.read_scaling(AXES["X"], AXES["Y"])
        except (ControllerError, OSError) as e:
            self._log(f"card parameters not readable ({e}); keeping Machine scaling")
            return
        self.scaling = sc
        if sc.ppm <= 0:
            self._log("Hardware restore failed: the card reports 0 counts/mm (gp97); keeping "
                      "Machine scaling")
            return
        self.mc.pulses_per_mm = sc.position_ppm
        self._log(f"card parameters: {sc.summary()}")
        if self.adopt_card_scaling:
            sx, sy = sc.step_ppm(sc.x_axis), sc.step_ppm(sc.y_axis)
            if sx > 0 and sy > 0 and sc.period_us > 0:
                self.machine.pulses_per_mm_x, self.machine.pulses_per_mm_y = sx, sy
                self.machine.period_ms = sc.period_ms
                if self.sim is not None:
                    self.sim.period_ms = sc.period_ms

    def connect_simulator(self, time_scale: float = 1.0, **sim_kw) -> bool:
        """Start an in-process :class:`CardSimulator` on localhost UDP and connect to it.  The
        simulator's parameter image matches ``machine`` (period, pulses/mm)."""
        from .simulator import CardSimulator
        self.disconnect(quiet=True)
        m = self.machine
        sim_kw.setdefault("ppm", m.pulses_per_mm_x)          # position counts = step pulses
        sim_kw.setdefault("step_ppm", m.pulses_per_mm_x)
        self.sim = CardSimulator("127.0.0.1", 0, period_ms=m.period_ms,
                                 time_scale=time_scale, **sim_kw).start()
        self._log(f"simulator started on 127.0.0.1:{self.sim.port} (UDP)")
        self.cfg.transport = "udp"
        return self.connect("127.0.0.1", self.sim.port)

    def disconnect(self, quiet: bool = False, keep_sim: bool = False) -> None:
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
        if self.mc is not None:
            self.mc.close()
            self.mc = None
        if self.sim is not None and not keep_sim:
            self.sim.stop()
            self.sim = None
        with self._slock:
            self._s = MachineState(x=self._s.x, y=self._s.y)
        self.breakpoint = None
        if not quiet:
            self._log("disconnected")

    def state(self) -> MachineState:
        return self._s

    # ------------------------------------------------------------------ polling
    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            mc = self.mc
            if mc is None:
                break
            try:
                # FUN_1004eab0: fast read once the fast-status map is written (startup)
                st = mc.poll() if getattr(mc, "fast_len", 0) > 0 else mc.read_status()
                self._errors = 0
            except (ControllerError, OSError) as e:
                self._errors += 1
                if self._errors == 3:
                    self._log(f"mf166: status polling failed ({e})")
                    with self._slock:
                        self._s.mode = "offline"
                self._poll_stop.wait(max(self.poll_s, 0.2))
                continue
            self._apply_status(st)
            self._poll_stop.wait(self.poll_s)

    def _apply_status(self, st) -> None:
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
            self._log(f"card alarm: {a.text_en} (code {a.code})")
        for a in cleared:
            self._log(f"card alarm cleared: {a.text_en}")
        if raised or cleared:
            for fn in list(self._alarm_listeners):
                try:
                    fn(list(new_alarms))
                except Exception:  # noqa: BLE001
                    pass
            self._emit("alarmsChanged", list(new_alarms))
        if homing_done:
            self._log("mp28: go origin done" if not new_alarms else "go origin ended with alarms")
            self._emit("homingDone", not new_alarms)
        if raised and self.pause_on_alarm and self._busy() and not self._paused:
            # MainApp FUN_00598280: any alarm during a job -> "Sys Pause" (FUN_005b9a60)
            threading.Thread(target=self.pause, name="mlaser-alarm-pause", daemon=True).start()

    # ------------------------------------------------------------------ motion
    def home(self, axes: str = "XY") -> None:
        """Go Origin (mf15/mf16/mf17/mf18/mf222): mailbox ``[2, mask, 0]`` (FUN_10032da0).

        ``"Z"`` is the panel's Z回原 (mp30, FUN_005bec40): only with a follower (ZF.ZFType != 0,
        cfg +0x4994); without one the original sends nothing."""
        if not self._ok():
            return
        if self._busy():
            self._log("Go Origin refused: a job is running")
            return
        mode = HOME_MODES.get(axes.upper())
        if mode is None:
            self._log(f"Go Origin: unknown axes {axes!r}")
            return
        if mode == HOME_ZF and not self._zf_type():
            self._log("mp30: no follower configured (ZF.ZFType = 0), nothing to home")
            return
        mask = self._safe("Go Origin", self.mc.home_mode, mode, AXES["X"], AXES["Y"], AXES["W"])
        if mask:
            self._home_mask = mask
            self._homing = True
            self.breakpoint = None
            with self._slock:
                self._s.mode, self._s.homed = "origin", False
            self._log(f"mf15: Go Origin {axes} (mode {mode}, [2, 0x{mask:x}, 0])")

    def startup_homing(self, confirm: Optional[Callable[[str], bool]] = None,
                       normal_exit: bool = True) -> bool:
        """The start-up part of FUN_00596320 after the connection (O):

        1. NC +0x164 laser gate / emission off (FUN_1002db10, per LGP.LaserControlType: nothing
           for the standard card with control type 0) and NC +0x178 red light off
           (FUN_1002ddb0: output LGP.DORedLight for fiber, LGP.CO2DORedLight for CO2);
        2. softPara.ini NormalExit == 1, X|Y not homed (NC +0x224 = FUN_10039590 & 3) and the
           coordinate system is not Manual: the go-origin prompt (mp119, DAT_008c2068); OK -> sysGoOrigin
           (FUN_005b5de0: mf156 "System will go origin, continue?", then panel step 8 =
           FUN_005b6070, mode 3 (4 with AF.AFType 5, 5 with 6) -> ``[2, mask, 0]``).  Cancel:
           NC +0x198 soft limits when the two state reads are 0 (not reproduced: no traffic in
           the capture's path);
        3. CNCModule +0x2cc (VM +0x1a4 FUN_1003d3a0): ``read 1050x3``, the third word is added
           to cfg +0x4448 (a statistics counter, U).

        ``confirm(key)`` answers the dialogs (default: OK).  Returns True when homing started."""
        if not self._ok():
            return False
        ask = confirm or (lambda _k: True)
        self._gate_off()
        self._red_light_off()
        go = False
        manual = getattr(self.coords, "kind", 0) == 7
        if normal_exit and (self._homed_mask & 3) == 0 and not manual:
            go = ask("mp119") and ask("mf156")
        # The original posts the go-origin as panel step 8 (sent by the panel thread) and the
        # start-up routine goes on to the 1050 read at once: on the wire the two are 0.4..2 ms
        # apart in either order (two captures).  The rebuild sends the read first, the order of
        # the reference capture.
        try:
            self.mc.read_regs(1050, 3)
        except (ControllerError, OSError) as e:
            self._log(f"statistics read (1050) failed: {e}")
        if go:
            af = self.machine.iparam("AF.AFType", 0)
            self.home({5: "XYZ", 6: "XYW"}.get(af, "XY"))
        return go

    def _zf_type(self) -> int:
        return self.machine.iparam("ZF.ZFType", 0)

    def _laser_type(self) -> int:
        """SP.m_iEnableLaserType (cfg +0x4728): 0 fiber, 1 CO2, 2 blue."""
        lt = getattr(self, "laser_type", None)
        return int(lt) if lt is not None else self.machine.iparam("SP.m_iEnableLaserType", 0)

    def _axis_number(self, axis: str) -> Optional[int]:
        """Panel axis index MainApp passes to the jog (FUN_005bdae0 param 1: 0 X, 1 Y)."""
        return AXES.get(axis.upper())

    def _card_axis(self, a: int) -> int:
        """Card axis of panel axis a (cfg +0xbbd4 / +0xbbd8; X 0, Y 1 by default)."""
        return a

    def _jog_distance(self, a: int, direction: int, length: Optional[float]) -> float:
        """Jog distance in mm, FUN_005bdae0 (O): ``length`` (MC.StepLength in step mode) or the
        axis stroke MAC.SoftLimitMaxLen (cfg +0xbc78 + 0xa0*axis; + MP.SecondSoftLimitOffsetX/Y
        when MP.CO2EnableSecondSoftLimit and the laser type is CO2), signed by the direction.
        With MS.EnableSoftLimit the distance is clamped to the soft limits [offset, offset +
        stroke] (axis offset cfg +0xbcd8, 0 in the rebuild)."""
        m = self.machine
        ca = self._card_axis(a)
        sfx = "" if ca == 0 else f"_{ca}"
        stroke = m.fparam(f"MAC{sfx}.SoftLimitMaxLen", 1000.0)
        if m.param("MP.CO2EnableSecondSoftLimit", False) and self._laser_type() == 1 and ca in (0, 1):
            stroke += m.fparam("MP.SecondSoftLimitOffsetX" if ca == 0 else "MP.SecondSoftLimitOffsetY", 0.0)
        d = stroke if length is None else float(length)
        if not m.param("MS.EnableSoftLimit", False):
            return d if direction > 0 else -d
        pos = self._current_xy()[a] if a in (0, 1) else 0.0
        if direction > 0:
            room = stroke - pos
            return max(0.0, min(d, room))
        room = 0.0 - pos
        return min(0.0, max(-d, room))

    def _jog(self, axis: str, direction: int, fast: bool, length: Optional[float], what: str) -> None:
        """MainApp FUN_005bdae0 -> CNCModule +0x4c (FUN_10032b80), O (asm 0x5bdfb7..0x5be101):
        ``[3, axis, ftol(ppm*v), acc, 10*acc, ftol(ppm*d)]`` with
        v = MC.JogFastSpeed (fast mode, cfg +0x4802) or MC.JogSlowSpeed,
        acc = ftol(MC.XFastMoveAcc), times 0.1 (DAT_0083d520) and truncated again for the slow
        jog -- not scaled by ppm -- and d from :meth:`_jog_distance`."""
        if not self._ok() or self._busy():
            return
        a = self._axis_number(axis)
        if a is None:
            return
        if a >= 2 and not self._zf_type():
            # Z+/Z- (OnZPlusBtn/OnZSubBtn FUN_005ae710) need the follower (ZF.ZFType)
            self._log(f"{what} {axis}: no follower configured (ZF.ZFType = 0)")
            return
        m = self.machine
        p = self.position_ppm
        v = m.jog_fast_speed if fast else m.jog_speed
        acc = F.ftol(m.move_acc)
        if not fast:
            acc = F.ftol(acc * 0.1)
        d = self._jog_distance(a, direction, length)
        if d == 0:
            return
        self._safe(what, self.mc.jog, a, F.ftol(p * v), acc, F.ftol(p * d))
        if length is None:
            self._s.mode = "jog"

    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None:
        """Panel jog button pressed (continuous): jog over the axis stroke; jog_stop stops it."""
        self._jog(axis, direction, fast, None, "jog")

    def jog_step(self, axis: str, direction: int, step_mm: float, fast: bool = False) -> None:
        """Panel jog button in step mode (MC.IsStepMove): the same command over ``step_mm``
        (MC.StepLength / the panel's step box); the release sends nothing (FUN_005ad9f0)."""
        self._jog(axis, direction, fast, abs(float(step_mm)), "jog step")

    def jog_stop(self, axis: Optional[str] = None) -> None:
        """Jog button released (WM_LBUTTONUP, FUN_005ad9f0 -> CManuPanel::jogStop FUN_005cd960):
        CNCModule +0x58 = "send stop manu cmd" (FUN_10046420, VM event 10) with no program
        running -> :meth:`_vm_stop_jog`."""
        if self.mc is None or self._busy():
            return
        self._vm_stop_jog()

    def _vm_stop_jog(self) -> None:
        """VM thread FUN_100571f0 event 10 without a program ("vm stop jog", O):

        * FUN_10054900: ``[1, 0x1f, 2, decel, 200000]``, decel = FUN_10038de0 =
          ftol(clamp(MP.JogStopDccFactor * MC.XFastMoveAcc, 2000, FCP.MaxAcc)), unscaled;
        * ``[103, ZFUpSpeed*10, ZFDockHeight*1000]`` when the follower state is 0x75 (the rebuild
          does not track that state: not sent);
        * ``[118, 5, 0]`` when VM +0x38 is set (a program ran, FUN_10055840 event 9) and the
          laser type is fiber; VM +0x70 (FUN_100390c0) drops it without a follower;
        * state 0x19, and once the axes are idle (FUN_10058030): FUN_100518b0 103 := 3 and
          FUN_10051bf0 103 := 1.
        """
        self._safe("stop", self.mc.stop_motion, self._decel_counts())
        if self._program_ran and self._laser_type() == 0 and self._zf_type():
            self._safe("stop", self.mc.command, [F.OP_FOLLOW, 5, 0])
        self._wait_axes_idle(2.0)
        self._safe("stop", self.mc.fifo_stop)
        self._safe("stop", self.mc.fifo_clear)

    def _wait_axes_idle(self, timeout_s: float) -> None:
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            try:
                st = self.mc.read_status()
            except (ControllerError, OSError):
                return
            if st.machine_state() in (0, 4):
                return
            time.sleep(0.01)

    def move_to(self, x: float, y: float) -> None:
        """mp111/mp112 Position-to (FUN_005c2790, O): relative move to (x, y) from the cached head
        position at v = MC.JogFastSpeed / MC.JogSlowSpeed (fast mode) and acc =
        MC.XFastMoveAcc * 0.8 (DAT_00863330), times 0.1 for the slow mode."""
        if not self._ok() or self._busy():
            return
        m = self.machine
        fast = bool(m.param("MC.IsFastMode", False))
        acc = m.move_acc * 0.8 * (1.0 if fast else 0.1)
        cx, cy = self._current_xy()
        self.move_by(x - cx, y - cy, m.jog_fast_speed if fast else m.jog_speed, acc)

    def _current_xy(self) -> tuple[float, float]:
        """Fresh X/Y in mm (axis RO k2 / ppm, NC +0x1ac FUN_1002b900)."""
        try:
            st = self.mc.read_status()
            p = st.pulses_per_mm or self.position_ppm
            return st.axes[AXES["X"]].position / p, st.axes[AXES["Y"]].position / p
        except Exception:  # noqa: BLE001
            return self._s.x, self._s.y

    def move_by(self, dx: float, dy: float, speed: Optional[float] = None,
                acc: Optional[float] = None) -> None:
        """Relative X/Y move ``[5, 3, ftol(v*ppm), ftol(acc), 10*ftol(acc), ftol(dx*ppm),
        ftol(dy*ppm), 0, 0]`` (panel step 9 -> FUN_005bea00 -> CNCModule +0x50 FUN_10032910).

        O (MainApp FUN_005ca160 / FUN_005c97f0 / FUN_005bc220 asm): the speed and the distances
        are scaled by ppm (cfg +0xbc28) and truncated (__ftol2); the acceleration word is
        ftol(acc) **unscaled**.  Defaults: MC.XFastMoveSpeed, MC.XFastMoveAcc."""
        if not self._ok() or self._busy():
            return
        p = self.position_ppm
        sp = self.machine.move_speed if speed is None else speed
        ac = self.machine.move_acc if acc is None else acc
        self._safe("move", self.mc.move, 0b11, F.ftol(sp * p), F.ftol(ac),
                   [F.ftol(dx * p), F.ftol(dy * p)])

    def return_zero(self, confirm: Optional[Callable[[str], bool]] = None,
                    target: Optional[tuple[float, float]] = None) -> Optional[tuple[float, float]]:
        """mp29 回零位 "Home" (FUN_005bc220, O):

        * tool coordinate system 1..6 while X|Y are not both homed (NC +0x224 < 3): the prompt
          (mp133, DAT_008c3a64) and, on OK, sysGoOrigin (FUN_005b5de0, mf156, ``[2, 3, 0]``);
        * otherwise move to the target FUN_00469890 = the first point of the panel's job list
          (``target``) or (0, 0).  For (0, 0) (|t| < 0.001, FUN_00473c10) the mp79 prompt
          "Axis will go origin, continue?" comes first and the speed is 100 mm/s
          (DAT_008341f0); another target goes without prompt at MC.XFastMoveSpeed.  The move:
          panel step 9 with acc MC.XFastMoveAcc and Δ = target − cached head position.
          (MP.IsZFGoOriginAfterDone with a follower homes ZF first: steps 0x12 / 2-5; with
          only a follower: 0x17 / 2-7.  Not reproduced: no follower in the evidence.)"""
        if not self._ok() or self._busy():
            return None
        ask = confirm or (lambda _k: True)
        kind = getattr(self.coords, "kind", 0)
        if 1 <= kind <= 6 and (self._homed_mask & 3) != 3:
            if ask("mp133") and ask("mf156"):
                self.home("XY")
            return None
        t = (0.0, 0.0) if target is None else (float(target[0]), float(target[1]))
        zero = abs(t[0]) < 0.001 and abs(t[1]) < 0.001
        if zero and not ask("mp79"):
            return None
        cx, cy = self._s.x, self._s.y
        self.move_by(t[0] - cx, t[1] - cy, 100.0 if zero else self.machine.move_speed,
                     self.machine.move_acc)
        self._log(f"mp29: move to ({t[0]:.3f}, {t[1]:.3f})")
        return t

    # ------------------------------------------------------------------ laser helpers (O)
    def _out(self, port: int, on: bool) -> None:
        """CNCModule +0x13c (FUN_10030620): one output; ports < 1 send nothing."""
        if port and port >= 1:
            self._safe("output", self.mc.set_output, int(port), bool(on))

    def _gate_off(self) -> None:
        """CNCModule +0x164 (FUN_1002db10): laser gate/emission off.  CO2 (type 1) with
        LGP.CO2LaserControlType != 0: DA off (LGP.CO2LaserDAPort) and LGP.CO2DOLaserGate off;
        fiber: by LGP.LaserControlType (2 with LGP.LaserType 1: LGP.DOLaserGate off + laser
        protocol; 3: LGP.DOLaserGate off and the DA off unless MP.LaserDAKeepOutput; 1/4 laser
        protocol only).  Clears the gate flag (+0x4a51 / +0x4a52)."""
        m = self.machine
        if self._laser_type() == 1:
            if m.iparam("LGP.CO2LaserControlType", 2):
                self._da_off(m.iparam("LGP.CO2LaserDAPort", 0))
                self._out(m.iparam("LGP.CO2DOLaserGate", 0), False)
        elif self._laser_type() == 0:
            ct = m.iparam("LGP.LaserControlType", 0)
            if ct == 2 and m.iparam("LGP.LaserType", 0) == 1:
                self._out(m.iparam("LGP.DOLaserGate", 0), False)
            elif ct == 3:
                self._out(m.iparam("LGP.DOLaserGate", 0), False)
                if not m.param("MP.LaserDAKeepOutput", True):
                    self._da_off(m.iparam("LGP.LaserDAPort", 0))
        self._gate_open = False

    def _da_off(self, port: int) -> None:
        if port in (1, 2):
            self._safe("DA", self.mc.set_da, port, 0)

    def _red_light_off(self) -> None:
        """CNCModule +0x178 (FUN_1002ddb0): red light of the current laser type off
        (CO2: LGP.CO2DORedLight, fiber: LGP.DORedLight)."""
        m = self.machine
        lt = self._laser_type()
        if lt == 1:
            self._out(m.iparam("LGP.CO2DORedLight", 0), False)
        elif lt == 0:
            self._out(m.iparam("LGP.DORedLight", 0), False)
        with self._slock:
            self._s.outputs["red_light"] = False

    def laser_off(self) -> None:
        """CNCModule +0x170 (FUN_10030f70, O): laser emission off.  CO2 with
        LGP.CO2LaserControlType != 0: LGP.CO2DOLaser off and its DA off; fiber with
        LGP.LaserControlType 2/3: LGP.DOLaser off; then (unless a timed point shot is running,
        DAT_100b4f50) PWM off ``[9999, 3, LC.PtLaserFreq, 0, 0]`` and ``[9999, 17, ...]``."""
        if self.mc is None:
            return
        m = self.machine
        lt = self._laser_type()
        if lt == 1:
            if m.iparam("LGP.CO2LaserControlType", 2):
                self._out(m.iparam("LGP.CO2DOLaser", 0), False)
                self._da_off(m.iparam("LGP.CO2LaserDAPort", 0))
        elif lt == 0 and m.iparam("LGP.LaserControlType", 0) in (2, 3):
            if self._timed_shot:
                return
            self._out(m.iparam("LGP.DOLaser", 0), False)
        if not self._timed_shot:
            f = int(m.pt_laser_freq)
            self._safe("laser off", self.mc.command, F.mailbox_sys(F.SYS_PWM, f, 0, 0))
            self._safe("laser off", self.mc.command, F.mailbox_sys(F.SYS_PWM_BUS, f, 0, 0))
        with self._slock:
            self._s.outputs["laser"] = False

    def laser_press(self) -> None:
        """激光 (mp51) pressed: FUN_005b05c0 -> CNCModule +0x168(1, 0) = FUN_10035530 (O).

        Nothing unless the gate is open (cfg +0x4a51 fiber / +0x4a52 CO2, set by the shutter
        output) and LC.PtLaserFreq > 0 (fiber).  Fiber: ``[9999, 3, PtLaserFreq,
        MC.LaserPointPower, 1]``, the LGP.LaserControlType outputs (3: DA LGP.LaserDAPort :=
        LC.PtLaserPeakCurrent, LGP.DOLaser on); then with MC.PtLaserTime_ms == 0 the PWM on
        again (laser stays on until the release), with MC.PtLaserTime_ms > 0 a timed point
        shot program through the FIFO (I: the rebuild's :meth:`laser_test` of that duration).
        CO2 (LGP.CO2LaserControlType 1/2): DA, ``[9999, 3|17, freq, power, 1]``,
        LGP.CO2DOLaser on, same timing rule."""
        if not self._ok() or self._busy() or self._laser_on:
            return
        if not self.laser_enabled:
            self._log("laser refused: laser output is disabled (dry-run mode)")
            return
        m = self.machine
        lt = self._laser_type()
        freq, power = int(m.pt_laser_freq), m.iparam("MC.LaserPointPower", 50)
        t_ms = m.iparam("MC.PtLaserTime_ms", 0)
        self._laser_on = True
        if not self._gate_open:
            return
        if lt == 1:
            ct = m.iparam("LGP.CO2LaserControlType", 2)
            if not ct:
                return
            sub = F.SYS_PWM_BUS if ct == 2 else F.SYS_PWM
            self._safe("laser", self.mc.command, F.mailbox_sys(sub, freq, power, 1))
            self._out(m.iparam("LGP.CO2DOLaser", 0), True)
        elif lt == 0 and freq > 0:
            self._safe("laser", self.mc.command, F.mailbox_sys(F.SYS_PWM, freq, power, 1))
            if m.iparam("LGP.LaserControlType", 0) == 3:
                port = m.iparam("LGP.LaserDAPort", 0)
                if port in (1, 2):
                    self._safe("DA", self.mc.set_da, port, m.iparam("LC.PtLaserPeakCurrent", 100) * 100)
                self._out(m.iparam("LGP.DOLaser", 0), True)
            if t_ms <= 0:
                self._safe("laser", self.mc.command, F.mailbox_sys(F.SYS_PWM, freq, power, 1))
        else:
            return
        with self._slock:
            self._s.outputs["laser"] = True
        if t_ms > 0:
            self._timed_shot = True
            self.laser_test(t_ms / 1000.0)
            self._timed_shot = False

    def laser_release(self) -> None:
        """激光 (mp51) released (FUN_005ad9f0 WM_LBUTTONUP -> FUN_005b0630(MC.PtLaserTime_ms != 0),
        O): with MC.PtLaserTime_ms == 0 CNCModule +0x170 = :meth:`laser_off`; with a timed shot
        nothing (the program ends the pulse)."""
        if not self._laser_on:
            return
        self._laser_on = False
        if self.machine.iparam("MC.PtLaserTime_ms", 0) == 0 and self.mc is not None:
            self.laser_off()

    # ------------------------------------------------------------------ IO
    def set_output(self, name: str, on: bool) -> None:
        if name not in self._s.outputs:
            raise KeyError(name)
        if not self._ok():
            return
        if name == "laser":
            # mp51 is a push button in the original: press = laser on, release = laser off
            (self.laser_press if on else self.laser_release)()
            return
        if name == "follow":
            # [118, 4, on, value] follower on/off via the MC (FUN_10041740, INFERRED)
            self._safe("follow", self.mc.command, [F.OP_FOLLOW, 4, int(bool(on)), 0])
            self._s.outputs["follow"] = bool(on)
            self._log("mp41: follower command sent ([118,4,...], INFERRED)")
            return
        n = self.output_map.get(name)
        if not n:
            self._log(f"{name}: no output number configured")
            return
        self._safe(f"output {name}", self.mc.set_output, int(n), bool(on))
        self._s.outputs[name] = bool(on)
        if name == "shutter":
            self._gate_open = bool(on)       # FUN_10035530 needs the gate flag +0x4a51
        self._log(f"output {name} (DO{n}) = {'on' if on else 'off'}")

    def set_gas_type(self, gas_type: int) -> None:
        super().set_gas_type(gas_type)
        self._log(f"gas type {gas_type}: the valve is switched by the job's process items")

    def set_laser_power(self, pct: float) -> None:
        super().set_laser_power(pct)
        if self.mc is not None and self._s.connected:
            self._safe("laser power", self.mc.set_da, 1, int(round(pct * 100)))

    def laser_test(self, duration_s: float = 0.1, power_pct: Optional[float] = None) -> None:
        """mp51 'Laser': fire the beam in place for ``duration_s`` (zero-motion FIFO steps with the
        laser byte set).  INFERRED: the laser gate is the step item's laser byte."""
        if not self._ok() or self._busy():
            return
        if not self.laser_enabled:
            self._log("laser test refused: laser output is disabled (dry-run mode)")
            return
        from ..toolpath import items as I
        from ..nc import compile_items
        from ..model import Document, LayerParams
        pct = self._s.laser_power_pct if power_pct is None else power_pct
        doc = Document(layers={1: LayerParams(1, power_pct=pct)})
        items = [I.LayerChange(1), I.LaserOn(1), I.Delay(duration_s), I.LaserOff()]
        job = compile_items(items, self.machine, (self._s.x, self._s.y), doc)
        self._run_job(job, "process", f"laser test {duration_s * 1000:.0f} ms at {pct:.0f} %",
                      track=False)

    # ------------------------------------------------------------------ jobs
    def _compile(self, document, selected_only: bool, dry_run: bool):
        from ..model import Document
        from ..nc import compile_job
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
        from ..model import Document
        head = (self._s.x, self._s.y)
        off = self.coords.job_offset(document.bbox(), head)
        extra = dict(document.extra)
        extra["job_offset"] = off
        if extra.get("rotated") and extra.get("rotate_center_is_origin", True):
            extra["rotate_center"] = self.coords.job_origin(head)
        return Document(parts=document.parts, layers=document.layers, version=document.version,
                        extra=extra)

    def start(self, document, selected_only: bool = False) -> None:
        if self.breakpoint is not None and not self._busy():
            self.resume()
            return
        self._start_doc(document, selected_only, False, "process")

    def dry_run(self, document) -> None:
        self._start_doc(document, False, True, "dry_run")

    def _start_doc(self, document, selected_only, dry, mode) -> None:
        if not self._ok():
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
        self.document = document
        for fn in list(self.job_start_hooks):
            try:
                fn(document)
            except Exception as e:  # noqa: BLE001 - a hook must not stop the job
                self._log(f"job start hook failed: {e}")
        try:
            job = self._compile(document, selected_only, dry)
        except Exception as e:  # noqa: BLE001 - surface CAM errors to the user
            self._log(f"compile failed: {e}")
            return
        if not job.wire:
            self._log("mp74: nothing to cut")
            return
        if job.dry_run and mode == "process":
            self._log("laser disabled: running the job as a dry run")
        self._run_job(job, mode, job.summary())

    def frame(self, document) -> None:
        """mp58 Frame: trace the job bounding box with the laser off."""
        if not self._ok() or self._busy():
            return
        if hasattr(document, "bbox"):
            from ..nc.compiler import placed_document
            bb = placed_document(self.place(document)).bbox()
        else:
            bb = (0, 0, 0, 0)
        if bb == (0, 0, 0, 0):
            self._log("mp78: graphic data is empty, cannot go frame")
            return
        from ..nc import compile_items, frame_items
        job = compile_items(frame_items(bb, self.machine.frame_speed), self.machine,
                            (self._s.x, self._s.y), dry_run=True)
        self._run_job(job, "frame", f"frame {bb[2] - bb[0]:.1f} x {bb[3] - bb[1]:.1f} mm", track=False)

    def _make_streamer(self):
        from .stream import JobStreamer, ProcessStreamer
        if self._use_process():
            return ProcessStreamer(self.cfg, self.position_ppm)
        return JobStreamer(self.mc)

    def _run_job(self, job, mode: str, what: str, track: bool = True, base_job=None,
                 offset: int = 0, after: Optional[Callable[[bool], None]] = None) -> None:
        """Stream ``job`` in a worker thread.  ``base_job``/``offset`` map the streaming wire index
        to the breakpoint job (resume jobs); ``track=False`` for frame/laser-test/aux moves."""
        if base_job is None and track:
            self.job = job
        self._bp_job = (base_job or job) if track else None
        self._run_job_obj = job
        self._run_offset = offset
        self._exec_index = -1
        self._program_ran = True             # VM +0x38 (FUN_10055840, event 9 "start manu")
        self._cancel = threading.Event()
        self._paused = False
        self._job_mode = mode
        with self._slock:
            self._s.mode, self._s.progress, self._s.job_index = mode, 0.0, -1
            self._s.outputs["laser"] = False     # updated from the executed step's laser byte
        self._log(f"{mode}: {what}")
        streamer = self._make_streamer()
        self._streamer = streamer
        if hasattr(streamer, "on_event"):
            streamer.on_event = self._stream_event
        markers = (base_job or job).markers

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

        def work():
            t0 = time.monotonic()
            ok = False
            try:
                streamer.run_job(job, progress, self._cancel, lambda: self._paused)
                if self._cancel.is_set():
                    self._log(f"{mode} stopped")
                else:
                    ok = True
                    self._log(f"done: {mode} in {time.monotonic() - t0:.1f} s")
                    if track:
                        self.breakpoint = None
            except (ControllerError, OSError) as e:
                self._log(f"{mode} aborted: {e}")
                try:
                    self.mc.fifo_stop()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._exec_index = getattr(streamer, "exec_index", self._exec_index)
                b = getattr(streamer, "buf", None)
                self.stream_stats = b if isinstance(b, dict) else (b.__dict__.copy() if b else {})
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

        self._worker = threading.Thread(target=work, name="mlaser-job", daemon=True)
        self._worker.start()

    def _stream_event(self, kind, *a) -> None:
        if kind == "underrun":
            self._log(f"FIFO watchdog: only {a[1]:.0f} ms of motion buffered at wire {a[0]} - "
                      "laser held off and FIFO stopped until the buffer refills")
        elif kind == "resumed":
            self._log(f"FIFO watchdog: buffer refilled ({a[1]:.0f} ms), stream restarted")

    def wait_job(self, timeout: float = 60.0) -> bool:
        """Block until the running job's worker ends (tests / scripting)."""
        if self._worker is None:
            return True
        self._worker.join(timeout)
        return not self._worker.is_alive()

    # ------------------------------------------------------------------ pause / resume / stop
    def pause(self) -> None:
        """Pause.  ``pause_mode="breakpoint"`` (original, MainApp FUN_005b9a60): stop the program
        ("vm stop manu": decelerated stop, PWM and process outputs off, follower stop), keep the
        executed wire index as the breakpoint and write AutosaveParam.  ``"fifo"``: 103 := 3 and
        keep streaming (INFERRED phase-1 behaviour, exact continuation)."""
        if not self._busy() or self._paused:
            return
        if self.pause_mode == "fifo":
            self._paused = True
            self._safe("pause", self.mc.fifo_stop)
            self._s.mode = "pause"
            self._log("paused (FIFO stop)")
            return
        bp_job, offset = self._bp_job, self._run_offset
        self._paused = True
        self._safe("pause", self.mc.stop_manu, self._decel_counts(), int(self.machine.pt_laser_freq),
                   self._process_outputs_mask())
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=10.0)
        if bp_job is None:                    # frame / laser test: a plain stop
            self._paused = False
            self._safe("stop", self.mc.fifo_clear)
            self._s.mode = "ready"
            self._log("stopped")
            return
        idx = max(0, min(len(bp_job.wire), self._exec_index + offset))
        self._set_breakpoint(bp_job, idx, "send pause manu cmd")

    def _set_breakpoint(self, job, idx: int, why: str) -> None:
        from ..nc.resume import Breakpoint, BreakpointStore, position_mm
        self._safe("pause", self.mc.fifo_clear)
        x, y = position_mm(job, idx)
        self.breakpoint = Breakpoint(job, idx, x, y)
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
        self._emit("breakpointChanged", self.breakpoint)

    def resume(self, mode: str = "exact") -> None:
        """Continue: from the breakpoint (re-run the contour's pierce block there, laser-off
        rapid from the current position), or release the FIFO hold in ``fifo`` mode."""
        if self._busy() and self._paused:
            self._paused = False
            self._safe("resume", self.mc.fifo_start)
            self._s.mode = self._job_mode or "process"
            self._log("resumed")
            return
        bp = self.breakpoint
        if bp is None or self._busy() or not self._ok():
            return
        if self.active_alarms:
            self._log("cannot resume while alarms are active (Clear Alarm first)")
            return
        from ..nc.resume import resume_job
        rj = resume_job(bp.job, bp.index, (self._s.x, self._s.y), mode=mode, doc=self.document)
        self.breakpoint = None
        self._emit("breakpointChanged", None)
        self._run_job(rj, bp.mode, f"resume from wire {bp.index}", base_job=bp.job,
                      offset=-rj.resume_offset)

    def resume_saved(self, document, autosave_dir: Optional[str] = None, mode: str = "contour") -> bool:
        """BreakCon after a restart: compile ``document`` and continue from the newest
        AutosaveParam record (rebuild index convention, see mlaser.nc.resume)."""
        from ..nc.resume import Breakpoint, BreakpointStore, position_mm
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
        self.breakpoint = Breakpoint(job, idx, x, y)
        self.resume(mode)
        return True

    def stop(self) -> None:
        """停止 (mp56, CManuPanel::OnStopBtn FUN_005ba0d0, O).

        * Job running: cancel streaming, then the program stop ("vm stop manu": decelerated stop,
          PWM off ``[9999, 3|17, LC.PtLaserFreq, 0, 0]``, process outputs off, follower stop),
          FIFO stop and clear.
        * No job (job state 0): CNCModule +0xe8 ``[101]`` (follower cancel, FUN_1002c460) when
          the follower link is up (NC +0x34; the on-board follower shares the card link), then
          CNCModule +0x58 with no program on the card = "vm stop jog" (:meth:`_vm_stop_jog`:
          ``[1, 0x1f, 2, decel, 200000]``, 103 := 3, 103 := 1).  A job paused at a breakpoint
          (state 11) skips the ``[101]``.
        """
        running = self._busy()
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=10.0)
        if self.mc is not None:
            if running:
                self._safe("stop", self.mc.stop_manu, self._decel_counts(), int(self.machine.pt_laser_freq),
                           self._process_outputs_mask())
                self._safe("stop", self.mc.fifo_stop)
                self._safe("stop", self.mc.fifo_clear)
            else:
                if self.breakpoint is None and self._s.connected:
                    self._safe("stop", self.mc.command, [CANCEL_CMD])
                self._vm_stop_jog()
        self._paused = False
        self.breakpoint = None
        self._emit("breakpointChanged", None)
        with self._slock:
            self._s.outputs["laser"] = False
            self._s.mode = "ready" if self._s.connected else "offline"
        self._log("stopped")

    def _along(self, distance: float) -> None:
        bp = self.breakpoint
        if bp is None:
            self._log("mp61/mp62: Backward/Forward only while a job is paused")
            return
        if self._busy() or not self._ok():
            return
        from ..nc.resume import Breakpoint, along_path
        seg, new = along_path(bp.job, bp.index, distance)
        if not seg.wire:
            self._log("Backward/Forward: at the end of the path")
            return
        keep = bp

        def after(ok: bool) -> None:
            idx = new if ok else keep.index
            x, y = seg.end_mm if ok else (keep.x, keep.y)
            self.breakpoint = Breakpoint(keep.job, idx, x, y, keep.mode)
            with self._slock:
                self._s.mode = "pause"
            self._emit("breakpointChanged", self.breakpoint)

        self.breakpoint = None
        self._run_job(seg, "pause", f"{'backward' if distance < 0 else 'forward'} "
                      f"{abs(distance):.2f} mm along the path (laser off)", track=False, after=after)

    def step_backward(self, length_mm: float) -> None:
        """mp61 Backward along the path (laser off) by ``length_mm`` (mp65) while paused."""
        self._along(-abs(float(length_mm)))

    def step_forward(self, length_mm: float) -> None:
        """mp62 Forward along the path (laser off) while paused."""
        self._along(abs(float(length_mm)))

    def clear_alarms(self) -> None:
        """gp90 Clear Alarm: ``[9999, 5, 0, 0]`` (FUN_100315f0) + local list."""
        if self.mc is not None and self._s.connected:
            self._safe("clear alarm", self.mc.reset_alarms)
        self._s.alarms.clear()
        self._log("mf21: alarms cleared ([9999, 5, 0, 0])")

    def set_origin(self) -> None:
        """[9999, 8, 0] zero/reset coordinates (FUN_100314b0, INFERRED)."""
        if self._ok() and not self._busy():
            self._safe("set origin", self.mc.zero_position, 0)
            self._log("mf514: position zeroed ([9999,8,0], INFERRED)")

    def go_origin_point(self) -> None:
        self.move_to(0.0, 0.0)

    def snapshot(self) -> MachineState:
        """Thread-safe copy of the current state."""
        with self._slock:
            return copy.deepcopy(self._s)
