"""The Settings window's catalogue: which ConfigService keys it shows, where, to whom, who reads
them, and the one-time migration of the original's Options values.  Qt-free.

The original's Options dialog (``app.options``) was a generic grid over four parameter tables
(SoftParam, GraphParam, ImportGraphParam, ManuParam: 475 entries).  docs/dev/settings-audit.md
decides each of them: **keep** (a typed key below, read by the code named in ``consumer``),
**move** (it belongs in the Machine workspace's hardware parameters, a tool dialog, the
technology tables, the Units service...), **drop** or **parity-only** (kept for the original's
profile import and export, never shown).  The Settings window (:mod:`mlaser.ui.settings_dialog`)
shows exactly the :data:`ENTRIES` whose key is in the schema, plus the scalar settings of
third-party plugins on the Plugins page.

Every entry names its ``consumer``: the module that reads the key (tests/test_settings_catalog.py
checks that the module really mentions it, so a setting without a reader cannot be shown).

Migration (:func:`migrate_legacy`): a value of the original's table that the user changed
(an older Options dialog of this program stored it, or an original install was imported with
``import_dat``) is taken once into the new key; ``settings.migrated`` records the value taken, so
a later change of the new key is not overwritten, but importing a profile again with a
different value is taken again.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .usermodes import ENGINEER, OPERATOR

#: (page id, i18n key of its title) in sidebar order
PAGES: tuple[tuple[str, str], ...] = (
    ("general", "settings.page.general"),
    ("appearance", "settings.page.appearance"),
    ("canvas", "settings.page.canvas"),
    ("import", "settings.page.import"),
    ("cam", "settings.page.cam"),
    ("costing", "settings.page.costing"),
    ("run", "settings.page.run"),
    ("network", "settings.page.network"),
    ("notify", "settings.page.notify"),
    ("plugins", "settings.page.plugins"),
    ("users", "settings.page.users"),
    ("machine", "settings.page.machine"),
    ("advanced", "settings.page.advanced"),
)
PAGE_IDS = tuple(p for p, _ in PAGES)

#: the cutting-order modes of ``CamParams.sort_mode`` (mlaser.cam.ordering) offered as default
SORT_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "settings.opt.sort.keep"), (1, "settings.opt.sort.left_right"), (2, "settings.opt.sort.right_left"),
    (3, "settings.opt.sort.bottom_top"), (4, "settings.opt.sort.top_bottom"), (5, "settings.opt.sort.nearest"),
    (6, "settings.opt.sort.in_out"), (7, "settings.opt.sort.out_in"), (20, "settings.opt.sort.shortest"),
    (21, "settings.opt.sort.grid"),
)
THEME_CHOICES = (("light", "shell.theme.light"), ("dark", "shell.theme.dark"),
                 ("high_contrast", "shell.theme.high_contrast"))


@dataclass(frozen=True)
class Entry:
    """One visible setting."""

    key: str
    page: str
    group: str                       # i18n key of the group heading
    consumer: str                    # module (path from the checkout) that reads the key
    min_mode: str = ENGINEER         # lowest user mode that may change it
    restart: bool = False            # takes effect at the next start
    choices: tuple = ()              # ((value, label key), ...) for str / int settings
    dynamic: str = ""                # choices filled at run time: "languages" | "profiles"
    secret: bool = False             # shown as a password field
    home: str = ""                   # the command that is this value's one home (shown read-only here)
    home_label: str = ""             # i18n key of the "Edit in …" button

    @property
    def readonly(self) -> bool:
        """The value has its home elsewhere (one home per setting): a read-only summary here."""
        return bool(self.home)

    @property
    def help_key(self) -> str:
        return "settings.help." + self.key

    @property
    def label_key(self) -> str:
        return "settings.key." + self.key


def _e(page: str, group: str, consumer: str, *keys: str, **kw) -> list[Entry]:
    return [Entry(k, page, f"settings.group.{group}", consumer, **kw) for k in keys]


MW = "mlaser/ui/mainwindow.py"
SA = "mlaser/ui/settings_apply.py"
#: the one-shot CAM tools whose last-used values are remembered (mlaser.ui.tool_memory)
CAM_TOOLS = ("array", "circular_array", "sheet_fill", "fill", "linear_fill", "fillet", "unload", "alpha", "cool",
             "rect_share", "circle_fly", "line_fly", "bridge", "onekey", "engrave_scan")
TM = "mlaser/ui/tool_memory.py"

ENTRIES: tuple[Entry, ...] = tuple(
    # ---------------------------------------------------------------- General (+ the units.* keys)
    _e("general", "language", MW, "shell.language", min_mode=OPERATOR, dynamic="languages")
    + _e("general", "startup", "mlaser/ui/__main__.py", "app.reopen_last", min_mode=OPERATOR, restart=True)
    + _e("general", "projects", "mlaser/ui/ext_project.py", "plugin.ext_project.autosave_interval_s")
    # ---------------------------------------------------------------- Appearance
    + _e("appearance", "appearance", "mlaser/ui/shell/__init__.py", "shell.theme", min_mode=OPERATOR,
         choices=THEME_CHOICES)
    + _e("appearance", "appearance", "mlaser/ui/shell/__init__.py", "shell.classic_ribbon", min_mode=OPERATOR)
    # ---------------------------------------------------------------- Canvas
    + _e("canvas", "canvas", SA, "display.show_bbox", "display.unclosed_red", "display.show_index",
         "display.show_start", "display.show_direction", "display.show_move_path", min_mode=OPERATOR)
    + _e("canvas", "editing", "mlaser/ui/ext_draw.py", "design.nudge_step", "design.nudge_auto")
    # ---------------------------------------------------------------- Import / Export
    + _e("import", "cleanup", SA, "import.cleanup_on_open", "import.remove_small", "import.small_gate",
         "import.remove_overlap", "import.overlap_gate", "import.merge_type", "import.connect_gate",
         "import.smooth", "import.smooth_accuracy", "import.close_gap")
    + _e("import", "dxf", "mlaser/ui/ext_options.py", "plugin.ext_options.dxf_modern")
    + _e("import", "orders", "mlaser/ui/workspaces/orders.py", "plugin.orders.review_after_import",
         "plugin.orders.join_tol", "plugin.orders.close_gap")
    + _e("import", "orders", "mlaser/ui/workspaces/orders.py", "plugin.orders.recognition",
         choices=tuple((v, f"settings.opt.recognition.{v}") for v in ("nested", "layer", "block", "file", "none")))
    + _e("import", "orders", "mlaser/ui/workspaces/orders.py", "plugin.orders.feature_layers",
         "plugin.orders.ignore_layers")
    + _e("import", "orders", "mlaser/ui/workspaces/orders.py", "plugin.orders.islands",
         choices=(("keep", "settings.opt.islands.keep"), ("parts", "settings.opt.islands.parts")))
    + _e("import", "labels", "mlaser/ui/ext_labels.py", "plugin.ext_labels.label_size",
         "plugin.ext_labels.remnant_size", "plugin.ext_labels.ticket_page", "plugin.ext_labels.remnant_auto",
         "plugin.ext_labels.mark_layer", "plugin.ext_labels.mark_min_height", "plugin.ext_labels.mark_max_height",
         "plugin.ext_labels.mark_margin")
    # ---------------------------------------------------------------- CAM tools (last used) and checks
    # (leads, micro-joints and the cutting order are per part / per job: Process › Properties)
    + _e("cam", "tools", TM, *(f"cam.tool.{t}" for t in CAM_TOOLS))
    + _e("cam", "checks", "mlaser/ui/workspaces/cutting_strategy.py", "cam.preflight_lead_check",
         "cam.preflight_tip_check")
    # ---------------------------------------------------------------- Costing (one rate model)
    + _e("costing", "currency", "mlaser/services/costing.py", "costing.currency")
    + _e("costing", "estimate", "mlaser/services/costing.py", "costing.machine_per_h", "costing.power_per_kwh",
         "costing.gas_o2_per_m3", "costing.gas_n2_per_m3", "costing.gas_air_per_m3", "costing.material_per_kg",
         "costing.wall_plug_pct", "costing.machine_kw", "costing.nozzle_cd")
    + _e("costing", "estimate", "mlaser/services/costing.py", "costing.laser_rated_w",
         home="workspace.machine", home_label="settings.home.machine")
    + _e("costing", "ledger", "mlaser/ui/workspaces/report.py", "costing.gas_per_h", "costing.power_per_h",
         "costing.per_cut_m", "costing.per_pierce", "costing.material_per_m2")
    # ---------------------------------------------------------------- Run & Queue
    + _e("run", "remnants", "mlaser/ui/workspaces/run.py", "plugin.run.remnant_auto", "plugin.run.remnant_min_width",
         "plugin.run.remnant_min_area")
    # ---------------------------------------------------------------- Network & API
    + _e("network", "monitor", "mlaser/ui/workspaces/run.py", "plugin.run.monitor_enabled",
         "plugin.run.monitor_host", "plugin.run.monitor_port")
    + _e("network", "monitor", "mlaser/ui/workspaces/run.py", "plugin.run.monitor_token", secret=True)
    + _e("network", "automation", "mlaser/ui/ext_automation.py", "plugin.ext_automation.enabled",
         "plugin.ext_automation.host", "plugin.ext_automation.port", "plugin.ext_automation.queue_intake",
         "plugin.ext_automation.uploads", "plugin.ext_automation.job_folders", "plugin.ext_automation.max_upload_mb",
         home="api.settings", home_label="settings.home.api")
    # ---------------------------------------------------------------- Notifications
    + _e("notify", "notify_general", "mlaser/ui/ext_notify.py", "plugin.ext_notify.enabled",
         "plugin.ext_notify.machine_name", "plugin.ext_notify.include_simulator",
         home="notify.settings", home_label="settings.home.notify")
    + _e("notify", "notify_ntfy", "mlaser/ui/ext_notify.py", "plugin.ext_notify.ntfy_enabled",
         "plugin.ext_notify.ntfy_server", "plugin.ext_notify.ntfy_topic",
         home="notify.settings", home_label="settings.home.notify")
    + _e("notify", "notify_email", "mlaser/ui/ext_notify.py", "plugin.ext_notify.email_enabled",
         "plugin.ext_notify.smtp_host", "plugin.ext_notify.smtp_port", "plugin.ext_notify.smtp_security",
         "plugin.ext_notify.smtp_user", "plugin.ext_notify.email_from", "plugin.ext_notify.email_to",
         home="notify.settings", home_label="settings.home.notify")
    + _e("notify", "notify_webhook", "mlaser/ui/ext_notify.py", "plugin.ext_notify.webhook_enabled",
         "plugin.ext_notify.webhook_url",
         home="notify.settings", home_label="settings.home.notify")
    + _e("notify", "notify_limits", "mlaser/ui/ext_notify.py", "plugin.ext_notify.min_interval_s",
         "plugin.ext_notify.max_per_hour", "plugin.ext_notify.quiet_start", "plugin.ext_notify.quiet_end",
         "plugin.ext_notify.quiet_alarms",
         home="notify.settings", home_label="settings.home.notify")
    + _e("notify", "notify_events", "mlaser/notify/core.py", *(
        f"plugin.ext_notify.{p}_{ev}" for ev in ("sheet_done", "queue_waiting", "alarm", "job_finished",
                                                  "maintenance_due") for p in ("event", "channels")),
         home="notify.settings", home_label="settings.home.notify")
    # ---------------------------------------------------------------- Advanced
    + _e("advanced", "engines", "mlaser/ui/ext_options.py", "plugin.ext_options.kerf_clipper")
    + _e("advanced", "engines", "mlaser/ui/ext_options.py", "plugin.ext_options.parity_profile",
         home="app.profile", home_label="settings.home.profile")
)
BY_KEY: dict[str, Entry] = {e.key: e for e in ENTRIES}

#: keys of built-in plugins that are state or have their home elsewhere, never on the Plugins page
HIDDEN_PLUGIN_KEYS = frozenset({"plugin.ext_process.machine_profile", "plugin.ext_practice.progress"})

#: a note under a page's title (i18n key)
PAGE_NOTES = {"cam": "settings.cam.note"}

#: links shown on pages: (page, command id, i18n key of the button)
LINKS: tuple[tuple[str, str, str], ...] = (
    ("general", "app.import_language", "settings.link.import_language"),
    ("import", "shell.import_params", "settings.link.import_params"),
    ("users", "shell.user_mode", "settings.link.user_mode"),
    ("users", "shell.set_pins", "settings.link.set_pins"),
    ("machine", "workspace.machine", "settings.link.machine_workspace"),
    ("machine", "hw.options", "settings.link.hw_options"),
    ("machine", "hw.set_ip", "settings.link.set_ip"),
    ("machine", "machine.safety_io", "settings.link.safety_io"),
    ("notify", "notify.test", "settings.link.notify_test"),
    ("plugins", "app.plugin_manager", "settings.link.plugin_manager"),
    ("network", "app.netcheck", "settings.link.netcheck"),
)


def entries_for(page: str) -> list[Entry]:
    return [e for e in ENTRIES if e.page == page]


def plugin_settings(schema: dict) -> dict[str, list]:
    """Scalar settings of plugins not covered by :data:`ENTRIES` (third-party plugins):
    ``{plugin name: [Setting, ...]}`` for the Plugins page."""
    out: dict[str, list] = {}
    for k, s in schema.items():
        if not s.source.startswith("plugin:") or k in BY_KEY or k in HIDDEN_PLUGIN_KEYS or s.kind == "json":
            continue
        out.setdefault(s.source.split(":", 1)[1], []).append(s)
    return out


# --------------------------------------------------------------------------- migration
_LANG_BY_INDEX = ("zh", "en", "ru", "de", "es", "pt", "fr", "it", "pl", "vi", "tr")   # SP.Lang options


def _index(table: tuple) -> Callable[[Any], Any]:
    def f(v: Any) -> Any:
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None
        return table[i] if 0 <= i < len(table) else None
    return f


def _same(v: Any) -> Any:
    return v


def _sort_mode(v: Any) -> Any:
    """GRP.SortType index -> CamParams.sort_mode (index + 1; 7 "small parts first" is a flag)."""
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i + 1 if 0 <= i <= 6 else None


def _currency(v: Any) -> Any:
    from .costing import iso_currency
    return iso_currency(v) or None


#: (original key, new key, conversion; None = nothing to take)
LEGACY: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("SP.Lang", "shell.language", _index(_LANG_BY_INDEX)),
    ("SOP.IsStartAutoLoadGraph", "app.reopen_last", _same),
    ("SOP.LastLoadFilePath", "app.last_file", _same),
    ("SP.Skin", "shell.theme", _index(("light", "dark"))),
    ("SP.SoftwareThemeType", "shell.theme", _index(("dark", None))),
    ("GRP.IsShowEnvelopBox", "display.show_bbox", _same),
    ("GRP.IsShowEncolseWithRed", "display.unclosed_red", _same),
    ("GRP.IsShowNO", "display.show_index", _same),
    ("GRP.IsShowStartPt", "display.show_start", _same),
    ("GRP.IsShowDirArrow", "display.show_direction", _same),
    ("GRP.IsShowLinkerLine", "display.show_move_path", _same),
    ("IGP.IsAutoFilterMicoGraph", "import.remove_small", _same),
    ("IGP.MicoGraphGate", "import.small_gate", _same),
    ("IGP.IsFilteOverlapGraph", "import.remove_overlap", _same),
    ("IGP.OverlapGate", "import.overlap_gate", _same),
    ("IGP.MegerConnectGraphType", "import.merge_type", _same),
    ("IGP.ConnectGate", "import.connect_gate", _same),
    ("IGP.IsAutoSmoothGraph", "import.smooth", _same),
    ("IGP.SmoothAccuracy", "import.smooth_accuracy", _same),
    ("GRP.GuideLineType", "cam.lead_in_type", _same),
    ("GRP.GuideLineAngle", "cam.lead_in_angle", _same),
    ("GRP.GuideLineLength", "cam.lead_in_length", _same),
    ("GRP.GuideArcRadius", "cam.lead_in_radius", _same),
    ("GRP.GuideLineType_o", "cam.lead_out_type", _same),
    ("GRP.GuideLineAngle_o", "cam.lead_out_angle", _same),
    ("GRP.GuideLineLength_o", "cam.lead_out_length", _same),
    ("GRP.GuideArcRadius_o", "cam.lead_out_radius", _same),
    ("GRP.IsOnlyForEncolseContour", "cam.lead_closed_only", _same),
    ("GRP.LeadPosType", "cam.lead_start_mode", _same),
    ("GRP.SortType", "cam.sort_mode", _sort_mode),
    ("GRP.SortIsSmallFirst", "cam.small_first", _same),
    ("GRP.AutoMicroLinkType", "cam.mj_kind", _same),
    ("GRP.AutoMicroLinkNum", "cam.mj_count", _same),
    ("GRP.AutoMicroLinkStep", "cam.mj_distance", _same),
    ("GraphParam:GRP.MicroLinkLength", "cam.mj_length", _same),
    ("GRP.MinEnableMicoLinkGraphSize", "cam.mj_min_size", _same),
    ("IGP.KeyboardMoveStepLength", "design.nudge_step", _same),
    ("SP.KeyboardMoveStep", "design.nudge_step", _same),     # the key the rebuild's fine-tune stored
    ("IGP.AutoCalcKeyboardMoveStepLength", "design.nudge_auto", _same),
    ("GRP.StdSheetWidth", "nest.sheet_width", _same),
    ("GRP.StdSheetHeight", "nest.sheet_height", _same),
    # costing: the Report ledger's and the Process cost card's rates become one model (the
    # Process values win where both were set; currencies become ISO codes)
    *((f"plugin.report.{k}", f"costing.{k}", _same) for k in (
        "machine_per_h", "gas_per_h", "power_per_h", "per_cut_m", "per_pierce", "material_per_m2")),
    ("plugin.report.currency", "costing.currency", _currency),
    *((f"plugin.ext_process.{k}", f"costing.{k}", _same) for k in (
        "machine_per_h", "power_per_kwh", "gas_o2_per_m3", "gas_n2_per_m3", "gas_air_per_m3", "material_per_kg",
        "laser_rated_w", "wall_plug_pct", "machine_kw", "nozzle_cd")),
    ("plugin.ext_process.currency", "costing.currency", _currency),
    # the Units service's keys (registered by mlaser.services.units; skipped until they exist)
    ("MC.UnitType", "units.system", _index(("metric", "imperial"))),
    ("UN.SpeedUnit", "units.speed", _index(("mm/s", "m/min", "in/s", "in/min"))),
    ("UN.AccUnit", "units.accel", _index(("mm/s²", "g", "in/s²"))),
    ("UN.GasPressureUnit", "units.pressure", _index(("bar", "MPa"))),
)
MIGRATED_KEY = "settings.migrated"


def migrate_legacy(cfg: Any) -> list[str]:
    """Take the original's changed values into the new keys (see the module docstring).
    Returns the new keys written."""
    done = dict(cfg.get(MIGRATED_KEY, {}) or {})
    wrote: list[str] = []
    for old, new, conv in LEGACY:
        if not cfg.is_set(old) or cfg.setting(new) is None:
            continue
        if new.startswith("units.") and cfg.is_set(new):
            continue                    # the display units the user chose win over the original's
        raw = cfg.values[old]
        if old in done and done[old] == raw:
            continue
        v = conv(raw)
        done[old] = raw
        if v is None:
            continue
        try:
            cfg.set(new, v)
            wrote.append(new)
        except ValueError:
            continue
    if done != (cfg.get(MIGRATED_KEY, {}) or {}):
        cfg.set(MIGRATED_KEY, done)
    return wrote


def consumers() -> dict[str, str]:
    """{key: consumer module} of every visible entry (the no-orphan registry)."""
    return {e.key: e.consumer for e in ENTRIES}


def visible(schema: dict, mode: Optional[str] = None) -> list[Entry]:
    """The entries whose key exists in ``schema``."""
    return [e for e in ENTRIES if e.key in schema]
