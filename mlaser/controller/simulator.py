"""Software emulation of the MCC100 motion card for tests and hardware-free GUI use.

It speaks the frame format of :mod:`mlaser.controller.frames` over UDP (default,
like the real card) or TCP, keeps a register image, executes mailbox commands
(reg 101), FIFO control (reg 103) and FIFO frames (reg 102) over time and reports
status through the 1000/2000 register blocks.  Behaviour that the reverse
engineering could not pin down (time unit of step items, what a jog flag means,
...) follows the INFERRED choices documented in docs/protocol.md.

Run standalone::

    python -m mlaser.controller.simulator --port 5020
"""
from __future__ import annotations

import argparse
import collections
import logging
import select
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import frames as F
from .startup import FAST_LEN_MAX, FAST_MAP, FAST_MAP_REG

log = logging.getLogger(__name__)

EXC_ILLEGAL_FUNCTION = 1
EXC_ILLEGAL_ADDRESS = 2
EXC_ILLEGAL_VALUE = 3


@dataclass
class SimAxis:
    pos: float = 0.0          # pulses (float for sub-pulse velocity integration)
    vel: float = 0.0          # pulses/s (signed, last tick)
    target: Optional[float] = None
    speed: float = 0.0
    mode: int = 0             # byte3 of the status word: 2 homing, 3 jog, 5 move (INFERRED)
    alarm: int = 0            # latched alarm flags, bits 0..5 (alarms.AX_*)
    homed: bool = False
    limit_pos: bool = False   # hardware limit switch inputs (test hooks)
    limit_neg: bool = False
    servo_alarm: bool = False

    @property
    def busy(self) -> bool:
        return self.target is not None


@dataclass
class SimState:
    axes: list = field(default_factory=lambda: [SimAxis() for _ in range(F.N_AXES)])
    outputs: int = 0
    inputs: int = 0
    da: list = field(default_factory=lambda: [0, 0])
    pwm: tuple = (0, 0)
    control: int = 0
    fifo: collections.deque = field(default_factory=collections.deque)
    fifo_bytes: int = 0
    fifo_seq: int = 0
    fifo_running: bool = False
    laser: bool = False
    laser_power: int = 0
    step_left_s: float = 0.0
    step_vel: tuple = (0.0, 0.0)
    params: dict = field(default_factory=dict)       # 5000.., 50000.., 59500.., 59600..
    alarm_w1: int = 0         # latched RO[6] bits >= 24 (e-stop 30, servo 29, bus 25 ...)
    alarm_w2: int = 0         # latched RO[7] bits (FIFO starvation 5, overspeed 7 ...)
    estop: bool = False
    marks: int = 0            # RO[23]: OP_MARK items executed (INFERRED breakpoint counter)
    steps_since_mark: int = 0  # RO[24]
    underruns: int = 0        # FIFO drained while running with data expected (statistics)
    min_fifo_bytes: Optional[int] = None   # lowest FIFO fill seen while running
    hold_left_s: float = 0.0  # op 2001: remaining delay / wait timeout
    wait: Optional[dict] = None   # pending op-2001 condition (frames.decode_wait)
    zf_state: int = 0         # follower state reached by ops 103..109 (2 arrived, 3 follow, 4 pierce)
    zf_height_um: int = 0
    pwm_on: bool = False
    laser_sync: int = 0       # 9999/18 source (0 = beam gate off)
    history: list = field(default_factory=list)      # executed FIFO items (op, params)
    latch: list = field(default_factory=lambda: [0] * F.N_AXES)   # axis RO k7 (edge-seek latch)
    zf_regs: dict = field(default_factory=dict)      # follower status overrides (index -> value)
    zf_edge: Optional[dict] = None                   # armed edge seek (mailbox 117)
    downloads: list = field(default_factory=list)    # func 0x26 writes (addr, bytes)
    control_writes: list = field(default_factory=list)   # values written to register 100
    reboots: int = 0                                 # 100 := 8888 after a download (I: firmware reboot)
    hw_blocks: dict = field(default_factory=dict)    # 59600 + slot -> words [0xffffffff, 0xe0, ...]
    commands: list = field(default_factory=list)     # mailbox commands received
    fast_map: list = field(default_factory=lambda: list(FAST_MAP))   # 60000 map (FUN_1003ee50)
    fast_map_writes: int = 0


