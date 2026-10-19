"""Register tables shown by the controller monitor (``mf119``) and how to read them.

The monitor's row labels are global string arrays built by static initialisers in MainApp
(O, addresses below); each array index is the register index of one card block:

==================  ===========================  ==========================================
block               registers                    label array (initialiser)
==================  ===========================  ==========================================
status RO           1000 + i, i < 36             RORegName_1..32, A241105_0/1, A250516_5/6
                                                 (FUN_006f0870)
axis RO             2000 + 10a + k, k < 10       AxisRORegName_1,2,3,4,6,12,7,8,9,10
                                                 (FUN_006f0b60)
system RW           50000 + 2i, i < 26           SystemRWRegName_1..10,28, RWRegName_4/5,
                                                 SystemRWRegName_29..32,34,35, A241009_2,
                                                 SystemRWRegName_36..41 (FUN_006f1020)
axis RW             50200 + 40a + 2k, k < 14     AxisRWRegName_1,3,2,4..14 (FUN_006f0d80)
device run info     1050..1052                   power-on / communication / laser-on time
                                                 (VM +0x1a4 FUN_1003d3a0, see devicereport)
==================  ===========================  ==========================================

The order of the RO labels matches the protocol facts already established (RO[4] inputs =
"Input status", RO[5] outputs, RO[6]/[7] alarm words 1/2, RO[15]/[16] FIFO frame id / space,
RO[19] processing status, RO[21]/[22] extended IO, RO[23]/[24] profile status / contour index),
which confirms the index = position mapping (O).  The legacy ``RegName1..143`` table belongs
to the old MCC3721 card and is not used by this build's monitor (I).

The monitor also shows the IO page: 58 DO labels (DO parameter names, the 20 custom DOs,
``ss22`` "Oil" for MP.OilDOPort, ``ss23`` "Dedust(%d, %d)" for the section-DO matrix
MP.SecitonDORow x MP.SecitonDOColumn at cfg+0xc9c) (MainApp FUN_00624a00, O), DI 1..28 and
the per-axis limit indicators ``ss19`` NEG / ``ss20`` ORG / ``ss21`` POS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mlaser.protocol import frames as F

RO_KEYS = [f"RORegName_{i}" for i in range(1, 33)] + ["A241105_0", "A241105_1", "A250516_5", "A250516_6"]
AXIS_RO_KEYS = ["AxisRORegName_1", "AxisRORegName_2", "AxisRORegName_3", "AxisRORegName_4",
                "AxisRORegName_6", "AxisRORegName_12", "AxisRORegName_7", "AxisRORegName_8",
                "AxisRORegName_9", "AxisRORegName_10"]
SYS_RW_KEYS = (["SystemRWRegName_%d" % i for i in range(1, 11)] + ["SystemRWRegName_28", "RWRegName_4",
               "RWRegName_5"] + ["SystemRWRegName_%d" % i for i in (29, 30, 31, 32, 34, 35)]
               + ["A241009_2"] + ["SystemRWRegName_%d" % i for i in range(36, 42)])
AXIS_RW_KEYS = ["AxisRWRegName_1", "AxisRWRegName_3", "AxisRWRegName_2"] + [
    "AxisRWRegName_%d" % i for i in range(4, 15)]
AXIS_NAMES = ["X", "Y1", "Y2", "W", "Z"]          # RegName9..16 naming for card axes 0..3 (I for 4)

# English fallbacks (lang.txt column 2) so the tables are readable without a translator
RO_EN = ["Program Identification", "Program Version", "date", "time", "Input Status", "Output Status",
         "Alarm status_1", "Alarm status_2", "Operating status", "AD sampling value", "DA1 sampling value",
         "DA2 sampling value", "DA3 sampling value", "PWM frequency", "PWM Duty Cycle",
         "FIFO frame identification", "FIFO space margin", "FIFO interpolation data configuration",
         "Slave device information", "Processing status", "Processing position", "Expand input status",
         "Expanded output status", "Profile Status", "Contour Index", "Power-on time",
         "Communication time", "Light emission time", "Dual drive feedback deviation",
         "Sampling encoder configuration", "Sampling encoder data length", "Parameter status", "Wifi",
         "Run Time", "PWM(5V) frequency", "PWM(5V) Duty Cycle"]
AXIS_RO_EN = ["Status", "speed", "Pulse position", "Encoder position", "Stop pulse",
              "Encoder value when stopped", "Total mileage", "Calculate the cumulative number of pulses",
              "Calculate the cumulative number of encoders", "Accumulated pulse number"]
SYS_RW_EN = ["IP address", "Subnet Mask", "Default Gateway", "Baud rate", "spare", "Bus cycle",
             "Adjust the axis to enable", "DA1 calibration value", "DA2 calibration value",
             "DA3 calibration value", "Communication timeout", "Safety deceleration", "Safe speed reduction",
             "Special input configuration", "Dual drive configuration", "Dual drive tolerance",
             "Dual drive warning time", "Coefficient of Precision", "Brake closing delay", "A241009_2",
             "Input filter time IN1 IN2", "Input filter time IN3 IN4", "Input filter time IN5 IN6",
             "Input filter time IN7 IN8", "Input filter time IN9 IN10", "Input filter time IN11 IN12"]
AXIS_RW_EN = ["Configuration parameters", "Negative soft limit value", "Positive soft limit value",
              "Acceleration", "Jerk", "Coarse return speed", "Fine return speed", "Origin offset", "spare",
              "Lead", "Equivalent command pulse number", "Equivalent encoder feedback pulse number",
              "Input Configuration", "Output Configuration"]

REG_RUN_INFO = F.REG_LATCH          # 1050: 3 words read by VM +0x1a4 (FUN_1003d3a0)
AX_MILEAGE = 6                      # axis RO k6 "Total mileage" (FUN_1003d3a0 reads axis*10+6)


def label(tr, key: str, fallback: str) -> str:
    if tr is not None:
        try:
            if tr.has(key):
                return tr.tr(key)
        except Exception:  # noqa: BLE001
            pass
    return fallback


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


@dataclass
class ControllerSnapshot:
    """One refresh of the controller monitor."""
    ro: list[int] = field(default_factory=lambda: [0] * F.N_STATUS_RO)
    axis_ro: list[list[int]] = field(default_factory=lambda: [[0] * 10 for _ in range(F.N_AXES)])
    sys_rw: list[int] = field(default_factory=list)
    axis_rw: list[list[int]] = field(default_factory=list)
    run_info: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def inputs(self) -> list[bool]:
        """DI 1..28 (RO[4] bits 0..11, RO[21] bits 0..15)."""
        lo, hi = self.ro[F.RO_INPUTS], self.ro[F.RO_INPUTS_EXT]
        return [bool(lo >> i & 1) for i in range(12)] + [bool(hi >> i & 1) for i in range(16)]

    def outputs(self) -> list[bool]:
        """DO 1..26 (RO[5] bits 0..9, RO[22] bits 0..15)."""
        lo, hi = self.ro[F.RO_OUTPUTS], self.ro[F.RO_OUTPUTS_EXT]
        return [bool(lo >> i & 1) for i in range(10)] + [bool(hi >> i & 1) for i in range(16)]

    def axis_limits(self, a: int) -> tuple[bool, Optional[bool], bool]:
        """(NEG ss19, ORG ss20, POS ss21) for card axis ``a`` from the axis status word
        (alarm bits of FUN_00598280: 0 hard +limit, 1 hard -limit, O).  The origin-switch
        source is U (None)."""
        st = self.axis_ro[a][0] if a < len(self.axis_ro) else 0
        return bool(st & 2), None, bool(st & 1)


def read_snapshot(mc, params: bool = True) -> ControllerSnapshot:
    """Read every block of the monitor through :class:`MotionController` (read-only)."""
    snap = ControllerSnapshot()
    try:
        st = mc.read_status()
        snap.ro = list(st.ro)
        snap.axis_ro = [list(a.raw) for a in st.axes]
    except Exception as e:  # noqa: BLE001 - shown in the monitor
        snap.errors.append(f"status: {e}")
    try:
        snap.run_info = list(mc.read_regs(REG_RUN_INFO, 3))
    except Exception as e:  # noqa: BLE001
        snap.errors.append(f"1050: {e}")
    if params:
        try:
            snap.sys_rw = list(mc.read_system_params())
        except Exception as e:  # noqa: BLE001
            snap.errors.append(f"system RW: {e}")
        try:
            snap.axis_rw = [list(a) for a in mc.read_axis_params()]
        except Exception as e:  # noqa: BLE001
            snap.errors.append(f"axis RW: {e}")
    return snap


def table_rows(snap: ControllerSnapshot, tr=None) -> dict[str, list[tuple[str, str, str]]]:
    """``{page: [(address, label, value)]}`` for the monitor pages."""
    pages: dict[str, list[tuple[str, str, str]]] = {}
    pages["RO"] = [(str(F.REG_STATUS_RO + i), label(tr, RO_KEYS[i], RO_EN[i]), _fmt(i, v))
                   for i, v in enumerate(snap.ro[:len(RO_KEYS)])]
    rows = []
    for a, rec in enumerate(snap.axis_ro):
        for k, v in enumerate(rec[:10]):
            rows.append((str(F.REG_AXIS_RO + 10 * a + k), f"{AXIS_NAMES[a]}: " + label(tr, AXIS_RO_KEYS[k], AXIS_RO_EN[k]),
                         f"0x{v & 0xFFFFFFFF:08x}" if k == 0 else str(s32(v))))
    pages["AxisRO"] = rows
    pages["SystemRW"] = [(str(F.REG_SYS_RW + 2 * i), label(tr, SYS_RW_KEYS[i], SYS_RW_EN[i]), str(s32(v)))
                         for i, v in enumerate(snap.sys_rw[:len(SYS_RW_KEYS)])]
    rows = []
    for a, rec in enumerate(snap.axis_rw):
        for k, v in enumerate(rec[:14]):
            rows.append((str(F.REG_AXIS_RW + 40 * a + 2 * k),
                         f"{AXIS_NAMES[a]}: " + label(tr, AXIS_RW_KEYS[k], AXIS_RW_EN[k]), str(s32(v))))
    pages["AxisRW"] = rows
    return pages


def _fmt(i: int, v: int) -> str:
    if i in (F.RO_CONFIG, F.RO_INPUTS, F.RO_OUTPUTS, 6, 7, F.RO_INPUTS_EXT, F.RO_OUTPUTS_EXT):
        return f"0x{v & 0xFFFFFFFF:08x}"
    return str(s32(v))


# ---------------------------------------------------------------- follower / AF / EC monitors
ZF_RO_KEYS = [f"ZFReadOnly{i:02d}" for i in range(1, 19)]     # getReg(7, i) labels (docs/re/devices.md)
AF_RO_KEYS = {0: "af0", 1: "af1", 2: "af2", 3: "af3", 4: "af4", 5: "af5", 6: "af6", 7: "af7", 8: "af8",
              9: "af17", 10: "af18", 11: "af30_1", 13: "af30_2"}
EC_RO_KEYS = {0: "ec0", 1: "ec1", 2: "ec2", 3: "ec3", 4: "ec4", 5: "ec5", 6: "ec6", 7: "ec7", 8: "ec8",
              9: "ec9", 10: "ec10"}


def device_rows(kind: str, raw: list[int], tr=None) -> list[tuple[str, str, str]]:
    """Rows (index, label, scaled value) for the follower / AF / EC status words, using the
    field tables of :mod:`mlaser.devices` (scales from MainApp, docs/re/devices.md)."""
    rows = []
    if kind == "zf":
        from mlaser.devices.follower import STATUS_FIELDS
        for i, v in enumerate(raw):
            name, k = STATUS_FIELDS[i] if i < len(STATUS_FIELDS) else (f"word{i}", 1)
            key = ZF_RO_KEYS[i] if i < len(ZF_RO_KEYS) else ""
            rows.append((str(i), label(tr, key, name), _scaled(s32(v), k)))
    elif kind == "af":
        from mlaser.devices.autofocus import STATUS_FIELDS
        for i, v in enumerate(raw):
            name, k = STATUS_FIELDS.get(i, (f"word{i}", 1))
            rows.append((str(i), label(tr, AF_RO_KEYS.get(i, ""), name), _scaled(s32(v), k)))
    else:
        from mlaser.devices.ethercat import STATUS_FIELDS
        for i, v in enumerate(raw):
            name, k = STATUS_FIELDS.get(i, (f"word{i}", 1))
            rows.append((str(i), label(tr, EC_RO_KEYS.get(i, ""), name), _scaled(s32(v), k)))
    return rows


def _scaled(v: int, k: float) -> str:
    return str(v) if k == 1 else f"{v * k:.3f}"


def read_device(kind: str, mc=None, client=None) -> tuple[list[int], Optional[str]]:
    """Status words of the follower (card regs 10000..), the AF head (card regs 13000.. or a
    network :class:`AutoFocus`) or the extension card (:class:`EtherCATBridge`)."""
    try:
        if kind == "zf":
            from mlaser.devices.follower import OnBoardFollower
            return OnBoardFollower(mc).read_status(), None
        if kind == "af":
            if client is not None:
                return client.read_status(), None
            from mlaser.devices.autofocus import CardAutoFocus
            return CardAutoFocus(mc).read_status(), None
        if client is not None:
            return client.read_status(), None
        return [], "extension card not connected"
    except Exception as e:  # noqa: BLE001
        return [], str(e)
