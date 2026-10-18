"""Job-stream parity fixes (docs/parity.md §9, JB-01..06, JN-01..08, JC-12): the rebuild's
ManuItems and FIFO stream follow MainApp FUN_00462630 / NCModule FUN_10043c20."""
from __future__ import annotations

import pytest

from mlaser.cam import items as I
from mlaser.controller import frames as F
from mlaser.controller import process as P
from mlaser.model import Document, LayerParams
from mlaser.nc import Machine, compile_items
from mlaser.nc import compiler as C


def _cut_job(moves, m=None, **gp):
    lp = LayerParams(1, speed=100, power_pct=90, duty_pct=70, frequency_hz=3000)
    lp.extra = {"process": {"GP." + k: v for k, v in gp.items()}}
    m = m or Machine()
    return compile_items([I.LaserOn(1), *moves, I.LaserOff()], m, start=tuple(moves[0].start),
                         doc=Document(layers={1: lp}))


def test_quantise_truncates_and_carries():
    """FUN_00473bd0: d = ftol(v + carry), carry = v + carry - d (no rounding)."""
    em = C._Emitter(Machine(), (0.0, 0.0), False)
    assert em.quantise(0.0099, 0.0) == (0, 0) and em.carry[0] == pytest.approx(0.99)
    assert em.quantise(0.0199, 0.0) == (1, 0) and em.carry[0] == pytest.approx(0.99)
    # toward zero, not floor: -1.5 + 0.99 = -0.51 -> 0
    assert em.quantise(0.0049, 0.0) == (0, 0) and em.carry[0] == pytest.approx(-0.51)
    em2 = C._Emitter(Machine(), (0.0, 0.0), False, carry=(0.5, -0.5))
    assert em2.quantise(0.005, 0.005) == (1, 0)          # the remainder is an input (never reset)


def test_one_step_per_sample_including_sample_0_and_no_closing_step():
    moves = [I.CutLine((10.0, 10.0), (40.0, 10.0), 100.0, 1)]
    job = _cut_job(moves)
    plan = C.plan_path(moves, Machine().motion_config())
    ss = plan.sample(1.0)
    st = job.step_deltas()
    assert len(st) == len(ss)                            # sample 0 included, nothing appended
    assert st[0][:2] == (0, 0) and st[0][3] == 70        # sample 0: zero delta, beam on
    assert {s[4] for s in st} == {0xFFFF}                # +8 = 0xFFFF for every sample (JB-02)
    # cumulative position: within one pulse of the last sample (may be short), rest in the carry
    sx = sum(s[0] for s in st)
    assert 0 <= ss[-1] * 100 - sx < 1
    assert job.end_mm == (40.0, 10.0) and job.pulse_carry[0] == pytest.approx(ss[-1] * 100 - sx, abs=1e-6)


def test_no_laser_sync_on_before_the_samples():
    """JB-04: the job loop's sync on is cleared by the start cut; the sync off stays."""
    job = _cut_job([I.CutLine((0.0, 0.0), (5.0, 0.0), 50.0, 1)])
    d = [F.describe_item(w[0] & 0xFFFF, w[1:]) for w in job.wire]
    syncs = [x["src"] for x in d if x["kind"] == "laser_sync"]
    assert syncs == [0]
    assert job.cut_blocks and job.cut_blocks[0][2] == 1


