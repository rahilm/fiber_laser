"""ConfigService: one typed configuration store for the application.

Before Wave 2 configuration lived in about nine places (architecture.md §1.8): the parameter
registry, a free-text ``window.settings`` dict that only some dialogs and extensions read (and
that the controller and NC compiler never saw), ``nc.Machine()`` always built with defaults,
ad-hoc dicts (``mj_params``, lead state, ``ext_machine.values``), ``diag_state.json`` and several
app-data directory schemes.  This service replaces them:

* **Schema generated from the registry.**  Every entry of ``formats/data/mlaser_params.json``
  (1,129 descriptors recovered from MainApp.exe) becomes a :class:`Setting` with its type,
  unit, range, options and default; application and plugin settings are registered the same
  way (:meth:`ConfigService.register`).  :meth:`ConfigService.set` validates.
* **One store of explicit values** (``values``): what the user set.  Everything else reads the
  schema default, so an untouched installation behaves exactly as before.
* **Persistence** under platformdirs (``config.json`` in the user config directory;
  ``MLASER_CONFIG_DIR`` / ``MLASER_DATA_DIR`` override it, tests use a temporary directory),
  plus import/export of the original's ``HardPara.xml`` / ``ManuPara.xml`` / ``LayerPara.xml``
  through :mod:`mlaser.formats.params`.
* **Machine profile.**  :meth:`ConfigService.machine` builds the :class:`mlaser.nc.Machine`
  the job compiler, the motion planner (``MotionConfig``) and the process sequencer
  (``ProcessConfig``) use: the rebuild's bench machine with every parameter the user changed
  applied (``profile = "bench"``, default), or the machine described entirely by the parameter
  set (``profile = "params"``, chosen automatically after importing a ``dat`` directory).

Qt-free.  :meth:`ConfigService.mapping` is a ``MutableMapping`` view used where code expects the
old ``settings`` dict (diag ``ParamSet``, extensions).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
from collections.abc import MutableMapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from mlaser._atomic import atomic_write, atomic_write_text

MISSING: Any = object()
log = logging.getLogger("mlaser.config")

#: the ``config.json`` format this build writes and reads.  A file with a higher number comes
#: from a newer mlaser: it is refused (kept, never overwritten unasked; the start-up notice),
#: not loaded with its unknown parts dropped.  A lower one is brought up to date by
#: :data:`MIGRATIONS` before its values are read.
CONFIG_VERSION = 1

#: ``{n: fn}``: ``fn(data)`` turns the parsed file of format ``n`` into format ``n + 1`` (a new
#: dict).  Add one entry with every increase of :data:`CONFIG_VERSION`; a format with no path
#: up to the current one is refused like a damaged file.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class ConfigLoadError(ValueError):
    """``config.json`` exists but cannot be used (see :meth:`ConfigService.load`); ``kind``:
    ``unreadable`` (the read failed), ``damaged`` (not JSON, not UTF-8, or not the expected
    shape) or ``newer`` (the format of a newer mlaser)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ConfigWriteHeld(OSError):
    """A write of ``config.json`` refused while a load error is not acknowledged (the file
    that could not be loaded is never replaced unasked)."""


def migrate_config(data: dict, version: int) -> dict:
    """``data`` of format ``version`` brought to :data:`CONFIG_VERSION` through
    :data:`MIGRATIONS`; :class:`ConfigLoadError` when there is no path."""
    while version < CONFIG_VERSION:
        fn = MIGRATIONS.get(version)
        if fn is None:
            raise ConfigLoadError("damaged", f"no migration from configuration format {version}")
        data = fn(data)
        version += 1
    return data


def parse_config(raw: bytes) -> tuple[tuple[str, dict], dict]:
    """``(profile, values)`` of a ``config.json``'s bytes, migrated to the current format;
    :class:`ConfigLoadError` (``damaged`` / ``newer``) when the file cannot be used.  The
    values are not validated here (an out-of-range value falls back to its default)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigLoadError("damaged", f"{type(exc).__name__}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigLoadError("damaged", f"the file holds a JSON {type(data).__name__}, not an object")
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ConfigLoadError("damaged", f"format version {version!r} is not a positive whole number")
    if version > CONFIG_VERSION:
        raise ConfigLoadError("newer", f"format version {version} (this version of mlaser reads up "
                                       f"to {CONFIG_VERSION})")
    data = migrate_config(data, version)
    prof = data.get("profile")
    prof = {} if prof is None else prof
    values = data.get("values")
    values = {} if values is None else values
    if not isinstance(prof, dict) or not isinstance(values, dict):
        raise ConfigLoadError("damaged", "\"profile\" and \"values\" must be JSON objects")
    base = prof.get("base", "bench")
    overrides = prof.get("overrides")
    overrides = {} if overrides is None else overrides
    if base not in ("bench", "params") or not isinstance(overrides, dict):
        raise ConfigLoadError("damaged", f"unknown machine profile {base!r}")
    return (str(base), dict(overrides)), values

# registry type -> setting kind
_KIND = {
    "int": "int",
    "double": "float",
    "pulse_equiv": "float",
    "string": "str",
    "enum": "enum",
    "bool": "bool",
    "do_port": "int",
    "di_port": "int",
    "ipv4": "str",
    "da_port": "int",
    "gas_ratio": "int",
    "do_port_ext": "int",
    "serial_port": "int",
    "color": "int",
}


@dataclass(frozen=True)
class Setting:
    """One typed configuration entry."""

    key: str
    kind: str = "str"  # int | float | bool | str | enum | json
    default: Any = None
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    options: tuple = ()  # enum: labels (index = value)
    label: str = ""  # lang key or text
    section: str = ""  # registry category (ManuParam, AxisParam...) or namespace
    file: Optional[str] = None  # HardPara.xml / ManuPara.xml / LayerPara.xml / None
    source: str = "registry"  # registry | app | plugin:<name>
    #: the one place the value is edited (docs/dev/settings-audit.md, the IA settings map):
    #: settings | machine | process | nest | run | hidden ("" = derived by :func:`home_of`)
    home: str = ""
    #: the rebuild reads it (registry rows: :data:`mlaser.services.settings_homes.LIVE_KEYS`)
    live: bool = True

    def validate(self, value: Any) -> Any:
        """``value`` converted to this setting's type; ValueError when it does not fit."""
        k = self.kind
        if k == "json":
            return value
        if k == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on"):
                    return True
                if v in ("0", "false", "no", "off", ""):
                    return False
                raise ValueError(f"{self.key}: not a boolean: {value!r}")
            return bool(value)
        if k == "str":
            return "" if value is None else str(value)
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.key}: not a number: {value!r}") from None
        if num != num:
            raise ValueError(f"{self.key}: NaN")
        if k in ("int", "enum"):
            if num != int(num):
                raise ValueError(f"{self.key}: not an integer: {value!r}")
            out: Any = int(num)
        else:
            out = num
        lo, hi = self.min, self.max
        if k == "enum" and self.options:
            lo, hi = 0, len(self.options) - 1
        if lo is not None and hi is not None and hi > lo and not (lo <= out <= hi):
            raise ValueError(f"{self.key}: {out} outside [{lo:g}, {hi:g}] {self.unit}".rstrip())
        return out


