"""Wire format of the MCC100 motion card ("CExtModbus" in NCModule.dll).

Everything here is pure data conversion (no sockets).  See ``docs/protocol.md``
for the full write-up and evidence.  Addresses below are virtual addresses in
``Module/NCModule.dll`` (image base 0x10000000) of Mlaser v0.0.1.51_SP1.

Frame layout (observed, encoder FUN_10028190, decoder FUN_10029a90)::

    off  size  field
    0    2     transaction id, big-endian (CMCHalAPI+0x14e, post-incremented per request)
    2    2     CRC-16/MODBUS over bytes[4:]  (PC->card: big-endian; card->PC: little-endian
               in the one captured reply; the PC never checks the reply CRC)
    4    2     LEN, big-endian = number of bytes that follow (unit .. end)
    6    1     unit id, always 0
    7    1     function code
    8    ...   function payload, all multi-byte fields LITTLE-endian

    0x30 read  : addr u32, count u16                       (LEN = 8)
    0x40 write : addr u32, count u16, count * u32 values   (LEN = 8 + 4n)
    0x20/0x21  : count u16, count * u8                     (LEN = 4 + n)   [no address]
    0x26       : addr u32, count u16, count * u8           (LEN = 8 + n)

Replies carry the same header.  0x30 replies: addr u32, count u16, count*u32;
0x40/0x26 replies: addr u32, count u16; 0x20 replies: count u16 + data bytes (0x21 replies
take the decoder's default branch);
exception replies use ``func | 0x80`` followed by one exception-code byte.

The NC module represents a request internally as ``vector<int>`` =
``[func, addr, count, values...]`` (log field ``DataEx``) and a decoded reply as
``[func, addr, count, values...]`` (log field ``Data``; exact shapes per function in
``Response.as_data``); helpers here use the same shapes so log lines can be checked directly.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .enums import FifoCtrl, Func, MailboxOp, Op, SysCmd, WaitMode, ZfState

__port__ = {
    "source": "NCModule",
    "functions": {"FUN_10028190": "encode_request", "FUN_10029a90": "decode_response",
                  "FUN_1001d6d0": "ERR_* (send/receive loop error codes)",
                  "FUN_10043c20": "fifo_item / encode_* (FIFO item layouts)"},
    "parity": ["nc", "app_frames", "machine"],
    "note": "foundation module (protocol layer); the parity ports build on it",
}

# --------------------------------------------------------------------------- functions
FUNC_READ = Func.READ   # read N 32-bit registers            (DAT_1008b7c4 = 0x30)
FUNC_WRITE = Func.WRITE  # write N 32-bit registers           (DAT_1008b7c8 = 0x40)
FUNC_WRITE_BYTES = Func.WRITE_BYTES  # byte block without address (0x20/0x21) - use UNKNOWN
FUNC_WRITE_BYTES2 = Func.WRITE_BYTES2
FUNC_WRITE_BYTES_AT = Func.WRITE_BYTES_AT  # addr + byte block (DAT_1008b7cc = 0x26) - use UNKNOWN
EXCEPTION_FLAG = 0x80

HEADER_LEN = 8          # tid(2) crc(2) len(2) unit(1) func(1)
MAX_FRAME = 0x5A8       # 1448-byte send/recv buffers (CMCHalAPI ctor FUN_10017690)

# Error codes produced by the original send/receive loop FUN_1001d6d0 (returned to
# callers and printed as "ErrCode:" in Log/<date>.log).
ERR_OK = 1
ERR_ENCODE_BASE = 1000      # 1000 + encoder error
ERR_DECODE_BASE = 2000      # 2000 + decoder error (2004 = transaction-id mismatch -> keep reading)
ERR_EXCEPTION_BASE = 500    # 500 + exception code from an 0x80 reply (log: 502 = code 2)
ERR_MISMATCH = 603          # reply func/addr does not match the request (0x25b)
ERR_TIMEOUT = 10060         # WSAETIMEDOUT, also used when select() times out


class FrameError(ValueError):
    """Malformed or unexpected frame.  ``code`` mirrors the original error numbering."""

    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- CRC
def _make_table() -> list[int]:
    tab = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        tab.append(c)
    return tab


_CRC_TABLE = _make_table()


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF).

    The DLL uses the classic auchCRCHi/auchCRCLo tables at 0x1008a580/0x1008a680
    (checked against this implementation on the captured reply, CRC 0x45D0).
    """
    c = 0xFFFF
    for b in data:
        c = (c >> 8) ^ _CRC_TABLE[(c ^ b) & 0xFF]
    return c


def crc_ok(frame: bytes) -> bool:
    """True if bytes[2:4] hold the CRC of bytes[4:] in either byte order."""
    if len(frame) < 4:
        return False
    c = crc16_modbus(frame[4:])
    got = frame[2:4]
    return got == c.to_bytes(2, "big") or got == c.to_bytes(2, "little")


# --------------------------------------------------------------------------- request
@dataclass
class Request:
    func: int
    addr: int = 0
    count: int = 0
    values: list[int] = field(default_factory=list)   # u32 words (0x40) or bytes (0x2x)
    tid: int = 0

    def as_dataex(self) -> list[int]:
        """The vector<int> the NC module logs as ``DataEx`` (``func addr count values``)."""
        if self.func == FUNC_READ:
            return [self.func, self.addr, self.count]
        return [self.func, self.addr, self.count, *self.values]


