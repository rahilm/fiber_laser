"""IntEnum / IntFlag codes (mlaser.protocol.enums, model.GlyphKind, chf GraphKind).

The old module constants are now enum members; their integer values must be exactly the values
they had before (copied from the code before Wave 1), and they must behave like plain ints in
everything that produces wire bytes or file text.  The goldens prove the streams themselves.
"""
from __future__ import annotations

import importlib
import json
import struct
from enum import IntEnum

import pytest

OLD_VALUES = {'mlaser.controller.alarms': {'AX_DUAL': 5,
                              'AX_HARD_NEG': 1,
                              'AX_HARD_POS': 0,
                              'AX_SERVO': 4,
                              'AX_SOFT_NEG': 3,
                              'AX_SOFT_POS': 2,
                              'W1_BUS_FAULT': 25,
                              'W1_ESTOP': 30,
                              'W1_OUTPUT_FAULT': 26,
                              'W1_SERVO': 29,
                              'W2_FIFO_STARVATION': 5,
                              'W2_OVERSPEED': 7},
 'mlaser.model': {'GLYPH_ARC': 3,
                  'GLYPH_CIRCLE': 4,
                  'GLYPH_ELLIPSE': 5,
                  'GLYPH_LINE': 2,
                  'GLYPH_LWPOLY': 6,
                  'GLYPH_POINT': 1,
                  'GLYPH_SPLINE': 7},
 'mlaser.nc.process': {'CLS_ADV_FIX_HEIGHT': 17,
                       'CLS_AFTER_GRADUAL': 19,
                       'CLS_DWELL_3002': 3,
                       'CLS_FINAL_LIFT': 9,
                       'CLS_FIX_HEIGHT': 10,
                       'CLS_FOLLOW': 7,
                       'CLS_LIFT': 8,
                       'CLS_MARKER': 14,
                       'CLS_MOTION': 0,
                       'CLS_PEAK': 11,
                       'CLS_PIERCE_END': 5,
                       'CLS_PIERCE_START': 4,
                       'CLS_PRE_JUMP': 18,
                       'CLS_SYS': 1,
                       'CLS_WAIT_ZF': 6,
                       'STAGE_CLEAN': 15,
                       'STAGE_SMOOTH': 14,
                       'SUB_CRASH': 6,
                       'SUB_DA': 3,
                       'SUB_DELAY': 5,
                       'SUB_DO': 4,
                       'SUB_LASER': 1,
                       'SUB_LASER_SYNC': 8},
 'mlaser.protocol.frames': {'AX_EDGE_LATCH': 7,
                            'AX_FEEDBACK': 4,
                            'AX_LATCH': 6,
                            'AX_POSITION': 2,
                            'AX_STATUS': 0,
                            'AX_VELOCITY': 1,
                            'FIFO_CLEAR': 1,
                            'FIFO_START': 2,
                            'FIFO_STOP': 3,
                            'FUNC_READ': 48,
                            'FUNC_WRITE': 64,
                            'FUNC_WRITE_BYTES': 32,
                            'FUNC_WRITE_BYTES2': 33,
                            'FUNC_WRITE_BYTES_AT': 38,
                            'OP_104': 104,
                            'OP_105': 105,
                            'OP_106': 106,
                            'OP_108': 108,
                            'OP_109': 109,
                            'OP_2004': 2004,
                            'OP_3002': 3002,
                            'OP_AXIS_MOVE': 3,
                            'OP_FOLLOW': 118,
                            'OP_FOLLOW_ON': 104,
                            'OP_FOLLOW_TO': 105,
                            'OP_FROG_JUMP': 106,
                            'OP_ITEM3': 3,
                            'OP_JOG': 3,
                            'OP_MARK': 3001,
                            'OP_MOVE': 5,
                            'OP_REG': 2004,
                            'OP_STEP': 3000,
                            'OP_STOP': 103,
                            'OP_STOP_AXES': 1,
                            'OP_SYS': 9999,
                            'OP_WAIT': 2001,
                            'OP_Z_ABS': 103,
                            'OP_Z_GRADUAL': 108,
                            'OP_Z_REL': 109,
                            'SYS_18': 18,
                            'SYS_9': 9,
                            'SYS_AXIS_CFG': 1,
                            'SYS_DA': 4,
                            'SYS_INIT': 5,
                            'SYS_LASER_SYNC': 18,
                            'SYS_OUTPUTS': 2,
                            'SYS_OUTPUTS_EXT': 13,
                            'SYS_PWM': 3,
                            'SYS_PWM_BUS': 17,
                            'SYS_ZERO': 8,
                            'WAIT_AXIS': 2,
                            'WAIT_DEFAULT_TIMEOUT_MS': 3000,
                            'WAIT_INPUT': 1,
                            'WAIT_TIME': 0,
                            'WAIT_ZF': 3,
                            'ZF_ARRIVED': 2,
                            'ZF_FOLLOWING': 3,
                            'ZF_FOLLOW_TO_DONE': 4}}


@pytest.mark.parametrize("module", sorted(OLD_VALUES))
def test_old_names_keep_their_values(module):
    mod = importlib.import_module(module)
    for name, value in OLD_VALUES[module].items():
        v = getattr(mod, name)
        assert int(v) == value and v == value, (module, name)
        assert type(v) is int or isinstance(v, IntEnum), (module, name)


def test_enum_members_format_like_ints():
    from mlaser.model import GlyphKind
    from mlaser.protocol import frames as F
    from mlaser.protocol.enums import ManuClass, Op, SysCmd

    for m in (Op.STEP, SysCmd.PWM, ManuClass.PEAK, GlyphKind.SPLINE):
        assert str(m) == str(int(m)) and f"{m}" == str(int(m)) and "%d" % m == str(int(m))
        assert json.dumps([m]) == f"[{int(m)}]"
        assert struct.pack("<i", m) == struct.pack("<i", int(m))
    assert F.fifo_item(F.OP_MARK) == F.fifo_item(3001)
    assert F.mailbox_sys(F.SYS_PWM, 5000, 50, 1) == F.mailbox_sys(3, 5000, 50, 1)
    assert F.encode_step(1, -1) == F.encode_step(1, -1)
