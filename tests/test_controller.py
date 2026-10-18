"""Tests for mlaser.controller: frames from the real log, client <-> simulator."""
from __future__ import annotations

import struct
import time

import pytest

from mlaser.protocol import frames as F
from mlaser.controller.client import ControllerError, FifoStreamer, MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.devices import OnBoardFollower, encode_std_modbus

# Lines from $ORIG/Log/2026-09-16.log
LOG_BUFFER = "02 be d0 45 00 03 00 b0 02"
LOG_DATAEX_READ = "30 2710 12"
LOG_DATAEX_HELLO = "40 64 01 22b8"
LOG_DATAEX_OUT = "40 65 04 270f 02 01 01"


# ---------------------------------------------------------------- frames
def test_crc_matches_captured_reply():
    buf = F.parse_hex(LOG_BUFFER)
    assert F.crc16_modbus(buf[4:]) == 0x45D0
    assert F.crc_ok(buf)
    assert buf[2:4] == (0x45D0).to_bytes(2, "little")   # card sends CRC low byte first


def test_decode_logged_exception_reply():
    buf = F.parse_hex(LOG_BUFFER)
    resp = F.decode_response(buf, expected_tid=0x02BE)
    assert resp.tid == 0x02BE and resp.is_exception
    assert resp.func == 0x30 and resp.exception == 2
    # log "Data:000000b0 00000002" (FUN_10029a90 keeps the 0x80 flag, D-10)
    assert resp.as_data() == [0xB0, 2]
    req = F.parse_dataex(LOG_DATAEX_READ)
    assert (req.func, req.addr, req.count) == (0x30, 10000, 18)
    assert F.check_reply(req, resp) == 502                # log "ErrCode:502"


def test_tid_mismatch_is_code_4():
    with pytest.raises(F.FrameError) as ei:
        F.decode_response(F.parse_hex(LOG_BUFFER), expected_tid=1)
    assert ei.value.code == 4


def test_encode_logged_requests():
    req = F.parse_dataex(LOG_DATAEX_READ)
    req.tid = 0x02BE
    fr = F.encode_request(req)
    assert fr[:2] == b"\x02\xbe"
    assert fr[4:6] == b"\x00\x08"
    assert fr[6:] == bytes([0, 0x30]) + struct.pack("<IH", 10000, 18)
    assert fr[2:4] == F.crc16_modbus(fr[4:]).to_bytes(2, "big")
    assert F.decode_request(fr).as_dataex() == [0x30, 10000, 18]

    hello = F.parse_dataex(LOG_DATAEX_HELLO)
    assert hello.values == [8888] and hello.addr == F.REG_CONTROL
    fr = F.encode_request(hello)
    assert len(fr) == 14 + 4 and fr[4:6] == b"\x00\x0c"
    assert struct.unpack_from("<IHI", fr, 8) == (100, 1, 8888)

    out = F.parse_dataex(LOG_DATAEX_OUT)
    assert out.addr == F.REG_MAILBOX
    assert out.values == F.mailbox_sys(F.SYS_OUTPUTS, 1, 1)      # output 1 on
    assert F.decode_request(F.encode_request(out)).values == [9999, 2, 1, 1]


def test_response_roundtrip():
    r = F.Response(F.FUNC_READ, 7, 1000, 3, [1, 2, 0xFFFFFFFF])
    d = F.decode_response(F.encode_response(r), 7)
    assert d.values == [1, 2, 0xFFFFFFFF] and d.addr == 1000
    w = F.decode_response(F.encode_response(F.Response(F.FUNC_WRITE, 9, 101, 4)), 9)
    assert (w.func, w.addr, w.count) == (0x40, 101, 4)


def test_step_item_roundtrip():
    it = F.encode_step(-5, 127, 5000, True, 1234)
    assert it[0] == 0x80BB8
    assert F.decode_step(it[1:]) == (-5, 127, 5000, 100, 1234)
    it = F.encode_step(3, -1, 0, False, 99)
    assert F.decode_step(it[1:]) == (3, -1, 5000, 0, 0)
    with pytest.raises(ValueError):
        F.encode_step(128, 0)


