"""Mlaser machine / process parameter files (``dat/HardPara.xml``, ``dat/ManuPara.xml``,
``dat/LayerPara.xml``; legacy ``dat/SystemPara.xml``; backups ``Bk*.xml``) and the per-layer
cutting parameters.

Evidence
--------
* **Descriptor tables** (MainApp.exe static initialisers, e.g. 0x7ddfac for ``GP.CutSpeed``):
  global arrays of 0xd0-byte records ``{+0 category, +0x1c key, +0x38 lang key, +0x54 value ptr
  (app+off), +0x58 type, +0x5c default (text), +0x78 unit, +0x98 min, +0xa0 max, +0xa8 option
  list, +0xac option count, +0xb0 condition, +0xcc flag}``.  Table getters ``FUN_006352f0`` ..
  ``FUN_00635f90``; they were extracted to ``data/mlaser_params.json`` (1129 entries: keys,
  types, defaults, units, ranges, struct offsets and the label keys). The labels themselves come
  from mlaser's own catalogue (``mlaser/i18n/en.json``) and, for other languages, from
  translations the user imported from their own install (docs/ASSETS.md).
* **Files** (``FUN_004d90e0``): ``<exe>\\dat`` (created with SHCreateDirectoryExW) holds
  ``HardPara.xml`` ("hardware parameter file", ParaModule slot 0), ``ManuPara.xml``
  ("software parameter file", slot 1) and ``LayerPara.xml`` ("layer parameter file", slot 2).
  If ``SystemPara.xml`` exists and ``HardPara.xml`` does not, all three slots are loaded from
  the legacy SystemPara.xml (migration).  Missing files are re-created from defaults.
* **Which table goes to which slot**: ParaModule vtable+0x18 (load) / +0x1c (save) calls at
  MainApp 0x4d96b8 (slot 0, hard table 0xafb4d8 x457), 0x4dab6e.. 0x4db24f (slot 1: tables
  0xaec428, 0xb24ea8, 0xb27060, 0xb290b8, 0xb12ee0, 0xb1e650, 0xb29fb8), 0x4d9c4d (slot 2, the
  11 per-layer copies of 0xaf1210 x164) and 0x4da42b (slot 2, CO2 layer tables, 22 entries).
* **Per-layer copies** (``FUN_006354c0``): 11 layers; layer *i* uses category
  ``"LayerParam%d" % (i+1)`` (``FUN_004ea320`` = base + number) and a value block at
  app+0x4ed8 + i*0x480.  CO2 layers use ``CO2LayerParam%d``.
* **XML mapping** (ParaModule.dll ``CXMLParaEngine``): full key = ``"P" + category + "." + key``
  (``DAT_1001af94`` = L"P", separator ``DAT_1001af90`` = L"."; load ``FUN_10011010``, save
  ``FUN_10011510``).  ``FUN_10010d30`` creates ``<ParameterRoot>`` then one element per path
  component and stores the last component as an attribute.  So ``GP.CutSpeed`` of layer 1 is
  ``<ParameterRoot><PLayerParam1><GP CutSpeed="100"/></PLayerParam1></ParameterRoot>``.
  Missing attributes fall back to the descriptor default.  Values are written with
  boost::lexical_cast (doubles ``%.17g``, bools ``0/1``).  Files are written to ``<name>.tmp``
  then ``MoveFileExW`` (``FUN_1000e360``).  The engine can also use the registry
  (``RegCreateKeyW(HKCU, ...)``, ``FUN_1000ba20``) - not reproduced.
* Encoding of the XML on disk: UTF-8 assumed (the engine auto-detects UTF-8/UTF-16; INFERRED).
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from ..model import LayerParams

PathLike = Union[str, Path]
DATA_FILE = Path(__file__).with_name("data") / "mlaser_params.json"
N_LAYERS = 11            # FUN_006354c0 loop bound 0xb
ROOT_TAG = "ParameterRoot"
KEY_PREFIX = "P"         # DAT_1001af94

TYPE_NAMES = {1: "int", 2: "double", 3: "string", 4: "enum", 5: "bool", 6: "do_port", 7: "di_port",
              8: "ipv4", 9: "pulse_equiv", 10: "da_port", 11: "gas_ratio", 12: "do_port_ext",
              13: "serial_port", 14: "color"}
FILES = ("HardPara.xml", "ManuPara.xml", "LayerPara.xml")


@dataclass(frozen=True)
class ParamDef:
    """One descriptor record from MainApp's parameter tables."""
    file: Optional[str]
    table: str
    index: int
    category: str
    key: str
    lang: str
    label_en: str
    label_zh: str
    type: int
    type_name: str
    default: str
    unit: str
    min: float
    max: float
    cond: str = ""
    flag: Optional[int] = None
    struct_offset: Optional[int] = None
    options: tuple = ()

    def parse(self, text: Optional[str]) -> Any:
        """Convert stored text to a Python value according to ``type``."""
        if text is None:
            text = self.default
        t = self.type
        try:
            if t == 3:
                return "" if text in ('L""', None) else text
            if t == 2 or t == 9:
                return float(text)
            if t == 5:
                return text.strip() not in ("0", "", "false", "False")
            return int(float(text))
        except (TypeError, ValueError):
            return self.parse(self.default) if text != self.default else text

    def format(self, value: Any) -> str:
        t = self.type
        if t == 3:
            return str(value)
        if t == 5:
            return "1" if value else "0"
        if t in (2, 9):
            v = float(value)
            s = "%.17g" % v
            return s
        return str(int(value))

    @property
    def default_value(self) -> Any:
        return self.parse(self.default)


