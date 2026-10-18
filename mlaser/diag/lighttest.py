"""Light test ("出光测试" newLang200, command id 0x4272, dialog FUN_00567010).

Parameters (LaserTest table, O): ``LT.ltPower`` newLang201 power %, ``LT.ltDutyfactor``
newLang202 duty %, ``LT.ltFreq`` newLang203 frequency (Hz), ``LT.ltEnableTime`` newLang204
"long light at specified time", ``LT.ltLaserTime`` newLang205 light duration (s).

Behaviour (FUN_005686e0 start, FUN_00568500 timer, FUN_005688a0 stop, O):

* Start switches the laser on through the run panel (FUN_005b0680, the same path as the panel's
  laser key);
* with ``ltEnableTime`` and a duration > 0, ``remaining = ftol(duration)`` seconds and a 1 s
  timer counts down, showing newLang206 "Remaining light time"; at 0 the laser is switched off
  (FUN_005b06f0) and the timer killed;
* without it the laser stays on until Stop / closing the dialog (FUN_005686c0 calls the stop
  routine before EndDialog);
* MainApp sets cfg+0x43a4 while the dialog is open (shared with the burn-in dialog).

The rebuild fires through the facade: ``set_laser_power`` + ``set_output("laser", ...)``; a
card controller with ``laser_test(duration, power)`` (UiController) is used for timed shots.
Duty and frequency are passed to the card PWM (``[9999, 3, freq, duty]``) when a card is
connected (I: the run panel's laser key uses the configured PWM).
"""
from __future__ import annotations

import time
from typing import Callable

from .paramset import ParamSet

KEYS = ("LT.ltPower", "LT.ltDutyfactor", "LT.ltFreq", "LT.ltEnableTime", "LT.ltLaserTime")


def params(settings=None) -> ParamSet:
    return ParamSet(KEYS, settings, overrides={"LT.ltPower": 10, "LT.ltDutyfactor": 100,
                                              "LT.ltFreq": 5000, "LT.ltLaserTime": 1.0})


class LightTest:
    def __init__(self, ctl, p: ParamSet, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.monotonic):
        self.ctl, self.p, self.log, self.clock = ctl, p, log, clock
        self.on = False
        self.remaining = 0          # whole seconds shown by newLang206
        self._t_end = 0.0

    @property
    def timed(self) -> bool:
        return bool(self.p["LT.ltEnableTime"]) and float(self.p["LT.ltLaserTime"]) > 0

    def start(self) -> bool:
        if not self.ctl.state().connected:
            self.log("gp0: hardware is not connected")
            return False
        power = float(self.p["LT.ltPower"])
        mc = getattr(self.ctl, "mc", None)
        if mc is not None:
            try:
                mc.set_pwm(int(self.p["LT.ltFreq"]), int(self.p["LT.ltDutyfactor"]))
            except Exception as e:  # noqa: BLE001
                self.log(f"light test PWM: {e}")
        if self.timed and hasattr(self.ctl, "laser_test"):
            dur = float(self.p["LT.ltLaserTime"])
            self.ctl.laser_test(dur, power)
        else:
            self.ctl.set_laser_power(power)
            try:
                self.ctl.set_output("laser", True)
            except KeyError:
                pass
        self.on = True
        if self.timed:
            self.remaining = int(float(self.p["LT.ltLaserTime"]))
            self._t_end = self.clock() + float(self.p["LT.ltLaserTime"])
        self.log(f"newLang200: laser on, {power:g} %")
        return True

    def tick(self) -> bool:
        """1 s timer of FUN_00568500; returns True while the laser is on."""
        if not self.on:
            return False
        if self.timed:
            left = self._t_end - self.clock()
            self.remaining = max(0, int(left + 0.999))
            if left <= 0:
                self.stop()
                return False
        return True

    def stop(self) -> None:
        if not self.on:
            return
        self.on = False
        self.remaining = 0
        try:
            self.ctl.set_output("laser", False)
        except KeyError:
            pass
        self.log("newLang200: laser off")