_XML_KEYS: dict[tuple[str, str], str] = {}  # (registry category, key) -> setting key


#: settings that shared one label id with another setting of the same window (the original
#: reused a lang key: Match code 1 three times, the fiber and CO2 laser ports, the edge-seek
#: values under one "compensates distance" label...): each gets its own label (UI polish)
LABEL_FIXES: dict[str, str] = {
    "SOP.JoystickID4": "param.remote_control.match_code_4",
    "SOP.JoystickID5": "param.remote_control.match_code_5",
    "LGP.CO2LaserDAPort": "param.da.co2_da_port",
    "LGP.CO2LaserDAType": "param.da.co2_da_range",
    "LGP.CO2DORemoteStart": "param.io.co2_remote_key",
    "LGP.CO2DOLaserGate": "param.io.co2_shutter",
    "LGP.CO2DOLaser": "param.io.co2_laser_emission",
    "LGP.CO2DORedLight": "param.io.co2_red_light",
    "LGP.CO2LaserControlType": "param.general.co2_control_type",
    "EC.ECIOType": "param.general.ec_io_type",
    "LGP.LaserSerialPort": "param.pc_serial.laser_port_number",
    "AF.SerialPort": "param.pc_serial.af_port_number",
    "LGP.LaserSerialBaudRate": "param.pc_serial.laser_baud_rate",
    "AF.SerialBaudRate": "param.pc_serial.af_baud_rate",
    "MP.EnableVerCorrect": "param.precision_compensate.enable_squareness",
    "MP.VerCorrectType": "param.precision_compensate.squareness_type",
    "MP.VerCorrectABLength": "param.precision_compensate.squareness_ab",
    "MP.VerCorrectACLength": "param.precision_compensate.squareness_ac",
    "MP.VerCorrectL1Length": "param.precision_compensate.squareness_l1",
    "MP.VerCorrectL2Length": "param.precision_compensate.squareness_l2",
    "DI.AlarmDIStr": "param.di.alarm_inputs",
    "MSC.TestCardID": "param.misc.test_card_id",
    "MSC.TestPersonID": "param.misc.test_person_id",
    "MSC.DockPtType": "param.misc.park_point_type",
    "MSC.ToolCoordPtStr": "param.misc.tool_coordinate_points",
    "ZF.ZFFastJogSpeed": "param.zf_run_property.fast_jog_speed",
    "GRP.EdgeBoardSizeX": "param.normal_axis.edge_sheet_size_x",
    "GRP.EdgeSeekStartPointX": "param.normal_axis.edge_seek_start_x",
    "GRP.EdgeBoardSizeY": "param.normal_axis.edge_sheet_size_y",
    "GRP.EdgeSeekMode": "param.normal_axis.edge_seek_mode",
    "GRP.EdgeSeekStartPointY": "param.normal_axis.edge_seek_start_y",
    "GRP.EdgeAutoBoardSize": "param.z_follower.edge_auto_sheet_size",
    "GRP.EdgeSeekIsCurrentStart": "param.z_follower.edge_seek_from_current",
    "GP.FocusGradualTime2": "param.third_pierce_basic.focus_gradual_time",
    "GP.LayerFileName": "param.misc.layer_file_name",
    "GP.PowerCurveSmoothType": "param.misc.power_curve_smoothing",
    "GP.FreqCurveSmoothType": "param.misc.frequency_curve_smoothing",
    "CO2LayerParam:GP.LayerFileName": "param.misc.layer_file_name",
    "CO2LayerParam:GP.PowerCurveSmoothType": "param.misc.power_curve_smoothing",
    "CO2LayerParam:GP.FreqCurveSmoothType": "param.misc.frequency_curve_smoothing",
}


def _registry_settings() -> dict[str, Setting]:
    """One Setting per registry entry.  The key is the parameter name (``GC.GasDelay``); the 13
    names that occur in two categories get ``"<category>:<name>"`` for the second one."""
    from mlaser.formats.params import param_defs

    out: dict[str, Setting] = {}
    for d in param_defs():
        if "." not in d.key:  # struct sub-records (no own value)
            continue
        key = d.key if d.key not in out else f"{d.category}:{d.key}"
        if key in out:
            continue
        _XML_KEYS[(d.category, d.key)] = key
        kind = _KIND.get(d.type_name, "str")
        default = d.default_value
        if kind == "str" and default == 'L""':
            default = ""
        unit = d.unit if d.unit not in ('L""',) else ""
        has_range = d.max > d.min
        s = Setting(
            key,
            kind,
            default,
            unit,
            d.min if has_range else None,
            d.max if has_range else None,
            tuple(o[0] or o[1] for o in d.options),
            LABEL_FIXES.get(key, d.lang),
            d.category,
            d.file,
        )
        out[key] = replace(s, home=home_of(s), live=key.split(":", 1)[-1] in _live_keys())
    return out


