"""Automation API: an opt-in local REST + WebSocket server for the shop's own tools.

Read-only status and events, plus queue intake whose jobs wait for the operator's approval at
the machine.  No endpoint can start, stop, hold, arm, move or approve anything: see
:mod:`.server` and docs/dev/API.md.  Standard library only.
"""
from .openapi import openapi  # noqa: F401
from .server import DEFAULT_PORT, PREFIX, ApiError, AutomationServer  # noqa: F401