def test_micro_joint_gap_inclusive_rule():
    """FUN_00469950: samples idx(start)..idx(end) inclusive, idx = first sample with s >= pos."""
    ss = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    assert C.gap_flags(ss, [(0.7, 1.5)]) == [False, False, True, True, False, False, False]
    assert C.gap_flags(ss, [(0.7, 1.6)]) == [False, False, True, True, True, False, False]
    assert C.gap_flags(ss, [(2.9, 9.0)])[-1] and C.gap_flags(ss, [(3.1, 9.0)]) == [False] * 7
    moves = [I.CutLine((0, 0), (15, 0), 100.0, 1), I.CutLine((15, 0), (16, 0), 100.0, 1, False),
             I.CutLine((16, 0), (30, 0), 100.0, 1)]
    st = _cut_job(moves).step_deltas()
    assert sum(1 for s in st if not s[3]) == 11          # 1 mm at 100 mm/s: 11 gap samples, not 10
    m = Machine()
    m.process.adv_micro_link, m.process.adv_micro_link_pct = True, 60
    st = _cut_job(moves, m).step_deltas()
    gap = [s for s in st if s[3] == 42]                  # 70 * 60 / 100
    assert len(gap) == 11 and {s[4] for s in gap} == {0xFFFF}


def test_end_cut_no_close_gas_from_current_layer():
    cfg = P.ProcessConfig.bench()
    l1 = LayerParams(1, gas=1)
    l1.extra = {"process": {"GP.NoCloseGasInManu": 1}}
    l2 = LayerParams(2, gas=1)
    seq = P.ProcessSequencer(cfg, {1: l1, 2: l2})
    seq.st.gas, seq.st.gas_on = 1, True
    kept = seq.end_cut(1, 2, 100.0)
    assert not any(it.cls == P.CLS_SYS and it.a == P.SUB_DO and it.value == 0 and it.b == 1 for it in kept)
    seq2 = P.ProcessSequencer(cfg, {1: l2, 2: l1})       # flag only on the next layer: gas closes
    seq2.st.gas, seq2.st.gas_on = 1, True
    assert any(it.cls == P.CLS_SYS and it.a == P.SUB_DO and it.value == 0 for it in seq2.end_cut(1, 2, 100.0))


def test_bolt_drill_dwell_and_ramp():
    """JB-06: DrillDelay * 1000/cycle zero class-0 items (beam off), then FUN_00458650's ramp."""
    cfg = P.ProcessConfig.bench()
    lp = LayerParams(1)
    lp.extra = {"process": {"GP.ManuType": 2, "GP.DrillDelay0": 30, "GP.DrillPower0": 60, "GP.DrillFreq0": 800,
                            "GP.BoltDrill_Enable_1": 1, "GP.BoltDrill_Power_1": 90, "GP.BoltDrill_Freq_1": 2000}}
    items = P.ProcessSequencer(cfg, {1: lp}).pierce(1)
    zero = [it for it in items if it.cls == P.CLS_MOTION]
    assert len(zero) == 30 and all(it.a == it.b == it.duty == 0 for it in zero)
    ramp = [it for it in items if it.cls == P.CLS_SYS and it.a == P.SUB_LASER][1:-1]
    assert len(ramp) == 30
    assert [(it.freq, it.duty) for it in ramp[:3]] == [(840, 61), (880, 62), (920, 63)]
    assert (ramp[-1].freq, ramp[-1].duty) == (2000, 90)
    assert items[[i for i, it in enumerate(items) if it.cls == P.CLS_SYS and it.a == P.SUB_LASER][-1]].freq == 800
    cfg2 = P.replace(cfg, interp_cycle_us=500)
    assert sum(1 for it in P.ProcessSequencer(cfg2, {1: lp}).pierce(1) if it.cls == P.CLS_MOTION) == 60
    # in a job the dwell items are op-3000 steps with duty 0
    m = Machine()
    lp.pierce = {"type": 2}
    job = compile_items([I.Pierce((0, 0), 1), I.LaserOn(1), I.CutLine((0, 0), (2, 0), 50.0, 1), I.LaserOff()],
                        m, doc=Document(layers={1: lp}))
    assert sum(1 for idx, mi in job.manu if mi.cls == P.CLS_MOTION) == 30
    st = job.step_deltas()
    assert all(s[:2] == (0, 0) and s[3] == 0 for s in st[:30]) and st[30][3] > 0


