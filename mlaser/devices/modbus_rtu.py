"""Modbus-RTU-over-network client and simulator used by the AF and EC heads.

Observed (NCModule.dll):

* CAFNetHalAPI::readReg FUN_1001dc20 / ::writeReg FUN_1001ee70 encode with device type 9 =
  CSerialModbus (slave 1).  CECNetHalAPI::readReg FUN_1001ff30 / ::writeReg FUN_10020fe0 use
  type 0xb = CExtCardModbus (slave 2).  The RTU bytes (with CRC) are sent as one UDP datagram
  (or TCP segment when AccessType=0) to the device endpoint, no extra header.
* Reply check (FUN_1001dc20): ``reply.func == req.func`` and for reads
  ``bytecount == 2*count`` -> 1 (OK); ``reply.func == req.func + 0x80`` -> ``500 + code``;
  anything else -> 3.  The log strings are ``"AF Send Buf: "``/``"AF Recv Buf: "`` (and ``EC``).
* Registers are 32-bit values sent as two 16-bit words, low word first (see codecs).
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Sequence

from .codecs import (EXCEPTION_FLAG, FUNC_BYTES_AT, FUNC_READ_HOLDING, FUNC_WRITE_MULTI, CodecError,
                     RtuCodec, join_u32, split_u32)
from .transport import LoopbackTransport, Transport, UdpServer

OK = 1
ERR_MISMATCH = 3
ERR_EXCEPTION_BASE = 500
ERR_NO_REPLY = 2


class DeviceError(RuntimeError):
    def __init__(self, msg: str, code: int):
        super().__init__(f"{msg} (code {code})")
        self.code = code


class RtuClient:
    """Register access to one RTU slave over a :class:`Transport`."""

    def __init__(self, transport: Transport, codec: RtuCodec, name: str = "dev",
                 write_delay_ms: int = 0):
        self.t, self.codec, self.name = transport, codec, name
        self.write_delay_ms = write_delay_ms
        self.last_tx = b""
        self.last_rx = b""

    def close(self) -> None:
        self.t.close()

    def _check(self, req: Sequence[int], rx: bytes) -> list[int]:
        try:
            rep = self.codec.decode(rx)
        except CodecError as e:
            raise DeviceError(f"{self.name}: bad reply {rx.hex(' ')}", 2000 - e.code) from e
        if rep[0] == req[0] + EXCEPTION_FLAG:
            raise DeviceError(f"{self.name}: exception {rep[1]}", ERR_EXCEPTION_BASE + rep[1])
        if rep[0] != req[0] or (req[0] == FUNC_READ_HOLDING and rep[1] != 2 * req[2]):
            raise DeviceError(f"{self.name}: mismatched reply {rx.hex(' ')}", ERR_MISMATCH)
        return rep

    def _valid(self, rx: bytes) -> bool:
        try:
            self.codec.decode(rx)
            return True
        except CodecError:
            return False

    def request(self, vec: Sequence[int], delay_ms: int = 0) -> list[int]:
        tx = self.codec.encode(vec)
        self.last_tx = tx
        rx = self.t.exchange(tx, self.codec.reply_length(vec), self._valid, delay_ms)
        if rx is None:
            raise DeviceError(f"{self.name}: no reply to {tx.hex(' ')}", ERR_NO_REPLY)
        self.last_rx = rx
        return self._check(vec, rx)

    # 16-bit words
    def read_words(self, addr: int, count: int) -> list[int]:
        return self.request([FUNC_READ_HOLDING, addr, count])[2:]

    def write_words(self, addr: int, words: Sequence[int]) -> None:
        self.request([FUNC_WRITE_MULTI, addr, len(words), *words], self.write_delay_ms)

    # 32-bit registers (two words each, low first)
    def read_u32(self, addr: int, n: int = 1, signed: bool = True) -> list[int]:
        return join_u32(self.read_words(addr, 2 * n), signed)

    def write_u32(self, addr: int, values: Sequence[int]) -> None:
        self.write_words(addr, split_u32(values))


class RtuSimulator:
    """A register file answering RTU frames for one slave id.  ``words`` maps a 16-bit word
    address to its value; ``on_write(addr, words)`` lets device models react."""

    def __init__(self, slave: int, on_write: Optional[Callable[[int, list[int]], None]] = None,
                 readable: Optional[Callable[[int], bool]] = None):
        self.codec = RtuCodec(slave)
        self.words: dict[int, int] = {}
        self.on_write = on_write
        self.readable = readable or (lambda a: True)
        self.lock = threading.RLock()
        self.requests: list[list[int]] = []
        self.drop = 0
        self._server: Optional[UdpServer] = None

    # helpers for models
    def set_u32(self, addr: int, value: int) -> None:
        w = split_u32([value])
        self.words[addr], self.words[addr + 1] = w

    def get_u32(self, addr: int, signed: bool = True) -> int:
        return join_u32([self.words.get(addr, 0), self.words.get(addr + 1, 0)], signed)[0]

    def handle(self, frame: bytes) -> Optional[bytes]:
        try:
            req = self.codec.decode_request(frame)
        except CodecError:
            return None
        with self.lock:
            self.requests.append(req)
            if self.drop > 0:
                self.drop -= 1
                return None
            func = req[0]
            if func == FUNC_READ_HOLDING:
                a, n = req[1], req[2]
                if not all(self.readable(a + i) for i in range(n)):
                    return self.codec.encode_reply([func | EXCEPTION_FLAG, 2])
                return self.codec.encode_reply([func] + [self.words.get(a + i, 0) for i in range(n)])
            if func == FUNC_WRITE_MULTI:
                a, n, vals = req[1], req[2], req[3:]
                for i, v in enumerate(vals):
                    self.words[a + i] = v
                if self.on_write:
                    self.on_write(a, list(vals))
                return self.codec.encode_reply([func, a, n])
            if func == FUNC_BYTES_AT:
                return self.codec.encode_reply([func, req[1], req[2]])
            return self.codec.encode_reply([func | EXCEPTION_FLAG, 1])

    def loopback(self) -> LoopbackTransport:
        return LoopbackTransport(self.handle)

    def serve(self, host: str = "127.0.0.1", port: int = 0, transport: str = "udp") -> UdpServer:
        self._server = UdpServer(self.handle, host, port, transport).start()
        return self._server

    def stop(self) -> None:
        if self._server:
            self._server.stop()
