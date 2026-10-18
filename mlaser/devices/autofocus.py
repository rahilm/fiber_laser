"""Autofocus heads ("AF" / "AdvAF", electric focus), CAFNetHalAPI.

Observed (Module/NCModule.dll):

* Transport: CAFNetHalAPI (vtable 0x100880d4), device type 9 = CSerialModbus, i.e. **Modbus RTU
  frames for slave 1** in UDP datagrams (TCP if AccessType=0) to ``AFIP:AFPort``
  (10.1.1.168:888) or, when ``sys+0x4ad4`` is set, ``AdvAFIP:AdvAFPort`` (:666) (FUN_1004ed60).
  Log tags ``"AF Send Buf: "`` / ``"AF Recv Buf: "`` (CAFNetHalAPI::readReg FUN_1001dc20).
* The AF type parameter ``AF.AFType`` = ``sys+0x4ac8`` (MainApp: 0 none, 1 MCC serial, 2 net,
  3 DA, 4/7 on-board) selects the path (syncReadAFReg FUN_1004bee0,
  syncWriteAFReg FUN_1004b510, updateAFStatus FUN_1004f980, updateAFProp FUN_100503e0):
  1 -> AF HAL at VM+0x2c, 2 -> AF HAL at VM+0x28 (network); otherwise (3/5/6) the head sits
  behind the motion card and is read with MC function 0x30 at **13000** (status, 14 x u32) and
  **13200** (properties).  0 = no AF.
* Network register access: read ``[3, addr, 2]`` and rebuild ``w1 << 16 | w0``; write
  ``[0x10, addr, 2, lo, hi]``; a write is retried every 30 ms while the reply mismatches.
  Status: 28 words at **1000** -> 14 values (``getReg(9, i)``, VM+0x458); properties: words at
  **2000** -> ``getReg(10, i)`` (VM+0x490), count from a VM slot (UNKNOWN).
* Firmware download "FTC Upgrade" (ribbon ``hw.upgrade_ftc``, lang mf440; fillAFDownFile
  FUN_10053fb0, ``MaxAFDownItem=200``, ``AFUpdateTimeout``) is described only, not implemented.

MainApp usage (AF view, FUN_004ef420 / FUN_00516d80 / FUN_004fbeb0 ...), observed call sites:

* register **101** = command: ``1`` stop (FUN_004f26c0), ``2`` go home (FUN_004fbeb0),
  ``5`` (FUN_004f27f0, meaning UNKNOWN, INFERRED reset), ``[4, pos_mm*1000, speed*1000]`` move to
  an absolute focus position (jog up/down/position FUN_004fc449/004fcd29/004fd4d0, target =
  current position +- step), ``[3, x*1000, 0]`` (FUN_004f2840, UNKNOWN).
* register **100**: ``9999`` save, ``8888`` handshake/reset; register **2020** origin offset
  (read, adjusted, limit 18000, FUN_004f29e0 / FUN_004f2dd0).
* Status block (``getReg(9, i)``): see :data:`STATUS_FIELDS`; index 5 bit 31 = homed.
"""
from __future__ import annotations

from typing import Optional


from .codecs import SERIAL_MODBUS
from .modbus_rtu import RtuClient, RtuSimulator
from .transport import NetTransport, Transport

REG_STATUS = 1000
N_STATUS = 14
REG_PROPS = 2000
CARD_REG_STATUS = 13000      # DAT_1008b7a4 = 0x32c8
CARD_REG_PROPS = 13200       # DAT_1008b7a8 = 0x3390
REG_COMMAND = 101
REG_CONTROL = 100
REG_ORIGIN = 2020
CMD_STOP, CMD_HOME, CMD_ABS, CMD_MOVE, CMD_5 = 1, 2, 3, 4, 5
CONTROL_SAVE, CONTROL_HELLO = 9999, 8888

