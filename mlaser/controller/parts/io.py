"""Outputs, laser fire / test / off, laser power and gas type of the UI controller.

Arming: switching the laser, the shutter/gate or a process gas valve **on**, a PWM duty, a DA
value and the laser source emission need the machine armed; the supervisor's guard refuses them
otherwise (every command, whoever sends it).  The checks here only give the operator a clear
message first.
"""
from __future__ import annotations

import logging
from typing import Optional

from ...protocol import frames as F


class IOPart:
    def _out(self, port: int, on: bool) -> None:
        """CNCModule +0x13c (FUN_10030620): one output; ports < 1 send nothing."""
        if port and port >= 1:
            self._safe("output", self._mc.set_output, int(port), bool(on))

    def _port_guarded(self, port: int) -> bool:
        return bool(port and 1 <= port <= 32 and self.sup.guarded_outputs >> (port - 1) & 1)

    def _guarded_outputs_mask(self) -> int:
        """Outputs that need arming to switch on: the process outputs (gas valves, laser DO), the
        shutter/gate and the laser/gate DOs of the laser parameters (never the heartbeat)."""
        m = self._process_outputs_mask()
        ports = {self.output_map.get("shutter"), self.output_map.get("gas")}
        mp = self.machine
        for k in ("LGP.DOLaserGate", "LGP.DOLaser", "LGP.CO2DOLaserGate", "LGP.CO2DOLaser"):
            try:
                ports.add(mp.iparam(k, 0))
            except Exception:  # noqa: BLE001
                pass
        for p in ports:
            if p and 1 <= p <= 32:
                m |= 1 << (p - 1)
        hb = int(getattr(self, "heartbeat_output", 0) or 0)
        if hb:
            m &= ~(1 << (hb - 1))
        return m

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

    # ------------------------------------------------------------------ laser helpers (O)
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
            self._safe("DA", self._mc.set_da, port, 0)

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
        if self._mc is None:
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
            ok = self._try("laser off", self._mc.command, F.mailbox_sys(F.SYS_PWM, f, 0, 0))
            ok = self._try("laser off", self._mc.command, F.mailbox_sys(F.SYS_PWM_BUS, f, 0, 0)) and ok
            if not ok:
                # a failed laser-off is never swallowed: halt through the halt channel + FAULT
                self.raise_fault("laser off failed (PWM off not acknowledged)")
        with self._slock:
            self._s.outputs["laser"] = False

    def _armed_for(self, what: str) -> bool:
        if not self.laser_enabled:
            self._log(f"{what} refused: laser output is disabled (dry-run mode)")
            return False
        if not self.fsm.armed:
            self._log(f"{what} refused: the machine is not armed", logging.WARNING)
            return False
        return True

    def laser_press(self) -> None:
        """激光 (mp51) pressed: FUN_005b05c0 -> CNCModule +0x168(1, 0) = FUN_10035530 (O).

        Nothing unless the gate is open (cfg +0x4a51 fiber / +0x4a52 CO2, set by the shutter
        output) and LC.PtLaserFreq > 0 (fiber).  Fiber: ``[9999, 3, PtLaserFreq,
        MC.LaserPointPower, 1]``, the LGP.LaserControlType outputs (3: DA LGP.LaserDAPort :=
        LC.PtLaserPeakCurrent, LGP.DOLaser on); then with MC.PtLaserTime_ms == 0 the PWM on
        again (laser stays on until the release), with MC.PtLaserTime_ms > 0 a timed point
        shot program through the FIFO (I: the rebuild's :meth:`laser_test` of that duration).
        CO2 (LGP.CO2LaserControlType 1/2): DA, ``[9999, 3|17, freq, power, 1]``,
        LGP.CO2DOLaser on, same timing rule.  Needs the machine armed."""
        if not self._ok() or self._busy() or self._laser_on:
            return
        if not self._armed_for("laser"):
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
            self._safe("laser", self._mc.command, F.mailbox_sys(sub, freq, power, 1))
            self._out(m.iparam("LGP.CO2DOLaser", 0), True)
        elif lt == 0 and freq > 0:
            self._safe("laser", self._mc.command, F.mailbox_sys(F.SYS_PWM, freq, power, 1))
            if m.iparam("LGP.LaserControlType", 0) == 3:
                port = m.iparam("LGP.LaserDAPort", 0)
                if port in (1, 2):
                    self._safe("DA", self._mc.set_da, port, m.iparam("LC.PtLaserPeakCurrent", 100) * 100)
                self._out(m.iparam("LGP.DOLaser", 0), True)
            if t_ms <= 0:
                self._safe("laser", self._mc.command, F.mailbox_sys(F.SYS_PWM, freq, power, 1))
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
        if self.machine.iparam("MC.PtLaserTime_ms", 0) == 0 and self._mc is not None:
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
            self._safe("follow", self._mc.command, [F.OP_FOLLOW, 4, int(bool(on)), 0])
            self._s.outputs["follow"] = bool(on)
            self._log("mp41: follower command sent ([118,4,...], INFERRED)")
            return
        n = self.output_map.get(name)
        if not n:
            self._log(f"{name}: no output number configured")
            return
        if on and self._port_guarded(int(n)) and not self._armed_for(f"output {name}"):
            return
        if not self._try(f"output {name}", self._mc.set_output, int(n), bool(on)):
            return
        self._s.outputs[name] = bool(on)
        if name == "shutter":
            self._gate_open = bool(on)       # FUN_10035530 needs the gate flag +0x4a51
        self._log(f"output {name} (DO{n}) = {'on' if on else 'off'}")

    def set_gas_type(self, gas_type: int) -> None:
        self._s.gas_type = gas_type
        self._log(f"gas type {gas_type}: the valve is switched by the job's process items")

    def set_laser_power(self, pct: float) -> None:
        """Panel laser power (mp53): DA channel 1.  Sent while armed; otherwise kept and sent on
        arming (a DA value is an emission-class command)."""
        self._s.laser_power_pct = pct
        if self._mc is not None and self._s.connected and self.fsm.armed:
            self._safe("laser power", self._mc.set_da, 1, int(round(pct * 100)))
            self._power_dirty = False
        else:
            self._power_dirty = True

    def _on_armed(self) -> None:
        if self._mc is not None and self._power_dirty:
            self._power_dirty = False
            self._safe("laser power", self._mc.set_da, 1, int(round(self._s.laser_power_pct * 100)))

    def laser_test(self, duration_s: float = 0.1, power_pct: Optional[float] = None) -> None:
        """mp51 'Laser': fire the beam in place for ``duration_s`` (zero-motion FIFO steps with the
        laser byte set).  INFERRED: the laser gate is the step item's laser byte.  Needs the
        machine armed."""
        if not self._motion_ok("laser test") or self._busy():
            return
        if not self._armed_for("laser test"):
            return
        from ...toolpath import items as I
        from ...nc import compile_items
        from ...model import Document, LayerParams
        pct = self._s.laser_power_pct if power_pct is None else power_pct
        doc = Document(layers={1: LayerParams(1, power_pct=pct)})
        items = [I.LayerChange(1), I.LaserOn(1), I.Delay(duration_s), I.LaserOff()]
        job = compile_items(items, self.machine, (self._s.x, self._s.y), doc)
        if self._refuse(self.preflight("laser_test", emits=True)):
            return
        self._run_job(job, "process", f"laser test {duration_s * 1000:.0f} ms at {pct:.0f} %",
                      track=False, emits=True)


__all__ = ["IOPart"]
