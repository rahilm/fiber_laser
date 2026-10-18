"""Encoders/decoders of the FIFO process items (NCModule FUN_10043c20 and helpers)."""
from __future__ import annotations

import pytest

from mlaser.protocol import frames as F


def _hdr(w):
    return w[0]


def test_headers_match_builder_constants():
    # header constants pushed by FUN_10043c20 / FUN_1003ec20 / FUN_1003f3e0 / FUN_1003f470
    assert _hdr(F.encode_z_abs(300, 20)) == 0x80067
    assert _hdr(F.encode_follow_on(200, 1000)) == 0x80068
    assert _hdr(F.encode_follow_to(200, 1, 300, 9)) == 0x100069
    assert _hdr(F.encode_frog_jump(1, 2, 3, 4, 5)) == 0x18006A
    assert _hdr(F.encode_z_gradual(10, 2, 1)) == 0x10006C
    assert _hdr(F.encode_z_rel(300, -20)) == 0x8006D
    assert _hdr(F.encode_crash_protect(0, 50)) == 0xC0076
    assert _hdr(F.wait_ms(100)) == 0x807D1
    assert _hdr(F.encode_reg_write(101, [3, 1, 2])) == 0x1807D4
    assert _hdr(F.encode_reg_wait(1010, 10050)) == 0x1407D4
    assert _hdr(F.encode_mark()) == 0xBB9
    assert _hdr(F.encode_laser(5000, 60)) == 0x10270F
    assert _hdr(F.encode_da(0, 5000)) == 0xC270F
    assert _hdr(F.encode_laser_sync(1000)) == 0x8270F
    assert _hdr(F.encode_axis_move(4, 10000, 1000, 5000)) == 0x140003
    assert F.OP_STOP == F.OP_Z_ABS == 103     # legacy alias kept


@pytest.mark.parametrize("mode,a,b,c,cond", [
    (F.WAIT_TIME, 250, 0, 0, 250),
    (F.WAIT_INPUT, 0, 5, 1, ((5 | 0x10000) << 8) | 1),
    (F.WAIT_AXIS, 0, 0x10, 0, ((0x10 | 0x20000) << 8)),
    (F.WAIT_ZF, 0, 0, 2, 0x3000002),
])
def test_wait_condition_encoding(mode, a, b, c, cond):
    w = F.encode_wait(mode, a, b, c, timeout_ms=1234)
    assert w == [0x807D1, cond, 1234]
    d = F.decode_wait(w[1:])
    assert d["mode"] == mode and d["timeout_ms"] == 1234
    if mode == F.WAIT_TIME:
        assert d["ms"] == a
    elif mode == F.WAIT_ZF:
        assert d["state"] == c
    elif mode == F.WAIT_INPUT:
        assert (d["input"], d["value"]) == (b, c)
    else:
        assert (d["axis"], d["value"]) == (b, c)
    assert F.encode_wait(7) == []


def test_follower_items_round_trip():
    w = F.encode_follow_to(200.0, 1.0, 300.0, 9.0, vib_thin=3, vib_thick=5)
    assert w[1:] == [2000, (5 << 8 | 3) << 16 | 1000, 3000, 9000]
    d = F.describe_item(F.OP_FOLLOW_TO, w[1:])
    assert d["kind"] == "follow_to" and d["height"] == 1.0 and d["rest"] == 9.0 and d["vib"] == (3, 5)
    d = F.describe_item(F.OP_Z_REL, F.encode_z_rel(30.0, -20.0)[1:])
    assert d == {"op": 109, "kind": "z_rel", "speed": 30.0, "delta": -20.0}
    d = F.describe_item(F.OP_Z_GRADUAL, F.encode_z_gradual(12.5, 3.8, 1.2)[1:])
    assert d["speed"] == 12.5 and d["delta"] == 3.8 and d["target"] == 1.2 and d["flag"] == 1
    d = F.describe_item(F.OP_FOLLOW_ON, F.encode_follow_on(200, 1200, 1, 0)[1:])
    assert d["height_units"] == 1200 and d["vib"] == (1, 0)
    assert F.split_follower_word(F.follower_word(1500, 7, 9)) == (1500, 7, 9)
    # ftol truncates toward zero like MSVC _ftol
    assert F.encode_z_rel(0.06, -0.0019)[1:] == [0, -1 & 0xFFFFFFFF]


def test_sys_items():
    assert F.encode_laser(3000, 60)[1:] == [F.SYS_PWM, 3000, 60, 1]
    assert F.encode_laser(3000, 0)[1:] == [F.SYS_PWM, 3000, 0, 0]
    assert F.encode_output(0, True)[1:] == [F.SYS_OUTPUTS, 1, 1]
    assert F.encode_output(9, False)[1:] == [F.SYS_OUTPUTS, 1 << 9, 0]
    assert F.encode_output(12, True)[1:] == [F.SYS_OUTPUTS_EXT, 1 << 2, 1 << 2]
    assert F.encode_output(26, True) == []
    d = F.describe_item(F.OP_SYS, F.encode_output(12, True)[1:])
    assert d["outputs"] == {13: True}
    d = F.describe_item(F.OP_SYS, F.encode_laser_sync(1000)[1:])
    assert d["kind"] == "laser_sync" and d["src"] == 1000
    d = F.describe_item(F.OP_FOLLOW, F.encode_crash_protect(20, 50.0)[1:])
    assert d["kind"] == "crash_protect" and d["args"] == [20, 50]


def test_process_items_parse_as_fifo_stream():
    items = [F.encode_mark(), F.wait_ms(100), F.encode_follow_to(200, 5, 300), F.wait_zf(4, 20000),
             F.encode_laser(500, 50), F.encode_z_gradual(4, 3.8, 1.2), F.encode_step(3, -2, 5000, 60),
             F.encode_z_rel(200, -20), F.encode_axis_move(4, 10000, 1000, -3000)]
    words = [w for it in items for w in it]
    parsed = F.parse_fifo_items(words)
    assert [op for op, _ in parsed] == [w[0] & 0xFFFF for w in items]
    kinds = [F.describe_item(op, p)["kind"] for op, p in parsed]
    assert kinds == ["mark", "wait", "follow_to", "wait", "laser", "z_gradual", "step", "z_rel", "axis_move"]
    frames = F.build_fifo_frames(items, 1)
    assert sum(len(f) - 1 for f in frames) == len(words)