def encode_request(req: Request, crc_byteorder: str = "big") -> bytes:
    """Encode a PC->card request exactly like FUN_10028190."""
    f = req.func
    if f == FUNC_READ:
        body = struct.pack("<BBIH", 0, f, req.addr & 0xFFFFFFFF, req.count & 0xFFFF)
    elif f == FUNC_WRITE:
        if req.count != len(req.values):
            raise FrameError("count != len(values)", 2)
        body = struct.pack("<BBIH", 0, f, req.addr & 0xFFFFFFFF, req.count) + b"".join(
            struct.pack("<I", v & 0xFFFFFFFF) for v in req.values)
    elif f in (FUNC_WRITE_BYTES, FUNC_WRITE_BYTES2):
        if req.count != len(req.values):
            raise FrameError("count != len(values)", 2)
        body = struct.pack("<BBH", 0, f, req.count) + bytes(v & 0xFF for v in req.values)
    elif f == FUNC_WRITE_BYTES_AT:
        if req.count != len(req.values):
            raise FrameError("count != len(values)", 2)
        body = struct.pack("<BBIH", 0, f, req.addr & 0xFFFFFFFF, req.count) + bytes(
            v & 0xFF for v in req.values)
    else:
        raise FrameError(f"unsupported function {f:#x}", 1)
    tail = struct.pack(">H", len(body)) + body
    crc = crc16_modbus(tail).to_bytes(2, crc_byteorder)
    frame = struct.pack(">H", req.tid & 0xFFFF) + crc + tail
    if len(frame) > MAX_FRAME:
        raise FrameError(f"frame too long ({len(frame)} > {MAX_FRAME})", 3)
    return frame


def decode_request(frame: bytes) -> Request:
    """Parse a request (used by the simulator and for log analysis)."""
    if len(frame) < HEADER_LEN:
        raise FrameError("short frame", 2)
    tid, _crc, ln, _unit, f = struct.unpack_from(">HHHBB", frame, 0)
    if ln != len(frame) - 6:
        raise FrameError(f"LEN {ln} does not match frame size {len(frame)}", 5)
    if f == FUNC_READ:
        addr, cnt = struct.unpack_from("<IH", frame, 8)
        return Request(f, addr, cnt, [], tid)
    if f == FUNC_WRITE:
        addr, cnt = struct.unpack_from("<IH", frame, 8)
        if len(frame) != 14 + 4 * cnt:
            raise FrameError("bad write length", 5)
        vals = list(struct.unpack_from("<%dI" % cnt, frame, 14))
        return Request(f, addr, cnt, vals, tid)
    if f in (FUNC_WRITE_BYTES, FUNC_WRITE_BYTES2):
        (cnt,) = struct.unpack_from("<H", frame, 8)
        return Request(f, 0, cnt, list(frame[10:10 + cnt]), tid)
    if f == FUNC_WRITE_BYTES_AT:
        addr, cnt = struct.unpack_from("<IH", frame, 8)
        return Request(f, addr, cnt, list(frame[14:14 + cnt]), tid)
    raise FrameError(f"unsupported function {f:#x}", 1)


# --------------------------------------------------------------------------- response
@dataclass
class Response:
    func: int
    tid: int
    addr: int = 0
    count: int = 0
    values: list[int] = field(default_factory=list)
    exception: Optional[int] = None    # exception code when func has bit 0x80
    # The exact ``vector<int>`` FUN_10029a90 builds (log field ``Data``).  Set by
    # decode_response; None for responses built by hand (as_data then derives it).
    data: Optional[list[int]] = None

    @property
    def is_exception(self) -> bool:
        return self.exception is not None

    def as_data(self) -> list[int]:
        """vector<int> as FUN_10029a90 builds it and the log prints it as ``Data:``.

        O (FUN_10029a90, checked against the DLL by the parity harness):
        - first word: the raw function byte, so an exception keeps the 0x80 flag:
          ``[func | 0x80, code]`` (log 13:34:44 ``Data:000000b0 00000002``);
        - 0x30: ``[0x30, addr, count, values...]``; 0x40 / 0x26: ``[func, addr, count]``;
        - 0x20: ``[0x20, 0, count]``; the data bytes are **not** appended;
        - anything else (0x21, exceptions, unknown codes): ``[func, byte8]``.
        """
        if self.data is not None:
            return list(self.data)
        if self.is_exception:
            return [self.func | EXCEPTION_FLAG, self.exception]
        f = self.func
        if f == FUNC_READ:
            return [f, self.addr, self.count, *self.values]
        if f in (FUNC_WRITE, FUNC_WRITE_BYTES_AT):
            return [f, self.addr, self.count]
        if f == FUNC_WRITE_BYTES:
            return [f, 0, self.count]
        return [f, *(self.values[:1] or [0])]


def encode_response(resp: Response, crc_byteorder: str = "little") -> bytes:
    """Encode a card->PC reply (simulator).  Little-endian CRC as in the captured reply."""
    f = resp.func
    if resp.is_exception:
        body = struct.pack("<BBB", 0, f | EXCEPTION_FLAG, resp.exception & 0xFF)
    elif f == FUNC_READ:
        body = struct.pack("<BBIH", 0, f, resp.addr, len(resp.values)) + b"".join(
            struct.pack("<I", v & 0xFFFFFFFF) for v in resp.values)
    elif f in (FUNC_WRITE, FUNC_WRITE_BYTES_AT):
        body = struct.pack("<BBIH", 0, f, resp.addr, resp.count)
    elif f in (FUNC_WRITE_BYTES, FUNC_WRITE_BYTES2):
        body = struct.pack("<BBH", 0, f, len(resp.values)) + bytes(resp.values)
    else:
        raise FrameError(f"unsupported function {f:#x}", 1)
    tail = struct.pack(">H", len(body)) + body
    return struct.pack(">H", resp.tid & 0xFFFF) + crc16_modbus(tail).to_bytes(2, crc_byteorder) + tail


