"""Card start-up: the parameter download, the fast-status map and the servo-info read.

Everything here follows NCModule.dll of Mlaser v0.0.1.51_SP1 (addresses are NCModule virtual
addresses) and was checked against the frames the ORIGINAL MainApp sent at start-up in the
parity harness (``parity/out/app_startup/capture/card_frames.jsonl``, docs/re/parity.md §4):

1. VM event 0 (FUN_100571f0 case 0): open the HAL, checkMCStatus (FUN_1004ed60: read
   RO[0..1]) and, when the card answered, ``101 := [9999, 5, 0, 0]``.
2. The status poll FUN_1004eab0 (VM +0x144) runs from then on (see ``FAST_MAP``).
3. Parameter download FUN_1002e500 (CNCModule +0x194, MainApp FUN_004ae4b0): see
   :func:`download_requests`.  It ends with ``100 := 9999`` and VM +0x248 (FUN_10054e70:
   clear FIFO ``103 := 1``, then read register 1032 = FIFO capacity, 65000 when unreadable).
4. Fast-status map (FUN_1003ee50, VM +0x20c): ``60000 := 60 words``, see :data:`FAST_MAP`.
5. Servo info (FUN_10054a00, VM +0x210): read 63000 x 128.

**Intentionally omitted (licence gate).**  Between steps 2 and 3, and again after step 5,
the original writes four random words to register 59500 and reads 59500 back
(NCModule FUN_10039390 / FUN_1003d090).  That is a hardware challenge used to decide
whether the card is authorised ("非授权硬件").  It is a licensing check: it is not
implemented, not answered and not analysed here (BRIEF ground rules; docs/re/protocol.md §8).

Only the standard (non-bus) card without on-board follower, rotary axis, AF focus axis or
precision compensation is covered exactly; the branches for those options are noted where
they are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..protocol import frames as F

# --------------------------------------------------------------------------- fast status
FAST_MAP_REG = 60000
FAST_LEN_MAX = 144          # FUN_10052c30 unpacks up to 0x90 words


def default_fast_map() -> list[int]:
    """The 120 register numbers FUN_1003ee50 lists (O).

    Loop 1: 2000, 2002, ..., 2098 = the 50 axis-RO words (10 per axis) in the card's 16-bit
    register addressing.  Loop 2: 50200 + 40a + 2k for a < 5, k < 14 = the 14 used words of
    each axis parameter block.  FUN_10052c30 stores fast words 0..49 where the 2000 x 50 read
    goes (VM+0x120) and 50..119 where the 50200 x 100 read keeps its 14 words per axis
    (VM+0x1ec), so one fast read replaces those two reads.
    """
    regs = [r for top in range(2020, 2120, 20) for r in range(top - 20, top, 2)]
    regs += [r for top in range(50228, 50428, 40) for r in range(top - 28, top, 2)]
    return regs


def pack_fast_map(regs: Sequence[int]) -> list[int]:
    """Two u16 register numbers per word, the first in the low half (FUN_1003ee50).  An odd
    trailing register is sent alone."""
    out = []
    for i in range(0, len(regs), 2):
        w = regs[i] & 0xFFFF
        if i + 1 < len(regs):
            w |= (regs[i + 1] & 0xFFFF) << 16
        out.append(w)
    return out


def unpack_fast_map(words: Sequence[int]) -> list[int]:
    """Inverse of :func:`pack_fast_map` (zero high halves are dropped only at the end)."""
    regs = []
    for w in words:
        regs += [w & 0xFFFF, (w >> 16) & 0xFFFF]
    while regs and regs[-1] == 0:
        regs.pop()
    return regs


FAST_MAP = default_fast_map()
assert len(FAST_MAP) == 120

FIFO_CAPACITY_REG = 1032          # FUN_10054e70 -> cfg +0x47f0
FIFO_CAPACITY_DEFAULT = 65000     # used when the read fails (FUN_10054e70)
SERVO_INFO_COUNT = 0x80           # FUN_10054a00


# --------------------------------------------------------------------------- parameters
def _b(v) -> int:
    return 1 if v else 0


@dataclass
class AxisDownload:
    """One ``MachineAxisConfig_n`` block (cfg +0xbc58 + 0xa0*n) as the download reads it."""
    negative_type: int = 0        # +0xbc58 MAC.NegativeType
    forward_type: int = 0         # +0xbc5c MAC.ForwardType
    zero_type: int = 0            # +0xbc60 MAC.ZoreType
    word_bc64: int = 0            # +0xbc64 (no file parameter, U)
    sample_type: int = 1          # +0xbc68 MAC.SampleType
    go_origin_dir: int = 0        # +0xbc6c MAC.GoOriginalDirection
    second_home: bool = False     # +0xbc70 MAC.SecondGoHome
    z_phase: bool = False         # +0xbc71 MAC.EnableZphaseSignal
    reverse: bool = False         # +0xbc72 MAC.AxisReverse
    encoder_reverse: bool = False  # +0xbc73 MAC.EncoderReverse
    rotating: bool = False        # +0xbc74 MAC.IsRotatingShaft
    flag_bc75: bool = False       # +0xbc75 (set by FUN_10033xxx follower code, U)
    flag_bc76: bool = True        # +0xbc76 := (cfg +0x4843 == 0), set by the download itself
    flag_bc77: bool = False       # +0xbc77 (U)
    soft_max_len: float = 1000.0  # +0xbc78 MAC.SoftLimitMaxLen, mm
    acceleration: float = 20000.0  # +0xbc80 MAC.Acceleration
    word4: float = 200000.0       # +0xbc88: no file parameter; restored from card axis word 4
                                  # (FUN_10039f20 case 4).  200000 = value of the shipped install (O)
    fast_speed: float = 30.0      # +0xbc90 MAC.FastSpeed, mm/s
    second_speed: float = 20.0    # +0xbc98 MAC.SecondSpeed, mm/s
    return_length: float = 10.0   # +0xbca0 MAC.ReturnLength, mm
    lead: float = 100.0           # +0xbca8 MAC.SpeedRatio, mm/rev
    ppr: int = 10000              # +0xbcb0 MAC.WritePluse
    negative_input: int = 0       # +0xbcb8 MAC.NegativeLimitInput
    forward_input: int = 0        # +0xbcbc MAC.ForwardLimitInput
    origin_input: int = 0         # +0xbcc0 MAC.OriginalInput
    brake_output: int = 0         # +0xbcc4 MAC.BrakeOutput

    def limits_word(self) -> int:
        """FUN_1002b190: switch types and flags packed into axis word 0."""
        v = ((((self.go_origin_dir * 2 | self.sample_type) * 2 | self.word_bc64) * 2
              | self.zero_type) * 2 | self.forward_type) * 2
        v |= (0x400 * _b(self.rotating) | 0x200 * _b(self.encoder_reverse)
              | 0x100 * _b(self.reverse) | 0x80 * _b(self.z_phase) | 0x40 * _b(self.second_home)
              | 0x40000 * _b(self.flag_bc77) | 0x20000 * _b(self.flag_bc76)
              | 0x10000 * _b(self.flag_bc75))
        return (v | self.negative_type) & 0xFFFFFFFF

    def block(self, ppm: int) -> list[int]:
        """The 14 words written to 50200 + 40*axis (FUN_1002e500 axis loop, O from the asm at
        0x1002e7ae..0x1002f7a0).  Standard axis only: the AF focus-axis branch (AF.AFType 5/6,
        return length from +0xbcf0), the rotary branch (+0x4e68, lead from the tube diameter)
        and the bus-card second soft limit (+0x49d6) are not reproduced."""
        f = F.ftol
        if self.go_origin_dir == 0:
            s1, s2 = f(ppm * -0.5), f(self.soft_max_len * ppm)
        else:
            s1, s2 = f(-(self.soft_max_len * ppm)), f(ppm * 0.5)
        ports = ((self.origin_input << 8) | self.forward_input) << 8 | self.negative_input
        words = [self.limits_word(), s1, s2, f(self.acceleration), f(self.word4),
                 f(ppm * self.fast_speed), f(ppm * self.second_speed), f(ppm * self.return_length),
                 0, f(ppm * self.lead), self.ppr, self.ppr, ports, self.brake_output]
        return [w & 0xFFFFFFFF for w in words]


@dataclass
class CardParameters:
    """The configuration values FUN_1002e500 sends (cfg offsets of the MainApp config struct;
    keys are the HardPara.xml names whose ParaModule struct offset matches)."""
    servo_alarm_type: int = 0     # +0x3b7c DI.ServoAlarmType
    servo_alarm_port: int = 0     # +0x3b78 DI.ServoAlarm
    estop_port: int = 0           # +0x35b0 DI.EStop
    dual_drive: bool = False      # +0xbc14 MAC.DoubleDevice
    dual_slave_axis: int = 2      # +0xbc1c (axis mapping, no file parameter, I default)
    y_axis: int = 1               # +0xbbd8
    dual_tolerance: int = 2000    # +0xbc20 MAC.AllowableError
    dual_alarm_time: int = 1000   # +0xbc24 MAC.AlarmTime
    brake_time: int = 500         # +0xbc50 MAC.CloseBreakTime
    axes: list = field(default_factory=lambda: [AxisDownload() for _ in range(F.N_AXES)])
    ppm: int = 1000               # +0xbc28 position counts per mm (card sys word 17; the
                                  # shipped install downloads with 1000, O)
    limit_dec_factor: int = 1     # +0x4334 SP.LimitDeccFactor
    max_acc: float = 20000.0      # +0x4a40 FCP.MaxAcc
    da_keep_output: bool = True   # +0x49c0 MP.LaserDAKeepOutput
    da_port: int = 0              # +0x49a8 LGP.LaserDAPort
    da_type: int = 0              # +0x49ac LGP.LaserDAType
    da_value: int = 0             # +0x48c0 (int, U)
    da_min: int = 50              # VM+0x1190 DAMinVal
    card_type: int = 0            # +0x4728: 0 standard card, 1/2 bus cards
    wait_signal_port: int = 0     # +0x224 DO.WaitSignal
    remote_start_port: int = 0    # +0x2c4 LGP.DORemoteStart
    output_904: int = 0           # +0x904 output port switched on/off by the download (U)
    rotary: bool = False          # +0x4e68 rotary (tube) option
    ext_io: bool = False          # +0x4e00 extended IO board (outputs 11..26)
    zf_type: int = 0              # +0x4994 ZF.ZFType
    zf_axis: int = 3              # +0x4ab8 ZF axis: no file parameter; 3 = the value the
                                  # original skips in NC +0x2a4 (capture, manual coord select)
    zf_output_8dc: int = 0        # +0x8dc
    di_smooth: list = field(default_factory=lambda: [0] * 12)   # +0x3ec4.. DI.DIn SmoothTime
    pwm_sync: bool = False        # +0x49c1 MP.PWMOutputSync
    do_laser: int = 0             # +0xbc LGP.DOLaser
    co2_control_type: int = 2     # +0x49c8 LGP.CO2LaserControlType
    co2_pwm_sync: bool = True     # +0x49d5 MP.CO2PWMOutputSync
    co2_do_laser: int = 0         # +0x88c LGP.CO2DOLaser
    af_type: int = 0              # +0x4ac8 AF.AFType

    @classmethod
    def from_values(cls, values: dict, ppm: int = 1000, word4: float = 200000.0) -> "CardParameters":
        """As :meth:`from_machine_params` from a ``{key: value}`` dict (``Machine.params``)."""
        class _Store:
            def get(self, d):
                return values[d.key]

        class _MP:
            hard = manu = _Store()
        return cls.from_machine_params(_MP(), ppm, word4)

    @classmethod
    def from_machine_params(cls, mp, ppm: int = 1000, word4: float = 200000.0) -> "CardParameters":
        """Build from a :class:`mlaser.formats.params.MachineParams` by matching the ParaModule
        struct offsets of the parameter definitions (``ParamDef.struct_offset``)."""
        from ..formats.params import param_defs
        by_off: dict[int, object] = {}
        for d in param_defs():
            if d.file in ("HardPara.xml", "ManuPara.xml") and "." in d.key:
                by_off.setdefault(d.struct_offset, d)

        def get(off: int, default):
            d = by_off.get(off)
            if d is None:
                return default
            st = mp.hard if d.file == "HardPara.xml" else mp.manu
            try:
                v = st.get(d)
            except (KeyError, ValueError, TypeError):
                return default
            return default if v is None or v == "" else v

        p = cls(ppm=int(ppm))
        p.servo_alarm_type = int(get(0x3b7c, 0))
        p.servo_alarm_port = int(get(0x3b78, 0))
        p.estop_port = int(get(0x35b0, 0))
        p.dual_drive = bool(get(0xbc14, False))
        p.dual_tolerance = int(get(0xbc20, 2000))
        p.dual_alarm_time = int(get(0xbc24, 1000))
        p.brake_time = int(get(0xbc50, 500))
        p.limit_dec_factor = int(get(0x4334, 1))
        p.max_acc = float(get(0x4a40, 20000.0))
        p.da_keep_output = bool(get(0x49c0, True))
        p.da_port = int(get(0x49a8, 0))
        p.da_type = int(get(0x49ac, 0))
        p.wait_signal_port = int(get(0x224, 0))
        p.remote_start_port = int(get(0x2c4, 0))
        p.zf_type = int(get(0x4994, 0))
        p.di_smooth = [int(get(0x3ec4 + 4 * i, 0)) for i in range(12)]
        p.pwm_sync = bool(get(0x49c1, False))
        p.do_laser = int(get(0xbc, 0))
        p.co2_control_type = int(get(0x49c8, 2))
        p.co2_pwm_sync = bool(get(0x49d5, True))
        p.co2_do_laser = int(get(0x88c, 0))
        p.af_type = int(get(0x4ac8, 0))
        for a in range(F.N_AXES):
            o = 0xa0 * a
            ax = AxisDownload(word4=word4)
            ax.negative_type = int(get(0xbc58 + o, 0))
            ax.forward_type = int(get(0xbc5c + o, 0))
            ax.zero_type = int(get(0xbc60 + o, 0))
            ax.sample_type = int(get(0xbc68 + o, 1))
            ax.go_origin_dir = int(get(0xbc6c + o, 0))
            ax.second_home = bool(get(0xbc70 + o, False))
            ax.z_phase = bool(get(0xbc71 + o, False))
            ax.reverse = bool(get(0xbc72 + o, False))
            ax.encoder_reverse = bool(get(0xbc73 + o, False))
            ax.rotating = bool(get(0xbc74 + o, False))
            ax.soft_max_len = float(get(0xbc78 + o, 1000.0))
            ax.acceleration = float(get(0xbc80 + o, 20000.0))
            ax.fast_speed = float(get(0xbc90 + o, 30.0))
            ax.second_speed = float(get(0xbc98 + o, 20.0))
            ax.return_length = float(get(0xbca0 + o, 10.0))
            ax.lead = float(get(0xbca8 + o, 100.0))
            ax.ppr = int(get(0xbcb0 + o, 10000))
            ax.negative_input = int(get(0xbcb8 + o, 0))
            ax.forward_input = int(get(0xbcbc + o, 0))
            ax.origin_input = int(get(0xbcc0 + o, 0))
            ax.brake_output = int(get(0xbcc4 + o, 0))
            p.axes[a] = ax
        return p


def _output_bit(port: int, on: bool, ext_io: bool) -> Optional[list[int]]:
    """CNCModule +0x13c = FUN_10030620: one output; ports < 1 send nothing."""
    if 1 <= port <= 10:
        return [9999, F.SYS_OUTPUTS, 1 << (port - 1), (int(on) & 0xFF) << (port - 1)]
    if 11 <= port <= 26 and ext_io:
        return [9999, F.SYS_OUTPUTS_EXT, 1 << (port - 11), (int(on) & 0xFF) << (port - 11)]
    return None


def _da_value(percent: float, da_type: int, da_min: int) -> int:
    """setDA FUN_1004d640: ftol(percent * k * 100), k = 1 / 0.5 / 0.4 for DA types 0 / 1 / 2
    (other types send 0); 0 < v < DAMinVal is raised to DAMinVal."""
    k = {0: 1.0, 1: 0.5, 2: 0.4}.get(da_type)
    if k is None:
        return 0
    v = F.ftol(percent * k * 100.0)
    return da_min if 0 < v < da_min else v


def download_requests(p: CardParameters) -> list[tuple[int, list[int]]]:
    """``(register, words)`` writes of the parameter download FUN_1002e500, in order, for the
    standard card (O, asm 0x1002e500..0x1002fd76; values checked against the capture):

    5001 := [9999, 9, 0xffff, 0]; 50026 := servo-alarm/e-stop word; 50028 := dual drive;
    50036 := brake time; 101 := [9999, 5, 0, 0]; 50200 + 40a := 14 axis words (a < 5);
    50022 := [ftol(f*MaxAcc), ftol(f*MaxAcc*10)]; DA reset; outputs reset; 50012 := ZF type;
    50040..50050 := DI smoothing pairs; follower [118, 5, x] (only with a follower);
    50106/50108 := PWM sync output; 50008 := AF.AFType == 1; 100 := 9999.

    Not reproduced: precision compensation (MP.EnableVerCorrect -> CNCModule +0x2e8, sends
    nothing with the default 0) and the on-board AF branch behind HAL +0x30.
    """
    f = F.ftol
    out: list[tuple[int, list[int]]] = []
    w = lambda reg, *vals: out.append((reg, [v & 0xFFFFFFFF for v in vals]))
    mb = lambda *vals: w(F.REG_MAILBOX, *vals)
    w(5001, 9999, F.SYS_9, 0xFFFF, 0)
    w(50026, ((p.servo_alarm_type << 21 | p.servo_alarm_port | 0x100000) << 8) | p.estop_port)
    if not p.dual_drive:
        w(50028, 0)
    else:
        w(50028, p.dual_slave_axis << 8 | p.y_axis, p.dual_tolerance, p.dual_alarm_time)
    w(50036, p.brake_time)
    mb(9999, F.SYS_INIT, 0, 0)                       # CNCModule +0x1e4 alarm reset
    for a, ax in enumerate(p.axes[:F.N_AXES]):
        w(F.REG_AXIS_RW + 40 * a, *ax.block(p.ppm))
    acc = p.limit_dec_factor * p.max_acc
    w(50022, f(acc), f(acc * 10.0))
    # DA (VM +0xac = setDA(port, type, percent))
    if not p.da_keep_output or p.da_port == 0 or p.card_type != 0:
        mb(9999, F.SYS_DA, 0, _da_value(0.0, 0, p.da_min))
        mb(9999, F.SYS_DA, 1, _da_value(0.0, 0, p.da_min))
    elif p.da_port in (1, 2):
        mb(9999, F.SYS_DA, p.da_port - 1, _da_value(float(p.da_value), p.da_type, p.da_min))
    # outputs: CNCModule +0x140 = FUN_100304a0(keep) -> all off except standby / remote key
    keep = 0
    if p.wait_signal_port > 0:
        keep = 1 << (p.wait_signal_port - 1)
    if p.remote_start_port > 0:
        keep |= 1 << (p.remote_start_port - 1)
    mb(9999, F.SYS_OUTPUTS, 0xFFFF, keep & 0xFFFF)
    if p.ext_io:
        mb(9999, F.SYS_OUTPUTS_EXT, 0xFFFF, (keep >> 16) & 0xFFFF)
    cmd = _output_bit(p.output_904, p.rotary, p.ext_io)
    if cmd:
        mb(*cmd)
    w(50012, 0 if p.zf_type == 0 else p.zf_axis + 1)
    for i in range(6):
        lo, hi = p.di_smooth[2 * i], p.di_smooth[2 * i + 1]
        w(50040 + 2 * i, hi * 0x10000 + lo)
    bus = p.card_type == 1
    if p.zf_type != 0:                               # VM +0x70 (FUN_100390c0) sends only with a follower
        mb(118, 5, 1 if bus else 0)
    cmd = _output_bit(p.zf_output_8dc, bus, p.ext_io)   # CNCModule +0x13c(+0x8dc, bus)
    if cmd:
        mb(*cmd)
    if p.card_type in (1, 2):
        if p.co2_control_type == 1:
            w(50106, p.co2_do_laser if p.co2_pwm_sync else 0)
        elif p.co2_control_type == 2:
            w(50108, p.co2_do_laser if p.co2_pwm_sync else 0)
        else:
            w(50106, 0)
            w(50108, 0)
    else:
        w(50106, p.do_laser if p.pwm_sync else 0)
    w(50008, 1 if p.af_type == 1 else 0)
    w(F.REG_CONTROL, F.CONTROL_APPLY)
    return out
