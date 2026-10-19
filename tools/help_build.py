"""setuptools hook: copy the help documents into the wheel as ``mlaser/help/docs``.

pyproject.toml names :class:`BuildPy` in ``[tool.setuptools.cmdclass]``.  An editable install
skips the copy; :func:`mlaser.diag.helpdocs.docs_root` then reads the checkout's ``docs/``.
The PyInstaller bundle copies the same files (``packaging/bundle_config.py`` ``help_datas``).

This file runs inside the isolated build environment, where mlaser cannot be imported, so the
selection is repeated here; ``tests/test_docs_links.py`` checks it equals
``mlaser.diag.helpdocs.SHIPPED`` / ``EXCLUDED``.
"""
from __future__ import annotations

import os
import shutil

try:
    from setuptools.command.build_py import build_py as _build_py
except ImportError:             # imported by the tests without setuptools: only the selection
    _build_py = object  # type: ignore[assignment,misc]

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


class BuildPy(_build_py):
    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return
        docs = os.path.join(ROOT, "docs")
        if os.path.isdir(docs):
            copy_docs(os.path.join(self.build_lib, "mlaser", "help", "docs"), docs)