def _labels():
    """Label lookups: English from mlaser's catalogue, Chinese from an imported original install
    (empty when none was imported). Option values that are not keys (``"1"``, ``"English"``)
    are returned as they are."""
    from mlaser.i18n import catalogue
    from mlaser.i18n.importer import imported
    cat = catalogue("en")
    zh_map = imported().get("zh", {}).get("legacy_strings", {})

    def en(key):
        if not key:
            return ""
        t = cat.text(key)
        return t if t is not None else key

    def zh(key):
        return zh_map.get(key, "") if key else ""
    return en, zh


@lru_cache(maxsize=1)
def param_defs() -> tuple[ParamDef, ...]:
    raw = json.loads(DATA_FILE.read_text("utf-8"))
    en, zh = _labels()
    out = []
    for r in raw:
        opts = tuple((o.get("lang"), en(o.get("lang")), zh(o.get("lang"))) for o in r.get("options", []) or [])
        out.append(ParamDef(r["file"], r["table"], r["index"], r["category"] or "", r["key"] or "",
                            r["lang"] or "", en(r["lang"]), zh(r["lang"]), r["type"] or 0,
                            r["type_name"], r["default"] if r["default"] is not None else "",
                            r["unit"] or "", r["min"] or 0.0, r["max"] or 0.0, r["cond"] or "",
                            r["flag"], r["struct_offset"], opts))
    return tuple(out)


def defs_for(table: str) -> list[ParamDef]:
    return [d for d in param_defs() if d.table == table]


def layer_defs(co2: bool = False) -> list[ParamDef]:
    return defs_for("co2layer" if co2 else "layer")


def layer_category(i: int, co2: bool = False) -> str:
    """0-based layer index -> XML category (``FUN_006354c0``)."""
    return ("CO2LayerParam%d" if co2 else "LayerParam%d") % (i + 1)


