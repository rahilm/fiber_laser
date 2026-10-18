"""Process items: piercing, gas, height follower and laser parameter changes on the MCC100 FIFO.

The original builds these in two layers, and this module reproduces both:

1. **MainApp** (``Mlaser.exe``) turns a cut element into 28-byte *ManuItems* (class at +0x16,
   flags at +0x18).  Class 1 is a "system command" whose sub-op sits at +0 (1 laser, 3 DA,
   4 digital output, 5 delay, 6 crash protect, 8 laser sync); classes 4..0x13 are follower steps
   that NCModule expands with the layer parameters.  Sequences (all **O**, MainApp addresses):

   * gas select ``FUN_004645d0``, gas-on delay ``FUN_004649c0``, gas off ``FUN_004648d0``
   * laser on ``FUN_00464ab0`` (LGP.DOLaser output + class 1 sub 1 with freq/duty), laser off
     ``FUN_00464c40``, delay ``FUN_00464d60``
   * pierce ``FUN_00464e10`` (1..5 stages from GP.ManuType, or the smooth pierce)
   * start cut ``FUN_00465460`` (gas, follow item, gas delay, laser on, GP.LaserOnDelay)
   * end cut ``FUN_004657e0`` (delays, laser off, gas off unless kept, lift / short-hop no lift)
   * laser sync ``FUN_004590a0`` around each cut block, pre-jump focus item (class 0x12) before
     a rapid that leads to a cut (job loop ``FUN_00459100`` at 0x0045b331)

2. **NCModule** ``FUN_10043c20`` encodes ManuItems into FIFO items using the machine and layer
   parameters (helpers FUN_1003ec20 wait, FUN_1003f3e0 crash protect, FUN_1003f470 focus,
   FUN_1003f830 focus wait, FUN_1003f990 peak-current DA).  :class:`ManuEncoder` ports the
   process classes; motion samples (class 0) stay in :mod:`mlaser.nc.compiler`.

Parameter offsets: the NC config object and MainApp's parameter block share one layout, so
the struct offsets in ``mlaser/formats/data/mlaser_params.json`` name the fields used here
(layer block = +0x4ed8 + 0x480 * layer; pierce stage s = layer + 0x158 + 0x58 * s).

Not reproduced (documented in docs/protocol.md "Process items"): the frog-jump lift (op 106,
needs MainApp's jump profile), clean-residue
spirals, autofocus types other than 0/5/6, the CO2/bus-card variants, gas pressure maps.
Licensing/dongle code is intentionally omitted.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional, Sequence

from ..protocol import frames as F

# --------------------------------------------------------------------------- ManuItem
# classes (ManuItem +0x16)
CLS_MOTION = 0
CLS_SYS = 1
CLS_DWELL_3002 = 3
CLS_PIERCE_START = 4
CLS_PIERCE_END = 5
CLS_WAIT_ZF = 6
CLS_FOLLOW = 7
CLS_LIFT = 8
CLS_FINAL_LIFT = 9
CLS_FIX_HEIGHT = 0xA
CLS_PEAK = 0xB
CLS_MARKER = 0xE
CLS_ADV_FIX_HEIGHT = 0x11
CLS_PRE_JUMP = 0x12
CLS_AFTER_GRADUAL = 0x13

# class 1 sub-ops (ManuItem +0)
SUB_LASER = 1
SUB_DA = 3
SUB_DO = 4
SUB_DELAY = 5
SUB_CRASH = 6
SUB_LASER_SYNC = 8

STAGE_SMOOTH = 0xE        # stage nibble used by the smooth pierce (flags | 0xe0)
STAGE_CLEAN = 0xF         # clean-residue spiral (flags | 0xf0), not reproduced

# GP.ManuType -> number of pierce stages (FUN_00464e10 switch)
PIERCE_STAGES = {2: 1, 3: 2, 4: 3, 6: 4, 7: 5}
MANU_DIRECT, MANU_FIX_HEIGHT, MANU_ADV_FIX_HEIGHT = 0, 1, 5


@dataclass
class ManuItem:
    """MainApp's 28-byte ManuItem (layout confirmed by FUN_100352b0's ``_tempManuItem`` dump).

    ``a`` (+0) is dX or the class-1 sub-op, ``b`` (+4) dY or the port/channel, ``value`` (+0x18)
    the flags (low nibble layer, bits 4..7 pierce stage, bits 8..11 first-stage flag) or the
    class-1 argument.
    """
    cls: int
    a: int = 0                 # +0
    b: int = 0                 # +4
    power: int = 0xFFFF        # +8
    c: int = 0                 # +0xc
    w10: int = 0               # +0x10
    duty: int = 0              # +0x12
    freq: int = 0              # +0x14
    value: int = 0             # +0x18

    @property
    def layer(self) -> int:
        return self.value & 0xF

    @property
    def stage(self) -> int:
        return (self.value >> 4) & 0xF

    @property
    def first(self) -> int:
        return (self.value >> 8) & 0xF


def sys_cmd(sub: int, b: int = 0, value: int = 0, duty: int = 0, freq: int = 0) -> ManuItem:
    return ManuItem(CLS_SYS, a=sub, b=b, value=value, duty=duty, freq=freq)


# --------------------------------------------------------------------------- configuration
@dataclass
class ProcessConfig:
    """Machine parameters the process items depend on (HardPara.xml / ManuPara.xml / ipAdd.ini).

    Field comments give the parameter key and the config offset used by the code.  Output
    ports are 1-based like the XML (0 = not configured); DA channels are the 1-based XML value.
    """
    enabled: bool = True
    zf_type: int = 0                     # ZF.ZFType +0x4994 (0 none -> no follower/pierce items)
    af_type: int = 0                     # AF.AFType +0x4ac8 (0 none, 5/6 focus axis)
    zf_follow_speed: float = 200.0       # ZF.ZFFollowSpeed +0x4a70 (mm/s)
    zf_up_speed: float = 300.0           # ZF.ZFUpSpeed +0x4a78 (mm/s)
    zf_dock_height: float = 20.0         # ZF.ZFDockHeight +0x4a90 (mm)
    direct_drill_max_height: float = 6.0  # MP.DirectDrillMaxHeight +0x4d70 (mm)
    crash_protect: bool = False          # MP.EnableManuCrashProtect +0x4dcc
    crash_up_height: float = 50.0        # MP.ManuCrashProtectUpHeight +0x4dd0
    crash_min_height: float = 20.0       # MP.EnableManuCrashProtectMinHeight +0x4dd8
    follow_timeout_ms: int = 20000       # ipAdd.ini FollowOvertime (VM+0x860)
    drill_timeout_ms: int = 20000        # ipAdd.ini SectionDrillOvertime (VM+0x864)
    gradual_add_timeout_ms: int = 20000  # ipAdd.ini GradualDrillAddOvertime (VM+0x868)
    da_min: int = 50                     # ipAdd.ini DAMinVal (VM+0x1190)
    z_pulses_per_mm: int = 1000          # config +0xbc28 (int; U value, used for op 104 heights)
    follow_when_dry_run: bool = False    # FC.EnableEmtptMoveFollow +0x4901
    # laser
    laser_type: int = 0                  # LGP.LaserType +0x49a0 (7 = GZ -> DA * 0.95)
    laser_control: int = 0               # LGP.LaserControlType +0x49a4 (3 = IO -> peak via DA)
    laser_da_port: int = 0               # LGP.LaserDAPort +0x49a8 (1-based)
    laser_da_range: int = 0              # LGP.LaserDAType +0x49ac (0 0-10V, 1 0-5V, 2 0-4V)
    do_laser: int = 0                    # LGP.DOLaser +0xbc
    # gas (index 0..5 = Air, O2, N2 low pressure, then high pressure)
    gas_valves: tuple = (0, 0, 0, 0, 0, 0)   # MGP.LowAir/LowO2/LowN2/HighAir/HighO2/HighN2 +0x134..
    ratio_switch: tuple = (0, 0, 0)      # MGP.RatioAirSwitch/O2/N2 +0x38c/+0x3b4/+0x3dc
    ratio_da: tuple = (0, 0, 0)          # MGP.RatioAir/O2/H2 +0x92c.. (1-based DA channel, 0 none)
    max_pressure: tuple = (10.0, 10.0, 10.0)  # MGP.NewDAMAxPressureAir/O2/N2 +0x3578..
    gas_delay_ms: int = 100              # GC.GasDelay +0x48d4
    first_gas_delay_ms: int = 100        # GC.DirectGasDelay +0x48d8
    change_gas_delay_ms: int = 100       # GC.ChangeGasDelay +0x48dc
    proportional_open_ms: int = 50       # MGP.iProportionalOpenSleep +0x4e08
    short_no_up_len: float = 10.0        # FC.ShortNoUpMaxLength +0x4918 (mm)
    # micro joints
    adv_micro_link: bool = False         # MC.EnableAdvMicoLink +0x4df1
    adv_micro_link_pct: int = 60         # MC.AdvMicLinkPower_Pre +0x4df4
    # focus axis (AF types 5/6)
    af_speed: float = 10.0               # AF.AFPosSpeed +0x4ae0
    af_axis: int = 4                     # AF.AFAxis +0x4ad8 (AF type 6; type 5 always uses axis 0x10)
    af_axis_acc: int = 1000              # per-axis acc at +0xbc80 + 0xa0*axis (U value)
    interp_cycle_us: int = 1000          # AX.InterpolationCycle +0x4910 (bolt-drill items per ms)
    other_outputs: dict = field(default_factory=dict)  # remaining DO ports by name (output_map)

    # ---------------------------------------------------------------- constructors
    @classmethod
    def bench(cls) -> "ProcessConfig":
        """Default for ``Machine()``: an on-board follower and three low-pressure valves on
        outputs 1..3 with one proportional valve on DA channel 1 (INFERRED bench wiring; the
        shipped HardPara defaults are all 0 = not configured, see :meth:`from_params`)."""
        return cls(zf_type=1, gas_valves=(1, 2, 3, 0, 0, 0), ratio_da=(1, 1, 1),
                   other_outputs={"red_light": 4, "dedust": 5, "lube": 6})

    @classmethod
    def from_params(cls, mp, ini: Optional[dict] = None) -> "ProcessConfig":
        """Fill from a :class:`mlaser.formats.params.MachineParams` (``ini``: ipAdd.ini values)."""
        from ..formats.params import param_defs
        by_key: dict = {}
        for d in param_defs():
            if d.file in ("HardPara.xml", "ManuPara.xml") and d.key not in by_key:
                by_key[d.key] = d

        def g(key, default=None):
            d = by_key.get(key)
            if d is None:
                return default
            st = mp.hard if d.file == "HardPara.xml" else mp.manu
            try:
                return st.get(d)
            except Exception:  # noqa: BLE001
                return d.default_value

        names = {"red_light": "LGP.DORedLight", "shutter": "LGP.DOLaserGate",
                 "remote_key": "LGP.DORemoteStart", "standby": "DO.WaitSignal",
                 "process": "DO.ManuSignal", "alarm": "DO.AlarmSignal", "dedust": "DO.AlarmRing",
                 "lube": "MP.OilDOPort", "cool_gas": "MGP.CoolGas", "follow": "ZF.DOFollow",
                 "drill": "ZF.DODrill", "clear_ash": "DO.ClearAsh"}
        other = {k: int(g(v, 0) or 0) for k, v in names.items()}
        ini = ini or {}
        return cls(
            zf_type=int(g("ZF.ZFType", 0)), af_type=int(g("AF.AFType", 0)),
            zf_follow_speed=float(g("ZF.ZFFollowSpeed", 200.0)), zf_up_speed=float(g("ZF.ZFUpSpeed", 300.0)),
            zf_dock_height=float(g("ZF.ZFDockHeight", 20.0)),
            direct_drill_max_height=float(g("MP.DirectDrillMaxHeight", 6.0)),
            crash_protect=bool(g("MP.EnableManuCrashProtect", False)),
            crash_up_height=float(g("MP.ManuCrashProtectUpHeight", 50.0)),
            crash_min_height=float(g("MP.EnableManuCrashProtectMinHeight", 20.0)),
            follow_timeout_ms=int(ini.get("FollowOvertime", 20000)),
            drill_timeout_ms=int(ini.get("SectionDrillOvertime", 20000)),
            gradual_add_timeout_ms=int(ini.get("GradualDrillAddOvertime", 20000)),
            da_min=int(ini.get("DAMinVal", 50)),
            follow_when_dry_run=bool(g("FC.EnableEmtptMoveFollow", False)),
            laser_type=int(g("LGP.LaserType", 0)), laser_control=int(g("LGP.LaserControlType", 0)),
            laser_da_port=int(g("LGP.LaserDAPort", 0)), laser_da_range=int(g("LGP.LaserDAType", 0)),
            do_laser=int(g("LGP.DOLaser", 0)),
            gas_valves=tuple(int(g(k, 0)) for k in ("MGP.LowAir", "MGP.LowO2", "MGP.LowN2",
                                                       "MGP.HighAir", "MGP.HighO2", "MGP.HighN2")),
            ratio_switch=tuple(int(g(k, 0)) for k in ("MGP.RatioAirSwitch", "MGP.RatioO2Switch",
                                                         "MGP.RatioN2Switch")),
            ratio_da=tuple(int(g(k, 0)) for k in ("MGP.RatioAir", "MGP.RatioO2", "MGP.RatioH2")),
            max_pressure=tuple(float(g(k, 10.0)) for k in ("MGP.NewDAMAxPressureAir",
                                                              "MGP.NewDAMAxPressureO2",
                                                              "MGP.NewDAMAxPressureN2")),
            gas_delay_ms=int(g("GC.GasDelay", 100)), first_gas_delay_ms=int(g("GC.DirectGasDelay", 100)),
            change_gas_delay_ms=int(g("GC.ChangeGasDelay", 100)),
            proportional_open_ms=int(g("MGP.iProportionalOpenSleep", 50)),
            short_no_up_len=float(g("FC.ShortNoUpMaxLength", 10.0)),
            adv_micro_link=bool(g("MC.EnableAdvMicoLink", False)),
            adv_micro_link_pct=int(g("MC.AdvMicLinkPower_Pre", 60)),
            af_speed=float(g("AF.AFPosSpeed", 10.0)),
            interp_cycle_us=int(g("AX.InterpolationCycle", 1000) or 1000),
            other_outputs=other)

    # ---------------------------------------------------------------- views
    @property
    def has_follower(self) -> bool:
        return self.enabled and self.zf_type != 0

    def output_map(self) -> dict[str, int]:
        """Named output numbers (1-based) for the UI, replacing ``ui_adapter.DEFAULT_OUTPUT_MAP``.
        Unconfigured ports (0) are left out."""
        names = ("gas_air", "gas_o2", "gas_n2", "gas_high_air", "gas_high_o2", "gas_high_n2")
        out = {n: p for n, p in zip(names, self.gas_valves) if p > 0}
        out.update({f"gas_ratio_{n}": p for n, p in zip(("air", "o2", "n2"), self.ratio_switch) if p > 0})
        if self.do_laser > 0:
            out["laser"] = self.do_laser
        out.update({k: v for k, v in self.other_outputs.items() if v > 0})
        lowest = next((p for p in self.gas_valves if p > 0), 0)
        if lowest:
            out.setdefault("gas", lowest)   # the UI's generic "gas" button
        return out

    def peak_da_value(self, pct: float) -> Optional[int]:
        """Laser peak power % -> DA value like FUN_1003f990 (None when no laser DA is set up)."""
        if self.laser_control != 3 or self.laser_da_port < 1:
            return None
        v = float(int(pct))
        if self.laser_type == 7:
            v *= 0.95
        v *= {0: 1.0, 1: 0.5, 2: 0.4}.get(self.laser_da_range, None) or 0.0
        n = F.ftol(v * 100.0)
        if 0 < n < self.da_min:
            n = self.da_min
        return n

    def gas_da_value(self, kind: int, pressure: float) -> int:
        """Gas pressure -> DA value (MainApp FUN_00635fd0 * 1000): ``10 * p / max_p * 1000``.
        The optional pressure maps (DO.Gt*GasDAMapStr) are not reproduced."""
        if kind >= 3 or not (0 < self.ratio_da[kind] < 3):
            return 0
        mx = self.max_pressure[kind] or 10.0
        return F.ftol(10.0 * pressure / mx * 1000.0)


# --------------------------------------------------------------------------- layer view
_LAYER_DEFAULTS: Optional[dict] = None


def _layer_defaults() -> dict:
    global _LAYER_DEFAULTS
    if _LAYER_DEFAULTS is None:
        from ..formats.params import layer_defs
        _LAYER_DEFAULTS = {d.key: d.default_value for d in layer_defs() if "." in d.key}
    return _LAYER_DEFAULTS


_STAGE_ALIASES = {"height": "DrillHeight", "focus": "DrillFocusPos", "duty": "DrillPower",
                  "power": "DrillPower", "freq": "DrillFreq", "frequency": "DrillFreq",
                  "peak": "DrillPeakCurrent", "gas": "DrillGasType", "pressure": "DrillGasPressure",
                  "time_ms": "DrillDelay", "gradual": "EnableGradualDrill"}


@dataclass
class Stage:
    """One pierce stage (layer + 0x158 + 0x58*s, offsets relative to the stage)."""
    height: float = 5.0          # +0x00 GP.DrillHeight{s}
    focus: float = 0.0           # +0x08 GP.DrillFocusPos{s}
    focus_gradual: bool = False  # +0x10 GP.EnableFocusGradual{s}
    focus_end: float = 0.0       # +0x18 GP.FocusGradualEndPos{s}
    pressure: float = 5.0        # +0x20 GP.DrillGasPressure{s}
    gas: int = 0                 # +0x28 GP.DrillGasType{s}
    duty: int = 50               # +0x2c GP.DrillPower{s}
    freq: int = 500              # +0x30 GP.DrillFreq{s}
    peak: int = 100              # +0x34 GP.DrillPeakCurrent{s}
    time_ms: int = 1000          # +0x38 GP.DrillDelay{s} (= GradualTime / FocusGradualTime)
    gradual: bool = False        # +0x3c GP.EnableGradualDrill{s}
    delay_on_ms: int = 0         # +0x40 GP.BeforeLaserOffDelay{s} (used right after laser on)
    delay_off_ms: int = 0        # +0x44 GP.AfterLaserOffDelay{s}
    bolt: bool = False           # +0x48 GP.BoltDrill_Enable_{s+1}
    bolt_duty: int = 0           # +0x4c GP.BoltDrill_Power_{s+1} (ramp end duty)
    bolt_freq: int = 0           # +0x50 GP.BoltDrill_Freq_{s+1} (ramp end frequency)


class LayerView:
    """Typed access to a layer's GP.* process values.

    Precedence: model fields (speed, duty_pct = GP.CutPower, frequency_hz = GP.CutFreq,
    power_pct = GP.CutPeakCurrent, gas, gas_pressure, cut_height) > ``extra['process']``
    overrides (GP.* keys) > ``extra['values']`` (as loaded from LayerPara.xml) > descriptor defaults.
    """

    def __init__(self, lp=None):
        self.lp = lp
        ex = (getattr(lp, "extra", None) or {}) if lp is not None else {}
        self.values = dict(_layer_defaults())
        self.values.update(ex.get("values", {}) or {})
        self.values.update(ex.get("process", {}) or {})
        if lp is not None:
            self.values.update({"GP.CutSpeed": lp.speed, "GP.CutPower": lp.duty_pct,
                                "GP.CutFreq": lp.frequency_hz, "GP.CutPeakCurrent": lp.power_pct,
                                "GP.CutGasType": lp.gas, "GP.CutAirPressure": lp.gas_pressure,
                                "GP.CutHeight": lp.cut_height})
        self.pierce = dict(getattr(lp, "pierce", None) or {}) if lp is not None else {}
        self.index = getattr(lp, "index", 1) if lp is not None else 1

    def __getitem__(self, key: str) -> Any:
        return self.values.get(key if key.startswith("GP.") else "GP." + key)

    def get(self, key: str, default=None) -> Any:
        v = self[key]
        return default if v is None else v

    def f(self, key, default=0.0) -> float:
        return float(self.get(key, default))

    def i(self, key, default=0) -> int:
        return int(float(self.get(key, default)))

    def b(self, key) -> bool:
        return bool(self.get(key, False))

    # ---------------------------------------------------------------- derived
    @property
    def manu_type(self) -> int:
        if "type" in self.pierce:
            return int(self.pierce["type"])
        st = self.pierce.get("stages")
        if st and not self.values.get("GP.ManuType"):
            return {1: 2, 2: 3, 3: 4, 4: 6, 5: 7}.get(min(5, len(st)), 0)
        return self.i("ManuType")

    @property
    def n_stages(self) -> int:
        return PIERCE_STAGES.get(self.manu_type, 0)

    def stage(self, s: int) -> Stage:
        g = lambda k, d: self.get(f"{k}{s}", d)
        st = Stage(height=float(g("DrillHeight", 5.0)), focus=float(g("DrillFocusPos", 0.0)),
                   focus_gradual=bool(g("EnableFocusGradual", False)),
                   focus_end=float(g("FocusGradualEndPos", 0.0)),
                   pressure=float(g("DrillGasPressure", 5.0)), gas=int(g("DrillGasType", 0)),
                   duty=int(g("DrillPower", 50)), freq=int(g("DrillFreq", 500)),
                   peak=int(g("DrillPeakCurrent", 100)), time_ms=int(g("DrillDelay", 1000)),
                   gradual=bool(g("EnableGradualDrill", False)),
                   delay_on_ms=int(g("BeforeLaserOffDelay", 0)), delay_off_ms=int(g("AfterLaserOffDelay", 0)),
                   bolt=bool(self.get(f"BoltDrill_Enable_{s + 1}", False)),
                   bolt_duty=int(float(self.get(f"BoltDrill_Power_{s + 1}", 0) or 0)),
                   bolt_freq=int(float(self.get(f"BoltDrill_Freq_{s + 1}", 0) or 0)))
        stages = self.pierce.get("stages") or []
        if s < len(stages) and isinstance(stages[s], dict):
            kw = {}
            fields_ = Stage.__dataclass_fields__
            rev = {"DrillHeight": "height", "DrillFocusPos": "focus", "EnableFocusGradual": "focus_gradual",
                   "FocusGradualEndPos": "focus_end", "DrillGasPressure": "pressure", "DrillGasType": "gas",
                   "DrillPower": "duty", "DrillFreq": "freq", "DrillPeakCurrent": "peak",
                   "DrillDelay": "time_ms", "EnableGradualDrill": "gradual",
                   "BeforeLaserOffDelay": "delay_on_ms", "AfterLaserOffDelay": "delay_off_ms",
                   "BoltDrill_Enable": "bolt"}
            for k, v in stages[s].items():
                # BoltDrill_* in formats.params stage dicts are attached to stage s, but the NC
                # reads stage s +0x48 = GP.BoltDrill_Enable_{s+1}: take them from the GP keys.
                if v is None or k.startswith("BoltDrill"):
                    continue
                k2 = rev.get(_STAGE_ALIASES.get(k, k), k)
                if k == "time_s":
                    k2, v = "time_ms", float(v) * 1000.0
                if k2 in fields_:
                    typ = type(getattr(st, k2))
                    kw[k2] = bool(v) if typ is bool else typ(float(v))
            st = replace(st, **kw)
        return st

    @property
    def cut_height(self) -> float:
        return self.f("CutHeight", 1.0)

    @property
    def vib(self) -> tuple[int, int]:
        """(thin, thick) vibration abatement levels as selected by GP.ZFVibAbatType."""
        t = self.i("ZFVibAbatType")
        thin, thick = self.i("ZFVibAbat_Level"), self.i("ZFVibAbat_Level_Thick")
        if t == 0:
            return 0, 0
        if t == 1:
            return thin, 0
        if t == 2:
            return 0, thick
        return thin, thick


# --------------------------------------------------------------------------- power curve
def parse_curve_nodes(text) -> list[tuple[int, int]]:
    """GP.PWMCurveNodes / GP.FreqCurveNodes -> [(speed %, value %)].

    CADModule FUN_10104880 consumes int pairs (x %, y %); the string format MainApp parses
    them from is UNKNOWN, so any sequence of integers is read pairwise (e.g. ``"0,30;50,60;100,100"``).
    Lists of pairs are accepted as well.
    """
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        return [(int(a), int(b)) for a, b in text]
    nums = [int(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", str(text))]
    return list(zip(nums[0::2], nums[1::2]))


class SpeedCurve:
    """Value-vs-speed curve of CInterpMrg (CADModule FUN_101049c0 / FUN_10104880 / FUN_10103030).

    * collinear interior nodes are dropped (FUN_101033a0);
    * segment i covers speed ``x_i*0.01*v_cut .. x_{i+1}*0.01*v_cut`` with value
      ``y_i*0.01*base + (v - x0) * slope``;
    * samples faster than ``v_cut - min(0.02*v_cut, 0.1)`` get ``base`` unchanged, as do all
      samples when the curve is disabled or empty;
    * the result is ``value + 0.5`` converted with the x87 rounding control set to CHOP
      (FUN_10103030 0x101030d0 ``or eax,0xc00`` before ``fistp``), i.e. truncated:
      ``trunc((v - x0) * slope + (y0 + 0.5))`` in that operation order (O, parity JC-12).
    """

    def __init__(self, cut_speed: float, base: int, nodes: Sequence[tuple[int, int]], enabled: bool = True):
        self.base = int(base)
        self.threshold = cut_speed - min(0.02 * cut_speed, 0.1)
        pts = list(nodes)
        i = len(pts) - 2
        while i > 0:
            (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
            if (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1) == 0:
                del pts[i]
            i -= 1
        self.segs = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            a, b = x0 * 0.01 * cut_speed, x1 * 0.01 * cut_speed
            slope = ((y1 - y0) * 0.01 * base) / (b - a) if b != a else 0.0
            self.segs.append((a, b, y0 * 0.01 * base, slope))
        self.enabled = enabled and bool(self.segs)

    def __call__(self, v: float) -> int:
        if not self.enabled or v > self.threshold:
            return self.base
        seg = self.segs[0]
        for s in self.segs:
            if s[0] < v <= s[1]:
                seg = s
                break
        return int((v - seg[0]) * seg[3] + (seg[2] + 0.5))


def layer_curves(lv: LayerView) -> tuple[SpeedCurve, SpeedCurve]:
    """(duty curve, frequency curve) for a layer: GP.PowerAdjustWithSpeed + GP.PWMCurveNodes on
    GP.CutPower, GP.FreqAdjustWithSpeed + GP.FreqCurveNodes on GP.CutFreq (MainApp passes them
    to CADModule slot 0x220 at 0x004598b9)."""
    v = lv.f("CutSpeed", 100.0)
    duty = SpeedCurve(v, lv.i("CutPower", 100), parse_curve_nodes(lv.get("PWMCurveNodes")),
                      lv.b("PowerAdjustWithSpeed"))
    freq = SpeedCurve(v, lv.i("CutFreq", 5000), parse_curve_nodes(lv.get("FreqCurveNodes")),
                      lv.b("FreqAdjustWithSpeed"))
    return duty, freq


def slow_start_end(lv: LayerView) -> tuple[Optional[tuple], Optional[tuple]]:
    """Override blocks at layer +0x380 (start) and +0x3a8 (end): ``(length mm, speed mm/s,
    duty, freq, laser override)`` or None when disabled (GP.UD_UpEnable / GP.UD_DownEnable).

    FUN_00462630 replaces the duty (+0x1c) and frequency (+0x20) of samples closer than the
    length (+0x08) to the start / end of the element when both enable bytes (+0x00, +0x18 =
    GP.UD_*AdvEnable) are set; the speed (+0x10) goes to the planner (0x00459701).
    """
    def blk(p):
        if not lv.b(f"UD_{p}Enable"):
            return None
        return (lv.f(f"UD_{p}Len", 10.0), lv.f(f"UD_{p}Speed", 100.0), lv.i(f"UD_{p}Duty", 10),
                lv.i(f"UD_{p}Freq", 1000), lv.b(f"UD_{p}AdvEnable"))
    return blk("Up"), blk("Down")


# --------------------------------------------------------------------------- MainApp layer
@dataclass
class SeqState:
    following: bool = False      # ctx+0x161
    follow_again: bool = False   # ctx+0x162 (set by the frog jump; unused here)
    first_gas: bool = True       # ctx+0x163
    gas: int = -1                # ctx+0x164
    gas_on: bool = False         # ctx+0x1e0
    freq: int = 0                # +0x14 of MainApp's reused ManuItem (laser off keeps the last freq)


class ProcessSequencer:
    """MainApp's ManuItem sequences (start cut, pierce, end cut, gas, laser)."""

    def __init__(self, cfg: ProcessConfig, layers=None, dry_run: bool = False):
        self.cfg = cfg
        self.layers = layers if layers is not None else {}
        self.dry = dry_run
        self.st = SeqState()

    def view(self, layer: int) -> LayerView:
        lp = self.layers.get(layer) if hasattr(self.layers, "get") else None
        v = LayerView(lp)
        v.index = layer
        return v

    # ---------------------------------------------------------------- primitives
    def gas_select(self, gas: int, pressure: float) -> list[ManuItem]:
        """FUN_004645d0: close the previous gas (valve, proportional switch, DA 0) when the type
        changes, open the new valve and switch, wait MGP.iProportionalOpenSleep, set the DA."""
        c, out = self.cfg, []
        cur = self.st.gas
        if cur != -1 and cur != gas and 0 <= cur < 6:
            if c.gas_valves[cur] > 0:
                out.append(sys_cmd(SUB_DO, c.gas_valves[cur] - 1, 0))
            k = cur - 3 if cur > 2 else cur
            if 0 <= k < 3:
                if c.ratio_switch[k] > 0:
                    out.append(sys_cmd(SUB_DO, c.ratio_switch[k] - 1, 0))
                if c.ratio_da[k] > 0:
                    out.append(sys_cmd(SUB_DA, c.ratio_da[k] - 1, 0))
        if 0 <= gas < 6:
            if c.gas_valves[gas] > 0:
                out.append(sys_cmd(SUB_DO, c.gas_valves[gas] - 1, 1))
            k = gas - 3 if gas > 2 else gas
            if 0 <= k < 3:
                if c.ratio_switch[k] > 0:
                    out.append(sys_cmd(SUB_DO, c.ratio_switch[k] - 1, 1))
                if c.ratio_da[k] > 0:
                    out.append(sys_cmd(SUB_DELAY, 0, c.proportional_open_ms))
                    out.append(sys_cmd(SUB_DA, c.ratio_da[k] - 1, c.gas_da_value(k, pressure)))
        return out

    def gas_delay(self, gas: int, consumed_ms: int = 0) -> list[ManuItem]:
        """FUN_004649c0: first-gas + gas-on + gas-change delays minus time already spent."""
        c, ms = self.cfg, 0
        if self.st.first_gas:
            ms = c.first_gas_delay_ms
            self.st.first_gas = False
        if not self.st.gas_on:
            ms += c.gas_delay_ms
        if self.st.gas != -1 and gas != self.st.gas:
            ms += c.change_gas_delay_ms
        self.st.gas = gas
        self.st.gas_on = True
        return [sys_cmd(SUB_DELAY, 0, max(0, ms - consumed_ms))]

    def gas_off(self) -> list[ManuItem]:
        """FUN_004648d0: close the current valve and zero its proportional DA."""
        c, out, g = self.cfg, [], self.st.gas
        if 0 <= g < 6 and c.gas_valves[g] > 0:
            out.append(sys_cmd(SUB_DO, c.gas_valves[g] - 1, 0))
        if 0 <= g < 3 and c.ratio_da[g] > 0:
            out.append(sys_cmd(SUB_DA, c.ratio_da[g] - 1, 0))
        self.st.gas_on = False
        return out

    def laser_on(self, freq: int, duty: int, on: bool = True) -> list[ManuItem]:
        """FUN_00464ab0: LGP.DOLaser on, then class 1 sub 1 with freq (>= 1) and duty."""
        out = []
        if self.cfg.do_laser > 0:
            out.append(sys_cmd(SUB_DO, self.cfg.do_laser - 1, 1))
        f = int(freq) if int(freq) > 0 else 1
        self.st.freq = f
        out.append(sys_cmd(SUB_LASER, 0, 0, duty=int(duty) if on else 0, freq=f))
        return out

    def laser_off(self) -> list[ManuItem]:
        """FUN_00464c40: LGP.DOLaser off, class 1 sub 1 with duty 0.  MainApp reuses one
        ManuItem, so +0x14 still holds the last laser frequency (sent as is by the NC)."""
        out = []
        if self.cfg.do_laser > 0:
            out.append(sys_cmd(SUB_DO, self.cfg.do_laser - 1, 0))
        out.append(sys_cmd(SUB_LASER, 0, 0, duty=0, freq=self.st.freq))
        return out

    @staticmethod
    def delay(ms: int) -> list[ManuItem]:
        """FUN_00464d60 (non-motion branch): class 1 sub 5 (NC skips it when ms <= 0)."""
        return [sys_cmd(SUB_DELAY, 0, int(ms))]

    def motion_delay(self, ms: int) -> list[ManuItem]:
        """FUN_00464d60 motion branch (``param_5 != 0``): ``(1000 / InterpolationCycle) * ms``
        zero ManuItems from the template constructor FUN_00413760 (class 0, all fields 0, so the
        card replays zero-delta periods with the beam OFF: duty 0)."""
        n = (1000 // max(1, int(self.cfg.interp_cycle_us))) * int(ms)
        return [ManuItem(CLS_MOTION, power=0) for _ in range(max(0, n))]

    def bolt_ramp(self, ms: int, st: Stage) -> list[ManuItem]:
        """FUN_00458650 (bolt drill, stage +0x48 set): ``n = (1000 / InterpolationCycle) * ms``
        class-1 sub-1 laser items, item k = 1..n with ``duty = trunc(k * dd + DrillPower)`` and
        ``freq = trunc(k * df + DrillFreq)``, ``dd = (BoltDrill_Power - DrillPower) / n``,
        ``df = (BoltDrill_Freq - DrillFreq) / n`` (x87 chop mode at 0x45871c / 0x45874a; the
        decompiler's ROUND is a truncation).  Duty is stored as a byte, frequency as a u16."""
        if not st.bolt:
            return []
        n = (1000 // max(1, int(self.cfg.interp_cycle_us))) * int(ms)
        if n <= 0:
            return []
        dd = float(st.bolt_duty - st.duty) / float(n)
        df = float(st.bolt_freq - st.freq) / float(n)
        out = []
        for k in range(1, n + 1):
            f = int(float(st.freq) + float(k) * df) & 0xFFFF
            d = int(float(st.duty) + float(k) * dd) & 0xFF
            out.append(sys_cmd(SUB_LASER, 0, 0, duty=d, freq=f))
        return out      # built in a local copy of the zero template: the reused ManuItem keeps its freq

    @staticmethod
    def laser_sync(on: bool) -> list[ManuItem]:
        """FUN_004590a0: class 1 sub 8, +4 = on."""
        return [sys_cmd(SUB_LASER_SYNC, int(bool(on)))]

    # ---------------------------------------------------------------- sequences
    def pierce(self, layer: int) -> list[ManuItem]:
        """FUN_00464e10: staged or smooth pierce; nothing for direct / fix-height layers, without
        a follower, or in a dry run (unless FC.EnableEmtptMoveFollow)."""
        lv, c = self.view(layer), self.cfg
        smooth = lv.b("EnableSmoothPierce")
        if lv.manu_type == 0 and not smooth:
            return []
        if not c.has_follower:
            return []
        if self.dry and not c.follow_when_dry_run:
            return []
        out: list[ManuItem] = []
        if smooth:
            out += self.gas_select(lv.i("CutGasType"), lv.f("CutAirPressure", 5.0))
            out += self.gas_delay(lv.i("CutGasType"), 0)
            out.append(ManuItem(CLS_PIERCE_START, value=layer | 0xE0))
            out += self.laser_on(lv.i("SmoothPierceDrillFreq", 500), lv.i("SmoothPierceDrillPower", 50), True)
            if lv.i("SmoothPierceDrillTime_ms", 100) >= 1:
                out.append(ManuItem(CLS_PIERCE_END, value=layer | 0xE0))
            return out
        n = lv.n_stages
        for s in range(n - 1, -1, -1):
            st = lv.stage(s)
            out += self.gas_select(st.gas, st.pressure)
            out += self.gas_delay(st.gas, 0)
            out.append(ManuItem(CLS_PIERCE_START, value=(s << 4) | layer | (int(s == n - 1) << 8)))
            out += self.laser_on(st.freq, st.duty, True)
            out += self.delay(st.delay_on_ms)
            gradual = (st.gradual or (st.focus_gradual and 1 <= c.af_type <= 6)) and st.time_ms >= 1
            if not gradual:
                if st.bolt:   # FUN_00464d60(.., DrillDelay, stage+0x48): zero motion items, beam off
                    out += self.motion_delay(max(0, st.time_ms))
                else:
                    out += self.delay(max(0, st.time_ms))
            else:
                out.append(ManuItem(CLS_PIERCE_END, value=(s << 4) | layer))
            if st.bolt:
                out += self.bolt_ramp(max(0, st.time_ms), st)   # FUN_00458650
                if gradual:
                    out.append(ManuItem(CLS_AFTER_GRADUAL, value=layer))
            out += self.laser_off()
            out += self.delay(st.delay_off_ms)
            out += self.gas_off()
        return out

    def start_cut(self, layer: int, laser: bool = True) -> list[ManuItem]:
        """FUN_00465460: cut gas, follow item (unless already following), gas delay, laser on with
        GP.CutFreq (+0x80) / GP.CutPower (+0x7c), GP.LaserOnDelay (+0x88)."""
        lv, c, out = self.view(layer), self.cfg, []
        if self.dry and not c.follow_when_dry_run:
            return out                  # MainApp+0x4847 (dry run) && !FC.EnableEmtptMoveFollow
        gas = lv.i("CutGasType")
        if not self.dry:
            out += self.gas_select(gas, lv.f("CutAirPressure", 5.0))
        if not self.st.following and c.has_follower and (not self.dry or c.follow_when_dry_run):
            mt = lv.manu_type
            if mt < 2 and lv.b("NoFollow"):
                it = ManuItem(CLS_PEAK, value=layer)
            elif mt == MANU_ADV_FIX_HEIGHT:
                it = ManuItem(CLS_ADV_FIX_HEIGHT, value=layer + 0x10)
            elif mt == MANU_FIX_HEIGHT:
                it = ManuItem(CLS_FIX_HEIGHT, value=layer)
            else:
                it = ManuItem(CLS_FOLLOW, value=layer + (0x10 if self.st.follow_again else 0))
            self.st.following = it.cls in (CLS_FOLLOW, CLS_ADV_FIX_HEIGHT)
            out.append(it)
        out += self.gas_delay(gas, 0)
        if not self.dry:
            out += self.laser_on(lv.i("CutFreq", 5000), lv.i("CutPower", 100), laser)
            out += self.delay(lv.i("LaserOnDelay"))
        return out

    def end_cut(self, layer: int, next_layer: Optional[int] = None, hop_mm: float = math.inf) -> list[ManuItem]:
        """FUN_004657e0: before/after laser-off delays, gas off (kept for a short hop with
        GP.ShortDistGasKeepOn, never closed with GP.NoCloseGasInManu), lift (class 8; class 9
        when no cut follows) unless the short-hop no-lift rule applies.

        Layers (O, 0x00465be8 / 0x00465c19 / 0x00465cdf): GP.NoCloseGasInManu is read from the
        CURRENT element's layer, GP.ShortDistGasKeepOn and the short-hop lift flags
        (ShortDistNoUp, ManuType, PreDrill, NoFollow, cut height) from the NEXT cut's layer."""
        lv, c, out = self.view(layer), self.cfg, []
        out += self.delay(lv.i("LaserOffBeforeDelay"))
        out += self.laser_off()
        out += self.delay(lv.i("LaserOffAfterDelay"))
        last = next_layer is None
        nv = self.view(next_layer) if not last else lv
        if not self.dry:
            if last or not lv.b("NoCloseGasInManu"):
                keep = (not last and nv.b("ShortDistGasKeepOn") and hop_mm < c.short_no_up_len)
                if not keep:
                    out += self.gas_off()
        if self.st.following and c.has_follower:
            lift = True
            if (not last and nv.b("ShortDistNoUp")
                    and (nv.manu_type == 0 or nv.b("PreDrill")) and not nv.b("NoFollow")
                    and abs(lv.cut_height - nv.cut_height) < 0.01 and hop_mm < c.short_no_up_len):
                lift = False
            if lift:
                out.append(ManuItem(CLS_FINAL_LIFT if last else CLS_LIFT, value=layer & 0xF))
                self.st.following = False
        return out

    def pre_jump(self, next_layer: int) -> list[ManuItem]:
        """Class 0x12 before a rapid that leads to a cut (job loop 0x0045b331): pre-position
        focus / peak power for the next layer during the jump."""
        return [ManuItem(CLS_PRE_JUMP, value=next_layer)]


# --------------------------------------------------------------------------- NC layer
# classes FUN_10043c20 skips without output (explicit test before the class chain)
_SILENT_CLASSES = (0xC, 0xD, 0xF, 0x10)
# follower classes gated by ``cfg+0x4994 != 0`` (ZF.ZFType); without a follower they fall
# through to the final else-branch, i.e. they are packed as an op-3000 step from their fields
_ZF_CLASSES = (CLS_PIERCE_START, CLS_PIERCE_END, CLS_FOLLOW, CLS_LIFT, CLS_FINAL_LIFT,
               CLS_FIX_HEIGHT, CLS_PEAK, CLS_ADV_FIX_HEIGHT)
FOCUS_WAIT_ADD_MS = 50.0     # FUN_1003f470: ftol(|dpos| / v * 1000 + 50.0) (0x1003f7df.., 50.0 at 0x1008b948)
AF5_AXIS = 0x10              # FUN_1003f470 0x1003f724 / FUN_1003f830 case 5: AF type 5 uses axis 0x10


class ManuEncoder:
    """Port of NCModule FUN_10043c20 (one ManuItem -> its FIFO items).

    Start state = NC program start (ctor FUN_10056850, FUN_10047c50): ``prev_cls`` (VM+0x117c)
    = -1, so the first process item of a job gets no op 3001; ``peak`` (VM+0x11c8) = 0, so a
    first peak of 0 % sends no DA; ``focus`` (VM+0x11d0) = the focus-axis position read from
    the card at program start (``focus_pos``, 0.0 by default), so a first focus target equal
    to it is not moved to.

    Classes: 1 system commands, 3 op 3002, 0xe op 3001, 0xc/0xd/0xf/0x10 nothing; 6 (+0 == 3:
    focus wait + follower wait), 0x12 (pre-jump focus / peak) and 0x13 (wait, focus wait) are
    encoded with or without a follower; the follower classes 4, 5, 7, 8, 9, 0xa, 0xb, 0x11 only
    with ZF.ZFType != 0.  Everything else (class 0 motion, 2, 0x14..0xff, and the follower classes
    without a follower) is the final else-branch: an op-3000 step built from the item's own
    dx/dy/freq/duty/+8 fields (:func:`frames.encode_step`).
    """

    def __init__(self, cfg: ProcessConfig, layers=None, dry_run: bool = False, focus_pos: float = 0.0):
        self.cfg = cfg
        self.layers = layers if layers is not None else {}
        self.dry = dry_run
        self.prev_cls = -1           # VM+0x117c (-1 at program start, FUN_10047c50 0x10047f00)
        self.height = 0.0            # global 0x100b4fa8: current pierce height
        self.focus: float = float(focus_pos)   # VM+0x11d0 last focus position (card value at start)
        self.peak: int = 0           # VM+0x11c8 last peak-current argument (0 at program start)
        self.process_ms = 0.0        # estimated card time spent in process items

    def view(self, layer: int) -> LayerView:
        lp = self.layers.get(layer) if hasattr(self.layers, "get") else None
        v = LayerView(lp)
        v.index = layer
        return v

    # ---------------------------------------------------------------- helpers
    def _crash(self, value: int) -> list[list[int]]:
        """FUN_1003f3e0."""
        c = self.cfg
        if c.crash_protect and c.has_follower:
            return [F.encode_crash_protect(value, c.crash_up_height)]
        return []

    def _peak(self, pct: int) -> list[list[int]]:
        """FUN_1003f990: DA for the laser peak power, only when it changes."""
        if self.dry or self.peak == pct:
            return []
        v = self.cfg.peak_da_value(pct)
        if v is None:
            return []
        self.peak = pct
        return [F.encode_da(self.cfg.laser_da_port - 1, v)]

    def _focus_axis(self) -> int:
        """AF type 5: the constant axis 0x10; AF type 6: AF.AFAxis (+0x4ad8)."""
        return AF5_AXIS if self.cfg.af_type == 5 else self.cfg.af_axis

    def _focus(self, speed: float, pos: float) -> tuple[list[list[int]], int]:
        """FUN_1003f470 for AF types 5/6 (focus axis move).  Returns (items, wait_ms or -1);
        the wait is ``ftol(|pos - VM+0x11d0| / v * 1000 + 50)``."""
        c = self.cfg
        if c.af_type not in (5, 6) or pos == self.focus:
            return [], -1
        speed = max(1.0, speed)
        ppm = c.z_pulses_per_mm
        ms = F.ftol(abs(pos - self.focus) / speed * 1000.0 + FOCUS_WAIT_ADD_MS)
        self.focus = pos
        return [F.encode_axis_move(self._focus_axis(), F.ftol(ppm * speed), c.af_axis_acc,
                                   F.ftol(ppm * pos))], ms

    def _focus_wait(self, ms: int) -> list[list[int]]:
        """FUN_1003f830 for AF types 5/6: wait for the focus axis (timeout VM+0x860 + ms)."""
        if ms < 0 or self.cfg.af_type not in (5, 6):
            return []
        return [F.encode_wait(F.WAIT_AXIS, b=self._focus_axis(), c=0,
                              timeout_ms=self.cfg.follow_timeout_ms + ms)]

    def _wait_zf(self, state: int, timeout: int) -> list[list[int]]:
        return [F.wait_zf(state, timeout)]

    @staticmethod
    def _clamp(v, lo, hi):
        return lo if v < lo else hi if v > hi else v

    def _zest(self, dist_mm: float, speed: float) -> None:
        if speed > 0:
            self.process_ms += abs(dist_mm) / speed * 1000.0

    # ---------------------------------------------------------------- encode
    def encode(self, it: ManuItem) -> list[list[int]]:
        out: list[list[int]] = []
        cls = it.cls
        if 0 < cls < 0x14 and cls != CLS_MARKER and self.prev_cls == 0:
            out.append(F.encode_mark())
        out += self._encode(it)
        self.prev_cls = cls
        return out

    def encode_all(self, items: Iterable[ManuItem]) -> list[list[int]]:
        out: list[list[int]] = []
        for it in items:
            out += self.encode(it)
        return out

    def _encode(self, it: ManuItem) -> list[list[int]]:
        c = self.cfg
        zf = c.has_follower
        cls = it.cls
        if cls == CLS_SYS:
            sub = it.a
            if sub in (1, 2):
                return [F.encode_laser(it.freq, it.duty)]
            if sub == SUB_DA:
                return [F.encode_da(it.b, it.value)]
            if sub == SUB_DO:
                w = F.encode_output(it.b, bool(it.value))
                return [w] if w else []
            if sub == SUB_DELAY:
                if it.value > 0:
                    self.process_ms += it.value
                    return [F.wait_ms(it.value)]
                return []
            if sub == SUB_CRASH:
                return self._crash(it.value)
            if sub == SUB_LASER_SYNC:
                return [F.encode_laser_sync(1000 if it.b == 1 else 0)]
            return []
        if cls == CLS_DWELL_3002:
            return [F.fifo_item(F.OP_3002, it.value)]
        if cls == CLS_MARKER:
            return [F.encode_mark()]
        if cls in _SILENT_CLASSES:
            return []
        lv = self.view(it.layer)
        if cls == CLS_WAIT_ZF:
            if it.a == 3:
                return self._focus_wait(10000) + self._wait_zf(it.b, c.follow_timeout_ms)
            return []
        if cls == CLS_PRE_JUMP:
            if lv.b("EnableSmoothPierce"):
                return self._focus(c.af_speed, lv.f("SmoothPierceDrillFocusPos"))[0]
            n = lv.n_stages
            if n:
                return self._focus(c.af_speed, lv.stage(n - 1).focus)[0]
            return self._peak(lv.i("CutPeakCurrent", 100)) + self._focus(c.af_speed, lv.f("CutFocusPos"))[0]
        if cls == CLS_AFTER_GRADUAL:
            return (self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms) if zf else []) + self._focus_wait(0)
        if cls not in _ZF_CLASSES or not zf:
            return [self._step_item(it)]
        if cls == CLS_PIERCE_START:
            return self._pierce_start(it, lv)
        if cls == CLS_PIERCE_END:
            return self._pierce_end(it, lv)
        if cls == CLS_FOLLOW:
            out = self._crash(0)
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out += fi + self._peak(lv.i("CutPeakCurrent", 100))
            h = max(lv.cut_height, 0.5)
            thin, thick = lv.vib
            out.append(F.encode_follow_on(c.zf_follow_speed, F.ftol(h * c.z_pulses_per_mm), thin, thick))
            out += self._wait_zf(F.ZF_FOLLOWING, c.follow_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
            self.height = lv.cut_height
            return out + self._focus_wait(ms)
        if cls == CLS_LIFT:
            out = [F.encode_z_rel(c.zf_follow_speed, -lv.f("UpHeight", 20.0)),
                   *self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)]
            self._zest(lv.f("UpHeight", 20.0), c.zf_follow_speed)
            return out + self._crash(F.ftol(c.crash_min_height))
        if cls == CLS_FINAL_LIFT:
            self._zest(c.zf_dock_height, c.zf_up_speed)
            return [F.encode_z_rel(c.zf_up_speed, -c.zf_dock_height),
                    *self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)]
        if cls == CLS_PEAK:
            return self._peak(lv.i("CutPeakCurrent", 100))
        if cls == CLS_FIX_HEIGHT:
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out = fi + self._peak(lv.i("CutPeakCurrent", 100))
            out.append(F.fifo_item(F.OP_FOLLOW_ON, F.ftol(c.zf_follow_speed * 10.0), 1000))
            out += self._wait_zf(F.ZF_FOLLOWING, c.follow_timeout_ms)
            out.append(F.encode_z_rel(c.zf_follow_speed, -lv.cut_height - 1.0))
            out += self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)
            return out + self._focus_wait(ms)
        if cls == CLS_ADV_FIX_HEIGHT:
            fi, ms = self._focus(c.af_speed, lv.f("CutFocusPos"))
            out = fi + self._peak(lv.i("CutPeakCurrent", 100))
            out.append(F.encode_z_abs(c.zf_follow_speed, lv.f("AdvFixHeightCutPos", 20.0)))
            out += self._wait_zf(F.ZF_ARRIVED, c.follow_timeout_ms)
            return out + self._focus_wait(ms)
        return []

    @staticmethod
    def _step_item(it: ManuItem) -> list[int]:
        """FUN_10043c20's final else-branch (non-bus card): op 3000 from +0 / +4 (dx, dy), +0x14
        (freq, 0 -> 5000), +0x12 (duty) and +8 (sent only with a duty).  |d| > 127 never comes
        from the rebuild (the original packs |low byte| with the per-axis direction word)."""
        return F.encode_step(int(it.a), int(it.b), int(it.freq) & 0xFFFF, int(it.duty) & 0xFF,
                             int(it.power) & 0xFFFF)

    def _pierce_start(self, it: ManuItem, lv: LayerView) -> list[list[int]]:
        """Class 4 (0x1004442a): follow to the stage height (first stage, op 105) or move
        relative from the previous stage height (op 109), then wait for the follower."""
        c = self.cfg
        out = self._crash(0)
        thin, thick = lv.vib
        if it.stage == STAGE_SMOOTH:
            raw = lv.f("SmoothPierceDrillHeight", 5.0)
            h = 1.0 if raw > 10.0 else raw
            out.append(F.encode_follow_to(c.zf_follow_speed, h, c.zf_up_speed, raw - h, thin, thick))
            out += self._wait_zf(F.ZF_FOLLOW_TO_DONE, c.drill_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
            self.height = raw
            return out
        st = lv.stage(it.stage)
        out += self._peak(st.peak)
        fi, ms = self._focus(c.af_speed, st.focus)
        out += fi
        if it.first == 1:
            h = 1.0 if st.height > c.direct_drill_max_height else st.height
            out.append(F.encode_follow_to(c.zf_follow_speed, h, c.zf_up_speed, st.height - h, thin, thick))
            out += self._wait_zf(F.ZF_FOLLOW_TO_DONE, c.drill_timeout_ms)
            self._zest(c.zf_dock_height, c.zf_follow_speed)
        else:
            out.append(F.encode_z_rel(c.zf_up_speed, -(st.height - self.height)))
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms)
            self._zest(st.height - self.height, c.zf_up_speed)
        out += self._focus_wait(ms)
        self.height = st.height
        return out

    def _pierce_end(self, it: ManuItem, lv: LayerView) -> list[list[int]]:
        """Class 5 (0x100447fc): gradual height change during the beam-on time (op 108 to the
        cut height after the last stage, op 109 to the next stage), speed = |dh| / time clamped
        to 0.1..200 mm/s."""
        c = self.cfg
        s = it.stage
        if s == STAGE_CLEAN:
            return []
        out: list[list[int]] = []
        if s == STAGE_SMOOTH:
            out += self._peak(lv.i("SmoothPierceDrillPeakCurrent", 100))
            t = lv.i("SmoothPierceDrillTime_ms", 100)
            ts = max(t, 1) * 0.001
            fpos, cpos = lv.f("SmoothPierceDrillFocusPos"), lv.f("CutFocusPos")
            fi, ms = self._focus(self._clamp(abs(fpos - cpos) / ts, 0.01, 30.0), cpos)
            out += fi
            h0, h1 = lv.f("SmoothPierceDrillHeight", 5.0), lv.cut_height
            delta = h0 - h1
            v = self._clamp(abs(delta) / ts, 0.1, 200.0)
            out.append(F.encode_z_gradual(v, delta, h1))
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms + t)
            self.height = h1
            return out + self._focus_wait(ms)
        st = lv.stage(s)
        out += self._peak(st.peak)
        ts = max(st.time_ms, 1) * 0.001
        if st.focus_gradual:
            fi, ms = self._focus(self._clamp(abs(st.focus_end - st.focus) / ts, 0.01, 30.0), st.focus_end)
        else:
            fi, ms = self._focus(c.af_speed, st.focus)
        out += fi
        if s == 0:
            nxt, last = lv.cut_height, True
        else:
            nxt, last = lv.stage(s - 1).height, False
        delta = st.height - nxt
        v = self._clamp(abs(delta) / ts, 0.1, 200.0)
        if last:
            out.append(F.encode_z_gradual(v, delta, nxt))
        else:
            out.append(F.encode_z_rel(v, delta))
        if not st.bolt:
            out += self._wait_zf(F.ZF_ARRIVED, c.drill_timeout_ms + st.time_ms)
            out += self._focus_wait(ms)
        self.process_ms += st.time_ms
        self.height = nxt
        return out


def encode_sequence(items: Iterable[ManuItem], cfg: ProcessConfig, layers=None,
                    dry_run: bool = False) -> list[list[int]]:
    """Convenience: encode a ManuItem list with a fresh :class:`ManuEncoder`."""
    return ManuEncoder(cfg, layers, dry_run).encode_all(items)


def describe(wire: Iterable[Sequence[int]]) -> list[dict]:
    """Decode wire items (header + params) with :func:`frames.describe_item`."""
    return [F.describe_item(w[0] & 0xFFFF, w[1:]) for w in wire]
