"""Capacitive height follower ("ZF", 调高器).

Observed (Module/NCModule.dll) - the live path in this build is the **on-board follower inside
the motion card** (CExtModbus frames, 32-bit registers, see docs/re/protocol.md):

* Status: updateZFStatus FUN_1004f3e0 reads 18 registers at **10000** (func 0x30) when the
  follower parameter ``sys+0x4994`` is non-zero, every ``ZFUpdateFactor`` cycles, and stores
  them at VM+0x36c = ``getReg(7, i)``.  A card without the follower answers exception 2
  (Log/2026-09-16.log ``Buffer:02 be d0 45 00 03 00 b0 02``).
* Properties: updateZFProp FUN_1004f720 reads 41 registers at **11000** (0x2af8) into VM+0x3b4
  = ``getReg(8, i)``; it first sets ``sys+0x4a98 = 20000`` and ``sys+0x4a9c = 200``.
* syncReadZFReg FUN_1004ae90 (VM slot +0x7c): MC read ``[0x30, addr, 2]``, then (0x1004af75)
  ``value = vec[3] << 16 + vec[2]`` where the CExtModbus reply vector is ``[func, addr, count,
  r0, r1]``.  So the original returns ``(r0 << 16) + 2`` - the indexing was evidently copied from
  the RTU (AF) code, whose vector is ``[func, bytecount, w0, w1]`` (observed quirk).  The flag
  test FUN_100394b0 (``syncReadZFReg(10004) & 0x80000000``) therefore tests **bit 15 of
  register 10004**.
* MainApp (ZF view, FUN_0062acd0 / FUN_0062fd40) shows ``getReg(7, i)`` with the labels
  ``ZFReadOnly01..17`` (see :data:`STATUS_FIELDS`); ``reg(7,2)`` bit 31 = homed, low byte =
  calibration grade (0x10 excellent, 0x11 good, 0x12 bad); ``reg(7,3) == 107`` while the head
  calibration runs.  Position-like values are micrometres (x0.001 mm).
* Follower command mailbox = MC register 101 with a single value: ``107`` starts the head
  calibration, ``101`` cancels it (MainApp FUN_0062cd50 -> CNCModule +0xf0 / +0xe8); the servo
  calibration (FUN_0062d240) writes ``[107, app+0xbc28 * 10]`` (field UNKNOWN).
* Properties are written one by one with ``writeReg(11000 + 2*idx, value)`` then committed with
  ``writeReg(150, 9999)``; factory reset ``writeReg(150, 5555)`` (MainApp FUN_00633000 /
  FUN_00633a10; the UI password gates "1273"/"3721" are not reproduced).  See :data:`PROP_FIELDS`.
* Calibration curve (VM slot +0xcc FUN_10042cf0, MainApp FUN_00633da0 "Calib Curve"): count
  ``n`` at register 12000, then ``2n+2`` registers from 12002 read in chunks of <= 200 and
  paired; MainApp writes the pairs with ``fprintf("%d, %d\n")`` to ``Help/calib.csv``.
* Follow on/off FUN_10041740 (VM slot +0x80): mailbox 101 ``[118, 4, on, int(sys+0x4dd0)]``,
  sent only when ``sys+0x4dcc`` (follow enabled) and ``sys+0x4994`` are set.
* "ZF Signal correction" FUN_10041630: mailbox ``[118, 7]`` (calibration start), only while the
  follower is present (``VM+0x42c == 1``).
* "z signal go origin" CNCModule +0x60 FUN_10037c10: writes 7 values to MC register **309**,
  which the HAL write filter drops in this build (dead code); homing Z goes through the
  normal axis home instead.
* fillZFDownFile FUN_10053c70: MC function 0x26 ``[0x26, addr, n, bytes...]`` (n <=
  ``MaxZFDownItem=512``) - a byte download (INFERRED firmware/table).

The ini also has a stand-alone network follower ``ZFIP:ZFPort`` (10.1.1.169:502) with the
CStdModbus codec; that codec is never referenced in this build, so :class:`NetworkFollower` is a
best-effort Modbus/TCP-like client (transport TCP INFERRED) with the byte-exact encoder.

Calibration table: ``Help/calib.csv`` (see :class:`CalibrationTable`).
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from mlaser.protocol import frames as F

from .codecs import FUNC_READ_HOLDING, FUNC_WRITE_MULTI, STD_MODBUS, CodecError

REG_STATUS = 10000
N_STATUS = 18
REG_PROPS = 11000
N_PROPS = 41
REG_FLAG = 10004
REG_ZORIGIN = 309          # dead in this build (HAL filter), FUN_10037c10
REG_MAILBOX = 101
REG_COMMIT = 150
COMMIT_SAVE = 9999
COMMIT_FACTORY = 5555
CMD_CALIBRATE = 107
CMD_CANCEL = 101
REG_CURVE_COUNT = 12000
REG_CURVE = 12002
CURVE_CHUNK = 200

# getReg(7, i) labels (MainApp label array 0xadcb20, FUN_007d5d60); scale applies to the raw int
STATUS_FIELDS = [
    ("info", 1), ("alarm", 1), ("run_status", 1), ("run_command", 1), ("signal_strength", 1),
    ("signal_height_mm", 0.001), ("axis_position_mm", 0.001), ("speed", 0.1), ("follow_error", 1),
    ("follow_time", 1), ("plate_coordinate_mm", 0.001), ("realtime_signal", 1),
    ("signal_compensation", 1), ("soft_lower_limit_mm", 0.001), ("edge_seek_samples", 1),
    ("edge_seek_max_dev_point", 1), ("last_edge_seek_pos_mm", 0.001), ("signal_fluctuation", 1),
]
# getReg(8, idx) property table (MainApp 0xb22e28, FUN_0080ba10): idx -> (name, scale)
PROP_FIELDS = {
    0: ("empty_move_speed", 1), 1: ("acceleration", 1), 2: ("acceleration_time", 1000),
    3: ("follow_tolerance", 1000), 4: ("drill_tolerance", 1000), 5: ("home_on_power_up", 1),
    6: ("home_fast_speed", 1), 7: ("home_fine_speed", 1), 8: ("origin_offset", 10),
    9: ("calibration_height", 10), 10: ("calibration_coarse_speed", 1), 11: ("calibration_fine_speed", 1),
    12: ("calibration_decel_signal_change", 1), 13: ("signal_variation_coefficient", 1),
    14: ("max_signal_deviation", 1), 15: ("soft_low_limit_1", 1000), 16: ("soft_low_limit_2", 1000),
    17: ("max_effective_signal", 1), 18: ("touch_sheet_alarm_signal", 1), 19: ("empty_move_touch_saturation", 1),
    20: ("touch_alarm_delay", 1), 21: ("empty_move_touch_delay", 1), 22: ("drill_touch_delay", 1),
    23: ("capacitance_abnormal_threshold", 1), 24: ("follow_error_alarm_height", 1000),
    25: ("follow_error_alarm_delay", 1), 26: ("alarm_keep_time", 1), 27: ("signal_filter_factor", 1),
    28: ("touch_auto_lift", 1), 29: ("zero_signal_alarm", 1), 30: ("dynamic_signal_correction", 1),
    31: ("signal_compensation_enable", 1), 32: ("signal_filter_factor_2", 1), 33: ("follow_kp", 1),
    34: ("follow_ki", 1), 35: ("follow_lowpass", 1), 36: ("follow_integral_saturation", 1),
    37: ("follow_decel_time", 1), 38: ("follow_sensitivity", 1), 40: ("fluctuation_alarm_threshold", 1),
}

SUB_FOLLOW = 4
SUB_CALIBRATE = 7


@dataclass
class FollowerStatus:
    """The 18 status registers (``getReg(7, i)``).  Field names for indices are documented in
    docs/re/devices.md; :attr:`raw` keeps everything."""
    raw: list[int] = field(default_factory=lambda: [0] * N_STATUS)

    def __getitem__(self, i: int) -> int:
        return self.raw[i]

    def value(self, name: str) -> float:
        """Scaled value by :data:`STATUS_FIELDS` name."""
        for i, (n, k) in enumerate(STATUS_FIELDS):
            if n == name:
                return self.raw[i] * k
        raise KeyError(name)

    @property
    def homed(self) -> bool:
        return bool(self.raw[2] & 0x80000000)

    @property
    def calibration_grade(self) -> str:
        return {0x10: "excellent", 0x11: "good", 0x12: "bad"}.get(self.raw[2] & 0xFF, "not done")

    @property
    def calibrating(self) -> bool:
        return self.raw[3] == CMD_CALIBRATE

    @property
    def flag(self) -> bool:
        """FUN_100394b0 as it really behaves: bit 15 of register 10004 (see module doc)."""
        return bool(self.raw[REG_FLAG - REG_STATUS] & 0x8000)


class OnBoardFollower:
    """Follower inside the motion card, driven through a :class:`MotionController`."""

    def __init__(self, mc):
        self.mc = mc

    def read_status(self) -> list[int]:
        return self.mc.read_regs(REG_STATUS, N_STATUS)

    def status(self) -> FollowerStatus:
        return FollowerStatus(self.read_status())

    def read_props(self) -> list[int]:
        return self.mc.read_regs(REG_PROPS, N_PROPS)

    def read_reg32(self, addr: int) -> int:
        """syncReadZFReg FUN_1004ae90, reproducing the original arithmetic
        ``(r0 << 16) + count`` (count = 2) truncated to 32 bits."""
        r = self.mc.read_regs(addr, 2)
        return ((r[0] << 16) + 2) & 0xFFFFFFFF

    def flag_10004(self) -> bool:
        """FUN_100394b0: ``syncReadZFReg(10004) & 0x80000000``."""
        return bool(self.read_reg32(REG_FLAG) & 0x80000000)

    def follow(self, enable: bool, height: float = 0) -> None:
        """FUN_10041740: ``[118, 4, on, int(height)]``; the value is the double at
        ``sys+0x4dd0`` converted with ftol (unit UNKNOWN; INFERRED to be the follow height in
        the card's units)."""
        self.mc.command([F.OP_FOLLOW, SUB_FOLLOW, 1 if enable else 0, int(height) & 0xFFFFFFFF])

    def signal_correction(self) -> None:
        """FUN_10041630 "ZF Signal correction": ``[118, 7]``."""
        self.mc.command([F.OP_FOLLOW, SUB_CALIBRATE])

    def calibrate(self) -> None:
        """Head calibration (MainApp FUN_0062cd50): ``writeReg(101, 107)``.  MainApp refuses
        when the follower is not homed (status 2 bit 31, message zf49)."""
        self.mc.write_reg(REG_MAILBOX, CMD_CALIBRATE)

    def cancel_calibration(self) -> None:
        """``writeReg(101, 101)`` (FUN_0062cd50 / FUN_0062d1c0)."""
        self.mc.write_reg(REG_MAILBOX, CMD_CANCEL)

    def servo_calibrate(self, value: int) -> None:
        """FUN_0062d240: ``writeRegs(101, [107, value * 10])`` (``value`` = app+0xbc28, UNKNOWN)."""
        self.mc.write_regs(REG_MAILBOX, [CMD_CALIBRATE, int(value) * 10])

    def props(self) -> dict[str, float]:
        raw = self.read_props()
        return {n: raw[i] / k for i, (n, k) in PROP_FIELDS.items() if i < len(raw)}

    def write_prop(self, idx: int, raw_value: int, commit: bool = True) -> None:
        """MainApp FUN_00633000: ``writeReg(11000 + 2*idx, v)`` then ``writeReg(150, 9999)``.
        (The address doubling is observed in MainApp although reads use ``11000 + idx``.)"""
        self.mc.write_reg(REG_PROPS + 2 * idx, int(raw_value))
        if commit:
            self.mc.write_reg(REG_COMMIT, COMMIT_SAVE)

    def factory_reset(self) -> None:
        """FUN_00633a10: ``writeReg(150, 5555)``."""
        self.mc.write_reg(REG_COMMIT, COMMIT_FACTORY)

    def read_calibration_curve(self) -> "CalibrationTable":
        """VM +0xcc FUN_10042cf0: n at 12000, ``2n+2`` regs from 12002 (chunks <= 200)."""
        n = self.mc.read_reg(REG_CURVE_COUNT)
        total = 2 * n + 2
        words: list[int] = []
        while len(words) < total:
            k = min(CURVE_CHUNK, total - len(words))
            words += self.mc.read_regs(REG_CURVE + len(words), k)
        signed = [w - (1 << 32) if w & 0x80000000 else w for w in words]
        pairs = list(zip(signed[0::2], signed[1::2]))
        return CalibrationTable(pairs[0][0], pairs[1:], pairs[0][1])

    def download(self, addr: int, data: bytes, chunk: int = 512) -> None:
        """fillZFDownFile FUN_10053c70: MC function 0x26 byte blocks."""
        for i in range(0, len(data), chunk):
            part = list(data[i:i + chunk])
            self.mc.transact(F.Request(F.FUNC_WRITE_BYTES_AT, addr + i, len(part), part))


# --------------------------------------------------------------------------- calibration
@dataclass
class CalibrationTable:
    """``Help/calib.csv``: text lines ``a, b`` (CRLF).  Observed shipped file: first line
    ``413023, 0`` then ``0, 406507``, ``0, 409718``, ``101, 410429`` ... ``4201, 412318``
    (64 lines).  INFERRED layout: line 0 = (raw value with the nozzle far away, 0) i.e. the
    reference/"infinite" reading; following lines = (height, raw capacitance reading) with
    the height in micrometres (0..4.2 mm, 0.2 mm steps), raw increasing towards the plate.
    """
    reference: int = 0
    points: list[tuple[int, int]] = field(default_factory=list)
    reference_b: int = 0          # second word of the header pair (0 in the shipped file)

    @classmethod
    def parse(cls, text: str) -> "CalibrationTable":
        rows = []
        for line in text.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].lstrip("-").isdigit() and parts[1].lstrip("-").isdigit():
                rows.append((int(parts[0]), int(parts[1])))
        if not rows:
            return cls()
        return cls(rows[0][0], rows[1:], rows[0][1])

    @classmethod
    def load(cls, path: str) -> "CalibrationTable":
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return cls.parse(fh.read())

    def dumps(self) -> str:
        lines = [f"{self.reference}, {self.reference_b}"] + [f"{h}, {r}" for h, r in self.points]
        return "\r\n".join(lines) + "\r\n"

    def save(self, path: str) -> None:
        with open(path, "w", encoding="ascii", newline="") as fh:
            fh.write(self.dumps())

    def height_for(self, raw: int) -> Optional[float]:
        """Interpolate height (table units) for a raw reading (monotonic part of the table)."""
        pts = sorted({r: h for h, r in self.points}.items())
        if len(pts) < 2:
            return None
        if raw <= pts[0][0]:
            (r0, h0), (r1, h1) = pts[0], pts[1]
        elif raw >= pts[-1][0]:
            (r0, h0), (r1, h1) = pts[-2], pts[-1]
        else:
            for (r0, h0), (r1, h1) in zip(pts, pts[1:]):
                if r0 <= raw <= r1:
                    break
        return h0 + (h1 - h0) * (raw - r0) / (r1 - r0) if r1 != r0 else float(h0)


