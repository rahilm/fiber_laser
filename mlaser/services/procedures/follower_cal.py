"""Guided follower (height sensor) calibration with the calibration curve.

The on-board follower calibrates itself (``101 := 107``, MainApp FUN_0062cd50); the procedure
checks it is homed (the original refuses otherwise, zf49), starts the calibration, waits for
it, reads the curve (registers 12000.., FUN_10042cf0), grades it and saves ``calib.csv`` (the
original's ``Help/calib.csv`` format, :mod:`mlaser.devices.follower`) to mlaser's own data
directory, keeping the previous file as ``.bak``.  A dry run saves nothing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from mlaser.devices.follower import CalibrationTable, OnBoardFollower

from .engine import Procedure, Step, StepFailed

CALIBRATION_TIMEOUT_S = 180.0


def default_curve_path() -> Path:
    from mlaser.services.config import data_dir

    return data_dir() / "calib.csv"


def curve_quality(t: CalibrationTable) -> dict:
    """Simple checks of a curve: point count, raw span, monotonic raw over height."""
    pts = sorted(t.points)
    raws = [r for _, r in pts]
    mono = all(b >= a for a, b in zip(raws, raws[1:])) or all(b <= a for a, b in zip(raws, raws[1:]))
    return {"points": len(pts), "span": (max(raws) - min(raws)) if raws else 0, "monotonic": mono,
            "height_range_um": (pts[0][0], pts[-1][0]) if pts else (0, 0)}


class FollowerCalibrationProcedure(Procedure):
    id = "follower_calibration"
    title = "wizard.follower.title"
    min_mode = "engineer"

    def __init__(self, machine: Any, save_path: Optional[os.PathLike | str] = None,
                 timeout_s: float = CALIBRATION_TIMEOUT_S, **kw: Any) -> None:
        self.save_path = Path(save_path) if save_path else None
        self.timeout_s = timeout_s
        self.table: Optional[CalibrationTable] = None
        self.grade = ""
        self.saved: Optional[Path] = None
        self._started = False
        super().__init__(machine, **kw)

    def build_steps(self) -> list[Step]:
        return [
            Step("intro", "wizard.follower.intro_title", "wizard.follower.intro_text", "info",
                 safety=("wizard.follower.safety_sheet", "wizard.safety.head_moves", "wizard.safety.abort")),
            Step("check", "wizard.check_title", "wizard.follower.check_text", "action", run=_check, auto=True),
            Step("calibrate", "wizard.follower.calibrate_title", "wizard.follower.calibrate_text", "action",
                 run=_calibrate, motion=True, safety=("wizard.safety.motion",),
                 action_label="wizard.follower.calibrate_button"),
            Step("curve", "wizard.follower.curve_title", "", "action", run=_curve, auto=True),
            Step("review", "wizard.follower.review_title", "wizard.follower.review_text", "confirm"),
            Step("save", "wizard.follower.save_title", "wizard.follower.save_text", "action", run=_save,
                 action_label="wizard.follower.save_button", skip=lambda p: p.dry_run),
        ]

    def follower(self) -> OnBoardFollower:
        card = self.machine.card()
        if card is None:
            raise StepFailed("wizard.error.not_connected")
        return OnBoardFollower(card)

    def on_abort(self) -> None:
        if self._started:
            try:
                self.follower().cancel_calibration()      # 101 := 101 (FUN_0062cd50 cancel)
            except Exception:  # noqa: BLE001 - best effort after the halt
                pass


def _check(p: FollowerCalibrationProcedure) -> dict:
    p.require_connected()
    try:
        st = p.follower().status()
    except StepFailed:
        raise
    except Exception as e:  # noqa: BLE001 - a card without the follower answers an exception
        raise StepFailed("wizard.follower.error.absent", detail=str(e)) from None
    if not st.homed:
        raise StepFailed("wizard.follower.error.not_homed")
    return {"grade": st.calibration_grade}


def _calibrate(p: FollowerCalibrationProcedure) -> str:
    p.operator_action("calibrate")
    zf = p.follower()
    zf.calibrate()
    p._started = True
    p.sleep(0.2)
    p.wait(lambda: not zf.status().calibrating, p.timeout_s, "follower calibration", poll_s=0.1)
    p._started = False
    p.grade = zf.status().calibration_grade
    p.log(f"follower calibration finished, grade {p.grade}")
    return p.grade


def _curve(p: FollowerCalibrationProcedure) -> dict:
    t = p.follower().read_calibration_curve()
    if len(t.points) < 2:
        raise StepFailed("wizard.follower.error.no_curve")
    p.table = t
    q = curve_quality(t)
    q["grade"] = p.grade
    return q


def _save(p: FollowerCalibrationProcedure) -> str:
    if p.table is None:
        raise StepFailed("wizard.follower.error.no_curve")
    path = p.save_path or default_curve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            os.replace(path, bak)
        except OSError:
            pass
    p.table.save(str(path))
    p.saved = path
    p.log(f"calibration curve saved to {path}")
    return str(path)


__all__ = ["FollowerCalibrationProcedure", "curve_quality", "default_curve_path"]
