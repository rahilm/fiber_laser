#!/usr/bin/env python3
"""Generate docs/dev/settings-audit.md (the audit of the original's Options dialog) and the
setting tables of docs/user/settings.md.

One row per parameter of the four tables behind the old ``app.options`` grid (SoftParam,
GraphParam, ImportGraphParam, ManuParam): what it did in the original, whether the rebuild reads
it (a scan of the code's string literals and of the ParaModule struct offsets the controller reads,
comments and docstrings excluded), its default, a sensible default, and the decision:

* **keep**: a typed ConfigService key (:data:`mlaser.services.settings_catalog.LEGACY` migrates the
  old value once);
* **move**: it belongs elsewhere (Machine settings, a tool dialog, the technology tables, a
  diagnostics tool, the Units service...);
* **drop**: no meaning in the rebuild (with why);
* **parity-only**: kept in the schema so the original's profile import / export round-trips
  (mlaser.services.settings_import, ``ConfigService.import_dat`` / ``export_dat``), never shown.

    python tools/settings_audit.py            # write the file
    python tools/settings_audit.py --check    # exit 1 when a parameter has no decision
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "dev" / "settings-audit.md"
TABLES = ("SoftParam", "GraphParam", "ImportGraphParam", "ManuParam")
#: modules that only describe the parameters (not readers)
NOT_READERS = {"mlaser/services/config.py", "mlaser/services/settings_catalog.py", "mlaser/ui/params.py",
               "mlaser/formats/params.py", "mlaser/services/settings_homes.py"}
HOMES = ROOT / "mlaser" / "services" / "settings_homes.py"

MC = "move: Machine settings › Motion, laser point and process control (hw.options, Service mode)"
NOT_BUILT = "parity-only: {what} is not built in the rebuild; kept so a profile import / export round-trips"

#: (regex on the key, decision) in order; the first match wins (after keep / Machine settings)
RULES: list[tuple[str, str]] = [
    (r"^SOP\.RemoteType$", "parity-only: the wireless pendant is started by its own command (dev.pendant); "
                           "the original's default 3 (LCR19 on) would enable a device the shop may not have"),
    (r"^SOP\.JoystickID\d$", "parity-only: pendant pairing codes, written by the pairing command (hw.remoter_match)"),
    (r"^(SOP\.HardwareType|SP\.m_iHardwareModel)$", "parity-only: the controller model is read from the card"),
    (r"^SP\.LimitDeccLengthRatio$", "parity-only: no reader (the card's limit deceleration uses SP.LimitDeccFactor)"),
    (r"^(SP\.(MachineID|DataCardID|CommandID)|SOP\.(EnableRemoteMonitor|RemoteMonitorHeartbeat|MonitorStartID|"
     r"MonitorEndID|MonitorTestInterval))$",
     "drop: the vendor's cloud remote monitor is not part of mlaser; the LAN monitor (Settings › Run & Queue) "
     "and the automation API replace it"),
    (r"^(SOP|MP)\.ShowManufacturerName$|^MP\.CustomCompanyName$", "drop: vendor branding"),
    (r"^MP\.CustomCompanyPassword$", "drop: a vendor password; mlaser has no vendor passwords (user modes use "
                                     "the owner's PINs, docs/user/shell.md)"),
    (r"^MP\.EnableOpenOperatePermission$", "drop: replaced by the user modes (with an Engineer PIN the "
                                           "application starts in Operator mode)"),
    (r"^SOP\.IsStartNeedGoOrigin$", "move: the safety guard's `require_homed` policy (docs/SAFETY.md: always "
                                    "on for a real card, not a preference)"),
    (r"^(SOP\.(ManuCount|PlanManuCount|TotalManuCount|AfterPlanFinishedActionType))$",
     "move: Diagnostics › processing count (mlaser.diag.batch, services.production)"),
    (r"^SP\.Interfere", "move: Diagnostics › Laser interferometer panel (aux.interferometer)"),
    (r"^SOP\.Device", "parity-only: a counter of the device report (state written by mlaser.diag.devicereport, "
                      "not a setting)"),
    (r"^SP\.UIDir$", "drop: the shell's layout (rail, docks, saved per workspace) replaces the ribbon orientation"),
    (r"^SP\.AlarmPanel", "drop: the safety banners keep fixed colours with text (docs/SAFETY.md)"),
    (r"^SP\.Enable(General|Debug)Log$", "drop: the log panel and the machine audit log are always on"),
    (r"^SP\.(DualCheck|AutoStopCheck|EnableDualFindHome)", "parity-only: the gantry dual-drive check is a card "
                                                           "parameter (Machine settings › axes, dual_*)"),
    (r"^SP\.m_iEnableLaserType$", "move: Machine › laser type (machine.laser_type; state)"),
    (r"^SP\.DrawblkColor$", "drop: the canvas colours come from the theme (contrast-checked layer colours)"),
    (r"^SP\.DxfReadUnit$", "move: Units service (units.*: the unit of a DXF without $INSUNITS; Units track)"),
    (r"^SP\.BaudRate$", "parity-only: no serial device of the rebuild uses it"),
    (r"^SP\.(Card|ZF|Laser)(IPAdd|IPAddtInt|SubnetMask|DefaultGateway)",
     "move: Machine › Network addresses (hw.set_ip, the `ip` setting); the registry default \"1\" is a packed "
     "integer, not an address"),
    (r"^GP\.(CoolPostionDelay|PreDrillMaxNum)$", "parity-only: no reader (pierce and cooling belong to the "
                                                 "technology tables)"),
    (r"^GRP\.(IsOnlyForSelectContour|OutsideIsNegativeSide)$",
     "parity-only: the lead applies to the selection (all parts without one); the kerf side is per part"),
    (r"^GRP\.LoopGapOverCutLength$", "move: Craft › Gap / overcut dialog (ext_craft; per job)"),
    (r"^GRP\.MicroLinkLength$", "move: the manual micro-joint tool (per job)"),
    (r"^GRP\.(Array|RowGap|ColGap|RowDir|ColDir|OffsetType$)", "move: Array dialog (ext_cam; per job)"),
    (r"^GRP\.RotateAngle_deg$", "move: Rotate dialog (per job)"),
    (r"^GRP\.(AutoMicoLinkDir|IsMicoLinkEncloseGraphHead)$", "parity-only: no reader (the joints follow the path)"),
    (r"^GRP\.BridgeWidth$", "move: Bridge tool dialog (per job)"),
    (r"^GRP\.OneKey", "move: Craft › One-key planning dialog (ext_craft)"),
    (r"^GRP\.(SmoothAccuracy|OverlapGate)$", "parity-only: duplicates of the import values (the Optimize "
                                             "dialog uses import.*)"),
    (r"^GRP\.OffsetDist$", "move: technology (the kerf width comes from the technology table per material "
                           "and thickness; Kerf dialog per part)"),
    (r"^GRP\.(ArcRound|UnloadAngle|IsLeadPtCool|IsPeekPtCool|PeekAngle|Alpha)", "move: the corner tool dialogs "
                                                                                 "(ext_cam: fillet, unload loop, "
                                                                                 "cool point, α corner; per job)"),
    (r"^GRP\.(CircleFly|FirstDoneSingleContourCircleScan|LineFly)", "move: Fly-cut dialogs (ext_cam; per job)"),
    (r"^GRP\.(Fill|FS_)", "move: Fill dialogs (ext_cam; per job)"),
    (r"^GRP\.(Max|Min)SizeError", NOT_BUILT.format(what="the size-error auto offset")),
    (r"^GRP\.Add(Part|Sheet)Num$", "drop: quantities of one nest (Nest workspace)"),
    (r"^GRP\.ManualConnectGate$", "move: Draw › Merge dialog (ext_draw; per job)"),
    (r"^GRP\.BallArm", "move: Diagnostics › Ball-arm dialog (aux.ball_arm)"),
    (r"^GRP\.AdvText", "move: Draw › Advanced text dialog (per job)"),
    (r"^GRP\.(Brush|Clean|BeforeCleanZGoOrigin|AfterCleanReturnOriginPt|CloseZFTouchSheetWarning|AfterCleanZF)",
     NOT_BUILT.format(what="the nozzle cleaning station")),
    (r"^GRP\.(Edge|FastEdge|SlowEdge|EnableFastEdgeSeekMode)", NOT_BUILT.format(what="this edge-seek variant")),
    (r"^GRP\.DualServoCalib", NOT_BUILT.format(what="the dual-servo calibration")),
    (r"^GRP\.(IsAutoStartAfterRotatePlatform|RotatePlatform)", NOT_BUILT.format(what="the rotary exchange platform")),
    (r"^GRP\.(EnableBatchCut|BatchCut|EnableDIInput)", "move: Diagnostics › Batch cut dialog (diag.batch)"),
    (r"^GRP\.(.*RollSheet|.*Roll|CutOff|BeforeManuDock|PerRollSheetOffset|EnableZFGoOriginBeforeRollSheet|"
     r"UnlimitRoll)", NOT_BUILT.format(what="coil (roll sheet) feeding")),
    (r"^GRP\.(min|scan|GCode|AutoScanEngrave|FiberScan|CO2Scan)", "parity-only: the rebuild's engraving "
                                                                   "(mlaser.engrave) has its own per-job "
                                                                   "parameters"),
    (r"^GRP\.RectShare", "move: Rectangle common-edge dialog (ext_cam; per job)"),
    (r"^QR\.", "move: Draw › QR code dialog (per job)"),
    (r"^IGP\.AutoSortType$", "parity-only: the DXF import reproduces the original's order (IMPORT_SORT_MODE = 5, "
                             "parity); the cutting order is the Sort commands' (cam.sort_mode)"),
    (r"^IGP\.(LongContour|AutoSplitThreshold|UIRefreshCycle)", "drop: drawing-performance knobs of the "
                                                               "original's canvas"),
    (r"^IGP\.EnableRuler$", "drop: the canvas has no rulers (the Units track owns ruler display)"),
    (r"^IGP\.EnableReadGraphColor$", "parity-only: the DXF import maps colours to layers by the original's rules"),
    (r"^IGP\.IsAutoSmoothSpline$", "parity-only: splines are flattened at geom.SPLINE_TOL; Optimize smooths"),
    (r"^MSC\.(RecenFileIndex|LatestFilePath)$", "drop: recent files belong to the platform's menus; the last "
                                                "drawing is app.last_file"),
    (r"^MSC\.Test", "drop: factory test fields"),
    (r"^MSC\.(MarkPtStr|DockPtType|ToolCoordPtStr|CoordSysType)$",
     "move: Machine › coordinates (controller state, set from the Machine workspace)"),
    (r"^MC\.MarkPtIndex$", "move: Machine › Run control panel (the selected mark point; state)"),
    (r"^MC\.MaintainParamStr$", "move: Diagnostics › Maintenance dialog"),
    (r"^MC\.BatchCutFileTableStr$", "move: Diagnostics › Batch cut dialog (its file list)"),
    (r"^MC\.RollSheetFileTableStr$", NOT_BUILT.format(what="coil (roll sheet) feeding")),
    (r"^(EC\.ECType|MP\.CompensateType)$", "move: Diagnostics (EtherCAT monitor, interferometer compensation)"),
    (r"^MC\.(IsStepMove|StepLength)$", "move: Machine › jog panel (the step chooser)"),
    (r"^MC\.(AutoHomeAfterStop|IsReturnAfterManu|ReturnPtTypeAfterManu)$",
     "parity-only: the rebuild never moves the machine on its own after a job or a stop (no unrequested "
     "motion); the original's default (on) is deliberately not followed"),
    (r"^(MC\.GasType|GC\.DefaultGasPressure)$", "parity-only: the manual gas output uses the technology's gas"),
    (r"^UN\.|^MC\.UnitType$", "move: Units service (units.*; the value is migrated once when the Units "
                              "service is present)"),
]
#: decisions of the second registration of a name (``"<table>:<name>"``) that differs from the first
KEY_RULES = {
    "GraphParam:GRP.OffsetType": "move: technology (the kerf side and width are per part and per "
                                 "technology; Kerf dialog)",
    "GraphParam:GRP.ArcRoundRadius": "move: the corner tool dialogs (ext_cam fillet; per job)",
    "GraphParam:GRP.IsOnlyForSelectContour": "parity-only: the lead applies to the selection (all parts without one)",
    "GraphParam:GRP.LoopGapOverCutLength": "move: Craft › Gap / overcut dialog (ext_craft; per job)",
}
DEFAULT_RULE = NOT_BUILT.format(what="this machine or card behaviour")

#: decisions for the kept parameters: where the new key lives
KEEP_HOME = {
    "shell.language": "Settings › General", "app.reopen_last": "Settings › General",
    "app.last_file": "state (the last drawing opened)", "shell.theme": "Settings › Appearance",
    "display.": "Settings › Canvas", "import.": "Settings › Import (the Optimize dialog starts from them)",
    "cam.lead_": "remembered by the Lead dialog; home: Process › Properties",
    "cam.sort_mode": "remembered by the Sort commands", "cam.small_first": "remembered by the Sort commands",
    "cam.mj_": "remembered by the Micro-joint dialog; home: Process › Properties",
    "design.nudge_": "Settings › Canvas", "nest.": "remembered by the Nest workspace (its home)",
}

#: sensible defaults that differ from the original's, with the reason
DEFAULT_NOTES = {
    "SP.Lang": ("\"\" = the system language (else English)",
                "the original's 0 is 简体中文, wrong in an English interface"),
    "SP.Skin": ("light", "one theme setting (shell.theme); SP.Skin and SP.SoftwareThemeType disagreed"),
    "SP.SoftwareThemeType": ("light", "one theme setting (shell.theme)"),
    "GRP.IsShowEncolseWithRed": ("on", "open contours cannot take a kerf or a lead: show them"),
    "GRP.IsShowNO": ("on", "the cutting order is what an operator checks"),
    "GRP.IsShowDirArrow": ("off", "arrows clutter large nests; the start points show the direction"),
    "GRP.StdSheetHeight": ("1500 mm (nest.sheet_height)", "1200 × 2400 portrait did not fit the 3000 × 1500 bed"),
    "GRP.StdSheetWidth": ("3000 mm (nest.sheet_width)", "the bed is landscape 3000 × 1500"),
    "GRP.AutoMicroLinkNum": ("4 (cam.mj_count)", "the rebuild's micro-joint dialog default"),
    "GRP.AutoMicroLinkStep": ("100 mm (cam.mj_distance)", "the rebuild's micro-joint dialog default"),
    "IGP.ConnectGate": ("0.1 mm, range up to 100 mm", "the Optimize dialog accepts up to 100 mm"),
    "SOP.RemoteType": ("not read", "3 = pendant on, for hardware a shop may not have"),
    "MC.IsReturnAfterManu": ("not followed", "no unrequested motion after a job"),
    "MC.AutoHomeAfterStop": ("not followed", "no unrequested motion after a stop"),
    "MS.EnableSoftLimit": ("unchanged (off)", "the guard checks the travel limits regardless (docs/SAFETY.md)"),
}


def option_settings():
    from mlaser.ui import params
    out = []
    for t in TABLES:
        out += [(t, s) for s in params.section(t)]
    return out


def readers(names: set[str]) -> dict[str, set[str]]:
    """{parameter name: modules that read it}: string literals in code (not docstrings or
    comments) and the struct offsets of ``get(0x...)`` readers (controller start-up)."""
    from mlaser.formats.params import param_defs
    by_off: dict[int, str] = {}
    for d in param_defs():
        if d.file in ("HardPara.xml", "ManuPara.xml") and "." in d.key:
            by_off.setdefault(d.struct_offset, d.key)
    out: dict[str, set[str]] = {n: set() for n in names}
    for p in sorted((ROOT / "mlaser").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel in NOT_READERS:
            continue
        src = p.read_text("utf-8")
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenError, SyntaxError):
            continue
        for i, t in enumerate(toks):
            if t.type != tokenize.STRING:
                continue
            j = i - 1
            while j >= 0 and toks[j].type in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT):
                j -= 1
            starts = j < 0 or toks[j].type == tokenize.NEWLINE or (toks[j].type == tokenize.OP and toks[j].string == ":")
            if starts and i + 1 < len(toks) and toks[i + 1].type in (tokenize.NEWLINE, tokenize.NL):
                continue                                   # a docstring
            for m in re.finditer(r"[A-Za-z]+\.[A-Za-z_0-9]+", t.string):
                if m.group(0) in out:
                    out[m.group(0)].add(rel)
        if "by_off" in src:
            for m in re.finditer(r"get\((0x[0-9a-fA-F]+)", src):
                k = by_off.get(int(m.group(1), 16))
                if k in out:
                    out[k].add(rel)
    return out


def doc_refs(names: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {n: [] for n in names}
    for p in sorted((ROOT / "docs" / "re").glob("*.md")) + [ROOT / "docs" / "dev" / "PARITY.md"]:
        txt = p.read_text("utf-8")
        for n in names:
            if n in txt:
                out[n].append(p.relative_to(ROOT).as_posix())
    return out


def decision(key: str) -> tuple[str, str]:
    """(kind, text) of a parameter's decision."""
    from mlaser.services import settings_catalog as C
    from mlaser.ui.params import MACHINE_CONTROL
    name = key.split(":", 1)[-1]
    for old, new, _conv in C.LEGACY:
        if old == key and not new.startswith("units."):
            home = next((v for k, v in KEEP_HOME.items() if new == k or (k.endswith((".", "_")) and new.startswith(k))),
                        "Settings")
            return "keep", f"keep: `{new}` ({home})"
    if key in MACHINE_CONTROL:
        return "move", MC
    if key in KEY_RULES:
        return KEY_RULES[key].split(":", 1)[0], KEY_RULES[key]
    for pat, text in RULES:
        if re.search(pat, name):
            return text.split(":", 1)[0], text
    return "parity-only", DEFAULT_RULE