def decode_response(frame: bytes, expected_tid: Optional[int] = None) -> Response:
    """Decode a reply exactly like FUN_10029a90 (the CRC is NOT checked, as in the original).

    Return codes of the original become FrameError codes: 2 for < 8 bytes, 4 for a
    transaction-id mismatch (the caller then keeps listening), 5 for a 0x30 reply whose
    ``count*4 != len-14``, 6 for a 0x20 reply whose ``count != len-10``.  The original
    switches on the raw function byte: 0x20, 0x26, 0x30 and 0x40 have their own branches,
    every other byte (0x21, ``func|0x80`` exceptions, unknown codes) takes the default
    branch, which stores only byte 8.  No other length is checked: a short 0x40/0x26 reply
    is read past its end by the DLL; here the missing bytes count as 0.
    """
    if len(frame) < HEADER_LEN:
        raise FrameError("short reply", 2)
    tid = struct.unpack_from(">H", frame, 0)[0]
    if expected_tid is not None and tid != (expected_tid & 0xFFFF):
        raise FrameError(f"tid {tid:#x} != {expected_tid:#x}", 4)
    n = len(frame)
    f = frame[7]
    pad = bytes(frame) + bytes(max(0, 14 - n))
    if f == FUNC_READ:
        addr, cnt = struct.unpack_from("<IH", pad, 8)
        if cnt * 4 != n - 14:
            raise FrameError("read reply length mismatch", 5)
        vals = list(struct.unpack_from("<%dI" % cnt, frame, 14))
        return Response(f, tid, addr, cnt, vals, data=[f, addr, cnt, *vals])
    if f in (FUNC_WRITE, FUNC_WRITE_BYTES_AT):
        addr, cnt = struct.unpack_from("<IH", pad, 8)
        return Response(f, tid, addr, cnt, data=[f, addr, cnt])
    if f == FUNC_WRITE_BYTES:
        (cnt,) = struct.unpack_from("<H", pad, 8)
        if cnt != n - 10:
            raise FrameError("byte reply length mismatch", 6)
        return Response(f, tid, 0, cnt, list(frame[10:]), data=[f, 0, cnt])
    # default branch: [func, byte8] -- exceptions keep the 0x80 flag (D-10)
    code = frame[8] if n > 8 else 0
    if f & EXCEPTION_FLAG:
        return Response(f & 0x7F, tid, exception=code, data=[f, code])
    return Response(f, tid, values=[code], data=[f, code])


def check_reply(req: Request, resp: Response) -> int:
    """Result code like FUN_1001d6d0: 1 ok, 500+exc for an exception, 603 mismatch."""
    if resp.is_exception:
        return ERR_EXCEPTION_BASE + resp.exception if resp.func == req.func else ERR_MISMATCH
    if resp.func == req.func and resp.addr == req.addr:
        return ERR_OK
    return ERR_MISMATCH


def parse_hex(s: str) -> bytes:
    """``"02 be d0 45"`` -> bytes (log ``Buffer:`` field)."""
    return bytes(int(x, 16) for x in s.split())


def parse_dataex(s: str) -> Request:
    """Log ``DataEx:`` field (hex words: func addr count values...) -> Request."""
    w = [int(x, 16) for x in s.split()]
    func, addr, count = w[0], w[1], w[2]
    return Request(func, addr, count, w[3:])


# --------------------------------------------------------------------------- FIFO items
def item_header(op: int, nparams: int) -> int:
    """FIFO item header word = (parameter bytes << 16) | opcode (FUN_10043c20)."""
    return ((nparams * 4) << 16) | (op & 0xFFFF)


def fifo_item(op: int, *params: int) -> list[int]:
    """One FIFO item: header word followed by its 32-bit parameters."""
    return [item_header(op, len(params)), *[p & 0xFFFFFFFF for p in params]]


def parse_fifo_items(words: Sequence[int]) -> list[tuple[int, list[int]]]:
    """Split a FIFO payload (after the frame sequence word) into (op, params)."""
    out = []
    i = 0
    n = len(words)
    while i < n:
        hdr = words[i] & 0xFFFFFFFF
        op, nbytes = hdr & 0xFFFF, hdr >> 16
        k = nbytes // 4
        if nbytes % 4 or i + 1 + k > n:
            raise FrameError(f"bad FIFO item header {hdr:#x} at word {i}", 7)
        out.append((op, list(words[i + 1:i + 1 + k])))
        i += 1 + k
    return out


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def encode_step(dx: int, dy: int, freq: int = 5000, duty: int = 0, power: int = 0xFFFF) -> list[int]:
    """Interpolation step item, op 3000 (0x80BB8), from FUN_10043c20 (non-bus branch).

    One item = one servo period of motion; the item carries no duration (the period is a
    card/planner setting, 1 ms by default).  Fields come from MainApp's 28-byte ManuItem, built
    in FUN_00462630:

    * ``dx``/``dy``: signed pulse increments for this period (int8, |d| <= 127) (ManuItem +0/+4)
    * ``freq``: PWM frequency, u16, 0 -> 5000 (ManuItem +0x14).  FUN_00462630 fills it from a
      per-sample u16 array and its slow-start/slow-end overrides write the configured frequency
      (+0x20 of the override block); 5000 matches the GP.CutFreq default of 5000 Hz.  Observed.
    * ``duty``: laser duty byte, 0 = beam off (ManuItem +0x12; scaled by a percentage and /100
      in FUN_00462630, zeroed for laser-off samples).  Observed.
    * ``power``: u16 sent only while duty != 0 (ManuItem +0x08, initialised to 0xFFFF in
      FUN_00462630).  Its meaning is INFERRED (power/DA word).

    ::

        w1 = (freq & 0xFF) << 24 | duty << 16 | (dy & 0xFF) << 8 | (dx & 0xFF)
        w2 = (power if duty else 0) << 16 | (freq >> 8)

    ``duty`` also accepts a bool (True -> 100) for convenience.
    """
    if not (-127 <= dx <= 127 and -127 <= dy <= 127):
        raise ValueError("step increments must fit int8 (|d| <= 127)")
    freq = int(freq) & 0xFFFF or 5000
    if duty is True:
        duty = 100
    duty = max(0, min(255, int(duty)))
    w1 = ((freq & 0xFF) << 24) | (duty << 16) | ((dy & 0xFF) << 8) | (dx & 0xFF)
    w2 = ((power & 0xFFFF if duty else 0) << 16) | (freq >> 8)
    return [item_header(OP_STEP, 2), w1, w2]