def _live_keys() -> frozenset:
    from .settings_homes import LIVE_KEYS
    return LIVE_KEYS


#: card / hardware categories (Machine › Setup)
CARD_SECTIONS = frozenset({"AxisParam", "MachineAxisConfig_0", "MachineAxisConfig_1", "MachineAxisConfig_2",
                           "MachineAxisConfig_3", "MachineAxisConfig_4", "MachineAxisConfig", "HomeParam",
                           "LaserParam", "GasParam", "ZFParam", "AFParam", "FCParam", "DAParam", "DIParam",
                           "DOParam", "ECParam"})
#: the original's Options rows that are machine behaviour or machine tools (Machine › Setup)
_MACHINE_PREFIXES = ("MC.Jog", "MC.XFastMove", "MC.LaserPointPower", "MC.PtLaserTime", "MC.IsFastMode",
                     "MC.MarkPt", "MC.Maintain", "MC.BatchCut", "MC.EnableAdvMicoLink", "MC.AdvMicLink",
                     "LC.", "GC.", "FC.", "MGP.", "MS.", "MSC.", "MP.", "GP.Mico", "GP.IsDrillInMicoLink",
                     "GP.EnableMicroLinkDecc", "GRP.Edge", "GRP.SlowEdge", "GRP.FastEdge", "GRP.BallArm",
                     "GRP.BatchCut", "GRP.EnableBatchCut", "GRP.EnableDIInput", "SP.Interfere", "SP.LimitDecc",
                     "SP.m_iEnableLaserType", "AF.", "EC.")
#: application keys whose home is not the Settings window
_APP_HOMES = (("safety.", "machine"), ("bed.", "machine"), ("plugin.ext_process.machine_profile", "machine"),
              ("plugin.ext_options.parity_profile", "machine"), ("costing.laser_rated_w", "machine"),
              ("nest.", "nest"), ("plugins.", "hidden"),
              ("shell.pin.", "hidden"), ("shell.user_mode", "hidden"), ("shell.layout", "hidden"),
              ("shell.view_state", "hidden"), ("shell.canvas_hud", "hidden"), ("shell.view_tabs", "hidden"),
              ("shell.workspace", "hidden"), ("shell.recent_commands", "hidden"), ("shell.welcome_done", "hidden"),
              ("app.last_file", "hidden"), ("settings.", "hidden"), ("plugin.ext_practice.", "hidden"))


def home_of(s: "Setting") -> str:
    """Where a setting is edited (its one home).  Registry rows: card rows and the machine
    behaviour rows of the original's Options go to Machine › Setup, layer rows to Process, nest
    rows to Nest, the rest of the original's Options is hidden (kept for the files only).
    Application and plugin keys: the Settings window unless listed in :data:`_APP_HOMES`; JSON
    state is hidden."""
    if s.home:
        return s.home
    k = s.key.split(":", 1)[-1]
    if s.source == "registry":
        if s.section in CARD_SECTIONS or (s.file == "HardPara.xml") or k.startswith(_MACHINE_PREFIXES):
            return "machine"
        if s.section in ("LayerParam", "CO2LayerParam"):
            return "process"
        if s.section == "NestParam":
            return "nest"
        return "hidden"
    for prefix, home in _APP_HOMES:
        if s.key.startswith(prefix):
            return home
    return "hidden" if s.kind == "json" else "settings"


_REGISTRY_CACHE: dict[str, Setting] = {}


def registry_settings() -> dict[str, Setting]:
    """The registry part of the schema (cached; shared by every ConfigService)."""
    if not _REGISTRY_CACHE:
        _REGISTRY_CACHE.update(_registry_settings())
    return _REGISTRY_CACHE


# --------------------------------------------------------------------------- paths
def _test_run() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def config_dir() -> Path:
    """Directory of ``config.json``: ``$MLASER_CONFIG_DIR``, a per-process temp directory under
    pytest, else the platform's user config directory (platformdirs)."""
    d = os.environ.get("MLASER_CONFIG_DIR")
    if d:
        return Path(d)
    if _test_run():
        return Path(tempfile.gettempdir()) / f"mlaser-test-{os.getpid()}" / "config"
    import platformdirs

    return Path(platformdirs.user_config_dir("mlaser", appauthor=False))


def data_dir() -> Path:
    """Directory for logs, reports, counters and backups: ``$MLASER_DATA_DIR``, a per-process temp
    directory under pytest, else the platform's user data directory (platformdirs).  Replaces
    the ``~/.mlaser`` / ``%LOCALAPPDATA%/NexCut`` schemes (architecture.md §1.8 #9)."""
    d = os.environ.get("MLASER_DATA_DIR")
    if d:
        return Path(d)
    if _test_run():
        return Path(tempfile.gettempdir()) / f"mlaser-test-{os.getpid()}"
    import platformdirs

    return Path(platformdirs.user_data_dir("mlaser", appauthor=False))


# --------------------------------------------------------------------------- safety I/O
#: card input numbers are 1-based (``MCStatus.input``: 1..12 RO[4], 13..28 RO[21]); outputs
#: 1..26 (``set_output``: 1..10 sub 2, 11..26 sub 13).  0 = not wired / off.
MAX_INPUT = 28
MAX_OUTPUT = 26