def new_consumer(key: str) -> str:
    """The module that reads a kept parameter's new key."""
    from mlaser.services import settings_catalog as C
    e = C.BY_KEY.get(key)
    if e is not None:
        return e.consumer.replace("mlaser/", "")
    return {"app.last_file": "ui/__main__.py", "shell.theme": "ui/shell/__init__.py"}.get(key, "ui/settings_apply.py")


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def build() -> tuple[str, dict]:
    from mlaser.ui.i18n import Translator
    tr = Translator()
    rows = option_settings()
    names = {s.key.split(":", 1)[-1] for _t, s in rows}
    rd = readers(names)
    refs = doc_refs(names)
    counts: dict[str, int] = {}
    lines = []
    for table, s in rows:
        name = s.key.split(":", 1)[-1]
        g, label = tr.tr_param(s.label)
        kind, text = decision(s.key)
        counts[kind] = counts.get(kind, 0) + 1
        r = sorted(rd.get(name, ()))
        reader = ", ".join(f"`{x.replace('mlaser/', '')}`" for x in r) if r else "**no reader**"
        if kind == "keep":
            new = text.split("`")[1]
            reader += f"; now `{new}` → `{new_consumer(new)}`"
        orig = f"{g} › {label}" if g else label
        if refs.get(name):
            orig += " (" + ", ".join(x.split("/")[-1] for x in refs[name]) + ")"
        dflt = s.default if s.default not in ("", None) else "–"
        unit = f" {s.unit}" if s.unit else ""
        note = DEFAULT_NOTES.get(name)
        sens = f"{note[0]}: {note[1]}" if note else "unchanged"
        lines.append(f"| {table} | `{s.key}` | {_cell(orig)} | {reader} | {_cell(dflt)}{_cell(unit)} | "
                     f"{_cell(sens)} | {_cell(text)} |")
    head = HEADER.format(n=len(rows), **{k.replace("-", "_"): counts.get(k, 0)
                                         for k in ("keep", "move", "drop", "parity-only")})
    table = ("| Table | Key | Original (group › label) | Read by the rebuild | Default | Sensible default | "
             "Decision |\n|---|---|---|---|---|---|---|\n" + "\n".join(lines) + "\n")
    return head + table + FOOTER, counts


