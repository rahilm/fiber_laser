"""A second, deliberately separate codec of the MCC100 frame format, written from the
documentation only (docs/re/protocol.md §2 "Frame format", §3 register map, §4 mailbox
commands, §5 FIFO streaming, §13-§15 status words).

It must NOT import ``mlaser.protocol`` (or anything else of ``mlaser``): the simulator shares the
client's codec, so a wrong reading of the format would be wrong on both sides and every test
would still pass (review arch.md A4).  Every constant below is restated from the documentation,
with the section it comes from; nothing is computed from the production code.  The CRC is a
bitwise implementation (the production codec uses a table).

Used by tests/test_wire_reference.py and tests/test_capture_replay.py.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- §2 frame format
HEADER = 8                     # tid(2) crc(2) LEN(2) unit(1) func(1)
MAX_DATAGRAM = 0x5A8           # §1: send and receive buffers are 0x5A8 = 1448 bytes
READ, WRITE = 0x30, 0x40       # §2 table: 0x30 read regs, 0x40 write regs
BYTES, BYTES2, BYTES_AT = 0x20, 0x21, 0x26
EXC = 0x80                     # f | 0x80 exception, 1 byte exception code
FUNCTIONS = (READ, WRITE, BYTES, BYTES2, BYTES_AT)

# ---------------------------------------------------------------- §3 register map
REG_CONTROL, REG_MAILBOX, REG_FIFO, REG_FIFO_CTRL = 100, 101, 102, 103
FIFO_CLEAR, FIFO_START, FIFO_STOP = 1, 2, 3          # reg 103: 1 clear, 2 start, 3 stop
RO_BASE, RO_COUNT = 1000, 36                          # status RO block, reg 1000+i, i<36
AXIS_RO_BASE, AXIS_RO_COUNT = 2000, 50                # axis RO, 5 axes x 10
RO_ALARM1, RO_ALARM2 = 6, 7                           # §13 RO[6], RO[7]
RO_FIFO_SEQ, RO_FIFO_FREE, RO_PROC = 15, 16, 19       # §3 RO table, §15

# §15 "RO[19] processing status is a bit field" (owner's captures of the original, 2026-11-07):
# 0 idle -> 0x100 after the first frames -> 0x301 while the job runs -> 0x001 drained -> 0 after
# 103 := 3.  The original reads it as a char: low byte == 1 -> machine state 4.
PROC_DATA, PROC_EXEC, PROC_LOW = 0x100, 0x200, 0xFF
PROC_DOCUMENTED_VALUES = (0x000, 0x100, 0x301, 0x001)

# ---------------------------------------------------------------- §4 mailbox commands
SYS = 9999
SYS_STEP_FORMAT = 1            # [9999, 1, axis_mask | 0x66800000, 0]; §15: 0x66800003 for X|Y
STEP_CARD_FORMAT = 0x66800000
SYS_OUTPUTS = 2                # [9999, 2, mask16, value16] outputs 1..16
SYS_PWM = 3                    # [9999, 3, freq, duty, ...]
SYS_ALARM_RESET = 5            # [9999, 5, 0, 0]
SYS_OUTPUTS_EXT = 13
SYS_JOB_RESET = 16             # §15: [9999, 16] before the job's FIFO clear
SYS_PWM_BUS = 17
STOP_AXES = 1                  # [1, 0x1f, 2, decel, 200000] decelerated stop of all axes
STOP_AXES_JERK = 200000
FOLLOWER = 118                 # [118, 5, 0] follower stop

# ---------------------------------------------------------------- §5 FIFO items
STEP_OP, STEP_HEADER = 3000, 0x80BB8   # interpolation step, 2 parameters
MARK_OP = 3001

# ---------------------------------------------------------------- §13 alarm word 2
ALARM2_BASE = 9000             # RO[7] bit b -> code 9000 + b
ALARM2_INTERP_LENGTH_BIT = 1   # 9001 "interpolation data content length is abnormal" (§15)


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS, bitwise: init 0xFFFF, reflected polynomial 0xA001 (§2: "the standard
    Modbus CRC")."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            lsb = crc & 1
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


class WireError(ValueError):
    pass


@dataclass
class Frame:
    """One decoded frame (request or reply) as the documentation describes it."""
    tid: int
    crc: int                   # the two CRC bytes as a big-endian number (as on the wire)
    crc_order: Optional[str]   # "big" / "little" when bytes[2:4] hold the CRC of bytes[4:], else None
    length: int
    unit: int
    func: int
    addr: Optional[int] = None
    count: Optional[int] = None
    values: list = field(default_factory=list)     # u32 words (0x30 reply / 0x40 request) or bytes
    exception: Optional[int] = None


def _crc_order(frame: bytes) -> Optional[str]:
    c = crc16(frame[4:])
    got = frame[2:4]
    if got == bytes([c >> 8, c & 0xFF]):
        return "big"
    if got == bytes([c & 0xFF, c >> 8]):
        return "little"
    return None


def _head(frame: bytes) -> Frame:
    if len(frame) < HEADER:
        raise WireError(f"{len(frame)} bytes: shorter than the 8-byte header")
    tid = (frame[0] << 8) | frame[1]
    crc = (frame[2] << 8) | frame[3]
    length = (frame[4] << 8) | frame[5]
    if length != len(frame) - 6:
        raise WireError(f"LEN {length} != bytes after the LEN field ({len(frame) - 6})")
    return Frame(tid, crc, _crc_order(frame), length, frame[6], frame[7])


def decode_request(frame: bytes) -> Frame:
    """PC -> card (§2 table, request payload column)."""
    f = _head(frame)
    p = frame[8:]
    if f.func == READ:
        if f.length != 8:
            raise WireError(f"read request LEN {f.length} != 8")
        f.addr, f.count = struct.unpack("<IH", p)
    elif f.func == WRITE:
        f.addr, f.count = struct.unpack_from("<IH", p)
        if f.length != 8 + 4 * f.count:
            raise WireError(f"write request LEN {f.length} != 8 + 4*{f.count}")
        f.values = list(struct.unpack_from(f"<{f.count}I", p, 6))
    elif f.func in (BYTES, BYTES2):
        (f.count,) = struct.unpack_from("<H", p)
        if f.length != 4 + f.count:
            raise WireError(f"byte request LEN {f.length} != 4 + {f.count}")
        f.values = list(p[2:])
    elif f.func == BYTES_AT:
        f.addr, f.count = struct.unpack_from("<IH", p)
        if f.length != 8 + f.count:
            raise WireError(f"byte request LEN {f.length} != 8 + {f.count}")
        f.values = list(p[6:])
    else:
        raise WireError(f"function {f.func:#x} is not in the documented table")
    return f


def decode_reply(frame: bytes) -> Frame:
    """card -> PC (§2 table, reply payload column; exception = func | 0x80 + 1 code byte)."""
    f = _head(frame)
    p = frame[8:]
    if f.func & EXC:
        if f.length != 3:
            raise WireError(f"exception reply LEN {f.length} != 3")
        f.func &= ~EXC
        f.exception = p[0]
    elif f.func == READ:
        f.addr, f.count = struct.unpack_from("<IH", p)
        if f.count * 4 != len(frame) - 14:
            raise WireError("read reply: count*4 != len-14")
        f.values = list(struct.unpack_from(f"<{f.count}I", p, 6))
    elif f.func in (WRITE, BYTES_AT):
        f.addr, f.count = struct.unpack_from("<IH", p)
    elif f.func == BYTES:
        (f.count,) = struct.unpack_from("<H", p)
        f.values = list(p[2:])
    else:
        raise WireError(f"reply function {f.func:#x} is not in the documented table")
    return f


def _frame(tid: int, body: bytes, crc_order: str) -> bytes:
    tail = struct.pack(">H", len(body)) + body
    c = crc16(tail)
    crc = bytes([c >> 8, c & 0xFF]) if crc_order == "big" else bytes([c & 0xFF, c >> 8])
    return struct.pack(">H", tid & 0xFFFF) + crc + tail


def encode_request(tid: int, func: int, addr: int = 0, values=(), count: Optional[int] = None) -> bytes:
    """PC -> card, CRC big-endian (§2: "PC->card: big-endian (hi byte first)")."""
    vals = list(values)
    if func == READ:
        body = struct.pack("<BBIH", 0, READ, addr, count if count is not None else 0)
    elif func == WRITE:
        body = struct.pack("<BBIH", 0, WRITE, addr, len(vals)) + struct.pack(f"<{len(vals)}I", *vals)
    else:
        raise WireError(f"function {func:#x} not supported by the reference encoder")
    return _frame(tid, body, "big")


def encode_reply(tid: int, func: int, addr: int = 0, values=(), count: Optional[int] = None,
                 exception: Optional[int] = None) -> bytes:
    """card -> PC, CRC little-endian (§2: "card->PC: little-endian in the captured reply")."""
    vals = list(values)
    if exception is not None:
        body = bytes([0, func | EXC, exception])
    elif func == READ:
        body = struct.pack("<BBIH", 0, READ, addr, len(vals)) + struct.pack(f"<{len(vals)}I", *vals)
    elif func == WRITE:
        body = struct.pack("<BBIH", 0, WRITE, addr, count if count is not None else len(vals))
    else:
        raise WireError(f"function {func:#x} not supported by the reference encoder")
    return _frame(tid, body, "little")


# ---------------------------------------------------------------- §5 FIFO payload
def fifo_items(words) -> list[tuple[int, list[int]]]:
    """Split the words after the frame sequence word: header = (param_bytes << 16) | opcode."""
    out, i, n = [], 0, len(words)
    while i < n:
        hdr = words[i]
        op, nbytes = hdr & 0xFFFF, hdr >> 16
        if nbytes % 4 or i + 1 + nbytes // 4 > n:
            raise WireError(f"bad item header {hdr:#x} at word {i}")
        out.append((op, list(words[i + 1:i + 1 + nbytes // 4])))
        i += 1 + nbytes // 4
    return out


def _s8(v: int) -> int:
    return v - 256 if v & 0x80 else v


def step_fields(w1: int, w2: int) -> dict:
    """§5 step item::

        w1 = (freq & 0xff) << 24 | duty << 16 | (dy & 0xff) << 8 | (dx & 0xff)
        w2 = (duty ? v8 : 0) << 16 | (freq >> 8)
    """
    return {"dx": _s8(w1 & 0xFF), "dy": _s8((w1 >> 8) & 0xFF), "duty": (w1 >> 16) & 0xFF,
            "freq": (((w2 & 0xFFFF) << 8) | (w1 >> 24)) & 0xFFFF, "v8": (w2 >> 16) & 0xFFFF}


def proc_state(ro19: int) -> int:
    """FUN_10039500 as §3/§15 describe it: low byte == 1 -> machine state 4 (else 0 here)."""
    return 4 if (ro19 & PROC_LOW) == 1 else 0


# ---------------------------------------------------------------- a minimal client
class RefClient:
    """Talks to a card (the simulator) over UDP with this codec only: lets a test check the
    simulator's behaviour without the client's constants or codec."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout_s: float = 1.0):
        import socket
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout_s)
        self.tid = 0

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "RefClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ask(self, frame: bytes) -> Frame:
        self.sock.sendto(frame, self.addr)
        data, _ = self.sock.recvfrom(2048)
        rep = decode_reply(data)
        if rep.tid != self.tid:
            raise WireError(f"reply tid {rep.tid} != request tid {self.tid}")
        self.tid = (self.tid + 1) & 0xFFFF
        return rep

    def read(self, addr: int, count: int) -> list[int]:
        rep = self._ask(encode_request(self.tid, READ, addr, count=count))
        if rep.exception is not None:
            raise WireError(f"read {addr} x {count}: exception {rep.exception}")
        return rep.values

    def write(self, addr: int, *values: int) -> Frame:
        return self._ask(encode_request(self.tid, WRITE, addr, values))

    def ro(self) -> list[int]:
        return self.read(RO_BASE, RO_COUNT)
