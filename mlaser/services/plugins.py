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
* plugin API 2 (docs/dev/PLUGIN_STANDARD.md §4): ``ctx.manifest``, ``ctx.permissions``,
  ``ctx.data_dir``, ``ctx.http`` (:mod:`.plugin_http`), ``ctx.secrets``,
  ``ctx.run_in_background(fn, on_done=, on_error=)``, ``ctx.services`` (orders, technology,
  queue, intake, config) and ``ctx.show_message(text, level)``.  An entry-point plugin declares
  its permissions in ``mlaser-plugin.toml`` (:mod:`.plugin_manifest`); the application enforces
  them on its own APIs (http hosts, secrets, the controller, the document, orders and queue).
  A legacy entry-point plugin (no manifest) keeps API-1 behaviour and gets none of the new
  services.

Discovery: the application's built-in plugins (``mlaser.ui.mainwindow.EXTENSIONS``) and every
entry point in the group ``mlaser.plugins`` (the MeerK40t pattern)::

    [project.entry-points."mlaser.plugins"]
    hello = "mlaser_hello:plugin"

**Trust model** (docs/dev/PLUGINS.md §6): a plugin is code running in the application's process
with the user's privileges and the machine's authority; nothing here sandboxes it.  What this
module enforces:

* entry-point plugins are **opt-in**: :func:`discover` lists them, but the application loads only
  those in the persisted allowlist (``plugins.allowlist``: name -> entry-point target, edited in
  Service mode; :func:`allowed`), and a changed target needs enabling again;
* an enabled plugin whose manifest permissions changed since they were accepted
  (``plugins.accepted``) shows as ``changed`` and needs enabling again; a manifest whose ``api``
  or ``mlaser`` range excludes this application is refused with a log line;
* they load **after** the safety UI exists (``MainWindow``), and cannot register commands in the
  core's reserved namespaces (``run.*``, ``shell.*``, ``workspace.*``: :data:`RESERVED_PREFIXES`),
  so a plugin can neither pre-claim Stop / Hold / Arm nor make start-up fail;
* ``ctx.controller`` of a third-party plugin is ``mlaser.controller.plugin_view.plugin_view(ctl,
  name)`` (Safety core: no guard internals; its ``arm`` passes ``source="plugin:<name>"``), and
  :func:`plugin_policy` decides the safety actions a plugin asks for (arm, reset a fault, hold
  the heartbeat, reconfigure safety): refused unless the service engineer granted that plugin
  machine authority (``plugins.authority``).  Installed with ``ctl.set_plugin_policy``
  (:func:`install_policy`).

This module is Qt-free; the host (``mlaser.ui.mainwindow.MainWindow``) implements the UI calls.
See docs/dev/PLUGINS.md.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol

from .commands import CommandRegistry, CommandSpec, DuplicateCommandError
from .config import Setting
from .plugin_manifest import (Permissions, check_identity, compatibility_errors, icon_for_dist, manifest_for_dist,
                              normalize_permissions)

log = logging.getLogger("mlaser.plugins")

#: the plugin API this application implements (2: the manifest, permissions and the services of
#: docs/dev/PLUGIN_STANDARD.md; additive to API 1)
PLUGIN_API = 2
ENTRY_POINT_GROUP = "mlaser.plugins"
HOOKS = ("controller_changed", "mode_changed", "document_changed", "plan", "close",
         "workspace_changed", "user_mode_changed",
         "document_imported")         # fn(path, document) after File > Open / Import read a drawing
#: the options of ``PluginContext.add_workspace`` (WorkspaceSpec fields a plugin may set)
WORKSPACE_OPTIONS = ("icon", "order", "shortcut", "min_mode", "canvas", "toolbar", "docks",
                     "description", "slot")


