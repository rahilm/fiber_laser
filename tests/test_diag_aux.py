"""Auxiliary functions: ball arm, interferometer, burn-in, light test, custom IO, batch,
maintenance, logs, help."""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mlaser.diag import ballarm, batch as B, burnin, customio as CIO, helpdocs  # noqa: E402
from mlaser.diag import interferometer as IF, lighttest, logs as L, maintain as M  # noqa: E402
from mlaser.diag.paramset import ParamSet, find_def  # noqa: E402
from mlaser.diag.program import Dwell, Move, ProgramRunner  # noqa: E402
from mlaser.model import Arc, Line  # noqa: E402
from mlaser.ui.facade import DemoController  # noqa: E402


def test_paramset_defaults_and_clamp():
    p = IF.params({})
    assert p["SP.InterfereTotalLength"] == 3000 and p["SP.InterfereStepLength"] == 150
    assert p["SP.InterfereIsFirstPtGapAdjust"] is True and p["SP.InterfereGapAdjust"] == 5
    p["SP.InterfereLoopTime"] = 1000                 # max 100 in the descriptor
    assert p["SP.InterfereLoopTime"] == 100
    assert find_def("ST.Type").options[1][1] == "Z-axis empty travel test"


def test_ball_arm_geometry():
    p = ballarm.params({})                           # L=100, R=50, 2 turns, horizontal, CW
    line, circle = ballarm.make_parts(p)
    seg = line.contours[0].segments[0]
    assert isinstance(seg, Line) and seg.start == (0.0, 0.0) and seg.end == (100.0, 0.0)
    arcs = circle.contours[0].segments
    assert len(arcs) == 2 and all(isinstance(a, Arc) and not a.ccw for a in arcs)
    assert arcs[0].center == (150.0, 0.0) and arcs[0].start == (100.0, 0.0)
    assert math.isclose(circle.contours[0].length(), 2 * 2 * math.pi * 50)
    p["GRP.BallArmGuideLineDir"] = 1
    p["GRP.BallArmCircleDir"] = 1
    end, c, a0, sweep = ballarm.geometry(100, 1, 50, 3, 1)
    assert end == (0.0, -100.0) and c == (0.0, -150.0) and a0 == math.pi / 2 and sweep > 0
    line, circle = ballarm.make_parts(p)
    assert circle.contours[0].segments[0].ccw
    assert abs(circle.contours[0].segments[0].start[1] + 100.0) < 1e-6


def test_interferometer_program():
    p = ParamSet(IF.KEYS, {"SP.InterfereTotalLength": 300.0, "SP.InterfereStepLength": 100.0,
                           "SP.InterfereStayTime": 2.0, "SP.InterfereGapAdjust": 5.0,
                           "SP.InterfereLoopTime": 1})
    steps = IF.build_program(p, (10.0, 20.0), home_dir=0, ppm=1000)
    moves = [s for s in steps if isinstance(s, Move)]
    xs = [m.x for m in moves]
    # gap take-up, p0..p3, overshoot, p3..p0
    assert xs == [5.0, 10.0, 110.0, 210.0, 310.0, 315.0, 310.0, 210.0, 110.0, 10.0]
    assert all(m.y == 20.0 and m.speed == 100.0 for m in moves)
    assert sum(1 for s in steps if isinstance(s, Dwell)) == len(moves)
    # home direction 1 -> moves go negative; no first gap -> start point skipped
    p["SP.InterfereIsFirstPtGapAdjust"] = False
    xs = [m.x for m in IF.build_program(p, (0.0, 0.0), home_dir=1) if isinstance(m, Move)]
    assert xs[:3] == [-100.0, -200.0, -300.0] and xs[3] == -305.0
    p["SP.InterfereAxisIndex"] = 1
    ys = [m.y for m in IF.build_program(p, (0.0, 0.0)) if isinstance(m, Move)]
    assert ys[0] == 100.0
    p["SP.InterfereStepLength"] = 1000.0
    try:
        IF.build_program(p, (0, 0))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pitch_table():
    t = IF.PitchTable.parse("pos,fwd,bwd\n0,0,0.01\n100,100.02,100.03\n200;199.99;200.01\n")
    assert t.positions == [0, 100, 200]
    fe, be = t.errors()
    assert [round(x, 3) for x in fe] == [0.0, 0.02, -0.01]
    assert round(t.mean_backlash(), 4) == round((0.01 + 0.01 + 0.02) / 3, 4)
    assert abs(t.error_at(50) - 0.01) < 1e-9
    steps = IF.build_program(ParamSet(IF.KEYS, {"SP.InterfereTotalLength": 200.0, "SP.InterfereStepLength": 100.0,
                                                "SP.InterfereStayTime": 0.0}), (0, 0), table=t, comp_type=2)
    xs = [round(m.x, 3) for m in steps if isinstance(m, Move)]
    assert 99.98 in xs
    assert t.to_csv().splitlines()[1].startswith("0.0000,")