def _safety_settings() -> list[Setting]:
    """The interlocks and the "PC alive" heartbeat (docs/SAFETY.md §8, §11), typed and persisted.
    The keys are the ``safety.<field>`` names :meth:`InterlockConfig.from_values` reads.  Every
    input defaults to 0 (not wired): the hardware is unknown until a service engineer
    commissions it, and the Run screen, the pre-flight and the commissioning checklist say so
    (:mod:`mlaser.services.safety_io`)."""
    out: list[Setting] = []
    for src, inp, act in (("gas", "gas_low_input", "gas_low_active"),
                          ("door", "door_input", "door_open_active"),
                          ("laser", "laser_alarm_input", "laser_alarm_active")):
        out += [
            Setting(f"safety.{inp}", "int", 0, "", 0, MAX_INPUT, label=f"safety.set.{inp}",
                    section="safety", source="app"),
            Setting(f"safety.{act}", "bool", True, label=f"safety.set.{act}", section="safety", source="app"),
            # the service engineer declares that this machine has no such sensor (silences the
            # pre-flight warning; the Run screen and the checklist still list it)
            Setting(f"safety.{src}_not_fitted", "bool", False, label="safety.set.not_fitted",
                    section="safety", source="app"),
        ]
    out += [
        Setting("safety.follower", "bool", True, label="safety.set.follower", section="safety", source="app"),
        Setting("safety.heartbeat_output", "int", 0, "", 0, MAX_OUTPUT, label="safety.set.heartbeat_output",
                section="safety", source="app"),
        Setting("safety.heartbeat_hz", "float", 5.0, "Hz", 0.5, 50.0, label="safety.set.heartbeat_hz",
                section="safety", source="app"),
    ]
    return out


SAFETY_SETTINGS: tuple[Setting, ...] = tuple(_safety_settings())

#: the project's own plugin index (attached to every release by .github/workflows/package.yml)
DEFAULT_PLUGIN_REPOSITORY = "https://github.com/rahilm/fiber_laser/releases/latest/download/mlaser-plugin-index.json"

#: application settings outside the registry (``plugins.allowlist``: entry-point plugins the
#: service engineer enabled, name -> entry-point target; docs/dev/PLUGINS.md §6)
APP_SETTINGS: tuple[Setting, ...] = (
    Setting("plugins.allowlist", "json", {}, label="plugins.allowlist", section="plugins", source="app"),
    Setting("plugins.authority", "json", [], label="plugins.authority", section="plugins", source="app"),
    # the manifest permissions the service engineer accepted when enabling ({id: permissions});
    # a plugin whose declared permissions changed since needs enabling again (PLUGIN_STANDARD.md §7)
    Setting("plugins.accepted", "json", {}, label="plugins.accepted", section="plugins", source="app"),
    # network hosts granted to a plugin in addition to its manifest ({id: [host, ...]})
    Setting("plugins.network", "json", {}, label="plugins.network", section="plugins", source="app"),
    # plugin repositories of the plugin manager (mlaser.services.plugin_manager): [{"url", "name"}]
    Setting("plugins.repositories", "json", [{"url": DEFAULT_PLUGIN_REPOSITORY, "name": "mlaser"}],
            label="plugins.repositories", section="plugins", source="app"),
)


#: the cutting bed (slat grid) for the tip-avoiding cut order (``mlaser.cam.tipping.BedModel``)
#: and the cutting-strategy checks (docs/user/cutting-strategy.md).  ``bed.slat_direction``: 0 =
#: the slats run along Y (spaced along X), 1 = along X; ``bed.slat_offset`` < 0 = unknown (the
#: check then assumes the worst position of the sheet on the slats).
BED_SETTINGS: tuple[Setting, ...] = (
    Setting("bed.slat_pitch", "float", 100.0, "mm", 5.0, 1000.0, label="bed.set.slat_pitch",
            section="bed", source="app"),
    Setting("bed.slat_direction", "enum", 0, options=("y", "x"), label="bed.set.slat_direction",
            section="bed", source="app"),
    Setting("bed.slat_offset", "float", -1.0, "mm", -1.0, 1000.0, label="bed.set.slat_offset",
            section="bed", source="app"),
    Setting("bed.slat_width", "float", 3.0, "mm", 0.5, 50.0, label="bed.set.slat_width",
            section="bed", source="app"),
    Setting("bed.tip_min_size", "float", 10.0, "mm", 0.0, 1000.0, label="bed.set.tip_min_size",
            section="bed", source="app"),
    Setting("bed.stability_margin", "float", 2.0, "mm", 0.0, 100.0, label="bed.set.stability_margin",
            section="bed", source="app"),
    Setting("cam.preflight_lead_check", "bool", True, label="cam.set.preflight_lead_check",
            section="cam", source="app"),
    Setting("cam.preflight_tip_check", "bool", False, label="cam.set.preflight_tip_check",
            section="cam", source="app"),
)


#: the application preferences of the Settings window (docs/user/settings.md): the parameters of
#: the original's Options tables that the audit keeps (docs/dev/settings-audit.md), as typed keys
#: with sensible defaults; ``mlaser.services.settings_catalog`` places them on pages, names their
#: consumers and migrates the original's values into them once.
def _pref(key: str, kind: str, default: Any, unit: str = "", lo: Optional[float] = None,
          hi: Optional[float] = None, options: tuple = ()) -> Setting:
    return Setting(key, kind, default, unit, lo, hi, options, label=f"settings.key.{key}",
                   section=key.split(".", 1)[0], source="app")