HEADER = """# Settings audit: the original's Options dialog

Generated by `tools/settings_audit.py` (do not edit by hand; run it after changing a decision).

The old `app.options` dialog was a generic grid over four parameter tables of the original
(SoftParam, GraphParam, ImportGraphParam, ManuParam: {n} entries, some registered twice). Most of
them were never read by the rebuild, many defaults made no sense, and the dialog duplicated the
Machine settings and several tool dialogs. `app.options` now opens the Settings window
(`mlaser/ui/settings_dialog.py`, docs/user/settings.md), which shows only application preferences
from `mlaser/services/settings_catalog.py`; each value has one home.

**Summary:** keep {keep} · move {move} · drop {drop} · parity-only {parity_only}.

* **keep**: a typed ConfigService key with a sensible default; the code named in the catalogue reads
  it. A value the user changed in the old dialog (or imported from an original installation) is
  taken into the new key once (`settings_catalog.migrate_legacy`, recorded in `settings.migrated`).
  Per-job values (leads, micro-joints, cutting order) are *remembered* by their dialog, not edited
  in Settings.
* **move**: the parameter belongs elsewhere; the decision names the place. The machine-side ones
  the controller and the job compiler read are in Machine settings (`hw.options` and the Machine
  workspace's hardware panel, section *Motion, laser point and process control*).
* **drop**: removed from every UI; the reason is given.
* **parity-only**: kept in the schema so the original's profile import (Settings import wizard,
  `ConfigService.import_dat`) and the export (`export_dat`) round-trip; never shown.

"Read by the rebuild" scans the code's string literals (comments and docstrings excluded) and the
ParaModule struct offsets the controller's start-up reads. A parameter whose name appears only in a
comment next to a hard-coded default is listed as **no reader**.

## Parameters

"""

