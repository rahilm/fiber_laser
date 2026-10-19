"""Homing, jog (with the hold-to-jog dead-man), moves and return-to-zero of the UI controller."""
from __future__ import annotations

import time
from typing import Callable, Optional

from ...protocol import frames as F
from ..client import (HOME_X, HOME_XY, HOME_XY_W, HOME_XY_ZF, HOME_Y, HOME_ZF, ControllerError,
                      stop_decel)
from .connection import AXES

# facade home() argument -> homing mode (cfg +0xbba8, MainApp FUN_004af3xx command handler)
HOME_MODES = {"X": HOME_X, "Y": HOME_Y, "XY": HOME_XY, "XYZ": HOME_XY_ZF, "Z": HOME_ZF,
              "ZF": HOME_ZF, "XYW": HOME_XY_W}
CANCEL_CMD = 101       # write(101, [101]): follower cancel (CNCModule +0xe8 = FUN_1002c460)


class MotionPart:
    def _motion_ok(self, what: str) -> bool:
        """Connected, no job worker, no latched fault."""
        if not self._ok():
            return False
        if self.fsm.faulted:
            self._log(f"{what} refused: a fault is latched ({self.fsm.fault}); reset it first")
            return False
        return True

    def _decel_counts(self) -> int:
        """Stop deceleration word of ``[1, 0x1f, 2, decel, 200000]`` (FUN_10054900 ->
        FUN_10038de0, O): ftol(clamp(MP.JogStopDccFactor * MC.XFastMoveAcc, 2000, FCP.MaxAcc)),
        not scaled by ppm (capture: 4000 with the defaults)."""
        m = self.machine
        return F.ftol(stop_decel(m.move_acc, m.jog_stop_factor, m.max_acc))

    # ------------------------------------------------------------------ homing
    def home(self, axes: str = "XY") -> None:
        """Go Origin (mf15/mf16/mf17/mf18/mf222): mailbox ``[2, mask, 0]`` (FUN_10032da0).

        ``"Z"`` is the panel's Z回原 (mp30, FUN_005bec40): only with a follower (ZF.ZFType != 0,
        cfg +0x4994); without one the original sends nothing."""
        if not self._motion_ok("Go Origin"):
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
        self.sup.clear_latch("go origin")
        mask = self._safe("Go Origin", self._mc.home_mode, mode, AXES["X"], AXES["Y"], AXES["W"])
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
            self._mc.read_regs(1050, 3)
        except (ControllerError, OSError) as e:
            self._log(f"statistics read (1050) failed: {e}")
        if go:
            af = self.machine.iparam("AF.AFType", 0)
            self.home({5: "XYZ", 6: "XYW"}.get(af, "XY"))
        return go

    def homed_xy(self) -> Optional[bool]:
        """X and Y report homed (axis status bit 15), None while disconnected."""
        if not self._s.connected:
            return None
        return (self._homed_mask & 3) == 3

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

    # ------------------------------------------------------------------ jog
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
        if not self._motion_ok(what) or self._busy():
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
        self.sup.clear_latch(what)
        if length is None:
            self._jogging = axis.upper()
            self._jog_alive = time.monotonic()
        self._safe(what, self._mc.jog, a, F.ftol(p * v), acc, F.ftol(p * d))
        if length is None:
            self._s.mode = "jog"

    def jog_start(self, axis: str, direction: int, fast: bool = False) -> None:
        """Panel jog button pressed (continuous): jog over the axis stroke; jog_stop stops it.
        Hold-to-jog: the caller sends :meth:`jog_keepalive` while the button is held, otherwise
        the dead-man stops the jog after ``jog_deadman_s``."""
        self._jog(axis, direction, fast, None, "jog")

    def jog_keepalive(self) -> None:
        """The jog button is still held (UI timer, pendant reader)."""
        self._jog_alive = time.monotonic()

    def jog_step(self, axis: str, direction: int, step_mm: float, fast: bool = False) -> None:
        """Panel jog button in step mode (MC.IsStepMove): the same command over ``step_mm``
        (MC.StepLength / the panel's step box); the release sends nothing (FUN_005ad9f0)."""
        self._jog(axis, direction, fast, abs(float(step_mm)), "jog step")

    def jog_stop(self, axis: Optional[str] = None) -> None:
        """Jog button released (WM_LBUTTONUP, FUN_005ad9f0 -> CManuPanel::jogStop FUN_005cd960):
        CNCModule +0x58 = "send stop manu cmd" (FUN_10046420, VM event 10) with no program
        running -> :meth:`_vm_stop_jog`."""
        self._jogging = None
        if self._mc is None or self._busy():
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

        If the stop itself fails (link trouble) the supervisor's halt channel sends the full halt.
        """
        ok = self._try("stop", self._mc.stop_motion, self._decel_counts())
        if self._program_ran and self._laser_type() == 0 and self._zf_type():
            self._try("stop", self._mc.command, [F.OP_FOLLOW, 5, 0])
        self._wait_axes_idle(2.0)
        ok = self._try("stop", self._mc.fifo_stop) and ok
        ok = self._try("stop", self._mc.fifo_clear) and ok
        if not ok:
            self.sup.safe_halt("stop (fallback, main channel failed)")

    def _wait_axes_idle(self, timeout_s: float) -> None:
        t_end = time.time() + timeout_s
        while time.time() < t_end:
            try:
                st = self._mc.read_status()
            except (ControllerError, OSError, AttributeError):
                return
            if st.machine_state() in (0, 4):
                return
            time.sleep(0.01)

    # ------------------------------------------------------------------ moves
    def move_to(self, x: float, y: float) -> None:
        """mp111/mp112 Position-to (FUN_005c2790, O): relative move to (x, y) from the cached head
        position at v = MC.JogFastSpeed / MC.JogSlowSpeed (fast mode) and acc =
        MC.XFastMoveAcc * 0.8 (DAT_00863330), times 0.1 for the slow mode.  Pre-flight: the
        target inside the travel, homed."""
        if not self._motion_ok("move") or self._busy():
            return
        if self._refuse(self.preflight("move", target=(x, y))):
            return
        m = self.machine
        fast = bool(m.param("MC.IsFastMode", False))
        acc = m.move_acc * 0.8 * (1.0 if fast else 0.1)
        cx, cy = self._current_xy()
        self.move_by(x - cx, y - cy, m.jog_fast_speed if fast else m.jog_speed, acc)

    def _current_xy(self) -> tuple[float, float]:
        """Fresh X/Y in mm (axis RO k2 / ppm, NC +0x1ac FUN_1002b900)."""
        try:
            st = self._mc.read_status()
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
        if not self._motion_ok("move") or self._busy():
            return
        p = self.position_ppm
        sp = self.machine.move_speed if speed is None else speed
        ac = self.machine.move_acc if acc is None else acc
        self.sup.clear_latch("move")
        self._safe("move", self._mc.move, 0b11, F.ftol(sp * p), F.ftol(ac),
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
        if not self._motion_ok("mp29") or self._busy():
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
        if self._refuse(self.preflight("move", target=t)):
            return None
        cx, cy = self._s.x, self._s.y
        self.move_by(t[0] - cx, t[1] - cy, 100.0 if zero else self.machine.move_speed,
                     self.machine.move_acc)
        self._log(f"mp29: move to ({t[0]:.3f}, {t[1]:.3f})")
        return t

    def set_origin(self) -> None:
        """[9999, 8, 0] zero/reset coordinates (FUN_100314b0, INFERRED)."""
        if self._motion_ok("set origin") and not self._busy():
            self._safe("set origin", self._mc.zero_position, 0)
            self._log("mf514: position zeroed ([9999,8,0], INFERRED)")

    def go_origin_point(self) -> None:
        self.move_to(0.0, 0.0)


__all__ = ["MotionPart", "HOME_MODES", "CANCEL_CMD"]