PREF_SETTINGS: tuple[Setting, ...] = (
    # General
    _pref("shell.language", "str", ""),        # "" = the system language (else English)
    _pref("app.reopen_last", "bool", False),
    _pref("app.last_file", "str", ""),                 # state: the last drawing opened
    # Display (the canvas view options)
    _pref("display.show_bbox", "bool", False),
    _pref("display.unclosed_red", "bool", True),
    _pref("display.show_index", "bool", True),
    _pref("display.show_start", "bool", True),
    _pref("display.show_direction", "bool", False),
    _pref("display.show_move_path", "bool", False),
    # Import (the clean-up of imported drawings, mf470 Optimize)
    _pref("import.cleanup_on_open", "bool", False),
    _pref("import.remove_small", "bool", True),
    _pref("import.small_gate", "float", 0.1, "mm", 0.01, 10.0),
    _pref("import.remove_overlap", "bool", True),
    _pref("import.overlap_gate", "float", 0.1, "mm", 0.01, 0.5),
    _pref("import.merge_type", "enum", 1, options=("settings.opt.merge.none", "settings.opt.merge.direction",
                                                    "settings.opt.merge.length", "settings.opt.merge.distance")),
    _pref("import.connect_gate", "float", 0.1, "mm", 0.01, 100.0),
    _pref("import.smooth", "bool", False),
    _pref("import.smooth_accuracy", "float", 0.05, "mm", 0.01, 10.0),
    _pref("import.close_gap", "float", 0.0, "mm", 0.0, 100.0),
    # Design / CAM defaults
    _pref("design.nudge_step", "float", 1.0, "mm", 0.01, 1000.0),
    _pref("design.nudge_auto", "bool", True),
    # Nest
    _pref("nest.sheet_width", "float", 3000.0, "mm", 1.0, 100000.0),
    _pref("nest.sheet_height", "float", 1500.0, "mm", 1.0, 100000.0),
    _pref("nest.spacing", "float", 3.0, "mm", 0.0, 100.0),
    _pref("nest.margin", "float", 5.0, "mm", 0.0, 500.0),
    # Costing: one rate model for the cost card, quotes, the CLI estimate and the Report ledger
    # (docs/user/settings.md › Costing); the old plugin.ext_process.* / plugin.report.* rates
    # are migrated once (settings_catalog.LEGACY)
    _pref("costing.currency", "str", "EUR"),          # ISO 4217 code
    _pref("costing.machine_per_h", "float", 0.0, "/h", 0.0, 1e6),
    _pref("costing.power_per_kwh", "float", 0.0, "/kWh", 0.0, 1000.0),
    _pref("costing.gas_o2_per_m3", "float", 0.0, "/m³", 0.0, 10000.0),
    _pref("costing.gas_n2_per_m3", "float", 0.0, "/m³", 0.0, 10000.0),
    _pref("costing.gas_air_per_m3", "float", 0.0, "/m³", 0.0, 10000.0),
    _pref("costing.material_per_kg", "float", 0.0, "/kg", 0.0, 100000.0),
    _pref("costing.material_per_m2", "float", 0.0, "/m²", 0.0, 1e6),
    _pref("costing.per_cut_m", "float", 0.0, "/m", 0.0, 1e6),
    _pref("costing.per_pierce", "float", 0.0, "", 0.0, 1e6),
    _pref("costing.gas_per_h", "float", 0.0, "/h", 0.0, 1e6),
    _pref("costing.power_per_h", "float", 0.0, "/h", 0.0, 1e6),
    _pref("costing.wall_plug_pct", "float", 35.0, "%", 1.0, 100.0),
    _pref("costing.machine_kw", "float", 0.0, "kW", 0.0, 1000.0),
    _pref("costing.nozzle_cd", "float", 0.8, "", 0.1, 1.0),
    _pref("costing.laser_rated_w", "float", 0.0, "W", 0.0, 100000.0),   # home: Machine (laser)
    # the original's values already taken into the keys above ({legacy key: value})
    Setting("settings.migrated", "json", {}, label="settings.migrated", section="settings", source="app"),
)


# --------------------------------------------------------------------------- service
@dataclass
class MachineProfile:
    """How :meth:`ConfigService.machine` builds the machine."""

    base: str = "bench"  # "bench": Machine() + changed parameters; "params": all from the set
    overrides: dict = field(default_factory=dict)  # Machine attribute overrides (e.g. smooth)


