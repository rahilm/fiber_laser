"""Task workspaces of the Wave 3 shell (docs/review/ux.md §4, docs/dev/PLUGINS.md §5).

The shell has a left rail with six built-in workspace **slots**, in this order:

========  =====  ========================================================  ===========
slot      key    task                                                       min. mode
========  =====  ========================================================  ===========
design    Ctrl+1 draw, import and edit on the canvas (the default)          engineer
process   Ctrl+2 technology, leads, kerf, joints, order, preview            engineer
nest      Ctrl+3 parts, sheets and remnants, nest results                   engineer
run       Ctrl+4 operator screen, queue                                     operator
report    Ctrl+5 production, costs, alarm history                           operator
machine   Ctrl+6 connect, home, calibrate, monitors, settings               operator
========  =====  ========================================================  ===========

A plugin contributes a workspace with ``ctx.add_workspace(name, widget, title, ...)``
(:class:`WorkspaceSpec` lists every option).  **Slot rule**: when ``slot=`` names a built-in
slot, or the workspace ``name`` or ``title`` equals one (case-insensitive: ``"process"``,
``"Run"``), the widget fills that slot and replaces its placeholder.  A second contribution to
the same slot becomes another tab inside it.  Any other workspace gets a rail button of its own
after the six (``order`` 100 and up, Ctrl+7..9 when free).

Qt-free: the widgets are opaque here; :mod:`mlaser.ui.shell` puts them on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

DESIGN, PROCESS, NEST, RUN, REPORT, MACHINE = "design", "process", "nest", "run", "report", "machine"
SLOTS: tuple[str, ...] = (DESIGN, PROCESS, NEST, RUN, REPORT, MACHINE)


@dataclass
class WorkspaceSpec:
    """One task workspace.

    * ``id``: ``"design"`` ... for the slots, ``"<plugin>.<name>"`` for other workspaces.
    * ``title``: lang key or text (translated in the rail and the palette).
    * ``widget``: the page.  ``None`` with ``canvas=True``: the shared canvas only.
    * ``icon``: Tabler icon name (``mlaser/ui/resources/icons/tabler``).
    * ``order``: position in the rail (slots 10..60; plugins default 100).
    * ``shortcut``: key sequence text (``"Ctrl+7"``); slots have Ctrl+1..6.
    * ``min_mode``: lowest user mode that sees it (``operator`` / ``engineer`` / ``service``).
    * ``canvas``: the page shows the shared drawing canvas; ``widget`` (if any) is then placed
      in a side panel to the right of the canvas.
    * ``toolbar``: command ids of the slim contextual toolbar above the page (at most ~15).
    * ``docks``: object names of the dock widgets shown while the workspace is active
      (``"lp0"`` layers/parts, ``"mf141"`` run control, ``"mf124"`` log, ``"mf460"`` nest);
      ``None`` keeps the docks as they are.
    * ``description``: one line for the rail tooltip and the empty-state placeholder.
    * ``slot``: the built-in slot this fills (``""``: derived from ``id``/``title``, see the
      module docstring).
    """

    id: str
    title: str = ""
    widget: Any = None
    icon: str = ""
    order: int = 100
    shortcut: str = ""
    min_mode: str = "engineer"
    canvas: bool = False
    toolbar: tuple[str, ...] = ()
    docks: Optional[tuple[str, ...]] = None
    description: str = ""
    owner: str = "core"
    slot: str = ""
    placeholder: bool = False
    extra: dict = field(default_factory=dict)


#: the built-in slots (placeholders until a plugin fills them; Design is the canvas)
BUILTIN: dict[str, WorkspaceSpec] = {
    DESIGN: WorkspaceSpec(
        DESIGN, "shell.ws.design", None, "pencil", 10, "Ctrl+1", "engineer", canvas=True,
        toolbar=("view.select_mode", "view.node_edit", "view.pan_mode", "-", "file.import",
                 "draw.line", "draw.rect", "draw.circle", "draw.text", "-", "edit.undo", "edit.redo",
                 "-", "xform.align", "xform.mirror_h", "xform.rot_cw90", "tool.array", "-",
                 "view.zoom_fit"),
        docks=("lp0", "mf141", "mf124"), description="shell.ws.design.desc", slot=DESIGN),
    PROCESS: WorkspaceSpec(
        PROCESS, "shell.ws.process", None, "adjustments", 20, "Ctrl+2", "engineer", canvas=True,
        toolbar=("craft.lead", "craft.kerf", "craft.microjoint", "craft.reverse", "craft.start_point",
                 "-", "sort.run", "sort.nearest", "-", "tool.onekey", "-", "show.start",
                 "show.direction", "show.move_path", "-", "sim.speed", "sim.stop"),
        docks=("lp0", "mf141", "mf124"), description="shell.ws.process.desc", slot=PROCESS,
        placeholder=True),
    NEST: WorkspaceSpec(
        NEST, "shell.ws.nest", None, "layout-board-split", 30, "Ctrl+3", "engineer", canvas=True,
        toolbar=("nest.new", "nest.open", "nest.save", "-", "nest.import_part", "nest.add_part",
                 "nest.import_sheet", "nest.add_sheet", "-", "nest.params", "nest.run", "nest.to_main"),
        docks=("mf460", "mf124"), description="shell.ws.nest.desc", slot=NEST, placeholder=True),
    RUN: WorkspaceSpec(
        RUN, "shell.ws.run", None, "player-play", 40, "Ctrl+4", "operator", canvas=True,
        toolbar=("file.open", "file.task_import", "-", "machine.home", "coord.return_origin", "-",
                 "card.break_resume", "card.alarms", "card.clear_alarms", "-", "view.zoom_fit"),
        docks=("mf141", "mf124"), description="shell.ws.run.desc", slot=RUN, placeholder=True),
    REPORT: WorkspaceSpec(
        REPORT, "shell.ws.report", None, "report-analytics", 50, "Ctrl+5", "operator",
        toolbar=(), docks=("mf124",), description="shell.ws.report.desc", slot=REPORT, placeholder=True,
        extra={"commands": ("diag.work_report", "diag.work_report_all", "diag.log", "diag.log_dir",
                            "card.alarms", "diag.device_report", "diag.measure")}),
    MACHINE: WorkspaceSpec(
        MACHINE, "shell.ws.machine", None, "tool", 60, "Ctrl+6", "operator",
        toolbar=(), docks=("mf141", "mf124"), description="shell.ws.machine.desc", slot=MACHINE,
        placeholder=True,
        extra={"commands": ("hw.connect_sim", "hw.reconnect", "machine.home", "machine.home_xyz",
                            "coord.return_origin", "machine.edge_seek", "dev.follower", "dev.laser",
                            "dev.pendant", "app.options", "hw.options", "hw.set_ip", "hw.net_test",
                            "card.params", "mon.controller", "mon.ftc", "aux.light_test",
                            "aux.maintain", "hw.upgrade", "param.backup_file", "param.restore_file")}),
}


def slot_for(name: str, title: str = "", slot: str = "") -> str:
    """The built-in slot a contributed workspace fills ("" for none), by the slot rule."""
    for cand in (slot, name.rsplit(".", 1)[-1], title):
        c = (cand or "").strip().lower()
        if c in SLOTS:
            return c
    return ""


# --------------------------------------------------------------------------- commands
_PREFIX_WORKSPACE = {
    "file": DESIGN, "edit": DESIGN, "draw": DESIGN, "xform": DESIGN, "select": DESIGN,
    "view": DESIGN, "show": DESIGN, "geom": DESIGN, "tool": DESIGN, "camx": DESIGN,
    "craft": PROCESS, "sort": PROCESS, "sim": PROCESS, "opt": PROCESS,
    "nest": NEST,
    "run": RUN, "job": RUN, "card": RUN,
    "diag": REPORT,
    "machine": MACHINE, "hw": MACHINE, "coord": MACHINE, "param": MACHINE, "mon": MACHINE,
    "aux": MACHINE, "dev": MACHINE,
}
_ID_WORKSPACE = {
    "tool.onekey": PROCESS, "tool.optimize": PROCESS, "tool.bridge": PROCESS, "tool.engrave": PROCESS,
    "card.params": MACHINE, "card.stream_process": MACHINE, "diag.measure": MACHINE,
    "diag.device_report": MACHINE, "diag.log": REPORT, "file.task_import": RUN,
    "app.options": MACHINE, "app.profile": MACHINE,
}


def command_workspace(cmd_id: str, spec: Any = None) -> str:
    """The workspace a command belongs to (declared by ``spec.workspace``, else by its id);
    ``""`` for application-wide commands (``app.*``, ``shell.*``, ``workspace.*``)."""
    declared = getattr(spec, "workspace", "") if spec is not None else ""
    if declared:
        return declared
    if cmd_id in _ID_WORKSPACE:
        return _ID_WORKSPACE[cmd_id]
    return _PREFIX_WORKSPACE.get(cmd_id.split(".", 1)[0], "")


__all__ = ["SLOTS", "BUILTIN", "WorkspaceSpec", "slot_for", "command_workspace",
           "DESIGN", "PROCESS", "NEST", "RUN", "REPORT", "MACHINE"]
