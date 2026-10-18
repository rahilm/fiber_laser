"""Machine operations: coordinates, upgrade, restart, hardware/file parameter backup, task and
offline files, network test, laser type (agent H, phase 3) -- against the simulator only."""
import os
import time
from pathlib import Path

import pytest

from mlaser.controller import coords as C
from mlaser.controller import frames as F
from mlaser.controller import upgrade as U
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.machine_ops import CO2, FIBER, MachineOps
from mlaser.controller.nettest import network_test
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.ui_adapter import UiController
from mlaser.formats import backup as B
from mlaser.formats.params import MachineParams
from mlaser.model import Contour, Document, Line, Part

ORIG_MCF = Path("/home/dev/scratch/mlaser/extracted/Mlaser-v0.0.1.51_SP1/Mlaser-v0.0.1.51_SP1/Update/MCC100_V201.77.mcf")


@pytest.fixture
def rig():
    sim = CardSimulator(time_scale=10.0).start()
    mc = MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port))
    mc.open()
    yield sim, mc
    mc.close()
    sim.stop()


def _square(x=0.0, y=0.0, s=10.0):
    pts = [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
    return Part([Contour([Line(a, b) for a, b in zip(pts, pts[1:])])])


# ------------------------------------------------------------------ coordinates
def test_coord_strings_and_anchor():
    pts = [(1.5, 2.0), (0, 0), (3, 4), (0, 0), (0, 0), (-1, 7.25)]
    s = C.format_points(pts)
    assert C.parse_points(s) == pts
    assert C.parse_points("1,2,3") == [(0.0, 0.0)] * 6        # not 12 tokens -> zeros (0x4dbb9a)
    bb = (0, 0, 100, 50)
    assert C.dock_anchor(bb, 7) == (0, 0)
    assert C.dock_anchor(bb, 3) == (100, 50)
    assert C.dock_anchor(bb, 5) == (50, 25)
    assert C.dock_anchor(bb, 0) is None
    c = C.CoordSystems(kind=2)
    c.set_tool_origin((10, 20))
    assert c.tools[1] == (10, 20)
    assert c.return_target() == ((10, 20), 100.0)
    assert C.CoordSystems.from_values(c.to_values()).tools == c.tools
    c.select(C.MANUAL)
    assert c.return_target() == ((0.0, 0.0), 100.0)


def test_tool_origin_set_return_and_job_placement():
    ctl = UiController(poll_s=0.01)
    assert ctl.connect_simulator(time_scale=20.0)
    try:
        ops = MachineOps(ctl, {})
        ops.select_coord(1)
        ctl.move_to(30.0, 40.0)
        assert ctl.sim.wait_idle(5)
        time.sleep(0.1)
        assert ops.set_origin() == pytest.approx((30.0, 40.0), abs=1e-3)
        ctl.move_to(5.0, 5.0)
        assert ctl.sim.wait_idle(5)
        time.sleep(0.1)
        ops.return_origin()
        assert ctl.sim.wait_idle(5)
        assert ctl.sim.position(0) / ctl.sim.ppm == pytest.approx(30.0, abs=1e-3)
        doc = Document(parts=[_square(100, 100)])
        placed = ctl.place(doc)
        assert placed.extra["job_offset"] == pytest.approx((-70.0, -60.0))   # dock 7 -> (30, 40)
        from mlaser.nc.compiler import placed_document
        assert placed_document(placed).bbox() == pytest.approx((30, 40, 40, 50))
        assert "job_offset" not in doc.extra
    finally:
        ctl.disconnect()


def test_manual_origin_zeroes_card(rig):
    sim, mc = rig
    class Ctl:
        pass
    ctl = UiController(poll_s=0.01)
    assert ctl.connect_simulator(time_scale=20.0)
    try:
        ctl.move_to(12.0, 3.0)
        assert ctl.sim.wait_idle(5)
        ops = MachineOps(ctl, {})
        ops.select_coord(C.MANUAL)
        ops.set_origin()
        assert [9999, 8, 3] in ctl.sim.state.commands
        assert ctl.sim.position(0) == 0
    finally:
        ctl.disconnect()


# ------------------------------------------------------------------ upgrade / restart
def _fw(tmp_path, n=5000, ext=".mcf", good_header=True):
    body = bytes((i * 7) & 0xFF for i in range(n))
    if ext == ".mcf":
        body = (n if good_header else n + 1).to_bytes(4, "little") + body[4:]
    p = tmp_path / f"fw{ext}"
    p.write_bytes(body)
    return p


def test_check_file(tmp_path):
    assert U.check_file(_fw(tmp_path), "mc").declared_length == 5000
    with pytest.raises(U.UpgradeError):
        U.check_file(_fw(tmp_path, good_header=False), "mc")
    short = tmp_path / "s.mcf"
    short.write_bytes(b"\x0d\0\0\0" + b"x" * 9)
    with pytest.raises(U.UpgradeError, match="too short"):
        U.check_file(short, "mc")
    with pytest.raises(U.UpgradeError, match="expected"):
        U.check_file(_fw(tmp_path, ext=".afb"), "mc")
    if ORIG_MCF.exists():
        assert len(U.check_file(ORIG_MCF, "mc").data) == 102912
    assert U.bundled_firmware_name(20177) == "MCC100_V201.77.mcf"
    assert U.needs_upgrade(20100) and not U.needs_upgrade(20177) and not U.needs_upgrade(0)


def test_mc_upload_against_simulator(rig, tmp_path):
    sim, mc = rig
    fw = U.check_file(_fw(tmp_path, 5000), "mc")
    seen = []
    n = U.upload_mc(mc, fw, seen.append)
    assert n == 5000 and seen[-1] == 100
    assert [a for a, _ in sim.state.downloads] == [0, 1400, 2800, 4200]
    assert sim.firmware_image() == fw.data
    assert sim.state.control_writes[-1] == 8888 and sim.state.reboots == 1
    assert mc._timeout_ms == mc.cfg.timeout_ms             # restored


def test_device_upload_rtu(tmp_path):
    from mlaser.devices.modbus_rtu import RtuClient, RtuSimulator
    from mlaser.devices.codecs import RtuCodec
    dev = RtuSimulator(1)
    rtu = RtuClient(dev.loopback(), RtuCodec(1), "AF")
    fw = U.check_file(_fw(tmp_path, 450, ".afb"), "af")
    sleeps = []
    U.upload_device(rtu, fw, sleep=sleeps.append)
    funcs = [r[0] for r in dev.requests]
    assert funcs[0] == 0x10 and funcs[-1] == 0x10 and funcs.count(0x26) == 3
    assert dev.words[100] == 6666 & 0xFFFF
    assert sleeps[:2] == [0.5, 2.0]


def test_restart(rig):
    sim, mc = rig
    U.restart_card(mc)
    assert sim.state.control_writes == [8888]


# ------------------------------------------------------------------ parameters
def test_hardware_blocks_roundtrip(rig):
    sim, mc = rig
    mp = MachineParams.defaults()
    d_int = next(d for d in B.hard_table() if d.type == 1)
    d_dbl = next(d for d in B.hard_table() if d.type == 2)
    mp.hard.set(d_int, 1234)
    mp.hard.set(d_dbl, 12.345678)
    B.write_hard_to_card(mc, mp.hard)
    assert set(sim.state.hw_blocks) == {59600, 59601}
    assert sim.state.hw_blocks[59600][:2] == [0xFFFFFFFF, 0xE0]
    mp2 = MachineParams.defaults()
    n = B.read_hard_from_card(mc, mp2.hard)
    assert n > 400
    assert mp2.hard.get(d_int) == 1234
    assert mp2.hard.get(d_dbl) == pytest.approx(12.3456, abs=1e-9)    # trunc(v*10000)/10000


def test_param_file_and_backups(tmp_path):
    mp = MachineParams.defaults()
    d = next(d for d in B.hard_table() if d.type == 2)
    mp.hard.set(d, 7.5)
    f = B.export_params_file(mp, tmp_path / "all.xml")
    mp2 = MachineParams.defaults()
    counts = B.import_params_file(f, mp2, layers=False)
    assert counts["HardPara.xml"] > 400 and counts["LayerPara.xml"] == 0
    assert mp2.hard.get(d) == 7.5
    bk = B.ParamBackup(mp2, tmp_path / "NexCut", tmp_path / "File")
    bk.import_from_file(f, layers=True)
    assert (tmp_path / "File" / "BkHardPara.xml").exists()
    assert (tmp_path / "File" / "BkLayerPara.xml").exists()


def test_task_roundtrip(tmp_path):
    doc = Document(parts=[_square(), _square(20, 0)])
    p = B.save_task(tmp_path / "job", doc, laser_type=1, order=[1, 0], only_selected=True)
    assert p.suffix == ".aut"
    raw = p.read_bytes()
    assert raw.index(b"TASK_GRAPH") < raw.index(b"TASK_LAYER") < raw.index(b"TASK_IS_BREAK")
    with pytest.raises(ValueError, match="A250421_0"):
        B.load_task(p, tmp_path / "Temp", laser_type=0)
    d2, t = B.load_task(p, tmp_path / "Temp", laser_type=1)
    assert len(d2.parts) == 2 and t.manu == [1, 0] and t.only_selected == 1
    assert (tmp_path / "Temp" / "ManuContour.dat").exists()


def test_offline_container(tmp_path):
    from mlaser.nc import compile_job
    from mlaser.nc.offline import export_offline, unpack_container
    doc = Document(parts=[_square()])
    job = compile_job(doc)
    out = export_offline(tmp_path / "job", job, doc, laser_type=0)
    assert out.suffix == ".enc"
    parts = unpack_container(out.read_bytes())
    olpf = parts["_tempManuItem.olpf"].decode()
    lines = olpf.splitlines()
    assert lines[0] == "Manu_Begin 1 1" and "Manu_End" in lines and lines[-1].startswith("Size ")
    assert int(lines[-1].split()[1]) == len(lines) - 3
    assert parts["_tempSource.chf"].startswith(b"scFlie")


def test_offline_card_upload(rig):
    from mlaser.nc.offline import upload_offline_to_card
    sim, mc = rig
    upload_offline_to_card(mc, "a.enc", b"x" * 3000)
    assert sim.state.commands[0][0] == 7 and sim.state.commands[-1] == [8]
    assert sim.firmware_image() == b"x" * 3000


# ------------------------------------------------------------------ network test / laser type
def test_network_test(rig):
    sim, mc = rig
    res = network_test(("127.0.0.1", sim.port), ("127.0.0.1", 9), count=2, timeout_s=0.2)
    assert res[0].ok and res[0].received == 2 and "rtt" in res[0].summary()
    assert not res[1].ok


def test_laser_type_switch():
    """FUN_004b12e0: laser off (PWM pair at LC.PtLaserFreq), old type's red light off, the CO2
    enable output, [118, 5, x] only with a follower (VM +0x70), then the soft limits."""
    ctl = UiController(poll_s=0.01)
    assert ctl.connect_simulator(time_scale=20.0)
    try:
        vals = {"LGP.doCO2EnableOutput": 6, "LGP.DORedLight": 4, "ZF.ZFType": 1}
        ctl.machine.params.update(vals)          # HardPara values (Machine.from_params)
        ops = MachineOps(ctl, dict(vals))
        n0 = len(ctl.sim.state.commands)
        assert ops.switch_laser_type(CO2)
        cmds = ctl.sim.state.commands[n0:]
        f = ctl.machine.pt_laser_freq
        assert cmds[:3] == [[9999, 3, f, 0, 0], [9999, 17, f, 0, 0], [9999, 2, 8, 0]]
        assert [118, 5, 1] in cmds
        assert ctl.sim.state.outputs >> 5 & 1
        assert ops.values["SP.m_iEnableLaserType"] == 1
        assert ctl.laser_type == 1
        # soft limits rewritten (NC +0x198): stroke 1000 mm, go-origin direction 0
        ppm = int(ctl.position_ppm)
        assert ctl.sim.state.params.get(50202) == int(-0.5 * ppm) & 0xFFFFFFFF
        assert ctl.sim.state.params.get(50204) == 1000 * ppm      # 32-bit words, stride 2
    finally:
        ctl.disconnect()


def test_laser_type_switch_without_follower_sends_no_118():
    ctl = UiController(poll_s=0.01)
    assert ctl.connect_simulator(time_scale=20.0)
    try:
        ops = MachineOps(ctl, {"ZF.ZFType": 0})
        n0 = len(ctl.sim.state.commands)
        assert ops.switch_laser_type(CO2) and ops.switch_laser_type(FIBER)
        assert not any(c[:1] == [118] for c in ctl.sim.state.commands[n0:])
    finally:
        ctl.disconnect()


# ------------------------------------------------------------------ UI
@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_ext_machine_ui(qapp, tmp_path, monkeypatch):
    from mlaser.ui import ext_machine
    from mlaser.ui.mainwindow import MainWindow
    w = MainWindow()
    ext = ext_machine.register(w, app_dir=str(tmp_path))
    msgs = []
    w.log_panel.log = msgs.append
    for cmd in ("coord.tool3", "hw.upgrade", "hw.restart", "machine.edge_clear"):
        w.ribbon.actions[cmd].trigger()
    qapp.processEvents()
    assert ext.ops.coords.kind == 3
    assert any("needs a connected motion card" in m for m in msgs)
    # laser type swaps the layer table
    w.document.layers = {}
    w.ribbon.actions["machine.laser_co2"].trigger()
    assert ext.ops.laser_type == CO2 and len(w.document.layers) == 11
    # recovery from autosave.chf
    from mlaser.formats.chf import save_chf
    (tmp_path / "File").mkdir()
    save_chf(Document(parts=[_square()]), str(tmp_path / "File" / "autosave.chf"))
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.ribbon.actions["app.recovery"].trigger()
    qapp.processEvents()
    assert len(w.document.parts) == 1
    w.close()
