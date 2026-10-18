"""Compiled jobs with process items: pierce / gas / follower / laser sequences and invariants."""
from __future__ import annotations

import math

import pytest

from mlaser.protocol import frames as F
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
from mlaser.nc import Machine, compile_job


def _rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def _doc(pierce_type=3, **values):
    lp = LayerParams(1, speed=50, power_pct=80, duty_pct=60, frequency_hz=3000, gas=1,
                     gas_pressure=6.0, cut_height=1.2, pierce={"type": pierce_type})
    lp.extra = {"process": {"GP." + k: v for k, v in values.items()}}
    parts = [Part([_rect(0, 0, 30, 20)], Craft(layer=1)), Part([_rect(40, 0, 10, 10)], Craft(layer=1))]
    return Document(parts, {1: lp})


def _desc(job):
    return [F.describe_item(w[0] & 0xFFFF, w[1:]) for w in job.wire]


def test_job_contains_pierce_gas_follow_sequence():
    m = Machine(pulses_per_mm_x=1000.0, pulses_per_mm_y=800.0)
    job = compile_job(_doc(), {"return_to_origin": True, "origin": (5.0, -3.0)}, m)
    d = _desc(job)
    # sum-of-deltas invariant still holds with the process items interleaved
    st = [x for x in d if x["kind"] == "step"]
    assert (job.start_pulses[0] + sum(x["dx"] for x in st),
            job.start_pulses[1] + sum(x["dy"] for x in st)) == job.end_pulses
    # truncation with the remainder carried (JB-01): within one pulse of the nominal end
    assert abs(job.end_pulses[0] - 5000) <= 1 and abs(job.end_pulses[1] - (-3.0 * 800)) <= 1
    words = [w for it in job.wire for w in it]
    assert len(F.parse_fifo_items(words)) == len(job.wire)
    assert job.pierces == 2 and job.process_items > 0 and job.process_time_s > 4.0

    # per contour: follow-to (pierce stage 1), relative move (stage 0), follow on (cut height),
    # laser PWM on, cut steps with the laser, sync off, laser off, lift.  No sync ON: the job
    # loop's FUN_004590a0(1) is cleared by the start cut's resize(0) (parity JB-04)
    k = [x["kind"] for x in d]
    first_cut = next(i for i, x in enumerate(d) if x["kind"] == "step" and x["duty"])
    head = k[:first_cut]
    i105, i109, i104 = head.index("follow_to"), head.index("z_rel"), head.index("follow_on")
    assert i105 < i109 < i104 < len(head) - 1
    assert "laser_sync" not in head
    las = [x for x in d[:first_cut] if x["kind"] == "laser"][-1]
    assert (las["freq"], las["duty"]) == (3000, 60)
    # pierce beam and gas come before the follow-on to cut height
    pierce_laser = [x for x in d[:i104] if x["kind"] == "laser" and x["on"]]
    assert [(x["freq"], x["duty"]) for x in pierce_laser] == [(500, 50), (500, 50)]
    assert any(x["kind"] == "outputs" and x["outputs"] == {2: True} for x in d[:first_cut])   # O2 valve
    # cut steps carry the layer frequency / duty
    assert {(x["freq"], x["duty"]) for x in st if x["duty"]} == {(3000, 60)}
    # after the last contour: sync off, laser off, gas off, final lift to the dock height
    tail = d[[i for i, x in enumerate(d) if x["kind"] == "step" and x["duty"]][-1] + 1:]
    tk = [x["kind"] for x in tail]
    assert tk.index("laser_sync") < tk.index("laser") < tk.index("z_rel")
    z = [x for x in tail if x["kind"] == "z_rel"][-1]
    assert z["delta"] == -20.0 and z["speed"] == 300.0
    assert any(x["kind"] == "outputs" and x["outputs"] == {2: False} for x in tail)
    # every ZF move is followed by a follower wait
    for i, x in enumerate(d):
        if x["kind"] in ("follow_to", "follow_on", "z_rel", "z_gradual"):
            assert d[i + 1]["kind"] == "wait" and d[i + 1]["mode"] == F.WAIT_ZF


def test_dry_run_has_no_laser_gas_or_follower():
    job = compile_job(_doc(), None, Machine(), dry_run=True)
    k = {x["kind"] for x in _desc(job)}
    assert not k & {"follow_to", "follow_on", "z_gradual", "da", "outputs"}
    assert not any(x["duty"] for x in _desc(job) if x["kind"] == "step")
    assert not any(x["kind"] == "laser" and x["on"] for x in _desc(job))


def test_process_disabled_is_phase1_behaviour():
    m = Machine()
    m.process.enabled = False
    job = compile_job(_doc(), None, m)
    assert {x["kind"] for x in _desc(job)} == {"step"}


def test_no_follower_machine_skips_pierce_and_lift():
    m = Machine()
    m.process.zf_type = 0
    d = _desc(compile_job(_doc(), None, m))
    k = {x["kind"] for x in d}
    assert not k & {"follow_to", "follow_on", "z_rel", "z_gradual"}
    assert "laser" in k and "outputs" in k