FOOTER = """
## ConfigService settings whose defaults were wrong

| Setting | Was | Now | Why |
|---|---|---|---|
| `SP.Lang` (language) | 0 = 简体中文 | `shell.language` = "" (the system language, else English) | an English interface showed Chinese as the language |
| `SP.Skin` / `SP.SoftwareThemeType` | White / Dark (two settings, disagreeing) | one setting, `shell.theme` | the grid's theme conflicted with the application's theme |
| `GRP.StdSheetWidth` × `GRP.StdSheetHeight` | 1200 × 2400 mm | `nest.sheet_width` × `nest.sheet_height` = 3000 × 1500 mm | the bed (canvas table, nest default) is 3000 × 1500 |
| `GRP.IsShowEncolseWithRed` | off | `display.unclosed_red` on | open contours cannot take a kerf offset or a lead-in |
| `plugin.report.currency` | "EUR" while `plugin.ext_process.currency` was "€" | "" = the costing currency | quotes and the ledger showed different currencies |
| `SP.KeyboardMoveStep` | a key outside the schema (never validated, not in the registry) | `design.nudge_step` (0.01–1000 mm) | the arrow-key step was stored under an invented name |
| `IGP.ConnectGate` | range 0.01–0.5 mm | `import.connect_gate` 0.01–100 mm | the Optimize dialog accepts up to 100 mm; a stored value outside the range was dropped on load |
| `SOP.RemoteType` | 3 (LCR19 pendant on) | not read | it would enable a device a shop may not have; the pendant has its own command |
| `MC.IsReturnAfterManu` / `MC.AutoHomeAfterStop` | on | not followed | no unrequested motion after a job or a stop |
| `SP.CardIPAddtInt` and the other addresses | "1" | not shown (Machine › Network addresses) | a packed integer, not an address |
| `MS.EnableSoftLimit` | off | unchanged, Machine settings | safety-related: the guard checks the travel limits regardless (docs/SAFETY.md); not a Settings value |

## Other settings surfaces (one home each)

Every `Setting` has a `home` (`settings` / `machine` / `process` / `nest` / `run` / `hidden`) and
`live` (the rebuild reads it, `mlaser/services/settings_homes.py`); the Settings window lists only
`settings`-home keys, and its search opens the home of the others (`mlaser.ui.homes.edit_in`).

| Surface | Holds | In the Settings window |
|---|---|---|
| Machine › Setup (Machine workspace › Hardware; `hw.options` opens it) | card and machine parameters, the machine-side Options rows, the cutting bed, the parity profile; Service mode | Machine connection page: read-only summary and links; search opens it |
| Safety I/O dialog (`machine.safety_io`) | interlocks and the heartbeat | never here; linked from Machine connection |
| Network addresses (`hw.set_ip`) | card, follower and laser addresses | linked |
| Settings › Costing (`process.rates` opens it) | one rate model `costing.*` for the cost card, quotes, the CLI and the Report ledger | edited here; the laser's rated power is read-only (Machine) |
| Notification settings (`notify.settings`) | channels, secrets, events | read-only summary, "Edit in Notification settings…" |
| Automation API settings (`api.settings`) | the HTTP API and its token | read-only summary, link |
| Optimize (`camx.cleanup`) | a one-off clean-up | starts from the import defaults (Settings › Import), does not change them |
| One-shot tool dialogs (array, fill, fly-cut, bridge, OneKey...) | a one-off operation | Settings › CAM tools shows the last-used values with Reset |
| Plugin manager (`app.plugin_manager`, also `app.plugins`) | install, enable, permissions, the allowlist | linked; third-party plugins' own settings on the Plugins page |
| Lead, Micro-joint, Sort, Nest panel | per-part / per-job values | not shown: they remember their last values |
"""


