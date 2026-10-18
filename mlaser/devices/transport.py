"""Transports and settings shared by the peripheral clients.

Network HALs (CAFNetHalAPI, CECNetHalAPI, CLaserNetHalAPI) share the socket open routine
FUN_100065a0 (observed): ``WSAStartup(1.1)``; access type 0 -> ``socket(AF_INET, SOCK_STREAM)``
+ ``connect`` + SO_SNDTIMEO/SO_RCVTIMEO/SO_LINGER(0); access type 1 -> ``socket(AF_INET,
SOCK_DGRAM)`` with SO_SNDTIMEO/SO_RCVTIMEO only.  Requests go out with ``sendto`` and replies come
back with ``recvfrom`` in both cases.  The retry knobs live in the HAL object and are set by
slot 7 (FUN_100067a0 / FUN_10006820): ``+0x28`` max sends, ``+0x2c`` max receive waits,
``+0x30`` sleep between attempts (ms).

The serial HAL (CSerialHalAPI, open FUN_100068a0 -> CSerialPort::Open FUN_1002afc0) opens
``\\\\.\\COM<n>`` with ``baud = [9600, 19200, 38400, 57600, 115200][index]``, parity argument 2
(mapped to NOPARITY by the switch in FUN_1002afc0), 8 data bits, stop-bits argument 0 (ONESTOPBIT),
no flow control: **8N1**.  Timeouts: ``ReadIntervalTimeout = t``, ``ReadTotalTimeoutConstant =
10 t`` with ``t = SerialTimeout`` (ini 20 ms).  Reads are fixed-length (see codecs).

Cross-platform: sockets use the stdlib; serial ports use ``pyserial`` (imported lazily, only
when a serial transport is opened).
"""
from __future__ import annotations

import select
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

SERIAL_BAUDS = (9600, 19200, 38400, 57600, 115200)   # FUN_100068a0 local table


class TransportError(OSError):
    pass


@dataclass
class DeviceSettings:
    """Endpoints and timing from File/ipAdd.ini ([IP] and [Soft]); defaults are the code
    defaults (``GetPrivateProfileInt`` fallbacks in FUN_10058b70 / FUN_1004ed60) overridden by
    the shipped ini values where those differ."""
    card: tuple[str, int] = ("10.1.1.168", 502)
    zf: tuple[str, int] = ("10.1.1.169", 502)          # ZFIP/ZFPort (CStdModbus, unused)
    onb_zf: tuple[str, int] = ("10.1.1.168", 999)      # OnBZFIP/OnBZFPort
    laser: tuple[str, int] = ("10.1.1.170", 10001)     # LaserIP/LaserPort (laser type 2 = IPG)
    onb_laser: tuple[str, int] = ("10.1.1.168", 888)   # OnBLaserIP/Port (laser type 1 = Raycus)
    af: tuple[str, int] = ("10.1.1.168", 888)          # AFIP/AFPort
    adv_af: tuple[str, int] = ("10.1.1.168", 666)      # AdvAFIP/AdvAFPort
    ec: tuple[str, int] = ("10.1.1.168", 888)          # ECIP/ECPort
    adv_ec: tuple[str, int] = ("10.1.1.168", 666)      # AdvECIP/AdvECPort
    ec3710: tuple[str, int] = ("10.1.1.170", 502)      # EC3710IP/EC3710Port
    access: str = "udp"                                # [Soft] AccessType=1
    zf_timeout_ms: int = 500
    zf_serial_timeout_ms: int = 500
    zf_max_send: int = 3
    zf_max_recv: int = 1
    zf_send_interval_ms: int = 10
    laser_timeout_ms: int = 100
    laser_max_send: int = 5
    laser_max_recv: int = 1
    laser_send_interval_ms: int = 10
    serial_timeout_ms: int = 20
    serial_max_send: int = 3
    serial_max_recv: int = 1
    serial_send_interval_ms: int = 10
    af_timeout_ms: int = 500
    af_max_send: int = 3
    af_max_recv: int = 1
    af_send_interval_ms: int = 10
    ec_timeout_ms: int = 500
    ec_max_send: int = 3
    ec_max_recv: int = 1
    ec_send_interval_ms: int = 10

    @classmethod
    def from_ini(cls, path: str) -> "DeviceSettings":
        vals: dict[str, str] = {}
        with open(path, "rb") as fh:
            for raw in fh.read().decode("gbk", "replace").splitlines():
                if "=" in raw and not raw.lstrip().startswith(";"):
                    k, v = raw.split("=", 1)
                    vals[k.strip()] = v.strip()
        s = cls()

        def ep(name: str, cur: tuple[str, int]) -> tuple[str, int]:
            return vals.get(name + "IP", cur[0]), int(vals.get(name + "Port", cur[1]))

        s.card = ep("Card", s.card)
        s.zf, s.onb_zf, s.laser, s.onb_laser = ep("ZF", s.zf), ep("OnBZF", s.onb_zf), ep("Laser", s.laser), ep("OnBLaser", s.onb_laser)
        s.af, s.adv_af, s.ec, s.adv_ec, s.ec3710 = ep("AF", s.af), ep("AdvAF", s.adv_af), ep("EC", s.ec), ep("AdvEC", s.adv_ec), ep("EC3710", s.ec3710)
        if vals.get("AccessType") == "0":
            s.access = "tcp"
        for f in list(cls.__dataclass_fields__):
            if f.endswith(("_timeout_ms", "_max_send", "_max_recv", "_send_interval_ms")):
                prefix, rest = f.split("_", 1)
                key = {"zf": "ZF", "laser": "Laser", "serial": "Serial", "af": "AF", "ec": "EC"}.get(prefix)
                if prefix == "zf" and rest.startswith("serial"):
                    k = "ZFSerialTimeout"
                else:
                    k = key + {"timeout_ms": "Timeout", "max_send": "MaxSendTime", "max_recv": "MaxRecvTime",
                               "send_interval_ms": "SendInterval"}[rest]
                if k in vals:
                    setattr(s, f, int(vals[k]))
        return s


