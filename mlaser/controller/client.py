"""Client for the MCC100 motion card (``CMCHalAPI`` + ``CVirtualMachine`` in NCModule.dll).

Transport (observed):
  * UDP by default (``[Soft] AccessType=1`` -> "Access: UDP", FUN_100571f0), TCP when 0.
    UDP: ``socket(AF_INET, SOCK_DGRAM)``, no bind, ``sendto`` CardIP:CardPort
    (10.1.1.168:502).  TCP: ``connect`` + SO_SNDTIMEO/SO_RCVTIMEO/SO_LINGER (FUN_100062a0).
  * Before every request any stale datagram is drained (select 0 + recv 100, FUN_100061f0).
  * Retries (FUN_1001d6d0): ``sends = MCFifoTime // MCTimeout`` (1600//500 = 3) send
    attempts, each followed by up to ``MCMaxRecvTime`` (3) receive waits of ``MCTimeout``
    ms (500).  On the very first timeout the frame is resent immediately (log shows
    "Try-Times(R/S):1/1" then "1/2 2/2 3/2 1/3 2/3 3/3").  ``MCSendInterval`` ms (1) of
    sleep between sends.  Replies with a wrong transaction id are skipped.

All addresses in docstrings refer to Module/NCModule.dll unless stated otherwise.
"""
from __future__ import annotations

import logging
import math
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

from ..protocol import frames as F

if TYPE_CHECKING:  # annotations only (startup imports this module)
    from .startup import CardParameters

log = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    """A request failed; ``code`` uses the original NCModule error numbering."""

    def __init__(self, msg: str, code: int):
        super().__init__(f"{msg} (code {code})")
        self.code = code


