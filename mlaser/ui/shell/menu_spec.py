"""The menu bar as data: which command goes into which menu, per platform (Qt-free).

:func:`layout` takes the registered command ids and the platform and returns the menus::

    [Menu("file", "shell.menu.file", [Item("file.new"), SEP, Sub("shell.menu.open_recent", ...)...]), ...]

* every registered command appears **once** (a ribbon button with a drop-down becomes a
  submenu whose first item is the button's own command); commands no menu names explicitly
  go to the menu of their prefix (:data:`PREFIX_MENU`) or, failing that, to Tools, so a
  plugin's commands are in the menu bar too;
* the platform decides the conventions (docs/dev/NATIVE.md, ui_audit DESIGN.md §8):
  macOS moves About, Settings…, Plugins… and Quit into the application menu through menu
  roles (Qt adds Services, Hide, Hide Others, Show All); Windows has Options… under Tools and
  Exit at the end of File; Linux has Preferences… under Edit and Quit at the end of File;
* the menus are File, Edit, View, Draw, Process, Workspace, Machine, Tools, Window, Help.

The command palette (Ctrl+K / ⌘K, Help > Search commands) stays: the menus are for discovery,
the palette for speed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Union

MACOS, WINDOWS, LINUX = "macos", "windows", "linux"
SEP = "-"

#: menu roles (QAction.MenuRole names; only macOS acts on them)
ABOUT, PREFERENCES, QUIT, APP_SPECIFIC = "AboutRole", "PreferencesRole", "QuitRole", "ApplicationSpecificRole"


@dataclass
class Sub:
    title: str                       # lang id of the submenu title ("" = the head command's label)
    items: list = field(default_factory=list)
    head: str = ""                   # the command whose label titles the submenu (ribbon drop-down)
    id: str = ""                     # object name / dynamic content ("recent", "theme", ...)


@dataclass
class Menu:
    id: str
    title: str                       # lang id
    items: list = field(default_factory=list)


Entry = Union[str, Sub]


# ---------------------------------------------------------------------------- the menus
def _menus(plat: str) -> list[Menu]:
    file_end: list[Entry] = [SEP, "app.exit"] if plat != MACOS else ["app.exit"]
    file_ = Menu("file", "shell.menu.file", [
        "file.new", "file.open", Sub("shell.menu.open_recent", id="recent"), "file.import", SEP,
        "file.save", "file.save_as", SEP,
        "file.open_project", "file.save_project", "file.export_chf", SEP,
        Sub("shell.menu.import_more", ["file.bitmap", "file.import_scan_gcode", "file.task_import",
                                       "file.task_save", "import.review", "parts.review"]),
        Sub("shell.menu.orders", ["orders.add_files", "orders.import_csv", "orders.template", "orders.to_nest"]),
        Sub("shell.menu.parameters", ["param.backup", "param.backup_hw", "param.backup_file", SEP,
                                      "param.restore", "param.restore_hw", "param.restore_file", SEP,
                                      "shell.import_params", "app.import_language"]),
        *file_end,
    ])
    edit_items: list[Entry] = [
        "edit.undo", "edit.redo", SEP, "edit.copy", "edit.paste", "edit.delete", SEP,
        "select.all", "select.invert",
        Sub("shell.menu.select", ["select.unclosed", "select.similar", "select.inner", "select.outer",
                                  "tool.group_select_all"]),
        SEP,
        Sub("", head="xform.size", items=["xform.scale_10", "xform.scale_30", "xform.scale_50", "xform.scale_75",
                                          "xform.scale_200", "xform.scale_500", SEP, "xform.size_10mm",
                                          "xform.size_50mm", "xform.size_100mm", "xform.size_200mm", SEP,
                                          "xform.scale_dialog"]),
        Sub("", head="xform.rotate", items=["xform.rot_cw90", "xform.rot_ccw90", "xform.rot_180", "xform.rot_any"]),
        "xform.mirror_h", "xform.mirror_v",
        Sub("", head="xform.align", items=["xform.align_left", "xform.align_right", "xform.align_hcenter",
                                           "xform.align_top", "xform.align_bottom", "xform.align_vcenter",
                                           "xform.align_center"]),
        SEP,
        Sub("", head="tool.group", items=["tool.group_explode", "tool.group_explode_all"]),
        "geom.merge", "geom.explode", "geom.split", "geom.smooth",
        "props.show",
    ]
    if plat == LINUX:
        edit_items += [SEP, "app.options"]
    edit = Menu("edit", "shell.menu.edit", edit_items)
    view = Menu("view", "shell.menu.view", [
        "view.zoom_in", "view.zoom_out", "view.zoom_fit", SEP,
        "view.select_mode", "view.pan_mode", "view.node_edit", SEP,
        "view.fit_table", SEP,
        "show.toolpath", "show.bbox", "show.unclosed_red", "show.index", "show.start", "show.direction",
        "show.move_path", SEP,
        "preview.play", "preview.clear", "run.simulate", "sim.speed", "sim.stop", SEP,
        Sub("shell.menu.appearance", id="theme"),          # quick toggle; the editor: Settings › Appearance
        "shell.classic_ribbon",
        Sub("shell.mode.title", ["shell.mode_operator", "shell.mode_engineer", "shell.mode_service", SEP,
                                 "shell.user_mode", "shell.set_pins"], id="mode"),
    ])
    draw = Menu("draw", "shell.menu.draw", [
        "draw.line", "draw.pline", "draw.rect", "draw.round_rect", "draw.polygon", "draw.star", SEP,
        "draw.circle", "draw.circle3", "draw.arc3", "draw.scan_arc", "draw.ellipse", "draw.curve", "draw.point", SEP,
        "draw.text", "draw.adv_text", "draw.advert_text", "draw.qr", SEP,
        "draw.trim", "draw.extend", "draw.linear_fill", "draw.fine_tune", SEP,
        "tool.measure",
    ])
    process = Menu("process", "shell.menu.process", [
        "process.show", "process.library", "nav.layer_parameters", "process.apply", "process.rates", SEP,
        Sub("", head="craft.lead", items=["craft.lead_manual", "craft.lead_check", "craft.lead_clear_mark",
                                          "craft.lead_clear", SEP, "cam.lead_place", "cam.lead_check",
                                          "cam.lead_fix", "cam.tip_check", "cam.tip_fix"]),
        "craft.start_point", "craft.home_ref",
        Sub("", head="craft.kerf", items=["craft.kerf_set", "craft.kerf_cancel", "craft.kerf_clipper", SEP,
                                          "craft.inside", "craft.outside", "craft.circle_cut"]),
        Sub("", head="craft.microjoint", items=["craft.mj_manual", "craft.mj_auto", "craft.mj_params",
                                                "craft.mj_explode", "craft.mj_clear"]),
        "craft.reverse",
        Sub("", head="craft.seal", items=["craft.gap", "craft.overcut"]),
        Sub("", head="craft.cool_point", items=["craft.cool_auto", "craft.cool_clear"]),
        Sub("", head="craft.chamfer", items=["craft.chamfer_manual", "craft.chamfer_auto"]),
        "craft.unload", SEP,
        Sub("", head="sort.run", items=["sort.small_first", "sort.left_right", "sort.right_left", "sort.bottom_top",
                                        "sort.top_bottom", "sort.nearest", "sort.shortest", "sort.grid",
                                        "sort.in_out", "sort.out_in", "sort.layer_first", "sort.layer_last",
                                        "sort.keep", SEP, "sort.first", "sort.last", "sort.avoid"]),
        Sub("", head="sort.manual", items=["sort.manual_prev", "sort.manual_end"]),
        SEP,
        "tool.array", Sub("shell.menu.array_more", ["camx.array_circular", "camx.array_sheet", "camx.rect_share"]),
        Sub("", head="tool.scan_cut", items=["tool.scan_circle", "tool.scan_line", "tool.engrave"]),
        Sub("", head="tool.share_edge", items=["tool.share_edge_c"]),
        "tool.bridge",
        Sub("", head="tool.optimize", items=["opt.smooth", "opt.segment", "opt.dedup", "opt.join",
                                             "opt.replace_circle", "opt.adjust_start", SEP, "camx.cleanup",
                                             "camx.fill_circle", "camx.alpha", "camx.alpha_clear"]),
        "tool.onekey", SEP,
        Sub("shell.menu.labels", ["labels.part_labels", "labels.etch_part_ids", "labels.remnant_labels",
                                  "labels.job_tickets", "labels.remove_marking"]),
        SEP,
        Sub("shell.menu.nest", ["nest.show", "nest.new", "nest.open", "nest.save", "nest.save_as", "nest.export", SEP,
                                "nest.import_part", "nest.add_part", "nest.import_sheet", "nest.add_sheet", SEP,
                                "nest.params", "nest.run", "nest.simple", "nest.to_main"]),
    ])
    workspace = Menu("workspace", "shell.menu.workspace", [])          # filled by layout()
    machine = Menu("machine", "shell.menu.machine", [
        "machine.workspace", SEP, "hw.connect_sim", "hw.reconnect", "hw.set_ip", "hw.net_test", SEP,
        Sub("", head="machine.home", items=["machine.home_zf_then_xy", "machine.home_xyz", "machine.home_x",
                                            "machine.home_y", "machine.home_af"]),
        Sub("", head="machine.edge_seek", items=["machine.edge_disc", "machine.edge_capacitive",
                                                 "machine.edge_two_point", "machine.edge_clear"]),
        Sub("", head="coord.select", items=["coord.float", "coord.tool1", "coord.tool2", "coord.tool3", "coord.tool4",
                                            "coord.tool5", "coord.tool6", "coord.manual"]),
        "coord.set_origin", "coord.return_origin", SEP,
        "run.frame", "run.dry_run", "run.start", "run.hold", "run.stop", "run.arm", SEP,
        "card.break_resume", "card.alarms", "card.clear_alarms", "queue.add_current", "queue.confirm_sheet",
        "job.offline", SEP,
        Sub("", head="machine.laser_type", items=["machine.laser_fiber", "machine.laser_co2"]),
        "dev.laser", "dev.follower", "dev.pendant", "hw.remoter_match", SEP,
        Sub("shell.menu.wizards", ["wizard.commissioning", "wizard.laser_test", "wizard.focus_home",
                                   "wizard.follower_calibration", "wizard.edge_seek_disc", "wizard.edge_seek_cap3",
                                   "wizard.edge_seek_cap4", "wizard.edge_seek_two_point"]),
        Sub("shell.menu.monitors", ["mon.controller", "mon.ftc", "mon.af", "mon.ext", "monitor.toggle"]),
        Sub("shell.menu.auxiliary", ["aux.interferometer", "aux.ball_arm", "aux.burn_in", "aux.light_test",
                                     "aux.batch_cut", "aux.maintain", "aux.custom_io", "aux.custom_io_nl",
                                     "aux.dedust"]),
        SEP,
        "machine.setup", "hw.options", "card.params", "machine.safety_io", "machine.import_install", "card.stream_process",
        Sub("", head="hw.upgrade", items=["hw.upgrade_ftc", "hw.upgrade_ext"]),
        "hw.restart",
    ])
    tools_items: list[Entry] = [
        Sub("shell.menu.diagnostics", ["diag.log", "diag.log_dir", "diag.device_report", "diag.measure",
                                       "diag.work_report", "diag.work_report_all", "app.netcheck"]),
        Sub("shell.menu.automation", ["api.toggle", "api.settings", "notify.settings", "notify.test"]),
        "practice.open", "app.recovery", SEP, "app.profile", "app.dxf_modern", "app.plugins",
    ]
    if plat != MACOS:
        tools_items += ["app.plugin_manager"]
    if plat == WINDOWS:
        tools_items += [SEP, "app.options"]
    tools = Menu("tools", "shell.menu.tools", tools_items)
    window = Menu("window", "shell.menu.window", [
        "window.minimize", "window.zoom", "window.fullscreen", SEP,
        Sub("shell.menu.panels", id="docks"), "shell.reset_layout",
    ])
    help_items: list[Entry] = ["shell.palette", SEP, "app.help", "shell.docs", "shell.shortcuts", "shell.welcome"]
    if plat != MACOS:
        help_items += [SEP, "app.about"]
    help_ = Menu("help", "shell.menu.help", help_items)
    app_menu: list[Entry] = ["app.about", "app.options", "app.plugin_manager", "app.exit"] if plat == MACOS else []
    out = [file_, edit, view, draw, process, workspace, machine, tools, window, help_]
    if app_menu:
        # macOS: these move to the application menu (roles); kept in Help/File so Qt can find
        # them in a menu (Qt merges them from wherever they are)
        help_.items += [SEP, "app.about", "app.options", "app.plugin_manager"]
    return out


#: menu entries that go to the one home of a setting (mlaser/ui/homes.py, ia/SETTINGS_MAP.md)
#: instead of opening the command's own editor: command id (or a menu-only ``nav.*`` id) ->
#: (value id, needs a part selection, label lang id or "" for the command's own label).
#: Once a command's handler navigates itself (WORK_ITEMS P-6 / S-6) its entry here is moot.
NAV: dict[str, tuple[str, bool, str]] = {
    "nav.layer_parameters": ("layer.GP.CutSpeed", False, "shell.menu.layer_parameters"),
    "craft.lead": ("craft.lead_in.kind", True, ""),
    "craft.kerf": ("craft.kerf_width", True, ""),
    "craft.microjoint": ("craft.micro_joint.count", True, ""),
    "hw.options": ("cfg:machine.setup", False, ""),
    "nest.params": ("nest.spacing", False, ""),
}
#: the command whose user-mode gate a menu-only entry follows
NAV_GATE = {"nav.layer_parameters": "process.apply"}

#: menu for commands no menu names explicitly (plugins, new commands), by id prefix
PREFIX_MENU = {
    "file": "file", "edit": "edit", "select": "edit", "xform": "edit", "geom": "edit",
    "view": "view", "show": "view", "preview": "view",
    "draw": "draw",
    "craft": "process", "cam": "process", "camx": "process", "sort": "process", "opt": "process",
    "tool": "process", "process": "process", "nest": "process", "labels": "process",
    "workspace": "workspace",
    "machine": "machine", "hw": "machine", "coord": "machine", "card": "machine", "run": "machine",
    "job": "machine", "queue": "machine", "dev": "machine", "mon": "machine", "monitor": "machine",
    "aux": "machine", "wizard": "machine", "param": "file",
    "window": "window", "shell": "help", "app": "tools",
}

#: commands that are not menu items (the theme submenu lists the theme commands itself)
DYNAMIC_PREFIXES = ("shell.theme_",)


def roles(plat: str, settings_cmd: str = "app.options") -> dict[str, str]:
    """command id -> menu role (macOS application menu)."""
    if plat != MACOS:
        return {}
    return {"app.about": ABOUT, settings_cmd: PREFERENCES, "app.exit": QUIT, "app.plugin_manager": APP_SPECIFIC}


#: menu words for commands whose ribbon label names an old editor (ia/SETTINGS_MAP.md: the
#: menus point at the single home of each setting)
MENU_LABELS = {"hw.options": "shell.menu.machine_setup"}


#: per-platform label overrides (lang ids): the platform's own words for the same command
def platform_labels(plat: str, settings_cmd: str = "app.options") -> dict[str, str]:
    return {**MENU_LABELS, **_platform_labels(plat, settings_cmd)}


def _platform_labels(plat: str, settings_cmd: str) -> dict[str, str]:
    if plat == MACOS:
        return {settings_cmd: "shell.menu.settings_mac", "app.exit": "shell.menu.quit", "app.about": "shell.menu.about",
                "app.plugin_manager": "shell.menu.plugins"}
    if plat == WINDOWS:
        return {settings_cmd: "shell.menu.options_win", "app.exit": "shell.menu.exit", "app.about": "shell.menu.about",
                "window.zoom": "shell.window.maximize"}
    return {settings_cmd: "shell.menu.preferences", "app.exit": "shell.menu.quit", "app.about": "shell.menu.about",
            "window.zoom": "shell.window.maximize"}


def _ids(entries: Iterable[Entry]) -> list[str]:
    out = []
    for e in entries:
        if isinstance(e, Sub):
            if e.head:
                out.append(e.head)
            out.extend(_ids(e.items))
        elif e != SEP:
            out.append(e)
    return out


def _filter(entries: list[Entry], known: set[str], seen: set[str]) -> list[Entry]:
    """Keep known commands, each once; drop empty submenus and doubled / edge separators."""
    out: list[Entry] = []
    for e in entries:
        if isinstance(e, Sub):
            items = _filter(e.items, known, seen)
            head = e.head if e.head in known and e.head not in seen else ""
            if head:
                seen.add(head)
            if head and not items:
                out.append(head)                 # a drop-down without its children: a plain item
            elif items or e.id:
                out.append(Sub(e.title, items, head, e.id))
        elif e == SEP:
            if out and out[-1] != SEP:
                out.append(SEP)
        elif e in known and e not in seen:
            seen.add(e)
            out.append(e)
    while out and out[-1] == SEP:
        out.pop()
    return out


def layout(commands: Iterable[str], plat: str, settings_cmd: Optional[str] = None) -> list[Menu]:
    """The menus for the registered ``commands`` on ``plat`` (see the module docstring)."""
    known = {c for c in commands if not c.startswith(DYNAMIC_PREFIXES)}
    known |= {c for c in NAV if c.startswith("nav.") and NAV_GATE.get(c, "") in known}
    menus = _menus(plat)
    if settings_cmd and settings_cmd != "app.options":
        # the Settings track's command takes the place of app.options
        def swap(entries: list[Entry]) -> list[Entry]:
            out: list[Entry] = []
            for e in entries:
                if isinstance(e, Sub):
                    out.append(Sub(e.title, swap(e.items), settings_cmd if e.head == "app.options" else e.head, e.id))
                else:
                    out.append(settings_cmd if e == "app.options" else e)
            return out
        menus = [Menu(m.id, m.title, swap(m.items)) for m in menus]
        if "app.options" in known:
            # both exist: app.options stays reachable under Tools
            for m in menus:
                if m.id == "tools":
                    m.items = m.items + [SEP, "app.options"]
    seen: set[str] = set()
    # the application-menu commands of macOS first, so their place is the one with the role
    out = [Menu(m.id, m.title, _filter(m.items, known, seen)) for m in menus]
    by_id = {m.id: m for m in out}
    # Workspace: the built-in six in rail order (their ⌘1…⌘6), then the plugins' workspaces
    builtin = [f"workspace.{w}" for w in ("design", "process", "nest", "run", "report", "machine")]
    workspaces = [c for c in builtin if c in known and c not in seen]
    extra = sorted(c for c in known if c.startswith("workspace.") and c not in seen and c not in workspaces)
    by_id["workspace"].items = workspaces + ([SEP] + extra if extra and workspaces else extra)
    seen.update(workspaces + extra)
    rest = sorted(c for c in known if c not in seen)
    for cid in rest:
        target = PREFIX_MENU.get(cid.split(".", 1)[0], "tools")
        menu = by_id.get(target) or by_id["tools"]
        if not any(isinstance(e, Sub) and e.id == "more" for e in menu.items):
            if menu.items and menu.items[-1] != SEP:
                menu.items.append(SEP)
            menu.items.append(Sub("shell.menu.more", [], id="more"))
        more = next(e for e in menu.items if isinstance(e, Sub) and e.id == "more")
        more.items.append(cid)
        seen.add(cid)
    return out


def placement(menus: list[Menu]) -> dict[str, str]:
    """command id -> menu id (each command once; tests)."""
    out: dict[str, str] = {}

    def walk(mid: str, entries: list[Entry]) -> None:
        for e in entries:
            if isinstance(e, Sub):
                if e.head:
                    if e.head in out:
                        raise ValueError(f"{e.head} twice ({out[e.head]}, {mid})")
                    out[e.head] = mid
                walk(mid, e.items)
            elif e != SEP:
                if e in out:
                    raise ValueError(f"{e} twice ({out[e]}, {mid})")
                out[e] = mid
    for m in menus:
        walk(m.id, m.items)
    return out


def all_ids(menus: list[Menu]) -> list[str]:
    out: list[str] = []
    for m in menus:
        out.extend(_ids(m.items))
    return out


def mnemonic_titles(titles: list[str], first: Iterable[int] = ()) -> list[str]:
    """``&File``, ``&Edit``, ``&View`` ...: a distinct access key per top-level menu (Windows,
    Linux; Alt+letter opens the menu).  A title that already has one keeps it; the menus in
    ``first`` (indexes: File, Edit, View, Window, Help) choose before the others, so they get
    the letters users expect."""
    used: set[str] = set()
    out = list(titles)
    for i, t in enumerate(titles):
        if "&" in t.replace("&&", ""):
            j = t.replace("&&", "\0\0").index("&")
            if j + 1 < len(t):
                used.add(t[j + 1].lower())
    order = [i for i in first if 0 <= i < len(titles)] + [i for i in range(len(titles)) if i not in set(first)]
    for i in order:
        t = titles[i]
        if "&" in t.replace("&&", ""):
            continue
        pick = next((k for k, ch in enumerate(t) if ch.isalnum() and ch.lower() not in used), -1)
        if pick >= 0:
            used.add(t[pick].lower())
            out[i] = t[:pick] + "&" + t[pick:]
    return out


#: menus whose access key is chosen first (the conventional File, Edit, View, Window, Help)
MNEMONIC_FIRST = ("file", "edit", "view", "window", "help")


__all__ = ["Menu", "Sub", "SEP", "MACOS", "WINDOWS", "LINUX", "layout", "placement", "all_ids", "roles",
           "platform_labels", "mnemonic_titles", "PREFIX_MENU"]
