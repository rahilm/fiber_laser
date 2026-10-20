"""Ribbon layout of the original MainApp.exe, as data.

Evidence: the order of ``语言包``/key literal pairs in MainApp.exe .rdata between
0x45681c (``mf7``) and the nest category (``nset_UI_Result2MainView``) is the order in
which the ribbon is built (CMainFrame ribbon init, FUN_004a7d20 and neighbours in
decomp/MainApp.c, which call ``CBCGPRibbonBar::AddCategory`` / ``CBCGPRibbonCategory::AddPanel``
/ ``CBCGPRibbonButton``). Category image lists observed as ``AddCategory(..., 0x161, 0x162)``
etc. map to the PNG strips 348/350, 351/352, 353/354, 355/356, 441/442, 451/452
(32 px / 16 px pairs). The assignment of panels to tabs follows the string order and the
dark-theme tab tiles (RT_BITMAP 458-466: 开始 Start, 绘图 Draw, 工艺 Process, 高级 Advanced,
排样 Nest, 控制器/调高器/电动头/扩展板 monitors).

Each button: ``Btn(command_id, lang_key, icon=None, menu=[...])``. ``icon`` names a Tabler
SVG icon; when it is ``None`` the icon comes from :data:`mlaser.ui.icons.COMMAND_ICONS`, and a
command with no entry there is shown as a text-only button. The vendor's bitmap strips are not
used (docs/ASSETS.md). Menu children are ``(command_id, lang_key)`` pairs. ``priority`` per
command lives in docs/re/ui.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Btn:
    cmd: str
    key: str
    icon: Optional[str] = None      # Tabler icon name; None = icons.COMMAND_ICONS[cmd]
    menu: list[tuple[str, str]] = field(default_factory=list)
    checkable: bool = False
    large: bool = True


@dataclass
class Group:
    key: str
    buttons: list[Btn]


@dataclass
class Tab:
    key: str
    groups: list[Group]


# Application ("File", mf7) menu: mf8 New, mf9 Open, mf13 Import, mf10 Save, mf11 Save as, ...
FILE_MENU: list[tuple[str, str]] = [
    ("file.new", "mf8"), ("file.open", "mf9"), ("file.import", "mf13"),
    ("file.save", "mf10"), ("file.save_as", "mf11"),
    ("file.open_project", "project.open"), ("file.save_project", "project.save"),
    ("file.export_chf", "project.export_chf"), ("-", ""),
    ("file.bitmap", "A250622_0"), ("file.import_scan_gcode", "A251104_0"),
    ("file.task_import", "A250320_1"), ("file.task_save", "A250320_2"), ("-", ""),
    ("param.backup_file", "mf234"), ("param.restore_file", "mf235"), ("-", ""),
    ("app.options", "mf137"), ("app.import_language", "cmd.app.import_language"), ("-", ""),
    ("app.about", "mf24"), ("app.exit", "mf164"),
]

TABS: list[Tab] = [
    Tab("mf6", [  # 开始 Start
        Group("mf7", [
            Btn("file.new", "mf8"),
            Btn("file.open", "mf9"),
            Btn("file.import", "mf13"),
            Btn("file.save", "mf10"),
            Btn("file.save_as", "mf11"),
            Btn("file.bitmap", "A250622_0"),
        ]),
        Group("mf14", [  # Initialize
            Btn("machine.home", "mf15", menu=[
                ("machine.home_zf_then_xy", "mf482"), ("machine.home_xyz", "mf222"),
                ("machine.home_x", "mf16"), ("machine.home_y", "mf17"),
                ("machine.home_af", "A240827_1")]),
            Btn("machine.edge_seek", "mf19", menu=[
                ("machine.edge_disc", "newLang27"), ("machine.edge_capacitive", "A250211_0"),
                ("machine.edge_two_point", "A250211_1"), ("machine.edge_clear", "A250211_3")]),
            Btn("machine.laser_type", "gp120", menu=[
                ("machine.laser_fiber", "A241024_0"), ("machine.laser_co2", "A241024_1")]),
        ]),
        Group("mf307", [  # Coordinate
            Btn("coord.select", "mf307", menu=[
                ("coord.float", "mf300"), ("coord.tool1", "mf301"), ("coord.tool2", "mf302"),
                ("coord.tool3", "mf303"), ("coord.tool4", "mf304"), ("coord.tool5", "mf305"),
                ("coord.tool6", "mf306"), ("coord.manual", "mf513")]),
            Btn("coord.set_origin", "mp129"),
            Btn("coord.return_origin", "mp130"),
        ]),
        Group("mf22", [  # Backup
            Btn("param.backup", "mf210", menu=[("param.backup_hw", "mf480"), ("param.backup_file", "mf234")]),
            Btn("param.restore", "mf211", menu=[("param.restore_hw", "mf481"), ("param.restore_file", "mf235")]),
        ]),
        Group("mf23", [  # Help
            Btn("app.options", "mf137"),
            Btn("app.help", "mf23"),
            Btn("app.about", "mf24"),
        ]),
    ]),
    Tab("mf26_1", [  # Common / view editing (常用)
        Group("mf26_2", [
            Btn("view.select_mode", "mf27", checkable=True),
            Btn("view.node_edit", "mf28", checkable=True),
            Btn("view.pan_mode", "mf29", checkable=True),
            Btn("view.zoom_in", "mf198", large=False),
            Btn("view.zoom_out", "mf199", large=False),
            Btn("view.zoom_fit", "mf200", large=False),
            Btn("view.fit_table", "canvas.cmd.fit_table", large=False),
            Btn("edit.undo", "mf30", large=False),
            Btn("edit.redo", "mf31", large=False),
        ]),
        Group("mf32", [  # Select
            Btn("select.all", "mf33", large=False),
            Btn("select.invert", "mf34", large=False),
            Btn("select.unclosed", "mf35", large=False),
            Btn("select.similar", "mf37", large=False),
            Btn("select.inner", "mf651", large=False),
            Btn("select.outer", "mf652", large=False),
        ]),
        Group("mf38_0", [  # Show
            Btn("show.bbox", "mf38", checkable=True, large=False),
            Btn("show.unclosed_red", "mf39", checkable=True, large=False),
            Btn("show.index", "mf40", checkable=True, large=False),
            Btn("show.start", "mf41", checkable=True, large=False),
            Btn("show.direction", "mf42", checkable=True, large=False),
            Btn("show.move_path", "mf43", checkable=True, large=False),
            Btn("show.toolpath", "canvas.cmd.toolpath", checkable=True, large=False),
        ]),
        Group("mf590", [  # Geometric transformations
            Btn("xform.size", "mf591", menu=[
                ("xform.scale_10", "10%"), ("xform.scale_30", "30%"), ("xform.scale_50", "50%"),
                ("xform.scale_75", "75%"), ("xform.scale_200", "200%"), ("xform.scale_500", "500%"),
                ("xform.size_10mm", "10mm"), ("xform.size_50mm", "50mm"), ("xform.size_100mm", "100mm"),
                ("xform.size_200mm", "200mm"), ("xform.scale_dialog", "mf60")]),
            Btn("xform.align", "mf70", menu=[
                ("xform.align_left", "mf71"), ("xform.align_right", "mf72"), ("xform.align_hcenter", "mf73"),
                ("xform.align_top", "mf74"), ("xform.align_bottom", "mf75"), ("xform.align_vcenter", "mf76"),
                ("xform.align_center", "mf77")]),
            Btn("xform.mirror_h", "mf68", large=False),
            Btn("xform.mirror_v", "mf69", large=False),
            Btn("xform.rotate", "mf61", menu=[
                ("xform.rot_cw90", "mf62"), ("xform.rot_ccw90", "mf63"), ("xform.rot_180", "mf63_1"),
                ("xform.rot_any", "mf66")]),
        ]),
    ]),
    Tab("mf186", [  # 工艺 Process (tile 460; mf860 "Basic Process" panel)
        Group("mf87", [  # Lead line
            Btn("craft.lead", "mf87", menu=[
                ("craft.lead_manual", "mf88"), ("craft.lead_check", "mf89"),
                ("craft.lead_clear_mark", "mf91"), ("craft.lead_clear", "mf90")]),
            Btn("craft.start_point", "mf93"),
            Btn("craft.home_ref", "mf92"),
        ]),
        Group("mf113", [  # Compensate / kerf
            Btn("craft.kerf", "mf113", menu=[
                ("craft.kerf_set", "mf486"), ("craft.kerf_cancel", "mf217")]),
            Btn("craft.inside", "mf98", large=False),
            Btn("craft.outside", "mf99", large=False),
            Btn("craft.circle_cut", "mf487", large=False),
        ]),
        Group("mf100", [  # Micro joint
            Btn("craft.microjoint", "mf100", menu=[
                ("craft.mj_manual", "mf101"), ("craft.mj_auto", "mf103"), ("craft.mj_params", "mf102"),
                ("craft.mj_explode", "mf103_1"), ("craft.mj_clear", "mf103_2")]),
            Btn("craft.reverse", "mf94", large=False),
            Btn("craft.seal", "mf96", menu=[("craft.gap", "mf95"), ("craft.overcut", "mf97")], large=False),
            Btn("craft.cool_point", "mf406", menu=[
                ("craft.cool_auto", "mf407"), ("craft.cool_clear", "mf408")], large=False),
        ]),
        Group("mf400", [  # Corners / geometry ops
            Btn("craft.chamfer", "mf400", menu=[("craft.chamfer_manual", "mf484"), ("craft.chamfer_auto", "mf401")], large=False),
            Btn("craft.unload", "mf404", large=False),
            Btn("geom.merge", "mf84", large=False),
            Btn("geom.explode", "mf85", large=False),
            Btn("geom.split", "mf216", large=False),
            Btn("geom.smooth", "mf213", large=False),
        ]),
        Group("mf105", [  # Sort
            Btn("sort.run", "mf105", menu=[
                ("sort.small_first", "mf204"), ("sort.left_right", "mf106"), ("sort.right_left", "mf107"),
                ("sort.bottom_top", "mf108"), ("sort.top_bottom", "mf109"), ("sort.nearest", "mf110"),
                ("sort.in_out", "mf202"), ("sort.out_in", "mf203"), ("sort.layer_first", "mf610"),
                ("sort.layer_last", "mf611")]),
            Btn("sort.manual", "mf111"),
        ]),
        Group("mf790", [  # Tools
            Btn("tool.array", "mf79"),
            Btn("tool.group", "mf80", menu=[
                ("tool.group_select_all", "mf81"), ("tool.group_explode", "mf82"), ("tool.group_explode_all", "mf83")]),
            Btn("tool.scan_cut", "mf112", menu=[
                ("tool.scan_circle", "mf112_1"), ("tool.scan_line", "mf112_2"), ("tool.engrave", "mf112_5")]),
            Btn("tool.share_edge", "mf250", menu=[("tool.share_edge_c", "mf250_1")]),
            Btn("tool.bridge", "mf114"),
            Btn("tool.measure", "mf116"),
            Btn("tool.optimize", "mf470", menu=[
                ("opt.smooth", "mf474"), ("opt.segment", "mf475"), ("opt.dedup", "mf471"),
                ("opt.join", "mf476"), ("opt.replace_circle", "mf472"), ("opt.adjust_start", "mf604")]),
            Btn("tool.onekey", "mf196"),
        ]),
    ]),
    Tab("mf4401", [  # 绘图 Draw
        Group("mf4402", [
            Btn("draw.line", "mf45", large=False),
            Btn("draw.pline", "mf56", large=False),
            Btn("draw.rect", "mf51", large=False),
            Btn("draw.round_rect", "mf52", large=False),
            Btn("draw.polygon", "mf53", large=False),
            Btn("draw.star", "mf54", large=False),
            Btn("draw.circle", "mf46", large=False),
            Btn("draw.circle3", "mf47", large=False),
            Btn("draw.arc3", "mf48", large=False),
            Btn("draw.scan_arc", "mf49", large=False),
            Btn("draw.ellipse", "mf50", large=False),
            Btn("draw.text", "mf55", large=False),
            Btn("draw.curve", "mf57", large=False),
            Btn("draw.point", "mf58", large=False),
        ]),
        Group("mf129", [
            Btn("draw.trim", "A250707_12", large=False),
            Btn("draw.extend", "A250707_13", large=False),
            Btn("draw.advert_text", "mf254", large=False),
            Btn("draw.adv_text", "mf251", large=False),
            Btn("draw.qr", "A260420_0", large=False),
            Btn("draw.linear_fill", "A250707_10", large=False),
        ]),
        Group("mf445", [
            Btn("draw.fine_tune", "mf445", checkable=True),
        ]),
    ]),
    Tab("mf117", [  # System analysis
        Group("mf118", [  # Monitor
            Btn("mon.controller", "mf119"),
            Btn("mon.ftc", "mf120"),
            Btn("mon.af", "mf249"),
            Btn("mon.ext", "mf620"),
        ]),
        Group("mf125", [  # Diagnostics
            Btn("diag.log", "mf124", menu=[("diag.log_dir", "mf431")]),
            Btn("diag.device_report", "mf801"),
            Btn("diag.measure", "mf229"),
            Btn("diag.work_report", "mf252", menu=[("diag.work_report_all", "mf253")]),
        ]),
        Group("pd312", [  # Process / simulation
            Btn("sim.speed", "mf121"),
            Btn("job.offline", "A241115_1"),
            Btn("sim.stop", "ec13"),
        ]),
    ]),
    Tab("mf127", [  # 高级 Advanced
        Group("mf128", [  # Hardware
            Btn("hw.options", "mf129"),
            Btn("hw.reconnect", "mf130"),
            Btn("hw.connect_sim", "Connect to simulator"),   # rebuild addition (no lang key)
            Btn("hw.restart", "mf131"),
            Btn("hw.upgrade", "mf132", menu=[("hw.upgrade_ftc", "mf440"), ("hw.upgrade_ext", "mf625")]),
            Btn("hw.set_ip", "mf133"),
            Btn("hw.net_test", "mf436"),
            Btn("hw.remoter_match", "mf135"),
        ]),
        Group("mf136", [  # Software
            Btn("app.options", "mf137"),
            Btn("app.recovery", "mf138"),
        ]),
        Group("mf139", [  # Auxiliary
            Btn("aux.interferometer", "mf180"),
            Btn("aux.ball_arm", "mf702"),
            Btn("aux.burn_in", "newLang1"),
            Btn("aux.light_test", "newLang200"),
            Btn("aux.batch_cut", "mf705"),
            Btn("aux.maintain", "A251113_0"),
            Btn("aux.custom_io", "newLang301", menu=[("aux.custom_io_nl", "newLang300")]),
            Btn("aux.dedust", "mf140", checkable=True),
        ]),
    ]),
    Tab("mf460", [  # 排样 Nest
        Group("mf460", [
            Btn("nest.new", "mf_S_File_NewProject"),
            Btn("nest.save", "mf_S_File_SaveProject"),
            Btn("nest.save_as", "mf_S_File_SaveAs"),
            Btn("nest.import_part", "mf461"),
            Btn("nest.import_sheet", "mf473"),
            Btn("nest.run", "mf464"),
            Btn("nest.add_part", "mf465"),
            Btn("nest.to_main", "nset_UI_Result2MainView"),
        ]),
    ]),
]


def all_commands() -> list[str]:
    out: list[str] = [c for c, _ in FILE_MENU if c != "-"]
    for t in TABS:
        for g in t.groups:
            for b in g.buttons:
                out.append(b.cmd)
                out.extend(c for c, _ in b.menu)
    return out
