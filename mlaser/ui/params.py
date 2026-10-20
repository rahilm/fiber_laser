"""The parameter sections of the option dialogs, over the ConfigService schema.

Every entry is a :class:`mlaser.services.config.Setting` generated from the 1,129-descriptor
registry (``formats/data/mlaser_params.json``): key (``GP.CutSpeed``), label key (``pd125``,
whose catalogue text is ``Group.Label``), section (the category: ``ManuParam``, ``AxisParam``
...), type, unit, range and options.  The separate ``ParamDef`` view this module used to build
(and the scraped ``resources/params.json`` before it) are gone: there is one schema.
"""
from __future__ import annotations

from mlaser.services.config import Setting, registry_settings
from mlaser.services.technology import CORE_FIELDS as LAYER_FIELD_MAP  # noqa: F401 - re-exported

from .i18n import Translator

# Which LayerParams attribute a GP.* layer parameter maps to (model.LayerParams): the mapping the
# job compiler uses (parity_ports.mainapp.layer.LayerView): duty_pct = GP.CutPower (PWM duty),
# power_pct = GP.CutPeakCurrent (laser power).  It used to map GP.CutPower to power_pct, so a
# "Cut Power" typed in the dialog became the peak current.

# Sections offered in the Machine settings dialog (``hw.options``; others are internal).  The
# original's Options tables (SoftParam, GraphParam, ImportGraphParam, ManuParam) are no longer a
# dialog: ``app.options`` opens the Settings window (mlaser.ui.settings_dialog), and
# docs/dev/settings-audit.md decides each of their parameters.  The machine-side ones that the
# controller and the job compiler read moved here, into the virtual section ``MachineControl``.
OPTION_SECTIONS = {
    "hw.options": ["AxisParam", "MachineAxisConfig_0", "MachineAxisConfig", "HomeParam", "LaserParam",
                   "GasParam", "ZFParam", "AFParam", "FCParam", "DAParam", "DIParam", "DOParam",
                   "MachineControl"],
}

#: the original Options parameters that belong to the machine (read by the controller, the
#: process sequencer or the job compiler): their one home is Machine settings
MACHINE_CONTROL: tuple[str, ...] = (
    # process control (mlaser.nc.process / nc.machine)
    "GC.GasDelay", "GC.DirectGasDelay", "GC.ChangeGasDelay", "MGP.iProportionalOpenSleep",
    "FC.ShortNoUpMaxLength", "FC.EnableEmtptMoveFollow", "MP.DirectDrillMaxHeight",
    "MP.EnableManuCrashProtect", "MP.ManuCrashProtectUpHeight", "MP.EnableManuCrashProtectMinHeight",
    "MP.OilDOPort", "MC.EnableAdvMicoLink", "MC.AdvMicLinkPower_Pre", "GP.MicoLinkSlowDownVel",
    "GP.IsDrillInMicoLink", "GP.EnableMicroLinkDecc", "MP.EnableSmallCircleSpeedLimit",
    "MP.SmallCircleSpeedLimitRatio",
    # manual motion (controller)
    "MC.JogFastSpeed", "MC.JogSlowSpeed", "MC.XFastMoveSpeed", "MC.XFastMoveAcc", "MP.JogStopDccFactor",
    "MC.IsFastMode", "SP.LimitDeccFactor",
    # laser point / outputs (controller start-up and I/O)
    "LC.PtLaserFreq", "LC.PtLaserPeakCurrent", "MC.LaserPointPower", "MC.PtLaserTime_ms",
    "MP.LaserDAKeepOutput", "MP.PWMOutputSync", "MP.CO2PWMOutputSync",
    # limits (the guard checks the travel regardless: docs/SAFETY.md)
    "MS.EnableSoftLimit", "MP.CO2EnableSecondSoftLimit", "MP.SecondSoftLimitOffsetX", "MP.SecondSoftLimitOffsetY",
    # devices
    "AF.AFType", "MP.RotatePlatformType",
    # edge seek (mlaser.controller.edgeseek)
    "GRP.EdgeSeekSensitivity", "GRP.EdgeSeekFollowHeight", "GRP.EdgeSeekUpHeight",
    "GRP.SlowEdgeSeekMoveOutTolerance", "GRP.EdgeSeekXYSlowSpeed", "GRP.EdgeSeekOutSpeed",
    "GRP.EdgeBoardSizeX", "GRP.EdgeBoardSizeY", "GRP.EdgeAutoBoardSize", "GRP.EdgeSeekXPercent",
    "GRP.EdgeSeekYPercent", "GRP.EdgeOffsetX", "GRP.EdgeOffsetY", "GRP.EdgeSeekMode",
    "GRP.EdgeSeekIsCurrentStart", "GRP.EdgeSeekStartPointX", "GRP.EdgeSeekStartPointY",
)

GAS_KEYS = ["mp43", "mp44", "mp45", "mp46", "mp47", "mp48"]   # Air, O2, N2, High Air, High O2, High N2


def registry() -> tuple[Setting, ...]:
    return tuple(s for s in registry_settings().values() if s.label and s.section)


def section(name: str) -> list[Setting]:
    if name == "MachineControl":
        reg = registry_settings()
        return [reg[k] for k in MACHINE_CONTROL if k in reg]
    return [s for s in registry() if s.section == name]


def hw_section(config, name: str) -> list[Setting]:
    """A Machine settings section of ``config``'s schema (``MachineControl`` is virtual)."""
    if name == "MachineControl":
        return [config.schema[k] for k in MACHINE_CONTROL if k in config.schema]
    return config.section(name)


def grouped(defs: list[Setting], tr: Translator) -> dict[str, list[tuple[Setting, str]]]:
    """Group settings by the property-grid category in their label (``Group.Label``)."""
    out: dict[str, list[tuple[Setting, str]]] = {}
    for d in defs:
        g, label = tr.tr_param(d.label)
        out.setdefault(g or "-", []).append((d, label))
    return out