def decode_step(params: Sequence[int]) -> tuple[int, int, int, int, int]:
    """Inverse of :func:`encode_step` -> (dx, dy, freq, duty, power).  Laser on == duty != 0."""
    w1, w2 = params[0] & 0xFFFFFFFF, params[1] & 0xFFFFFFFF
    dx, dy = _s8(w1), _s8(w1 >> 8)
    duty = (w1 >> 16) & 0xFF
    freq = ((w2 & 0xFFFF) << 8) | (w1 >> 24)
    return dx, dy, freq & 0xFFFF, duty, (w2 >> 16) & 0xFFFF


# FIFO / mailbox opcodes seen in the frame builder (FUN_10043c20 and helpers).
# Tags: O = observed in code (address given), I = inferred meaning, U = unknown.
# Speeds in follower items are mm/s * 10 and heights um (mm * 1000) unless noted; "ftol" is the
# MSVC truncating float->int conversion (NC FUN_10061360), reproduced by int().
OP_STOP_AXES = MailboxOp.STOP_AXES  # mailbox [1, axis_mask]                                         I meaning
OP_AXIS_MOVE = Op.AXIS_MOVE  # FIFO 0x140003 [axis|0x80000000, v*ppm, acc, acc*10, pos*ppm] (FUN_1003f470,
                        # focus axis, AF types 5/6) = the jog layout below; bit 31 I "absolute"
OP_ITEM3 = OP_AXIS_MOVE  # legacy name
OP_JOG = MailboxOp.JOG  # mailbox [3, mask|flag, speed, acc, jerk, distance] (FUN_10032b80)
OP_MOVE = MailboxOp.MOVE  # mailbox [5, mask|flag, speed, acc, jerk, p1..p4]  (FUN_10032910)
OP_Z_ABS = Op.Z_ABS     # 0x67 [up_speed*10, z_um]: Z to an absolute height.  O: FIFO class 0x11
                        # (adv. fix height) and the stop path FUN_100571f0 at 0x100575fc (mailbox
                        # [103, ZFUpSpeed*10, ZFDockHeight*1000] then [118, 5, 0]); meaning I
OP_STOP = OP_Z_ABS      # legacy name (the "stop" sequence lifts Z to the dock height)
OP_FOLLOW_ON = Op.FOLLOW_ON  # 0x68 [follow_speed*10, vib<<16 | height*ppm] (class 7 start-cut follow,
                        # class 0xa with height word 1000); 8-param roll-sheet variant 0x200068. O
OP_104 = OP_FOLLOW_ON
OP_FOLLOW_TO = Op.FOLLOW_TO  # 0x69 [follow_speed*10, vib<<16 | h_um, up_speed*10, rest_um] first pierce
                        # stage / smooth pierce (FUN_10043c20 class 4, 0x10044530). O
OP_105 = OP_FOLLOW_TO
OP_FROG_JUMP = Op.FROG_JUMP  # 0x6a 6 params, end-cut "frog jump" lift during the rapid (class 8 with
                        # flag 0x10, 0x100450cd).  O layout, I meaning; not generated by the rebuild
OP_106 = OP_FROG_JUMP
OP_Z_GRADUAL = Op.Z_GRADUAL  # 0x6c [speed*10, delta_um, target_um, 1] gradual height change while the
                        # beam is on (class 5, last stage -> cut height, 0x10044a05). O
OP_108 = OP_Z_GRADUAL
OP_Z_REL = Op.Z_REL     # 0x6d [speed*10, delta_um] relative Z move, positive = down / negative = up
                        # (class 4 stage n>1, class 5, 8 lift, 9 final lift, 0xa).  O
OP_109 = OP_Z_REL
OP_FOLLOW = Op.FOLLOW   # 0x76: [118, sub, ...] follower (ZF) control via MC; sub 4 = crash protect
                        # [4, min_height|0, up_height] (FUN_1003f3e0), sub 5 stop, sub 7 calibration
OP_WAIT = Op.WAIT       # 0x7d1: [condition, timeout_ms] (FUN_1003ec20), see encode_wait.  O
OP_REG = Op.REG         # 0x7d4: register op on the card: [0, reg, nbytes, words...] write /
                        # [1, reg, nbytes, value, 0x80000002] wait (FUN_1003f470/FUN_1003f830, AF
                        # type 1 via reg 101/1010).  O layout, I meaning
OP_2004 = OP_REG
OP_STEP = Op.STEP       # 0xbb8: interpolation step (see encode_step)
OP_MARK = Op.MARK       # 0xbb9: no params.  O: emitted for ManuItem class 0xe and once when the item
                        # class goes from 0 (motion) to a process class 1..19 (VM+0x117c holds the
                        # previous class).  I: "end of motion block"
OP_3002 = Op.DWELL_3002  # 0xbba [ManuItem+0x18] for ManuItem class 3.  MainApp never creates class 3
                        # in this build (U)
