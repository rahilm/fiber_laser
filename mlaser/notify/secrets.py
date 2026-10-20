"""Secret values of the notification channels (SMTP password, ntfy token, webhook secret).

The configuration (``config.json``) never holds a secret: it stores the channel settings only.
Secrets go to the operating system's keyring when the optional ``keyring`` package is installed
and has a working back end (Windows Credential Manager, macOS Keychain, Secret Service); else to
a JSON file the caller names (the application: ``secrets.json`` in the user config directory),
created with permissions 0600 (owner read/write only; best effort on Windows, where the file
lives in the user's own profile).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("mlaser.notify")

SERVICE = "mlaser"
FILE_MODE = 0o600


def _load_keyring() -> Any:
    try:
        return importlib.import_module("keyring")
    except Exception:  # noqa: BLE001 - not installed, or broken: use the file
        return None


class SecretStore:
    """``get(name)`` / ``set(name, value)`` (an empty value deletes) / ``has(name)``.

    ``keyring_module``: a keyring-compatible module (tests); None imports ``keyring`` when
    ``use_keyring``.  A keyring that fails (no back end, locked) falls back to the file."""

    def __init__(self, path: os.PathLike | str, use_keyring: bool = True, keyring_module: Any = None) -> None:
        self.path = Path(path)
        self._kr = keyring_module if keyring_module is not None else (_load_keyring() if use_keyring else None)

    # ------------------------------------------------------------------ back end
    @property
    def backend(self) -> str:
        return "keyring" if self._kr is not None else "file"

    def _kr_failed(self, exc: BaseException) -> None:
        log.warning("notifications: keyring unavailable (%s: %s); secrets go to %s",
                    type(exc).__name__, exc, self.path)
        self._kr = None

    # ------------------------------------------------------------------ file
    def _read_file(self) -> dict:
        try:
            st = self.path.stat()
        except OSError:
            return {}
        if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("notifications: %s was readable by others; permissions set to 0600", self.path)
            try:
                os.chmod(self.path, FILE_MODE)
            except OSError:
                pass
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _write_file(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), FILE_MODE)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, FILE_MODE)
            os.write(fd, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, FILE_MODE)
        except OSError:
            pass

    # ------------------------------------------------------------------ API
    def get(self, name: str) -> str:
        if self._kr is not None:
            try:
                v = self._kr.get_password(SERVICE, name)
                if v:
                    return str(v)
            except Exception as exc:  # noqa: BLE001
                self._kr_failed(exc)
        return self._read_file().get(name, "")

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def set(self, name: str, value: Optional[str]) -> None:
        value = value or ""
        if self._kr is not None:
            try:
                if value:
                    self._kr.set_password(SERVICE, name, value)
                else:
                    try:
                        self._kr.delete_password(SERVICE, name)
                    except Exception:  # noqa: BLE001 - nothing stored
                        pass
                data = self._read_file()            # never keep a stale copy in the file
                if name in data:
                    del data[name]
                    self._write_file(data)
                return
            except Exception as exc:  # noqa: BLE001
                self._kr_failed(exc)
        data = self._read_file()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        self._write_file(data)


__all__ = ["SecretStore", "SERVICE", "FILE_MODE"]
