"""The context of a built-in extension: :class:`ExtensionContext`.

The built-in extensions (``mlaser.ui.ext_*``, ``mainwindow.EXTENSIONS``) get this subclass of
:class:`mlaser.services.plugins.PluginContext` instead of the plain one.  It adds typed, named
access to the parts of the main window they need (the canvas, the safety UI, the log panel, the
shell, the run and settings entry points ...), so an extension never reads a window attribute by
name: a misspelt name is an ``AttributeError`` at once (and a mypy error), not a silent ``None``
(review 2026-11, A12).  Every accessor reads the window when it is called, so a part installed
after the extensions register (the safety UI) or replaced later (the controller, the machine
profile) is the current one.

``ctx.window`` stays for third-party plugins (docs/dev/PLUGINS.md); the built-ins do not use it
(``tests/test_extension_context.py``).  ``ctx.parent`` is the main window as a Qt parent: for
dialogs, file dialogs and timers, and as the argument of the core UI helpers that take the window
(``busy``, ``job_locked``, ``bundle_button`` ...); an extension does not read attributes of it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from mlaser.services.plugins import PluginContext

if TYPE_CHECKING:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QDockWidget

    from mlaser.cam.toolpath import CamParams
    from mlaser.machine.api import ControllerFacade
    from mlaser.model import Document
    from mlaser.nc.machine import Machine
    from mlaser.services.config import ConfigService, SettingsView
    from mlaser.services.jobs import JobService

    from .canvas import CanvasView
    from .i18n import Translator
    from .mainwindow import MainWindow
    from .panels import ControlPanel, LogPanel
    from .part_list import PartListPanel
    from .ribbon import Ribbon
    from .safety_ui import SafetyUI
    from .shell import Shell


def dialogs_suppressed(window: Any) -> bool:
    """True under the test harness (the window's ``_test_no_dialogs``): no modal question is
    asked.  For the dialogs of the extensions, which get the window only as their parent."""
    return bool(getattr(window, "_test_no_dialogs", False))


def text(key: str, default: str = "", **values: Any) -> str:
    """The text of ``key`` in the application's language (``default`` when the catalogue lacks
    it), for the dialogs of the extensions (the window's translator is the application's)."""
    from .i18n import translator
    t = translator()
    try:
        if values:
            return t.format(key, default=default or None, **values)
        return t.tr(key, default or None)
    except Exception:  # noqa: BLE001 - a broken catalogue entry must not break the screen
        return default or key


class ExtensionContext(PluginContext):
    """:class:`PluginContext` of a built-in extension, with typed access to the main window."""

    host: "MainWindow"

    # ------------------------------------------------------------------ the window itself
    @property
    def parent(self) -> "MainWindow":
        """The main window as the Qt parent of dialogs, file dialogs and timers, and the window
        argument of the core UI helpers.  Not for reading window attributes."""
        return self.host

    @property
    def dialogs_suppressed(self) -> bool:
        """True under the test harness (``_test_no_dialogs``): no modal question is asked."""
        return dialogs_suppressed(self.host)

    # ------------------------------------------------------------------ services
    @property
    def controller(self) -> "ControllerFacade":
        """The live machine controller (read at each access: it can be replaced)."""
        return self.host.ctl

    @property
    def document(self) -> "Document":
        return self.host.document

    @property
    def tr(self) -> "Translator":
        return self.host.tr_

    def text(self, key: str, default: str = "", **values: Any) -> str:
        """The text of ``key`` (``default`` when the catalogue lacks it), ``{placeholders}``
        filled from ``values`` (:func:`mlaser.ui.workspaces.tr`)."""
        from .workspaces import tr
        return tr(self.host, key, default, **values)

    def plural(self, base: str, n: float, **values: Any) -> str:
        """The plural text ``base`` for the count ``n`` (:func:`mlaser.ui.workspaces.trn`)."""
        from .workspaces import trn
        return trn(self.host, base, n, **values)

    @property
    def config(self) -> "ConfigService":
        return self.host.config

    @property
    def jobs(self) -> "JobService":
        return self.host.jobs

    @property
    def config_values(self) -> "SettingsView":
        """The dict view of the set configuration values (``ConfigService.mapping``), as the
        Options dialogs and the original's ``SP.*`` keys use it."""
        return self.host.settings

    @property
    def path(self) -> Optional[str]:
        """The path of the open drawing or project (None: not saved yet)."""
        return self.host.path

    @property
    def machine(self) -> "Machine":
        """The machine profile (:class:`mlaser.nc.machine.Machine`)."""
        return self.host.machine

    @property
    def cam_params(self) -> "CamParams":
        return self.host.cam_params

    @property
    def toolpath(self) -> Any:
        """The current toolpath (None before the first plan)."""
        return self.host.toolpath

    @property
    def estimate(self) -> Optional[dict]:
        """The time / length estimate of the current toolpath (None or {} before one exists)."""
        return self.host.estimate

    def extension(self, name: str) -> Any:
        """What another plugin's ``register`` returned (None when it is not loaded)."""
        return self.host.extension(name)

    def provider(self, name: str) -> Optional[Callable[..., Any]]:
        """The single-slot provider ``name`` (``ctx.provide``), None when nobody provides it."""
        return self.host.hooks.provider(name)

    def add_start_guard(self, guard: Callable[[str], tuple[bool, str]]) -> None:
        """A start interlock: ``guard(mode) -> (allowed, reason)``, asked before every start
        (:meth:`mlaser.services.jobs.JobService.add_start_guard`)."""
        self.host.jobs.add_start_guard(guard)

    # ------------------------------------------------------------------ parts of the window
    @property
    def canvas(self) -> "CanvasView":
        return self.host.canvas

    @property
    def safety_ui(self) -> "SafetyUI":
        """The safety UI (state banner, run bar, pre-flight, fault reset).  Installed after the
        extensions register, so read it when it is needed, not in ``register``."""
        return self.host.safety_ui

    @property
    def log_panel(self) -> "LogPanel":
        return self.host.log_panel

    @property
    def control(self) -> "ControlPanel":
        """The jog / control panel of the window."""
        return self.host.control

    @property
    def part_list(self) -> "PartListPanel":
        return self.host.part_list

    @property
    def shell(self) -> "Shell":
        """The workspace shell (rail, user modes, layout)."""
        return self.host.shell

    @property
    def ribbon(self) -> "Ribbon":
        return self.host.ribbon

    def action(self, cmd_id: str) -> Optional["QAction"]:
        """The window's QAction of a command (None when there is none)."""
        return self.host.ribbon.actions.get(cmd_id)

    @property
    def poll_timer(self) -> "QTimer":
        """The window's status poll timer (the controller's ``poll`` runs on it)."""
        return self.host._timer

    @property
    def left_dock(self) -> "QDockWidget":
        """The left dock (layers) that the Process panel shares as a tab."""
        return self.host.left_dock

    def tabify_dock(self, first: "QDockWidget", second: "QDockWidget") -> None:
        self.host.tabifyDockWidget(first, second)

    def status_message(self, text: str, timeout_ms: int = 0) -> None:
        """A status-bar message (not logged; :meth:`show_message` logs too)."""
        self.host.statusBar().showMessage(text, timeout_ms)

    @property
    def open_filter(self) -> str:
        """The file filter of File > Open (every format the window opens)."""
        return self.host.FILTER

    @property
    def plugin_catalog(self) -> list:
        """The entry-point plugins found at start, ``[info, status]`` (Plugin manager)."""
        return self.host.plugin_catalog

    # ------------------------------------------------------------------ entry points of the window
    def run_job(self, mode: str = "process", selected_only: bool = False,
                on_result: Optional[Callable[..., Any]] = None) -> None:
        """Run the planned job (pre-flight, arming): the only way a built-in starts a cut."""
        self.host.run_job(mode, selected_only=selected_only, on_result=on_result)

    @property
    def mj_params(self) -> Any:
        """The micro-joint parameters of the window (``craft.mj_params``)."""
        return self.host.mj_params

    def sort(self, how: str, **flags: Any) -> None:
        """Order the parts (the window's one sort path, ``sort.*``)."""
        self.host.sort(how, **flags)

    def set_sort_mode(self, mode: int, **flags: Any) -> None:
        self.host.set_sort_mode(mode, **flags)

    def find_child(self, cls: type, name: str) -> Any:
        """A widget of the window by its object name (None when there is none)."""
        return self.host.findChild(cls, name)

    def refresh_views(self) -> None:
        self.host.refresh_views()

    def update_toolpath(self) -> None:
        """Re-plan the toolpath after a change of the CAM parameters."""
        self.host._update_toolpath()

    def set_document(self, doc: "Document") -> None:
        self.host.set_document(doc)

    def settings_dialog(self, key: Optional[str] = None) -> Any:
        """Open the Settings window (at ``key``)."""
        return self.host.settings_dialog(key)

    def confirm_unsaved(self, closing: bool = False) -> bool:
        return self.host.confirm_unsaved(closing)

    def load_path(self, path: str, confirm: bool = True) -> bool:
        """Open a drawing or project file the way File > Open does."""
        return self.host.load_path(path, confirm=confirm)

    def connect_simulator(self, time_scale: float = 1.0) -> bool:
        return self.host.connect_simulator(time_scale)
