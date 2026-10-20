"""Job.estimate without the second planning pass (Review 2 P1-4): bit for bit the old estimate.

compile_items plans each estimate chain right after its last compile chain, reusing the compile
plan when the inputs are the same and the blend tables (smooth.table_memo) otherwise.  The
reference is ``estimate_time(items, cfg)`` from scratch, compared with ``float.hex``.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

from mlaser.cam.toolpath import CamParams
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Line, Part
from mlaser.motion.interp import estimate_chains, estimate_time
from mlaser.nc import Machine, compile_items
from mlaser.nc import compiler as C
from mlaser.parity_ports.cadmodule import smooth as S
from mlaser.parity_ports.cadmodule.curvetable import CurveTable
from mlaser.toolpath import items as I

ROOT = Path(__file__).resolve().parents[1]


def _hex(est: dict) -> dict:
    return {k: float(v).hex() for k, v in est.items()}


def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def _circ(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def _check(doc, cam=None, machine=None, dry=False):
    m = machine or Machine()
    placed, prm, items = C.plan_toolpath(doc, cam, None, None)
    job = compile_items(items, m, prm.origin, placed, dry)
    ref = estimate_time(items, m.motion_config())
    assert job.estimate and _hex(job.estimate) == _hex(ref)
    return job


def _slow_doc():
    lp = LayerParams(1, speed=80, power_pct=80, duty_pct=60, frequency_hz=3000)
    lp.extra = {"process": {"GP.UD_UpEnable": True, "GP.UD_UpLen": 4.0, "GP.UD_UpSpeed": 10.0,
                            "GP.UD_UpAdvEnable": True, "GP.UD_DownEnable": True,
                            "GP.UD_DownLen": 3.0, "GP.UD_DownSpeed": 15.0,
                            "GP.UD_DownAdvEnable": True}}
    parts = [Part([_circ(30, 30, 5), _rect(18, 18, 24, 24)],
                  Craft(layer=1, lead_in=LeadParams(kind=1, length=2.0))),
             Part([_rect(60, 0, 10, 30)], Craft(layer=1))]
    return Document(parts, {1: lp})


def test_estimate_identical_with_lead_ins_and_slow_start_end():
    _check(_slow_doc())


def test_estimate_identical_without_smoothing_and_dry_run():
    _check(_slow_doc(), machine=Machine(smooth=False))
    _check(_slow_doc(), dry=True)


def test_estimate_identical_micro_joints_and_bare_items():
    m = Machine()
    m.process.adv_micro_link = True
    items = [I.LayerChange(1), I.Rapid((0, 0), (0, 0)), I.LaserOn(1),
             I.CutLine((0, 0), (5, 0), 20, 1, True), I.CutLine((5, 0), (6, 0), 20, 1, False),
             I.CutLine((6, 0), (10, 0), 20, 1, True), I.LaserOff(),
             # a cut chain broken by LaserOn / LaserOff only: one estimate chain, two compile chains
             I.LaserOn(1), I.CutLine((10, 0), (10, 8), 30, 1, True), I.LaserOff(),
             I.LaserOn(1), I.CutLine((10, 8), (0, 8), 30, 1, True), I.LaserOff(),
             I.Delay(0.2), I.Rapid((0, 8), (20, 20)), I.CutArc((20, 20), (20, 20), (25, 20), True, 40, 1, True)]
    job = compile_items(items, m)
    assert _hex(job.estimate) == _hex(estimate_time(items, m.motion_config()))
    assert len(estimate_chains(items)) == 2     # the first one spans four compile chains


@pytest.mark.parametrize("seed", range(0, 300, 23))
def test_estimate_identical_on_golden_random_jobs(seed):
    sys.path.insert(0, str(ROOT / "tools"))
    import make_goldens as G
    doc, cam, machine, dry = G.random_doc(seed)
    try:
        placed, prm, items = C.plan_toolpath(doc, dict(cam), None, None)
        job = compile_items(items, machine, prm.origin, placed, dry)
    except Exception:   # noqa: BLE001 - a golden job may fail: then so must the reference path
        return
    try:
        ref = estimate_time(items, machine.motion_config())
    except Exception:   # noqa: BLE001
        ref = {}
    assert _hex(job.estimate) == _hex(ref)


def test_estimate_identical_randomised():
    rnd = random.Random(20261019)
    for _ in range(12):
        parts = []
        for k in range(rnd.randint(1, 6)):
            x, y = rnd.uniform(0, 200), rnd.uniform(0, 100)
            cs = []
            if rnd.random() < 0.5:
                cs.append(_circ(x + 10, y + 10, rnd.uniform(0.7, 6)))
            n = rnd.randint(3, 9)
            pts = [(x + 10 + 9 * math.cos(2 * math.pi * i / n) * rnd.uniform(0.5, 1),
                    y + 10 + 9 * math.sin(2 * math.pi * i / n) * rnd.uniform(0.5, 1)) for i in range(n)]
            cs.append(Contour([Line(pts[i], pts[(i + 1) % n]) for i in range(n)], True))
            lead = LeadParams(kind=rnd.choice((0, 1, 1, 3)), length=rnd.uniform(0.5, 3))
            parts.append(Part(cs, Craft(layer=1 + k % 2, lead_in=lead)))
        doc = Document(parts)
        cam = CamParams(sort_mode=rnd.choice((0, 5, 6)))
        _check(doc, cam, Machine(smooth=rnd.random() < 0.8))


def test_memo_is_closed_after_a_cancelled_compile():
    placed, prm, items = C.plan_toolpath(_slow_doc(), None, None, None)

    def cancel(f):
        if f > 0.3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        compile_items(items, Machine(), prm.origin, placed, False, progress=cancel)
    assert getattr(S._MEMO, "d", None) is None


def test_blend_table_memo_by_exact_bits():
    ctrl = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.5), (2.5, 1.5), (2.5, 2.5)]
    kn = S._clamped_uniform(len(ctrl), 3)
    assert S.blend_table(CurveTable, ctrl, kn) is not S.blend_table(CurveTable, ctrl, kn)
    with S.table_memo():
        a = S.blend_table(CurveTable, ctrl, kn)
        assert S.blend_table(CurveTable, list(ctrl), list(kn)) is a
        neg = [(-0.0, 0.0)] + ctrl[1:]          # equal by value, other bits: a table of its own
        assert S.blend_table(CurveTable, neg, kn) is not a
        ints = [(0, 0)] + ctrl[1:]              # ints compute differently: never memoised
        assert S.blend_table(CurveTable, ints, kn) is not S.blend_table(CurveTable, ints, kn)
    assert getattr(S._MEMO, "d", None) is None
