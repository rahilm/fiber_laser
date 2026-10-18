"""mf436 Network Test, done without shelling out to ``ping``.

Original (MainApp FUN_004c1ed0, command 0x32d3, O): ``CreateProcessW(NULL, "cmd.exe /k ping
10.1.1.168 -l 1000 && ping 10.1.1.169 -l 1000")`` -- four ICMP echoes of 1000 bytes to the motion
card and to the network follower address.  ``File\\pingMC.bat`` / ``pingZF.bat`` are referenced by
strings (0x8d312c / 0x8d3154) but no code uses them (O: dead).

Rebuild (I): ICMP needs raw sockets (root) on most systems, so each device is probed with a
harmless UDP request in its own protocol and the round trip is timed:

* motion card ``CardIP:CardPort``: func 0x30 read of RO[0..1] (the frame of checkMCStatus);
* follower ``ZFIP:ZFPort``: CStdModbus read holding register 0, 1 word (devices.md §2);
* optional extra endpoints: any (name, host, port, payload) the caller passes.

A device that answers with any datagram counts as reachable (like a ping reply).
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..protocol import frames as F


@dataclass
class ProbeResult:
    name: str
    host: str
    port: int
    sent: int = 0
    received: int = 0
    rtts_ms: list = field(default_factory=list)
    error: str = ""

    @property
    def loss_pct(self) -> float:
        return 100.0 * (self.sent - self.received) / self.sent if self.sent else 100.0

    @property
    def ok(self) -> bool:
        return self.received > 0

    def summary(self) -> str:
        if not self.rtts_ms:
            why = f" ({self.error})" if self.error else ""
            return (f"{self.name} {self.host}:{self.port}: sent {self.sent}, received 0, "
                    f"100% loss{why}")
        return (f"{self.name} {self.host}:{self.port}: sent {self.sent}, received {self.received}, "
                f"{self.loss_pct:.0f}% loss, rtt min/avg/max = {min(self.rtts_ms):.1f}/"
                f"{sum(self.rtts_ms) / len(self.rtts_ms):.1f}/{max(self.rtts_ms):.1f} ms")


def mc_probe(tid: int = 0) -> bytes:
    """checkMCStatus frame: read 1000 x 2 (FUN_1004ed60)."""
    return F.encode_request(F.Request(F.FUNC_READ, F.REG_STATUS_RO, 2, tid=tid))


def zf_probe(tid: int = 0) -> bytes:
    """Modbus/TCP-style read holding register 0 (CStdModbus layout, tid 0)."""
    return struct.pack(">HHHBBHH", tid & 0xFFFF, 0, 6, 0, 3, 0, 1)


def probe(name: str, host: str, port: int, payload: bytes, count: int = 4,
          timeout_s: float = 1.0) -> ProbeResult:
    r = ProbeResult(name, host, port)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        r.error = str(e)
        return r
    try:
        s.settimeout(timeout_s)
        for _ in range(count):
            r.sent += 1
            t0 = time.perf_counter()
            try:
                s.sendto(payload, (host, port))
                s.recvfrom(2048)
            except (socket.timeout, OSError) as e:
                r.error = str(e) or "timeout"
                continue
            r.received += 1
            r.rtts_ms.append((time.perf_counter() - t0) * 1000.0)
    finally:
        s.close()
    return r


def network_test(card: tuple[str, int] = ("10.1.1.168", 502),
                 follower: Optional[tuple[str, int]] = ("10.1.1.169", 502), count: int = 4,
                 timeout_s: float = 1.0, extra: Iterable[tuple[str, str, int, bytes]] = ()) -> list[ProbeResult]:
    """Probe the card and the follower (the two hosts the original pings)."""
    out = [probe("MC", card[0], card[1], mc_probe(), count, timeout_s)]
    if follower is not None:
        out.append(probe("ZF", follower[0], follower[1], zf_probe(), count, timeout_s))
    for name, host, port, payload in extra:
        out.append(probe(name, host, port, payload, count, timeout_s))
    return out
