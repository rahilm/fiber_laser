"""Main window: the task shell (top bar, workspace rail, canvas, docks, run bar) or the classic
ribbon (layout per docs/re/ui.md); the shell is :mod:`mlaser.ui.shell` (docs/user/shell.md)."""
from __future__ import annotations

import copy
import os
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel, QMainWindow,
                               QMessageBox, QScrollArea, QTabWidget)

from mlaser import ops
from mlaser.model import Document, LayerParams, LeadParams
from mlaser.ops.craft import MicroJointParams, default_lead_params  # noqa: F401 - re-exported
from mlaser.services.commands import CommandRegistry, UnknownCommandError
from mlaser.services.config import ConfigService
from mlaser.services.document import DocumentService, clone_parts
from mlaser.services.jobs import ControllerSubmitter, JobService, build_request
from mlaser.services.plugins import ALLOWLIST_KEY, Hooks, discover, plugin_status, register_plugin

from . import dialogs
from .canvas import CanvasView, layer_color
from ..machine.api import ControllerFacade
from .facade import DemoController
from .i18n import Translator, translator
from .panels import ControlPanel, LayerPanel, LogPanel, PartListPanel
from .params import OPTION_SECTIONS
from .ribbon import Ribbon

# ribbon sort command -> CamParams.sort_mode (CADModule 0x1003f010 modes 1..7, docs/re/cam.md §8;
# 6/7 CRingSort; 0 keep, 20/21 rebuild additions).  The one path for every ordering mode: the
# duplicate bbox fallback (docops.sort_parts) and ext_nest's second binding of nearest / in-out /
# out-in / run are gone.
SORT_MODES = {"left_right": 1, "right_left": 2, "bottom_top": 3, "top_bottom": 4, "nearest": 5,
              "run": 5, "in_out": 6, "out_in": 7, "keep": 0, "shortest": 20, "grid": 21}
SORT_LABELS = {"keep": "Keep current order", "shortest": "Shortest path", "grid": "Grid"}
# Built-in plugins (mlaser.ui.<name>, each with register(ctx), docs/dev/PLUGINS.md); third-party
# plugins come from the "mlaser.plugins" entry-point group.
EXTENSIONS = ("ext_cam", "ext_nest", "ext_devices", "ext_card",
              "ext_draw", "ext_craft", "ext_machine", "ext_diag", "ext_options",
              "ext_process",   # Wave 3 Process track: technology, Process workspace, properties, preview
              "ext_project",   # .mlz projects, autosave (File > Open / Save through its providers)
              # Wave 3 workspaces (full module paths; plugin names run / nest / report)
              "mlaser.ui.workspaces.run", "mlaser.ui.workspaces.nest", "mlaser.ui.workspaces.report")


def _clear_joints(doc) -> None:      # old name, kept for callers of mlaser.ui.mainwindow
    ops.clear_joints(doc)


def _lazy(module: str, attr: str) -> Optional[Callable]:
    """``module.attr``, or None when it cannot be imported (the failure is logged, not hidden)."""
    import importlib
    import logging
    try:
        return getattr(importlib.import_module(module), attr)
    except Exception:  # noqa: BLE001 - logged
        logging.getLogger("mlaser.ui").exception("cannot load %s.%s", module, attr)
        return None

class _RunRouter:
    """The controller as the run controls (ControlPanel, pendant) see it: Start / Dry Cut /
    Frame run the planned job (``MainWindow.run_job``); everything else is the live controller."""

    def __init__(self, window) -> None:
        self._w = window

    def __getattr__(self, name):
        return getattr(self._w.ctl, name)

    def start(self, document=None, selected_only: bool = False) -> None:
        self._w.run_job("process", selected_only)

    def dry_run(self, document=None) -> None:
        self._w.run_job("dry_run")

    def frame(self, document=None) -> None:
        self._w.run_job("frame")


class _Bridge(QObject):
    """Runs callables posted from worker threads on the GUI thread (queued signal)."""
    call = Signal(object)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.call.connect(lambda fn: fn())


