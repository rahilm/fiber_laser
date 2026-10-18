"""Line lead-in joint (parity JC-08): build_toolpath tags the lead-in moves, compile_items finds
the joint (also after the slow-start split) and hands it to plan_path, which blends it with
SmoothParams.lead_tol (0.001) instead of the corner tolerance."""
from __future__ import annotations

import pytest

from mlaser.toolpath import items as I
from mlaser.cam.toolpath import build_toolpath
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Part
from mlaser.motion import interp
from mlaser.motion.interp import MotionConfig, lead_in_index, plan_path
from mlaser.nc import Machine, compile_job
import mlaser.nc.compiler as C


def _doc(lead):
    s = (15.0, 0.0)
    circle = Contour([Arc(s, s, (0.0, 0.0), True)], True)
    return Document([Part([circle], Craft(layer=1, lead_in=lead))], {1: LayerParams(1, speed=100)})


def _captured(doc, monkeypatch):
    seen = []
    real = interp.plan_path

    def spy(moves, cfg, return_geom=False, lead_index=None):
        seen.append(lead_index)
        return real(moves, cfg, return_geom=return_geom, lead_index=lead_index)
    monkeypatch.setattr(C, "plan_path", spy)
    compile_job(doc, {"return_to_origin": True}, Machine())
    return seen


def test_line_lead_is_tagged_and_reaches_plan_path(monkeypatch):
    doc = _doc(LeadParams(1, 90.0, 3.0, 2.0))
    cuts = [it for it in build_toolpath(doc) if isinstance(it, (I.CutLine, I.CutArc))]
    assert getattr(cuts[0], "lead", None) == "in" and getattr(cuts[1], "lead", None) is None
    assert lead_in_index(cuts) == 1
    assert _captured(doc, monkeypatch) == [1]


def test_no_lead_gives_no_lead_index(monkeypatch):
    doc = _doc(LeadParams(0))
    cuts = [it for it in build_toolpath(doc) if isinstance(it, (I.CutLine, I.CutArc))]
    assert lead_in_index(cuts) is None
    assert _captured(doc, monkeypatch) == [None]


def test_arc_lead_has_no_marker():
    doc = _doc(LeadParams(2, 90.0, 3.0, 2.0))
    cuts = [it for it in build_toolpath(doc) if isinstance(it, (I.CutLine, I.CutArc))]
    assert lead_in_index(cuts) is None          # FUN_1006f2d0: marker only after a LINE lead-in


def test_lead_index_after_slow_start_split():
    moves = [I.CutLine((18, 0), (15, 0), 100), I.CutArc((15, 0), (15, 0), (0, 0), True, 100)]
    moves[0].lead = "in"
    split = C._slow_moves(moves, (2.0, 20.0, 0, 0, 0), None)
    assert len(split) == 3
    assert lead_in_index(split, 1, 3.0) == 2


def test_lead_joint_is_blended_tighter():
    moves = [I.CutLine((18, 0), (15, 0), 100), I.CutArc((15, 0), (15, 0), (0, 0), True, 100)]
    p_corner = plan_path(moves, MotionConfig())
    p_lead = plan_path(moves, MotionConfig(), lead_index=1)
    # lead_tol 0.001 < corner tol 0.05: a smaller blend and a slower joint, i.e. a longer cut
    assert p_lead.length > p_corner.length
    assert p_lead.duration > p_corner.duration


def test_small_circle_limit_rule():
    """CInterpMrg+0x18/+0x20 (MP.EnableSmallCircleSpeedLimit / SmallCircleSpeedLimitRatio):
    checked against the original under the harness (r 3 plain, r 5 with line or arc lead-in:
    applied; with a lead-out (4 primitives): not applied)."""
    lim = MotionConfig(small_circle_limit=True, small_circle_ratio=0.6)
    lead = [I.CutLine((8, 0), (5, 0), 100), I.CutArc((5, 0), (5, 0), (0, 0), True, 100)]
    assert plan_path(lead, lim, lead_index=1).duration > plan_path(lead, MotionConfig(), lead_index=1).duration
    out = lead + [I.CutLine((5, 0), (5, 3), 100)]
    assert plan_path(out, lim, lead_index=1).duration == pytest.approx(
        plan_path(out, MotionConfig(), lead_index=1).duration)
    big = [I.CutArc((7, 0), (7, 0), (0, 0), True, 100)]            # r 7 > 6: never limited
    assert plan_path(big, lim).duration == pytest.approx(plan_path(big, MotionConfig()).duration)


def test_machine_motion_config_uses_paramodule_defaults():
    cfg = Machine().motion_config()
    assert cfg.smooth is True
    assert (cfg.small_circle_limit, cfg.small_circle_ratio) == (False, 0.6)
    assert (cfg.micro_link_redrill, cfg.micro_link_decel, cfg.micro_link_vel) == (False, False, 10.0)
    assert (cfg.scan_circle_vel_ratio, cfg.scan_circle_pwm_delay, cfg.scan_line_open_pwm,
            cfg.scan_line_close_pwm) == (1.0, 0.0, -10.0, -10.0)
    m = Machine(params={"MP.EnableSmallCircleSpeedLimit": 1, "MP.SmallCircleSpeedLimitRatio": 0.3})
    assert (m.motion_config().small_circle_limit, m.motion_config().small_circle_ratio) == (True, 0.3)
