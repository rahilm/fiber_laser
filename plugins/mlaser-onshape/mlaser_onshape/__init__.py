"""mlaser plugin "Onshape" (plugin standard v1, plugin API 2).

Browse Onshape documents, choose the parts of an assembly that this laser cuts (by a process
custom property, or every sheet-like part), measure their thickness, map the Onshape material
to the technology database, draw each part's flat face as an R12 DXF and add them as order
lines grouped by material x thickness, ready for Load into Nest.  It talks only to the Onshape
server through ``ctx.http`` and never starts anything on the machine.  See README.md.
"""
from __future__ import annotations

from typing import Any

__version__ = "0.1.0"
PLUGIN_API = 2

#: members of the plugin context this plugin needs (plugin API 2)
NEEDS = ("http", "secrets", "data_dir", "run_in_background")


class OnshapePlugin:
    PLUGIN_API = PLUGIN_API

    def __init__(self) -> None:
        self.ctx: Any = None
        self.service: Any = None
        self.workspace: Any = None

    def register(self, ctx: Any) -> "OnshapePlugin":
        missing = [m for m in NEEDS if getattr(ctx, m, None) is None]
        if missing:
            raise RuntimeError("the Onshape plugin needs plugin API 2 (missing: " + ", ".join(missing) + ")")
        from .service import OnshapeService
        self.ctx = ctx
        self.service = OnshapeService(ctx)
        from .ui import OnshapeWorkspace
        self.workspace = OnshapeWorkspace(self.service)
        ctx.add_workspace("onshape", self.workspace, "Onshape", icon="file-import", order=35, min_mode="engineer",
                          description="Onshape parts to DXF order lines")
        ctx.register_command("onshape.refresh", self.workspace.reload_documents, label="Onshape: refresh documents",
                             min_mode="engineer")
        ctx.register_command("onshape.prepare", self.workspace.prepare_selected,
                             label="Onshape: download DXFs and create orders", min_mode="engineer")
        return self


plugin = OnshapePlugin()