def _demo():
    ctl = DemoController()
    ctl.connect()
    return ctl


def test_program_runner_with_demo_controller():
    ctl = _demo()
    r = ProgramRunner(ctl, [Move(5.0, 0.0), Dwell(0.02), Move(5.0, 5.0)])
    assert r.run_blocking(10.0) == "done"
    st = ctl.state()
    assert (round(st.x, 3), round(st.y, 3)) == (5.0, 5.0)


def test_burn_in_cycles():
    from mlaser.ui.docops import demo_document
    ctl = _demo()
    doc = demo_document()
    for p in doc.parts:                        # keep the dry run short
        p.contours = p.contours[:1]
    DemoController.RAPID = 4000.0
    try:
        p = burnin.params({"ST.Type": burnin.TYPE_XYZ, "ST.xyCycle": 2, "ST.zStartPos": 1.0, "ST.zEndPos": 3.0,
                           "ST.STIsReturnAfterManu": burnin.RETURN_ORIGIN})
        steps = burnin.build_cycle(p, doc)
        assert [type(s).__name__ for s in steps] == ["DryRun", "MoveZ", "MoveZ", "Move"]
        b = burnin.BurnIn(ctl, p, doc)
        assert b.start()
        import time
        t_end = time.monotonic() + 30
        while b.tick() and time.monotonic() < t_end:
            time.sleep(0.005)
        assert b.state == "done" and b.progress() == (2, 2)
        assert abs(ctl.state().z - 3.0) < 1e-6
    finally:
        DemoController.RAPID = 400.0
    # XY test without graphic refuses
    b = burnin.BurnIn(ctl, burnin.params({}), None)
    assert not b.start()


def test_light_test_countdown():
    ctl = _demo()
    t = [0.0]
    lt = lighttest.LightTest(ctl, lighttest.params({"LT.ltLaserTime": 3.0, "LT.ltPower": 30}), clock=lambda: t[0])
    assert lt.start() and ctl.state().outputs["laser"] and ctl.state().laser_power_pct == 30
    assert lt.remaining == 3
    t[0] = 1.2
    assert lt.tick() and lt.remaining == 2
    t[0] = 3.01
    assert not lt.tick() and not ctl.state().outputs["laser"]
    lt2 = lighttest.LightTest(ctl, lighttest.params({"LT.ltEnableTime": False}))
    assert lt2.start() and lt2.tick()
    lt2.stop()
    assert not ctl.state().outputs["laser"]


def test_custom_io_parse_and_toggle():
    items = CIO.parse_custom_do("Clamp#7#-,Light#8#0,bad#x#1,Two#9#1,short#3")
    assert [(c.name, c.port, c.self_locking) for c in items] == [("Clamp", 7, True), ("Light", 8, False), ("Two", 9, True)]
    assert CIO.parse_custom_do(CIO.format_custom_do(items)) == items
    drv = CIO.OutputDriver(_demo())
    assert drv.toggle(7) is True and drv.get(7) and drv.toggle(7) is False
    assert CIO.dedust_port({"DO.ClearAsh": 5}) == 5
    assert CIO.dedust_port({}, type("C", (), {"output_map": {"dedust": 4}})()) == 4