@dataclass
class TransportConfig:
    """Timing knobs, defaults from File/ipAdd.ini ([IP] section)."""
    host: str = "10.1.1.168"           # CardIP
    port: int = 502                    # CardPort
    transport: str = "udp"             # AccessType=1 -> udp, 0 -> tcp
    timeout_ms: int = 500              # MCTimeout
    fifo_time_ms: int = 1600           # MCFifoTime (sends = fifo_time // timeout)
    max_recv: int = 3                  # MCMaxRecvTime
    send_interval_ms: int = 1          # MCSendInterval
    fifo_timeout_ms: int = 600         # FifoTimeout (used while streaming FIFO frames) INFERRED
    connect_wait_ms: int = 500         # ConnectWait

    @property
    def max_send(self) -> int:
        return max(1, self.fifo_time_ms // max(1, self.timeout_ms))

    @classmethod
    def from_ini(cls, path: str) -> "TransportConfig":
        """Read ipAdd.ini (GBK text, ``key=value`` lines)."""
        vals: dict[str, str] = {}
        with open(path, "rb") as fh:
            for raw in fh.read().decode("gbk", "replace").splitlines():
                if "=" in raw:
                    k, v = raw.split("=", 1)
                    vals[k.strip()] = v.strip()
        c = cls()
        c.host = vals.get("CardIP", c.host)
        c.port = int(vals.get("CardPort", c.port))
        c.timeout_ms = int(vals.get("MCTimeout", c.timeout_ms)) or 500
        c.fifo_time_ms = int(vals.get("MCFifoTime", c.fifo_time_ms))
        c.max_recv = int(vals.get("MCMaxRecvTime", c.max_recv))
        c.send_interval_ms = int(vals.get("MCSendInterval", c.send_interval_ms))
        c.fifo_timeout_ms = int(vals.get("FifoTimeout", c.fifo_timeout_ms))
        c.connect_wait_ms = int(vals.get("ConnectWait", c.connect_wait_ms))
        if vals.get("AccessType") == "0":
            c.transport = "tcp"
        return c


AX_HOMED_BIT = 0x8000     # axis status word k0 bit 15 = homed (FUN_10039590)


@dataclass
class AxisStatus:
    """One 10-word record of the axis RO block (reg 2000 + 10*axis)."""
    raw: list[int]

    @property
    def status(self) -> int:
        return self.raw[F.AX_STATUS]

    @property
    def alarm(self) -> int:
        """Alarm flag bits 0..5 (hard +/-, soft +/-, servo, dual drive; MainApp FUN_00598280)."""
        return self.status & 0x3F

    @property
    def homed(self) -> bool:
        """Bit 15 = axis homed (O: NCModule FUN_10039590, VM slot 0x1f8 reached through
        CNCModule +0x224, collects bit 15 of the five axis status words; MainApp's status timer
        FUN_00598280 reports 系统回原完成 (mp125) once that mask covers the homed axes)."""
        return bool(self.status & AX_HOMED_BIT)

    @property
    def busy(self) -> bool:
        return bool((self.status >> 16) & 0xFF)

    @property
    def mode(self) -> int:
        return (self.status >> 24) & 0xFF

    @property
    def velocity(self) -> int:
        return _s32(self.raw[F.AX_VELOCITY])

    @property
    def position(self) -> int:
        return _s32(self.raw[F.AX_POSITION])

    @property
    def feedback(self) -> int:
        return _s32(self.raw[F.AX_FEEDBACK])


@dataclass
class MCStatus:
    ro: list[int]
    axes: list[AxisStatus]
    pulses_per_mm: float = 1000.0

    @property
    def inputs(self) -> int:
        return self.ro[F.RO_INPUTS]

    @property
    def outputs(self) -> int:
        return self.ro[F.RO_OUTPUTS]

    @property
    def fifo_seq(self) -> int:
        return self.ro[F.RO_FIFO_SEQ]

    @property
    def fifo_free(self) -> int:
        return self.ro[F.RO_FIFO_FREE]

    @property
    def running(self) -> bool:
        return self.ro[F.RO_RUNNING] == 1

    @property
    def alarm_word1(self) -> int:
        """RO[6]: alarm word 1 (MainApp FUN_00598280 via CNCModule +0x228)."""
        return self.ro[6]

    @property
    def alarm_word2(self) -> int:
        """RO[7]: alarm word 2 (bit 5 = FIFO starvation), CNCModule +0x22c."""
        return self.ro[7]

    @property
    def break_indices(self) -> tuple[int, int]:
        """RO[23], RO[24]: the two ints VM +0x138 (FUN_10039820) returns from VM+0xec/+0xf0,
        i.e. RO block (VM+0x90) words 23/24.  MainApp stores them in AutosaveParam as the
        breakpoint (graph index, path index) - FUN_004693c0.  Meaning INFERRED."""
        return self.ro[23], self.ro[24]

    def alarms(self, axis_names: Optional[dict] = None) -> list:
        """Decoded alarms (:mod:`mlaser.controller.alarms`)."""
        from . import alarms as A
        return A.decode(self.ro[6], self.ro[7], [a.status for a in self.axes], axis_names)

    def homed_mask(self) -> int:
        """Bit n = axis n reports homed (status bit 15; FUN_10039590 builds the same mask)."""
        return sum(1 << i for i, a in enumerate(self.axes) if a.homed)

    def homing(self) -> bool:
        """An axis is busy in mode 2 (FUN_10039500 machine state 1)."""
        return any(a.mode == 2 and a.busy for a in self.axes)

    def machine_state(self) -> int:
        """FUN_10039500: 4 running FIFO, 1 homing, 2 manual motion, 0 idle (names INFERRED)."""
        if self.ro[F.RO_RUNNING] == 1:
            return 4
        for a in self.axes:
            if a.mode == 2 and a.busy:
                return 1
            if a.mode in (3, 4, 5) and a.busy:
                return 2
        return 0

    def position_mm(self, axis: int) -> float:
        """FUN_1002c070: pulses / pulses-per-mm (config +0xbc28)."""
        return self.axes[axis].position / self.pulses_per_mm

    def input(self, n: int) -> bool:
        """Input n (1-based, 1..12 from RO[4]; 13..28 from RO[21])."""
        if 1 <= n <= 12:
            return bool(self.ro[F.RO_INPUTS] >> (n - 1) & 1)
        if 13 <= n <= 28:
            return bool(self.ro[F.RO_INPUTS_EXT] >> (n - 13) & 1)
        raise ValueError(n)

    def output(self, n: int) -> bool:
        """Output n (1-based, 1..10 from RO[5]; 11..26 from RO[22])."""
        if 1 <= n <= 10:
            return bool(self.ro[F.RO_OUTPUTS] >> (n - 1) & 1)
        if 11 <= n <= 26:
            return bool(self.ro[F.RO_OUTPUTS_EXT] >> (n - 11) & 1)
        raise ValueError(n)


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


# ====================================================================== machine scaling
# Card parameter blocks are addressed in 16-bit units: 32-bit word i of the system RW block
# lives at 50000 + 2*i and word k of axis a's block at 50200 + 40*a + 2*k (writes in the
# parameter download FUN_1002e500: 50028 = word 14, 50036 = word 18, 50200 + 40*a per axis;
# reads FUN_10052a20 / FUN_10052800 fetch 26 resp. 100 consecutive words).
SYS_W_PERIOD_US = 5        # AX.InterpolationCycle (cfg +0x4910), FUN_10039f20 case 0
SYS_W_ZF_TYPE = 6          # ZF.ZFType (cfg +0x4994)
SYS_W_DUAL = 14            # dual drive: slave<<8 | Y axis (cfg +0xbc14/+0xbc1c/+0xbbd8)
SYS_W_DUAL_TOL = 15        # MAC.AllowableError (+0xbc20)
SYS_W_DUAL_TIME = 16       # MAC.AlarmTime (+0xbc24)
SYS_W_PPM = 17             # position counts per mm (cfg +0xbc28), FUN_10039f20 case 0xc
SYS_W_BRAKE = 18           # MAC.CloseBreakTime (+0xbc50)
AX_W_LIMITS = 0            # limit/origin switch types (FUN_1002b190 / FUN_10038d00)
AX_W_SOFT_A, AX_W_SOFT_B = 1, 2   # soft limits, counts (/ppm -> mm, +0xbc78)
AX_W_ACC = 3               # MAC.Acceleration (+0xbc80, raw)
AX_W_ACC_TIME = 4          # (+0xbc88, raw)
AX_W_HOME_FAST = 5         # MAC.FastSpeed, counts/s (/ppm -> mm/s, +0xbc90)
AX_W_HOME_SLOW = 6         # MAC.SecondSpeed (+0xbc98)
AX_W_HOME_RETURN = 7       # MAC.ReturnLength, counts (+0xbca0)
AX_W_LEAD = 9              # MAC.SpeedRatio ("lead") * ppm (+0xbca8)
AX_W_PPR = 10              # MAC.WritePluse (pulses per revolution, +0xbcb0)
AX_W_INPUTS = 12           # limit/origin input ports, bytes (+0xbcb8/+0xbcbc/+0xbcc0)
AX_W_BRAKE_OUT = 13        # brake output (+0xbcc4)

STEP_MAX = 127             # signed int8 dx/dy per step item (FUN_10043c20)


def sys_param_addr(word: int) -> int:
    """Register address of system RW word ``word`` (16-bit addressing)."""
    return F.REG_SYS_RW + 2 * word


def axis_param_addr(axis: int, word: int) -> int:
    """Register address of axis RW word ``word`` of card axis ``axis``."""
    return F.REG_AXIS_RW + 40 * axis + 2 * word


@dataclass
class AxisScaling:
    """Per card axis (MachineAxisConfig_n, stride 0xa0 at cfg +0xbc58)."""
    ppr: int = 10000           # MAC.WritePluse default (HardPara)
    lead_mm: float = 100.0     # MAC.SpeedRatio default (HardPara, labelled "lead")
    home_fast: float = 30.0    # mm/s, MAC.FastSpeed default
    home_slow: float = 20.0    # mm/s, MAC.SecondSpeed default
    home_return: float = 10.0  # mm, MAC.ReturnLength default
    acc: float = 20000.0       # MAC.Acceleration default

    @property
    def step_ppm(self) -> float:
        """Step-item pulses per mm.  MainApp FUN_00462630 computes
        ``pulses = Δmm * 10000 * f`` with ``f = FUN_00458b60(axis) = ppr / lead / 10000``
        (DAT_0084cc18 = 10000.0), i.e. ``ppr / lead`` pulses per mm."""
        return self.ppr / self.lead_mm if self.lead_mm else 0.0


@dataclass
class CardScaling:
    """Scaling and timing the card reports (FUN_10039f20, "hardware restore")."""
    period_us: int = 1000      # AX.InterpolationCycle default 1000 µs (CHardwarePropView sets 250)
    ppm: int = 0               # position/velocity counts per mm (0 = card did not report)
    axes: list = field(default_factory=lambda: [AxisScaling() for _ in range(F.N_AXES)])
    x_axis: int = 0            # cfg +0xbbd4
    y_axis: int = 1            # cfg +0xbbd8
    # card soft limits per axis, mm (axis RW words 1/2 / ppm, cfg +0xbc78); missing = not set
    soft_limits: dict = field(default_factory=dict)

    @property
    def period_ms(self) -> float:
        return self.period_us / 1000.0

    def step_ppm(self, axis: int) -> float:
        return self.axes[axis].step_ppm

    @property
    def position_ppm(self) -> float:
        """Divisor for axis RO positions/velocities (FUN_1002c070).  Falls back to the X step
        scale when the card reports 0 (MainApp then shows "Hardware restore failed")."""
        return float(self.ppm) if self.ppm > 0 else self.step_ppm(self.x_axis)

    def max_speed(self, axis: int) -> float:
        """Fastest speed (mm/s) a step item can express: 127 pulses per period."""
        p = self.step_ppm(axis)
        return STEP_MAX / p / (self.period_us * 1e-6) if p and self.period_us else 0.0

    @classmethod
    def from_words(cls, sys_words: Sequence[int], axis_words: Sequence[int],
                   x_axis: int = 0, y_axis: int = 1) -> "CardScaling":
        """Decode the 26-word system block and the 100-word axis block like FUN_10039f20."""
        ppm = _s32(sys_words[SYS_W_PPM]) if len(sys_words) > SYS_W_PPM else 0
        per = _s32(sys_words[SYS_W_PERIOD_US]) if len(sys_words) > SYS_W_PERIOD_US else 0
        axes = []
        soft: dict = {}
        for a in range(F.N_AXES):
            w = list(axis_words[a * F.AXIS_RW_STRIDE:a * F.AXIS_RW_STRIDE + 14]) + [0] * 14
            d = AxisScaling()
            if ppm > 0:
                if w[AX_W_LEAD]:
                    d.lead_mm = _s32(w[AX_W_LEAD]) / ppm
                d.home_fast = _s32(w[AX_W_HOME_FAST]) / ppm or d.home_fast
                d.home_slow = _s32(w[AX_W_HOME_SLOW]) / ppm or d.home_slow
                d.home_return = _s32(w[AX_W_HOME_RETURN]) / ppm or d.home_return
            if w[AX_W_PPR]:
                d.ppr = _s32(w[AX_W_PPR])
            if w[AX_W_ACC]:
                d.acc = float(_s32(w[AX_W_ACC]))
            axes.append(d)
            lo, hi = _s32(w[AX_W_SOFT_A]), _s32(w[AX_W_SOFT_B])
            if ppm > 0 and (lo or hi):
                lo_mm, hi_mm = sorted((lo / ppm, hi / ppm))
                if hi_mm > lo_mm:
                    soft[a] = (lo_mm, hi_mm)
        return cls(period_us=per if per > 0 else 1000, ppm=max(0, ppm), axes=axes,
                   x_axis=x_axis, y_axis=y_axis, soft_limits=soft)

    def summary(self) -> str:
        x, y = self.axes[self.x_axis], self.axes[self.y_axis]
        lim = "; card soft limits " + ", ".join(
            f"axis {a} {lo:g}..{hi:g} mm" for a, (lo, hi) in sorted(self.soft_limits.items())
        ) if self.soft_limits else "; no card soft limits"
        return (f"period {self.period_us} µs, position {self.position_ppm:g} counts/mm, "
                f"X {x.ppr} p/rev / {x.lead_mm:g} mm = {x.step_ppm:g} p/mm, "
                f"Y {y.ppr} p/rev / {y.lead_mm:g} mm = {y.step_ppm:g} p/mm, "
                f"step speed cap {min(self.max_speed(self.x_axis), self.max_speed(self.y_axis)):.0f} mm/s"
                + lim)


# Homing modes (cfg +0xbba8, set by MainApp before CNCModule +0x5c = FUN_10032da0)
HOME_X, HOME_Y, HOME_XY, HOME_XY_ZF, HOME_XY_W, HOME_ZF = 1, 2, 3, 4, 5, 8
HOME_ZF_BIT = 0x10000      # follower (ZF) bit in the homing mask
OP_HOME = 2                # mailbox [2, axis_mask, 0] (FUN_10032da0, write to reg 101)
SYS_RESET = 5              # [9999, 5, 0, 0] alarm reset (FUN_100315f0, CNCModule +0x1e4)


def home_mask(mode: int, x_axis: int = 0, y_axis: int = 1, w_axis: int = 3) -> int:
    """Axis mask FUN_10032da0 builds from the homing mode (switch on cfg +0xbba8)."""
    bx, by, bw = 1 << x_axis, 1 << y_axis, 1 << w_axis
    return {HOME_X: bx, HOME_Y: by, HOME_XY: bx | by, HOME_XY_ZF: bx | by | HOME_ZF_BIT,
            HOME_XY_W: bx | by | bw, HOME_ZF: HOME_ZF_BIT}.get(mode, 0)


def stop_decel(acc_mm_s2: float = 4000.0, factor: int = 1, max_acc: float = 20000.0) -> float:
    """Stop deceleration (mm/s²) as FUN_10038de0: clamp(JogStopDccFactor * XFastMoveAcc,
    2000 (DAT_1008b810), FCP.MaxAcc).  Defaults: factor 1, 4000 mm/s², max 20000 mm/s²."""
    return min(max(factor * acc_mm_s2, 2000.0), max_acc)


class MotionController:
    """Register-level and high-level access to the motion card."""

    def __init__(self, host: str = "10.1.1.168", port: int = 502, transport: str = "udp",
                 config: Optional[TransportConfig] = None, pulses_per_mm: float = 1000.0,
                 apply_hal_filter: bool = True, guard=None):
        self.cfg = config or TransportConfig(host=host, port=port, transport=transport)
        if config is None:
            self.cfg.host, self.cfg.port, self.cfg.transport = host, port, transport
        self.pulses_per_mm = pulses_per_mm   # INFERRED default; real value is a machine parameter
        self.apply_hal_filter = apply_hal_filter
        self._sock: Optional[socket.socket] = None
        self._tid = 0
        self._lock = threading.RLock()
        self._timeout_ms = self.cfg.timeout_ms
        self._rxbuf = b""
        self.fast_len = 0            # VM+0x1204: words of the fast status read (0 = none)
        self.fast_map: list[int] = []
        self.sys_rw: list[int] = []
        self.axis_rw: list[list[int]] = []
        self.fifo_capacity: Optional[int] = None
        # Safety hooks (controller.supervisor.SafetySupervisor): ``guard(req)`` is called for
        # every outgoing request before it is sent and raises to refuse it (sealed: set once,
        # at construction or by the first assignment, never replaced or removed); ``last_ok``
        # is the time.monotonic() of the last successful reply (link watchdog).
        self._guard = guard
        self.last_ok = 0.0
        self.last_sent = 0.0         # time.monotonic() of the last request sent
        self.requests = 0

    @property
    def guard(self):
        return self._guard

    @guard.setter
    def guard(self, fn) -> None:
        if self._guard is not None:
            raise AttributeError("the safety guard of a channel is sealed: it cannot be replaced or removed")
        self._guard = fn

    # ------------------------------------------------------------------ socket
    def open(self) -> "MotionController":
        """Create the socket (FUN_100062a0)."""
        with self._lock:
            self.close()
            if self.cfg.transport == "tcp":
                s = socket.create_connection((self.cfg.host, self.cfg.port),
                                             timeout=self.cfg.connect_wait_ms / 1000)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            self._sock = s
            self._rxbuf = b""
        return self

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def set_timeout(self, timeout_ms: int) -> None:
        """Change the per-receive timeout (FUN_100064e0)."""
        self._timeout_ms = max(1, int(timeout_ms))

    # ------------------------------------------------------------------ low level
    def _drain(self) -> None:
        """FUN_100061f0: drop one pending datagram before sending."""
        s = self._sock
        if s is None or self.cfg.transport == "tcp":
            return
        r, _, _ = select.select([s], [], [], 0)
        if r:
            try:
                s.recv(2048)
            except OSError:
                pass

    def _send(self, data: bytes) -> None:
        if self.cfg.transport == "tcp":
            self._sock.sendall(data)
        else:
            self._sock.sendto(data, (self.cfg.host, self.cfg.port))

    def _recv(self, timeout_s: float) -> Optional[bytes]:
        s = self._sock
        if self.cfg.transport != "tcp":
            r, _, _ = select.select([s], [], [], timeout_s)
            if not r:
                return None
            data, _addr = s.recvfrom(2048)
            return data
        # TCP: reassemble by the LEN field
        deadline = time.monotonic() + timeout_s
        while True:
            if len(self._rxbuf) >= 6:
                ln = int.from_bytes(self._rxbuf[4:6], "big")
                if len(self._rxbuf) >= 6 + ln:
                    fr, self._rxbuf = self._rxbuf[:6 + ln], self._rxbuf[6 + ln:]
                    return fr
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([s], [], [], left)
            if not r:
                return None
            chunk = s.recv(4096)
            if not chunk:
                raise ControllerError("connection closed", 10054)
            self._rxbuf += chunk

    def transact(self, req: F.Request) -> F.Response:
        """Send one request and wait for its reply with the original retry policy."""
        g = self._guard
        if g is not None:
            g(req)                 # raises (CommandRefused) when the safety policy refuses
        with self._lock:
            if self._sock is None:
                self.open()
            self.requests += 1
            req.tid = self._tid
            self._tid = (self._tid + 1) & 0xFFFF
            frame = F.encode_request(req)
            sends, recvs = self.cfg.max_send, max(1, self.cfg.max_recv)
            tmo = self._timeout_ms / 1000
            last = F.ERR_TIMEOUT
            self._drain()
            for s_try in range(sends):
                self.last_sent = time.monotonic()
                self._send(frame)
                for r_try in range(recvs):
                    data = self._recv(tmo)
                    if data is None:
                        last = F.ERR_TIMEOUT
                        log.debug("MC-RecvErr_selectFunc ErrCode:10060 Try-Times(R/S):%d/%d",
                                  r_try + 1, s_try + 1)
                        if s_try == 0 and r_try == 0:
                            break          # first timeout -> resend at once (FUN_1001d6d0)
                        continue
                    try:
                        resp = F.decode_response(data, req.tid)
                    except F.FrameError as e:
                        last = F.ERR_DECODE_BASE + e.code
                        if e.code == 4:
                            continue       # stale reply: keep listening
                        raise ControllerError("DecodeRecvData", last) from e
                    rc = F.check_reply(req, resp)
                    if rc == F.ERR_OK:
                        self.last_ok = time.monotonic()
                        return resp
                    raise ControllerError("RecvDataErr", rc)
                time.sleep(self.cfg.send_interval_ms / 1000)
            raise ControllerError("no reply", last)

    def read_regs(self, addr: int, count: int) -> list[int]:
        """Function 0x30: read ``count`` 32-bit registers."""
        if self.apply_hal_filter:
            fake = F.read_filter(addr)
            if fake is not None:
                if fake == 1:
                    return [0] * count
                raise ControllerError(f"read of {addr} suppressed by HAL filter", 0)
        resp = self.transact(F.Request(F.FUNC_READ, addr, count))
        return resp.values

    def read_reg(self, addr: int) -> int:
        return self.read_regs(addr, 1)[0]

    def write_regs(self, addr: int, values: Sequence[int]) -> None:
        """Function 0x40: write 32-bit registers (syncWriteRegs FUN_1004a620)."""
        vals = [int(v) & 0xFFFFFFFF for v in values]
        if self.apply_hal_filter and F.is_write_filtered(addr):
            log.debug("write to %d dropped (CMCHalAPI filter FUN_10022710)", addr)
            return
        self.transact(F.Request(F.FUNC_WRITE, addr, len(vals), vals))

    def write_reg(self, addr: int, value: int) -> None:
        """syncWriteReg FUN_1004a2f0 (a 503 reply to write(101,101) is tolerated there)."""
        try:
            self.write_regs(addr, [value])
        except ControllerError as e:
            if not (e.code == 503 and addr == 101 and value == 101):
                raise

    # ------------------------------------------------------------------ connect / status
    def connect(self) -> tuple[int, int]:
        """VM event 0 (FUN_100571f0 case 0): open, checkMCStatus (FUN_1004ed60: read RO[0..1])
        and, when the card answered, ``101 := [9999, 5, 0, 0]`` (alarm reset / VM start).

        O: these are the first two frames the original MainApp sends (parity capture
        ``app_startup``).  Earlier versions wrote ``100 := 8888`` here; that is the card
        restart command (mf131, :meth:`hello`), not part of the connect sequence.
        Returns (config_word, version_code).
        """
        self.open()
        self.fast_len = 0
        cfgw, ver = self.read_regs(F.REG_STATUS_RO, 2)
        self.reset_alarms()
        return cfgw, ver

    def hello(self) -> None:
        """write(100, 8888) - FUN_10039300 (VM +0x168): card restart (mf131, firmware update)."""
        self.write_reg(F.REG_CONTROL, F.CONTROL_HELLO)

    def apply_parameters(self) -> None:
        """write(100, 9999) - end of the parameter download FUN_1002e500. INFERRED purpose."""
        self.write_reg(F.REG_CONTROL, F.CONTROL_APPLY)

    # ------------------------------------------------------------------ start-up (startup.py)
    def download_parameters(self, params: "CardParameters" = None) -> int:
        """Parameter download FUN_1002e500 (CNCModule +0x194) followed by VM +0x248
        (FUN_10054e70: clear FIFO, read the FIFO capacity at 1032).  Returns the capacity
        (65000 when register 1032 cannot be read, as the original)."""
        from .startup import FIFO_CAPACITY_DEFAULT, FIFO_CAPACITY_REG, CardParameters, download_requests
        for reg, words in download_requests(params or CardParameters()):
            self.write_regs(reg, words)
        self.fifo_clear()
        try:
            self.fifo_capacity = self.read_reg(FIFO_CAPACITY_REG)
        except ControllerError:
            self.fifo_capacity = FIFO_CAPACITY_DEFAULT
        return self.fifo_capacity

    def write_fast_map(self, regs: Optional[Sequence[int]] = None) -> None:
        """FUN_1003ee50 (VM +0x20c): ``60000 := pack(regs)``.  The fast length (VM+0x1204) is
        set *before* the write, so the poll switches to the fast read even when the card
        rejects the map (O)."""
        from .startup import FAST_MAP, FAST_MAP_REG, pack_fast_map
        regs = list(FAST_MAP if regs is None else regs)
        words = pack_fast_map(regs)
        self.fast_len = len(words) * 2
        self.fast_map = regs
        self.write_regs(FAST_MAP_REG, words)

    def read_status_fast(self) -> list[int]:
        """updateMCStatusFast FUN_10052c30: read ``fast_len`` words at 60001."""
        return self.read_regs(F.REG_FAST, self.fast_len)

    def read_servo_info(self) -> list[int]:
        """writeServoInfo FUN_10054a00 (VM +0x210): read 63000 x 128 (ServoInfo.txt)."""
        from .startup import SERVO_INFO_COUNT
        return self.read_regs(F.REG_SERVO_INFO, SERVO_INFO_COUNT)

    def poll(self) -> MCStatus:
        """One cycle of the status poll FUN_1004eab0 (VM +0x144).

        Without a fast map: RO 1000 x 36, axis RO 2000 x 50, axis RW 50200 x 100, system RW
        50000 x 26.  With one (``fast_len > 0``): RO, system RW, then fast 60001 x fast_len,
        whose words 0..49 are the axis RO block and 50..119 the 14 used words per axis
        parameter block (FUN_10052c30).  Caches ``sys_rw`` and ``axis_rw`` (14 per axis).
        """
        ro = self.read_regs(F.REG_STATUS_RO, F.N_STATUS_RO)
        if self.fast_len < 1:
            ax = self.read_regs(F.REG_AXIS_RO, F.N_AXES * F.AXIS_RO_STRIDE)
            rw = self.read_regs(F.REG_AXIS_RW, F.N_AXES * F.AXIS_RW_STRIDE)
            self.axis_rw = [rw[i * 20:i * 20 + 14] for i in range(F.N_AXES)]
            self.sys_rw = self.read_regs(F.REG_SYS_RW, F.N_SYS_RW)
        else:
            self.sys_rw = self.read_regs(F.REG_SYS_RW, F.N_SYS_RW)
            fast = self.read_status_fast() + [0] * 120
            ax = fast[:50]
            self.axis_rw = [fast[50 + 14 * i:64 + 14 * i] for i in range(F.N_AXES)]
        axes = [AxisStatus(ax[i * 10:(i + 1) * 10]) for i in range(F.N_AXES)]
        return MCStatus(ro, axes, self.pulses_per_mm)

    def startup(self, params: "CardParameters" = None) -> tuple[int, int]:
        """The original's start-up sequence (parity capture ``app_startup``), licence gate
        excluded: connect, one status poll, parameter download (+ FIFO clear and capacity),
        fast-status map, FIFO clear, servo info.  Later :meth:`poll` calls use the fast read.  The original's 59500 hardware challenge between the
        poll and the download is a licensing check and is intentionally not sent
        (docs/re/protocol.md §8).  Returns (config_word, version_code)."""
        cfgw, ver = self.connect()
        self.poll()
        # CNCModule +0x48 = FUN_1002b3f0: +0x194 download (ends with VM +0x248: 103 := 1,
        # read 1032), VM +0x20c fast map, +0x220 = FUN_1002bde0 (103 := 1), VM +0x210 servo info
        self.download_parameters(params)
        self.write_fast_map()
        self.fifo_clear()
        self.read_servo_info()
        return cfgw, ver

    def read_status(self) -> MCStatus:
        """updateMCStatusRO + updateMCStatusAxisRO (FUN_100521b0, FUN_100523c0)."""
        ro = self.read_regs(F.REG_STATUS_RO, F.N_STATUS_RO)
        ax = self.read_regs(F.REG_AXIS_RO, F.N_AXES * F.AXIS_RO_STRIDE)
        axes = [AxisStatus(ax[i * 10:(i + 1) * 10]) for i in range(F.N_AXES)]
        return MCStatus(ro, axes, self.pulses_per_mm)

    def read_system_params(self) -> list[int]:
        """updateMCStatusSystemRW (FUN_10052a20): 26 words at 50000."""
        return self.read_regs(F.REG_SYS_RW, F.N_SYS_RW)

    def read_axis_params(self) -> list[list[int]]:
        """updateMCStatusAxisRW (FUN_10052800): 100 words at 50200, 14 used per axis."""
        w = self.read_regs(F.REG_AXIS_RW, F.N_AXES * F.AXIS_RW_STRIDE)
        return [w[i * 20:i * 20 + 14] for i in range(F.N_AXES)]

    def read_scaling(self, x_axis: int = 0, y_axis: int = 1) -> CardScaling:
        """Read the system and axis parameter blocks and decode them (FUN_10039f20)."""
        sysw = self.read_system_params()
        axw = self.read_regs(F.REG_AXIS_RW, F.N_AXES * F.AXIS_RW_STRIDE)
        return CardScaling.from_words(sysw, axw, x_axis, y_axis)

    def write_sys_param(self, word: int, value: int) -> None:
        """Write system RW word ``word`` (address 50000 + 2*word, as FUN_1002e500 does)."""
        self.write_reg(sys_param_addr(word), value)

    def write_axis_param(self, axis: int, word: int, values) -> None:
        """Write axis RW words starting at ``word`` (address 50200 + 40*axis + 2*word)."""
        vals = list(values) if isinstance(values, (list, tuple)) else [values]
        self.write_regs(axis_param_addr(axis, word), vals)

    def position_mm(self) -> tuple[float, ...]:
        st = self.read_status()
        return tuple(st.position_mm(i) for i in range(F.N_AXES))

    # ------------------------------------------------------------------ mailbox commands
    def command(self, words: Sequence[int]) -> None:
        """Immediate command: write(101, [op, params...])."""
        self.write_regs(F.REG_MAILBOX, list(words))

    def set_outputs(self, mask: int, value: int) -> None:
        """Several outputs at once; ``mask`` / ``value`` are port masks (bit n-1 = output n):
        ``[9999, 2, mask, value]`` for outputs 1..10, ``[9999, 13, mask, value]`` for the
        extended IO outputs 11..26 (:func:`mlaser.protocol.frames.output_bit`)."""
        for sub, m16 in F.sub_masks_of_ports(mask):
            shift = 0 if sub == F.SYS_OUTPUTS else F.N_BASE_OUTPUTS
            self.command(F.mailbox_sys(sub, m16, (int(value) >> shift) & m16))

    def set_output(self, n: int, on: bool) -> None:
        """FUN_10030620: output n (1..10 -> sub 2, 11..26 -> sub 13)."""
        sub, bit = F.output_bit(n)
        self.command(F.mailbox_sys(sub, 1 << bit, int(bool(on)) << bit))

    def set_da(self, channel: int, value: int) -> None:
        """Analog output (laser power). FUN_100308f0: channel 1/2, value clamped 50..10000
        (DAMinVal=50; 10000 = full scale, INFERRED 0.01 % units)."""
        if channel not in (1, 2):
            raise ValueError(channel)
        v = int(value)
        if 0 < v < 50:
            v = 50
        v = min(max(v, 0), 10000)
        self.command(F.mailbox_sys(F.SYS_DA, channel - 1, v))

    def set_pwm(self, frequency: int, duty: int, bus: bool = False) -> None:
        """[9999, 3, frequency, duty] (FUN_100309f0; [9999, 17, ...] on bus cards).
        Units UNKNOWN (config fields +0x48bc / +0x4818)."""
        self.command(F.mailbox_sys(F.SYS_PWM_BUS if bus else F.SYS_PWM, frequency, duty))

    def jog(self, axis: int, speed: int, accel: int, distance: int, flag: bool = False) -> None:
        """Single-axis jog, FUN_10032b80 (CNCModule +0x4c):
        ``[3, axis(|0x80000000), speed, acc, acc*10, distance]``.

        Word 1 is the axis **number** (X 0, Y 1), not a mask: FUN_10032b80 copies its argument
        unchanged and MainApp's jog FUN_005bdae0 passes the panel axis index (asm 0x5be0c8..
        0x5be101; capture: X jog ``[3, 0, ...]``, Y jog ``[3, 1, ...]``).  ``flag`` sets bit 31
        (meaning UNKNOWN; MainApp's XY jog passes 0).  Speed and distance are card counts
        (×cfg+0xbc28), the acceleration word is passed through as given (MainApp sends
        MC.XFastMoveAcc unscaled, see ``UiController.jog_start``).
        """
        if not 0 <= int(axis) < F.N_AXES:
            raise ValueError(f"jog axis {axis}")
        word = int(axis) | (0x80000000 if flag else 0)
        self.command([F.OP_JOG, word, int(speed), int(accel), int(accel) * 10, int(distance) & 0xFFFFFFFF])

    def jog_mm(self, axis: int, speed_mm_s: float, accel: int, distance_mm: float) -> None:
        """Jog with speed and distance in mm (truncated like MainApp's __ftol2:
        ``ftol(v·ppm)``, ``ftol(d·ppm)``); ``accel`` is the raw acceleration word."""
        p = self.pulses_per_mm
        self.jog(axis, int(speed_mm_s * p), int(accel), int(distance_mm * p))

    def move(self, axis_mask: int, speed: int, accel: int, targets: Sequence[int],
             flag: bool = False) -> None:
        """Multi-axis move, FUN_10032910: [5, mask(|0x80000000), speed, acc, acc*10, p1..p4].
        p-order observed as stack args 0xc, 0x10, 0x18, 0x14 (X, Y, then two more; INFERRED).

        The p values are **relative** distances (counts): every MainApp caller passes target −
        current position (FUN_005bea00 for the edge seek, FUN_005ca160 return-to-tool-origin;
        O), and NC logs the command as "start jog"."""
        t = list(targets) + [0] * (4 - len(targets))
        mask = axis_mask | (0x80000000 if flag else 0)
        self.command([F.OP_MOVE, mask, speed, accel, accel * 10, *[v & 0xFFFFFFFF for v in t[:4]]])

    def stop(self, decel: int = 0, zf_lift: Optional[tuple[float, float]] = None) -> None:
        """Stop jogging ("vm stop jog", FUN_100571f0 events 10/11 with no job running):
        FUN_10054900 = ``[1, 0x1f, 2, decel, 200000]``, then ``[118, 5, 0]``.

        The original sends ``[103, ZFUpSpeed*10, ZFDockHeight*1000]`` (op 103 = Z to an absolute
        height, 0x100575fc) in between only when the follower state is 0x75; pass
        ``zf_lift=(up_speed_mm_s, dock_height_mm)`` to send it."""
        self.stop_motion(decel or 0)
        if zf_lift is not None:
            self.command([F.OP_STOP, int(zf_lift[0] * 10), int(zf_lift[1] * 1000)])
        self.command([F.OP_FOLLOW, 5, 0])

    def stop_axes(self, mask: int = 0x1F) -> None:
        """[1, mask] (FUN_100301a0). INFERRED: stop/disable listed axes."""
        self.command([F.OP_STOP_AXES, mask])

    def zero_position(self, arg: int = 0) -> None:
        """[9999, 8, arg] (FUN_100314b0). INFERRED: set current position as zero."""
        self.command(F.mailbox_sys(F.SYS_ZERO, arg))

    def home(self, mask: int) -> None:
        """Go origin: mailbox ``[2, axis_mask, 0]`` (FUN_10032da0 = CNCModule +0x5c, observed
        ``push 0x65`` before the syncWriteRegs call at 0x10032f32).  Bit n = card axis n,
        bit 16 = follower (ZF).  The card runs the whole sequence itself with the downloaded
        axis parameters (home fast/slow speed, return distance, switch type, direction); the
        axes report mode 2 + busy while homing (FUN_10039500 state 1).  The older
        "z signal go origin" write to reg 309 (FUN_10037c10) is dropped by the HAL filter."""
        self.command([OP_HOME, mask & 0xFFFFFFFF, 0])

    def home_mode(self, mode: int, x_axis: int = 0, y_axis: int = 1, w_axis: int = 3) -> int:
        """Home by mode (1 X, 2 Y, 3 XY, 4 XY+ZF, 5 XY+W, 8 ZF); returns the mask sent."""
        m = home_mask(mode, x_axis, y_axis, w_axis)
        if m:
            self.home(m)
        return m

    def reset_alarms(self) -> None:
        """Clear alarms: ``[9999, 5, 0, 0]`` (FUN_100315f0 = CNCModule +0x1e4, called by the
        Clear Alarm handler FUN_004b8690 and before the axis-parameter download)."""
        self.command(F.mailbox_sys(SYS_RESET, 0, 0))

    def stop_motion(self, decel: int, mask: int = 0x1F, jerk: int = 200000) -> None:
        """Decelerated stop of all axes, FUN_10054900: ``[1, 0x1f, 2, decel, 200000]``
        (decel = FUN_10038de0 in counts/s², INFERRED unit)."""
        self.command([F.OP_STOP_AXES, mask, 2, int(decel), int(jerk)])

    def stop_manu(self, decel: int, pwm_freq: int = 0, outputs_off: int = 0,
                  fifo_stop: bool = False, follower: bool = True, ext_outputs_off: int = 0) -> None:
        """The VM's program stop (FUN_100571f0 events 10/11 with a job running, "vm stop
        manu"; posted by both "send stop manu cmd" FUN_10046420 and "send pause manu cmd"
        FUN_10046560):

        1. FUN_10054900: FIFO stop (103 := 3) when the VM flag +0xdc is set, otherwise the
           decelerated axis stop ``[1, 0x1f, 2, decel, 200000]``;
        2. PWM off ``[9999, 3, freq, 0, 0]`` and ``[9999, 17, freq, 0, 0]``;
        3. process outputs off ``[9999, 2, 0xffff, outputs & ~process]`` (+ ``[9999, 13, ..]``);
        4. follower stop ``[118, 5, 0]``.

        ``outputs_off`` is a port mask (bit n-1 = output n, 1..26): outputs 1..10 go out with
        sub 2, 11..26 with sub 13; ``ext_outputs_off`` is an extra sub-13 mask (bit k = output
        11+k).
        """
        if fifo_stop:
            self.fifo_stop()
        else:
            self.stop_motion(decel)
        self.command(F.mailbox_sys(F.SYS_PWM, pwm_freq, 0, 0))
        self.command(F.mailbox_sys(F.SYS_PWM_BUS, pwm_freq, 0, 0))
        words = dict(F.sub_masks_of_ports(outputs_off))
        if ext_outputs_off & 0xFFFF:
            words[F.SYS_OUTPUTS_EXT] = words.get(F.SYS_OUTPUTS_EXT, 0) | (ext_outputs_off & 0xFFFF)
        for sub in (F.SYS_OUTPUTS, F.SYS_OUTPUTS_EXT):
            if words.get(sub):
                self.command(F.mailbox_sys(sub, words[sub], 0))
        if follower:
            self.command([F.OP_FOLLOW, 5, 0])

    # ------------------------------------------------------------------ FIFO
    def fifo_clear(self) -> None:
        self.write_reg(F.REG_FIFO_CTRL, F.FIFO_CLEAR)      # FUN_10051bf0

    def fifo_start(self) -> None:
        self.write_reg(F.REG_FIFO_CTRL, F.FIFO_START)      # FUN_100515e0

    def fifo_stop(self) -> None:
        self.write_reg(F.REG_FIFO_CTRL, F.FIFO_STOP)       # FUN_100518b0

    def fifo_write_frame(self, frame: Sequence[int]) -> None:
        """write(102, [seq, items...]) with the FIFO timeout (FUN_10052e90)."""
        old = self._timeout_ms
        self.set_timeout(self.cfg.fifo_timeout_ms)
        try:
            self.write_regs(F.REG_FIFO_DATA, frame)
        finally:
            self.set_timeout(old)

    def streamer(self, **kw) -> "FifoStreamer":
        return FifoStreamer(self, **kw)


# ====================================================================== FIFO streamer
@dataclass
class StreamStats:
    frames: int = 0
    items: int = 0
    words: int = 0


class FifoStreamer:
    """Convert motion items to FIFO wire items and stream them with flow control.

    Flow control as in fillFifo (FUN_10052e90): before each frame the card's
    RO[16] (free bytes) must be >= frame_bytes + 2000; frame sequence numbers start
    at RO[15] + 1.  At most ``frames_per_fill`` (20) frames per poll.

    Accepted items (duck-typed by class name, see mlaser.toolpath.items):
      * ``list[int]`` - an already encoded wire item (header + params)
      * Rapid / CutLine / CutArc - interpolated into op-3000 step items
      * Delay - zero-motion step items (safe encoding; the native delay op is UNKNOWN)
      * IO - [9999, 2|13, mask, value] FIFO item
      * LaserOn/LaserOff - set the laser byte of following cut steps
      * LayerChange/Pierce/Follow/Marker - no wire item (UNKNOWN encodings), ignored
    """

    def __init__(self, mc: MotionController, tick_s: float = 0.001,
                 pulses_per_mm: Optional[float] = None, rapid_speed: float = 200.0,
                 laser_power: int = 0, frames_per_fill: int = 20, margin_bytes: int = 2000):
        self.mc = mc
        self.tick_s = tick_s
        # tick_s must equal the card's servo period: each step item is one period
        self.ppmm = pulses_per_mm or mc.pulses_per_mm
        self.rapid_speed = rapid_speed
        self.laser_power = laser_power
        self.frames_per_fill = frames_per_fill
        self.margin = margin_bytes
        self._pos = (0.0, 0.0)
        self._pulse = [0, 0]
        self._laser = False
        self.stats = StreamStats()

    # -------------------------------------------------------------- encoding
    def set_origin(self, x_mm: float, y_mm: float) -> None:
        self._pos = (x_mm, y_mm)
        self._pulse = [round(x_mm * self.ppmm), round(y_mm * self.ppmm)]

    def _steps_to(self, pts: Iterable[tuple[float, float]], speed: float, laser: bool) -> list[list[int]]:
        out = []
        for x, y in pts:
            tx, ty = round(x * self.ppmm), round(y * self.ppmm)
            while True:
                dx, dy = tx - self._pulse[0], ty - self._pulse[1]
                if dx == 0 and dy == 0:
                    break
                n = max(1, math.ceil(max(abs(dx), abs(dy)) / 127))
                sx, sy = round(dx / n), round(dy / n)
                out.append(F.encode_step(sx, sy, 5000, 100 if laser else 0, self.laser_power or 0xFFFF))
                self._pulse[0] += sx
                self._pulse[1] += sy
            self._pos = (x, y)
        return out

    def _sample_line(self, a, b, speed):
        L = math.dist(a, b)
        step = max(speed * self.tick_s, 1e-9)
        n = max(1, math.ceil(L / step))
        return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]

    def _sample_arc(self, it, speed):
        cx, cy = it.center
        r = math.dist(it.center, it.start)
        a0 = math.atan2(it.start[1] - cy, it.start[0] - cx)
        a1 = math.atan2(it.end[1] - cy, it.end[0] - cx)
        d = a1 - a0
        if it.ccw:
            while d <= 1e-12:
                d += 2 * math.pi
        else:
            while d >= -1e-12:
                d -= 2 * math.pi
        L = abs(d) * r
        n = max(1, math.ceil(L / max(speed * self.tick_s, 1e-9)))
        pts = [(cx + r * math.cos(a0 + d * i / n), cy + r * math.sin(a0 + d * i / n)) for i in range(1, n)]
        pts.append(tuple(it.end))
        return pts

    def _reposition(self, p) -> list[list[int]]:
        """Laser-off traverse when a cut does not start at the current point."""
        if math.dist(self._pos, p) <= 1e-6:
            return []
        return self._steps_to(self._sample_line(self._pos, p, self.rapid_speed), self.rapid_speed, False)

    def encode_item(self, it) -> list[list[int]]:
        """One motion item -> list of wire items."""
        if isinstance(it, (list, tuple)) and it and isinstance(it[0], int):
            return [list(it)]
        name = type(it).__name__
        if name == "Rapid":
            return self._steps_to(self._sample_line(self._pos, it.end, self.rapid_speed),
                                  self.rapid_speed, False)
        if name == "CutLine":
            pre = self._reposition(it.start)
            return pre + self._steps_to(self._sample_line(it.start, it.end, it.feed), it.feed,
                                        self._laser and it.laser)
        if name == "CutArc":
            pre = self._reposition(it.start)
            return pre + self._steps_to(self._sample_arc(it, it.feed), it.feed, self._laser and it.laser)
        if name == "Delay":
            n = max(1, round(it.seconds / self.tick_s))
            return [F.encode_step(0, 0, 5000, 100 if self._laser else 0, self.laser_power or 0xFFFF)
                    for _ in range(n)]
        if name == "IO":
            p = int(it.port)
            if 1 <= p <= 10:
                return [F.sys_item(F.SYS_OUTPUTS, 1 << (p - 1), int(bool(it.value)) << (p - 1))]
            if 11 <= p <= 26:
                return [F.sys_item(F.SYS_OUTPUTS_EXT, 1 << (p - 11), int(bool(it.value)) << (p - 11))]
            raise ValueError(p)
        if name == "LaserOn":
            self._laser = True
            return []
        if name == "LaserOff":
            self._laser = False
            return []
        return []      # LayerChange, Pierce, Follow, Marker: wire encoding UNKNOWN

    def encode(self, items: Iterable) -> list[list[int]]:
        out: list[list[int]] = []
        for it in items:
            out.extend(self.encode_item(it))
        return out

    # -------------------------------------------------------------- streaming
    def stream(self, items: Iterable, start: bool = True, timeout_s: float = 60.0,
               wait_done: bool = False, poll_s: float = 0.03) -> StreamStats:
        """Clear FIFO, send all frames with flow control, start, optionally wait for idle."""
        wire = self.encode(items)
        mc = self.mc
        mc.fifo_clear()
        st = mc.read_status()
        frames = F.build_fifo_frames(wire, st.fifo_seq + 1)
        self.stats = StreamStats(items=len(wire))
        deadline = time.monotonic() + timeout_s
        i = 0
        started = False
        while i < len(frames):
            if time.monotonic() > deadline:
                raise ControllerError("FIFO stream timeout", F.ERR_TIMEOUT)
            st = mc.read_status()
            free = st.fifo_free
            sent = 0
            while i < len(frames) and sent < self.frames_per_fill:
                fr = frames[i]
                need = (len(fr) + 3) * 4 + self.margin
                if free < need:
                    break
                mc.fifo_write_frame(fr)
                free -= len(fr) * 4
                self.stats.frames += 1
                self.stats.words += len(fr)
                i += 1
                sent += 1
            if start and not started and (i >= len(frames) or sent == 0):
                mc.fifo_start()
                started = True
            if sent == 0:
                time.sleep(poll_s)
        if start and not started:
            mc.fifo_start()
        if wait_done:
            while time.monotonic() < deadline:
                if not mc.read_status().running:
                    break
                time.sleep(poll_s)
        return self.stats
