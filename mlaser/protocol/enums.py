"""Named codes of the MCC100 protocol and of the NC item stream (Wave 1, architecture.md P1-12).

Every enum here is an ``IntEnum`` / ``IntFlag`` whose values are the integers that go on the wire,
so a member can be used anywhere the bare number was (arithmetic, ``struct.pack``, JSON, ``%d``,
``str()``) and the goldens prove that nothing on the wire changed.  The module-level constants of
:mod:`mlaser.protocol.frames`, :mod:`mlaser.protocol.manu` and :mod:`mlaser.controller.alarms`
(``OP_STEP``, ``CLS_PEAK``, ``AX_SERVO``, ...) are these members under their old names.

Evidence and tags (O observed, I inferred, U unknown) are in the comments next to the old names
and in docs/protocol.md; the addresses are NCModule.dll / MainApp.exe of Mlaser v0.0.1.51_SP1.
"""
from __future__ import annotations

from enum import IntEnum, IntFlag

__port__ = {
    "source": "NCModule",
    "functions": {"FUN_10043c20": "Op / ManuClass / SysSub (FIFO item and ManuItem codes)",
                  "FUN_10028190 / FUN_10029a90": "Func (frame function codes)",
                  "FUN_1003ec20": "WaitMode", "MainApp FUN_00598280": "alarm word bits"},
    "note": "codes only; the values are the wire values",
}


class Func(IntEnum):
    """Frame function codes (byte 7 of a CExtModbus frame)."""
    WRITE_BYTES = 0x20          # byte block without address
    WRITE_BYTES2 = 0x21
    WRITE_BYTES_AT = 0x26       # addr + byte block
    READ = 0x30                 # read N 32-bit registers
    WRITE = 0x40                # write N 32-bit registers


class Op(IntEnum):
    """FIFO item opcodes (low 16 bits of an item header; ``frames.item_header``)."""
    AXIS_MOVE = 3               # 0x140003 focus-axis move (AF types 5/6)
    Z_ABS = 103                 # Z to an absolute height
    FOLLOW_ON = 104             # follower on at the cut height
    FOLLOW_TO = 105             # follow to the pierce height
    FROG_JUMP = 106             # end-cut lift during the rapid (not generated)
    Z_GRADUAL = 108             # gradual height change while the beam is on
    Z_REL = 109                 # relative Z move
    FOLLOW = 118                # follower control via the card (sub-command first)
    WAIT = 2001                 # [condition, timeout_ms]
    REG = 2004                  # register write / wait on the card
    STEP = 3000                 # interpolation step (frames.encode_step)
    MARK = 3001                 # end of a motion block
    DWELL_3002 = 3002           # ManuItem class 3 (never created by MainApp)
    SYS = 9999                  # system command, first parameter = SysCmd


class MailboxOp(IntEnum):
    """Immediate commands written to the mailbox register 101 (besides Op.SYS / Op.FOLLOW)."""
    STOP_AXES = 1               # [1, axis_mask]
    JOG = 3                     # [3, mask|flag, speed, acc, jerk, distance]
    MOVE = 5                    # [5, mask|flag, speed, acc, jerk, p1..p4]


class SysCmd(IntEnum):
    """Sub-commands of Op.SYS (9999), mailbox and FIFO."""
    AXIS_CFG = 1
    OUTPUTS = 2                 # outputs 1..16
    PWM = 3                     # [3, frequency, duty, enable]
    DA = 4                      # [4, channel, value 0..10000]
    INIT = 5
    ZERO = 8
    SYS_9 = 9
    OUTPUTS_EXT = 13            # outputs 11..26
    PWM_BUS = 17                # bus-card PWM
    LASER_SYNC = 18             # [18, src] around every cut block


class WaitMode(IntEnum):
    """Op.WAIT conditions (FUN_1003ec20 mode argument)."""
    TIME = 0
    INPUT = 1
    AXIS = 2
    ZF = 3


class ZfState(IntEnum):
    """Follower states waited for with WaitMode.ZF."""
    ARRIVED = 2                 # after relative / absolute Z moves
    FOLLOWING = 3               # after Op.FOLLOW_ON
    FOLLOW_TO_DONE = 4          # after Op.FOLLOW_TO


class FifoCtrl(IntEnum):
    """Values written to the FIFO control register 103."""
    CLEAR = 1
    START = 2
    STOP = 3


class ManuClass(IntEnum):
    """MainApp ManuItem classes (ManuItem +0x16), the input of NCModule FUN_10043c20."""
    MOTION = 0
    SYS = 1                     # system command, sub-op in +0 (SysSub)
    DWELL_3002 = 3
    PIERCE_START = 4
    PIERCE_END = 5
    WAIT_ZF = 6
    FOLLOW = 7
    LIFT = 8
    FINAL_LIFT = 9
    FIX_HEIGHT = 0xA
    PEAK = 0xB
    MARKER = 0xE
    ADV_FIX_HEIGHT = 0x11
    PRE_JUMP = 0x12
    AFTER_GRADUAL = 0x13


class SysSub(IntEnum):
    """Sub-ops of ManuClass.SYS items (ManuItem +0)."""
    LASER = 1
    DA = 3
    DO = 4
    DELAY = 5
    CRASH = 6
    LASER_SYNC = 8


class PierceStage(IntEnum):
    """Special stage nibbles of a pierce ManuItem (flags bits 4..7)."""
    SMOOTH = 0xE                # smooth pierce (flags | 0xe0)
    CLEAN = 0xF                 # clean-residue spiral (flags | 0xf0), not reproduced


class AxisAlarmBit(IntEnum):
    """Bits of an axis status word raised as per-axis alarms (code 8100 + 32*axis + bit)."""
    HARD_POS = 0
    HARD_NEG = 1
    SOFT_POS = 2
    SOFT_NEG = 3
    SERVO = 4
    DUAL = 5


class AxisAlarm(IntFlag):
    """The same bits as a mask of the axis status word (``AXIS_ALARM_MASK`` = all of them)."""
    HARD_POS = 1 << AxisAlarmBit.HARD_POS
    HARD_NEG = 1 << AxisAlarmBit.HARD_NEG
    SOFT_POS = 1 << AxisAlarmBit.SOFT_POS
    SOFT_NEG = 1 << AxisAlarmBit.SOFT_NEG
    SERVO = 1 << AxisAlarmBit.SERVO
    DUAL = 1 << AxisAlarmBit.DUAL


class AlarmWord1Bit(IntEnum):
    """Named bits of alarm word 1 (RO[6], code 8000 + bit)."""
    BUS_FAULT = 25
    OUTPUT_FAULT = 26
    SERVO = 29
    ESTOP = 30


class AlarmWord2Bit(IntEnum):
    """Bits of alarm word 2 (RO[7], code 9000 + bit)."""
    ILLEGAL_COMMAND = 0
    INTERP_LENGTH = 1
    AXIS_COMMAND = 2
    FTC_COMMAND = 3
    PLC_COMMAND = 4
    FIFO_STARVATION = 5
    IMMEDIATE_COMMAND = 6
    OVERSPEED = 7