# getReg(9, i): (name, scale)   MainApp FUN_004ef420 / FUN_00516d80
STATUS_FIELDS = {0: ("model", 1), 1: ("software_version", 1), 2: ("inputs", 1), 4: ("alarm", 1),
                 5: ("run_status", 1), 6: ("command", 1), 7: ("position_mm", 0.001),
                 8: ("speed", 0.001), 9: ("value9", 0.001), 10: ("value10", 0.1),
                 11: ("value11", 0.001), 13: ("value13", 0.001)}


class AutoFocus:
    """Network AF head (types 1/2)."""

    def __init__(self, transport: Transport):
        self.rtu = RtuClient(transport, SERIAL_MODBUS, "AF")

    @classmethod
    def connect(cls, host: str = "10.1.1.168", port: int = 888, access: str = "udp",
                timeout_ms: int = 500) -> "AutoFocus":
        return cls(NetTransport(host, port, access, timeout_ms, 3, 1, 10))

    def close(self) -> None:
        self.rtu.close()

    def read_reg(self, addr: int) -> int:
        return self.rtu.read_u32(addr, 1)[0]

    def write_reg(self, addr: int, value: int, retries: int = 3) -> None:
        from .modbus_rtu import DeviceError, ERR_MISMATCH
        import time
        for i in range(retries):
            try:
                return self.rtu.write_u32(addr, [value])
            except DeviceError as e:
                if e.code != ERR_MISMATCH or i == retries - 1:
                    raise
                time.sleep(0.03)

    def read_status(self) -> list[int]:
        return self.rtu.read_u32(REG_STATUS, N_STATUS)

    def read_props(self, n: int) -> list[int]:
        return self.rtu.read_u32(REG_PROPS, n)

    # commands (register 101)
    def stop(self) -> None:
        self.write_reg(REG_COMMAND, CMD_STOP)

    def home(self) -> None:
        self.write_reg(REG_COMMAND, CMD_HOME)

    def move_to(self, position_mm: float, speed: float) -> None:
        """``writeAFRegs(101, [4, pos*1000, speed*1000])``."""
        self.rtu.write_u32(REG_COMMAND, [CMD_MOVE, round(position_mm * 1000), round(speed * 1000)])

    def position_mm(self) -> float:
        return self.read_status()[7] * 0.001

    def homed(self) -> bool:
        return bool(self.read_status()[5] & 0x80000000)

    def save(self) -> None:
        self.write_reg(REG_CONTROL, CONTROL_SAVE)

    def set_origin_offset(self, value: int) -> None:
        """FUN_004f29e0: register 2020 (limit 18000), then save + handshake."""
        if not -18000 <= value <= 18000:
            raise ValueError("origin offset limited to 18000")
        self.write_reg(REG_ORIGIN, value)
        self.write_reg(REG_CONTROL, CONTROL_SAVE)
        self.write_reg(REG_CONTROL, CONTROL_HELLO)


class CardAutoFocus:
    """AF head behind the motion card (types 3/5/6): plain MC register reads."""

    def __init__(self, mc):
        self.mc = mc

    def read_status(self) -> list[int]:
        return self.mc.read_regs(CARD_REG_STATUS, N_STATUS)

    def read_props(self, n: int) -> list[int]:
        return self.mc.read_regs(CARD_REG_PROPS, n)


class AFSimulator(RtuSimulator):
    """Slave-1 register file with a focus-axis model: command 4 moves (status 7), 2 homes
    (status 7 := 0, status 5 bit 31 set)."""

    def __init__(self):
        super().__init__(1, self._on_write)

    def _on_write(self, addr: int, words: list[int]) -> None:
        from .codecs import join_u32
        if addr != REG_COMMAND:
            return
        v = join_u32(words)
        if v[0] == CMD_MOVE and len(v) >= 2:
            self.set_status(7, v[1])
        elif v[0] == CMD_HOME:
            self.set_status(7, 0)
            self.set_status(5, 0x80000000)

    def set_status(self, i: int, value: int) -> None:
        self.set_u32(REG_STATUS + 2 * i, value)