def test_item_headers_match_dll_constants():
    assert F.item_header(F.OP_SYS, 2) == 0x8270F
    assert F.item_header(F.OP_SYS, 3) == 0xC270F
    assert F.item_header(F.OP_WAIT, 2) == 0x807D1
    assert F.item_header(F.OP_3002, 1) == 0x40BBA
    assert F.item_header(105, 4) == 0x100069
    items = [F.sys_item(F.SYS_OUTPUTS, 1, 1), F.encode_step(1, 2)]
    flat = [w for it in items for w in it]
    assert F.parse_fifo_items(flat) == [(9999, [2, 1, 1]), (3000, items[1][1:])]


def test_frame_packing_limit():
    items = [F.encode_step(1, 1) for _ in range(500)]
    frames = F.build_fifo_frames(items, first_seq=7)
    assert [f[0] for f in frames] == list(range(7, 7 + len(frames)))
    for f in frames:
        assert (len(f) + 3) * 4 <= 0x4B0 + 12
        assert len(F.encode_request(F.Request(F.FUNC_WRITE, 102, len(f), f))) <= F.MAX_FRAME
    assert sum(len(f) - 1 for f in frames) == 500 * 3


def test_hal_filters():
    assert F.is_write_filtered(309)          # homing register: never sent
    assert not F.is_write_filtered(101)
    assert not F.is_write_filtered(150)
    assert not F.is_write_filtered(50000)
    assert F.is_write_filtered(52000)
    assert F.read_filter(1000) is None and F.read_filter(2000) is None
    assert F.read_filter(500) == 0 and F.read_filter(1500) == 1


def test_std_modbus_encoder():
    b = encode_std_modbus(0x03, 0x0010, count=2)
    assert b == bytes.fromhex("0000000000060003" "00100002")


def test_ini_loader(tmp_path):
    p = tmp_path / "ipAdd.ini"
    p.write_bytes(b"[IP]\r\nCardIP=10.1.1.168\r\nCardPort=502\r\nMCTimeout=500\r\nMCFifoTime=1600\r\n"
                  b"MCMaxRecvTime=3\r\n[Soft]\r\nAccessType=1\r\n")
    c = TransportConfig.from_ini(str(p))
    assert (c.host, c.port, c.transport, c.max_send, c.max_recv) == ("10.1.1.168", 502, "udp", 3, 3)


# ---------------------------------------------------------------- client <-> simulator
@pytest.fixture(params=["udp", "tcp"])
def rig(request):
    sim = CardSimulator(transport=request.param).start()
    cfg = TransportConfig(host="127.0.0.1", port=sim.port, transport=request.param,
                          timeout_ms=200, fifo_time_ms=600, max_recv=2)
    mc = MotionController(config=cfg, pulses_per_mm=1000.0).open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_connect_and_status(rig):
    sim, mc = rig
    cfgw, ver = mc.connect()
    assert ver == 20177
    # O (parity capture app_startup): RO[0..1] then 101 := [9999, 5, 0, 0]; no 100 := 8888
    assert sim.state.commands == [[9999, 5, 0, 0]]
    assert sim.state.control_writes == []
    st = mc.read_status()
    assert len(st.ro) == 36 and len(st.axes) == 5
    assert st.fifo_free == sim.fifo_capacity
    assert st.machine_state() == 0


def test_outputs_inputs_da_pwm(rig):
    sim, mc = rig
    mc.set_output(3, True)
    mc.set_output(12, True)
    st = mc.read_status()
    assert st.output(3) and not st.output(2) and st.output(12)
    mc.set_output(3, False)
    assert not mc.read_status().output(3)
    sim.set_input(5, True)
    assert mc.read_status().input(5)
    mc.set_da(1, 20)              # clamped to DAMinVal 50
    mc.set_da(2, 6000)
    assert sim.state.da == [50, 6000]
    mc.set_pwm(5000, 80)
    assert sim.state.pwm == (5000, 80)
    assert sim.state.commands[0] == [9999, 2, 4, 4]