def test_encoder_program_start_state_and_fall_through():
    cfg = P.ProcessConfig.bench()
    enc = P.ManuEncoder(cfg, {})
    assert enc.prev_cls == -1 and enc.peak == 0 and enc.focus == 0.0
    # classes 2 and 0x14.. are op-3000 steps from their own fields (JN-02)
    w = enc.encode(P.ManuItem(0x14, a=3, b=-2, duty=10, freq=700, power=0x1234))
    assert w == [F.encode_step(3, -2, 700, 10, 0x1234)]
    assert enc.encode(P.ManuItem(0xC)) == []
    # ZF type 0: the follower classes become one zero step each, class 6 still waits (JN-03)
    nozf = P.ManuEncoder(P.replace(cfg, zf_type=0), {})
    assert nozf.encode(P.ManuItem(P.CLS_FOLLOW, value=1)) == [F.encode_step(0, 0, 0, 0, 0xFFFF)]
    assert nozf.encode(P.ManuItem(P.CLS_WAIT_ZF, a=3, b=4))[-1][0] & 0xFFFF == F.OP_WAIT


def test_focus_axis_wait_and_start_position():
    """JN-04/05/06: +50 ms, axis 0x10 for AF type 5, no move to the start position."""
    lp = LayerParams(1, cut_height=1.0)
    lp.extra = {"process": {"GP.CutFocusPos": 1.5}}
    for af, axis in ((5, 0x10), (6, 7)):
        cfg = P.replace(P.ProcessConfig.bench(), af_type=af, af_axis=7, af_speed=12.0)
        enc = P.ManuEncoder(cfg, {1: lp})
        d = P.describe(enc.encode(P.ManuItem(P.CLS_FOLLOW, value=1)))
        mv = [x for x in d if x["op"] == F.OP_AXIS_MOVE]
        assert len(mv) == 1
        wt = [x for x in d if x["kind"] == "wait" and x["mode"] == F.WAIT_AXIS]
        assert wt[0]["timeout_ms"] == cfg.follow_timeout_ms + int(1.5 / 12.0 * 1000 + 50)
        assert enc.encode(P.ManuItem(P.CLS_PRE_JUMP, value=1)) == []     # already there
        enc0 = P.ManuEncoder(cfg, {1: LayerParams(1)})
        assert enc0.encode(P.ManuItem(P.CLS_PRE_JUMP, value=1)) == []    # focus 0 == start position
        first = enc.__class__(cfg, {1: lp}).encode(P.ManuItem(P.CLS_FOLLOW, value=1))
        assert any((w[0] & 0xFFFF) == F.OP_AXIS_MOVE and (w[1] & 0xFF) == axis for w in first)


def test_first_peak_zero_sends_no_da():
    cfg = P.replace(P.ProcessConfig.bench(), laser_control=3, laser_da_port=2)
    lp = LayerParams(1, power_pct=0)
    assert P.ManuEncoder(cfg, {1: lp}).encode(P.ManuItem(P.CLS_PEAK, value=1)) == []
    assert P.ManuEncoder(cfg, {1: LayerParams(1, power_pct=50)}).encode(P.ManuItem(P.CLS_PEAK, value=1))


def test_frames_size_test_once_per_manuitem():
    """JN-08: a ManuItem's expansion is never split; frames may exceed 1200 bytes by one expansion."""
    step = F.encode_step(1, 0, 5000, 0, 0)
    big = [F.fifo_item(F.OP_WAIT, 0, 1)] * 12                       # one 36-word expansion
    groups = [[step]] * 98 + [big] + [[step]] * 10
    fr = F.build_fifo_frames(groups, 1)
    assert len(fr[0]) - 1 == 98 * 3 + 36                            # 330 words in frame 1
    flat = F.build_fifo_frames([w for g in groups for w in g], 1)
    assert len(flat[0]) - 1 == 297                                  # per item: split inside
    assert [w for f in fr for w in f[1:]] == [w for f in flat for w in f[1:]]
