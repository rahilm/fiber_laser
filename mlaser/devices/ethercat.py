"""EtherCAT / extension card ("EC", "AdvEC", "EC3710"), CECNetHalAPI.

Observed (Module/NCModule.dll):

* Transport: CECNetHalAPI (vtable 0x100880fc) with device type 0xb = CExtCardModbus, i.e.
  **Modbus RTU frames for slave 2** sent as UDP datagrams (TCP if AccessType=0).  Endpoint by
  card variant (FUN_1004ed60): variant 1 -> ``EC3710IP:EC3710Port`` (10.1.1.170:502), variant 3
  -> ``ECIP:ECPort`` (10.1.1.168:888); ``AdvECIP``/``AdvECPort`` (:666) exist in the ini only.
  The card variant is ``RO[1] / 10000`` when ``RO[1] % 10000 >= 6000``, else 3 when bits 17..18 of
  ``RO[0]`` are 2, else 1 (FUN_1004ed60).
* Addressing (syncWriteECReg FUN_1004c280, syncWriteECRegs FUN_1004c640, syncReadECReg
  FUN_1004cc20): the register address gets ``+50000`` when the card variant (VM+0x1148) is 1 or
  >= 5, or when ``sys+0x4ac8 == 3``; otherwise it is used as is and the HAL waits 500 ms
  (0x1f4) between send and receive.  Values are 32-bit, written as (lo16, hi16) word pairs with
  function 0x10.
* Command mailbox = register 101 (0x65), the same convention as the motion card:

  ==========================  ===========================================  ==============
  CNCModule slot              words written to 101                          function
  ==========================  ===========================================  ==============
  +0xb0 "EC Jog: Speed %.2f,  ``[4, round(speed*1000), round(length*1000)]``  FUN_10037e70
  Length %.2f"                (speed > 0)
  +0xb4/+0xb8 "EC Pulse Jog"  ``[4, speed, length]`` (ints, speed > 0)        FUN_10038030
  +0xbc "EC Stop"             single register write ``101 := 1``             FUN_100381c0
  +0xc4 "EC Go Origin"        ``[2, 1 if sys+0x4bbc == 1 else 0]``           FUN_100382b0
  ==========================  ===========================================  ==============

  CNCModule +0xa4 forwards to VM +0x90 (syncWriteECRegs), +0xa8 to VM +0x94 (syncWriteECReg).
* Status (updateECStatus FUN_10050900): read 30 words at 1000 (51000 with the +50000 rule)
  -> 15 x 32-bit values stored at VM+0x5a4 = ``getReg(11, i)``.  Properties (updateECProp
  FUN_10050c70): read ``2n`` words at 2000 (52000) -> VM+0x5e0 = ``getReg(12, i)``; ``n`` comes
  from a VM slot (+0x98) and is UNKNOWN.  Meaning of the individual values: see docs/re/devices.md.
* MainApp (EC view FUN_00549940) also uses: ``[7]`` to 101 (CNCModule +0xc8, UNKNOWN),
  ``writeECReg(104, freq)`` / ``writeECReg(105, duty)`` PWM (+0xd8/+0xdc), register 103
  (+0xcc), ``writeECReg(100, 8888/9999)``, reads of 1012 and 2026.  Status ``getReg(11, i)``:
  0 product mark, 1 software version, 2 DI, 3 DO, 4 alarm, 5 run status (bit 31 homed),
  6 command, 7 position x0.001, 8 speed x0.001, 9 PWM frequency, 10 PWM duty.
* Unit of jog speed/length: the double is multiplied by 1000.0 (0x1008adc8) - INFERRED mm -> um.
"""
from __future__ import annotations

from typing import Optional

from .codecs import EXT_CARD_MODBUS, join_u32
from .modbus_rtu import RtuClient, RtuSimulator
from .transport import NetTransport, Transport

REG_MAILBOX = 101
REG_STATUS = 1000
N_STATUS = 15
REG_PROPS = 2000
HIGH_OFFSET = 50000