class _CloseWatch(QObject):
    """Runs the plugins' ``close`` hooks before the window's closeEvent (an event filter, so the
    window's own close handling stays untouched)."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._w = window
        self._done = False

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._w and ev.type() == QEvent.Close and not self._done:
            self._done = True
            self._w.hooks.emit("close")
            self._w.jobs.shutdown()
            self._w.config.flush()
        return False


class MainWindow(QMainWindow):
    def __init__(self, controller: Optional[ControllerFacade] = None, tr: Optional[Translator] = None,
                 extensions: tuple = (), config: Optional[ConfigService] = None,
                 entry_points: bool = False):
        super().__init__()
        self.tr_ = tr or translator()
        self.ctl = controller or DemoController()
        # services (mlaser.services, docs/dev/ARCHITECTURE.md): typed configuration, the document
        # with its undo history, the command registry and the plugin hooks
        self.config = config or ConfigService()
        self.settings = self.config.mapping()     # dict view of the set values (Options dialogs)
        self.docs = DocumentService()
        self.commands = CommandRegistry()
        self.hooks = Hooks()
        # planned jobs: compiled in a worker, debounced, cached; the canvas shows the plan's
        # toolpath and Start / Dry Cut / Frame stream exactly that plan (services.jobs)
        self._bridge = _Bridge(self)
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer_fn: Optional[Callable] = None
        self._plan_timer.timeout.connect(lambda: self._plan_timer_fn() if self._plan_timer_fn else None)
        self.jobs = JobService(post=self._bridge.call.emit, schedule=self._schedule)
        self.jobs.on_toolpath.append(self._plan_toolpath_ready)
        self.jobs.on_planned.append(self._plan_ready)
        self.jobs.on_failed.append(lambda t, exc: self.log_panel.log(f"toolpath: {exc}"))
        self.plan = None                          # the current preview PlannedJob
        self._plan_head: Optional[tuple] = None
        self.extensions: dict = {}
        self.plugin_contexts: dict = {}
        self.workspaces: dict = {}
        # lead dialog state: defaults of a fresh install (D-17) + options (D-18/D-19)
        self.lead_in, self.lead_out = default_lead_params()
        self.lead_closed_only = True     # "只对封闭图形有效" (closed graphs only), on by default
        self.lead_start_mode = 0         # "自动选择起点位置(长边优先)" (auto, long side first)
        from mlaser.cam.toolpath import CamParams
        self.cam_params = CamParams()            # sort mode etc.
        self.machine = self.config.machine()     # mlaser.nc.Machine from the machine profile
        self.config.subscribe(self._config_changed)
        self._toolpath: list = []
        self.estimate: dict = {}
        self.mj_params = MicroJointParams()
        self.resize(1400, 900)
        self.setWindowTitle("Mlaser")

        self.ribbon = Ribbon(self.tr_, self)
        self.lang_combo = QComboBox()
        self.lang_combo.setObjectName("language")
        self.tr_.bind(self.lang_combo, "shell.language", self.lang_combo.setAccessibleName)
        self.tr_.bind(self.lang_combo, "shell.language", self.lang_combo.setToolTip)
        self._fill_lang_combo()
        self.lang_combo.currentIndexChanged.connect(
            lambda i: self.set_language(self.lang_combo.itemData(i)) if self.lang_combo.itemData(i) else None)

        self.canvas = CanvasView(self)
        self.canvas.setAccessibleName("Canvas")
        self.tr_.bind(self.canvas, "shell.canvas", self.canvas.setAccessibleName)
        # the task shell (mlaser.ui.shell): central workspace stack (Design holds the canvas), the
        # rail, the top bar; plugins add workspaces while they load (host_register_workspace)
        from .shell import install as install_shell
        install_shell(self)

        # left: layers + parts (tabbed dock)
        self.layer_panel = LayerPanel(self.tr_)
        self.part_list = PartListPanel(self.tr_)
        left = QTabWidget()
        left.addTab(self.layer_panel, "")
        left.addTab(self.part_list, "")
        self.tr_.bind(left, "gp83", lambda t: left.setTabText(0, t))
        self.tr_.bind(left, "np_Part", lambda t: left.setTabText(1, t))
        self.left_dock = self._dock("lp0", left, Qt.LeftDockWidgetArea)

        # right: control panel
        self.control = ControlPanel(self.tr_, self.ctl, lambda: self.document)
        sa = QScrollArea()
        sa.setWidget(self.control)
        sa.setWidgetResizable(True)
        self.right_dock = self._dock("mf141", sa, Qt.RightDockWidgetArea)

        # bottom: log / alarms
        self.log_panel = LogPanel(self.tr_)
        self.log_dock = self._dock("mf124", self.log_panel, Qt.BottomDockWidgetArea)
        self.ctl.add_log_listener(self.log_panel.log)

        self.run_controls = _RunRouter(self)
        self.control.ctl = self.run_controls       # Start / Dry Cut / Frame: the planned job
        self.resizeDocks([self.log_dock], [150], Qt.Vertical)
        self.resizeDocks([self.left_dock, self.right_dock], [320, 300], Qt.Horizontal)
        self._status_bar()
        self._wire()
        self._shortcuts()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self.docs.subscribe(self._document_changed)
        self.set_document(Document())
        self._close_watch = _CloseWatch(self)
        self.installEventFilter(self._close_watch)
        self.plugin_catalog: list = []           # entry-point plugins found (services.plugins)
        self._load_extensions(extensions)
        # --- safety I/O (ui/safety_io.py): the interlocks and the heartbeat from the ConfigService
        # reach the controller now, after every change and after a controller swap
        from . import safety_io
        safety_io.install(self)
        # --- safety UI (ui/safety_ui.py): event dispatcher, state/alarm banners, run bar,
        # global Hold/Stop keys, pre-flight dialogs (docs/SAFETY.md)
        from . import safety_ui
        safety_ui.install(self)
        # --- third-party plugins: opt-in (the allowlist), after the safety UI exists, outside the
        # core's command namespaces (docs/dev/PLUGINS.md §6)
        if entry_points:
            self._load_entry_points()
        # ---
        self.shell.finish()       # commands, keys, palette, theme, user mode, layout

    # ------------------------------------------------------------------ plugins
    def _load_extensions(self, names, entry_points: bool = False) -> dict:
        """Register the built-in plugins ``names`` (modules of this package); ``entry_points``
        loads the enabled ``mlaser.plugins`` entry points right after them (the application
        itself loads those later, see :meth:`_load_entry_points`).  A failing plugin is logged
        with its reason and rolled back; the core window still works."""
        mods = list(dict.fromkeys(n if "." in n or ":" in n else f"{__package__}.{n}" for n in names))
        for info in discover(mods, entry_points=False):
            self._register(info)
        if entry_points:
            self._load_entry_points()
        return self.extensions

    def _register(self, info) -> bool:
        ctx, result = register_plugin(self, info)
        if ctx is None:
            return False
        self.plugin_contexts[info.name] = ctx
        self.extensions[info.name] = result
        return True

    def _load_entry_points(self) -> list:
        """The ``mlaser.plugins`` entry points: every one is listed in :attr:`plugin_catalog`
        (``[info, status]``), only those the allowlist enables (``plugins.allowlist``, Service
        mode, Options+ > Plugins) are imported.  Status: ``loaded`` / ``failed`` / ``disabled``
        / ``changed`` (enabled for another target: needs enabling again)."""
        allow = self.config.get(ALLOWLIST_KEY, {})
        taken = set(self.extensions)
        for info in discover((), entry_points=True):
            st = plugin_status(info, allow)
            if st == "enabled":
                if info.name in taken:
                    st = "failed"
                    self.log(f"plugin {info.name}: name already taken, not loaded")
                else:
                    st = "loaded" if self._register(info) else "failed"
            elif st == "changed":
                self.log(f"plugin {info.name}: its entry point changed to {info.target}; "
                         "not loaded until it is enabled again (Options+ > Plugins, Service mode)")
            self.plugin_catalog.append([info, st])
        found = [i.name for i, s in self.plugin_catalog if s in ("disabled", "changed")]
        if found:
            self.log(f"plugins installed but not enabled: {', '.join(found)} (Options+ > Plugins)")
        return self.plugin_catalog

    def extension(self, name: str):
        """The object a plugin's register() returned (None when it is not loaded)."""
        return self.extensions.get(name)

    def log(self, msg: str) -> None:
        self.log_panel.log(msg)

    def host_add_dock(self, widget, title: str, area: str = "right", key: str = "") -> QDockWidget:
        areas = {"left": Qt.LeftDockWidgetArea, "right": Qt.RightDockWidgetArea,
                 "bottom": Qt.BottomDockWidgetArea, "top": Qt.TopDockWidgetArea}
        d = QDockWidget(title, self)
        d.setObjectName(key or title)
        d.setWidget(widget)
        self.addDockWidget(areas.get(area, Qt.RightDockWidgetArea), d)
        shell = getattr(self, "shell", None)
        if shell is not None:          # an exclusive workspace (Run) keeps plugin docks out
            shell.host.watch_docks()
        return d

    def host_add_ribbon_tab(self, title: str, commands: list[str]):
        return self.ribbon.add_command_tab(title, commands)

    def host_add_workspace(self, name: str, widget, title: str):
        """The plugin API of Wave 2 (``name, widget, title``): a workspace with the defaults."""
        return self.host_register_workspace(name, widget, title, {})

    def host_register_workspace(self, name: str, widget, title: str, options: dict):
        """A task workspace (``PluginContext.add_workspace``; options of
        :class:`mlaser.services.workspaces.WorkspaceSpec`).  It fills a built-in slot (Design,
        Process, Nest, Run, Report, Machine) by the slot rule, else it gets a rail button of its
        own.  Kept in ``self.workspaces`` as (title, widget) too."""
        if name in self.workspaces:
            raise KeyError(f"workspace {name!r} already exists")
        from mlaser.services.workspaces import WorkspaceSpec
        opts = dict(options)
        extra = dict(opts.pop("extra", {}) or {})
        extra["canvas_given"] = "canvas" in opts
        extra["min_mode_given"] = "min_mode" in opts
        if "toolbar" in opts:
            opts["toolbar"] = tuple(opts["toolbar"] or ())
        if opts.get("docks") is not None:
            opts["docks"] = tuple(opts["docks"])
        spec = WorkspaceSpec(id=name, title=title or name, widget=widget, owner=name.split(".", 1)[0],
                             extra=extra, **opts)
        self.shell.register_workspace(spec)
        self.workspaces[name] = (title, widget)
        return widget

    def show_workspace(self, ws_id: str) -> bool:
        """Switch to a workspace (``"design"`` ... ``"machine"`` or a plugin workspace id)."""
        return self.shell.show_workspace(ws_id)

    @property
    def user_mode(self) -> str:
        return self.shell.modes.mode

    def set_user_mode(self, mode: str, pin: Optional[str] = None) -> bool:
        """Programmatic user-mode switch (no dialog); ``pin`` when a PIN protects ``mode``."""
        return self.shell.set_user_mode(mode, pin)

    @property
    def document(self) -> Document:
        return self.docs.document

    @document.setter
    def document(self, doc: Document) -> None:
        self.docs.adopt(doc, self.docs.path)

    @property
    def path(self) -> Optional[str]:
        return self.docs.path

    @path.setter
    def path(self, value: Optional[str]) -> None:
        self.docs.path = value

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        QTimer.singleShot(0, self.canvas.zoom_fit)   # fit once the view has its real size

    def closeEvent(self, ev) -> None:
        # --- safety: leave the machine halted (program stop, PWM 0, outputs off, FIFO
        # stop/clear) before the connection goes; a failure is logged, never swallowed
        from . import safety_ui
        if safety_ui.job_running(self) and not getattr(self, "_test_no_dialogs", False):
            if QMessageBox.question(self, "Job running", "A job is running. Stop it and quit?",
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                ev.ignore()
                return
        import logging
        try:
            self.ctl.safe_halt("main window closed")
        except Exception:  # noqa: BLE001
            logging.getLogger("mlaser.ui").exception("safe_halt on close failed")
        try:
            self.ctl.disconnect()
        except Exception:  # noqa: BLE001
            logging.getLogger("mlaser.ui").exception("disconnect on close failed")
        # ---
        super().closeEvent(ev)

    # ------------------------------------------------------------------ setup
    def _dock(self, key: str, w, area) -> QDockWidget:
        d = QDockWidget(self)
        d.setObjectName(key)
        d.setWidget(w)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.tr_.bind(d, key, d.setWindowTitle)
        self.addDockWidget(area, d)
        return d

    def _status_bar(self) -> None:
        """Panes observed in MainApp (CBCGPRibbonStatusBar AddElement ids MC, ZF, LF, XY, CV, PD, DA)."""
        sb = self.statusBar()
        self.st_mc, self.st_zf, self.st_lf = QLabel(), QLabel(), QLabel()
        self.st_xy, self.st_cv, self.st_pd, self.st_da = QLabel(), QLabel(), QLabel(), QLabel()
        for w in (self.st_mc, self.st_zf, self.st_lf, self.st_xy, self.st_cv, self.st_pd):
            sb.addWidget(w)
        sb.addPermanentWidget(self.st_da)
        self.st_da.setText("X: 0.000 Y: 0.000")

    def _wire(self) -> None:
        a = self.ribbon.actions
        handlers: dict[str, Callable[[], None]] = {
            "file.new": self.file_new, "file.open": self.file_open, "file.import": self.file_import,
            "file.save": self.file_save, "file.save_as": lambda: self.file_save(True),
            "app.exit": self.close, "app.import_language": self.import_language, "app.about": lambda: dialogs.AboutDialog(self.tr_, self).exec(),
            "app.options": lambda: self.options_dialog("app.options"),
            "hw.options": lambda: self.options_dialog("hw.options"),
            "hw.set_ip": self.ip_dialog, "hw.reconnect": self.reconnect,
            "machine.home": lambda: self.ctl.home("XY"), "machine.home_x": lambda: self.ctl.home("X"),
            "machine.home_y": lambda: self.ctl.home("Y"), "machine.home_xyz": lambda: self.ctl.home("XYZ"),
            "machine.home_zf_then_xy": lambda: (self.ctl.home("Z"), self.ctl.home("XY")),
            "view.select_mode": lambda: self.set_mode(CanvasView.MODE_SELECT),
            "view.pan_mode": lambda: self.set_mode(CanvasView.MODE_PAN),
            "view.node_edit": lambda: self.set_mode(CanvasView.MODE_NODE),
            "view.zoom_in": lambda: self.canvas.zoom(1.25), "view.zoom_out": lambda: self.canvas.zoom(0.8),
            "view.zoom_fit": self.canvas.zoom_fit,
            "edit.undo": self.undo, "edit.redo": self.redo,
            "select.all": lambda: self.canvas.select_parts(range(len(self.document.parts))),
            "select.invert": lambda: self._select(lambda p: not p.selected),
            "select.unclosed": lambda: self._select(lambda p: any(not c.closed for c in p.contours)),
            "xform.mirror_h": lambda: self._edit(ops.mirror, True, label="mirror"),
            "xform.mirror_v": lambda: self._edit(ops.mirror, False, label="mirror"),
            "xform.rot_cw90": lambda: self._edit(ops.rotate, -90, label="rotate"),
            "xform.rot_ccw90": lambda: self._edit(ops.rotate, 90, label="rotate"),
            "xform.rot_180": lambda: self._edit(ops.rotate, 180, label="rotate"),
            "xform.rotate": lambda: self._edit(ops.rotate, 90, label="rotate"),
            "xform.rot_any": self.rotate_dialog,
            "xform.scale_10": lambda: self._edit(ops.scale, 0.1, label="scale"),
            "xform.scale_50": lambda: self._edit(ops.scale, 0.5, label="scale"),
            "xform.scale_200": lambda: self._edit(ops.scale, 2.0, label="scale"), "xform.scale_dialog": self.scale_dialog,
            "xform.scale_30": lambda: self._edit(ops.scale, 0.3, label="scale"),
            "xform.scale_75": lambda: self._edit(ops.scale, 0.75, label="scale"),
            "xform.scale_500": lambda: self._edit(ops.scale, 5.0, label="scale"),
            **{f"xform.size_{mm}mm": (lambda mm=mm: self._edit(ops.scale_to, float(mm), label="size")) for mm in (10, 50, 100, 200)},
            "craft.lead": self.lead_dialog,
            "craft.lead_clear": lambda: self._edit(ops.apply_lead, LeadParams(0), LeadParams(0), False, label="clear leads"),
            "craft.kerf": self.kerf_dialog, "craft.kerf_set": self.kerf_dialog,
            "craft.kerf_cancel": lambda: self._edit(ops.apply_kerf, -1, 0.0, label="kerf"),
            "craft.inside": lambda: self._edit(ops.apply_kerf, 0, self._kerf_width(), label="kerf"),
            "craft.outside": lambda: self._edit(ops.apply_kerf, 1, self._kerf_width(), label="kerf"),
            "craft.reverse": lambda: self._edit(ops.reverse, label="reverse"),
            "craft.microjoint": self.microjoint_dialog, "craft.mj_params": self.microjoint_dialog,
            "sim.speed": self.sim_speed_dialog, "sim.stop": lambda: self.canvas.stop_simulation(True),
            "diag.log": lambda: self.log_dock.show(),
            "edit.copy": self.copy_selected, "edit.paste": self.paste,
        }
        for how in ("left", "right", "hcenter", "top", "bottom", "vcenter", "center"):
            handlers[f"xform.align_{how}"] = lambda _=False, h=how: self._edit(ops.align, h, label="align",
                                                                             scope="selection")
        for how in SORT_MODES:
            handlers[f"sort.{how}"] = lambda _=False, h=how: self.sort(h)
        handlers["sort.small_first"] = lambda: self.sort("small_first")
        handlers["craft.mj_auto"] = self.microjoint_auto
        handlers["craft.mj_clear"] = lambda: self._edit(ops.clear_joints, label="clear micro-joints")
        handlers["hw.connect_sim"] = self.connect_simulator
        show = {"show.bbox": "show_bbox", "show.unclosed_red": "unclosed_red", "show.index": "show_index",
                "show.start": "show_start", "show.direction": "show_direction", "show.move_path": "show_move_path"}
        for cmd, attr in show.items():
            a[cmd].setChecked(getattr(self.canvas.options, attr))
            handlers[cmd] = lambda on, at=attr: (setattr(self.canvas.options, at, bool(on)), self.canvas.rebuild())
        # every QAction is connected once to the registry; plugins register handlers, not slots
        self.commands.on_register.append(self._command_registered)
        self.commands.on_error.append(lambda cmd, exc: self.log_panel.log(f"{cmd}: {type(exc).__name__}: {exc}"))
        self.ribbon.on_new_action = self._connect_action
        for act in a.values():
            self._connect_action(act)
        for cmd, fn in handlers.items():
            self.commands.register(cmd, fn, "core", checkable=cmd in show)
        self.commands.register("edit.delete", self.delete_selected, "core", "cmd.edit.delete")
        for how, label in SORT_LABELS.items():
            self.ribbon.action(f"sort.{how}", label)
        self.handlers = self.commands.handlers()

        self.canvas.setContextMenuPolicy(Qt.CustomContextMenu)
        self.canvas.customContextMenuRequested.connect(self._context_menu)
        self.canvas.mouseMoved.connect(lambda x, y: self.st_da.setText(f"X: {x:.3f} Y: {y:.3f}"))
        self.canvas.selectionChanged.connect(self.part_list.sync_selection)
        self.part_list.selectionToCanvas.connect(lambda idx: self.canvas.select_parts(idx))
        self.layer_panel.assignLayer.connect(self.assign_layer)
        self.layer_panel.editLayer.connect(self.edit_layer)
        self.control.simulateRequested.connect(self.simulate)

    def _shortcuts(self) -> None:
        """Accelerators observed in RT_ACCELERATOR 128 (Ctrl+N/O/S/Z/X/C/V, Alt+Backspace,
        Shift+Del, Ctrl+Ins, Shift+Ins, F6) and the view key handler FUN_004d0480 (Esc cancel +
        deselect, arrows nudge selection, Del delete, Shift multi-select, Ctrl+A/C/V/Y/Z).
        The command keys live in :mod:`mlaser.ui.shell.keymap` (set on the commands' actions by
        the shell, listed in Help > Keyboard shortcuts); here only the canvas keys."""
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=lambda: (self.canvas.select_parts([]), self.set_mode(CanvasView.MODE_SELECT)))
        for key, (dx, dy) in ((Qt.Key_Left, (-1, 0)), (Qt.Key_Right, (1, 0)), (Qt.Key_Up, (0, 1)), (Qt.Key_Down, (0, -1))):
            QShortcut(QKeySequence(key), self.canvas, activated=lambda dx=dx, dy=dy: self.nudge(dx, dy))

    def _connect_action(self, act) -> None:
        """Every action is connected once.  A command registered ``checkable`` runs on
        ``toggled`` with the state (a programmatic setChecked runs it too, as before); any other
        command runs on ``triggered`` (view.* modes are checkable actions whose handler sets the
        checked state itself)."""
        cmd = act.data() or act.objectName()
        act.triggered.connect(lambda _=False, c=cmd: None if self._toggle_cmd(c) else self._dispatch(c))
        act.toggled.connect(lambda on, c=cmd: self._dispatch(c, on) if self._toggle_cmd(c) else None)

    def _toggle_cmd(self, cmd: str) -> bool:
        spec = self.commands.get(cmd)
        return bool(spec and spec.checkable)

    def _command_registered(self, spec) -> None:
        """A plugin bound a command that has no ribbon action yet: create one (label = lang key
        or text); a checkable command gets a checkable action."""
        if spec.id not in self.ribbon.actions:
            label = spec.label if spec.label and spec.label != spec.id else spec.id
            if label == spec.id and self.tr_.has(f"cmd.{spec.id}"):
                label = f"cmd.{spec.id}"            # the catalogue's readable id
            self.ribbon.action(spec.id, label, checkable=spec.checkable)
        elif spec.checkable:
            self.ribbon.actions[spec.id].setCheckable(True)

    def _dispatch(self, cmd: str, checked=None) -> None:
        try:
            if checked is None:
                self.commands.run(cmd)
            else:
                self.commands.run(cmd, bool(checked))
        except UnknownCommandError:
            self.not_implemented(cmd)

    # ------------------------------------------------------------------ helpers
    def run(self, cmd: str) -> None:
        act = self.ribbon.actions.get(cmd)
        if act is not None:
            act.trigger()
        else:
            self._dispatch(cmd)

    def not_implemented(self, cmd: str) -> None:
        self.log_panel.log(f"{cmd}: not implemented in the rebuild yet")

    def _fill_lang_combo(self) -> None:
        self.lang_combo.blockSignals(True)
        self.lang_combo.clear()
        for li in self.tr_.available():
            self.lang_combo.addItem(li.name, li.code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(self.tr_.language)))
        self.lang_combo.blockSignals(False)

    def import_language(self, path: Optional[str] = None) -> list[str]:
        """File > Import Language Files: load the translations of the user's own Mlaser install
        (folder or one Lang/*.txt file) for the languages mlaser does not ship."""
        if path is None:
            path = QFileDialog.getExistingDirectory(self, self.tr_.tr("main.import_language_title"))
        if not path:
            return []
        try:
            codes = self.tr_.import_from(path)
        except (OSError, ValueError) as exc:
            self.log_panel.log(f"{self.tr_.tr('main.import_language_none')} ({exc})")
            return []
        names = ", ".join(self.tr_.info(c).name for c in codes if c in {li.code for li in self.tr_.available()})
        self.log_panel.log(self.tr_.format("main.import_language_done", languages=names))
        self._fill_lang_combo()
        return codes

    def set_language(self, code: str) -> None:
        self.tr_.set_language(code)
        idx = self.lang_combo.findData(code)
        if idx >= 0 and idx != self.lang_combo.currentIndex():
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(idx)
            self.lang_combo.blockSignals(False)
        self.layer_panel.refresh()
        self.part_list.refresh()
        shell = getattr(self, "shell", None)
        if shell is not None:
            shell._update_title()

    def set_mode(self, mode: str) -> None:
        self.canvas.set_mode(mode)
        a = self.ribbon.actions
        for cmd, m in (("view.select_mode", CanvasView.MODE_SELECT), ("view.pan_mode", CanvasView.MODE_PAN),
                       ("view.node_edit", CanvasView.MODE_NODE)):
            a[cmd].setChecked(m == mode)
        self.hooks.emit("mode_changed", mode)

    def set_document(self, doc: Document, path: Optional[str] = None) -> None:
        """Show ``doc`` (open, new, import).  Recorded for undo only after :meth:`_snapshot`."""
        if self._job_locked("new document"):      # safety: the document is locked while a job runs
            return
        self._fill_layers(doc)
        self.docs.adopt(doc, path)

    @staticmethod
    def _fill_layers(doc: Document) -> None:
        for li in sorted({p.craft.layer for p in doc.parts}):
            # safety: a layer the file does not define is shown with defaults but marked as a
            # placeholder; the pre-flight check refuses to cut it until its parameters are set
            doc.layers.setdefault(li, LayerParams(index=li, color=layer_color(li).name(),
                                                  extra={"placeholder": True}))

    def _document_changed(self, kind: str) -> None:
        """DocumentService listener: refresh the views after an edit, undo/redo or a new
        document (a new document is zoomed to fit, an edit keeps the view)."""
        doc = self.docs.document
        self._fill_layers(doc)
        if kind == "replace":
            self.canvas.set_document(doc)
        else:
            self.canvas.document = doc
            self.canvas.rebuild()
        self._update_toolpath()
        self.layer_panel.set_document(doc)
        self.part_list.set_document(doc)       # bounding boxes are cached (22 ms at 2,000 parts)
        self.hooks.emit("document_changed", kind)

    def _refresh_part_list(self) -> None:
        self.part_list.set_document(self.docs.document)

    def refresh_views(self) -> None:
        """Rebuild the views after a change that is deliberately not an undo step."""
        self._document_changed("edit")

    def _config_changed(self, key: str) -> None:
        """ConfigService listener: the machine profile feeds the job compiler (re-plan) and the
        controller's own manual moves."""
        if key.startswith("shell."):             # the shell's own settings (theme, layout...)
            return
        m = self.config.machine()
        if m == self.machine:
            return
        self.machine = m
        try:
            self.ctl.machine = m                 # the controller's own manual moves
        except AttributeError:
            pass
        self._update_toolpath()

    # ------------------------------------------------------------------ planned job
    def _schedule(self, delay_s: float, fn):
        """JobService debounce timer on the GUI thread (one shared single-shot timer)."""
        self._plan_timer_fn = fn
        self._plan_timer.start(int(delay_s * 1000))
        return self._plan_timer.stop

    def _preview_request(self):
        doc = self.document
        if not doc.parts:
            self.jobs.preview = None
            self.plan = None
            self._set_toolpath([])
            return None
        req = build_request("process", self.docs, self.ctl, self.machine, self.cam_params)
        self._plan_head = req.start
        return req

    def _plan_toolpath_ready(self, ticket) -> None:
        if ticket is self.jobs.preview:
            self._set_toolpath(ticket.toolpath or [])

    def _plan_ready(self, plan, ticket) -> None:
        if ticket is not self.jobs.preview:
            return
        self.plan = plan
        self.estimate = dict(plan.estimate)
        self.hooks.emit("plan", plan)

    def _set_toolpath(self, items) -> None:
        self._toolpath = list(items)
        self.canvas.set_toolpath(self._toolpath)

    @property
    def toolpath(self) -> list:
        """The toolpath of the current plan (waits for the toolpath stage of a pending plan;
        the compile itself keeps running in the worker)."""
        if self.jobs.pending or (self.jobs.preview is not None and not self.jobs.preview.toolpath_event.is_set()):
            self.jobs.settle(toolpath_only=True)
        return self._toolpath

    @toolpath.setter
    def toolpath(self, items) -> None:
        self._set_toolpath(items or [])

    def run_job(self, mode: str = "process", selected_only: bool = False) -> None:
        """Start (``process``), Dry Cut (``dry_run``) or Frame (``frame``) with the planned job:
        the plan for the current inputs (normally the cached preview plan) goes to the
        controller; a compile, when one is needed, runs in the worker, never here."""
        ctl = self.ctl
        doc = self.document
        if mode == "process" and ctl.breakpoint is not None and not ctl.job_active():
            ctl.resume()
            return
        if not doc.parts or (selected_only and not any(p.selected for p in doc.parts)):
            self.log_panel.log("mp78: graphic data is empty, cannot go frame" if mode == "frame"
                               else "mp74: graphic data is empty, cannot start")
            return
        req = build_request(mode, self.docs, ctl, self.machine, self.cam_params, selected_only)

        def result(ok: bool, msg: str) -> None:
            if not ok:
                self.log_panel.log(msg)
        self.jobs.run(req, ControllerSubmitter(ctl, self.log_panel.log), result)

    def _update_toolpath(self) -> None:
        """Re-plan (debounced, in the worker): edits, sort mode, machine profile, controller."""
        self.jobs.schedule_preview(self._preview_request)

    def _refresh(self) -> None:
        """Views after an in-place change; records it when :meth:`_snapshot` was called first."""
        if self.docs._pending is not None:
            self.docs.commit()
            return
        self._document_changed("edit")

    def _snapshot(self) -> None:
        """Core helper for whole-document changes (open / new): the next set_document is undoable.
        Plugins use ``ctx.edit`` / ``ctx.replace_document`` instead."""
        self.docs.checkpoint()

    def _edit(self, fn, *args, label: str = "edit", scope: str = "targets"):
        """Undoable edit ``fn(document, *args)``; ``scope`` = the parts it changes in place:
        "targets" (selection, else all), "selection" or "all"."""
        doc = self.docs.document
        parts = (ops.targets(doc) if scope == "targets" else
                 [p for p in doc.parts if p.selected] if scope == "selection" else None)
        return self.docs.edit(lambda d: fn(d, *args), label, parts=parts)

    def _select(self, pred) -> None:
        ops.select_where(self.document, pred)
        self.canvas.refresh_styles()
        self.part_list.sync_selection()

    def undo(self) -> None:
        label = self.docs.undo()
        if label:
            self.statusBar().showMessage(f"undo: {label}", 3000)

    def redo(self) -> None:
        label = self.docs.redo()
        if label:
            self.statusBar().showMessage(f"redo: {label}", 3000)

    def _operator_blocked(self) -> bool:
        """Operator mode (mlaser.services.usermodes): the drawing and the parameters are not
        edited from the keyboard, the context menu or the layer table."""
        shell = getattr(self, "shell", None)
        if shell is not None and shell.modes.mode == "operator":
            self.statusBar().showMessage(self.tr_.tr("shell.mode.no_params"), 4000)
            return True
        return False

    def delete_selected(self) -> None:
        if self._operator_blocked():
            return
        if any(p.selected for p in self.document.parts):
            self._edit(ops.delete_selected, label="delete", scope="selection")

    def _context_menu(self, pos) -> None:
        """Canvas right-click menu (lang keys mv0..mv35 observed: mv1 Reset View, mv7 Copy,
        mv8 Paste, mv9 Delete, mv11 Clear track line, mv34 Select Same Layer Contours)."""
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        t = self.tr_.tr
        m.addAction(t("mv1"), self.canvas.zoom_fit)
        m.addSeparator()
        m.addAction(t("mv7"), self.copy_selected)
        m.addAction(t("mv8"), self.paste)
        m.addAction(t("mv9"), self.delete_selected)
        m.addSeparator()
        m.addAction(t("mv11"), lambda: self.canvas.stop_simulation(clear=True))
        def same_layer():
            layers = {p.craft.layer for p in self.document.parts if p.selected}
            self._select(lambda p: p.craft.layer in layers)
        m.addAction(t("mv34"), same_layer)
        m.exec(self.canvas.mapToGlobal(pos))

    def copy_selected(self) -> None:
        self._clipboard = clone_parts(p for p in self.document.parts if p.selected)

    def paste(self) -> None:
        """Paste copies offset by 10 mm (the original pastes at a picked point, menu 134)."""
        clip = getattr(self, "_clipboard", None)
        if not clip or self._operator_blocked():
            return
        # paste deselects every part, so all parts are in the scope (selection flags change)
        self._edit(ops.paste_parts, clip, 10.0, 10.0, label="paste", scope="all")

    def nudge(self, dx: float, dy: float) -> None:
        """mf445 keyboard fine-tuning (arrow keys): the ``nudge`` provider when a plugin sets one
        (ext_draw: pd377 step / auto step), else move the selection by SP.KeyboardMoveStep."""
        if self._operator_blocked():
            return
        provider = self.hooks.provider("nudge")
        if provider is not None:
            provider(dx, dy)
            return
        if not any(p.selected for p in self.document.parts):
            return
        step = float(self.settings.get("SP.KeyboardMoveStep", 1.0))
        self._edit(lambda d: ops.translate(d, dx * step, dy * step, [p for p in d.parts if p.selected]),
                   label="nudge", scope="selection")

    def assign_layer(self, layer: int) -> None:
        if self._operator_blocked():
            return
        if any(p.selected for p in self.document.parts):
            self._edit(ops.set_layer, layer, label="layer", scope="selection")

    def edit_layer(self, layer: int) -> None:
        if self._operator_blocked():               # Operator mode: no parameter editing
            return
        lp = self.document.layers.setdefault(layer, LayerParams(index=layer, color=layer_color(layer).name(),
                                                                extra={"placeholder": True}))
        if dialogs.LayerParamDialog(self.tr_, lp, self).exec():
            lp.extra.pop("placeholder", None)        # parameters confirmed: the layer is defined
            self.layer_panel.refresh()

    def _kerf_width(self) -> float:
        sel = [p for p in self.document.parts if p.selected] or self.document.parts
        return sel[0].craft.kerf_width if sel and sel[0].craft.kerf_width else 0.1

    # ------------------------------------------------------------------ files
    FILTER = ("Supported (*.mlz *.dxf *.chf *.nc *.txt *.cnc *.g *.plt *.svg);;mlaser project (*.mlz);;"
              "DXF (*.dxf);;CHF (*.chf);;All (*)")
    SAVE_FILTER = "mlaser project (*.mlz);;CHF (*.chf)"

    def _job_locked(self, what: str) -> bool:
        """Safety (docs/SAFETY.md): no file open / new document while a job runs."""
        if not getattr(self, "safety_ui", None):
            return False
        from . import safety_ui
        return safety_ui.job_locked(self, what)

    def file_new(self) -> None:
        if self._job_locked("File > New"):
            return
        self._snapshot()
        self.set_document(Document())

    def load_path(self, path: str) -> bool:
        if self._job_locked("open"):
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mlz":                                   # ext_project: native projects
            opener = self.hooks.provider("project.open")
            if opener is None:
                self.log_panel.log(f"mf155: no importer for {ext} yet")
                return False
            return bool(opener(path))
        loader = None
        if ext == ".chf":
            loader = _lazy("mlaser.formats.chf", "load_chf")
        elif ext == ".dxf":
            loader = _lazy("mlaser.io.dxf_import", "import_dxf")
            mode = self.hooks.provider("dxf.mode")              # ext_options: modern (ezdxf) import
            if loader is not None and mode is not None and mode() is not None:
                import functools
                loader = functools.partial(loader, mode=mode())
        elif ext == ".svg":
            loader = _lazy("mlaser.io.svg_import", "import_svg")
        elif ext in (".nc", ".txt", ".cnc", ".g"):
            loader = _lazy("mlaser.io.gcode", "read_gcode")
        if loader is None:
            self.log_panel.log(f"mf155: no importer for {ext} yet")
            return False
        from mlaser.io.limits import ImportCancelled, ImportLimitError
        from .import_worker import ImportBusy, run_import, show_error
        try:
            # off the GUI thread, with a progress dialog and Cancel (untrusted input:
            # mlaser.io.limits refuses expansion bombs)
            doc = run_import(self, lambda: loader(path), f"Opening {os.path.basename(path)}...")
        except ImportCancelled:
            self.log_panel.log(f"{os.path.basename(path)}: opening cancelled")
            return False
        except ImportBusy as exc:
            self.log_panel.log(f"{os.path.basename(path)}: not opened ({exc})")
            return False
        except ImportLimitError as exc:
            self.log_panel.log(f"mv32: {os.path.basename(path)}: {exc}")
            show_error(self, "File not imported", f"{os.path.basename(path)}\n\n{exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - show any loader failure to the user
            self.log_panel.log(f"mv32: {exc}")
            return False
        if self._job_locked("open"):                    # a job started while the file was read
            return False
        self._snapshot()
        self.set_document(doc, path if ext == ".chf" else None)
        self.log_panel.log(f"{os.path.basename(path)}: {len(doc.parts)} parts")
        return True

    def file_open(self) -> None:
        if self._job_locked("File > Open"):
            return
        path, _ = QFileDialog.getOpenFileName(self, self.tr_.tr("mf150"), "", self.FILTER)
        if path:
            self.load_path(path)

    def file_import(self) -> None:
        if self._job_locked("File > Import"):
            return
        path, _ = QFileDialog.getOpenFileName(self, self.tr_.tr("mf13"), "", self.FILTER)
        if not path:
            return
        before = self.document
        if self.load_path(path) and before.parts:
            merged = copy.deepcopy(before)
            merged.parts.extend(self.document.parts)
            self.set_document(merged, self.path)

    def file_save(self, save_as: bool = False) -> None:
        """Save: a new drawing (or Save As) asks for a file, ``.mlz`` project by default
        (``project.save`` provider of ext_project); a document opened from a ``.chf`` is saved
        back as ``.chf``; File > Export CHF (``file.export_chf``) writes the interchange file."""
        project_save = self.hooks.provider("project.save")
        path = self.path
        if save_as or not path:
            start = os.path.splitext(path)[0] + ".mlz" if path else ""
            filt = self.SAVE_FILTER if project_save is not None else "chf (*.chf)"
            path, _ = QFileDialog.getSaveFileName(self, self.tr_.tr("mf152"), start, filt)
            if path and project_save is not None and not os.path.splitext(path)[1]:
                path += ".mlz"
        if not path:
            return
        if not path.lower().endswith(".chf") and project_save is not None:
            if project_save(path):
                self.shell.mark_saved()
            return
        saver = _lazy("mlaser.formats.chf", "save_chf")
        if saver is None:
            self.log_panel.log("mf153: .chf writer not available yet")
            return
        if path:
            from mlaser.cam.raster_engraving import expand_document
            saver(expand_document(self.document), path)    # raster parts as their burn segments
            self.path = path
            self.log_panel.log(f"np_FileSaveDone: {path}")
            self.shell.mark_saved()

    # ------------------------------------------------------------------ dialogs
    def options_dialog(self, which: str) -> None:
        dialogs.ParamGridDialog(self.tr_, "mf137" if which == "app.options" else "mf129",
                                OPTION_SECTIONS[which], self.settings, self).exec()

    def ip_dialog(self) -> None:
        d = dialogs.IPDialog(self.tr_, self.settings.get("ip"), self)
        if d.exec():
            self.settings["ip"] = d.result_values()

    def reconnect(self) -> None:
        host = (self.settings.get("ip") or {}).get("pd445", "10.1.1.168")
        ok = self.ctl.connect(host)
        self.log_panel.log("mf165" if ok else "mf166")

    def lead_dialog(self) -> None:
        d = dialogs.LeadDialog(self.tr_, self.lead_in, self.lead_out, self)
        if d.exec():
            self._edit(ops.apply_lead, self.lead_in, self.lead_out, self.lead_closed_only,
                       self.lead_start_mode, label="lead")
            self.log_panel.log("mf207")

    def kerf_dialog(self) -> None:
        d = dialogs.KerfDialog(self.tr_, 1, self._kerf_width(), self)
        if d.exec():
            self._edit(ops.apply_kerf, *d.result_values(), label="kerf")

    def sort(self, how: str, **flags) -> None:
        """Cutting order: every ordering mode is a ``CamParams.sort_mode`` of
        :mod:`mlaser.cam.ordering` (``SORT_MODES``); ``small_first`` toggles the flag.  The plan
        (preview and job) is rebuilt with it."""
        if how == "small_first":
            self.cam_params.small_first = not self.cam_params.small_first
        else:
            self.cam_params.sort_mode = SORT_MODES.get(how, 5)
        for k, v in flags.items():
            setattr(self.cam_params, k, v)
        self._update_toolpath()
        self.log_panel.log(f"sort: mode {self.cam_params.sort_mode}, small first {self.cam_params.small_first}")

    def set_sort_mode(self, mode: int, **flags) -> None:
        """Set ``CamParams.sort_mode`` (and flags such as ``avoid_cut``) directly."""
        self.cam_params.sort_mode = int(mode)
        for k, v in flags.items():
            setattr(self.cam_params, k, v)
        self._update_toolpath()
        self.log_panel.log(f"sort: mode {mode}")

    def microjoint_dialog(self) -> None:
        d = dialogs.MicroJointDialog(self.tr_, self)
        mp = self.mj_params
        d.kind.setCurrentIndex(mp.kind); d.number.setValue(mp.count)
        d.distance.setValue(mp.distance); d.length.setValue(mp.length); d.min_size.setValue(mp.min_size)
        if d.exec():
            mp.update(kind=d.kind.currentIndex(), count=d.number.value(), distance=d.distance.value(),
                      length=d.length.value(), min_size=d.min_size.value())
            self.microjoint_auto()

    def microjoint_auto(self) -> None:
        """mf103 auto micro-joints (:func:`mlaser.ops.auto_microjoints`)."""
        n = self._edit(ops.auto_microjoints, self.mj_params, label="micro-joints")
        self.log_panel.log(f"mf103: {n} micro-joints")

    def rotate_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf185", "pd339", 30.0, -360, 360, self)
        if d.exec():
            self._edit(ops.rotate, d.value.value(), label="rotate")

    def scale_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf60", "pd348", 100.0, 0.1, 10000, self)
        if d.exec():
            self._edit(ops.scale, d.value.value() / 100.0, label="scale")

    def sim_speed_dialog(self) -> None:
        d = dialogs.ValueDialog(self.tr_, "mf121", "mf121", self.canvas.sim_speed, 0.1, 100, self)
        if d.exec():
            self.canvas.sim_speed = d.value.value()

    # ------------------------------------------------------------------ machine
    def simulate(self) -> None:
        """mp59 Sim: time-sampled simulation of the CAM toolpath (mlaser.motion.simulate) with
        the job time estimate (mlaser.motion.estimate_time)."""
        if not self.document.parts:
            self.log_panel.log("mp76")
            return
        sim = _lazy("mlaser.motion.interp", "simulate")
        est = _lazy("mlaser.motion.interp", "estimate_time")
        if not self.toolpath or sim is None or self.machine is None:
            self.canvas.start_simulation()
            return
        cfg = self.machine.motion_config()
        self.estimate = e = est(self.toolpath, cfg)
        pierces = sum(1 for m in self.toolpath if type(m).__name__ == "Pierce")
        total = e["total"]
        args = (int(total // 60), total % 60, e["cut_length"] / 1000.0, e["rapid_length"] / 1000.0, pierces)
        self.log_panel.log(self.tr_.format("mp83", *args))   # panel.estimate
        samples = sim(self.toolpath, cfg, period_ms=4.0)
        self.canvas.start_simulation(path=[(s.x, s.y, s.laser) for s in samples])

    def set_controller(self, ctl: ControllerFacade) -> None:
        """Swap the machine controller (e.g. demo -> real adapter)."""
        try:
            self.ctl.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.ctl = ctl
        self.control.ctl = self.run_controls
        ctl.add_log_listener(self.log_panel.log)
        self.hooks.emit("controller_changed", ctl)      # plugins re-attach (PluginContext)
        self._update_toolpath()                          # start / placement / layers may differ

    def connect_simulator(self, time_scale: float = 1.0) -> bool:
        """Start the in-process MCC100 simulator (mlaser.controller.simulator) on localhost UDP
        and connect the real protocol stack to it."""
        cls = _lazy("mlaser.controller.ui_adapter", "UiController")
        if cls is None:
            self.log_panel.log("mlaser.controller not available")
            return False
        if not isinstance(self.ctl, cls):
            self.set_controller(cls(machine=self.machine, cam_params=self.cam_params))
        self.ctl.cam_params = self.cam_params
        return self.ctl.connect_simulator(time_scale=time_scale)

    _last_mode = "offline"
    _MC = {"offline": "mp22", "ready": "mp7", "origin": "mp8", "jog": "mp9", "stop": "mp10",
           "process": "mp11", "pause": "mp55", "frame": "mp58", "dry_run": "mp57"}
    _ZF = {"offline": "mp23", "ready": "mp12", "follow": "mp13", "drill": "mp14", "jog_up": "mp15",
           "jog_down": "mp16", "estop": "mp17"}

    def _poll(self) -> None:
        st = self.ctl.state()
        t = self.tr_.tr
        mc = t(self._MC.get(st.mode, "mp22"))
        if not mc.startswith("MCC") and "<" not in mc:
            mc = f"MCC <{mc}>"
        self.st_mc.setText(mc)
        self.st_zf.setText(t(self._ZF.get(st.ftc, "mp23")))
        self.st_lf.setText(t("mp25" if st.laser == "ready" else "mp24"))
        self.st_xy.setText(f"X: {st.x:.3f}  Y: {st.y:.3f}  Z: {st.z:.3f}")
        self.st_cv.setText(self.tr_.format("mp80", unit="", speed=st.speed))   # status.speed
        if not self.canvas.simulation_running():
            self.canvas.set_head_position(st.x, st.y)
        if st.mode in ("process", "dry_run", "frame"):
            self.canvas.add_track_point(st.x, st.y, st.mode == "process" and st.outputs.get("laser", False))
        elif st.mode == "ready" and self._last_mode in ("process", "dry_run", "frame"):
            self.canvas.add_track_point(st.x, st.y, False)
        self._last_mode = st.mode
        self._replan_if_head_moved(st)

    def _replan_if_head_moved(self, st) -> None:
        """The plan starts at the head position (it is the sort origin and the first rapid's
        start), so an idle head that moved means a new plan (debounced)."""
        h = self._plan_head
        if (st.mode == "ready" and h is not None and self.document.parts
                and abs(st.x - h[0]) + abs(st.y - h[1]) > 0.5):
            self._plan_head = (st.x, st.y)
            self.jobs.schedule_preview(self._preview_request, 0.3)