OP_SYS = Op.SYS         # 0x270f: system command, first parameter = sub-command

# 9999 sub-commands (mailbox reg 101 and FIFO 0x?270f items)
SYS_AXIS_CFG = SysCmd.AXIS_CFG  # [1, axis_mask | 0x66800000 (step card) / 0x78800000 (bus card), 0]
                        # (FUN_10051e70).  O layout, I meaning "interpolation axes / step format"
SYS_OUTPUTS = SysCmd.OUTPUTS  # [2, mask16, value16]  outputs 1..16 (FUN_100304a0/FUN_10030620)
SYS_PWM = SysCmd.PWM    # [3, frequency, duty, enable]: FIFO header 0x10270f from ManuItem class 1
                        # sub 1/2 (freq +0x14, duty +0x12, duty != 0); mailbox setPWM FUN_1004d960
                        # sends [3, f, d, 0] and FUN_100309f0 [3, LC.PtLaserFreq, MC.LaserPointPower]
SYS_DA = SysCmd.DA      # [4, channel(0/1), value 0..10000] (FUN_100307d0, FIFO class 1 sub 3,
                        # peak current FUN_1003f990, gas pressure); 10000 = full scale (I: 10 V)
SYS_INIT = SysCmd.INIT  # [5, 0, 0] at VM start (FUN_100571f0 / FUN_100315f0)        I
SYS_ZERO = SysCmd.ZERO  # [8, x] then clears per-axis offsets (FUN_100314b0)          I
SYS_9 = SysCmd.SYS_9    # [9, 0xffff, 0] written to reg 5001 before outputs are reset (FUN_10031180)
SYS_OUTPUTS_EXT = SysCmd.OUTPUTS_EXT  # [13, mask16, value16] outputs 11..26 (extended IO)
SYS_PWM_BUS = SysCmd.PWM_BUS  # [17, frequency, duty, enable] bus-card variant of SYS_PWM
SYS_LASER_SYNC = SysCmd.LASER_SYNC  # FIFO [18, src] around every cut block (ManuItem class 1 sub 8, MainApp
                        # FUN_004590a0(1)/(0)); src 1000 fiber, 1001 bus card, CO2 DO port 9/10,
                        # 0 = off.  O values, I meaning "gate the laser with the step items"
SYS_18 = SYS_LASER_SYNC

# Wait (op 2001) modes, FUN_1003ec20(vec, mode, a, b, c, timeout)
WAIT_TIME = WaitMode.TIME  # condition = a = milliseconds (delay)                       O enc, I meaning
WAIT_INPUT = WaitMode.INPUT  # condition = (b | 0x10000) << 8 | c : input b (0-based) == c  O enc, I
WAIT_AXIS = WaitMode.AXIS  # condition = (b | 0x20000) << 8 | c : axis b (index/mask) state c  O enc, I
WAIT_ZF = WaitMode.ZF   # condition = c | 0x3000000 : follower state == c                O enc, I
ZF_ARRIVED = ZfState.ARRIVED  # state waited for after relative/absolute Z moves (op 103/108/109)  I
ZF_FOLLOWING = ZfState.FOLLOWING  # state waited for after op 104 (follow on)                      I
ZF_FOLLOW_TO_DONE = ZfState.FOLLOW_TO_DONE  # state waited for after op 105 (follow to pierce height)        I
WAIT_DEFAULT_TIMEOUT_MS = 3000   # timeout pushed with every timed wait (0x10044294: push 0xbb8)


def sys_item(sub: int, *params: int) -> list[int]:
    """FIFO system item (op 9999) = header, sub-command, params."""
    return fifo_item(OP_SYS, sub, *params)


def mailbox_sys(sub: int, *params: int) -> list[int]:
    """Immediate system command written to the mailbox register 101."""
    return [OP_SYS, sub, *[p & 0xFFFFFFFF for p in params]]


# --------------------------------------------------------------------------- process items
def ftol(x: float) -> int:
    """MSVC ``_ftol`` (NC FUN_10061360 / MainApp FUN_00640740): truncate toward zero."""
    return int(x)


def s32(v: int) -> int:
    """u32 register value -> signed int."""
    return _s32v(v)


