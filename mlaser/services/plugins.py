"""The plugin API: :class:`PluginContext`, hooks and plugin discovery.

A plugin is a module (or any object) with ``register(ctx)``; ``ctx`` is the
:class:`PluginContext` the application creates for it.  Everything a plugin does to the
application goes through the context, so there is a contract to code against and conflicts are
detected instead of depending on load order (architecture.md §1.3, P1-5):

* ``ctx.register_command(id, handler, label=..., checkable=...)``: one handler per id
  (:class:`mlaser.services.commands.CommandRegistry`); a duplicate raises.
* ``ctx.edit(fn, label, parts=None)``: an undoable document transaction
  (:class:`mlaser.services.document.DocumentService`); ``ctx.replace_document(doc, label)``.
* hooks: ``ctx.on_controller_changed(fn)``, ``ctx.on_mode_changed(fn)``,
  ``ctx.on_document_changed(fn)``, ``ctx.on_plan(fn)``, ``ctx.on_close(fn)``.
* ``ctx.provide(name, fn)``: single-slot providers (``"nudge.step"``); a second provider raises.
* UI: ``ctx.add_dock(widget, title, area)``, ``ctx.add_ribbon_tab(title, command ids)``,
  ``ctx.add_workspace(name, widget, title, icon=, order=, shortcut=, min_mode=, canvas=,
  toolbar=, docks=, description=, slot=)`` (task workspaces of the Wave 3 shell;
  :mod:`mlaser.services.workspaces`), ``ctx.show_workspace(id)``, ``ctx.user_mode``.
* hooks of the shell: ``ctx.on_workspace_changed(fn)``, ``ctx.on_user_mode_changed(fn)``.
* ``ctx.settings_schema([...Setting])``: typed, validated, persisted plugin settings under
  ``plugin.<name>.<key>`` (:class:`mlaser.services.config.ConfigService`).
* services: ``ctx.docs``, ``ctx.config``, ``ctx.jobs``, ``ctx.commands``; ``ctx.controller``,
  ``ctx.document``, ``ctx.window`` (Qt parent for dialogs), ``ctx.tr``, ``ctx.log(msg)``.

Discovery: the application's built-in plugins (``mlaser.ui.mainwindow.EXTENSIONS``) and every
entry point in the group ``mlaser.plugins`` (the MeerK40t pattern)::

    [project.entry-points."mlaser.plugins"]
    hello = "mlaser_hello:plugin"

This module is Qt-free; the host (``mlaser.ui.mainwindow.MainWindow``) implements the UI calls.
See docs/dev/PLUGINS.md.
"""

from __future__ import annotations

import importlib
import logging
import traceback
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Optional, Protocol

from .commands import CommandRegistry, CommandSpec, DuplicateCommandError
from .config import Setting

log = logging.getLogger("mlaser.plugins")

PLUGIN_API = 1
ENTRY_POINT_GROUP = "mlaser.plugins"
HOOKS = ("controller_changed", "mode_changed", "document_changed", "plan", "close",
         "workspace_changed", "user_mode_changed")
#: the options of ``PluginContext.add_workspace`` (WorkspaceSpec fields a plugin may set)
WORKSPACE_OPTIONS = ("icon", "order", "shortcut", "min_mode", "canvas", "toolbar", "docks",
                     "description", "slot")


class DuplicateProviderError(KeyError):
    pass


class Host(Protocol):
    """What the application gives the plugin API (implemented by ``MainWindow``)."""

    commands: CommandRegistry
    hooks: "Hooks"
    docs: Any
    config: Any
    jobs: Any
    ctl: Any
    tr_: Any

    def log(self, msg: str) -> None: ...
    def host_add_dock(self, widget: Any, title: str, area: str, key: str) -> Any: ...
    def host_add_ribbon_tab(self, title: str, commands: list[str]) -> Any: ...
    def host_add_workspace(self, name: str, widget: Any, title: str) -> Any: ...
    # optional (the Wave 3 shell): host_register_workspace(name, widget, title, options),
    # show_workspace(id), user_mode; a host without them gets host_add_workspace and defaults