Validator = Callable[[bytes], bool]


class Transport:
    """``exchange(request, expect_len, validator, delay_ms) -> reply bytes or None``."""

    def open(self) -> "Transport":
        return self

    def close(self) -> None:
        pass

    def send_only(self, data: bytes) -> None:
        raise NotImplementedError

    def exchange(self, data: bytes, expect_len: Optional[int] = None,
                 validator: Optional[Validator] = None, delay_ms: int = 0) -> Optional[bytes]:
        raise NotImplementedError

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


class NetTransport(Transport):
    """Socket transport with the HAL retry loop (CAFNetHalAPI::readReg FUN_1001dc20 and
    CLaserNetHalAPI write FUN_10010cd0): up to ``max_send`` sends; after each send optional
    ``delay_ms`` sleep, then up to ``max_recv`` receive waits of ``timeout_ms``; a reply that does
    not validate counts as a failed attempt; ``interval_ms`` sleep before the next send."""

    def __init__(self, host: str, port: int, access: str = "udp", timeout_ms: int = 500,
                 max_send: int = 3, max_recv: int = 1, interval_ms: int = 10):
        self.host, self.port, self.access = host, port, access
        self.timeout_ms, self.max_send, self.max_recv, self.interval_ms = timeout_ms, max_send, max_recv, interval_ms
        self.sock: Optional[socket.socket] = None
        self.lock = threading.RLock()

    def open(self) -> "NetTransport":
        if self.sock is not None:
            return self
        if self.access == "tcp":
            s = socket.create_connection((self.host, self.port), timeout=max(self.timeout_ms, 1) / 1000)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(max(self.timeout_ms, 1) / 1000)
        self.sock = s
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _drain(self) -> None:
        """FUN_100061f0: discard one stale datagram before a request (UDP)."""
        if self.access != "udp":
            return
        r, _, _ = select.select([self.sock], [], [], 0)
        if r:
            try:
                self.sock.recv(4096)
            except OSError:
                pass

    def send_only(self, data: bytes) -> None:
        with self.lock:
            self.open()
            if self.access == "tcp":
                self.sock.sendall(data)
            else:
                self.sock.sendto(data, (self.host, self.port))

    def _recv(self) -> Optional[bytes]:
        r, _, _ = select.select([self.sock], [], [], self.timeout_ms / 1000)
        if not r:
            return None
        try:
            return self.sock.recv(4096)
        except OSError:
            return None

    def exchange(self, data: bytes, expect_len: Optional[int] = None,
                 validator: Optional[Validator] = None, delay_ms: int = 0) -> Optional[bytes]:
        with self.lock:
            self.open()
            self._drain()
            for _ in range(max(1, self.max_send)):
                self.send_only(data)
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                for _ in range(max(1, self.max_recv)):
                    rx = self._recv()
                    if rx is None:
                        continue
                    if validator is None or validator(rx):
                        return rx
                if self.interval_ms:
                    time.sleep(self.interval_ms / 1000)
            return None

    def recv_only(self) -> Optional[bytes]:
        """CLaserNetHalAPI read (FUN_10010c00): a single ``recvfrom`` without sending."""
        with self.lock:
            self.open()
            return self._recv()