USER_DOC = ROOT / "docs" / "user" / "settings.md"

USER_HEAD = """# Settings

The application's preferences: **Settings…** in the application menu on macOS (⌘,), **Options…**
in the Tools menu on Windows, **Preferences…** in the Edit menu on Linux (Ctrl+, on both). The
command palette finds it as *Settings* (`app.settings`; `app.options` opens the same window).

* **Pages** on the left (a drop-down above the page when the window is narrow); **search** finds a
  setting on any page by its name, its help text or its key.
* Each setting has its help text under the field. **Modified** marks a value that differs from the
  default; **Reset** next to it, or **Reset page**, goes back to the default. A value outside the
  allowed range is refused with the reason under the field.
* **Applying changes.** On macOS a change applies at once (there is only a Close button). On
  Windows and Linux the changes apply with **OK** or **Apply**; **Cancel** discards them. Most
  changes take effect immediately (the canvas, the theme, the language, the nest defaults...);
  the few that need a restart say *Applies after restart*.
* **User modes.** In Operator mode only the operator's preferences (language, units, display)
  can be changed; the other pages are shown read-only. Engineer mode changes everything here.
* **One home per value.** Settings holds application-wide preferences. Values that belong to the
  machine, a material or technology, a layer or one job have their home elsewhere; where Settings
  shows one of them, it is read-only with an **Edit in …** button that opens its home. Safety
  values (interlocks, heartbeat) are never here: they are in the Safety I/O dialog, linked from
  *Machine connection*.
* Unit-bearing values are shown in your display units (Settings › Units); they are always stored in
  millimetres.
* Values are stored in `config.json` in the user configuration folder (see
  [Importing your Mlaser setup](importing-mlaser-setup.md) for the original program's files).
  When you import an original installation, the Options values you had changed there are taken
  into the matching settings once.

The tables below list every setting: its key (for scripts and `config.json`), its default and
what it does. The developer audit of the original Options dialog is in
[docs/dev/settings-audit.md](../dev/settings-audit.md).
"""