class Hooks:
    """Named event lists (``HOOKS``) plus single-slot providers."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[tuple[str, Callable[..., Any]]]] = {h: [] for h in HOOKS}
        self._providers: dict[str, tuple[str, Callable[..., Any]]] = {}
        self.errors: list[tuple[str, str, str]] = []  # (hook, owner, exception)

    def add(self, name: str, fn: Callable[..., Any], owner: str = "core") -> Callable[[], None]:
        if name not in self._hooks:
            raise KeyError(f"unknown hook {name!r} (known: {', '.join(HOOKS)})")
        entry = (owner, fn)
        self._hooks[name].append(entry)
        return lambda: self._hooks[name].remove(entry) if entry in self._hooks[name] else None

    def emit(self, name: str, *args) -> None:
        for owner, fn in list(self._hooks.get(name, ())):
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 - one plugin must not break the others
                self.errors.append((name, owner, f"{type(exc).__name__}: {exc}"))
                log.error("hook %s of %s failed:\n%s", name, owner, traceback.format_exc())

    def count(self, name: str) -> int:
        return len(self._hooks.get(name, ()))

    def provide(self, name: str, fn: Callable[..., Any], owner: str = "core") -> None:
        if name in self._providers:
            raise DuplicateProviderError(f"provider {name!r} already set by {self._providers[name][0]!r}")
        self._providers[name] = (owner, fn)

    def provider(self, name: str) -> Optional[Callable[..., Any]]:
        p = self._providers.get(name)
        return p[1] if p else None

    def remove_owner(self, owner: str) -> None:
        for name, lst in self._hooks.items():
            self._hooks[name] = [e for e in lst if e[0] != owner]
        for k in [k for k, v in self._providers.items() if v[0] == owner]:
            del self._providers[k]


class PluginSettings:
    """A plugin's typed settings (``plugin.<name>.<key>`` in the ConfigService)."""

    def __init__(self, config: Any, prefix: str, keys: Iterable[str]) -> None:
        self._config, self._prefix, self.keys = config, prefix, list(keys)

    def _k(self, key: str) -> str:
        if key not in self.keys:
            raise KeyError(f"{key!r} is not in the settings schema of {self._prefix[:-1]!r}")
        return self._prefix + key

    def __getitem__(self, key: str) -> Any:
        return self._config.get(self._k(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._config.set(self._k(key), value)

    def get(self, key: str) -> Any:
        return self[key]

    def setting(self, key: str) -> Setting:
        return self._config.setting(self._k(key))


class PluginContext:
    """The API object of one plugin (see the module docstring)."""

    def __init__(self, host: Host, name: str) -> None:
        self.host = host
        self.name = name
        self.commands_registered: list[str] = []
        self.settings: Optional[PluginSettings] = None

    # ------------------------------------------------------------------ services
    @property
    def docs(self):
        return self.host.docs

    @property
    def config(self):
        return self.host.config

    @property
    def jobs(self):
        return self.host.jobs

    @property
    def commands(self) -> CommandRegistry:
        return self.host.commands

    @property
    def document(self):
        return self.host.docs.document

    @property
    def controller(self):
        return self.host.ctl

    @property
    def window(self):
        """The application window: the Qt parent for a plugin's dialogs and widgets."""
        return self.host

    @property
    def tr(self):
        return self.host.tr_

    def log(self, msg: str) -> None:
        self.host.log(msg)

    # ------------------------------------------------------------------ commands
    def register_command(
        self,
        cmd_id: str,
        handler: Callable[..., Any],
        label: str = "",
        checkable: bool = False,
        min_mode: str = "",
        workspace: str = "",
    ) -> CommandSpec:
        """Bind ``cmd_id`` (a ribbon command, or a new one with ``label`` as its lang key or
        text) to ``handler``.  A checkable command's handler receives the checked state.
        ``min_mode`` (``"operator"`` / ``"engineer"`` / ``"service"``) is the lowest user mode
        that may run it (default: :func:`mlaser.services.usermodes.default_min_mode`);
        ``workspace`` the workspace it belongs to (palette, shortcut sheet).
        Raises :class:`DuplicateCommandError` when the id is already bound."""
        spec = self.host.commands.register(
            cmd_id, handler, self.name, label or cmd_id, checkable, min_mode=min_mode, workspace=workspace
        )
        self.commands_registered.append(cmd_id)
        return spec

    def register_commands(self, table: dict[str, Callable[..., Any]]) -> None:
        for cmd_id, fn in table.items():
            self.register_command(cmd_id, fn)

    # ------------------------------------------------------------------ document
    def edit(self, fn: Callable[[Any], Any], label: str = "", parts=None) -> Any:
        """Run ``fn(document)`` as one undoable transaction and refresh the views."""
        return self.host.docs.edit(fn, label or f"{self.name}: edit", parts=parts)

    def replace_document(self, doc: Any, label: str = "", path: Optional[str] = None) -> None:
        """Make ``doc`` the open document (undoable)."""
        self.host.docs.replace(doc, path, label or f"{self.name}: replace")

    # ------------------------------------------------------------------ hooks
    def on(self, hook: str, fn: Callable[..., Any]) -> Callable[[], None]:
        return self.host.hooks.add(hook, fn, self.name)

    def on_controller_changed(self, fn: Callable[[Any], Any]) -> Callable[[], None]:
        """``fn(controller)`` after the machine controller was replaced."""
        return self.on("controller_changed", fn)

    def on_mode_changed(self, fn: Callable[[str], Any]) -> Callable[[], None]:
        """``fn(mode)`` when the canvas interaction mode changes (select / pan / node; Esc)."""
        return self.on("mode_changed", fn)

    def on_document_changed(self, fn: Callable[[str], Any]) -> Callable[[], None]:
        """``fn(kind)`` after an edit, undo, redo or a new document."""
        return self.on("document_changed", fn)

    def on_plan(self, fn: Callable[[Any], Any]) -> Callable[[], None]:
        """``fn(planned_job)`` when the JobService has a new PlannedJob."""
        return self.on("plan", fn)

    def on_close(self, fn: Callable[[], Any]) -> Callable[[], None]:
        return self.on("close", fn)

    def on_workspace_changed(self, fn: Callable[[str], Any]) -> Callable[[], None]:
        """``fn(workspace_id)`` after the shell switched workspace (``"run"``, ``"<plugin>.x"``)."""
        return self.on("workspace_changed", fn)

    def on_user_mode_changed(self, fn: Callable[[str], Any]) -> Callable[[], None]:
        """``fn(mode)`` after the user mode changed (``operator`` / ``engineer`` / ``service``)."""
        return self.on("user_mode_changed", fn)

    def provide(self, name: str, fn: Callable[..., Any]) -> None:
        self.host.hooks.provide(name, fn, self.name)

    # ------------------------------------------------------------------ UI
    def add_dock(self, widget: Any, title: str, area: str = "right", key: str = "") -> Any:
        return self.host.host_add_dock(widget, title, area, key or f"plugin.{self.name}.{title}")

    def add_ribbon_tab(self, title: str, commands: list[str]) -> Any:
        """A ribbon tab with one button per command id (registered or ribbon commands)."""
        return self.host.host_add_ribbon_tab(title, commands)

    def add_workspace(self, name: str, widget: Any, title: str = "", **options: Any) -> Any:
        """A task workspace in the shell's rail (docs/dev/PLUGINS.md §5).

        ``name`` (or ``title``, or ``slot=``) equal to a built-in slot (``design``, ``process``,
        ``nest``, ``run``, ``report``, ``machine``) fills that slot; anything else gets its own
        rail button.  ``options`` are :class:`mlaser.services.workspaces.WorkspaceSpec` fields:
        ``icon`` (Tabler name), ``order``, ``shortcut``, ``min_mode``, ``canvas`` (show the
        shared canvas, ``widget`` as its side panel), ``toolbar`` (command ids), ``docks``
        (dock object names shown in it), ``description``, ``slot``.  Returns ``widget``.
        The id is ``"<plugin>.<name>"``; the three-argument form of Wave 2 still works."""
        unknown = set(options) - set(WORKSPACE_OPTIONS)
        if unknown:
            raise TypeError(f"add_workspace: unknown option(s) {', '.join(sorted(unknown))}")
        ws_id = f"{self.name}.{name}"
        reg = getattr(self.host, "host_register_workspace", None)
        if callable(reg):
            return reg(ws_id, widget, title or name, options)
        return self.host.host_add_workspace(ws_id, widget, title or name)

    def show_workspace(self, ws_id: str) -> bool:
        """Switch the shell to ``ws_id`` (a slot name or ``"<plugin>.<name>"``); False when the
        host has no shell or the user mode hides it."""
        fn = getattr(self.host, "show_workspace", None)
        return bool(fn(ws_id)) if callable(fn) else False

    @property
    def user_mode(self) -> str:
        """The current user mode (``"engineer"`` when the host has no shell)."""
        return getattr(self.host, "user_mode", "engineer")

    # ------------------------------------------------------------------ settings
    def settings_schema(self, settings: Iterable[Setting]) -> PluginSettings:
        prefix = f"plugin.{self.name}."
        items = [
            replace(
                s,
                key=prefix + s.key,
                section=s.section or f"plugin.{self.name}",
                source=f"plugin:{self.name}",
            )
            for s in settings
        ]
        self.host.config.register(items)
        self.settings = PluginSettings(self.host.config, prefix, [s.key[len(prefix) :] for s in items])
        return self.settings


# --------------------------------------------------------------------------- loading
@dataclass
class PluginInfo:
    name: str
    target: str  # "module" or "module:attr"
    origin: str = "builtin"  # builtin | entry-point


def discover(
    builtin: Iterable[str] = (), group: str = ENTRY_POINT_GROUP, entry_points: bool = True
) -> list[PluginInfo]:
    """Built-in plugin module paths first, then the ``group`` entry points (sorted by name)."""
    out = [PluginInfo(b.rsplit(".", 1)[-1], b) for b in builtin]
    if entry_points:
        try:
            from importlib.metadata import entry_points as _eps

            eps = sorted(_eps(group=group), key=lambda e: e.name)
        except Exception:  # noqa: BLE001 - broken metadata must not stop the app
            eps = []
        seen = {p.name for p in out}
        for ep in eps:
            if ep.name in seen:
                log.error("plugin entry point %s ignored: the name is taken", ep.name)
                continue
            out.append(PluginInfo(ep.name, ep.value, "entry-point"))
    return out


def resolve(target: str) -> Any:
    mod, _, attr = target.partition(":")
    obj: Any = importlib.import_module(mod)
    for a in filter(None, attr.split(".")):
        obj = getattr(obj, a)
    return obj


def register_plugin(host: Host, info: PluginInfo) -> tuple[Optional[PluginContext], Any]:
    """Import and register one plugin.  A failure is logged (``host.log`` and logging) and its
    partial registrations are removed; returns (context, register() result) or (None, error)."""
    try:
        obj = resolve(info.target)
    except Exception as exc:  # noqa: BLE001 - reported
        msg = f"plugin {info.name}: import failed: {type(exc).__name__}: {exc}"
        log.error("%s\n%s", msg, traceback.format_exc())
        host.log(msg)
        return None, exc
    reg = getattr(obj, "register", None) if not callable(obj) or hasattr(obj, "register") else obj
    if reg is None or not callable(reg):
        msg = f"plugin {info.name}: no register(ctx)"
        host.log(msg)
        return None, TypeError(msg)
    need = getattr(obj, "PLUGIN_API", PLUGIN_API)
    if need > PLUGIN_API:
        msg = f"plugin {info.name}: needs plugin API {need}, this is {PLUGIN_API}"
        host.log(msg)
        return None, RuntimeError(msg)
    ctx = PluginContext(host, info.name)
    try:
        return ctx, reg(ctx)
    except Exception as exc:  # noqa: BLE001 - reported, rolled back
        host.commands.unregister_owner(info.name)
        host.hooks.remove_owner(info.name)
        kind = "duplicate command" if isinstance(exc, DuplicateCommandError) else type(exc).__name__
        msg = f"plugin {info.name} failed ({kind}): {exc}"
        log.error("%s\n%s", msg, traceback.format_exc())
        host.log(msg)
        return None, exc
