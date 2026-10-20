"""An example mlaser plugin (plugin API 2, docs/dev/PLUGIN_STANDARD.md in the mlaser repository)."""
from __future__ import annotations

__version__ = "0.1.0"
PLUGIN_API = 2


class ExamplePlugin:
    def __init__(self) -> None:
        self.ctx = None

    def register(self, ctx):
        self.ctx = ctx
        ctx.register_command("example.square", self.square, label="Example: add a square", icon=ctx.icon)
        ctx.add_ribbon_tab("Example", ["example.square"])
        return self

    def square(self) -> None:
        from mlaser.model import Contour, Craft, Line, Part
        pts = [(0, 0), (20, 0), (20, 20), (0, 20)]
        part = Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], closed=True)], Craft(layer=1),
                    name="example")
        self.ctx.edit(lambda doc: doc.parts.append(part), "example square", parts=())   # needs "document"
        self.ctx.show_message("example: one square added")


plugin = ExamplePlugin()