class ConfigService:
    """The typed configuration store (see the module docstring)."""

    FILE = "config.json"

    def __init__(self, path: Optional[os.PathLike | str] = None, load: bool = True) -> None:
        self.schema: dict[str, Setting] = dict(registry_settings())
        from mlaser.services.units import UNITS_SETTINGS   # display units (units.py; never the stored values)

        self.schema.update({s.key: replace(s, home=home_of(s))
                            for s in SAFETY_SETTINGS + APP_SETTINGS + BED_SETTINGS + PREF_SETTINGS + UNITS_SETTINGS})
        self.values: dict[str, Any] = {}
        self.profile = MachineProfile()
        self.path = Path(path) if path is not None else config_dir() / self.FILE
        self._listeners: list[Callable[[str], None]] = []
        self._machine_cache: Any = None
        self._dirty = False
        self._batch = 0                      # batch(): the listeners are told at its end
        self._pending: list[str] = []
        self._write_lock = threading.Lock()  # save(): one writer at a time, never an older state
        self._issued = 0
        self._written = 0
        #: why ``config.json`` could not be loaded ("" = it loaded, or there was none): the
        #: settings are then the defaults, the file is not written (:class:`ConfigWriteHeld`)
        #: until :meth:`acknowledge_load_error` or :meth:`restore_backup`, and the window
        #: refuses arming until then (``ui/config_notice.py``)
        self.load_error = ""
        self.load_error_kind = ""            # unreadable | damaged | newer
        #: the copy of the file that could not be loaded (``config.json.bad-<time>``), if made
        self.load_error_evidence: Optional[Path] = None
        self.load_error_acknowledged = False
        self._held = False
        self._held_told = False
        if load:
            self.load()
            self.migrate_legacy()
        install_layer_defaults(self)

    # ------------------------------------------------------------------ schema
    def register(self, settings: Iterable[Setting], replace_existing: bool = False) -> None:
        """Add application or plugin settings; an existing key is refused unless
        ``replace_existing`` (duplicates are an error, not a silent override)."""
        for s in settings:
            if s.key in self.schema and not replace_existing:
                raise KeyError(f"setting {s.key!r} is already defined ({self.schema[s.key].source})")
            self.schema[s.key] = s if s.home else replace(s, home=home_of(s))

    def setting(self, key: str) -> Optional[Setting]:
        return self.schema.get(key)

    def section(self, name: str) -> list[Setting]:
        return [s for s in self.schema.values() if s.section == name]

    def sections(self) -> list[str]:
        return sorted({s.section for s in self.schema.values() if s.section})

    # ------------------------------------------------------------------ values
    def get(self, key: str, default: Any = MISSING) -> Any:
        if key in self.values:
            return self.values[key]
        s = self.schema.get(key)
        if s is not None:
            return s.default
        if default is MISSING:
            raise KeyError(key)
        return default

    def is_set(self, key: str) -> bool:
        return key in self.values

    def set(self, key: str, value: Any) -> Any:
        s = self.schema.get(key)
        v = s.validate(value) if s is not None else value
        if self.values.get(key, MISSING) == v and key in self.values:
            return v
        self.values[key] = v
        self._changed(key)
        return v

    def reset(self, key: str) -> None:
        if key in self.values:
            del self.values[key]
            self._changed(key)

    def update(self, values: dict) -> None:
        for k, v in values.items():
            self.set(k, v)

    def _changed(self, key: str) -> None:
        self._dirty = True
        self._machine_cache = None
        if self._batch:
            if key not in self._pending:
                self._pending.append(key)
            return
        for fn in list(self._listeners):
            fn(key)

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        """Set several values, then tell the listeners (each changed key once, in order): a
        listener that recomputes from all the values (the window's machine profile, 5-10 ms)
        then does so once, not once per value (closing the window wrote 11 counters: 80 ms of
        the GUI thread's 85 ms close, 0.3 s on a slow runner)."""
        self._batch += 1
        try:
            yield
        finally:
            self._batch -= 1
            if not self._batch:
                keys, self._pending = self._pending, []
                for key in keys:
                    for fn in list(self._listeners):
                        fn(key)

    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def mapping(self) -> "SettingsView":
        return SettingsView(self)

    def namespace(self, prefix: str, registry_fallback: bool = True) -> "NamespaceView":
        """A ``dict``-like store for one component (``"machine_ops"``): keys are kept as
        ``"<prefix>.<key>"``; reading a key that is not stored there falls back to the value
        the user set for the registry parameter of that name (so Options reach the component)."""
        return NamespaceView(self, prefix, registry_fallback)

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        """Read ``config.json``.  Absent: a fresh installation (the defaults).  Present but
        unreadable, not JSON / UTF-8, of the wrong shape or of a newer format: the defaults
        too, but never silently (architecture review A3): the error is logged, kept in
        :attr:`load_error`, the file's bytes are copied to ``config.json.bad-<time>`` (the file
        itself stays where it is, so another mlaser command or the next start sees the same
        problem), and the file is not written until the operator answered the start-up notice
        (:meth:`acknowledge_load_error` / :meth:`restore_backup`).  A clean load keeps a copy
        of the file as ``config.json.bak`` (the backup that notice offers)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            self._load_failed(ConfigLoadError("unreadable", f"{type(exc).__name__}: {exc}"), None)
            return
        try:
            (base, overrides), values = parse_config(raw)
        except ConfigLoadError as exc:
            self._load_failed(exc, raw)
            return
        self._apply_loaded(base, overrides, values)
        self._keep_backup(raw)

    def _apply_loaded(self, base: str, overrides: dict, values: dict) -> None:
        self.profile = MachineProfile(base, overrides)
        dropped = []
        for k, v in values.items():
            s = self.schema.get(k)
            try:
                self.values[k] = s.validate(v) if s is not None else v
            except ValueError:
                dropped.append(k)    # an out-of-range stored value falls back to the default
        if dropped:
            log.warning("configuration %s: %d stored value(s) out of range, using the default: %s",
                        self.path, len(dropped), ", ".join(sorted(dropped)[:20]))
        self._dirty = False
        self._machine_cache = None

    @property
    def backup_path(self) -> Path:
        """``config.json.bak``: the last file that loaded cleanly."""
        return self.path.with_name(self.path.name + ".bak")

    def _keep_backup(self, raw: bytes) -> None:
        bak = self.backup_path
        try:
            if bak.is_file() and bak.read_bytes() == raw:
                return
            atomic_write(bak, raw)
        except OSError as exc:
            log.warning("configuration backup %s not written: %s", bak, exc)

    def _evidence_path(self, raw: Optional[bytes]) -> Path:
        """Where the copy of a file that could not be loaded goes: an existing copy with the
        same bytes (the same damage seen again), else ``<name>.bad-<time>``."""
        name = self.path.name
        if raw is not None:
            for old in sorted(self.path.parent.glob(f"{name}.bad-*"), reverse=True)[:5]:
                with contextlib.suppress(OSError):
                    if old.read_bytes() == raw:
                        return old
        import time
        stamp = time.strftime("%Y%m%d-%H%M%S")
        p, n = self.path.with_name(f"{name}.bad-{stamp}"), 1
        while p.exists():
            n += 1
            p = self.path.with_name(f"{name}.bad-{stamp}-{n}")
        return p

    def _load_failed(self, exc: ConfigLoadError, raw: Optional[bytes]) -> None:
        self.load_error, self.load_error_kind = str(exc), exc.kind
        self.load_error_acknowledged = False
        self._held = True
        if raw is not None:
            ev = self._evidence_path(raw)
            try:
                if not ev.exists():
                    atomic_write(ev, raw)
                self.load_error_evidence = ev
            except OSError as e2:
                log.error("configuration: the copy %s could not be written: %s", ev, e2)
        log.error("configuration %s could not be loaded (%s: %s); copy: %s.  mlaser runs on the default "
                  "settings and does not write the file until the start-up notice is answered; arming "
                  "is refused until then", self.path, exc.kind, exc,
                  self.load_error_evidence or "none (the file itself was left in place)")

    @property
    def load_blocked(self) -> bool:
        """A load error the operator has not answered yet (no writes, no arming)."""
        return bool(self.load_error) and not self.load_error_acknowledged

    def backup_usable(self) -> bool:
        """Is there a ``config.json.bak`` that loads cleanly (the notice offers to restore it)?"""
        try:
            parse_config(self.backup_path.read_bytes())
        except (OSError, ConfigLoadError):
            return False
        return True

    def acknowledge_load_error(self) -> bool:
        """The operator continues with the default settings (the start-up notice): the file is
        written again from now on.  A file that could not be read (so no copy exists) is first
        moved aside to ``config.json.bad-<time>``; when that fails too, writes stay held for
        this session (the file may be intact) and False is returned.  Either way the error
        counts as acknowledged (:attr:`load_blocked` is False)."""
        if not self.load_error or (self.load_error_acknowledged and not self._held):
            return True
        self.load_error_acknowledged = True
        if self.load_error_evidence is None and self.path.exists():
            ev = self._evidence_path(None)
            try:
                os.replace(self.path, ev)
            except OSError as exc:
                log.error("configuration %s could not be moved aside (%s): it is not written in this "
                          "session", self.path, exc)
                return False
            self.load_error_evidence = ev
        self._held = False
        log.warning("configuration load error acknowledged by the operator: continuing with the default "
                    "settings (the unusable file is kept as %s)", self.load_error_evidence)
        return True

    def restore_backup(self) -> bool:
        """Load ``config.json.bak`` (the last file that loaded cleanly) in place of the file
        that could not be loaded, write it as ``config.json`` and tell the listeners (``"*"``).
        False (nothing changed) when the backup is missing or unusable."""
        try:
            raw = self.backup_path.read_bytes()
            (base, overrides), values = parse_config(raw)
        except (OSError, ConfigLoadError) as exc:
            log.error("configuration backup %s could not be restored: %s", self.backup_path, exc)
            return False
        self.values = {}
        self._apply_loaded(base, overrides, values)
        self.migrate_legacy()
        self._held = False
        self.load_error_acknowledged = True
        log.warning("configuration restored from %s (the unusable file is kept as %s)",  # i18n: ok (log)
                    self.backup_path, self.load_error_evidence)
        self._changed("*")
        try:
            self.save()
        except OSError as exc:
            log.error("configuration %s not written after the restore: %s", self.path, exc)
        return True

    def _snapshot(self) -> tuple[int, str]:
        vals = {k: v for k, v in self.values.items() if _jsonable(v)}
        data = {
            "version": CONFIG_VERSION,
            "profile": {"base": self.profile.base, "overrides": self.profile.overrides},
            "values": vals,
        }
        self._issued += 1
        return self._issued, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)

    def _write(self, p: Path, ticket: int, text: str) -> None:
        with self._write_lock:
            if p == self.path and self._held:
                raise ConfigWriteHeld(f"{p} is not written: it could not be loaded ({self.load_error}) and "
                                      "the start-up notice has not been answered")
            if p == self.path and ticket < self._written:
                return                       # a newer state is on disk already
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, text)
            if p == self.path:
                self._written = ticket

    def save(self, path: Optional[os.PathLike | str] = None) -> Path:
        p = Path(path) if path is not None else self.path
        ticket, text = self._snapshot()
        self._write(p, ticket, text)
        self._dirty = False
        return p

    def flush(self, background: bool = False) -> None:
        """Write the settings if they changed.  ``background``: the state is taken here and
        written by a (non-daemon) thread, so the caller - the GUI thread closing the window or
        saving the counters - never waits on the disk (Windows CI: a settings write held a
        close for 0.1-0.8 s); a later save never loses to it, and the interpreter waits for
        the write before it exits."""
        if not self._dirty:
            return
        if self._held:
            if not self._held_told:
                self._held_told = True
                log.warning("configuration %s not written: the load error is not acknowledged", self.path)
            return
        if not background:
            try:
                self.save()
            except OSError:
                pass
            return
        ticket, text = self._snapshot()
        self._dirty = False

        def write() -> None:
            try:
                self._write(self.path, ticket, text)
            except OSError:
                self._dirty = True
        threading.Thread(target=write, name="mlaser-config-save", daemon=False).start()

    # ------------------------------------------------------------------ original XML files
    def machine_params(self):
        """A :class:`mlaser.formats.params.MachineParams` (the three ParaModule slots) with the
        registry defaults and every set value."""
        from mlaser.formats.params import MachineParams, param_defs

        mp = MachineParams.defaults()
        by_key = {}
        for d in param_defs():
            if d.file in ("HardPara.xml", "ManuPara.xml") and "." in d.key:
                by_key.setdefault(d.key, []).append(d)
        for k, v in self.values.items():
            for d in by_key.get(k.split(":", 1)[-1], ()):
                st = mp.hard if d.file == "HardPara.xml" else mp.manu
                try:
                    st.set(d, v)
                except (TypeError, ValueError):
                    continue
        return mp

    def import_dat(self, dat_dir: os.PathLike | str, profile: str = "params") -> int:
        """Load ``HardPara.xml`` / ``ManuPara.xml`` (and the legacy ``SystemPara.xml``) of an
        original install; every value that differs from the default becomes a set value.  The
        machine profile switches to ``profile`` (default: the machine described by the files).
        Returns the number of values taken."""
        from mlaser.formats.params import MachineParams, param_defs

        mp = MachineParams.load_dir(Path(dat_dir), fill_defaults=False)
        n = 0
        for d in param_defs():
            if d.file not in ("HardPara.xml", "ManuPara.xml") or "." not in d.key:
                continue
            st = mp.hard if d.file == "HardPara.xml" else mp.manu
            raw = st.get_raw(d.category, d.key)
            if raw is None:
                continue
            key = _XML_KEYS.get((d.category, d.key), d.key)
            v = d.parse(raw)
            if v == d.default_value:
                self.values.pop(key, None)
                continue
            try:
                self.values[key] = self.schema[key].validate(v) if key in self.schema else v
            except ValueError:
                self.values[key] = v  # keep the machine's own value even when out of range
            n += 1
        self.profile = replace(self.profile, base=profile)
        self._changed("*")
        self.migrate_legacy()       # the imported Options values reach the Settings keys
        return n

    def migrate_legacy(self) -> list[str]:
        """Take the original's changed Options values into the Settings keys, once
        (:func:`mlaser.services.settings_catalog.migrate_legacy`)."""
        from .settings_catalog import migrate_legacy

        return migrate_legacy(self)

    def export_dat(self, dat_dir: os.PathLike | str) -> None:
        """Write the three XML files (registry defaults + set values) like ParaModule does."""
        self.machine_params().save_dir(dat_dir)

    # ------------------------------------------------------------------ machine profile
    def set_profile(self, base: str, **overrides) -> None:
        if base not in ("bench", "params"):
            raise ValueError(f"unknown machine profile {base!r}")
        self.profile = MachineProfile(base, {**self.profile.overrides, **overrides})
        self._changed("profile")

    def machine(self):
        """The :class:`mlaser.nc.Machine` for compiling jobs (cached until a value changes).

        ``bench``: ``Machine()`` with each parameter the user changed applied the way
        :meth:`Machine.from_params` applies it (attribute, ``ProcessConfig`` field and the raw
        ``params`` the motion config reads); with nothing changed this is exactly ``Machine()``.
        ``params``: ``Machine.from_params`` of the full parameter set."""
        if self._machine_cache is not None:
            return self._machine_cache
        from mlaser.nc.machine import Machine

        mp = self.machine_params()
        if self.profile.base == "params":
            m = Machine.from_params(mp)
        else:
            m = Machine()
            changed = {
                k.split(":", 1)[-1]
                for k in self.values
                if self.schema.get(k) is not None and self.schema[k].file in ("HardPara.xml", "ManuPara.xml")
            }
            if changed:
                cur = Machine.from_params(mp)
                ref = _reference_machine()
                for f in fields(Machine):
                    if f.name in ("process", "params", "cut"):
                        continue
                    a, b = getattr(cur, f.name), getattr(ref, f.name)
                    if a != b:
                        setattr(m, f.name, a)
                pc = {
                    f.name: getattr(cur.process, f.name)
                    for f in fields(cur.process)
                    if getattr(cur.process, f.name) != getattr(ref.process, f.name)
                }
                if pc:
                    m.process = replace(m.process, **pc)
                m.params = {k: v for k, v in cur.params.items() if k in changed}
        for k, v in self.profile.overrides.items():
            if hasattr(m, k):
                setattr(m, k, v)
        self._machine_cache = m
        return m

    def motion_config(self):
        return self.machine().motion_config()

    # ------------------------------------------------------------------ layer defaults
    def layer_defaults(self, co2: bool = False) -> dict[str, Any]:
        """The ``GP.*`` layer descriptor defaults the NC layer view starts from
        (``parity_ports.mainapp.layer``).  These are the registry defaults exactly: a layer's own
        values come from the document (LayerPara.xml / the layer table), so the compiled job does
        not depend on which ConfigService is open (the goldens prove it)."""
        from mlaser.formats.params import layer_default_values

        return layer_default_values(co2)

    def process_config(self):
        return self.machine().process_config()


def install_layer_defaults(svc: Optional["ConfigService"] = None) -> None:
    """Hand the layer defaults to the parity fence (``set_layer_defaults``): the ConfigService's
    provider, or the plain registry reader without one."""
    from mlaser.formats.params import layer_default_values
    from mlaser.parity_ports.mainapp.layer import set_layer_defaults

    set_layer_defaults(svc.layer_defaults if svc is not None else layer_default_values)


_REF: list = []


def _reference_machine():
    """``Machine.from_params`` of the pure registry defaults (what "unchanged" means)."""
    if not _REF:
        from mlaser.formats.params import MachineParams
        from mlaser.nc.machine import Machine

        _REF.append(Machine.from_params(MachineParams.defaults()))
    return _REF[0]


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


class SettingsView(MutableMapping):
    """``dict``-like view of the explicitly set values: it behaves like the old
    ``window.settings`` dict (``key in view`` only for set keys, ``get(key, default)``), but
    writes go through :meth:`ConfigService.set` (validated, persisted, listeners told)."""

    def __init__(self, svc: ConfigService) -> None:
        self._svc = svc

    def __getitem__(self, key: str) -> Any:
        if key in self._svc.values:
            return self._svc.values[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._svc.values:
            return self._svc.values[key]
        return default

    def __setitem__(self, key: str, value: Any) -> None:
        self._svc.set(key, value)

    def batch(self) -> contextlib.AbstractContextManager:
        """:meth:`ConfigService.batch`."""
        return self._svc.batch()

    def __delitem__(self, key: str) -> None:
        if key not in self._svc.values:
            raise KeyError(key)
        self._svc.reset(key)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._svc.values))

    def __len__(self) -> int:
        return len(self._svc.values)

    def __repr__(self) -> str:
        return f"SettingsView({self._svc.values!r})"


class NamespaceView(MutableMapping):
    """See :meth:`ConfigService.namespace`."""

    def __init__(self, svc: ConfigService, prefix: str, registry_fallback: bool = True) -> None:
        self._svc, self._p, self._fallback = svc, prefix.rstrip(".") + ".", registry_fallback

    def _own(self) -> list[str]:
        return [k[len(self._p) :] for k in self._svc.values if k.startswith(self._p)]

    def __getitem__(self, key: str) -> Any:
        full = self._p + key
        if full in self._svc.values:
            return self._svc.values[full]
        if self._fallback and key in self._svc.values and key in self._svc.schema:
            return self._svc.values[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._svc.set(self._p + key, value)

    def __delitem__(self, key: str) -> None:
        self._svc.reset(self._p + key)

    def __iter__(self) -> Iterator[str]:
        own = self._own()
        extra = [k for k in self._svc.values if self._fallback and k in self._svc.schema and k not in own]
        return iter(own + extra)

    def __len__(self) -> int:
        return len(list(iter(self)))
