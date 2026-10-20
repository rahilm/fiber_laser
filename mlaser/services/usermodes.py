"""User modes: Operator, Engineer and Service (docs/review/ux.md P1-10, docs/user/shell.md).

* **Operator**: the Run and Report workspaces plus a limited Machine workspace (home, connect,
  alarms, breakpoint resume); no parameter editing; large touch targets.
* **Engineer**: everything except the service functions.
* **Service**: firmware upgrades, motion-card / hardware parameters, monitors and diagnostics.

Every command has a minimum mode: the one it declares (``CommandSpec.min_mode``, set with
``ctx.register_command(..., min_mode="service")``) or, when it declares none, the default policy
of :func:`default_min_mode` (a table of the 256 built-in commands by id and prefix).  The
command palette, the rail, the workspaces and the QActions respect it.

Switching to a *higher* mode can be protected by a PIN that the owner sets (one for Engineer,
one for Service).  PINs are stored only as salted PBKDF2 hashes in the ConfigService.  There are
no vendor passwords and no back door: a forgotten PIN is cleared by deleting the
``shell.pin.*`` entries from ``config.json``.  Switching down never asks for a PIN.

Start-up: the last used mode (``shell.user_mode``, default Engineer), but never above the first
mode a PIN protects: with an Engineer PIN the application always starts in Operator mode, with
only a Service PIN at most in Engineer mode.

Qt-free.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Callable, Optional

from .config import Setting

OPERATOR, ENGINEER, SERVICE = "operator", "engineer", "service"
MODES: tuple[str, ...] = (OPERATOR, ENGINEER, SERVICE)
_RANK = {m: i for i, m in enumerate(MODES)}
DEFAULT_MODE = ENGINEER
PBKDF2_ITERATIONS = 120_000

#: the ConfigService entries of the user-mode service
SETTINGS = [
    Setting("shell.user_mode", "str", DEFAULT_MODE, label="shell.mode.setting", section="shell", source="app"),
    Setting("shell.pin.engineer", "str", "", label="shell.pin.engineer", section="shell", source="app"),
    Setting("shell.pin.service", "str", "", label="shell.pin.service", section="shell", source="app"),
]


def rank(mode: str) -> int:
    """0 operator, 1 engineer, 2 service (an unknown mode ranks as Engineer)."""
    return _RANK.get(mode, _RANK[ENGINEER])


# --------------------------------------------------------------------------- default policy
# Commands an operator may run: running the job, homing, connecting, viewing, loading a job,
# alarms and breakpoint resume, reports, help and the shell itself.
_OPERATOR_IDS = {
    "file.open", "file.task_import", "machine.home", "machine.home_x", "machine.home_y",
    "machine.home_xyz", "machine.home_zf_then_xy", "machine.home_af", "coord.return_origin",
    "view.zoom_in", "view.zoom_out", "view.zoom_fit", "view.fit_table", "view.pan_mode", "view.select_mode",
    "card.alarms", "card.clear_alarms", "card.break_resume", "diag.log", "diag.work_report",
    "diag.work_report_all", "app.about", "app.help", "app.exit", "app.import_language",
    "hw.connect_sim", "hw.reconnect", "sim.stop", "sim.speed", "job.offline",
}
_OPERATOR_PREFIXES = ("run.", "show.", "workspace.", "shell.")
# Service functions: firmware, motion-card and hardware parameters, monitors and diagnostics.
_SERVICE_IDS = {
    "hw.upgrade", "hw.upgrade_ext", "hw.upgrade_ftc", "hw.restart", "hw.options", "hw.set_ip",
    "hw.remoter_match", "card.params", "card.stream_process", "param.backup", "param.restore",
    "param.backup_hw", "param.restore_hw", "aux.interferometer", "aux.ball_arm", "aux.burn_in",
    "aux.custom_io", "aux.custom_io_nl", "diag.device_report", "diag.measure",
    "machine.laser_type", "machine.laser_fiber", "machine.laser_co2", "shell.set_pins",
}
_SERVICE_PREFIXES = ("mon.",)


def default_min_mode(cmd_id: str) -> str:
    """The minimum mode of a command that does not declare one."""
    if cmd_id in _SERVICE_IDS or cmd_id.startswith(_SERVICE_PREFIXES):
        return SERVICE
    if cmd_id in _OPERATOR_IDS or cmd_id.startswith(_OPERATOR_PREFIXES):
        return OPERATOR
    return ENGINEER


def min_mode_of(cmd_id: str, spec: Any = None) -> str:
    declared = getattr(spec, "min_mode", "") if spec is not None else ""
    return declared or default_min_mode(cmd_id)


def allowed(cmd_id: str, mode: str, spec: Any = None) -> bool:
    """May ``mode`` run ``cmd_id``?"""
    return rank(mode) >= rank(min_mode_of(cmd_id, spec))


# --------------------------------------------------------------------------- PINs
def hash_pin(pin: str, salt: Optional[bytes] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """``pbkdf2-sha256$<iterations>$<salt hex>$<hash hex>``."""
    salt = os.urandom(16) if salt is None else salt
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return f"pbkdf2-sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algo, it, salt, digest = stored.split("$")
        if algo != "pbkdf2-sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt), int(it))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), digest)


def valid_pin(pin: str) -> bool:
    """A PIN is 4 to 12 digits."""
    return pin.isdigit() and 4 <= len(pin) <= 12


class ModeRefused(PermissionError):
    pass


class UserModeService:
    """The current user mode, PIN checks and change listeners (persisted in ``config``)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        try:
            config.register(SETTINGS)
        except KeyError:
            pass  # a second service on the same config (tests): the schema is already there
        self._listeners: list[Callable[[str], None]] = []
        self.mode = self.startup_mode()

    # ------------------------------------------------------------------ PINs
    def _pin_key(self, mode: str) -> str:
        return f"shell.pin.{mode}"

    def has_pin(self, mode: str) -> bool:
        if mode == OPERATOR:
            return False
        return bool(self.config.get(self._pin_key(mode), ""))

    def needs_pin(self, target: str) -> bool:
        """Does switching from the current mode to ``target`` ask for a PIN?  Only going up, and
        only when a PIN protects ``target`` or a mode between the current one and it."""
        if rank(target) <= rank(self.mode):
            return False
        return any(self.has_pin(m) for m in MODES[rank(self.mode) + 1 : rank(target) + 1])

    def check_pin(self, target: str, pin: str) -> bool:
        """The PIN of ``target`` (or, when ``target`` has none, of the protected mode below it)."""
        for m in reversed(MODES[rank(self.mode) + 1 : rank(target) + 1]):
            stored = self.config.get(self._pin_key(m), "")
            if stored:
                return verify_pin(pin, stored)
        return True

    def set_pin(self, mode: str, pin: str) -> None:
        """Set (or with ``pin == ""`` clear) the PIN of ``mode``.  Only in Service mode (the
        owner); a PIN is 4-12 digits."""
        if self.mode != SERVICE:
            raise ModeRefused("PINs are set in Service mode")
        if mode not in (ENGINEER, SERVICE):
            raise ValueError(f"no PIN for {mode!r}")
        if pin and not valid_pin(pin):
            raise ValueError("a PIN is 4 to 12 digits")
        self.config.set(self._pin_key(mode), hash_pin(pin) if pin else "")

    # ------------------------------------------------------------------ mode
    def startup_mode(self) -> str:
        saved = self.config.get("shell.user_mode", DEFAULT_MODE)
        if saved not in MODES:
            saved = DEFAULT_MODE
        mode = OPERATOR       # walk up while no PIN protects the next mode
        for m in MODES[1 : rank(saved) + 1]:
            if self.has_pin(m):
                break
            mode = m
        return mode

    def set_mode(self, target: str, pin: Optional[str] = None) -> bool:
        """Switch to ``target``; False (no change) when a PIN is needed and ``pin`` is wrong."""
        if target not in MODES:
            raise ValueError(f"unknown user mode {target!r}")
        if target == self.mode:
            return True
        if self.needs_pin(target) and not (pin is not None and self.check_pin(target, pin)):
            return False
        self.mode = target
        self.config.set("shell.user_mode", target)
        for fn in list(self._listeners):
            fn(target)
        return True

    def subscribe(self, fn: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def allows(self, cmd_id: str, spec: Any = None) -> bool:
        return allowed(cmd_id, self.mode, spec)

    def at_least(self, mode: str) -> bool:
        return rank(self.mode) >= rank(mode)


__all__ = [
    "MODES", "OPERATOR", "ENGINEER", "SERVICE", "DEFAULT_MODE", "SETTINGS", "rank",
    "default_min_mode", "min_mode_of", "allowed", "hash_pin", "verify_pin", "valid_pin",
    "ModeRefused", "UserModeService",
]