def test_slow_start_end_and_speed_curve():
    doc = _doc(0, UD_UpEnable=True, UD_UpLen=5.0, UD_UpSpeed=10.0, UD_UpAdvEnable=True,
               UD_UpDuty=20, UD_UpFreq=1000, UD_DownEnable=True, UD_DownLen=3.0,
               UD_DownSpeed=10.0, UD_DownAdvEnable=True, UD_DownDuty=30, UD_DownFreq=1500)
    doc.parts = doc.parts[:1]
    job = compile_job(doc, None, Machine(pulses_per_mm_x=1000.0, pulses_per_mm_y=1000.0))
    st = [x for x in _desc(job) if x["kind"] == "step" and x["duty"]]
    combos = {(x["freq"], x["duty"]) for x in st}
    assert {(1000, 20), (1500, 30), (3000, 60)} <= combos
    assert (st[0]["freq"], st[0]["duty"]) == (1000, 20) and (st[-1]["freq"], st[-1]["duty"]) == (1500, 30)
    # the slow start is travelled at <= 10 mm/s: its steps cover <= 0.01 mm each
    slow = [x for x in st if x["duty"] == 20]
    assert max(math.hypot(x["dx"], x["dy"]) for x in slow) <= 10.0 + 1
    up_len = sum(math.hypot(x["dx"], x["dy"]) for x in slow) / 1000.0
    assert up_len == pytest.approx(5.0, abs=0.1)
    # power vs speed: slower samples (corners) get less duty
    doc2 = _doc(0, PowerAdjustWithSpeed=True, PWMCurveNodes="0,20;100,100")
    st2 = [x for x in _desc(compile_job(doc2, None, Machine())) if x["kind"] == "step" and x["duty"]]
    duties = {x["duty"] for x in st2}
    assert max(duties) == 60 and min(duties) < 30


def test_adv_micro_joint_power():
    from mlaser.toolpath import items as I
    from mlaser.nc import compile_items
    m = Machine()
    m.process.adv_micro_link = True
    m.process.adv_micro_link_pct = 50
    items = [I.LayerChange(1), I.LaserOn(1), I.CutLine((0, 0), (5, 0), 20, 1, True),
             I.CutLine((5, 0), (6, 0), 20, 1, False), I.CutLine((6, 0), (10, 0), 20, 1, True), I.LaserOff()]
    doc = _doc(0)
    st = [x for x in _desc(compile_items(items, m, doc=doc)) if x["kind"] == "step" and x["duty"]]
    assert {x["duty"] for x in st} == {60, 30}
    # +8 stays 0xFFFF in the gaps of a normal cut element (FUN_00462630; 0 only for types 4/5)
    assert {x["power"] for x in st} == {0xFFFF}


def test_machine_from_dat_dir(tmp_path):
    from mlaser.formats.params import MachineParams, param_defs
    mp = MachineParams.defaults()
    defs = {d.key: d for d in param_defs() if d.file in ("HardPara.xml", "ManuPara.xml")}
    for key, val in {"ZF.ZFType": 1, "MGP.LowO2": 4, "MGP.LowN2": 5, "MGP.RatioO2": 2,
                     "ZF.ZFFollowSpeed": 150.0, "AX.InterpolationCycle": 500, "LGP.DOLaser": 9,
                     "GC.GasDelay": 300}.items():
        d = defs[key]
        (mp.hard if d.file == "HardPara.xml" else mp.manu).set(d, val)
    mp.save_dir(tmp_path)
    ini = tmp_path / "ipAdd.ini"
    ini.write_text("[Soft]\nFollowOvertime=15000\nDAMinVal=60\n")
    m = Machine.from_dat_dir(tmp_path, ini_path=str(ini))
    pc = m.process
    assert m.period_ms == 0.5
    assert pc.zf_type == 1 and pc.zf_follow_speed == 150.0 and pc.gas_delay_ms == 300
    assert pc.gas_valves[1:3] == (4, 5) and pc.ratio_da == (0, 2, 0)
    assert pc.follow_timeout_ms == 15000 and pc.da_min == 60 and pc.do_laser == 9
    om = pc.output_map()
    assert om["gas_o2"] == 4 and om["gas_n2"] == 5 and om["laser"] == 9 and om["gas"] == 4
    job = compile_job(_doc(), None, m)
    d = _desc(job)
    assert any(x["kind"] == "outputs" and x["outputs"] == {9: True} for x in d)   # LGP.DOLaser
    assert any(x["kind"] == "da" and x["channel"] == 1 for x in d)                # O2 proportional


def test_simulator_consumes_process_items():
    from mlaser.controller.client import MotionController, TransportConfig
    from mlaser.controller.simulator import CardSimulator
    from mlaser.controller.stream import JobStreamer
    doc = _doc(3)
    doc.parts = doc.parts[1:]
    m = Machine()
    job = compile_job(doc, {"return_to_origin": False}, m, start=(0.0, 0.0))
    ops = {w[0] & 0xFFFF for w in job.wire}
    assert {F.OP_FOLLOW_TO, F.OP_Z_REL, F.OP_FOLLOW_ON, F.OP_WAIT, F.OP_MARK, F.OP_SYS} <= ops
    sim = CardSimulator(period_ms=m.period_ms, time_scale=8.0).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port)
        with MotionController(config=cfg, pulses_per_mm=m.pulses_per_mm_x) as mc:
            mc.connect()
            JobStreamer(mc).run_job(job, timeout_s=60)
            assert sim.wait_idle(10)
            st = mc.read_status()
        assert (st.axes[0].position, st.axes[1].position) == job.end_pulses
        assert sum(1 for op, _ in sim.state.history if op == F.OP_STEP) == job.steps
        assert len(sim.state.history) == len(job.wire)
    finally:
        sim.stop()


def test_resume_items_restore_cut_state():
    from mlaser.nc.compiler import resume_items
    doc = _doc(0)
    job = compile_job(doc, None, Machine())
    d = _desc(job)
    mid = [i for i, x in enumerate(d) if x["kind"] == "step" and x["duty"]][50]
    k = [x["kind"] for x in (F.describe_item(w[0] & 0xFFFF, w[1:]) for w in resume_items(job, mid, doc))]
    assert "follow_on" in k and "laser" in k and "laser_sync" not in k   # start cut only (JB-04)
    assert resume_items(job, 0, doc) == []
