"""MainApp process sequences (pierce, start/end cut, gas) and their NCModule encoding."""
from __future__ import annotations


import pytest

from mlaser.protocol import frames as F
from mlaser.nc import process as P
from mlaser.model import LayerParams


def _cfg(**kw):
    return P.ProcessConfig(**{**P.ProcessConfig.bench().__dict__, **kw})


def _layer(pierce_type=3, **values):
    lp = LayerParams(1, speed=50, power_pct=80, duty_pct=60, frequency_hz=3000, gas=1,
                     gas_pressure=6.0, cut_height=1.2, pierce={"type": pierce_type})
    lp.extra = {"process": {("GP." + k if not k.startswith("GP.") else k): v for k, v in values.items()}}
    return lp


def _kinds(wire):
    return [d["kind"] for d in P.describe(wire)]


def test_two_stage_pierce_items_and_encoding():
    cfg = _cfg()
    layers = {1: _layer(3)}
    seq = P.ProcessSequencer(cfg, layers)
    items = seq.pierce(1)
    starts = [it for it in items if it.cls == P.CLS_PIERCE_START]
    # stages run from the highest index down; the first one carries flag 1 (bits 8..11)
    assert [(it.stage, it.first, it.layer) for it in starts] == [(1, 1, 1), (0, 0, 1)]
    enc = P.ManuEncoder(cfg, layers)
    wire = enc.encode_all(items)
    d = P.describe(wire)
    fol = [x for x in d if x["kind"] == "follow_to"]
    # DrillHeight1 = 10 > MP.DirectDrillMaxHeight 6 -> follow to 1 mm + 9 mm rest
    assert fol == [{"op": 105, "kind": "follow_to", "speed": 200.0, "height": 1.0, "vib": (0, 0),
                    "up_speed": 300.0, "rest": 9.0}]
    rel = [x for x in d if x["kind"] == "z_rel"]
    assert rel[0]["delta"] == pytest.approx(5.0)      # 10 -> 5 mm: +5 = down, at ZF up speed
    assert rel[0]["speed"] == 300.0
    waits = [x for x in d if x["kind"] == "wait" and x["mode"] == F.WAIT_ZF]
    assert [(w["state"], w["timeout_ms"]) for w in waits] == [(4, 20000), (2, 20000)]
    lasers = [x for x in d if x["kind"] == "laser"]
    assert [(x["freq"], x["duty"], x["on"]) for x in lasers] == [(500, 50, True), (500, 0, False)] * 2
    timed = [x["ms"] for x in d if x["kind"] == "wait" and x["mode"] == F.WAIT_TIME]
    # first gas: DirectGasDelay + GasDelay; 50 ms proportional valve sleep; 1000 ms pierce
    assert 200 in timed and 50 in timed and timed.count(1000) == 2
    # program start: VM+0x117c = -1, so no 3001 before the first process item (parity JN-01)
    assert d[0]["kind"] != "mark" and "mark" not in [x["kind"] for x in d]
    enc2 = P.ManuEncoder(cfg, layers)
    enc2.prev_cls = 0                                 # after motion samples (class 0)
    assert P.describe(enc2.encode_all(P.ProcessSequencer(cfg, layers).pierce(1)))[0]["kind"] == "mark"


def test_gradual_last_stage_goes_to_cut_height():
    cfg = _cfg()
    layers = {1: _layer(2, EnableGradualDrill0=True, DrillHeight0=5.0, DrillDelay0=1000)}
    seq = P.ProcessSequencer(cfg, layers)
    items = seq.pierce(1)
    assert [it.cls for it in items if it.cls in (4, 5)] == [P.CLS_PIERCE_START, P.CLS_PIERCE_END]
    d = P.describe(P.ManuEncoder(cfg, layers).encode_all(items))
    g = [x for x in d if x["kind"] == "z_gradual"]
    # (5 - 1.2) mm over 1 s -> 3.8 mm/s, absolute target 1.2 mm
    assert g[0]["delta"] == pytest.approx(3.8) and g[0]["target"] == pytest.approx(1.2)
    assert g[0]["speed"] == pytest.approx(3.8)
    w = [x for x in d if x["kind"] == "wait" and x["mode"] == F.WAIT_ZF]
    assert w[-1]["state"] == 2 and w[-1]["timeout_ms"] == 20000 + 1000
    # no plain dwell for a gradual stage (the time is spent in the Z move)
    assert not [x for x in d if x["kind"] == "wait" and x["mode"] == 0 and x["ms"] == 1000]