CMD_STOP = 1
CMD_HOME = 2
CMD_JOG = 4
CMD_7 = 7
REG_CONTROL = 100
REG_PWM_FREQ = 104
REG_PWM_DUTY = 105
STATUS_FIELDS = {0: ("product", 1), 1: ("software_version", 1), 2: ("inputs", 1), 3: ("outputs", 1),
                 4: ("alarm", 1), 5: ("run_status", 1), 6: ("command", 1), 7: ("position", 0.001),
                 8: ("speed", 0.001), 9: ("pwm_frequency", 1), 10: ("pwm_duty", 1)}


class EtherCATBridge:
    """Client for the EC card.  ``high`` selects the ``+50000`` address form."""

    def __init__(self, transport: Transport, high: bool = False):
        self.high = high
        self.rtu = RtuClient(transport, EXT_CARD_MODBUS, "EC", write_delay_ms=0 if high else 500)

    @classmethod
    def connect(cls, host: str = "10.1.1.168", port: int = 888, access: str = "udp",
                timeout_ms: int = 500, high: bool = False) -> "EtherCATBridge":
        return cls(NetTransport(host, port, access, timeout_ms, 3, 1, 10), high)

    def close(self) -> None:
        self.rtu.close()

    def addr(self, a: int) -> int:
        return a + HIGH_OFFSET if self.high else a

    # registers
    def write_reg(self, addr: int, value: int) -> None:
        self.rtu.write_u32(self.addr(addr), [value])

    def write_regs(self, addr: int, values) -> None:
        self.rtu.write_u32(self.addr(addr), list(values))

    def read_regs(self, addr: int, n: int) -> list[int]:
        return self.rtu.read_u32(self.addr(addr), n)

    # commands
    def jog(self, speed: float, length: float) -> None:
        """FUN_10037e70 (speed, length in machine units; sent x1000)."""
        if speed <= 0:
            return
        self.write_regs(REG_MAILBOX, [CMD_JOG, round(speed * 1000.0), round(length * 1000.0)])

    def pulse_jog(self, speed: int, length: int) -> None:
        """FUN_10038030."""
        if speed <= 0:
            return
        self.write_regs(REG_MAILBOX, [CMD_JOG, int(speed), int(length)])

    def stop(self) -> None:
        """FUN_100381c0: syncWriteECReg(101, 1)."""
        self.write_reg(REG_MAILBOX, CMD_STOP)

    def home(self, positive: bool = False) -> None:
        """FUN_100382b0: ``[2, sys+0x4bbc == 1]`` (INFERRED: homing direction flag)."""
        self.write_regs(REG_MAILBOX, [CMD_HOME, 1 if positive else 0])

    def set_pwm(self, frequency: int, duty: int) -> None:
        """CNCModule +0xd8/+0xdc: registers 104 / 105."""
        self.write_reg(REG_PWM_FREQ, int(frequency))
        self.write_reg(REG_PWM_DUTY, int(duty))

    def save(self) -> None:
        self.write_reg(REG_CONTROL, 9999)

    def read_status(self) -> list[int]:
        return self.read_regs(REG_STATUS, N_STATUS)

    def position(self) -> float:
        return self.read_status()[7] * 0.001

    def read_props(self, n: int) -> list[int]:
        return self.read_regs(REG_PROPS, n)


class ECSimulator(RtuSimulator):
    """Slave-2 register file with a single-axis model: jog moves ``position`` (status word 0,
    INFERRED placement) by ``length``; home zeroes it; the last command is kept."""

    def __init__(self, high: bool = False):
        super().__init__(2, self._on_write)
        self.base = HIGH_OFFSET if high else 0
        self.position = 0
        self.commands: list[list[int]] = []

    def _on_write(self, addr: int, words: list[int]) -> None:
        if addr != self.base + REG_MAILBOX:
            return
        vals = join_u32(words)
        self.commands.append(vals)
        if vals[0] == CMD_JOG and len(vals) >= 3:
            self.position += vals[2]
        elif vals[0] == CMD_HOME:
            self.position = 0
        self.set_u32(self.base + REG_STATUS + 14, self.position)