def _window_schema():
    """The schema of a real window (every built-in plugin registered its settings)."""
    import tempfile
    for var in ("MLASER_CONFIG_DIR", "MLASER_APP_DIR", "MLASER_DATA_DIR"):
        os.environ.setdefault(var, tempfile.mkdtemp(prefix="mlaser-audit-"))
    os.environ.setdefault("MLASER_PLAN_PROCESS", "0")
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from mlaser.ui.facade import DemoController
        from mlaser.ui.mainwindow import EXTENSIONS, MainWindow
        w = MainWindow(DemoController(), extensions=EXTENSIONS)
        w._test_no_dialogs = True
        schema = dict(w.config.schema)
        w.close()
        app.processEvents()
        return schema
    except Exception as exc:  # noqa: BLE001 - fall back to the core schema
        print(f"(no window: {exc})", file=sys.stderr)
        return None


def user_doc() -> str:
    from mlaser.services import settings_catalog as C
    from mlaser.services.config import ConfigService
    from mlaser.ui.i18n import Translator
    tr = Translator()
    schema = dict(_window_schema() or ConfigService(load=False).schema)
    for e in C.ENTRIES:            # plugin settings are registered by their plugins: from the code
        if e.key not in schema:
            schema[e.key] = None

    def t(key: str, default: str = "") -> str:
        return tr.tr(key) if tr.has(key) else default

    def dflt(e) -> str:
        st = schema.get(e.key)
        if st is None:
            return "(the plugin's)"
        v = st.default
        if st.kind == "json":
            return "the tool's defaults"
        if st.kind == "bool":
            return "on" if v else "off"
        if e.choices:
            return dict((cv, t(k, str(cv))) for cv, k in e.choices).get(v, str(v))
        if st.kind == "enum" and st.options:
            o = st.options[int(v)]
            return t(o, str(o))
        if v in ("", None):
            return "(empty)" if e.key != "shell.language" else "system language"
        return f"{v:g} {st.unit}".strip() if isinstance(v, float) else f"{v} {st.unit}".strip()

    out = [USER_HEAD]
    for pid, title in C.PAGES:
        out.append(f"\n## {t(title, pid)}\n")
        es = C.entries_for(pid)
        if pid in C.PAGE_NOTES:
            out.append(t(C.PAGE_NOTES[pid]) + "\n")
        if pid == "general":
            out.append("Also on this page: the display units (metric or imperial, and per-quantity "
                       "overrides: the Units service's `units.*` settings). Stored values never change, "
                       "only what is shown and typed.\n")
        if pid == "users":
            out.append("The current user mode and which PINs are set, with *Change user mode…* and "
                       "*Set PINs…* (Service mode). See [The window](shell.md).\n")
        if pid == "machine":
            out.append("A read-only summary (connection, controller, card address, machine profile, "
                       "user mode) and links to the Machine workspace, Machine settings, the network "
                       "addresses and the Safety I/O dialog.\n")
        if pid == "plugins":
            out.append("The settings of installed third-party plugins, and a link to the Plugin "
                       "manager ([Plugins](plugins.md)).\n")
        if not es:
            continue
        out.append("| Setting | Key | Default | What it does |\n|---|---|---|---|")
        for e in es:
            label = t(e.label_key) or t((schema.get(e.key).label if schema.get(e.key) else "") or "", e.key)
            notes = []
            if e.min_mode == "operator":
                notes.append("Operator mode can change it.")
            if e.restart:
                notes.append("Applies after restart.")
            if e.home:
                notes.append(f"Read-only here: {t(e.home_label, e.home).rstrip('…')}.")
            text = " ".join([t(e.help_key)] + notes)
            out.append(f"| {_cell(label)} | `{e.key}` | {_cell(dflt(e))} | {_cell(text)} |")
        out.append("")
    out.append("""
## Where is each setting?

Every value has one home; the Settings window's search also finds values that live elsewhere
and opens their home.

| Kind of value | Its home | In Settings |
|---|---|---|
| Application preferences (language, units, theme, canvas, import defaults, costing, network, notifications, plugins, user modes) | **Settings** | edited here |
| Card and machine parameters (axes, homing, laser, gas, follower, I/O, motion and process rules, edge seek, coordinates, the cutting bed's slats, the machine profile) | **Machine › Setup** (Service mode) | read-only summary on *Machine connection*; search opens Machine › Setup |
| Interlocks and the heartbeat | **Machine › Safety I/O** | never shown; linked |
| Cutting technology (speed, power, gas, pierce, lead and kerf defaults per material) | **Process › Library** | search opens Process |
| Layer values | **Process › Layers** | search opens Process |
| One part's leads, kerf, micro-joints, start point, cooling | **Process › Properties** | not shown |
| One job's nest (spacing, edge, rotation, sheet) and cutting order | **Nest** / **Process** | not shown (the dialogs and panels remember their last values) |
| A one-shot tool's parameters (array, fill, fly-cut...) | the tool's dialog | *CAM tools* shows the last-used values with Reset |
| Notification channels and the automation API | their settings dialogs (with the passwords and tokens) | read-only, with *Edit in …* |

`app.last_file` records the last drawing opened (for *Open the last drawing at start*).
""")
    return "\n".join(out)