def test_smooth_pierce():
    cfg = _cfg()
    layers = {1: _layer(0, EnableSmoothPierce=True, SmoothPierceDrillHeight=4.0,
                        SmoothPierceDrillTime_ms=200)}
    items = P.ProcessSequencer(cfg, layers).pierce(1)
    assert [(it.cls, it.stage) for it in items if it.cls in (4, 5)] == [(4, 0xE), (5, 0xE)]
    d = P.describe(P.ManuEncoder(cfg, layers).encode_all(items))
    k = [x["kind"] for x in d]
    assert k.index("follow_to") < k.index("laser") < k.index("z_gradual")
    g = next(x for x in d if x["kind"] == "z_gradual")
    assert g["delta"] == pytest.approx(2.8) and g["speed"] == pytest.approx(14.0, abs=0.1)   # ftol truncation
    assert "laser" not in k[k.index("z_gradual"):]      # beam stays on into the cut


def test_no_pierce_without_follower_or_for_direct_cut():
    layers = {1: _layer(3)}
    assert P.ProcessSequencer(_cfg(zf_type=0), layers).pierce(1) == []
    assert P.ProcessSequencer(_cfg(), {1: _layer(0)}).pierce(1) == []
    assert P.ProcessSequencer(_cfg(), layers, dry_run=True).pierce(1) == []


def test_start_cut_follow_and_laser():
    cfg = _cfg()
    layers = {1: _layer(0, ZFVibAbatType=1, ZFVibAbat_Level=3, LaserOnDelay=40)}
    seq = P.ProcessSequencer(cfg, layers)
    items = seq.start_cut(1)
    assert seq.st.following
    d = P.describe(P.ManuEncoder(cfg, layers).encode_all(items))
    k = [x["kind"] for x in d]
    assert k.index("outputs") < k.index("follow_on") < k.index("laser")
    f = next(x for x in d if x["kind"] == "follow_on")
    assert f["height_units"] == 1200 and f["vib"] == (3, 0) and f["speed"] == 200.0
    lz = next(x for x in d if x["kind"] == "laser")
    assert (lz["freq"], lz["duty"]) == (3000, 60)
    assert d[-1] == {"op": 2001, "kind": "wait", "mode": 0, "ms": 40, "timeout_ms": 3000}
    # O2 = gas type 1 -> valve on output 2, proportional DA 10*6/10*1000
    outs = next(x for x in d if x["kind"] == "outputs")
    assert outs["outputs"] == {2: True}
    assert any(x["kind"] == "da" and x["value"] == 6000 for x in d)
    # already following: no second follow item
    assert not [it for it in seq.start_cut(1) if it.cls == P.CLS_FOLLOW]


def test_fix_height_start_cut():
    cfg = _cfg()
    layers = {1: _layer(1)}
    items = P.ProcessSequencer(cfg, layers).start_cut(1)
    d = P.describe(P.ManuEncoder(cfg, layers).encode_all(items))
    f = [x for x in d if x["kind"] in ("follow_on", "z_rel")]
    assert f[0]["height_units"] == 1000
    assert f[1]["delta"] == pytest.approx(-2.2)         # (-h - 1) mm


def test_end_cut_lift_short_hop_and_final():
    cfg = _cfg()
    layers = {1: _layer(0, ShortDistNoUp=True, ShortDistGasKeepOn=True)}
    seq = P.ProcessSequencer(cfg, layers)
    seq.start_cut(1)
    short = seq.end_cut(1, next_layer=1, hop_mm=5.0)
    assert not [it for it in short if it.cls in (P.CLS_LIFT, P.CLS_FINAL_LIFT)]
    assert seq.st.following and seq.st.gas_on            # gas kept, head kept down
    long_ = seq.end_cut(1, next_layer=1, hop_mm=50.0)
    assert [it.cls for it in long_ if it.cls in (8, 9)] == [P.CLS_LIFT]
    assert not seq.st.following and not seq.st.gas_on
    seq.start_cut(1)
    last = seq.end_cut(1, next_layer=None)
    assert [it.cls for it in last if it.cls in (8, 9)] == [P.CLS_FINAL_LIFT]
    d = P.describe(P.ManuEncoder(cfg, layers).encode_all(long_ + last))
    z = [x for x in d if x["kind"] == "z_rel"]
    assert z[0]["delta"] == -20.0 and z[0]["speed"] == 200.0      # GP.UpHeight at follow speed
    assert z[1]["delta"] == -20.0 and z[1]["speed"] == 300.0      # ZF.ZFDockHeight at up speed


