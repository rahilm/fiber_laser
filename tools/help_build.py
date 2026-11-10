"""setuptools hooks: the help documents and the build identity of the wheel and the sdist.

pyproject.toml names :class:`BuildPy` (``build_py``) and :class:`Sdist` (``sdist``) in
``[tool.setuptools.cmdclass]``.

* **Help documents**: copied into the wheel as ``mlaser/help/docs``.  An editable install skips
  the copy; :func:`mlaser.diag.helpdocs.docs_root` then reads the checkout's ``docs/``.  The
  PyInstaller bundle copies the same files (``packaging/bundle_config.py`` ``help_datas``).
* **Build identity**: ``mlaser/_build_info.py`` (``COMMIT``, ``DIRTY``; read by
  :func:`mlaser._buildid.build_identity`) is generated, never committed (``.gitignore``).
  ``build_py`` writes it into the wheel from ``git describe`` of the checkout; built from an
  sdist (no ``.git``) the sdist's own copy, written by ``sdist`` into the release tree, is kept;
  with neither the wheel says ``unknown``.  An editable install writes nothing (the running
  checkout asks git itself).  ``packaging/build.py`` and CI call :func:`write_build_info` too
  (``python tools/help_build.py --build-info PATH``).

This file runs inside the isolated build environment, where mlaser cannot be imported, so the
selection is repeated here; ``tests/test_docs_links.py`` checks it equals
``mlaser.diag.helpdocs.SHIPPED`` / ``EXCLUDED``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

try:
    from setuptools.command.build_py import build_py as _build_py
    from setuptools.command.sdist import sdist as _sdist
except ImportError:             # imported by the tests without setuptools: only the selection
    _build_py = object  # type: ignore[assignment,misc]
    _sdist = object  # type: ignore[assignment,misc]

SHIPPED = ("README.md", "SAFETY.md", "ASSETS.md", "ROADMAP.md", "user", "dev", "re")
EXCLUDED = ("re/history",)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def shipped_files(docs_dir: str) -> list[str]:
    out = []
    for item in SHIPPED:
        p = os.path.join(docs_dir, item)
        if os.path.isfile(p):
            out.append(item)
        elif os.path.isdir(p):
            for base, dirs, files in os.walk(p):
                dirs.sort()
                for n in sorted(files):
                    rel = os.path.relpath(os.path.join(base, n), docs_dir).replace(os.sep, "/")
                    if not any(rel == e or rel.startswith(e + "/") for e in EXCLUDED) and not n.startswith("."):
                        out.append(rel)
    return out


def copy_docs(dst: str, docs_dir: str = os.path.join(ROOT, "docs")) -> int:
    """Copy the shipped documents to ``dst``; returns the number of files."""
    files = shipped_files(docs_dir)
    for rel in files:
        target = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(docs_dir, *rel.split("/")), target)
    return len(files)


# ------------------------------------------------------------------ build identity
#: the same command as mlaser._buildid.GIT_DESCRIBE (this file cannot import mlaser;
#: tests/test_build_identity.py checks they are equal)
GIT_DESCRIBE = ("describe", "--always", "--dirty", "--abbrev=12", "--exclude=*")
BUILD_INFO = os.path.join("mlaser", "_build_info.py")


def git_identity(root: str = ROOT) -> "tuple[str, bool] | None":
    """(abbreviated commit, dirty) of the checkout at ``root``; None when ``root`` is not a
    repository root (an unpacked sdist, a source archive) or git is missing or fails.  The
    ``.git`` test keeps an sdist unpacked inside some other repository from taking its commit."""
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", root, *GIT_DESCRIBE], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    dirty = out.endswith("-dirty")
    return (out[:-len("-dirty")] if dirty else out), dirty


def build_info_text(commit: str, dirty: bool) -> str:
    return ('"""Generated at build time by tools/help_build.py; not in git (mlaser._buildid)."""\n'
            f"COMMIT = {commit!r}\nDIRTY = {bool(dirty)!r}\n")


def write_build_info(path: str, root: str = ROOT, keep_existing: bool = False) -> "str | None":
    """Write the build identity of ``root`` to ``path``.  Without git: keep an existing file when
    ``keep_existing`` (an sdist's copy), else write ``unknown``.  Returns the commit written,
    None when an existing file was kept.  A hard link (an sdist release tree links the source
    files) is replaced, never written through."""
    got = git_identity(root)
    if got is None and keep_existing and os.path.isfile(path):
        return None
    commit, dirty = got if got is not None else ("unknown", False)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.lexists(path):
        os.remove(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(build_info_text(commit, dirty))
    return commit


class BuildPy(_build_py):
    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return
        docs = os.path.join(ROOT, "docs")
        if os.path.isdir(docs):
            copy_docs(os.path.join(self.build_lib, "mlaser", "help", "docs"), docs)
        # an sdist's mlaser/_build_info.py was copied by build_py above: kept when git is absent
        write_build_info(os.path.join(self.build_lib, BUILD_INFO), ROOT, keep_existing=True)


class Sdist(_sdist):
    def make_release_tree(self, base_dir, files) -> None:
        super().make_release_tree(base_dir, files)
        write_build_info(os.path.join(base_dir, BUILD_INFO), ROOT, keep_existing=True)


if __name__ == "__main__":
    # python tools/help_build.py --build-info PATH   (packaging/build.py, CI before cibuildwheel)
    if len(sys.argv) == 3 and sys.argv[1] == "--build-info":
        print("build identity:", write_build_info(sys.argv[2]))
    else:
        sys.exit("usage: python tools/help_build.py --build-info PATH")