def test_exception_reply_maps_to_502(rig):
    sim, mc = rig
    with pytest.raises(ControllerError) as ei:
        mc.read_regs(F.REG_ONBZF, F.N_ONBZF)
    assert ei.value.code == 502
    with pytest.raises(ControllerError):
        OnBoardFollower(mc).read_status()


def test_filtered_write_not_sent(rig):
    sim, mc = rig
    n = sim.requests
    mc.write_regs(309, [1, 2, 3])
    assert sim.requests == n


def test_jog_and_move(rig):
    sim, mc = rig
    mc.jog_mm(0, speed_mm_s=200, accel=2000, distance_mm=5)    # acc word passed unscaled
    assert mc.read_status().machine_state() in (0, 2)
    assert sim.wait_idle(3)
    assert mc.read_status().axes[0].position == 5000
    mc.move(0b11, 400000, 1000000, [-1000, 2500])        # relative (FUN_005bea00 callers)
    assert sim.wait_idle(3)
    pos = mc.position_mm()
    assert pos[0] == pytest.approx(4.0) and pos[1] == pytest.approx(2.5)


def test_stop(rig):
    sim, mc = rig
    mc.jog(1, 1000, 1000, 10_000_000)
    time.sleep(0.05)
    assert mc.read_status().axes[1].busy
    mc.stop()
    st = mc.read_status()
    assert not st.axes[1].busy


def test_retry_after_dropped_reply():
    sim = CardSimulator(drop_replies=1).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=100, fifo_time_ms=300)
        with MotionController(config=cfg) as mc:
            assert mc.read_reg(1001) == 20177
            assert sim.requests == 2
    finally:
        sim.stop()


def test_timeout_when_nobody_listens():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        cfg = TransportConfig(host="127.0.0.1", port=port, timeout_ms=30, fifo_time_ms=60, max_recv=2)
        with MotionController(config=cfg) as mc:
            t0 = time.monotonic()
            with pytest.raises(ControllerError) as ei:
                mc.read_reg(1000)
            assert ei.value.code == F.ERR_TIMEOUT
            assert time.monotonic() - t0 < 1.0
    finally:
        s.close()


def test_fifo_stream_square(rig):
    from mlaser.cam.items import CutLine, IO, LaserOff, LaserOn, Rapid
    sim, mc = rig
    sq = [(10, 0), (10, 10), (0, 10), (0, 0)]
    items = [Rapid((0, 0), (0, 0)), IO(2, True), LaserOn()]
    prev = (0, 0)
    for p in sq:
        items.append(CutLine(prev, p, feed=400.0))
        prev = p
    items += [LaserOff(), Rapid((0, 0), (5, 5)), IO(2, False)]
    streamer = FifoStreamer(mc, tick_s=0.001, rapid_speed=800)
    stats = streamer.stream(items, wait_done=True, timeout_s=10)
    assert stats.frames >= 2
    assert sim.wait_idle(5)
    st = mc.read_status()
    assert st.axes[0].position == 5000 and st.axes[1].position == 5000
    assert not st.output(2)
    ops = [op for op, _ in sim.state.history]
    assert ops.count(9999) == 2
    lasers = [F.decode_step(p)[3] for op, p in sim.state.history if op == F.OP_STEP]
    assert any(lasers) and not lasers[-1]
    assert st.fifo_seq == stats.frames + 0  # seq started at RO[15]+1 = 1


def test_fifo_flow_control():
    sim = CardSimulator(fifo_capacity=6000).start()
    try:
        cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200)
        with MotionController(config=cfg) as mc:
            items = [F.encode_step(10, 0) for _ in range(1500)]   # 1500 periods
            st = FifoStreamer(mc).stream(items, wait_done=True, timeout_s=10)
            assert st.items == 1500
            assert sim.wait_idle(5)
            assert sim.position(0) == 15000
    finally:
        sim.stop()