# --------------------------------------------------------------------------- XML store
class ParamStore:
    """In-memory view of one ParameterRoot XML file: {full dotted path: text}."""

    def __init__(self, values: Optional[dict[str, str]] = None):
        self.values: dict[str, str] = dict(values or {})

    @staticmethod
    def full_key(category: str, key: str) -> str:
        return KEY_PREFIX + category + "." + key

    # ---- raw access
    def get_raw(self, category: str, key: str) -> Optional[str]:
        return self.values.get(self.full_key(category, key))

    def set_raw(self, category: str, key: str, text: str) -> None:
        self.values[self.full_key(category, key)] = text

    # ---- typed access through descriptors
    def get(self, d: ParamDef, category: Optional[str] = None) -> Any:
        return d.parse(self.get_raw(category or d.category, d.key))

    def set(self, d: ParamDef, value: Any, category: Optional[str] = None) -> None:
        self.set_raw(category or d.category, d.key, d.format(value))

    def fill_defaults(self, defs: Iterable[ParamDef], category: Optional[str] = None) -> None:
        for d in defs:
            if "." not in d.key:
                continue
            fk = self.full_key(category or d.category, d.key)
            self.values.setdefault(fk, d.default if d.default != 'L""' else "")

    # ---- file IO
    @classmethod
    def loads(cls, data: bytes) -> "ParamStore":
        root = ET.fromstring(data)
        vals: dict[str, str] = {}

        def walk(el, path):
            for k, v in el.attrib.items():
                vals[".".join(path + [k])] = v
            for ch in el:
                walk(ch, path + [ch.tag])

        for ch in root:
            walk(ch, [ch.tag])
        return cls(vals)

    @classmethod
    def load(cls, path: PathLike) -> "ParamStore":
        return cls.loads(Path(path).read_bytes())

    def dumps(self) -> bytes:
        root = ET.Element(ROOT_TAG)
        index: dict[tuple, ET.Element] = {}
        for full, text in self.values.items():
            parts = full.split(".")
            node = root
            for i in range(len(parts) - 1):
                k = tuple(parts[: i + 1])
                if k not in index:
                    index[k] = ET.SubElement(node, parts[i])
                node = index[k]
            node.set(parts[-1], text)
        ET.indent(root, space="\t")
        return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"

    def save(self, path: PathLike) -> None:
        """Write via ``<path>.tmp`` + rename, like ``FUN_1000e360``."""
        p = Path(path)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(self.dumps())
        os.replace(tmp, p)


# --------------------------------------------------------------------------- machine set
@dataclass
class MachineParams:
    """The three ParaModule slots."""
    hard: ParamStore = field(default_factory=ParamStore)
    manu: ParamStore = field(default_factory=ParamStore)
    layer: ParamStore = field(default_factory=ParamStore)

    @classmethod
    def defaults(cls) -> "MachineParams":
        m = cls()
        m.apply_defaults()
        return m

    def apply_defaults(self) -> None:
        for d in param_defs():
            if d.file == "HardPara.xml":
                self.hard.fill_defaults([d])
            elif d.file == "ManuPara.xml":
                self.manu.fill_defaults([d])
        for i in range(N_LAYERS):
            self.layer.fill_defaults(layer_defs(), layer_category(i))
            self.layer.fill_defaults(layer_defs(True), layer_category(i, True))

    @classmethod
    def load_dir(cls, dat_dir: PathLike, fill_defaults: bool = True) -> "MachineParams":
        d = Path(dat_dir)
        legacy = d / "SystemPara.xml"
        stores = []
        for name in FILES:
            p = d / name
            if not p.exists() and legacy.exists() and not (d / "HardPara.xml").exists():
                p = legacy
            stores.append(ParamStore.load(p) if p.exists() else ParamStore())
        m = cls(*stores)
        if fill_defaults:
            m.apply_defaults()
        return m

    def save_dir(self, dat_dir: PathLike) -> None:
        d = Path(dat_dir)
        d.mkdir(parents=True, exist_ok=True)
        for name, st in zip(FILES, (self.hard, self.manu, self.layer)):
            st.save(d / name)

    # ---- layers
    def layer_params(self, i: int, co2: bool = False) -> LayerParams:
        return layer_params_from_store(self.layer, i, co2)

    def set_layer_params(self, lp: LayerParams, co2: bool = False) -> None:
        layer_params_to_store(self.layer, lp, co2)


def _defmap(co2: bool) -> dict[str, ParamDef]:
    return {d.key: d for d in layer_defs(co2)}


# Model-field mapping (INFERRED semantics, see docs/re/formats.md):
#   speed <- GP.CutSpeed (mm/s), power_pct <- GP.CutPeakCurrent (laser power %, fiber) /
#   GP.CutDuty (CO2), duty_pct <- GP.CutPower (PWM duty %, fiber) / GP.CutDuty (CO2),
#   frequency_hz <- GP.CutFreq, gas <- GP.CutGasType, gas_pressure <- GP.CutAirPressure,
#   cut_height <- GP.CutHeight
_STAGE_KEYS = ("DrillHeight", "DrillFocusPos", "EnableFocusGradual", "FocusGradualEndPos",
               "FocusGradualTime", "DrillPower", "DrillFreq", "DrillPeakCurrent", "DrillGasType",
               "DrillGasPressure", "DrillDelay", "EnableGradualDrill", "GradualTime",
               "BeforeLaserOffDelay", "AfterLaserOffDelay")


