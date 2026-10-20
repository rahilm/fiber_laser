"""Process plugin (Wave 3 Process track): technology database, Process workspace, properties
panel and the preview scrubber with the time / cost card.

Registered through the plugin API only (docs/dev/PLUGINS.md):

* ``ctx.add_workspace("process", ...)``: the Process workspace
  (:mod:`mlaser.ui.workspaces.process`); until the shell's workspace switcher exists it is also
  shown in a dock (*Process*).
* docks *Properties* (:mod:`mlaser.ui.properties`) and *Preview* (the scrubber,
  :mod:`mlaser.ui.preview_scrubber`; its overlay draws on the canvas scene).
* commands ``process.show``, ``process.apply``, ``process.rates``, ``props.show``,
  ``preview.play``, ``preview.clear`` and a ribbon tab.
* settings ``plugin.ext_process.machine_profile`` (the technology store's machine profile); the
  cost rates are the application's (Settings › Costing, ``costing.*``): ``process.rates`` opens
  that page.
* hooks: ``on_plan`` (the PlannedJob of the preview: timeline, cost card), ``on_document_changed``
  (layers, properties), the canvas selection (properties).
"""

from __future__ import annotations

from typing import Any, Optional


from mlaser.services.config import Setting
from mlaser.services.costing import Rates, card_for_plan, rates_from_config
from mlaser.services.preview import Timeline, build_timeline
from mlaser.services.technology import TechnologyStore
from mlaser.ui.units_qt import on_units_changed

PLUGIN_API = 1
LAZY_TIMELINE_ITEMS = 1_000_000      # larger plans (about 0.5 s to build) wait for the first scrub / play

SETTINGS = [
    Setting("machine_profile", "str", "default", label="process.set.machine_profile"),
]


def rates_from(config: Any) -> Rates:
    """The cost card's rates: Settings › Costing (``costing.*``, one rate model for the app)."""
    return rates_from_config(config)


