"""Machine operations behind the phase-3 ribbon commands (laser type, AF home, coordinate
systems, edge seek, restart, upgrade, backup, recovery), independent of Qt.

``MachineOps(ctl, machine_params=None, app_dir=None)`` works with the card controller
(:class:`~mlaser.controller.ui_adapter.UiController`, attribute ``mc``) and degrades to logging
for controllers without a card (the UI's DemoController).  Evidence is in the modules it uses
(edgeseek, coords, upgrade, nettest, formats.backup, nc.offline) and docs/protocol.md §16-§21.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable, Optional

from . import frames as F
from .coords import CoordSystems, MANUAL
from .edgeseek import EdgeSeekError, EdgeSeekParams, EdgeSeeker, EdgeSeekResult

FIBER, CO2, BLUE = 0, 1, 2
LASER_TYPE_KEYS = {FIBER: "A241024_0", CO2: "A241024_1", BLUE: "A241024_2"}
LASER_TYPE_IDS = {0x26c0: FIBER, 0x26c1: CO2, 0x26c2: BLUE}     # FUN_004b12e0


def default_app_dir() -> Path:
    """Stand-in for the program directory (``<exe>``): holds ``File\\`` (softPara.ini,
    autosave.chf, AutosaveParam*.ini, Bk*.xml) and ``File\\Temp\\``.  ``$MLASER_APP_DIR`` or
    ``~/.mlaser``."""
    return Path(os.environ.get("MLASER_APP_DIR") or (Path.home() / ".mlaser"))


class MachineOps:
    def __init__(self, ctl, values: Optional[dict] = None, app_dir: Optional[Path] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.ctl = ctl
        self.values = values if values is not None else {}
        self.app_dir = Path(app_dir) if app_dir else default_app_dir()
        self._log_fn = log
        self.coords = CoordSystems.from_values(self.values)
        self.edge = EdgeSeekParams.from_values(self.values)
        self.last_edge: Optional[EdgeSeekResult] = None
        self.memorised_angle: float = float(self.values.get("edge_angle", 0.0) or 0.0)
        self.laser_type = int(float(self.values.get("SP.m_iEnableLaserType", 0) or 0))
        self.worker: Optional[threading.Thread] = None
        self.cancel = threading.Event()
        self.marks_set: set[int] = set()     # cfg +0x4298 + n: mark n assigned
        self._set_ctl_laser_type(self.laser_type)

    # ------------------------------------------------------------------ helpers
    @property
    def file_dir(self) -> Path:
        return self.app_dir / "File"

    @property
    def temp_dir(self) -> Path:
        return self.app_dir / "File" / "Temp"

    def log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(msg)
        elif hasattr(self.ctl, "_log"):
            self.ctl._log(msg)

    @property
    def mc(self):
        return getattr(self.ctl, "mc", None)

    def connected(self) -> bool:
        st = self.ctl.state() if hasattr(self.ctl, "state") else None
        return self.mc is not None and bool(getattr(st, "connected", False))

    def require_card(self, what: str) -> bool:
        if not self.connected():
            self.log(f"{what}: needs a connected motion card (mp22 offline)")
            return False
        return True

    def busy(self) -> bool:
        b = getattr(self.ctl, "_busy", None)
        return bool(b()) if callable(b) else False

    def head(self) -> tuple[float, float]:
        st = self.ctl.state()
        return (st.x, st.y)

    def save_values(self) -> None:
        self.values.update(self.coords.to_values())
        self.values["SP.m_iEnableLaserType"] = self.laser_type
        self.values["edge_angle"] = self.memorised_angle

    def run_async(self, name: str, fn, *a, on_done: Optional[Callable] = None) -> threading.Thread:
        self.cancel.clear()

        def body():
            try:
                r = fn(*a)
                if on_done:
                    on_done(r)
            except Exception as e:  # noqa: BLE001 - report to the log
                self.log(f"{name} failed: {e}")
        t = threading.Thread(target=body, name=f"mlaser-{name}", daemon=True)
        self.worker = t
        t.start()
        return t

    # ------------------------------------------------------------------ card parameters
    def _machine(self):
        return getattr(self.ctl, "machine", None)

    def param(self, key: str, default=None):
        """A parameter value: the ops values, then the controller's Machine (with ParamDef
        defaults)."""
        if key in self.values and self.values[key] not in (None, ""):
            return self.values[key]
        m = self._machine()
        if m is not None and hasattr(m, "param"):
            return m.param(key, default)
        return default

    def _ppm(self) -> int:
        p = getattr(self.ctl, "position_ppm", None)
        try:
            return int(p) if p else 1000
        except (TypeError, ValueError):
            return 1000

    def card_params(self):
        """startup.CardParameters of the controller, or built from the parameter values."""
        cp = getattr(self.ctl, "card_params", None)
        if cp is not None:
            return cp
        from .startup import CardParameters
        vals = dict(getattr(self._machine(), "params", None) or {})
        vals.update(self.values)
        return CardParameters.from_values(vals, self._ppm())

    def write_axis_switch_words(self, enable: bool = False, which: int = 0) -> None:
        """CNCModule +0x2a4 (FUN_100316b0, O): for card axes 0..4 except the ZF axis
        (cfg +0x4ab8) set the soft-limit flags of the axis switch block and rewrite axis word 0
        (FUN_1002b190 encoding) at 50200 + 40*axis.  ``which`` 0: +0xbc76 := 1 unless
        (``enable`` and MS.EnableSoftLimit); 1: +0xbc75 := not enable; 2: both."""
        cp = self.card_params()
        flag = not (enable and bool(self.param("MS.EnableSoftLimit", False)))
        for a, ax in enumerate(cp.axes[:5]):
            if a == cp.zf_axis:
                continue
            if which in (0, 2):
                ax.flag_bc76 = flag
            if which in (1, 2):
                ax.flag_bc75 = not enable
            self.mc.write_regs(50200 + 40 * a, [ax.limits_word()])

    def soft_limit_pairs(self) -> list[tuple[int, list[int]]]:
        """CNCModule +0x198 (FUN_100341b0, O asm 0x10034230..0x100342ba): for X (cfg +0xbbd4)
        and Y (+0xbbd8; also the dual-drive slave +0xbc1c with MAC.DoubleDevice):
        v = ±MAC.SoftLimitMaxLen (+ MP.SecondSoftLimitOffsetX/Y when MP.CO2EnableSecondSoftLimit
        and the laser type is CO2), negative when MAC.GoOriginalDirection != 0;
        ``50202 + 40*axis := [ftol(min(v, -0.5)*ppm), ftol(max(v, 0.5)*ppm)]``.  The skew
        branch (cfg +0x4cd8, rotated limits) is not reproduced."""
        ppm = self._ppm()
        cp = self.card_params()
        co2_second = bool(self.param("MP.CO2EnableSecondSoftLimit", False)) and self.laser_type == 1
        out = []
        axes = [(0, "MP.SecondSoftLimitOffsetX"), (1, "MP.SecondSoftLimitOffsetY")]
        if cp.dual_drive:
            axes.append((cp.dual_slave_axis, "MP.SecondSoftLimitOffsetY"))
        for a, off_key in axes:
            ax = cp.axes[a]
            v = ax.soft_max_len + (float(self.param(off_key, 0.0) or 0.0) if co2_second else 0.0)
            if ax.go_origin_dir != 0:
                v = -v
            out.append((50202 + 40 * a, [F.ftol(min(v, -0.5) * ppm), F.ftol(max(v, 0.5) * ppm)]))
        return out

    def write_soft_limits(self) -> None:
        for reg, words in self.soft_limit_pairs():
            self.mc.write_regs(reg, words)

    # ------------------------------------------------------------------ coordinates
    def select_coord(self, kind: int) -> None:
        """mf300..mf306 / mf513 (FUN_00482950 -> FUN_004829b0, O): MSC.CoordSysType := kind.
        Selecting Manual (7) also calls CNCModule +0x2a4(0, 0) (FUN_004829b0 at 0x483e4x):
        axis word 0 of every non-ZF axis is rewritten with the soft-limit flag +0xbc76 set
        (capture: 50200/50240/50280/50360 := 131088)."""
        self.coords.select(kind)
        if hasattr(self.ctl, "coords"):
            self.ctl.coords = self.coords
        if kind == MANUAL and self.connected():
            try:
                self.write_axis_switch_words(False, 0)
            except Exception as e:  # noqa: BLE001
                self.log(f"mf513: axis words not written: {e}")
        self.save_values()

    def set_origin(self) -> Optional[tuple]:
        """mp129 SetToolOrigin (FUN_005c9d50) / Manual (FUN_005ca650, O): for Manual
        ``[9999, 8, mask(X|Y)]`` (NC +0x1a8 FUN_100314b0, mask FUN_004b0060(cfg +0xbbd0)), the
        per-axis offsets cleared, CNCModule +0x198 soft limits (:meth:`write_soft_limits`), mark
        points := 0."""
        c = self.coords
        if not c.can_set_or_return:
            self.log("mp129: not available for the float coordinate system")
            return None
        if self.busy():
            self.log("mp129: machine busy")
            return None
        if c.kind == MANUAL:
            if not self.require_card("mf513 manual origin"):
                return None
            self.mc.command(F.mailbox_sys(F.SYS_ZERO, 0b11))
            try:
                self.write_soft_limits()
            except Exception as e:  # noqa: BLE001
                self.log(f"mf513: soft limits not written: {e}")
            c.marks = [(0.0, 0.0)] * 6
            self.log("mf513: manual origin = current position ([9999, 8, 0x3])")
            self.save_values()
            return (0.0, 0.0)
        pos = self.head()
        n = c.set_tool_origin(pos)
        self.log(f"mp131: Tool Coord {n} Origin Point, ({pos[0]:.3f}, {pos[1]:.3f})")
        self.save_values()
        return pos

    def return_origin(self) -> Optional[tuple]:
        """mp130 ReturnToolOrigin (FUN_005ca160 / FUN_005ca730): relative move to the origin."""
        c = self.coords
        if not c.can_set_or_return:
            self.log("mp130: not available for the float coordinate system")
            return None
        target, speed = c.return_target()
        cur = self.head()
        if hasattr(self.ctl, "move_by"):
            self.ctl.move_by(target[0] - cur[0], target[1] - cur[1], speed, c.move_acc)
        else:
            self.ctl.move_to(*target)
        self.log(f"mp130: return to ({target[0]:.3f}, {target[1]:.3f}) at {speed:g} mm/s")
        return target

    # ------------------------------------------------------------------ mark points (O)
    @property
    def mark_index(self) -> int:
        """MC.MarkPtIndex (cfg +0x4834): the panel's 标记点1..6 selection, 0-based."""
        try:
            return int(float(self.values.get("MC.MarkPtIndex", self.param("MC.MarkPtIndex", 0)) or 0))
        except (TypeError, ValueError):
            return 0

    def set_mark_index(self, n: int) -> None:
        self.values["MC.MarkPtIndex"] = int(n)

    def set_mark(self, n: Optional[int] = None) -> Optional[tuple]:
        """标记 (mp39, CManuPanel::OnMarkPtBtn FUN_005c9390, O): when the panel is idle, mark
        point n := head position (NC +0x1a4, axis RO k2 / ppm), its "set" flag (cfg +0x4298+n)
        := 1, message mp88 "Mark P%d, (%.3f, %.3f)", MSC.MarkPtStr saved (FUN_005cd520).  No card
        traffic."""
        n = self.mark_index if n is None else int(n)
        if not 0 <= n < 6:
            return None
        if self.busy():
            self.log("mp39: machine busy")
            return None
        pos = self.head()
        self.coords.marks[n] = (float(pos[0]), float(pos[1]))
        self.marks_set.add(n)
        self.log(f"mp88: Mark P{n + 1}, ({pos[0]:.3f}, {pos[1]:.3f})")
        self.save_values()
        return self.coords.marks[n]

    def return_mark(self, n: Optional[int] = None) -> Optional[tuple]:
        """返回 (af10, CManuPanel::OnReturnMarkPtBtn FUN_005c97f0, O asm 0x5c9ac0..0x5c9ce7):
        relative move to mark n: Δ = mark − head position, speed 100 mm/s (DAT_008341f0) when the
        mark is (0, 0) within 0.001 (FUN_00473c10), else MC.XFastMoveSpeed; acc MC.XFastMoveAcc
        (panel step 9 -> ``[5, 3, ftol(v*ppm), ftol(acc), 10*ftol(acc), ftol(dx*ppm),
        ftol(dy*ppm), 0, 0]``).  MP.IsZFGoOriginAfterDone with a follower homes ZF first
        (steps 0x12, 2-5; not reproduced)."""
        n = self.mark_index if n is None else int(n)
        if not 0 <= n < 6:
            return None
        if self.busy() or not self.require_card("af10 return to mark"):
            return None
        t = self.coords.marks[n]
        zero = abs(t[0]) < 0.001 and abs(t[1]) < 0.001
        speed = 100.0 if zero else self.coords.move_speed
        cur = self.head()
        self.ctl.move_by(t[0] - cur[0], t[1] - cur[1], speed, self.coords.move_acc)
        self.log(f"af10: return to mark P{n + 1} ({t[0]:.3f}, {t[1]:.3f})")
        return t

    # ------------------------------------------------------------------ edge seek
    def edge_seeker(self) -> EdgeSeeker:
        return EdgeSeeker(self.mc, self.edge, log=self.log, cancel=self.cancel)

    def edge_seek_capacitive(self, n: Optional[int] = None, apply: Optional[Callable] = None,
                             wait: bool = False):
        """A250211_0 (FUN_005c0ad0): run the 3/4-point sequence in a worker thread; ``apply``
        receives the :class:`EdgeSeekResult` (the dialog's OK, FUN_00557d90)."""
        if not self.require_card("A250211_0 edge seek") or self.busy():
            return None
        if not self.values.get("ZF.ZFType", 1):
            self.log("A250211_0: edge seek needs the on-board follower (ZF.ZFType)")
            return None

        def done(res):
            self.last_edge = res
            self.memorised_angle = res.angle
            self.save_values()
            self.log(res.summary())
            if apply:
                apply(res)
        t = self.run_async("edge-seek", self.edge_seeker().run_capacitive, n or self.edge.mode, None, True,
                           on_done=done)
        if wait:
            t.join()
        return t

    def edge_seek_disc(self, guess=None, apply: Optional[Callable] = None, wait: bool = False):
        """newLang27 disc search (FUN_005c1ff0 -> FUN_005c2060)."""
        if not self.require_card("newLang27 disc search") or self.busy():
            return None

        def done(res):
            self.last_edge = res
            self.log(res.summary())
            if apply:
                apply(res)
        t = self.run_async("disc-search", self.edge_seeker().run_disc, guess, True, on_done=done)
        if wait:
            t.join()
        return t

    # ------------------------------------------------------------------ laser type
    def switch_laser_type(self, new: int) -> bool:
        """gp120 Laser -> Fiber / CO2 / Blue (FUN_004b12e0, O), with the old type still active:
        CNCModule +0x170 laser off (FUN_10030f70: type outputs, ``[9999, 3, LC.PtLaserFreq, 0,
        0]`` + ``[9999, 17, ...]``), +0x164 gate off (FUN_1002db10), +0x178 red light off
        (FUN_1002ddb0: LGP.DORedLight for fiber, LGP.CO2DORedLight for CO2), DA off when
        MP.LaserDAKeepOutput and LGP.LaserDAPort; then SP.m_iEnableLaserType := new and
        fiber: DA off again, ``[118, 5, 0]``, LGP.doCO2EnableOutput := 0, message A250607_0;
        CO2: ``[118, 5, 1]``, output := 1, A250607_1; ``[118, 5, x]`` goes through VM +0x70
        (FUN_100390c0), which sends it only with a follower (ZF.ZFType != 0).  Finally CNCModule
        +0x198 soft limits for the new type (:meth:`write_soft_limits`); the layer table follows
        the type (FUN_00458c60: +0x4ed8 fiber / +0x8058 CO2)."""
        if new == self.laser_type:
            return False
        if self.busy():
            self.log("gp120: cannot change the laser type while a job runs")
            return False
        old = self.laser_type
        if self.connected():
            try:
                self._set_ctl_laser_type(old)
                for name in ("laser_off", "_gate_off", "_red_light_off"):
                    fn = getattr(self.ctl, name, None)
                    if callable(fn):
                        fn()
                self._da_keep_off()
                self._set_ctl_laser_type(new)
                zf = int(float(self.param("ZF.ZFType", 0) or 0))
                port = int(float(self.param("LGP.doCO2EnableOutput", 0) or 0))
                if new == FIBER:
                    self._da_keep_off()
                if new in (FIBER, CO2):
                    if zf:
                        self.mc.command([F.OP_FOLLOW, 5, 1 if new == CO2 else 0])
                    if port >= 1:
                        self.mc.set_output(port, new == CO2)
            except Exception as e:  # noqa: BLE001
                self.log(f"gp120: card commands failed: {e}")
        self.laser_type = new
        self._set_ctl_laser_type(new)
        self.log(f"gp120: laser type -> {LASER_TYPE_KEYS.get(new, new)}"
                 f" ({'A250607_1' if new == CO2 else 'A250607_0'})")
        if self.connected():
            try:
                self.write_soft_limits()
            except Exception as e:  # noqa: BLE001
                self.log(f"gp120: soft limits not written: {e}")
        self.save_values()
        return True

    def _set_ctl_laser_type(self, t: int) -> None:
        if hasattr(self.ctl, "laser_type"):
            self.ctl.laser_type = t

    def _da_keep_off(self) -> None:
        """CNCModule +0x154 (DA) to 0 when MP.LaserDAKeepOutput and LGP.LaserDAPort (1/2)."""
        if self.param("MP.LaserDAKeepOutput", True) in (True, 1, "1", "true", "True"):
            port = int(float(self.param("LGP.LaserDAPort", 0) or 0))
            if port in (1, 2):
                self.mc.set_da(port, 0)

    # ------------------------------------------------------------------ AF home
    def home_af(self) -> bool:
        """A240827_1 electric focus go origin (0xbc4 -> CAutoFocusView::GoOriginBtn FUN_004fbeb0):
        AF type 1/2 AF reg 101 := 2; 3 EC [2, dir]; 4 IO pulse on AFDA.GoOriginPort; 5 MC
        ``[2, 0x10, 0]``; 6 MC ``[2, 1 << AF.AFAxis, 0]``."""
        t = int(float(self.values.get("AF.AFType", 0) or 0))
        if t == 0:
            self.log("A240827_1: no autofocus head configured (AF.AFType = None)")
            return False
        if not self.require_card("A240827_1 focus go origin"):
            return False
        if t in (5, 6):
            mask = 0x10 if t == 5 else 1 << int(float(self.values.get("AF.AFAxis", 4) or 4))
            self.mc.home(mask)
        elif t in (1, 2):
            from ..devices.autofocus import AutoFocus
            host = str(self.values.get("AFIP", getattr(self.mc.cfg, "host", "10.1.1.168")))
            af = AutoFocus.connect(host, int(self.values.get("AFPort", 888)))
            try:
                af.home()
            finally:
                af.close()
        elif t == 3:
            from ..devices.ethercat import EtherCATBridge
            ec = EtherCATBridge.connect(str(self.values.get("ECIP", "10.1.1.168")),
                                        int(self.values.get("ECPort", 888)))
            try:
                ec.home(int(float(self.values.get("MP.RotatePlatformType", 0) or 0)) == 1)
            finally:
                ec.close()
        elif t == 4:
            import time
            port = int(float(self.values.get("AFDA.GoOriginPort", 0) or 0))
            if not port:
                self.log("A240827_1: AFDA.GoOriginPort not configured")
                return False
            self.mc.set_output(port, False)
            self.mc.set_output(port, True)
            time.sleep(int(float(self.values.get("AFDA.FocusGoOriginDelay", 200) or 200)) / 1000)
            self.mc.set_output(port, False)
        else:
            self.log(f"A240827_1: AF type {t} not supported (VM +0x200, U)")
            return False
        self.log("A240827_1: focus go origin")
        return True

    # ------------------------------------------------------------------ hardware
    def restart(self) -> bool:
        """mf131 (FUN_004b8740): write(100, 8888)."""
        if not self.require_card("mf131 restart"):
            return False
        from .upgrade import restart_card
        restart_card(self.mc)
        self.log("mf131: card restart (100 := 8888)")
        return True