class SerialTransport(Transport):
    """CSerialHalAPI over a serial port (8N1, see module docstring).  ``port`` is a pyserial
    port name (``COM3``, ``/dev/ttyUSB0``)."""

    def __init__(self, port: str, baud_index: int = 0, timeout_ms: int = 20, max_send: int = 3,
                 interval_ms: int = 10, no_reply: bool = False):
        self.port_name, self.baud = port, SERIAL_BAUDS[baud_index]
        self.timeout_ms, self.max_send, self.interval_ms = timeout_ms, max_send, interval_ms
        self.no_reply = no_reply       # FUN_10006a30 sets HAL+0x28: return right after the write
        self.ser = None
        self.lock = threading.RLock()

    def open(self) -> "SerialTransport":
        if self.ser is None:
            try:
                import serial  # pyserial, optional
            except ImportError as e:  # pragma: no cover - depends on environment
                raise TransportError("serial devices need the 'pyserial' package") from e
            self.ser = serial.Serial(self.port_name, self.baud, bytesize=8, parity="N", stopbits=1,
                                     timeout=10 * self.timeout_ms / 1000,
                                     inter_byte_timeout=self.timeout_ms / 1000)
        return self

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    def send_only(self, data: bytes) -> None:
        with self.lock:
            self.open()
            self.ser.reset_input_buffer()          # PurgeComm(0xc) before every write
            self.ser.write(data)

    def exchange(self, data: bytes, expect_len: Optional[int] = None,
                 validator: Optional[Validator] = None, delay_ms: int = 0) -> Optional[bytes]:
        with self.lock:
            for _ in range(max(1, self.max_send)):
                self.send_only(data)
                if self.no_reply:
                    return b""
                if delay_ms:
                    time.sleep(delay_ms / 1000)
                rx = self.ser.read(expect_len or 256)
                if rx and (validator is None or validator(rx)):
                    return rx
                time.sleep(self.interval_ms / 1000)
            return None


class LoopbackTransport(Transport):
    """In-process transport for tests: hands the request to ``handler(bytes) -> bytes|None``
    (a simulator's ``handle``)."""

    def __init__(self, handler: Callable[[bytes], Optional[bytes]], max_send: int = 3):
        self.handler, self.max_send = handler, max_send
        self.sent: list[bytes] = []

    def send_only(self, data: bytes) -> None:
        self.sent.append(data)
        self.handler(data)

    def exchange(self, data: bytes, expect_len: Optional[int] = None,
                 validator: Optional[Validator] = None, delay_ms: int = 0) -> Optional[bytes]:
        for _ in range(max(1, self.max_send)):
            self.sent.append(data)
            rx = self.handler(data)
            if rx is not None and (validator is None or validator(rx)):
                return rx
        return None


class UdpServer:
    """Tiny threaded UDP (or TCP) server around ``handler(bytes) -> bytes|None`` used by the
    device simulators."""

    def __init__(self, handler: Callable[[bytes], Optional[bytes]], host: str = "127.0.0.1",
                 port: int = 0, transport: str = "udp"):
        self.handler, self.host, self.port, self.transport = handler, host, port, transport
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    def start(self) -> "UdpServer":
        if self.transport == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(2)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((self.host, self.port))
        s.settimeout(0.1)
        self.port = s.getsockname()[1]
        self._sock = s
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="dev-sim")
        self._thread.start()
        return self

    def _serve(self) -> None:
        s = self._sock
        while not self._stop.is_set():
            try:
                if self.transport == "tcp":
                    c, _ = s.accept()
                    threading.Thread(target=self._client, args=(c,), daemon=True).start()
                    continue
                data, peer = s.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            out = self.handler(data)
            if out:
                try:
                    s.sendto(out, peer)
                except OSError:
                    pass

    def _client(self, c: socket.socket) -> None:
        c.settimeout(0.1)
        with c:
            while not self._stop.is_set():
                try:
                    data = c.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                out = self.handler(data)
                if out:
                    c.sendall(out)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