def test_peak_current_da_and_gas_scaling():
    cfg = _cfg(laser_control=3, laser_da_port=2, laser_da_range=1)
    assert cfg.peak_da_value(80) == 4000                 # 80 * 0.5 (0-5 V) * 100
    assert _cfg(laser_control=3, laser_da_port=1, laser_type=7).peak_da_value(100) == 9500
    assert _cfg(laser_control=3, laser_da_port=1).peak_da_value(0.3) is not None
    assert cfg.peak_da_value(0) == 0
    assert _cfg(laser_control=3, laser_da_port=1, da_min=50).peak_da_value(0.6) == 0
    assert _cfg().peak_da_value(80) is None              # no IO-controlled laser
    enc = P.ManuEncoder(cfg, {1: _layer(0)})
    w1 = enc.encode(P.ManuItem(P.CLS_PEAK, value=1))
    w2 = enc.encode(P.ManuItem(P.CLS_PEAK, value=1))
    assert [d for d in P.describe(w1) if d["kind"] == "da"][0] == {
        "op": 9999, "kind": "da", "sub": 4, "args": [1, 4000], "channel": 1, "value": 4000}
    assert not [d for d in P.describe(w2) if d["kind"] == "da"]   # unchanged -> not resent
    g = _cfg(max_pressure=(10.0, 25.0, 10.0))
    assert g.gas_da_value(1, 5.0) == 2000 and g.gas_da_value(0, 5.0) == 5000


def test_gas_change_closes_previous_gas():
    cfg = _cfg(ratio_switch=(7, 8, 0))
    seq = P.ProcessSequencer(cfg, {})
    seq.gas_select(0, 5.0)
    seq.gas_delay(0)
    items = seq.gas_select(1, 5.0)
    d = P.describe(P.encode_sequence(items, cfg))
    outs = [x["outputs"] for x in d if x["kind"] == "outputs"]
    assert outs == [{1: False}, {7: False}, {2: True}, {8: True}]
    delay = seq.gas_delay(1)[0]
    assert delay.value == cfg.change_gas_delay_ms       # gas still on: only the change delay


def test_speed_curve():
    c = P.SpeedCurve(100.0, 80, [(0, 50), (50, 75), (100, 100)])
    assert len(c.segs) == 1                              # collinear middle node removed
    assert c(100.0) == 80 and c(99.95) == 80             # near full speed: base value
    assert c(50.0) == int(40 + 0.5 + 50 * 0.4)
    # x87 chop mode (FUN_10103030 0x101030d0): value + 0.5 is truncated, not rounded (JC-12)
    c2 = P.SpeedCurve(100.0, 50, [(0, 0), (100, 100)])
    assert c2(0.05) == int(0.05 * 0.5 + 0.5) == 0 and c2(30.0) == 15 and c2(31.0) == 16
    assert P.SpeedCurve(100.0, 80, [], True)(10) == 80
    assert P.parse_curve_nodes("0,30;50,60;100,100") == [(0, 30), (50, 60), (100, 100)]
    assert P.parse_curve_nodes([(0, 1), (2, 3)]) == [(0, 1), (2, 3)]


def test_marker_on_motion_to_process_transition():
    enc = P.ManuEncoder(_cfg(), {})
    assert enc.prev_cls == -1                          # program start (FUN_10047c50): no 3001 yet
    assert enc.encode(P.sys_cmd(P.SUB_DELAY, 0, 10))[0] != F.encode_mark()
    enc.prev_cls = 0                                   # motion samples were emitted
    a = enc.encode(P.sys_cmd(P.SUB_DELAY, 0, 10))
    b = enc.encode(P.sys_cmd(P.SUB_DELAY, 0, 10))
    assert a[0] == F.encode_mark() and b[0] != F.encode_mark()
    enc.prev_cls = 0
    assert enc.encode(P.ManuItem(P.CLS_MARKER)) == [F.encode_mark()]   # class 0xe: one 3001


def test_output_map_from_config():
    m = P.ProcessConfig.bench().output_map()
    assert m["gas_air"] == 1 and m["gas_o2"] == 2 and m["gas_n2"] == 3 and m["gas"] == 1
    assert "laser" not in m
    assert P.ProcessConfig().output_map() == {}
