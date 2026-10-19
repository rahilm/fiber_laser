"""The parameter sections of the option dialogs, over the ConfigService schema.

Every entry is a :class:`mlaser.services.config.Setting` generated from the 1,129-descriptor
registry (``formats/data/mlaser_params.json``): key (``GP.CutSpeed``), label key (``pd125``,
whose catalogue text is ``Group.Label``), section (the category: ``ManuParam``, ``AxisParam``
...), type, unit, range and options.  The separate ``ParamDef`` view this module used to build
(and the scraped ``resources/params.json`` before it) are gone: there is one schema.
"""
from __future__ import annotations

from mlaser.services.config import Setting, registry_settings

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


def registry() -> tuple[Setting, ...]:
    return tuple(s for s in registry_settings().values() if s.label and s.section)


def section(name: str) -> list[Setting]:
    return [s for s in registry() if s.section == name]


def grouped(defs: list[Setting], tr: Translator) -> dict[str, list[tuple[Setting, str]]]:
    """Group settings by the property-grid category in their label (``Group.Label``)."""
    out: dict[str, list[tuple[Setting, str]]] = {}
    for d in defs:
        g, label = tr.tr_param(d.label)
        out.setdefault(g or "-", []).append((d, label))
    return out
