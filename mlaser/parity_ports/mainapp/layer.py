"""The GP.* layer parameter block of MainApp / NCModule (layer = +0x4ed8 + 0x480 * layer) as a
typed view, and its pierce stages (layer + 0x158 + 0x58 * s).  Moved from
``mlaser.controller.process`` (Wave 1).

Defaults come from the parameter registry (``formats/data/mlaser_params.json``, read lazily
through :mod:`mlaser.formats.params`): the one import outside the fence, recorded as an
import-linter ignore until the ConfigService (docs/ROADMAP.md Wave 2) hands the defaults in.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

__port__ = {
    "source": "MainApp",
    "functions": {"+0x4ed8 + 0x480 * layer": "LayerView", "+0x158 + 0x58 * s": "Stage",
                  "FUN_00464e10 (switch on GP.ManuType)": "PIERCE_STAGES / LayerView.n_stages"},
    "parity": ["jobnc", "jobmainapp"],
}

# GP.ManuType -> number of pierce stages (FUN_00464e10 switch)
PIERCE_STAGES = {2: 1, 3: 2, 4: 3, 6: 4, 7: 5}
MANU_DIRECT, MANU_FIX_HEIGHT, MANU_ADV_FIX_HEIGHT = 0, 1, 5


# --------------------------------------------------------------------------- layer view
_LAYER_DEFAULTS: Optional[dict] = None


def _layer_defaults() -> dict:
    global _LAYER_DEFAULTS
    if _LAYER_DEFAULTS is None:
        from ...formats.params import layer_defs
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