@dataclass
class SimSheet:
    """A sheet on the table for the edge-seek model: polygon (mm, machine coordinates) seen
    by the capacitive follower.  The follower reports "edge found" (follower reg 2 =
    0x80000006, edgeseek.ZF_EDGE_FOUND) when the head leaves it while edge seek is armed."""
    polygon: list

    @classmethod
    def rect(cls, x0: float, y0: float, w: float, h: float, angle_deg: float = 0.0) -> "SimSheet":
        """Rectangle with corner (x0, y0), rotated by ``angle_deg`` about that corner."""
        import math
        c, s_ = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
        pts = [(0, 0), (w, 0), (w, h), (0, h)]
        return cls([(x0 + c * x - s_ * y, y0 + s_ * x + c * y) for x, y in pts])

    @classmethod
    def disc(cls, cx: float, cy: float, r: float, n: int = 720) -> "SimSheet":
        import math
        return cls([(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
                    for i in range(n)])

    def contains(self, x: float, y: float) -> bool:
        inside = False
        pts = self.polygon
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside


class CardSimulator:
    """Threaded UDP/TCP server emulating the card.

    Every op-3000 step item lasts one servo period, ``period_ms`` (1 ms default; the item
    itself carries no duration, see ``frames.encode_step``).  ``fifo_capacity`` bytes are reported through RO[16].
    ``drop_replies`` drops that many replies first (to exercise client retries).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, transport: str = "udp",
                 version: int = 20177, period_ms: float = 1.0, fifo_capacity: int = 64 * 1024,
                 onboard_zf: bool = False, time_scale: float = 1.0, drop_replies: int = 0,
                 ppm: float = 1000.0, step_ppm: Optional[float] = None, ppr: int = 10000,
                 home_speed: float = 200.0, soft_limits: Optional[dict] = None,
                 starvation_alarm: bool = False, sheet: Optional[SimSheet] = None):
        self.host, self.port, self.transport = host, port, transport
        self.version = version
        self.period_ms = period_ms
        self.fifo_capacity = fifo_capacity
        self.onboard_zf = onboard_zf
        self.time_scale = time_scale
        self.drop_replies = drop_replies
        self.ppm = float(ppm)                         # position counts per mm (sys RW word 17)
        self.step_ppm = float(step_ppm or ppm)        # step-item pulses per mm (ppr / lead)
        self.soft_limits = dict(soft_limits or {})    # axis -> (min_mm, max_mm)
        self.starvation_alarm = starvation_alarm      # raise RO[7] bit 5 when the FIFO runs dry
        self.sheet = sheet                            # edge-seek model (SimSheet), None = no sheet
        if sheet is not None:
            self.onboard_zf = True
        self.state = SimState()
        self._init_params(ppr, home_speed)
        self.lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self.requests = 0

    def _init_params(self, ppr: int, home_speed: float) -> None:
        """Card parameter image in the layout FUN_10039f20 decodes (16-bit addressing:
        word i of the system block at 50000 + 2i, word k of axis a at 50200 + 40a + 2k)."""
        from .client import (AX_W_ACC, AX_W_HOME_FAST, AX_W_HOME_RETURN, AX_W_HOME_SLOW,
                             AX_W_LEAD, AX_W_PPR, SYS_W_PERIOD_US, SYS_W_PPM, axis_param_addr,
                             sys_param_addr)
        p = self.state.params
        p[sys_param_addr(SYS_W_PERIOD_US)] = int(round(self.period_ms * 1000))
        p[sys_param_addr(SYS_W_PPM)] = int(round(self.ppm))
        lead = ppr / self.step_ppm
        for a in range(F.N_AXES):
            p[axis_param_addr(a, AX_W_LEAD)] = int(round(lead * self.ppm))
            p[axis_param_addr(a, AX_W_PPR)] = int(ppr)
            p[axis_param_addr(a, AX_W_HOME_FAST)] = int(round(home_speed * self.ppm))
            p[axis_param_addr(a, AX_W_HOME_SLOW)] = int(round(home_speed / 4 * self.ppm))
            p[axis_param_addr(a, AX_W_HOME_RETURN)] = int(round(10 * self.ppm))
            p[axis_param_addr(a, AX_W_ACC)] = 20000

    @property
    def counts_per_step(self) -> float:
        """Position counts per step-item pulse (ppm / step_ppm)."""
        return self.ppm / self.step_ppm

    # ------------------------------------------------------------------ lifecycle
    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    def start(self) -> "CardSimulator":
        if self.transport == "tcp":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.listen(4)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((self.host, self.port))
        self.port = s.getsockname()[1]
        self._sock = s
        self._stop.clear()
        srv = threading.Thread(target=self._serve_tcp if self.transport == "tcp" else self._serve_udp,
                               name="mcc-sim-net", daemon=True)
        mot = threading.Thread(target=self._motion_loop, name="mcc-sim-motion", daemon=True)
        self._threads = [srv, mot]
        for t in self._threads:
            t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=1.0)
        self._sock = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ network
    def _serve_udp(self):
        s = self._sock
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([s], [], [], 0.05)
                if not r:
                    continue
                data, addr = s.recvfrom(2048)
            except OSError:
                break
            out = self.handle(data)
            if out is not None:
                try:
                    s.sendto(out, addr)
                except OSError:
                    pass

    def _serve_tcp(self):
        s = self._sock
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([s], [], [], 0.05)
                if not r:
                    continue
                c, _ = s.accept()
            except OSError:
                break
            threading.Thread(target=self._tcp_client, args=(c,), daemon=True).start()

    def _tcp_client(self, c: socket.socket):
        buf = b""
        with c:
            while not self._stop.is_set():
                try:
                    r, _, _ = select.select([c], [], [], 0.05)
                    if not r:
                        continue
                    chunk = c.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 6:
                    ln = int.from_bytes(buf[4:6], "big")
                    if len(buf) < 6 + ln:
                        break
                    fr, buf = buf[:6 + ln], buf[6 + ln:]
                    out = self.handle(fr)
                    if out is not None:
                        c.sendall(out)

    def handle(self, data: bytes) -> Optional[bytes]:
        """Process one request frame and return the reply bytes (or None to drop)."""
        self.requests += 1
        try:
            req = F.decode_request(data)
        except F.FrameError:
            return None
        with self.lock:
            try:
                if req.func == F.FUNC_READ and F.REG_HWPARAM <= req.addr < F.REG_HWPARAM + 32:
                    blk = list(self.state.hw_blocks.get(req.addr, []))
                    if not blk:
                        raise KeyError(req.addr)
                    vals = (blk + [0] * req.count)[:req.count]
                    resp = F.Response(F.FUNC_READ, req.tid, req.addr, req.count, vals)
                elif req.func == F.FUNC_WRITE and F.REG_HWPARAM <= req.addr < F.REG_HWPARAM + 32:
                    # writeHardParam2Card FUN_10050fb0: one block per slot register (O layout)
                    if req.values[:2] != [0xFFFFFFFF, 0xE0]:
                        raise ValueError("bad hardware parameter block header")
                    self.state.hw_blocks[req.addr] = list(req.values)
                    resp = F.Response(F.FUNC_WRITE, req.tid, req.addr, req.count)
                elif req.func == F.FUNC_WRITE_BYTES_AT:
                    # func 0x26 [addr, n, bytes] (fillDownFile FUN_10053930 / FUN_10053c70)
                    self.state.downloads.append((req.addr, bytes(req.values)))
                    resp = F.Response(F.FUNC_WRITE_BYTES_AT, req.tid, req.addr, req.count)
                elif req.func == F.FUNC_READ:
                    k = _stride(req.addr)
                    vals = [self._read(req.addr + k * i) for i in range(req.count)]
                    resp = F.Response(F.FUNC_READ, req.tid, req.addr, req.count, vals)
                elif req.func == F.FUNC_WRITE:
                    self._write(req.addr, req.values)
                    resp = F.Response(F.FUNC_WRITE, req.tid, req.addr, req.count)
                else:
                    resp = F.Response(req.func, req.tid, exception=EXC_ILLEGAL_FUNCTION)
            except KeyError:
                resp = F.Response(req.func, req.tid, exception=EXC_ILLEGAL_ADDRESS)
            except ValueError:
                resp = F.Response(req.func, req.tid, exception=EXC_ILLEGAL_VALUE)
        if self.drop_replies > 0:
            self.drop_replies -= 1
            return None
        return F.encode_response(resp)

    # ------------------------------------------------------------------ registers
    def _ro(self, i: int) -> int:
        st = self.state
        if i == F.RO_CONFIG:
            return 0
        if i == F.RO_VERSION:
            return self.version
        if i == F.RO_INPUTS:
            return st.inputs & 0xFFF
        if i == F.RO_OUTPUTS:
            return st.outputs & 0x3FF
        if i == F.RO_FIFO_SEQ:
            return st.fifo_seq & 0xFFFFFFFF
        if i == F.RO_FIFO_FREE:
            return max(0, self.fifo_capacity - st.fifo_bytes)
        if i == F.RO_RUNNING:
            return 1 if st.fifo_running else 0
        if i == 6:
            return self.alarm_word1()
        if i == 7:
            return st.alarm_w2 & 0xFFFFFFFF
        if i == 23:
            return st.marks & 0xFFFFFFFF
        if i == 24:
            return st.steps_since_mark & 0xFFFFFFFF
        if i == 32:
            # reg 1032: FUN_10054e70 reads one word here after the parameter download and
            # keeps it as the FIFO capacity (cfg +0x47f0, 65000 when unreadable).  O read, I value
            return self.fifo_capacity
        if i == F.RO_INPUTS_EXT:
            return (st.inputs >> 12) & 0xFFFF
        if i == F.RO_OUTPUTS_EXT:
            return (st.outputs >> 10) & 0xFFFF
        return 0

    def _axis_ro(self, a: int, k: int) -> int:
        ax = self.state.axes[a]
        if k == F.AX_STATUS:
            # bit 15 = homed (NCModule FUN_10039590 collects it for MainApp's "homing done")
            return ((ax.mode << 24) | ((1 if ax.busy else 0) << 16) | (0x8000 if ax.homed else 0)
                    | (self._axis_alarm(ax) & 0x3F))
        if k == F.AX_VELOCITY:
            return round(ax.vel) & 0xFFFFFFFF
        if k in (F.AX_POSITION, F.AX_FEEDBACK):
            return round(ax.pos) & 0xFFFFFFFF
        if k == F.AX_EDGE_LATCH:
            return self.state.latch[a] & 0xFFFFFFFF
        return 0

    def _read(self, addr: int) -> int:
        if F.REG_CONTROL <= addr <= F.REG_FIFO_CTRL:
            return self.state.control if addr == F.REG_CONTROL else 0
        if F.REG_STATUS_RO <= addr < F.REG_STATUS_RO + F.N_STATUS_RO:
            return self._ro(addr - F.REG_STATUS_RO)
        if F.REG_LATCH <= addr < F.REG_LATCH + 3:
            return 0
        if F.REG_AXIS_RO <= addr < F.REG_AXIS_RO + F.N_AXES * F.AXIS_RO_STRIDE:
            off = addr - F.REG_AXIS_RO
            return self._axis_ro(off // 10, off % 10)
        if F.REG_ONBZF <= addr < F.REG_ONBZF + F.N_ONBZF:
            if not self.onboard_zf:
                raise KeyError(addr)          # real card: exception 2 (Log/2026-09-16.log)
            i = addr - F.REG_ONBZF
            if i in self.state.zf_regs:
                return self.state.zf_regs[i] & 0xFFFFFFFF
            if i == 2 and self.sheet is not None:
                return 0x80000000              # homed (bit 31)
            return self.state.params.get(addr, 0)
        if F.REG_FAST <= addr < F.REG_FAST + 144:
            return self._fast(addr - F.REG_FAST)
        if (5000 <= addr <= 5008 or 50000 <= addr < 50400 or 59500 <= addr < 59700
                or F.REG_SERVO_INFO <= addr < F.REG_SERVO_INFO + 0x80):
            return self.state.params.get(addr, 0)
        raise KeyError(addr)

    def _fast(self, i: int) -> int:
        """Reg 60001 + i: the value of the i-th register of the fast-status map written to
        60000 (FUN_1003ee50; the original's map is the default, see startup.FAST_MAP)."""
        regs = self.state.fast_map
        if i >= len(regs):
            return 0
        return self._reg16(regs[i])

    def _reg16(self, reg: int) -> int:
        """Value of a register named in the fast map.  The map addresses the axis RO block in
        16-bit units (2000 + 2j = axis RO word j, O from FUN_1003ee50 / FUN_10052c30), like
        the parameter blocks; other registers resolve as single-word reads."""
        if F.REG_AXIS_RO <= reg < F.REG_AXIS_RO + 2 * F.N_AXES * F.AXIS_RO_STRIDE:
            j = (reg - F.REG_AXIS_RO) // 2
            return self._axis_ro(j // F.AXIS_RO_STRIDE, j % F.AXIS_RO_STRIDE)
        try:
            return self._read(reg)
        except KeyError:
            return 0

    def _write(self, addr: int, vals: list[int]) -> None:
        st = self.state
        if addr == F.REG_CONTROL:
            st.control = vals[0]
            st.control_writes.append(vals[0])
            if vals[0] == F.CONTROL_HELLO and st.downloads:
                st.reboots += 1               # I: 8888 after a 0x26 download = reboot into the new image
            return
        if addr == F.REG_MAILBOX:
            st.commands.append(list(vals))
            self._mailbox(vals)
            return
        if addr == F.REG_FIFO_DATA:
            self._fifo_frame(vals)
            return
        if addr == FAST_MAP_REG:
            # fast-status map (FUN_1003ee50): 2 u16 register numbers per word, low half first
            if not vals or len(vals) > FAST_LEN_MAX // 2:
                raise ValueError("bad fast map length")
            st.fast_map = [r for w in vals for r in (w & 0xFFFF, (w >> 16) & 0xFFFF)]
            st.fast_map_writes += 1
            return
        if addr == F.REG_FIFO_CTRL:
            v = vals[0]
            if v == F.FIFO_CLEAR:
                st.fifo.clear()
                st.fifo_bytes = 0
                st.fifo_running = False
                st.step_left_s = 0.0
                st.marks = st.steps_since_mark = 0
                st.min_fifo_bytes = None
                st.hold_left_s, st.wait = 0.0, None
            elif v == F.FIFO_START:
                st.fifo_running = True
            elif v == F.FIFO_STOP:
                st.fifo_running = False
            else:
                raise ValueError(v)
            return
        if 5000 <= addr <= 5008 or 50000 <= addr < 50400 or 59500 <= addr < 59700:
            k = _stride(addr)
            for i, v in enumerate(vals):
                st.params[addr + k * i] = v
            return
        raise KeyError(addr)

    # ------------------------------------------------------------------ commands
    def _sys(self, sub: int, p: list[int]) -> None:
        st = self.state
        p = p + [0] * 4
        if sub == F.SYS_OUTPUTS:
            m, v = p[0] & 0xFFFF, p[1] & 0xFFFF
            st.outputs = (st.outputs & ~m) | (v & m)
        elif sub == F.SYS_OUTPUTS_EXT:
            m, v = (p[0] & 0xFFFF) << 10, (p[1] & 0xFFFF) << 10
            st.outputs = (st.outputs & ~m) | (v & m)
        elif sub == F.SYS_DA:
            if p[0] in (0, 1):
                st.da[p[0]] = p[1]
        elif sub in (F.SYS_PWM, F.SYS_PWM_BUS):
            st.pwm = (p[0], p[1])                    # [3, freq, duty, on]
            st.pwm_on = bool(p[2]) and bool(p[1])
        elif sub == getattr(F, "SYS_LASER_SYNC", 18):
            st.laser_sync = p[0]
        elif sub == F.SYS_ZERO:
            for ax in st.axes:
                ax.pos = 0.0
        elif sub == F.SYS_INIT:
            self.reset_alarms()
        # SYS_INIT, SYS_AXIS_CFG, SYS_9, SYS_18: accepted, no visible effect

    def _mailbox(self, w: list[int]) -> None:
        if not w:
            raise ValueError("empty command")
        op, p = w[0], w[1:]
        st = self.state
        if op == F.OP_SYS:
            self._sys(p[0] if p else 0, p[1:])
        elif op == F.OP_JOG and len(p) >= 5:
            if self._motion_blocked():
                return
            # [3, axis(|0x80000000), v, a, 10a, d]: word 1 is the axis NUMBER (FUN_10032b80
            # copies MainApp's axis index; X 0, Y 1), relative distance d
            a = p[0] & 0x7FFFFFFF
            speed = abs(_s32(p[1]))
            dist = _s32(p[4])
            if 0 <= a < F.N_AXES:
                ax = st.axes[a]
                ax.target, ax.speed, ax.mode = ax.pos + dist, max(speed, 1), 3
        elif op == F.OP_MOVE and len(p) >= 4:
            # [5, mask, v, a, 10a, d1, d2, d4, d3]: RELATIVE move (MainApp passes target − current,
            # FUN_005bea00 / FUN_005ca160; FUN_10032910 logs "start jog").  The axes move on a
            # straight line at ``v`` along the path.
            if self._motion_blocked():
                return
            mask = p[0] & 0x7FFFFFFF
            speed = max(abs(_s32(p[1])), 1)
            raw = [_s32(v) for v in p[4:8]] + [0] * 4
            deltas = [raw[0], raw[1], raw[3], raw[2]]
            sel = [a for a in range(F.N_AXES) if mask >> a & 1][:4]
            ds = {a: float(deltas[k]) for k, a in enumerate(sel)}
            norm = sum(d * d for d in ds.values()) ** 0.5 or 1.0
            for a, d in ds.items():
                if d == 0:
                    continue
                ax = st.axes[a]
                ax.target, ax.speed, ax.mode = ax.pos + d, max(speed * abs(d) / norm, 1e-6), 5
        elif op == EDGE_SEEK_CMD:
            # [117, v*10, follow_um, up_um, sens, step_um, 0, out_v*10, 1] (FUN_10031cc0)
            st.zf_regs[3] = EDGE_SEEK_CMD
            st.zf_regs[2] = 0x80000003 if self.sheet is not None else 0x80000000
            st.zf_regs[14] = st.zf_regs[15] = 0
            st.zf_edge = {"out_speed": (_s32(p[6]) / 10.0) if len(p) > 6 else 50.0,
                          "start": None, "found": False}
        elif op == CANCEL_CMD:
            # write(101, 101): follower cancel (NC +0xe8)
            st.zf_edge = None
            st.zf_regs[3] = CANCEL_CMD
            st.zf_regs[2] = 0x80000000
        elif op == OP_HOME and p:
            # [2, mask, 0]: go origin (FUN_10032da0).  The card homes on its own; here every
            # selected axis runs to its home switch at 0 with the "fast" speed (axis word 5).
            if self._motion_blocked():
                return
            from .client import AX_W_HOME_FAST, axis_param_addr
            mask = p[0]
            for a in range(F.N_AXES):
                if mask >> a & 1:
                    ax = st.axes[a]
                    sp = st.params.get(axis_param_addr(a, AX_W_HOME_FAST), 0) or 50 * self.ppm
                    ax.target, ax.speed, ax.mode, ax.homed = 0.0, float(sp), 2, False
        elif op == F.OP_STOP_AXES:
            # [1, mask] / [1, 0x1f, 2, decel, jerk] (FUN_10054900): stop axes; with the
            # decelerating form the running program (FIFO) stops too.
            mask = p[0] if p else 0x1F
            for a, ax in enumerate(st.axes):
                if mask >> a & 1:
                    ax.target, ax.vel, ax.mode = None, 0.0, 0
            if len(p) >= 2:
                self._halt_fifo()
        elif op == F.OP_STOP:
            # [103, up_speed*10, z_um]: Z to an absolute height (frames.OP_Z_ABS); XY unaffected
            pass
        # OP_FOLLOW and others: accepted, ignored

    def _wait_ok(self, w: dict) -> bool:
        """Is an op-2001 condition met?  (input: 0-based input number == value; follower
        state; axis states are always met in the simulator)."""
        st = self.state
        if w["mode"] == F.WAIT_INPUT:
            return bool(st.inputs >> w["input"] & 1) == bool(w["value"])
        if w["mode"] == F.WAIT_ZF:
            return st.zf_state == w["state"]
        return True

    # ------------------------------------------------------------------ alarms (test hooks)
    def _axis_alarm(self, ax: SimAxis) -> int:
        from . import alarms as A
        v = ax.alarm
        if ax.limit_pos:
            v |= 1 << A.AX_HARD_POS
        if ax.limit_neg:
            v |= 1 << A.AX_HARD_NEG
        if ax.servo_alarm:
            v |= 1 << A.AX_SERVO
        return v

    def alarm_word1(self) -> int:
        from . import alarms as A
        st = self.state
        w = st.alarm_w1 | ((1 << A.W1_ESTOP) if st.estop else 0)
        for a, ax in enumerate(st.axes):
            if self._axis_alarm(ax):
                w |= 1 << a
        return w & 0xFFFFFFFF

    def _motion_blocked(self) -> bool:
        return self.alarm_word1() != 0

    def _halt_fifo(self) -> None:
        st = self.state
        st.fifo_running = False
        st.step_left_s = 0.0
        st.laser = False
        for ax in st.axes[:2]:
            ax.vel = 0.0

    def _trip(self) -> None:
        """An alarm stops all motion and the program (the card's reaction, INFERRED)."""
        st = self.state
        for ax in st.axes:
            ax.target, ax.vel = None, 0.0
            if ax.mode != 0:
                ax.mode = 0
        self._halt_fifo()

    def set_limit(self, axis: int, positive: bool = True, on: bool = True) -> None:
        """Hardware limit switch input (latched as an axis alarm until reset)."""
        from . import alarms as A
        with self.lock:
            ax = self.state.axes[axis]
            if positive:
                ax.limit_pos = on
            else:
                ax.limit_neg = on
            if on:
                ax.alarm |= 1 << (A.AX_HARD_POS if positive else A.AX_HARD_NEG)
                self._trip()

    def set_estop(self, on: bool = True) -> None:
        """Emergency-stop input (RO[6] bit 30)."""
        from . import alarms as A
        with self.lock:
            self.state.estop = on
            if on:
                self.state.alarm_w1 |= 1 << A.W1_ESTOP
                self._trip()

    def set_servo_alarm(self, axis: int, on: bool = True) -> None:
        """Servo drive alarm input of one axis (axis status bit 4)."""
        from . import alarms as A
        with self.lock:
            ax = self.state.axes[axis]
            ax.servo_alarm = on
            if on:
                ax.alarm |= 1 << A.AX_SERVO
                self._trip()

    def reset_alarms(self) -> None:
        """[9999, 5, 0, 0]: clear latched alarms whose cause has gone."""
        st = self.state
        for ax in st.axes:
            ax.alarm = self._axis_alarm(SimAxis(limit_pos=ax.limit_pos, limit_neg=ax.limit_neg,
                                                servo_alarm=ax.servo_alarm))
        st.alarm_w1 = 0
        st.alarm_w2 = 0

    def _fifo_frame(self, words: list[int]) -> None:
        st = self.state
        if not words:
            raise ValueError("empty frame")
        items = F.parse_fifo_items(words[1:])
        nbytes = 4 * len(words)
        if nbytes > self.fifo_capacity - st.fifo_bytes:
            raise ValueError("FIFO overflow")
        st.fifo_seq = words[0]
        for op, params in items:
            nb = 4 * (1 + len(params))
            st.fifo.append((op, params, nb))
            st.fifo_bytes += nb

    # ------------------------------------------------------------------ motion
    def _motion_loop(self):
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(0.002)
            now = time.monotonic()
            dt = (now - last) * self.time_scale
            last = now
            with self.lock:
                self.advance(dt)

    def advance(self, dt: float) -> None:
        """Advance the machine by ``dt`` seconds (also callable directly in tests)."""
        st = self.state
        prev = (st.axes[0].pos, st.axes[1].pos)
        for ax in st.axes:
            if ax.target is None:
                if not st.fifo_running:
                    ax.vel = 0.0
                continue
            d = ax.target - ax.pos
            stepv = ax.speed * dt
            if abs(d) <= stepv:
                ax.vel = d / dt if dt > 0 else 0.0
                ax.pos = ax.target
                if ax.mode == 2:
                    ax.homed = True
                ax.target, ax.mode = None, 0
            else:
                s = stepv if d > 0 else -stepv
                ax.pos += s
                ax.vel = s / dt
        if st.fifo_running:
            self._run_fifo(dt)
        if st.zf_edge is not None and self.sheet is not None:
            self._edge_model(prev)
        self._check_soft_limits()

    def _edge_model(self, prev: tuple) -> None:
        """Capacitive edge detection (INFERRED card behaviour reproducing what MainApp reads):
        while armed, the first time the head crosses from inside the sheet to outside, latch
        the crossing in axis RO k7, set follower reg 15 = samples (0.25 ms) from the start of
        the move at the move-out speed, reg 14 = reg 15, reg 2 = 0x80000006.  The axes keep
        moving until the PC stops them (FUN_005b6b30 sends [1, 0x1f])."""
        st, e = self.state, self.state.zf_edge
        x, y = st.axes[0], st.axes[1]
        cur = (x.pos, y.pos)
        moving = x.busy or y.busy
        if e["found"] or not moving:
            if not moving and not e["found"]:
                e["start"] = None
            return
        if e["start"] is None:
            e["start"] = prev
        p = self.ppm
        if self.sheet.contains(prev[0] / p, prev[1] / p) and not self.sheet.contains(cur[0] / p, cur[1] / p):
            lo, hi = 0.0, 1.0
            for _ in range(40):
                m = (lo + hi) / 2
                q = (prev[0] + (cur[0] - prev[0]) * m, prev[1] + (cur[1] - prev[1]) * m)
                if self.sheet.contains(q[0] / p, q[1] / p):
                    lo = m
                else:
                    hi = m
            q = (prev[0] + (cur[0] - prev[0]) * hi, prev[1] + (cur[1] - prev[1]) * hi)
            st.latch[0], st.latch[1] = round(q[0]), round(q[1])
            dist_mm = ((q[0] - e["start"][0]) ** 2 + (q[1] - e["start"][1]) ** 2) ** 0.5 / p
            samples = round(dist_mm / max(e["out_speed"], 1e-6) / 0.00025)
            st.zf_regs[15] = st.zf_regs[14] = samples
            st.zf_regs[2] = 0x80000006
            e["found"] = True

    def firmware_image(self) -> bytes:
        """The bytes received through func 0x26, assembled by address."""
        buf = bytearray()
        for addr, data in self.state.downloads:
            if len(buf) < addr + len(data):
                buf.extend(b"\0" * (addr + len(data) - len(buf)))
            buf[addr:addr + len(data)] = data
        return bytes(buf)

    def _check_soft_limits(self) -> None:
        from . import alarms as A
        for a, (lo, hi) in self.soft_limits.items():
            ax = self.state.axes[a]
            mm = ax.pos / self.ppm
            bit = A.AX_SOFT_POS if mm > hi else (A.AX_SOFT_NEG if mm < lo else None)
            if bit is not None and not ax.alarm >> bit & 1 and ax.mode != 2:
                ax.alarm |= 1 << bit
                self._trip()

    def _run_fifo(self, dt: float) -> None:
        st = self.state
        x, y = st.axes[0], st.axes[1]
        while dt > 0:
            if st.hold_left_s > 0:
                if st.wait is not None and self._wait_ok(st.wait):
                    st.wait, st.hold_left_s = None, 0.0
                    continue
                use = min(dt, st.hold_left_s)
                st.hold_left_s -= use
                dt -= use
                x.vel = y.vel = 0.0
                if st.hold_left_s <= 1e-12:
                    st.hold_left_s = 0.0
                    if st.wait is not None:              # timed out -> alarm, program stops
                        from . import alarms as A
                        bit = 3 if st.wait["mode"] == F.WAIT_ZF else 4
                        st.alarm_w2 |= 1 << bit
                        st.wait = None
                        self._halt_fifo()
                        return
                continue
            if st.step_left_s > 0:
                use = min(dt, st.step_left_s)
                x.pos += st.step_vel[0] * use
                y.pos += st.step_vel[1] * use
                x.vel, y.vel = st.step_vel
                st.step_left_s -= use
                dt -= use
                if st.step_left_s <= 1e-12:
                    # land exactly on the integer step target
                    if float(self.counts_per_step).is_integer():
                        x.pos, y.pos = round(x.pos), round(y.pos)
                    st.step_left_s = 0.0
                continue
            if not st.fifo:
                st.fifo_running = False      # INFERRED: card reports idle when drained
                st.laser = False
                x.vel = y.vel = 0.0
                st.underruns += 1
                if self.starvation_alarm:
                    from . import alarms as A
                    st.alarm_w2 |= 1 << A.W2_FIFO_STARVATION
                return
            op, params, nb = st.fifo.popleft()
            st.fifo_bytes = max(0, st.fifo_bytes - nb)
            if st.min_fifo_bytes is None or st.fifo_bytes < st.min_fifo_bytes:
                st.min_fifo_bytes = st.fifo_bytes
            st.history.append((op, params))
            if op == F.OP_STEP and len(params) >= 2:
                dx, dy, _freq, duty, power = F.decode_step(params)
                dur = self.period_ms * 1e-3
                k = self.counts_per_step
                st.laser, st.laser_power = duty != 0, power
                st.step_vel = (dx * k / dur, dy * k / dur)
                st.step_left_s = dur
                st.steps_since_mark += 1
            elif op == F.OP_MARK:
                st.marks += 1
                st.steps_since_mark = 0
            elif op == F.OP_WAIT and len(params) >= 2:
                w = F.decode_wait(params)
                if w["mode"] == F.WAIT_TIME:
                    st.hold_left_s = w["ms"] / 1000.0
                elif not self._wait_ok(w):
                    st.wait, st.hold_left_s = w, max(w["timeout_ms"], 1) / 1000.0
            elif op in (103, 108, 109) and params:
                # Z absolute / gradual / relative -> follower "arrived" (state 2), INFERRED model
                if op == 103 and len(params) >= 2:
                    st.zf_height_um = _s32(params[1])
                elif op == 109 and len(params) >= 2:
                    st.zf_height_um -= _s32(params[1])
                elif op == 108 and len(params) >= 3:
                    st.zf_height_um = _s32(params[2])
                st.zf_state = 2
            elif op == 104:
                st.zf_state = 3                      # following
            elif op == 105:
                st.zf_state = 4                      # follow-to (pierce height) done
            elif op == 3 and len(params) >= 5:
                # extra-axis move [axis|0x80000000, v, acc, jerk, pos] (focus axis); instant
                a = params[0] & 0xFF
                if 0 <= a < F.N_AXES:
                    st.axes[a].pos = float(_s32(params[4]))
            elif op == F.OP_SYS and params:
                self._sys(params[0], params[1:])
            # other ops: consumed without effect

    # ------------------------------------------------------------------ helpers
    def set_input(self, n: int, on: bool) -> None:
        with self.lock:
            bit = 1 << (n - 1)
            self.state.inputs = (self.state.inputs | bit) if on else (self.state.inputs & ~bit)

    def position(self, axis: int) -> float:
        with self.lock:
            return self.state.axes[axis].pos

    def wait_idle(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                st = self.state
                if not st.fifo_running and not st.fifo and all(not a.busy for a in st.axes):
                    return True
            time.sleep(0.005)
        return False


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


OP_HOME = 2
EDGE_SEEK_CMD = 117     # mailbox 0x75: follower edge-seek mode (NC FUN_10031cc0)
CANCEL_CMD = 101        # write(101, 101): follower cancel (NC +0xe8)


def _stride(addr: int) -> int:
    """Register stride of a multi-word access: the parameter blocks at 50000 (system) and
    50200 (axes) are addressed in 16-bit units, so 32-bit word i sits at addr + 2i."""
    return 2 if 50000 <= addr < 50400 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="MCC100 motion card simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--tcp", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    sim = CardSimulator(a.host, a.port, "tcp" if a.tcp else "udp").start()
    print(f"MCC100 simulator on {a.host}:{sim.port} ({sim.transport}); Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
