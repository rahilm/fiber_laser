"""Crash-safe file writes for every file the user keeps (drawings, projects, exports, reports,
settings); in no layer, so any package may import it (``parity_ports`` included: only the write
mechanics live here, the bytes are the caller's).

:func:`atomic_write` and :func:`atomic_open` write a temporary file in the target's folder,
flush and ``fsync`` it, ``os.replace`` it over the target and ``fsync`` the folder: a crash, a
power cut or a full disk leaves either the old file or the new one, never a truncated mix
(writing in place truncates the old file first).

The new file keeps the old one's permission bits (a new file gets the usual ``0666 & ~umask``,
not ``mkstemp``'s private ``0600``), and a symbolic link is followed (its target is replaced,
the link stays).  On Windows a replace that an antivirus or indexer briefly blocks is retried.
"""
from __future__ import annotations

import contextlib
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, Any, Iterator, Union

PathLike = Union[str, "os.PathLike[str]"]

# the process umask, read once at import (reading it means setting it: not from a worker thread)
_UMASK = os.umask(0)
os.umask(_UMASK)
_WIN = sys.platform == "win32"
_REPLACE_TRIES = 10 if _WIN else 1


def _target(path: PathLike) -> Path:
    p = Path(os.fspath(path))
    try:
        if p.is_symlink():
            p = Path(os.path.realpath(p))
    except OSError:
        pass
    return p


def _mode_for(p: Path) -> int:
    try:
        return stat.S_IMODE(p.stat().st_mode)
    except OSError:
        return 0o666 & ~_UMASK


def _replace(tmp: str, p: Path) -> None:
    for i in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if i == _REPLACE_TRIES - 1:
                raise
            time.sleep(0.05)       # Windows: the target is open in a scanner / indexer for a moment


def _fsync_dir(d: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):      # Windows: no directory handles to sync
        return
    try:
        dfd = os.open(str(d), os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


@contextlib.contextmanager
def atomic_open(path: PathLike, mode: str = "wb", **kw: Any) -> Iterator[IO[Any]]:
    """``open(path, mode, **kw)`` for writing, made atomic: the data goes to a temporary file
    that replaces ``path`` when the ``with`` block ends without an exception (with one, the old
    file stays and the temporary file is removed).  ``mode`` is ``"w"`` or ``"wb"`` (``"x"`` and
    appending make no sense here)."""
    if mode not in ("w", "wb", "wt"):
        raise ValueError(f"atomic_open: mode {mode!r} (only 'w', 'wt' or 'wb')")
    p = _target(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, mode, **kw) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, _mode_for(p))
        except OSError:
            pass
        _replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(p.parent)


def atomic_write(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (see the module docstring)."""
    with atomic_open(path, "wb") as f:
        f.write(data)


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8", newline: Union[str, None] = None) -> None:
    """Write ``text`` to ``path`` atomically (``newline`` as for :func:`open`)."""
    with atomic_open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)


__all__ = ["atomic_open", "atomic_write", "atomic_write_text"]