def _s32v(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def encode_wait(mode: int, a: int = 0, b: int = 0, c: int = 0,
                timeout_ms: int = WAIT_DEFAULT_TIMEOUT_MS) -> list[int]:
    """Wait item op 2001 = ``[0x807d1, condition, timeout]`` exactly like FUN_1003ec20.

    ``mode`` 0: delay ``a`` ms; 1: input ``b`` == ``c``; 2: axis ``b`` state ``c``; 3: follower
    state ``c``.  Other modes produce nothing in the original (returns ``[]`` here).
    """
    if mode == WAIT_TIME:
        cond = a
    elif mode == WAIT_INPUT:
        cond = ((b | 0x10000) << 8) | (c & 0xFF)
    elif mode == WAIT_AXIS:
        cond = ((b | 0x20000) << 8) | (c & 0xFF)
    elif mode == WAIT_ZF:
        cond = c | 0x3000000
    else:
        return []
    return fifo_item(OP_WAIT, cond, timeout_ms)


def decode_wait(params: Sequence[int]) -> dict:
    """Inverse of :func:`encode_wait` -> ``{mode, ms|input|axis|state, value, timeout_ms}``."""
    cond, timeout = params[0] & 0xFFFFFFFF, params[1] & 0xFFFFFFFF
    top = cond >> 24
    if top == 3:
        return {"mode": WAIT_ZF, "state": cond & 0xFFFFFF, "timeout_ms": timeout}
    if top == 2:
        return {"mode": WAIT_AXIS, "axis": (cond >> 8) & 0xFFFF, "value": cond & 0xFF, "timeout_ms": timeout}
    if top == 1:
        return {"mode": WAIT_INPUT, "input": (cond >> 8) & 0xFFFF, "value": cond & 0xFF, "timeout_ms": timeout}
    return {"mode": WAIT_TIME, "ms": cond, "timeout_ms": timeout}


def wait_ms(ms: int, timeout_ms: int = WAIT_DEFAULT_TIMEOUT_MS) -> list[int]:
    """Timed dwell on the card (ManuItem class 1 sub 5 -> FUN_1003ec20(mode 0, ms, .., 3000))."""
    return encode_wait(WAIT_TIME, int(ms), timeout_ms=timeout_ms)


def wait_zf(state: int, timeout_ms: int) -> list[int]:
    """Wait for follower state ``state`` (2 arrived, 3 following, 4 follow-to done)."""
    return encode_wait(WAIT_ZF, c=state, timeout_ms=timeout_ms)


def follower_word(height_units: int, vib_thin: int = 0, vib_thick: int = 0) -> int:
    """``(thick << 8 | thin) << 16 | height`` as built for ops 104/105/106 (layer +0x3d8/+0x3dc
    = GP.ZFVibAbat_Level / _Level_Thick, selected by GP.ZFVibAbatType +0x3ec).  O"""
    return ((((vib_thick & 0xFF) << 8) | (vib_thin & 0xFF)) << 16) | (height_units & 0xFFFF)


def split_follower_word(w: int) -> tuple[int, int, int]:
    """-> (height_units, vib_thin, vib_thick)."""
    w &= 0xFFFFFFFF
    return w & 0xFFFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF


def encode_z_abs(speed_mm_s: float, z_mm: float) -> list[int]:
    """Op 103: Z to absolute height ``z_mm`` (um on the wire) at ``speed`` (x10)."""
    return fifo_item(OP_Z_ABS, ftol(speed_mm_s * 10.0), ftol(z_mm * 1000.0))


def encode_follow_on(speed_mm_s: float, height_units: int, vib_thin: int = 0,
                     vib_thick: int = 0) -> list[int]:
    """Op 104 (2 params): follower on, track ``height_units`` (= height * pulses/mm, config
    +0xbc28; class 0xa sends the constant 1000).  0x10044fa8."""
    return fifo_item(OP_FOLLOW_ON, ftol(speed_mm_s * 10.0),
                     follower_word(height_units, vib_thin, vib_thick))


def encode_follow_to(follow_speed: float, height_mm: float, up_speed: float, rest_mm: float = 0.0,
                     vib_thin: int = 0, vib_thick: int = 0) -> list[int]:
    """Op 105: follow to ``height_mm`` (um, low 16 bits), then ``rest_mm`` more (um).
    Built for the first pierce stage (height > MP.DirectDrillMaxHeight -> 1 mm + rest) and the
    smooth pierce (0x10044530 / 0x100446dd)."""
    return fifo_item(OP_FOLLOW_TO, ftol(follow_speed * 10.0),
                     follower_word(ftol(height_mm * 1000.0), vib_thin, vib_thick),
                     ftol(up_speed * 10.0), ftol(rest_mm * 1000.0))


def encode_z_gradual(speed_mm_s: float, delta_mm: float, target_mm: float) -> list[int]:
    """Op 108: height change ``delta`` (um, + = down) to ``target`` (um) at ``speed`` (x10),
    trailing constant 1 (0x10044a05 / 0x10044c0c)."""
    return fifo_item(OP_Z_GRADUAL, ftol(speed_mm_s * 10.0), ftol(delta_mm * 1000.0),
                     ftol(target_mm * 1000.0), 1)


def encode_z_rel(speed_mm_s: float, delta_mm: float) -> list[int]:
    """Op 109: relative Z move ``delta_mm`` (um; positive = down, negative = up)."""
    return fifo_item(OP_Z_REL, ftol(speed_mm_s * 10.0), ftol(delta_mm * 1000.0))


def encode_frog_jump(p0: int, p1: int, p2: int, p4: int, word: int) -> list[int]:
    """Op 106 raw layout ``[M+0, M+4 * ppm, M+8, 0, u16 M+0x10, vib<<16 | u16 M+0x14]`` from an
    end-cut ManuItem M with flag 0x10 (0x100450cd).  Field meaning I (jump geometry, follow
    speed*10, next height); MainApp fills it in FUN_004657e0 from the rapid profile."""
    return fifo_item(OP_FROG_JUMP, p0, p1, p2, 0, p4 & 0xFFFF, word)


def encode_crash_protect(min_height: int, up_height_mm: float) -> list[int]:
    """``[118, 4, min_height | 0, ftol(MP.ManuCrashProtectUpHeight)]`` (FUN_1003f3e0).
    ``min_height`` 0 disables (sent before every follow/pierce), the lift sends
    ftol(MP.EnableManuCrashProtectMinHeight)."""
    return fifo_item(OP_FOLLOW, 4, int(min_height), ftol(up_height_mm))


def encode_laser(freq: int, duty: int, bus: bool = False) -> list[int]:
    """ManuItem class 1 sub 1/2 -> ``[9999, 3|17, freq, duty, duty != 0]`` (0x10044109)."""
    duty = max(0, min(255, int(duty)))
    return sys_item(SYS_PWM_BUS if bus else SYS_PWM, int(freq) & 0xFFFF, duty, int(duty != 0))


def encode_da(channel: int, value: int) -> list[int]:
    """``[9999, 4, channel, value]`` (class 1 sub 3, peak current FUN_1003f990, gas DA)."""
    return sys_item(SYS_DA, int(channel), int(value))


def encode_output(port0: int, on: bool) -> list[int]:
    """Class 1 sub 4 (0x100441cd): 0-based output ``port0`` 0..9 -> ``[2, 1<<p, v<<p]``,
    10..25 -> ``[13, 1<<(p-10), v<<(p-10)]``; others produce nothing."""
    if 0 <= port0 <= 9:
        return sys_item(SYS_OUTPUTS, 1 << port0, int(bool(on)) << port0)
    if 10 <= port0 < 26:
        return sys_item(SYS_OUTPUTS_EXT, 1 << (port0 - 10), int(bool(on)) << (port0 - 10))
    return []


def encode_laser_sync(src: int) -> list[int]:
    """``[9999, 18, src]`` header 0x8270f (class 1 sub 8).  ``src`` 1000 on (fiber), 0 off."""
    return sys_item(SYS_LASER_SYNC, int(src))


def encode_axis_move(axis: int, speed_pulses: int, acc: int, pos_pulses: int) -> list[int]:
    """Op 3 FIFO item ``[axis | 0x80000000, v, acc, acc*10, pos]`` (focus axis, FUN_1003f470)."""
    return fifo_item(OP_AXIS_MOVE, (axis & 0x7FFFFFFF) | 0x80000000, int(speed_pulses), int(acc),
                     int(acc) * 10, int(pos_pulses))


def encode_reg_write(reg: int, words: Sequence[int]) -> list[int]:
    """Op 2004 sub 0: ``[0, reg, 4*len(words), words...]`` (AF type 1 writes reg 101)."""
    return fifo_item(OP_REG, 0, reg, 4 * len(words), *words)


def encode_reg_wait(reg: int, value: int, flags: int = 0x80000002) -> list[int]:
    """Op 2004 sub 1: ``[1, reg, 4, value, flags]`` (FUN_1003f830 AF type 1: reg 1010, ms+10000)."""
    return fifo_item(OP_REG, 1, reg, 4, value, flags)


def encode_mark() -> list[int]:
    """Op 3001 (no parameters)."""
    return fifo_item(OP_MARK)


def describe_item(op: int, params: Sequence[int]) -> dict:
    """Decode any FIFO item into a dict (for logs, the UI and tests).  Units: mm, mm/s, ms."""
    p = [v & 0xFFFFFFFF for v in params]
    d: dict = {"op": op}
    if op == OP_STEP and len(p) >= 2:
        dx, dy, freq, duty, power = decode_step(p)
        d.update(kind="step", dx=dx, dy=dy, freq=freq, duty=duty, power=power)
    elif op == OP_WAIT and len(p) >= 2:
        d.update(kind="wait", **decode_wait(p))
    elif op == OP_SYS and p:
        sub = p[0]
        d.update(kind="sys", sub=sub, args=p[1:])
        if sub == SYS_PWM and len(p) >= 3:
            d.update(kind="laser", freq=p[1], duty=p[2], on=bool(p[3]) if len(p) > 3 else bool(p[2]))
        elif sub == SYS_DA and len(p) >= 3:
            d.update(kind="da", channel=p[1], value=p[2])
        elif sub in (SYS_OUTPUTS, SYS_OUTPUTS_EXT) and len(p) >= 3:
            base = 0 if sub == SYS_OUTPUTS else 10
            outs = {base + i + 1: bool(p[2] >> i & 1) for i in range(16) if p[1] >> i & 1}
            d.update(kind="outputs", outputs=outs)
        elif sub == SYS_LASER_SYNC and len(p) >= 2:
            d.update(kind="laser_sync", src=p[1])
    elif op == OP_Z_ABS and len(p) >= 2:
        d.update(kind="z_abs", speed=p[0] / 10.0, z=_s32v(p[1]) / 1000.0)
    elif op == OP_FOLLOW_ON and len(p) >= 2:
        h, thin, thick = split_follower_word(p[1])
        d.update(kind="follow_on", speed=p[0] / 10.0, height_units=h, vib=(thin, thick), extra=p[2:])
    elif op == OP_FOLLOW_TO and len(p) >= 4:
        h, thin, thick = split_follower_word(p[1])
        d.update(kind="follow_to", speed=p[0] / 10.0, height=h / 1000.0, vib=(thin, thick),
                 up_speed=p[2] / 10.0, rest=_s32v(p[3]) / 1000.0)
    elif op == OP_Z_GRADUAL and len(p) >= 4:
        d.update(kind="z_gradual", speed=p[0] / 10.0, delta=_s32v(p[1]) / 1000.0,
                 target=_s32v(p[2]) / 1000.0, flag=p[3])
    elif op == OP_Z_REL and len(p) >= 2:
        d.update(kind="z_rel", speed=p[0] / 10.0, delta=_s32v(p[1]) / 1000.0)
    elif op == OP_FOLLOW and p:
        d.update(kind="follower", sub=p[0], args=p[1:])
        if p[0] == 4 and len(p) >= 3:
            d.update(kind="crash_protect", min_height=p[1], up_height=p[2])
    elif op == OP_MARK:
        d.update(kind="mark")
    elif op == OP_AXIS_MOVE and len(p) >= 5:
        d.update(kind="axis_move", axis=p[0] & 0x7FFFFFFF, speed=p[1], acc=p[2], pos=_s32v(p[4]))
    elif op == OP_REG and p:
        d.update(kind="reg_write" if p[0] == 0 else "reg_wait", args=p[1:])
    else:
        d.update(kind="unknown", args=p)
    return d


def build_fifo_frames(items: Iterable[Sequence], first_seq: int,
                      max_bytes: int = 0x4B0) -> list[list[int]]:
    """Pack encoded items into FIFO frames (FUN_10043c20).

    Each frame is the value list for ``write(REG_FIFO_DATA, [seq, items...])``; ``seq``
    increments per frame starting at ``first_seq``.  The original starts a vector
    ``[0x40, 0x66, count, seq]`` and tests ``0x4af < words*4`` **once per ManuItem, after its
    whole expansion** (a leading op 3001 included), then closes the frame.  A frame therefore
    holds at least 1200 bytes when closed (except the last) and can exceed it by one expansion
    (306 words = 1224 bytes observed, parity JN-08).

    ``items`` is a sequence whose elements are either one FIFO item (a list of ints: header +
    params) or a *group* (a list of FIFO items: one ManuItem's expansion).  A plain item is its
    own group, so a flat item list is packed with the test after every item.
    """
    frames: list[list[int]] = []
    cur: list[int] = []
    seq = first_seq
    for it in items:
        if len(it) and not isinstance(it[0], int):   # a group: one ManuItem's FIFO items
            for w in it:
                cur.extend(w)
        else:
            cur.extend(it)
        if (len(cur) + 4) * 4 > max_bytes - 1:
            frames.append([seq & 0xFFFFFFFF, *cur])
            seq += 1
            cur = []
    if cur:
        frames.append([seq & 0xFFFFFFFF, *cur])
    return frames


# --------------------------------------------------------------------------- register map
REG_CONTROL = 100        # write 8888 (FUN_10039300) / 9999 after parameter download (FUN_1002e500)
REG_MAILBOX = 101        # immediate command [op, params...]
REG_FIFO_DATA = 102      # FIFO frame [seq, item words...]
REG_FIFO_CTRL = 103      # 1 = clear, 2 = start, 3 = stop
FIFO_CLEAR, FIFO_START, FIFO_STOP = FifoCtrl.CLEAR, FifoCtrl.START, FifoCtrl.STOP
CONTROL_HELLO = 8888     # INFERRED: handshake/reset written on connect
CONTROL_APPLY = 9999     # INFERRED: "parameters downloaded, apply"

REG_STATUS_RO = 1000     # 36 words (FUN_100521b0)
N_STATUS_RO = 36
REG_LATCH = 1050         # 3 words (FUN_1003d3a0 region)                               INFERRED
REG_AXIS_RO = 2000       # 5 axes x 10 words (FUN_100523c0)
N_AXES = 5
AXIS_RO_STRIDE = 10
REG_STATUS_RW = 5000     # 1 word (FUN_10052600)
REG_ONBZF = 10000        # on-board follower status, 18 words (FUN_1004f3e0)
N_ONBZF = 18
REG_SYS_RW = 50000       # 26 words (FUN_10052a20)
N_SYS_RW = 26
REG_AXIS_RW = 50200      # 5 axes x 20 words, 14 used (FUN_10052800)
AXIS_RW_STRIDE = 20
REG_HWPARAM = 59600      # + slot (<32): [0xffffffff, 0xe0, data...] (FUN_10050fb0/FUN_10051320)
REG_FAST = 60001         # combined fast status (FUN_10052c30)
REG_SERVO_INFO = 63000   # 0x80 words (FUN_10054a00)

# Indices inside the 1000-block (RO[i] = reg 1000+i), from getter users
RO_CONFIG = 0            # bits 17..18 card variant (FUN_1004ed60)
RO_VERSION = 1           # hardware/firmware version code (compared with MinHardwareVer)
RO_INPUTS = 4            # inputs 1..12 bitmask (FUN_1002b820)
RO_OUTPUTS = 5           # outputs 1..10 bitmask (FUN_1002b760 / FUN_100390f0)
RO_FIFO_SEQ = 15         # last FIFO frame sequence accepted (FUN_10052e90 uses +1)
RO_FIFO_FREE = 16        # free FIFO space in bytes (needs frame_bytes + 2000)
RO_RUNNING = 19          # ==1 -> machine state 4 "running program" (FUN_10039500)
RO_INPUTS_EXT = 21       # inputs 13..28 (extended IO)
RO_OUTPUTS_EXT = 22      # outputs 11..26 (extended IO)

# Indices inside one axis' 10-word RO record (reg 2000 + 10*axis + k)
AX_STATUS = 0            # bits0-3 alarm code, byte2 busy, byte3 mode (2 homing, 3..5 moving)
AX_VELOCITY = 1          # velocity in pulses/s (INFERRED unit; /pulses_per_mm -> mm/s)
AX_POSITION = 2          # command position, pulses (FUN_1002c070, softPara.ini XAxis=)
AX_FEEDBACK = 4          # feedback (encoder) position, pulses  INFERRED
AX_LATCH = 6             # latched position (probe/edge)        INFERRED
AX_EDGE_LATCH = 7        # edge-seek latch: MainApp FUN_005b6b30 reads getReg(3, axis*10+7) / ppm  O read, I meaning


def is_write_filtered(addr: int) -> bool:
    """CMCHalAPI write filter FUN_10022710: True = write is silently NOT sent.

    Writes to 106..4999 (except 150/151), 5009..5999 and 51001..58999 are
    dropped and reported as success.  (func 0x26 bypasses the filter.)
    """
    if 0x69 < addr < 5000 and addr not in (150, 151):
        return True
    if 5008 < addr < 6000:
        return True
    if 51000 < addr < 59000:
        return True
    return False


def read_filter(addr: int) -> Optional[int]:
    """CMCHalAPI read filter FUN_10022590: None = really sent, else the faked result
    code (0 = failure, 1 = success) returned with all-zero values."""
    if 0x68 < addr < 1000 and addr not in (150, 151):
        return 0
    if 1100 < addr < 2000 or 3000 < addr < 5000 or 5008 < addr < 6000 or 51000 < addr < 59000:
        return 1
    return None