def test_batch_list_and_counter(tmp_path):
    bl = B.BatchList([B.BatchEntry("a.dxf", 2, 2), B.BatchEntry("b.dxf", 3, 1)])
    assert bl.current().path == "b.dxf" and bl.remaining() == 2
    assert B.BatchList.from_text(bl.to_text()).entries == bl.entries
    s = {"SOP.PlanManuCount": 2, "SOP.AfterPlanFinishedActionType": B.ACTION_PROMPT}
    pc = B.ProductionCounter(s)
    assert pc.job_done() is None and pc.job_done() == "prompt" and pc.job_done() is None
    assert s["SOP.TotalManuCount"] == 3 and pc.start_allowed()
    s["SOP.AfterPlanFinishedActionType"] = B.ACTION_PROHIBIT
    assert not pc.start_allowed()
    pc.reset()
    assert pc.start_allowed()


def test_batch_runner(tmp_path):
    from types import SimpleNamespace
    f = tmp_path / "a.dxf"
    f.write_text("x")
    loaded, started = [], []
    ctl = _demo()
    ctl.start = lambda doc: started.append(doc)
    win = SimpleNamespace(ctl=ctl, document="DOC", handlers={}, load_path=lambda p: loaded.append(p) or True)
    bl = B.BatchList([B.BatchEntry(str(f), 2, 0)])
    r = B.BatchRunner(win, B.params({"GRP.EnableBatchCut": True, "GRP.BatchCutX_LeftEdge": 0.0,
                                     "GRP.BatchCutX_RightEdge": 0.0}), bl)
    assert r.start() and loaded == [str(f)]
    import time
    t_end = time.monotonic() + 5
    while not started and time.monotonic() < t_end:
        r.tick()
        time.sleep(0.01)
    assert started == ["DOC"]
    r.job_done(True)
    assert bl.entries[0].done == 1 and r.active and loaded == [str(f)]    # same file not reloaded
    r.job_done(True)
    assert bl.entries[0].done == 2 and not r.active
    assert not B.BatchRunner(win, B.params({}), bl).start()               # batch disabled


def test_maintenance_due():
    now = 1_000_000.0
    plan = M.MaintainPlan(M.default_items(now))
    assert len(plan.items) == 6 + 7 + 4 and not plan.check(now)
    it = plan.items[0]
    it.cycle_days, it.warn_days = 7, 2
    assert it.status(now + 5.5 * M.DAY_S, 0) == "warn"
    assert it.status(now + 7 * M.DAY_S, 0) == "due"
    it.mark_done(now + 7 * M.DAY_S, 0)
    assert it.status(now + 7 * M.DAY_S, 0) == "ok"
    p = plan.items[-1]
    p.mode = M.MODE_PROCESS
    p.cycle_days, p.warn_days = 1, 0
    assert p.status(now + 100 * M.DAY_S, 3600) == "ok" and p.status(now, M.DAY_S) == "due"
    c = plan.add_custom("Grease rails", 3)
    assert M.MaintainPlan.from_text(plan.to_text()).items[-1].key == "Grease rails" and c.custom
    assert M.MaintainPlan.from_text("garbage").items     # falls back to the defaults


def test_logs(tmp_path):
    import datetime as dt
    w = L.LogFileWriter(str(tmp_path), clock=lambda: dt.datetime(2026, 9, 16, 13, 34, 41, 379000))
    w.write("MC-RecvErr_selectFunc ErrCode:10060")
    w.write("bad thing", "Error")
    p = tmp_path / "2026-09-16.log"
    text = p.read_text("utf-8")
    assert text.splitlines()[0] == "2026-09-16 13:34:41.379 [Info] -> MC-RecvErr_selectFunc ErrCode:10060"
    lines = L.parse_log(L.read_log(str(p)))
    assert len(lines) == 2 and lines[1].level == "Error"
    assert len(L.filter_lines(lines, level="Error")) == 1 and len(L.filter_lines(lines, "errcode")) == 1
    (tmp_path / "Code.txt").write_bytes("检查".encode("gbk"))
    assert len(L.list_logs([str(tmp_path), str(tmp_path / "missing")])) == 2
    assert L.decode("检查".encode("gbk")) == "检查"


def test_help_topics():
    ts = helpdocs.topics()
    names = [os.path.basename(t.path) for t in ts]
    assert "ui.md" in names and "BRIEF.md" not in names
    assert helpdocs.search("Report") and "omitted" in helpdocs.about_text()
