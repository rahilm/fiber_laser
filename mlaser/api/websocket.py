"""The small part of RFC 6455 the event stream needs (standard library only).

The server only *sends* text frames (one JSON event each) and answers the control frames: a
ping gets its pong, a close is echoed and ends the connection.  Data the client sends (text or
binary frames) is never acted on: the stream is read-only, and a client frame larger than
:data:`MAX_CLIENT_FRAME` or unmasked (RFC 6455 §5.1) closes the connection.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
from typing import Optional

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_CLIENT_FRAME = 4096
CLOSE_NORMAL, CLOSE_GOING_AWAY, CLOSE_PROTOCOL, CLOSE_UNSUPPORTED = 1000, 1001, 1002, 1003
CLOSE_POLICY, CLOSE_TOO_BIG, CLOSE_TRY_AGAIN = 1008, 1009, 1013


class ProtocolError(Exception):
    def __init__(self, message: str, code: int = CLOSE_PROTOCOL) -> None:
        super().__init__(message)
        self.code = code


def accept_key(key: str) -> str:
    """``Sec-WebSocket-Accept`` for a client's ``Sec-WebSocket-Key``."""
    return base64.b64encode(hashlib.sha1((key.strip() + GUID).encode("ascii")).digest()).decode("ascii")


def valid_key(key: Optional[str]) -> bool:
    if not key:
        return False
    try:
        return len(base64.b64decode(key.strip(), validate=True)) == 16
    except (ValueError, TypeError):
        return False


def encode(opcode: int, payload: bytes = b"", mask: bool = False) -> bytes:
    """One final frame (servers send unmasked frames; ``mask`` is for test clients)."""
    head = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 1 << 16:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def close_payload(code: int, reason: str = "") -> bytes:
    return struct.pack("!H", code) + reason.encode("utf-8")[:120]


def _read_exact(rfile, n: int) -> bytes:
    data = rfile.read(n)
    if data is None or len(data) < n:
        raise EOFError("connection closed")
    return data


def read_frame(rfile, max_payload: int = MAX_CLIENT_FRAME, require_mask: bool = True) -> tuple[int, bytes]:
    """``(opcode, payload)`` of the next frame from ``rfile`` (a buffered socket reader)."""
    b0, b1 = _read_exact(rfile, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if b0 & 0x70:
        raise ProtocolError("reserved bits set")
    if opcode >= 0x8 and (n > 125 or not b0 & 0x80):
        raise ProtocolError("bad control frame")
    if n == 126:
        n = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    if require_mask and not masked:
        raise ProtocolError("client frames must be masked")
    if n > max_payload:
        raise ProtocolError("frame too large", CLOSE_TOO_BIG)
    key = _read_exact(rfile, 4) if masked else b""
    data = _read_exact(rfile, n) if n else b""
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return opcode, data


__all__ = ["accept_key", "valid_key", "encode", "read_frame", "close_payload", "ProtocolError", "OP_TEXT",
           "OP_BINARY", "OP_CLOSE", "OP_PING", "OP_PONG", "OP_CONT", "MAX_CLIENT_FRAME", "CLOSE_NORMAL",
           "CLOSE_GOING_AWAY", "CLOSE_PROTOCOL", "CLOSE_UNSUPPORTED", "CLOSE_POLICY", "CLOSE_TOO_BIG",
           "CLOSE_TRY_AGAIN"]