#: command namespaces of the core (safety controls, shell, workspaces): only built-in plugins
#: may register there
RESERVED_PREFIXES = ("run.", "shell.", "workspace.")
#: the ConfigService key of the entry-point allowlist ({name: "module:attr"})
ALLOWLIST_KEY = "plugins.allowlist"
#: plugin names the service engineer granted machine authority (arm, reset faults, hold the
#: heartbeat, reconfigure safety through the controller's plugin policy)
AUTHORITY_KEY = "plugins.authority"
#: {name: normalised manifest permissions} accepted when the plugin was enabled
ACCEPTED_KEY = "plugins.accepted"
#: {name: [host, ...]}: network hosts granted to a plugin in the Plugin manager (Service mode,
#: audited ``plugin_network_grant``), in addition to its manifest's ``permissions.network``
NETWORK_KEY = "plugins.network"


class DuplicateProviderError(KeyError):
    pass


class DocumentLockedError(RuntimeError):
    """``replace_document`` while a job runs or a breakpoint is pending."""


class ReservedCommandError(PermissionError):
    """A plugin that is not built in tried to register a command of the core's namespaces."""


def controller_for(ctl: Any, plugin: str, origin: str) -> Any:
    """What ``ctx.controller`` returns: the controller itself for a built-in plugin, the
    Safety-core ``plugin_view(ctl, plugin)`` for any other.  Fails closed: there is no fallback
    that would hand a third-party plugin the controller itself."""
    if origin == "builtin" or ctl is None:
        return ctl
    from mlaser.controller.plugin_view import plugin_view
    return plugin_view(ctl, plugin)


def plugin_policy(config: Any) -> Callable[[str, str, str], Optional[str]]:
    """The controller's plugin policy (``fn(source, action, reason) -> None | refusal``): a
    plugin (``source="plugin:<name>"``) may perform a safety action only when it is enabled
    (allowlist) and the service engineer granted it machine authority; everything else is
    refused with the reason shown to the operator.  Other sources are not judged here."""

    def policy(source: str, action: str, reason: str = "") -> Optional[str]:
        if not str(source).startswith("plugin:"):
            return None
        name = str(source).split(":", 1)[1]
        allow = config.get(ALLOWLIST_KEY, {}) or {}
        granted = config.get(AUTHORITY_KEY, []) or []
        if name in allow and name in granted:
            return None
        return (f"plugin {name!r} may not {action}: it has no machine authority "
                "(Options+ > Plugins, Service mode)")

    return policy


