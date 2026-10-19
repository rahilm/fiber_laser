"""Simulator rigs for a procedure's dry run (and the tests).

A dry run never touches the real machine: the wizard builds a private
:class:`~mlaser.controller.ui_adapter.UiController` connected to an in-process card simulator
(``controller.simulator``) that models what the procedure needs: a rotated sheet or a disc for
the edge seek, the on-board height follower (with a calibration that finishes), an autofocus
axis.  The rig is closed (simulator stopped) when the wizard closes.
"""
from __future__ import annotations

import time
from typing import Any, Optional

#: the sheet a dry-run edge seek finds: 1000 x 600 mm, corner (100, 50), rotated 1.5°
DEMO_SHEET = (100.0, 50.0, 1000.0, 600.0, 1.5)
DEMO_DISC = (300.0, 200.0, 80.0)


class TimedCalibration:
    """Follower model mix-in: a started head calibration finishes after ``seconds``."""

    def __init__(self, model: Any, seconds: float = 0.6) -> None:
        self._m = model
        self.seconds = seconds
        self._t0: Optional[float] = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._m, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_m", "seconds", "_t0"):
            object.__setattr__(self, name, value)
        elif name == "calibrating":
            object.__setattr__(self, "_t0", time.monotonic() if value else None)
            self._m.calibrating = bool(value)
        else:
            setattr(self._m, name, value)

    @property
    def calibrating(self) -> bool:
        if self._t0 is not None and time.monotonic() - self._t0 >= self.seconds:
            self._m.calibrating = False
            self._m.calibrated = True
            object.__setattr__(self, "_t0", None)
        return bool(self._m.calibrating)

    @property
    def calibrated(self) -> bool:
        _ = self.calibrating
        return bool(self._m.calibrated)


def simulator_controller(kind: str = "card", time_scale: float = 20.0, machine: Any = None,
                         sheet: Any = None, calibration_s: float = 0.6, poll_s: float = 0.02,
                         **ctl_kw: Any):
    """A connected UiController on a private simulator.

    ``kind``: ``card`` (plain card), ``sheet`` (rotated demo sheet, or ``sheet``), ``disc``
    (demo disc), ``follower`` (on-board follower with a finishing calibration)."""
    from mlaser.controller.simulator import SimSheet
    from mlaser.controller.ui_adapter import UiController

    ctl = UiController(machine=machine, poll_s=poll_s, require_homed=False, **ctl_kw)
    if kind == "sheet" and sheet is None:
        sheet = SimSheet.rect(*DEMO_SHEET)
    elif kind == "disc" and sheet is None:
        sheet = SimSheet.disc(*DEMO_DISC)
    if kind == "follower":
        from mlaser.controller.follower_sim import make_follower_card_simulator

        m = ctl.machine
        sim = make_follower_card_simulator(period_ms=m.period_ms, time_scale=time_scale,
                                           ppm=m.pulses_per_mm_x, step_ppm=m.pulses_per_mm_x,
                                           sheet=sheet)
        sim.zf = TimedCalibration(sim.zf, calibration_s)
        sim.start()
        ctl.sim = sim
        ctl.cfg.transport = "udp"
        if not ctl.connect("127.0.0.1", sim.port):
            sim.stop()
            raise RuntimeError("simulator: connect failed")
        return ctl
    if not ctl.connect_simulator(time_scale=time_scale, sheet=sheet):
        raise RuntimeError("simulator: connect failed")
    return ctl


def close(ctl: Any) -> None:
    try:
        ctl.disconnect()
    except Exception:  # noqa: BLE001 - closing a rig must not raise
        pass


__all__ = ["simulator_controller", "close", "TimedCalibration", "DEMO_SHEET", "DEMO_DISC"]
