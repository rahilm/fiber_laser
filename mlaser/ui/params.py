"""Parameter registry recovered from MainApp.exe and helpers to map it onto the model.

``resources/params.json`` lists ``{name, key, section, hint}`` for ~860 settings: ``name`` is
the ini/xml key used by the original (``GP.CutSpeed``, ``MC.JogFastSpeed``, ``SP.Language``...),
``key`` the lang.txt label key whose English text is ``Group.Label`` (property-grid
category + caption, e.g. ``pd125`` "Cut Basic.Cut Speed"), ``section`` the owning parameter
file/section (``LayerParam``, ``ManuParam``, ``GraphParam``, ``SoftParam``, ``DIParam``...).
``hint`` holds neighbouring literals (unit / default / max) and is NOT verified.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from .i18n import RES_DIR, Translator

# Which LayerParams attribute a GP.* layer parameter maps to (model.LayerParams).
LAYER_FIELD_MAP = {
    "GP.CutSpeed": "speed",
    "GP.CutPower": "power_pct",
    "GP.CutFreq": "frequency_hz",
    "GP.CutGasType": "gas",
    "GP.CutAirPressure": "gas_pressure",
    "GP.CutHeight": "cut_height",
}

# Sections offered in the "Options"/"Hardware" dialogs (others are internal).
OPTION_SECTIONS = {
    "app.options": ["SoftParam", "GraphParam", "ImportGraphParam", "ManuParam"],
    "hw.options": ["AxisParam", "MachineAxisConfig_0", "MachineAxisConfig", "HomeParam", "LaserParam",
                   "GasParam", "ZFParam", "AFParam", "FCParam", "DAParam", "DIParam", "DOParam"],
}

GAS_KEYS = ["mp43", "mp44", "mp45", "mp46", "mp47", "mp48"]   # Air, O2, N2, High Air, High O2, High N2


@dataclass(frozen=True)
class ParamDef:
    name: str
    key: str
    section: str
    hint: tuple

    def is_bool(self) -> bool:
        n = self.name.split(".", 1)[-1]
        return n.startswith(("Enable", "Is", "No", "Show")) or n in (
            "WithFilm", "PreDrill", "SlowStart", "NoManu", "NoFollow")


@lru_cache(maxsize=1)
def registry() -> tuple[ParamDef, ...]:
    path = os.path.join(RES_DIR, "params.json")
    try:
        data = json.load(open(path, encoding="utf-8"))["params"]
    except Exception:
        return ()
    return tuple(ParamDef(p["name"], p["key"], p["section"], tuple(p.get("hint", ()))) for p in data)


def section(name: str) -> list[ParamDef]:
    return [p for p in registry() if p.section == name]


def grouped(defs: list[ParamDef], tr: Translator) -> dict[str, list[tuple[ParamDef, str]]]:
    """Group definitions by the property-grid category in their label (``Group.Label``)."""
    out: dict[str, list[tuple[ParamDef, str]]] = {}
    for d in defs:
        g, label = tr.tr_param(d.key)
        out.setdefault(g or "-", []).append((d, label))
    return out