# --------------------------------------------------------------------------- network ZF
class NetworkFollower:
    """Stand-alone follower at ZFIP:ZFPort with the CStdModbus codec (unused in this build).
    16-bit Modbus holding registers; TCP (INFERRED)."""

    def __init__(self, host: str = "10.1.1.169", port: int = 502, timeout_ms: int = 500):
        self.host, self.port, self.timeout = host, port, timeout_ms / 1000
        self.sock: Optional[socket.socket] = None

    def open(self) -> "NetworkFollower":
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        return self

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def _xfer(self, vec: list[int]) -> list[int]:
        self.open()
        self.sock.sendall(STD_MODBUS.encode(vec))
        rx = self.sock.recv(1024)
        rep = STD_MODBUS.decode(rx)
        if rep[0] == vec[0] | 0x80:
            raise CodecError(f"exception {rep[1]}", 500 + rep[1])
        return rep

    def read_regs(self, addr: int, count: int) -> list[int]:
        return self._xfer([FUNC_READ_HOLDING, addr, count])[2:]

    def write_regs(self, addr: int, values: Sequence[int]) -> None:
        self._xfer([FUNC_WRITE_MULTI, addr, len(values), *values])


# --------------------------------------------------------------------------- simulator
class FollowerModel:
    """Physics stub: raw capacitance rises as the nozzle approaches the plate, following a
    calibration table; collision below ``collision_um``."""

    def __init__(self, table: Optional[CalibrationTable] = None):
        self.table = table or CalibrationTable(413023, [(h, 406507 + int(5800 * (1 - 1 / (1 + h / 300.0))))
                                                         for h in range(0, 4400, 200)])
        self.height_um = 1000
        self.following = False
        self.target = 0
        self.calibrating = False
        self.calibrated = False

    def raw(self) -> int:
        pts = sorted(self.table.points)
        h = self.height_um
        for (h0, r0), (h1, r1) in zip(pts, pts[1:]):
            if h0 <= h <= h1:
                return int(r0 + (r1 - r0) * (h - h0) / max(1, h1 - h0))
        return pts[-1][1] if h > pts[-1][0] else pts[0][1]


def __getattr__(name: str):
    # make_follower_card_simulator moved to mlaser.controller.follower_sim (Wave 1): it
    # subclasses the card simulator, and devices sits below controller.  The import is dynamic
    # so this compatibility forwarder adds no devices -> controller edge (docs/dev/ARCHITECTURE.md).
    if name == "make_follower_card_simulator":
        import importlib

        from mlaser._compat import warn_moved
        warn_moved(f"{__name__}.{name}", f"mlaser.controller.follower_sim.{name}")
        return importlib.import_module("mlaser.controller.follower_sim").make_follower_card_simulator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