def install_policy(ctl: Any, config: Any) -> bool:
    """Give the controller :func:`plugin_policy` (``ctl.set_plugin_policy``, Safety core);
    every MachineAPI controller has it (the demo's base implementation included)."""
    fn = getattr(ctl, "set_plugin_policy", None)
    if not callable(fn):
        return False
    fn(plugin_policy(config))
    return True


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

    def __init__(self, host: Host, name: str, origin: str = "builtin", manifest: Optional[dict] = None) -> None:
        self.host = host
        self.name = name
        self.origin = origin                # builtin | entry-point
        self.commands_registered: list[str] = []
        self.settings: Optional[PluginSettings] = None
        self._manifest = manifest
        if origin == "builtin":
            self._permissions = Permissions.everything()
        elif manifest is None:
            self._permissions = Permissions(legacy=True)
        else:
            self._permissions = Permissions.from_dict(manifest.get("permissions", {}))
        self._http: Any = None
        self._secrets: Any = None
        self._services: Any = None
        self._icon = ""

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
        """The machine controller (built-in plugins), or the Safety-core plugin view of it
        (:func:`controller_for`).  A plugin with a manifest needs ``permissions.machine`` **and**
        the service engineer's machine-authority grant (``plugins.authority``), else
        ``PermissionError``; a legacy plugin keeps the API-1 rule (the view, and the controller's
        plugin policy for every safety action)."""
        if self._manifest is not None and self.origin != "builtin":
            if not self._permissions.machine:
                raise PermissionError(f"plugin {self.name!r} did not declare permissions.machine")
            cfg = getattr(self.host, "config", None)
            granted = (cfg.get(AUTHORITY_KEY, []) or []) if cfg is not None else []
            if self.name not in granted:
                raise PermissionError(f"plugin {self.name!r} has no machine authority "
                                      "(Plugin manager, Service mode)")
        return controller_for(self.host.ctl, self.name, self.origin)

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
        icon: str = "",
    ) -> CommandSpec:
        """Bind ``cmd_id`` (a ribbon command, or a new one with ``label`` as its lang key or
        text) to ``handler``.  A checkable command's handler receives the checked state.
        ``min_mode`` (``"operator"`` / ``"engineer"`` / ``"service"``) is the lowest user mode
        that may run it (default: :func:`mlaser.services.usermodes.default_min_mode`);
        ``workspace`` the workspace it belongs to (palette, shortcut sheet); ``icon`` a Tabler icon
        name or an icon file (``ctx.icon``).
        Raises :class:`DuplicateCommandError` when the id is already bound, and
        :class:`ReservedCommandError` for an id of the core's namespaces (``run.*``, ``shell.*``,
        ``workspace.*``) from a plugin that is not built in."""
        if self.origin != "builtin" and cmd_id.startswith(RESERVED_PREFIXES):
            raise ReservedCommandError(f"command {cmd_id!r} is in a namespace reserved for the core "
                                       f"({', '.join(RESERVED_PREFIXES)})")
        if not min_mode and self._manifest is not None:
            min_mode = str(self._manifest.get("plugin", {}).get("min_mode", "") or "")
        spec = self.host.commands.register(
            cmd_id, handler, self.name, label or cmd_id, checkable, min_mode=min_mode, workspace=workspace
        )
        self.commands_registered.append(cmd_id)
        if icon:
            fn = getattr(self.host, "host_command_icon", None)
            if callable(fn):
                fn(cmd_id, icon)
        return spec

    def register_commands(self, table: dict[str, Callable[..., Any]]) -> None:
        for cmd_id, fn in table.items():
            self.register_command(cmd_id, fn)

    # ------------------------------------------------------------------ document
    def edit(self, fn: Callable[[Any], Any], label: str = "", parts=None) -> Any:
        """Run ``fn(document)`` as one undoable transaction and refresh the views
        (a plugin with a manifest needs ``permissions.document``)."""
        self._need("document")
        return self.host.docs.edit(fn, label or f"{self.name}: edit", parts=parts)

    def replace_document(self, doc: Any, label: str = "", path: Optional[str] = None) -> None:
        """Make ``doc`` the open document (undoable).  Refused (:class:`DocumentLockedError`, the
        reason shown to the operator) while a job runs or a breakpoint is pending: the cut must
        not continue on another drawing (review 2, P1-5).  Needs ``permissions.document``."""
        self._need("document")
        locked = getattr(self.host, "document_locked", None)
        if callable(locked) and locked(f"{self.name}: replace the document"):
            raise DocumentLockedError("the document is locked while a job runs or a breakpoint is pending")
        ctl = getattr(self.host, "ctl", None)
        job_active = getattr(ctl, "job_active", None)
        if not callable(locked) and callable(job_active) and job_active():
            raise DocumentLockedError("the document is locked while a job is active (Stop it first)")
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

    # ------------------------------------------------------------------ plugin API 2
    def _need(self, permission: str) -> None:
        """Enforce a declared permission (built-ins and legacy plugins: API-1 rules)."""
        if self._permissions.all or self._permissions.legacy:
            return
        if not self._permissions.allows(permission):
            raise PermissionError(f"plugin {self.name!r} did not declare permissions.{permission} "
                                  "(mlaser-plugin.toml)")

    def _api2(self, what: str) -> None:
        if self._permissions.legacy:
            raise PermissionError(f"{what} is plugin API 2: plugin {self.name!r} has no manifest "
                                  "(mlaser-plugin.toml, docs/dev/PLUGIN_STANDARD.md)")

    @property
    def manifest(self) -> Optional[dict]:
        """The parsed ``mlaser-plugin.toml`` (None: a built-in or legacy plugin)."""
        return self._manifest

    @property
    def icon(self) -> str:
        """The absolute path of the manifest's ``icon`` (SVG / PNG inside the package; "" when there
        is none), for ``add_workspace(icon=ctx.icon)`` and ``register_command(icon=ctx.icon)``.
        Found from the distribution's file list without importing."""
        return self._icon

    @property
    def permissions(self) -> Permissions:
        """The declared permissions, frozen (built-ins: everything)."""
        return self._permissions

    @property
    def data_dir(self) -> Path:
        """``<data dir>/plugins/data/<id>/``, created on first access."""
        self._api2("ctx.data_dir")
        from .plugin_manager import plugin_data_dir
        d = plugin_data_dir(self.name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def http(self):
        """The HTTP client (:class:`mlaser.services.plugin_http.HttpClient`): https only, hosts
        of ``permissions.network`` only."""
        self._api2("ctx.http")
        if self._http is None:
            from .plugin_http import HttpClient
            self._http = HttpClient(self.allows_host, owner="" if self._permissions.all else self.name)
        return self._http

    def granted_hosts(self) -> list[str]:
        """Hosts a service engineer granted this plugin in the Plugin manager (``plugins.network``),
        in addition to the manifest's ``network`` list (e.g. a company's own server)."""
        cfg = getattr(self.host, "config", None)
        grants = (cfg.get(NETWORK_KEY, {}) or {}) if cfg is not None else {}
        hosts = grants.get(self.name, []) if isinstance(grants, dict) else []
        return [h for h in hosts if isinstance(h, str)]

    def allows_host(self, host: str) -> bool:
        """``permissions.network`` or a granted host (read at every request: a grant applies
        at once)."""
        if self._permissions.allows_host(host):
            return True
        from .plugin_manifest import host_matches
        return any(host_matches(host, h) for h in self.granted_hosts())

    @property
    def secrets(self) -> "PluginSecrets":
        """``get`` / ``set`` / ``delete`` of credentials, namespaced to this plugin; needs
        ``permissions.secrets``."""
        self._api2("ctx.secrets")
        self._need("secrets")
        if self._secrets is None:
            self._secrets = PluginSecrets(self.name, self.data_dir if not self._permissions.all
                                          else _builtin_data_dir(self.name))
        return self._secrets

    def run_in_background(self, fn: Callable[..., Any], *, on_done: Optional[Callable[[Any], Any]] = None,
                          on_error: Optional[Callable[[BaseException], Any]] = None,
                          on_progress: Optional[Callable[[Any], Any]] = None) -> "BackgroundTask":
        """Run ``fn`` on a worker thread; ``on_done(result)`` / ``on_error(exc)`` run on the GUI
        thread.  ``handle.cancel()`` sets ``handle.cancelled`` (``fn`` checks it).

        Progress: when ``fn`` has a parameter named ``progress`` (or ``**kwargs``) it is called as
        ``fn(progress=report)``, else as ``fn()``; ``report(value)`` (a fraction 0..1, a text, or
        anything) may be called as often as ``fn`` likes: ``on_progress(value)`` runs on the GUI
        thread with the latest value only (coalesced: at most one delivery pending)."""
        task = BackgroundTask(self, fn, on_done, on_error, on_progress)
        task.start()
        return task

    def _post(self, fn: Callable[[], Any]) -> None:
        post = getattr(self.host, "post_to_gui", None)
        if not callable(post):
            post = getattr(getattr(self.host, "jobs", None), "post", None)
        if callable(post):
            post(fn)
        else:
            fn()                        # a host without a GUI thread (tests): deliver here

    @property
    def services(self) -> "PluginServices":
        """The window's public service objects: ``orders``, ``technology``, ``queue``,
        ``intake``, ``config`` (None when that part is absent)."""
        if self._services is None:
            self._services = PluginServices(self)
        return self._services

    def show_message(self, text: str, level: str = "info") -> None:
        """A status-bar message (also in the log panel)."""
        text = str(text)
        sb = getattr(self.host, "statusBar", None)
        if callable(sb):
            try:
                sb().showMessage(text, 8000)
            except Exception:  # noqa: BLE001 - the log line below still tells
                pass
        prefix = "" if level == "info" else f"{level.upper()}: "
        self.host.log(f"{self.name}: {prefix}{text}")

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


_SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _builtin_data_dir(name: str) -> Path:
    from .plugin_manager import plugin_data_dir
    return plugin_data_dir(name)


class PluginSecrets:
    """``ctx.secrets``: the notifications' secret store (:class:`mlaser.notify.secrets.SecretStore`:
    the keyring when available, else a 0600 JSON file), here under the names
    ``plugin.<id>.<key>`` and with the file in the plugin's data directory.  Values are never
    logged."""

    FILE = "secrets.json"

    def __init__(self, plugin: str, directory: Path, keyring_module: Any = None) -> None:
        from mlaser.notify.secrets import SecretStore
        self.plugin = plugin
        use_kr = keyring_module is not None or not _under_test()
        self._store = SecretStore(Path(directory) / self.FILE, use_keyring=use_kr, keyring_module=keyring_module)

    @property
    def backend(self) -> str:
        return self._store.backend

    def _name(self, key: str) -> str:
        if not isinstance(key, str) or not _SECRET_KEY_RE.match(key):
            raise ValueError("a secret key is 1-64 letters, digits, '_', '.', '-'")
        return f"plugin.{self.plugin}.{key}"

    def get(self, key: str) -> Optional[str]:
        return self._store.get(self._name(key)) or None

    def set(self, key: str, value: Optional[str]) -> None:
        if value is not None and not isinstance(value, str):
            raise TypeError("a secret value is a string")
        self._store.set(self._name(key), value or "")

    def delete(self, key: str) -> None:
        self._store.set(self._name(key), "")

    def __repr__(self) -> str:
        return f"<PluginSecrets {self.plugin} ({self.backend})>"


def _under_test() -> bool:
    import sys
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


class BackgroundTask:
    """The handle of ``ctx.run_in_background``."""

    def __init__(self, ctx: PluginContext, fn: Callable[..., Any], on_done: Optional[Callable[[Any], Any]],
                 on_error: Optional[Callable[[BaseException], Any]],
                 on_progress: Optional[Callable[[Any], Any]] = None) -> None:
        self.ctx, self.fn, self.on_done, self.on_error = ctx, fn, on_done, on_error
        self.on_progress = on_progress
        self._progress_lock = threading.Lock()
        self._progress_value: Any = None
        self._progress_pending = False
        self.cancelled = False
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, name=f"mlaser-plugin-{ctx.name}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.cancelled = True

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self.done.wait(timeout)

    def _deliver(self, cb: Optional[Callable[[Any], Any]], arg: Any) -> None:
        if cb is None:
            return

        def call() -> None:
            try:
                cb(arg)
            except Exception:  # noqa: BLE001 - a plugin callback must not break the GUI loop
                msg = f"plugin {self.ctx.name}: background callback failed"
                log.error("%s\n%s", msg, traceback.format_exc())
                self.ctx.log(msg)

        self.ctx._post(call)

    def progress(self, value: Any) -> None:
        """Called by ``fn`` on the worker thread; delivered coalesced on the GUI thread."""
        if self.on_progress is None:
            return
        with self._progress_lock:
            self._progress_value = value
            if self._progress_pending:
                return
            self._progress_pending = True

        def deliver() -> None:
            with self._progress_lock:
                v = self._progress_value
                self._progress_pending = False
            try:
                assert self.on_progress is not None
                self.on_progress(v)
            except Exception:  # noqa: BLE001
                log.error("plugin %s: progress callback failed\n%s", self.ctx.name, traceback.format_exc())

        self.ctx._post(deliver)

    def _wants_progress(self) -> bool:
        import inspect
        try:
            params = inspect.signature(self.fn).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(p.name == "progress" and p.kind != p.POSITIONAL_ONLY or p.kind == p.VAR_KEYWORD
                   for p in params)

    def _run(self) -> None:
        try:
            self.result = self.fn(progress=self.progress) if self._wants_progress() else self.fn()
        except BaseException as exc:  # noqa: BLE001 - delivered to on_error
            self.error = exc
            if self.on_error is None:
                log.error("plugin %s: background task failed: %s: %s", self.ctx.name, type(exc).__name__, exc)
            self.done.set()
            self._deliver(self.on_error, exc)
            return
        self.done.set()
        self._deliver(self.on_done, self.result)


class PluginServices:
    """``ctx.services``: the public service objects of the window, looked up when asked for.
    ``orders`` needs ``permissions.orders``; ``queue`` and ``intake`` need ``permissions.queue``."""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def _provider(self, name: str) -> Any:
        hooks = getattr(self._ctx.host, "hooks", None)
        fn = hooks.provider(name) if hooks is not None else None
        try:
            return fn() if callable(fn) else None
        except Exception:  # noqa: BLE001 - an absent part reads as None
            return None

    @property
    def orders(self) -> Any:
        self._ctx._need("orders")
        return self._provider("orders")

    @property
    def queue(self) -> Any:
        self._ctx._need("queue")
        return self._provider("queue")

    @property
    def intake(self) -> Any:
        self._ctx._need("queue")
        return self._provider("intake")

    @property
    def orders_load_group(self) -> Any:
        """``fn(key, replace=None)``: load one material x thickness group of the order book into
        Nest (the Orders plugin's ``orders.load_group`` provider); None when absent."""
        self._ctx._need("orders")
        hooks = getattr(self._ctx.host, "hooks", None)
        fn = hooks.provider("orders.load_group") if hooks is not None else None
        return fn if callable(fn) else None

    @property
    def technology(self) -> Any:
        tech = self._provider("technology")
        if tech is not None:
            return tech
        ext = getattr(self._ctx.host, "extension", None)
        proc = ext("ext_process") if callable(ext) else None
        return getattr(proc, "store", None)

    @property
    def config(self) -> Any:
        return getattr(self._ctx.host, "config", None)


# --------------------------------------------------------------------------- loading
@dataclass
class PluginInfo:
    name: str
    target: str  # "module" or "module:attr"
    origin: str = "builtin"  # builtin | entry-point
    dist: str = ""  # "package 1.2.3" of an entry point (for the Plugins dialog)
    manifest: Optional[dict] = None          # mlaser-plugin.toml (read without importing)
    manifest_error: str = ""                  # an invalid manifest: never loaded
    dist_name: str = ""
    version: str = ""
    location: str = ""                        # "site" (plugin manager) or "environment"
    icon: str = ""                            # absolute path of the manifest's icon ("" none)

    @property
    def legacy(self) -> bool:
        return self.origin != "builtin" and self.manifest is None and not self.manifest_error

    @property
    def permissions(self) -> Optional[Permissions]:
        if self.manifest is None:
            return None
        return Permissions.from_dict(self.manifest.get("permissions", {}))


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
            dist = getattr(ep, "dist", None)
            label = f"{getattr(dist, 'name', '')} {getattr(dist, 'version', '')}".strip() if dist else ""
            manifest, err = manifest_for_dist(dist, ep.value)
            if manifest is not None:
                bad = check_identity(manifest, ep.name, ep.value, str(getattr(dist, "version", "") or ""))
                if bad:
                    manifest, err = None, "; ".join(bad)
            out.append(PluginInfo(ep.name, ep.value, "entry-point", label, manifest, err,
                                  str(getattr(dist, "name", "") or "") if dist else "",
                                  str(getattr(dist, "version", "") or "") if dist else "",
                                  _location(dist), icon_for_dist(dist, ep.value, manifest)))
    return out


def _location(dist: Any) -> str:
    if dist is None:
        return ""
    try:
        from .plugin_manager import site_dir
        base = str(Path(dist.locate_file("")).resolve())
        return "site" if Path(base) == site_dir().resolve() else "environment"
    except Exception:  # noqa: BLE001
        return "environment"


def allowed(info: PluginInfo, allowlist: Optional[dict]) -> bool:
    """Is this entry-point plugin enabled?  Only when the allowlist names it **with the same
    target** (a package that later points the name at other code needs enabling again)."""
    if info.origin == "builtin":
        return True
    return bool(allowlist) and isinstance(allowlist, dict) and allowlist.get(info.name) == info.target


def plugin_status(info: PluginInfo, allowlist: Optional[dict], accepted: Optional[dict] = None) -> str:
    """``enabled`` / ``disabled`` / ``changed`` (enabled for another target, or its manifest
    permissions differ from the ones accepted at enabling: ``accepted`` = ``plugins.accepted``)."""
    if allowed(info, allowlist):
        if info.manifest is not None and isinstance(accepted, dict) and info.name in accepted:
            try:
                same = normalize_permissions(info.manifest.get("permissions", {})) == \
                    normalize_permissions(accepted[info.name])
            except ValueError:
                same = False
            if not same:
                return "changed"
        return "enabled"
    if isinstance(allowlist, dict) and info.name in allowlist:
        return "changed"
    return "disabled"


def resolve(target: str) -> Any:
    mod, _, attr = target.partition(":")
    obj: Any = importlib.import_module(mod)
    for a in filter(None, attr.split(".")):
        obj = getattr(obj, a)
    return obj


def register_plugin(host: Host, info: PluginInfo) -> tuple[Optional[PluginContext], Any]:
    """Import and register one plugin.  A failure is logged (``host.log`` and logging) and its
    partial registrations are removed; returns (context, register() result) or (None, error).
    An entry-point plugin with an invalid manifest, or whose manifest ``api`` / ``mlaser`` range
    excludes this application, is refused before it is imported."""
    if info.origin != "builtin":
        if info.manifest_error:
            msg = f"plugin {info.name}: invalid manifest, not loaded: {info.manifest_error}"
            log.error(msg)
            host.log(msg)
            return None, RuntimeError(msg)
        if info.manifest is not None:
            bad = compatibility_errors(info.manifest, PLUGIN_API)
            if bad:
                msg = f"plugin {info.name}: incompatible, not loaded: {'; '.join(bad)}"
                log.error(msg)
                host.log(msg)
                return None, RuntimeError(msg)
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
    ctx = PluginContext(host, info.name, info.origin, info.manifest if info.origin != "builtin" else None)
    ctx._icon = info.icon
    try:
        result = reg(ctx)
        if info.origin != "builtin":
            _audit_loaded(info)
        return ctx, result
    except Exception as exc:  # noqa: BLE001 - reported, rolled back
        host.commands.unregister_owner(info.name)
        host.hooks.remove_owner(info.name)
        kind = "duplicate command" if isinstance(exc, DuplicateCommandError) else type(exc).__name__
        msg = f"plugin {info.name} failed ({kind}): {exc}"
        log.error("%s\n%s", msg, traceback.format_exc())
        host.log(msg)
        return None, exc


def _audit_loaded(info: PluginInfo) -> None:
    """A third-party plugin is code with machine authority: its loading is an audit record."""
    try:
        from mlaser.machine.audit import audit
        perms = info.permissions
        audit("plugin_loaded", name=info.name, target=info.target, dist=info.dist, origin=info.origin,
              manifest=info.manifest is not None, permissions=perms.to_dict() if perms else None)
    except Exception:  # noqa: BLE001 - best effort
        log.warning("plugin loaded: %s (%s)", info.name, info.target)
