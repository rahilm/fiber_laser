"""Peripheral frame codecs: the ``IModbus`` family in Module/NCModule.dll.

Every codec turns a *request vector* (a list of ints, as the C++ code builds it with
``std::vector<int>``) into bytes, and reply bytes back into a vector.  The vtables were
recovered from RTTI (``.?AV<name>@@`` -> CompleteObjectLocator -> vtable); each has the
slots ``[0] dtor, [1] encode-ext, [2] encode, [3] decode-ext, [4] decode``:

=================  ==========  ===========  ===========  ========================  ======
class              vtable      encode       decode       singleton (getter)        used
=================  ==========  ===========  ===========  ========================  ======
CExtModbus         0x1008a844  0x10028190   0x10029a90   0x100acb50 (FUN_10027bb0)  MC
CStdModbus         0x1008a79c  0x10028740   0x10029f90   0x100acb60 (no getter)     no
CIPGModbus         0x1008a7b4  0x100288e0   0x1002aac0   0x100acb64 (FUN_10027bc0)  IPG
CSerialModbus      0x1008a7cc  0x10028950   0x1002a110   0x100acb68 (FUN_10027bd0)  AF
CExtCardModbus     0x1008a7e4  0x10028c50   0x1002a320   0x100acb6c (FUN_10027c10)  EC
CFTC61Modbus       0x1008a7fc  0x10028f50   0x1002a530   0x100acb70 (no getter)     no
CRaycusModbus      0x1008a814  0x100290e0   0x1002a980   0x100acb74 (FUN_10027c20)  Raycus
CMonitorModbus     0x1008a82c  0x10029150   0x1002aac0   0x100acb78                 cloud
=================  ==========  ===========  ===========  ========================  ======

"used" is observed: the only references to the CStdModbus and CFTC61Modbus singletons
(0x100acb60 / 0x100acb70) are their atexit destructors (FUN_10083970 / FUN_100839b0), so in
this build neither the "network follower" codec nor the FTC61 tunnel is ever called.  They are
implemented anyway (byte-exact) because the ini still carries ``ZFIP``/``ZFPort``.
CMonitorModbus belongs to the cloud monitor and is intentionally omitted.

Request vectors (observed in the callers, e.g. syncReadAFReg FUN_1004bee0,
syncWriteECReg FUN_1004c280):

* read  : ``[0x03, addr, count]``                        (count in 16-bit words)
* write : ``[0x10, addr, count, w0, w1, ...]``            (``count == len(words)``)
* bytes : ``[0x26, addr, count, b0, b1, ...]``            (RTU codecs only)

32-bit values travel as two 16-bit words, **low word first** (syncWriteAFReg FUN_1004b510
pushes ``v & 0xffff`` then ``v >> 16``; syncReadAFReg rebuilds ``w1 << 16 + w0``).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional, Sequence

from mlaser.controller.frames import crc16_modbus

FUNC_READ_HOLDING = 0x03
FUNC_WRITE_MULTI = 0x10
FUNC_BYTES_AT = 0x26        # addr u32 LE + count u16 LE + raw bytes (RTU codecs)
FUNC_TUNNEL = 0x21          # CFTC61Modbus: RTU frame wrapped in an MC-style header
FUNC_TUNNEL_REPLY = 0x20
EXCEPTION_FLAG = 0x80

# decoder error numbers as returned by the C++ (int)
ERR_SHORT = -101            # 0xffffff9b: frame too short / CRC wrong
ERR_LENGTH = -102           # 0xffffff9a: byte count does not match frame length
ERR_SLAVE = -103            # 0xffffff99: wrong slave address


class CodecError(ValueError):
    def __init__(self, msg: str, code: int = -1):
        super().__init__(f"{msg} ({code})")
        self.code = code


def split_u32(values: Sequence[int]) -> list[int]:
    """32-bit values -> 16-bit words, low word first (FUN_1004b510 / FUN_1004c640)."""
    out: list[int] = []
    for v in values:
        v &= 0xFFFFFFFF
        out += [v & 0xFFFF, (v >> 16) & 0xFFFF]
    return out


def join_u32(words: Sequence[int], signed: bool = True) -> list[int]:
    """Inverse of :func:`split_u32` (syncReadAFReg FUN_1004bee0: ``w1 << 16 + w0``)."""
    out = []
    for i in range(0, len(words) - 1, 2):
        v = ((words[i + 1] & 0xFFFF) << 16) | (words[i] & 0xFFFF)
        if signed and v & 0x80000000:
            v -= 1 << 32
        out.append(v)
    return out


def rtu_crc(data: bytes) -> bytes:
    """Modbus RTU CRC as appended by FUN_10027af0 (tables 0x1008a580/0x1008a680):
    the standard CRC-16/MODBUS, low byte first on the wire."""
    c = crc16_modbus(data)
    return bytes([c & 0xFF, c >> 8])


def rtu_crc_ok(frame: bytes) -> bool:
    """FUN_10027b50: recompute over ``frame[:-2]`` and compare the two trailing bytes."""
    return len(frame) >= 3 and rtu_crc(frame[:-2]) == frame[-2:]


# --------------------------------------------------------------------------- RTU
@dataclass
class RtuCodec:
    """CSerialModbus (slave 1, FUN_10028950 / FUN_1002a110) and CExtCardModbus (slave 2,
    FUN_10028c50 / FUN_1002a320).  Byte-for-byte the classic Modbus RTU framing plus the
    vendor function 0x26 (address and count little-endian)."""
    slave: int = 1

    def encode(self, vec: Sequence[int]) -> bytes:
        if len(vec) < 3:
            raise CodecError("request vector needs [func, addr, count]", -1)
        func, addr, count = vec[0] & 0xFF, vec[1], vec[2]
        out = bytearray([self.slave & 0xFF, func])
        if func in (FUNC_READ_HOLDING, 0x30):
            out += struct.pack(">HH", addr & 0xFFFF, count & 0xFFFF)
        elif func == FUNC_WRITE_MULTI:
            vals = list(vec[3:])
            if count != len(vals):
                raise CodecError("count != number of words", -1)
            out += struct.pack(">HHB", addr & 0xFFFF, count & 0xFFFF, (2 * count) & 0xFF)
            for v in vals:
                out += struct.pack(">H", v & 0xFFFF)
        elif func == FUNC_BYTES_AT:
            vals = list(vec[3:])
            if count != len(vals):
                raise CodecError("count != number of bytes", -1)
            out += struct.pack("<IH", addr & 0xFFFFFFFF, count & 0xFFFF)
            out += bytes(v & 0xFF for v in vals)
        # any other function: just slave + func (+ CRC), exactly like the C++ default path
        return bytes(out + rtu_crc(bytes(out)))

    def decode(self, frame: bytes) -> list[int]:
        """Reply bytes -> vector: read ``[3, bytecount, w...]``, write ``[0x10, addr, count]``,
        bytes ``[0x26, addr, count]``, exception ``[func|0x80, code]``."""
        if len(frame) < 3 or not rtu_crc_ok(frame):
            raise CodecError("short frame or bad CRC", ERR_SHORT)
        if frame[0] != self.slave:
            raise CodecError(f"slave {frame[0]} != {self.slave}", ERR_SLAVE)
        func = frame[1]
        if func == FUNC_READ_HOLDING:
            n = frame[2]
            if n != len(frame) - 5:
                raise CodecError("byte count mismatch", ERR_LENGTH)
            return [func, n] + [struct.unpack_from(">H", frame, 3 + 2 * i)[0] for i in range(n // 2)]
        if func == FUNC_WRITE_MULTI:
            a, c = struct.unpack_from(">HH", frame, 2)
            return [func, a, c]
        if func == FUNC_BYTES_AT:
            a, c = struct.unpack_from("<IH", frame, 2)
            return [func, a, c]
        return [func, frame[2]]

    def reply_length(self, vec: Sequence[int]) -> int:
        """Bytes the serial HAL waits for (FUN_10010f20: ``2*count + 5`` for reads; FUN_10011170: 8
        for writes)."""
        return 2 * vec[2] + 5 if vec[0] == FUNC_READ_HOLDING else 8

    # server side (simulators)
    def decode_request(self, frame: bytes) -> list[int]:
        if len(frame) < 4 or not rtu_crc_ok(frame) or frame[0] != self.slave:
            raise CodecError("bad request", ERR_SHORT)
        func = frame[1]
        if func == FUNC_READ_HOLDING:
            a, c = struct.unpack_from(">HH", frame, 2)
            return [func, a, c]
        if func == FUNC_WRITE_MULTI:
            a, c, nb = struct.unpack_from(">HHB", frame, 2)
            return [func, a, c] + [struct.unpack_from(">H", frame, 7 + 2 * i)[0] for i in range(c)]
        if func == FUNC_BYTES_AT:
            a, c = struct.unpack_from("<IH", frame, 2)
            return [func, a, c] + list(frame[8:8 + c])
        return [func]

    def encode_reply(self, vec: Sequence[int]) -> bytes:
        func = vec[0] & 0xFF
        out = bytearray([self.slave, func])
        if func & EXCEPTION_FLAG:
            out.append(vec[1] & 0xFF)
        elif func == FUNC_READ_HOLDING:
            words = list(vec[1:])
            out.append(2 * len(words))
            for w in words:
                out += struct.pack(">H", w & 0xFFFF)
        elif func == FUNC_WRITE_MULTI:
            out += struct.pack(">HH", vec[1] & 0xFFFF, vec[2] & 0xFFFF)
        elif func == FUNC_BYTES_AT:
            out += struct.pack("<IH", vec[1] & 0xFFFFFFFF, vec[2] & 0xFFFF)
        return bytes(out + rtu_crc(bytes(out)))


SERIAL_MODBUS = RtuCodec(1)      # CSerialModbus: AF heads, serial devices type 1/9/10
EXT_CARD_MODBUS = RtuCodec(2)    # CExtCardModbus: EtherCAT/extension card (type 0xb)


# --------------------------------------------------------------------------- Std (TCP-like)
class StdModbusCodec:
    """CStdModbus (FUN_10028740 encode / FUN_10029f90 decode).  Not referenced in this
    build (see module docstring).

    Request: ``00 00 00 00 | len BE = 2*len(vec) | 00 | func | addr BE | count BE`` and for 0x10
    ``| bytecount | words BE``.  tid/protocol are always 0, and the length field is
    ``2 * len(vec)``, which for writes is one less than real Modbus/TCP (observed quirk).
    Reply (decoder): ``func = b[7]``; 0x03: ``b[8]`` = byte count, must equal ``len-9``,
    words BE from ``b[9]``; 0x10: ``[0x10, addr, count]``; else ``[func, b[8]]``.
    """

    def encode(self, vec: Sequence[int]) -> bytes:
        func, addr, count = vec[0], vec[1], vec[2]
        out = bytearray(b"\0\0\0\0")
        out += struct.pack(">H", (2 * len(vec)) & 0xFFFF)
        out += bytes([0, func & 0xFF])
        out += struct.pack(">HH", addr & 0xFFFF, count & 0xFFFF)
        if func == FUNC_WRITE_MULTI:
            out.append((2 * len(vec[3:])) & 0xFF)
            for v in vec[3:]:
                out += struct.pack(">H", v & 0xFFFF)
        return bytes(out)

    def decode(self, frame: bytes) -> list[int]:
        if len(frame) < 9:
            raise CodecError("short frame", ERR_SHORT)
        func = frame[7]
        if func == FUNC_READ_HOLDING:
            n = frame[8]
            if n != len(frame) - 9:
                raise CodecError("byte count mismatch", ERR_LENGTH)
            return [func, n] + [struct.unpack_from(">H", frame, 9 + 2 * i)[0] for i in range(n // 2)]
        if func == FUNC_WRITE_MULTI:
            return [func] + list(struct.unpack_from(">HH", frame, 8))
        return [func, frame[8]]

    # server side
    def decode_request(self, frame: bytes) -> list[int]:
        if len(frame) < 12:
            raise CodecError("short request", ERR_SHORT)
        func = frame[7]
        a, c = struct.unpack_from(">HH", frame, 8)
        if func == FUNC_WRITE_MULTI:
            return [func, a, c] + [struct.unpack_from(">H", frame, 13 + 2 * i)[0] for i in range(c)]
        return [func, a, c]

    def encode_reply(self, vec: Sequence[int]) -> bytes:
        func = vec[0] & 0xFF
        if func == FUNC_READ_HOLDING:
            words = list(vec[1:])
            body = bytes([0, func, 2 * len(words)]) + b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
        elif func == FUNC_WRITE_MULTI:
            body = bytes([0, func]) + struct.pack(">HH", vec[1] & 0xFFFF, vec[2] & 0xFFFF)
        else:
            body = bytes([0, func, vec[1] & 0xFF])
        return b"\0\0\0\0" + struct.pack(">H", len(body)) + body


STD_MODBUS = StdModbusCodec()


def encode_std_modbus(func: int, addr: int, values: Sequence[int] = (), count: Optional[int] = None) -> bytes:
    """Convenience wrapper kept for the phase-1 API (FUN_10028740)."""
    return STD_MODBUS.encode([func, addr, count if count is not None else len(values), *values])


# --------------------------------------------------------------------------- FTC61 tunnel
class Ftc61Codec:
    """CFTC61Modbus (FUN_10028f50 / FUN_1002a530).  Not referenced in this build.

    Encode: the request is first RTU-encoded with the CSerialModbus singleton (call through
    ``PTR_vftable_100acb68 + 8``), then wrapped as
    ``00 00 00 00 | (n+4) BE16 | 00 21 | n LE16 | rtu bytes`` - an MC-style frame (CExtModbus
    header with tid = crc = 0) carrying function 0x21.  Decode: ``func = b[7]``; 0x20:
    ``count LE16 = b[8:10]`` must equal ``len-10``, then the raw bytes; 0x26/0x40: addr u32 LE +
    count u16 LE; 0x30: addr, count and ``count`` u32 LE values; else ``[func, b[8]]``.
    INFERRED: a pass-through to a serial device behind the card (the ribbon has
    "FTC Upgrade" = electric focus upgrade, lang mf440).
    """

    def encode(self, vec: Sequence[int]) -> bytes:
        rtu = SERIAL_MODBUS.encode(vec)
        n = len(rtu)
        return b"\0\0\0\0" + struct.pack(">H", (n + 4) & 0xFFFF) + bytes([0, FUNC_TUNNEL]) + struct.pack("<H", n) + rtu

    def decode(self, frame: bytes) -> list[int]:
        if len(frame) < 8:
            raise CodecError("short frame", -1)
        func = frame[7]
        if func == FUNC_TUNNEL_REPLY:
            (n,) = struct.unpack_from("<H", frame, 8)
            if n != len(frame) - 10:
                raise CodecError("count mismatch", -1)
            return [func, n] + list(frame[10:10 + n])
        if func in (FUNC_BYTES_AT, 0x40):
            a, c = struct.unpack_from("<IH", frame, 8)
            return [func, a, c]
        if func == 0x30:
            a, c = struct.unpack_from("<IH", frame, 8)
            if 4 * c != len(frame) - 14:
                raise CodecError("count mismatch", -1)
            return [func, a, c] + list(struct.unpack_from("<%dI" % c, frame, 14))
        return [func, frame[8]]

    @staticmethod
    def tunnel_payload(frame: bytes) -> bytes:
        """Server side: the RTU bytes inside a 0x21 request."""
        (n,) = struct.unpack_from("<H", frame, 8)
        return frame[10:10 + n]

    @staticmethod
    def encode_tunnel_reply(rtu: bytes) -> bytes:
        n = len(rtu)
        return b"\0\0\0\0" + struct.pack(">H", (n + 4) & 0xFFFF) + bytes([0, FUNC_TUNNEL_REPLY]) + struct.pack("<H", n) + rtu


FTC61_MODBUS = Ftc61Codec()


# --------------------------------------------------------------------------- raw
class RawCodec:
    """CIPGModbus / CRaycusModbus: request vector elements are copied byte-for-byte
    (FUN_100288e0 needs >= 1 element, FUN_100290e0 needs >= 3), replies are returned as a list
    of byte values (FUN_1002aac0 needs >= 3 bytes, FUN_1002a980 accepts anything)."""

    def __init__(self, min_request: int, min_reply: int):
        self.min_request, self.min_reply = min_request, min_reply

    def encode(self, vec: Sequence[int]) -> bytes:
        if len(vec) < self.min_request:
            raise CodecError("request too short", -1)
        return bytes(v & 0xFF for v in vec)

    def decode(self, frame: bytes) -> list[int]:
        if len(frame) < self.min_reply:
            raise CodecError("reply too short", -1)
        return list(frame)


IPG_RAW = RawCodec(1, 3)
RAYCUS_RAW = RawCodec(3, 0)
