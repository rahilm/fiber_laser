"""MainApp's ManuItem: the 28-byte process/motion record that NCModule encodes into FIFO items.

Moved from ``mlaser.controller.process`` (Wave 1): the record and its class, sub-op and stage
codes are the input vocabulary of the NC encoder (``mlaser.parity_ports.ncmodule.manu``) and of
the process sequences (``mlaser.nc.process``).  Layout confirmed by NCModule FUN_100352b0's
``_tempManuItem`` dump (docs/protocol.md "Process items").
"""
from __future__ import annotations

from dataclasses import dataclass

from .enums import ManuClass, PierceStage, SysSub

__port__ = {
    "source": "MainApp",
    "functions": {"ManuItem": "28-byte record (FUN_100352b0 _tempManuItem dump)"},
}


# --------------------------------------------------------------------------- ManuItem
# classes (ManuItem +0x16)
CLS_MOTION = ManuClass.MOTION
CLS_SYS = ManuClass.SYS
CLS_DWELL_3002 = ManuClass.DWELL_3002
CLS_PIERCE_START = ManuClass.PIERCE_START
CLS_PIERCE_END = ManuClass.PIERCE_END
CLS_WAIT_ZF = ManuClass.WAIT_ZF
CLS_FOLLOW = ManuClass.FOLLOW
CLS_LIFT = ManuClass.LIFT
CLS_FINAL_LIFT = ManuClass.FINAL_LIFT
CLS_FIX_HEIGHT = ManuClass.FIX_HEIGHT
CLS_PEAK = ManuClass.PEAK
CLS_MARKER = ManuClass.MARKER
CLS_ADV_FIX_HEIGHT = ManuClass.ADV_FIX_HEIGHT
CLS_PRE_JUMP = ManuClass.PRE_JUMP
CLS_AFTER_GRADUAL = ManuClass.AFTER_GRADUAL

# class 1 sub-ops (ManuItem +0)
SUB_LASER = SysSub.LASER
SUB_DA = SysSub.DA
SUB_DO = SysSub.DO
SUB_DELAY = SysSub.DELAY
SUB_CRASH = SysSub.CRASH
SUB_LASER_SYNC = SysSub.LASER_SYNC

STAGE_SMOOTH = PierceStage.SMOOTH  # stage nibble used by the smooth pierce (flags | 0xe0)
STAGE_CLEAN = PierceStage.CLEAN  # clean-residue spiral (flags | 0xf0), not reproduced


@dataclass
class ManuItem:
    """MainApp's 28-byte ManuItem (layout confirmed by FUN_100352b0's ``_tempManuItem`` dump).

    ``a`` (+0) is dX or the class-1 sub-op, ``b`` (+4) dY or the port/channel, ``value`` (+0x18)
    the flags (low nibble layer, bits 4..7 pierce stage, bits 8..11 first-stage flag) or the
    class-1 argument.
    """
    cls: int                   # +0x16, a ManuClass value
    a: int = 0                 # +0
    b: int = 0                 # +4
    power: int = 0xFFFF        # +8
    c: int = 0                 # +0xc
    w10: int = 0               # +0x10
    duty: int = 0              # +0x12
    freq: int = 0              # +0x14
    value: int = 0             # +0x18

    @property
    def layer(self) -> int:
        return self.value & 0xF

    @property
    def stage(self) -> int:
        return (self.value >> 4) & 0xF

    @property
    def first(self) -> int:
        return (self.value >> 8) & 0xF


def sys_cmd(sub: int, b: int = 0, value: int = 0, duty: int = 0, freq: int = 0) -> ManuItem:
    return ManuItem(CLS_SYS, a=sub, b=b, value=value, duty=duty, freq=freq)
