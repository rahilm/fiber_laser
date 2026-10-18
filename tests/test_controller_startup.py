"""Start-up sequence, fast-status map and reply decoding (parity items D-10, D-11, D-13, D-14).

Expected values come from the ORIGINAL MainApp's start-up traffic captured by the parity
harness (parity/out/app_startup, docs/parity.md §5) and from NCModule FUN_10029a90,
FUN_1002e500, FUN_1003ee50, FUN_10052c30.
"""
from __future__ import annotations

import pytest

from mlaser.protocol import frames as F
from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.controller.startup import (FAST_MAP, AxisDownload, CardParameters, default_fast_map,
                                       download_requests, pack_fast_map, unpack_fast_map)
from mlaser.formats.params import MachineParams


# ------------------------------------------------------------------ decoder (D-10, D-11)
def _reply(func: int, payload: bytes, tid: int = 7) -> bytes:
    body = bytes([0, func]) + payload
    return tid.to_bytes(2, "big") + b"\0\0" + len(body).to_bytes(2, "big") + body


@pytest.mark.parametrize("raw, data", [
    (_reply(0xB0, b"\x02"), [0xB0, 2]),                        # exception keeps 0x80
    (_reply(0xC0, b"\x04"), [0xC0, 4]),
    (_reply(0x20, b"\x03\x00\x07\x08\x09"), [0x20, 0, 3]),     # bytes not appended
    (_reply(0x21, b"\x03\x00\x07\x08\x0b"), [0x21, 3]),        # 0x21 takes the default branch
    (_reply(0x55, b"\x07"), [0x55, 7]),                        # unknown func: [func, byte8]
    (_reply(0x26, b"\xf4\x01\x00\x00\x02\x00"), [0x26, 500, 2]),
    (_reply(0x40, b"\x65\x00\x00\x00\x04\x00"), [0x40, 101, 4]),
    (_reply(0x30, b"\xe8\x03\x00\x00\x01\x00\x2a\x00\x00\x00"), [0x30, 1000, 1, 42]),
])
def test_decode_matches_fun_10029a90(raw, data):
    assert F.decode_response(raw, 7).as_data() == data


def test_decode_error_codes():
    with pytest.raises(F.FrameError) as e:
        F.decode_response(_reply(0x20, b"\x03\x00\x07\x08"), 7)
    assert e.value.code == 6
    with pytest.raises(F.FrameError) as e:
        F.decode_response(_reply(0x30, b"\xe8\x03\x00\x00\x02\x00\x01\x00\x00\x00"), 7)
    assert e.value.code == 5


# ------------------------------------------------------------------ fast map (D-13)
def test_fast_map_layout():
    assert FAST_MAP[:3] == [2000, 2002, 2004] and FAST_MAP[49] == 2098
    assert FAST_MAP[50:52] == [50200, 50202] and FAST_MAP[64] == 50240 and FAST_MAP[-1] == 50386
    words = pack_fast_map(default_fast_map())
    assert len(words) == 60
    assert words[0] == 131205072 == (2002 << 16 | 2000)      # first captured word
    assert unpack_fast_map(words) == FAST_MAP


@pytest.fixture
def rig():
    sim = CardSimulator().start()
    cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200, fifo_time_ms=600, max_recv=2)
    mc = MotionController(config=cfg).open()
    yield sim, mc
    mc.close()
    sim.stop()


def test_simulator_accepts_fast_map_and_serves_fast_status(rig):
    sim, mc = rig
    mc.write_fast_map()
    assert sim.state.fast_map_writes == 1 and sim.state.fast_map == FAST_MAP
    sim.state.axes[1].pos = 12345
    fast = mc.read_status_fast()
    assert len(fast) == 120
    assert fast[10 + F.AX_POSITION] == 12345                 # axis 1 word 2 = reg 2000 + 2*12
    assert fast[50:64] == mc.read_axis_params()[0]
    st = mc.poll()                                           # FUN_1004eab0 fast branch
    assert st.axes[1].position == 12345 and len(mc.axis_rw) == 5


# ------------------------------------------------------------------ start-up (D-14)
CAPTURED_AXIS_BLOCK = [131088, 4294966796, 1000000, 20000, 200000, 30000, 20000, 10000, 0,
                       100000, 10000, 10000, 0, 0]


def test_download_defaults_match_capture():
    reqs = download_requests(CardParameters.from_machine_params(MachineParams.defaults()))
    regs = [r for r, _ in reqs]
    assert regs == [5001, 50026, 50028, 50036, 101, 50200, 50240, 50280, 50320, 50360, 50022,
                    101, 101, 101, 50012, 50040, 50042, 50044, 50046, 50048, 50050, 50106, 50008, 100]
    d = dict(reqs[:5])
    assert d[5001] == [9999, 9, 0xFFFF, 0] and d[50026] == [268435456] and d[50036] == [500]
    assert reqs[5][1] == CAPTURED_AXIS_BLOCK
    assert reqs[10][1] == [20000, 200000]
    assert [w for r, w in reqs if r == 101] == [[9999, 5, 0, 0], [9999, 4, 0, 0], [9999, 4, 1, 0],
                                                [9999, 2, 0xFFFF, 0]]
    assert reqs[-1] == (100, [9999])


def test_axis_block_options():
    ax = AxisDownload(go_origin_dir=1, rotating=True, negative_input=3, forward_input=4,
                      origin_input=5, brake_output=6)
    w = ax.block(1000)
    assert w[0] == (0x20000 | 0x400 | (1 << 5) | (1 << 4))
    assert (w[1], w[2]) == ((-1000000) & 0xFFFFFFFF, 500)
    assert w[12] == (5 << 16 | 4 << 8 | 3) and w[13] == 6
    p = CardParameters(dual_drive=True, pwm_sync=True, do_laser=7, zf_type=1, zf_axis=2, af_type=1)
    d = download_requests(p)
    assert (50028, [2 << 8 | 1, 2000, 1000]) in d and (50106, [7]) in d
    assert (50012, [3]) in d and (101, [118, 5, 0]) in d and (50008, [1]) in d


def test_startup_sequence_without_licence_challenge(rig):
    sim, mc = rig
    seen = []
    orig = sim.handle
    sim.handle = lambda data: (seen.append(F.decode_request(data)), orig(data))[1]
    mc.startup()
    keys = [(q.func, q.addr, q.count) for q in seen]
    assert keys[:2] == [(0x30, 1000, 2), (0x40, 101, 4)]
    assert all(q.addr != 59500 for q in seen)                # licence gate: never sent
    tail = keys[-6:]
    assert tail == [(0x40, 100, 1), (0x40, 103, 1), (0x30, 1032, 1), (0x40, 60000, 60),
                    (0x40, 103, 1), (0x30, 63000, 128)]
    assert mc.fast_len == 120 and mc.fifo_capacity == sim.fifo_capacity
    assert sim.state.params[50200] == CAPTURED_AXIS_BLOCK[0]
    seen.clear()
    mc.poll()
    assert [(q.addr, q.count) for q in seen] == [(1000, 36), (50000, 26), (60001, 120)]
