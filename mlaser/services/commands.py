"""CommandRegistry: one handler per command id, registered once.

Before Wave 2 each extension disconnected ``QAction.triggered`` and connected its own handler, so
170 of 246 ribbon commands were rebound and 10 core handlers were silently replaced, depending on
load order (architecture.md §1.3).  Now every command (core and plugin) is registered here; a
second registration of the same id raises :class:`DuplicateCommandError` and is recorded in
:attr:`CommandRegistry.duplicates` (``tools/command_coverage.py`` reports them; 0 are allowed).
The Qt side connects each ``QAction`` once to :meth:`CommandRegistry.run`.  Qt-free.
"""

from __future__ import annotations

import inspect
import logging
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("mlaser.commands")


class DuplicateCommandError(KeyError):
    """A command id was registered twice."""


class UnknownCommandError(KeyError):
    """No handler is registered for the command id."""


@dataclass
class CommandSpec:
    id: str
    handler: Callable[..., Any]
    owner: str = "core"
    label: str = ""  # lang key or text for a new ribbon action
    checkable: bool = False  # the handler receives the checked state
    takes_arg: bool = False
    # Wave 3 shell (mlaser.services.usermodes): the lowest user mode that may run the command
    # ("operator" | "engineer" | "service"); "" = the default policy of usermodes.default_min_mode
    min_mode: str = ""
    # the task workspace the command belongs to ("design", "process", ...); "" = derived from
    # the id (mlaser.services.workspaces.command_workspace)
    workspace: str = ""

    def call(self, *args) -> Any:
        if self.takes_arg or self.checkable:
            return self.handler(*args) if args else self.handler()
        return self.handler()


def _takes_arg(fn: Callable) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL,):
            return True
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty:
            return True
    return False


@dataclass
class CommandRegistry:
    commands: dict[str, CommandSpec] = field(default_factory=dict)
    duplicates: list[tuple[str, str, str]] = field(default_factory=list)  # (id, first owner, second)
    failures: dict[str, str] = field(default_factory=dict)  # id -> last exception text
    on_register: list[Callable[[CommandSpec], None]] = field(default_factory=list)
    on_error: list[Callable[[str, BaseException], None]] = field(default_factory=list)

    def register(
        self,
        cmd_id: str,
        handler: Callable[..., Any],
        owner: str = "core",
        label: str = "",
        checkable: bool = False,
        min_mode: str = "",
        workspace: str = "",
    ) -> CommandSpec:
        if not cmd_id or not callable(handler):
            raise ValueError(f"bad command registration {cmd_id!r}")
        old = self.commands.get(cmd_id)
        if old is not None:
            self.duplicates.append((cmd_id, old.owner, owner))
            raise DuplicateCommandError(
                f"command {cmd_id!r} is already registered by {old.owner!r} "
                f"({owner!r} tried to register it again)"
            )
        if min_mode and min_mode not in ("operator", "engineer", "service"):
            raise ValueError(f"command {cmd_id!r}: unknown min_mode {min_mode!r}")
        spec = CommandSpec(cmd_id, handler, owner, label, checkable, _takes_arg(handler), min_mode, workspace)
        self.commands[cmd_id] = spec
        for fn in list(self.on_register):
            fn(spec)
        return spec

    def unregister_owner(self, owner: str) -> list[str]:
        gone = [k for k, s in self.commands.items() if s.owner == owner]
        for k in gone:
            del self.commands[k]
        return gone

    def __contains__(self, cmd_id: object) -> bool:
        return cmd_id in self.commands

    def get(self, cmd_id: str) -> Optional[CommandSpec]:
        return self.commands.get(cmd_id)

    def owner(self, cmd_id: str) -> Optional[str]:
        s = self.commands.get(cmd_id)
        return s.owner if s else None

    def run(self, cmd_id: str, *args) -> Any:
        """Run the handler.  An exception is logged, recorded in :attr:`failures`, passed to the
        ``on_error`` listeners (the log panel) and swallowed: a failing command must not take
        the application down.  Raises :class:`UnknownCommandError` for an unbound id."""
        spec = self.commands.get(cmd_id)
        if spec is None:
            raise UnknownCommandError(cmd_id)
        try:
            return spec.call(*args)
        except Exception as exc:  # noqa: BLE001 - reported, see docstring
            self.failures[cmd_id] = f"{type(exc).__name__}: {exc}"
            log.error("command %s failed:\n%s", cmd_id, traceback.format_exc())
            for fn in list(self.on_error):
                fn(cmd_id, exc)
            return None

    def handlers(self) -> "HandlerView":
        return HandlerView(self)


class HandlerView(Mapping):
    """``window.handlers``: read-only mapping command id -> callable running it."""

    def __init__(self, reg: CommandRegistry) -> None:
        self._reg = reg

    def __getitem__(self, cmd_id: str) -> Callable[..., Any]:
        if cmd_id not in self._reg.commands:
            raise KeyError(cmd_id)
        return lambda *a: self._reg.run(cmd_id, *a)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._reg.commands))

    def __len__(self) -> int:
        return len(self._reg.commands)