def layer_params_from_store(st: ParamStore, i: int, co2: bool = False) -> LayerParams:
    cat = layer_category(i, co2)
    dm = _defmap(co2)
    vals = {k: st.get(d, cat) for k, d in dm.items() if "." in k}
    g = lambda k, dv=None: vals.get(k, dv)
    lp = LayerParams(index=i)
    lp.speed = float(g("GP.CutSpeed", 100.0))
    if co2:
        lp.power_pct = float(g("GP.CutDuty", 100))
        lp.duty_pct = float(g("GP.CutDuty", 100))
    else:
        lp.power_pct = float(g("GP.CutPeakCurrent", 100))
        lp.duty_pct = float(g("GP.CutPower", 100))
    lp.frequency_hz = float(g("GP.CutFreq", 5000))
    lp.gas = int(g("GP.CutGasType", 0))
    lp.gas_pressure = float(g("GP.CutAirPressure", 5.0))
    lp.cut_height = float(g("GP.CutHeight", 1.0))
    stages = []
    for s in range(5):
        st_d = {k: vals.get(f"GP.{k}{s}") for k in _STAGE_KEYS if f"GP.{k}{s}" in vals}
        if s >= 1 and f"GP.BoltDrill_Enable_{s}" in vals:
            st_d["BoltDrill_Enable"] = vals[f"GP.BoltDrill_Enable_{s}"]
            st_d["BoltDrill_Power"] = vals.get(f"GP.BoltDrill_Power_{s}")
            st_d["BoltDrill_Freq"] = vals.get(f"GP.BoltDrill_Freq_{s}")
        if st_d:
            stages.append(st_d)
    lp.pierce = {"type": vals.get("GP.ManuType", 0), "stages": stages}
    lp.extra = {"category": cat, "co2": co2, "values": vals}
    return lp


def layer_params_to_store(st: ParamStore, lp: LayerParams, co2: bool = False) -> None:
    cat = layer_category(lp.index, co2)
    dm = _defmap(co2)
    vals = dict((lp.extra or {}).get("values", {}))
    vals["GP.CutSpeed"] = lp.speed
    if co2:
        vals["GP.CutDuty"] = lp.duty_pct
    else:
        vals["GP.CutPeakCurrent"] = lp.power_pct
        vals["GP.CutPower"] = lp.duty_pct
    vals["GP.CutFreq"] = lp.frequency_hz
    vals["GP.CutGasType"] = lp.gas
    vals["GP.CutAirPressure"] = lp.gas_pressure
    vals["GP.CutHeight"] = lp.cut_height
    pierce = lp.pierce or {}
    if "type" in pierce:
        vals["GP.ManuType"] = pierce["type"]
    for s, sd in enumerate(pierce.get("stages", [])):
        for k, v in sd.items():
            if k.startswith("BoltDrill_"):
                vals[f"GP.{k}_{s}"] = v
            else:
                vals[f"GP.{k}{s}"] = v
    for k, v in vals.items():
        d = dm.get(k)
        if d is not None and v is not None:
            st.set(d, v, cat)


def load_layer_params(path: PathLike, co2: bool = False) -> dict[int, LayerParams]:
    """Read LayerPara.xml (or a craft file) and return {layer index: LayerParams}."""
    st = ParamStore.load(path)
    return {i: layer_params_from_store(st, i, co2) for i in range(N_LAYERS)}


def save_layer_params(layers: dict[int, LayerParams], path: PathLike, co2: bool = False,
                      base: Optional[ParamStore] = None) -> None:
    st = base or ParamStore()
    for i in range(N_LAYERS):
        st.fill_defaults(layer_defs(co2), layer_category(i, co2))
    for lp in layers.values():
        layer_params_to_store(st, lp, co2)
    st.save(path)