class ProcessExtension:
    """What ``register`` returns (``window.extension("ext_process")``)."""

    def __init__(self, ctx: Any) -> None:
        from mlaser.ui.preview_scrubber import PreviewOverlay, ScrubberWidget
        from mlaser.ui.properties import PropertiesPanel
        from mlaser.ui.workspaces.process import ProcessWorkspace

        self.ctx = ctx
        t = ctx.tr.tr
        self.settings = ctx.settings_schema(SETTINGS)
        self.store = TechnologyStore(profile=str(self.settings["machine_profile"] or "default"))
        win = ctx.window
        self.canvas = win.canvas
        self.plan: Any = None
        self.timeline: Optional[Timeline] = None
        self.exec_dialogs = True
        self.workspace = ProcessWorkspace(ctx, self.store)
        from mlaser.ui.workspaces.cutting_strategy import install as install_strategy
        self.strategy = install_strategy(ctx, self.workspace.tabs, self.store)     # the Cutting strategy tab
        # the Process slot of the shell: the canvas with this panel at its right
        ctx.add_workspace("process", self.workspace, t("process.title"), canvas=True,
                          docks=["plugin.ext_process.properties", "plugin.ext_process.preview", "mf124"])
        self.properties = PropertiesPanel(ctx, self.canvas, technology_name=self.workspace.technology_name,
                                          store=self.store)
        self.scrubber = ScrubberWidget(ctx.tr)
        self.overlay = PreviewOverlay(self.canvas)
        self.props_dock = ctx.add_dock(self.properties, t("props.title"), "left", key="plugin.ext_process.properties")
        self.preview_dock = ctx.add_dock(self.scrubber, t("preview.title"), "bottom", key="plugin.ext_process.preview")
        # one tab group with the layer / part dock (left), so the run controls on the right keep
        # their height: Layers | Properties
        left = getattr(win, "left_dock", None)
        try:
            if left is not None:
                win.tabifyDockWidget(left, self.props_dock)
            self.props_dock.raise_()
        except (AttributeError, RuntimeError):
            pass
        self.scrubber.timeChanged.connect(self._time_changed)
        self.scrubber.need_timeline = lambda: self._build_timeline() if self.plan is not None else None
        self.workspace.card.rates_btn.clicked.connect(self.rates_dialog)
        self.canvas.selectionChanged.connect(self.properties.refresh)
        ctx.on_document_changed(self._document_changed)
        ctx.on_plan(self._plan)
        self.closed = False
        ctx.on_close(self._close)
        self.config_unsub = ctx.config.subscribe(self._config_changed)
        # the cost card's notes carry lengths (sheet size, nozzle): rebuilt in the new units
        on_units_changed(self.workspace, lambda _u: self.update_card(), now=False)
        ctx.register_command("process.show", self.show_process, label="process.cmd.show")
        ctx.register_command("process.apply", self.workspace.apply_selected, label="process.cmd.apply")
        ctx.register_command("process.rates", self.rates_dialog, label="process.cmd.rates")
        ctx.register_command("process.library", self.show_library, label="process.cmd.library")
        ctx.register_command("props.show", self.show_properties, label="props.cmd.show")
        ctx.register_command("preview.play", self.play, label="preview.cmd.play")
        ctx.register_command("preview.clear", self.scrubber.clear, label="preview.cmd.clear")
        ctx.add_ribbon_tab(t("process.tab"), ["process.show", "process.apply", "props.show", "preview.play",
                                              "preview.clear", "process.rates"])

    # ---------------------------------------------------------------- commands
    def show_process(self) -> None:
        """The Process workspace (the shell's rail); a host without the shell shows the panel."""
        if not self.ctx.show_workspace(f"{self.ctx.name}.process"):
            self.workspace.show()

    def show_library(self) -> None:
        """Process › Library (the technology records): the workspace on its Library page."""
        self.show_process()
        self.workspace.show_page(1)

    def show_properties(self) -> None:
        self.props_dock.show()
        self.props_dock.raise_()

    def play(self) -> None:
        self.preview_dock.show()
        if self.timeline is None and self.plan is not None:
            self._build_timeline()
        self.scrubber.play()

    def rates_dialog(self) -> None:
        """The rates' one home is Settings › Costing."""
        w = self.ctx.window
        opener = getattr(w, "settings_dialog", None)
        if opener is not None:
            opener().show_page("costing")

    # ---------------------------------------------------------------- hooks
    def _close(self) -> None:
        # every save is committed at once; the connection stays usable (a "close" command can
        # be cancelled, and the window may still run commands afterwards)
        self.closed = True
        self.scrubber.pause()

    def _document_changed(self, kind: str) -> None:
        if kind == "replace":
            # a drawing saved without override marks (.chf, DXF, an older project): derive them
            # once (no value changes; services.inheritance)
            from mlaser.services.inheritance import derive_marks
            derive_marks(self.ctx.document, self.store)
        self.workspace.refresh_layers()
        self.properties.refresh()

    def _config_changed(self, key: str) -> None:
        if key == "*" or key.startswith("costing."):
            self.update_card()

    def _plan(self, plan: Any) -> None:
        self.plan = plan
        self.timeline = None
        self.overlay.set_timeline(None)
        n = len(plan.job.wire) if plan is not None else 0
        if plan is not None and n <= LAZY_TIMELINE_ITEMS:
            self._build_timeline()
        else:
            self.scrubber.set_timeline(None)
            self.scrubber.play_btn.setEnabled(plan is not None)
            self.scrubber.state_label.setText(self.ctx.tr.tr("preview.lazy"))
            self.update_card()

    def _build_timeline(self) -> None:
        try:
            self.timeline = build_timeline(self.plan)
        except Exception as e:  # noqa: BLE001 - the preview must not break planning
            self.ctx.log(f"preview: {type(e).__name__}: {e}")
            self.timeline = None
        self.overlay.set_timeline(self.timeline)
        self.scrubber.set_timeline(self.timeline)
        self.update_card()

    def update_card(self) -> None:
        if self.plan is None or self.timeline is None:
            self.workspace.card.set_card(None)
            return
        unit = int(self.ctx.config.get("UN.GasPressureUnit", 0) or 0)
        self.card = card_for_plan(self.plan, self.timeline, rates_from(self.ctx.config), unit)
        self.workspace.card.set_card(self.card)

    def _time_changed(self, t: float) -> None:
        if t < 0:
            self.overlay.hide()
            return
        if self.timeline is None and self.plan is not None:
            self._build_timeline()
        if self.overlay.timeline is not self.timeline:
            self.overlay.set_timeline(self.timeline)
        self.overlay.set_time(t)


def register(ctx) -> ProcessExtension:
    return ProcessExtension(ctx)
