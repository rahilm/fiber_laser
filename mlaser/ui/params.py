"""Parameter registry for the option dialogs, and helpers to map it onto the model.

The registry is :func:`mlaser.formats.params.param_defs` (1129 descriptors recovered from
MainApp.exe's parameter tables: XML key, type, default, unit, range, owning category). Each
:class:`ParamDef` here exposes ``name`` (the ini/xml key, ``GP.CutSpeed``, ``SP.Language``...),
``key`` (the label key, whose catalogue text is ``Group.Label``, e.g. ``pd125`` "Cut Basic.Cut
Speed"), ``section`` (the category: ``ManuParam``, ``GraphParam``, ``SoftParam``, ``DIParam``...)
and ``hint`` (default, unit, min, max).

Earlier builds read a separate list scraped from MainApp.exe string literals
(``resources/params.json``); it was a less accurate subset of the descriptor tables and was
removed (docs/ASSETS.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from mlaser.formats.params import param_defs

from .i18n import Translator

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
    type_name: str = ""

    def is_bool(self) -> bool:
        if self.type_name:
            return self.type_name == "bool"
        n = self.name.split(".", 1)[-1]
        return n.startswith(("Enable", "Is", "No", "Show")) or n in (
            "WithFilm", "PreDrill", "SlowStart", "NoManu", "NoFollow")


@lru_cache(maxsize=1)
def registry() -> tuple[ParamDef, ...]:
    out = []
    for d in param_defs():
        if not d.key or not d.category:
            continue
        unit = d.unit if d.unit not in ("", 'L""') else ""
        out.append(ParamDef(d.key, d.lang, d.category, (d.default, unit, str(d.min), str(d.max)), d.type_name))
    return tuple(out)


def section(name: str) -> list[ParamDef]:
    return [p for p in registry() if p.section == name]


def grouped(defs: list[ParamDef], tr: Translator) -> dict[str, list[tuple[ParamDef, str]]]:
    """Group definitions by the property-grid category in their label (``Group.Label``)."""
    out: dict[str, list[tuple[ParamDef, str]]] = {}
    for d in defs:
        g, label = tr.tr_param(d.key)
        out.setdefault(g or "-", []).append((d, label))
    return out
