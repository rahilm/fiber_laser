"""Example mlaser plugin (docs/dev/PLUGINS.md).

Adds one command, ``hello.grid``, that appends a grid of squares to the drawing as one undoable
edit, a "Hello" ribbon tab with its button, and typed settings (rows, columns, size, gap) that
appear in the configuration store as ``plugin.hello.*``.  Install it next to mlaser
(``pip install -e examples/mlaser_hello``) and start the application with plugins enabled.
"""
from __future__ import annotations

from mlaser.model import Contour, Craft, Line, Part
from mlaser.services.config import Setting

PLUGIN_API = 1          # the plugin API version this plugin was written for


def square(x: float, y: float, size: float) -> Contour:
    pts = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], closed=True)


def grid_parts(rows: int, cols: int, size: float, gap: float, layer: int = 1) -> list[Part]:
    """The geometry, without any UI: easy to unit-test."""
    return [Part([square(c * (size + gap), r * (size + gap), size)], Craft(layer=layer), name="hello")
            for r in range(rows) for c in range(cols)]


class HelloPlugin:
    def __init__(self) -> None:
        self.settings = None
        self.ctx = None

    def register(self, ctx):
        self.ctx = ctx
        self.settings = ctx.settings_schema([
            Setting("rows", "int", 2, min=1, max=100, label="Rows"),
            Setting("cols", "int", 3, min=1, max=100, label="Columns"),
            Setting("size", "float", 10.0, unit="mm", min=0.1, max=1000.0, label="Square size"),
            Setting("gap", "float", 5.0, unit="mm", min=0.0, max=1000.0, label="Gap"),
        ])
        ctx.register_command("hello.grid", self.add_grid, label="Hello grid")
        ctx.add_ribbon_tab("Hello", ["hello.grid"])
        ctx.on_document_changed(lambda kind: None)        # e.g. refresh a panel of the plugin
        return self

    def add_grid(self) -> None:
        s = self.settings
        parts = grid_parts(s["rows"], s["cols"], s["size"], s["gap"])
        n = self.ctx.edit(lambda doc: (doc.parts.extend(parts), len(parts))[1], "hello grid", parts=())
        self.ctx.log(f"hello: {n} squares")


plugin = HelloPlugin()
