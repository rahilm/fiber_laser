"""Build identity at run time: the version and the commit this copy of mlaser was built from.

In no layer (like :mod:`mlaser._atomic`), so the log setup, the diagnostic bundle, the About
dialog and the CLI all read the same thing.  Where it comes from, first hit wins:

1. a **git checkout** (an editable install, ``uv run`` in the repository): ``git describe`` of the
   checkout that holds this package, asked once per process.  Only when the folder above the
   package is itself a repository root (``<root>/.git`` exists), so a wheel installed into some
   other repository's virtual environment never reports that repository's commit;
2. ``mlaser/_build_info.py``, **generated at build time** and never committed (``.gitignore``):
   written into the wheel by the ``build_py`` hook and into the sdist by the ``sdist`` hook
   (``tools/help_build.py``), and into the source tree for the PyInstaller bundle by
   ``packaging/build.py``;
3. ``"unknown"``.

The version is the installed distribution's metadata (``importlib.metadata``), else
:data:`mlaser.__version__` (a frozen bundle may carry no metadata).

Nothing here touches the network or the machine.
"""
from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import asdict, dataclass

#: ``git describe`` arguments: the abbreviated commit only (``--exclude=*``: never a tag name) and
#: ``-dirty`` when tracked files differ from the commit.  tools/help_build.py runs the same.
GIT_DESCRIBE = ("describe", "--always", "--dirty", "--abbrev=12", "--exclude=*")
UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildIdentity:
    version: str
    commit: str = UNKNOWN      # abbreviated SHA, or "unknown"
    dirty: bool = False        # built from (or running in) a checkout with uncommitted changes
    source: str = UNKNOWN      # "git" | "build" | "unknown"

    def text(self) -> str:
        """``0.1.0rc1 (commit 1a2b3c4d5e6f, modified)``; a short machine-readable line (logs, About)."""
        if self.commit == UNKNOWN:
            return f"{self.version} (commit unknown)"
        return f"{self.version} (commit {self.commit}{', modified' if self.dirty else ''})"

    def as_dict(self) -> dict:
        return asdict(self)


def parse_describe(out: str) -> tuple[str, bool]:
    """``"1a2b3c4d5e6f-dirty"`` -> ``("1a2b3c4d5e6f", True)``."""
    s = out.strip()
    dirty = s.endswith("-dirty")
    return (s[:-len("-dirty")] if dirty else s), dirty


def git_identity(root: str, timeout: float = 3.0) -> tuple[str, bool] | None:
    """(commit, dirty) of the repository whose top level is ``root``; None when ``root`` is not
    a repository root or git is missing or fails."""
    if not os.path.exists(os.path.join(root, ".git")):   # a directory, or a file in a worktree
        return None
    try:
        r = subprocess.run(["git", "-C", root, *GIT_DESCRIBE], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return parse_describe(r.stdout)


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("mlaser")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - stdlib
        pass
    from mlaser import __version__
    return str(__version__)


@functools.lru_cache(maxsize=1)
def build_identity() -> BuildIdentity:
    """The identity of the running copy (cached for the process)."""
    ver = _version()
    pkg = os.path.dirname(os.path.abspath(__file__))
    got = git_identity(os.path.dirname(pkg))
    if got is not None:
        return BuildIdentity(ver, got[0], got[1], "git")
    try:
        from mlaser import _build_info as bi   # type: ignore[attr-defined]  # generated, never committed
        commit = str(getattr(bi, "COMMIT", "") or UNKNOWN)
        return BuildIdentity(ver, commit, bool(getattr(bi, "DIRTY", False)),
                             "build" if commit != UNKNOWN else UNKNOWN)
    except ImportError:
        return BuildIdentity(ver)


__all__ = ["BuildIdentity", "build_identity", "git_identity", "parse_describe", "GIT_DESCRIBE", "UNKNOWN"]