def homes_module() -> str:
    """mlaser/services/settings_homes.py: the registry keys the rebuild reads (``Setting.live``)."""
    from mlaser.services.config import registry_settings
    reg = registry_settings()
    names = {k.split(":", 1)[-1] for k in reg}
    rd = readers(names)
    live = sorted(k for k in reg if rd.get(k.split(":", 1)[-1]))
    body = "\n".join(f'    "{k}",' for k in live)
    return ('"""Generated by tools/settings_audit.py: the registry parameters the rebuild reads (a string\n'
            "literal in code or a struct offset of the controller's start-up), ``Setting.live``.  The\n"
            "other registry rows are kept for the original's files (import / export round-trip) and are\n"
            'hidden from every settings surface (docs/dev/settings-audit.md)."""\n\n'
            f"LIVE_KEYS: frozenset[str] = frozenset({{\n{body}\n}})\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only check that every parameter has a decision")
    args = ap.parse_args(argv)
    text, counts = build()
    if args.check:
        total = sum(counts.values())
        print(f"settings audit: {total} parameters, " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0 if set(counts) <= {"keep", "move", "drop", "parity-only"} else 1
    OUT.write_text(text, "utf-8")
    HOMES.write_text(homes_module(), "utf-8")
    USER_DOC.write_text(user_doc(), "utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.exit(main())
