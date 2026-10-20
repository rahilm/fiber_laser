"""The controller as a plugin sees it (docs/SAFETY.md §3a, docs/dev/PLUGINS.md).

``PluginContext.controller`` returns :func:`plugin_view` of the real controller: the
:class:`~mlaser.machine.api.MachineAPI` members and nothing else.

* no route to the transport or the guard: no ``sup``/``_sup``, no ``fsm``, no ``mc``; the card
  handle (``card()``) forwards plain functions only (no bound methods);
* read-only: every attribute assignment is refused (``laser_enabled``, ``require_homed`` ...);
* privileged actions carry the plugin's name: ``arm()`` goes to the controller as
  ``arm(reason, source="plugin:<name>")``; ``arm``, ``reset_fault``, ``operator_action`` (it
  clears the halt latch) and ``configure_safety`` need the controller's plugin policy
  (``set_plugin_policy``, installed by the plugin allowlist UI) - without one they are refused
  and audited.  Everything that moves still passes the start gate, the pre-flight and the
  supervisor's guard.

Python cannot sandbox a plugin: private attributes, ``gc`` or ``ctypes`` reach anything, and a
plugin can open its own socket to the card.  The view keeps well-meaning plugins on the
supported, audited path; plugins remain trusted code (PLUGINS.md "Trust").
"""
from __future__ import annotations

from typing import Any, Optional

from ..machine.api import api_members

# members of a controller a plugin may use besides the MachineAPI ones
EXTRA_MEMBERS = frozenset({"safety_config", "configure_safety", "layer_table", "travel_limits",
                           "homed_xy"})


class PluginControllerView:
    """A read-only, audited view of a controller for one plugin (see the module doc)."""

    __slots__ = ("_ctl", "_source")

    def __init__(self, ctl: Any, plugin: str):
        object.__setattr__(self, "_ctl", ctl)
        object.__setattr__(self, "_source", f"plugin:{plugin}")

    # ------------------------------------------------------------------ privileged
    def arm(self, reason: str = "", source: Optional[str] = None) -> bool:
        return bool(self._ctl.arm(reason=reason, source=self._source))

    def reset_fault(self) -> bool:
        if self._ctl.plugin_policy_check(self._source, "reset_fault"):
            return False
        return bool(self._ctl.reset_fault())

    def operator_action(self, what: str = "") -> None:
        if self._ctl.plugin_policy_check(self._source, "operator_action", what):
            return
        self._ctl.operator_action(what or self._source)

    def configure_safety(self, **kw: Any) -> bool:
        kw["source"] = self._source
        fn = getattr(self._ctl, "configure_safety", None)
        return bool(fn(**kw)) if callable(fn) else False

    def set_plugin_policy(self, fn: Any) -> None:
        raise PermissionError("a plugin cannot change the plugin policy")

    # ------------------------------------------------------------------ the rest
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or (name not in _allowed() and name not in EXTRA_MEMBERS):
            raise AttributeError(f"controller: {name!r} is not available to plugins")
        v = getattr(object.__getattribute__(self, "_ctl"), name)
        if isinstance(v, list):
            return list(v)                       # a copy: the controller's lists stay its own
        return v

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the controller is read-only for plugins")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the controller is read-only for plugins")

    def __dir__(self):
        return sorted(_allowed() | EXTRA_MEMBERS)

    def __repr__(self) -> str:
        return f"<controller view for {self._source}>"


_ALLOWED: Optional[frozenset] = None


def _allowed() -> frozenset:
    global _ALLOWED
    if _ALLOWED is None:
        _ALLOWED = frozenset(api_members())
    return _ALLOWED


def plugin_view(ctl: Any, plugin: str) -> Any:
    """The view of ``ctl`` for ``plugin`` (None stays None)."""
    if ctl is None:
        return None
    if isinstance(ctl, PluginControllerView):
        ctl = object.__getattribute__(ctl, "_ctl")
    return PluginControllerView(ctl, plugin)


__all__ = ["PluginControllerView", "plugin_view", "EXTRA_MEMBERS"]
