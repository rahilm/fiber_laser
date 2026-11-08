"""Exclusive OS file locks that die with the process (no Qt, no mlaser imports).

Used for the single application instance (:mod:`mlaser.ui.single_instance`) and the per-card
connection lock (:mod:`mlaser.controller.cardlock`).  A lock *file* that merely exists is not a
lock: a crash leaves it behind, and its PID may be reused.  The lock here is the operating
system's, held on an open file descriptor: ``fcntl.flock`` (macOS, Linux, BSD) or
``msvcrt.locking`` on one byte past the note (Windows, a ``LockFileEx`` byte-range lock).  The
OS releases it when the process exits for any reason (crash, ``kill -9``, power loss of a
container), so there is no stale-lock state to detect.  The file's content (who holds it:
PID, host name, what for) is information for the operator's message only, never the lock.

Both locks are per open file description: a second :class:`FileLock` on the same path fails
also inside the same process (two controllers of one process cannot drive one card either).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

if sys.platform.startswith("win"):
    import msvcrt
else:
    import fcntl


def runtime_dir() -> Path:
    """Per-user directory for the lock files: ``$MLASER_RUNTIME_DIR``, else
    ``$XDG_RUNTIME_DIR/mlaser`` (Linux: per user session, tmpfs, cleaned at logout), else
    ``%LOCALAPPDATA%\\mlaser\\run`` (Windows), ``~/Library/Caches/mlaser/run`` (macOS) or
    ``<tmp>/mlaser-<uid>`` (anything else)."""
    env = os.environ.get("MLASER_RUNTIME_DIR")
    if env:
        return Path(env)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # under pytest a directory per process, like the config directory (services/config.py):
        # parallel workers must not see each other's instances and cards
        return Path(tempfile.gettempdir()) / f"mlaser-test-{os.getpid()}" / "run"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "mlaser" / "run"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "mlaser" / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return Path(xdg) / "mlaser"
    return Path(tempfile.gettempdir()) / f"mlaser-{os.getuid()}"


class LockHeld(OSError):
    """The lock belongs to another holder; ``owner`` is what it wrote (may be empty)."""

    def __init__(self, path: Path, owner: dict):
        self.path = path
        self.owner = owner
        pid = owner.get("pid")
        super().__init__(f"{path} is locked" + (f" by process {pid}" if pid else ""))


class FileLock:
    """An exclusive, non-blocking OS lock on ``path`` (see the module docstring).

    ``acquire(info)`` raises :class:`LockHeld` when another holder has it and writes ``info``
    (plus ``pid``, ``host``, ``time``) into the file for that holder's message; ``release()``
    unlocks and closes (the file stays: removing it would race a new holder)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, info: Optional[dict] = None) -> "FileLock":
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise LockHeld(self.path, read_owner(self.path)) from None
        self._fd = fd
        data = {"pid": os.getpid(), "host": socket.gethostname(), "time": time.time()}
        data.update(info or {})
        try:
            raw = (json.dumps(data) + "\n").encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, raw)
        except OSError:
            pass
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self) -> None:  # a forgotten lock is released with the object, as by the OS
        try:
            self.release()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass


def read_owner(path: Path | str) -> dict:
    """What the holder wrote into the lock file ({} when unreadable)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
        v = json.loads(raw.decode("utf-8", "replace").strip() or "{}")
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


#: Windows: the locked byte lies past the holder's note, so another process can still read it
_WIN_LOCK_OFFSET = 1 << 20

if sys.platform.startswith("win"):
    def _lock(fd: int) -> None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = ["FileLock", "LockHeld", "read_owner", "runtime_dir"]
