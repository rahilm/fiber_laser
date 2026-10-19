"""Autosave and crash recovery on the project format.

:class:`Autosave` keeps ``autosave.mlz`` (the whole project: typed model, raster parts, nest job,
queue, technology) in the application's ``File`` folder, written atomically.  Next to it it
writes ``autosave.chf`` as the original does (FUN_004804a0 / FUN_00457de0, File\\autosave.chf
when a job is prepared), because the breakpoint / resume path (AutosaveParam*.ini, parity
docs/re/parity.md) indexes the graphs of that file; ``chf=False`` skips it.

:meth:`Autosave.recover` prefers ``autosave.mlz`` and falls back to ``autosave.chf`` when the
project is missing, older than the ``.chf`` (written by the original program or an older
rebuild) or damaged.  Qt-free; the UI calls it from ``ext_machine`` (job prepared, System
Recovery, the start-up crash prompt) and from a periodic timer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from ..model import Document
from .format import Project, ProjectError, atomic_write, dumps_project, export_chf, load_project

PROJECT_NAME = "autosave.mlz"
CHF_NAME = "autosave.chf"


@dataclass
class Recovered:
    project: Project
    path: Path
    notes: list[str] = field(default_factory=list)   # e.g. "autosave.mlz damaged: ...; used autosave.chf"

    @property
    def document(self) -> Document:
        return self.project.document


class Autosave:
    def __init__(self, directory: Union[str, Path]) -> None:
        self.dir = Path(directory)
        self.last_saved: float = 0.0
        self.last_error: Optional[str] = None

    @property
    def project_path(self) -> Path:
        return self.dir / PROJECT_NAME

    @property
    def chf_path(self) -> Path:
        return self.dir / CHF_NAME

    def save(self, project: Union[Project, Document], chf: bool = True) -> list[Path]:
        """Write ``autosave.mlz`` (and ``autosave.chf``); returns the files written.  Errors are
        raised (the caller logs them); a failed write leaves the previous files intact."""
        if isinstance(project, Document):
            project = Project(document=project)
        self.dir.mkdir(parents=True, exist_ok=True)
        out = []
        atomic_write(self.project_path, dumps_project(project, embed_fonts=False))
        out.append(self.project_path)
        if chf:
            atomic_write(self.chf_path, export_chf(project.document))
            out.append(self.chf_path)
        self.last_saved = time.time()
        return out

    def available(self) -> Optional[Path]:
        """The file :meth:`recover` would use first (None: nothing to recover)."""
        mlz, chf = self.project_path, self.chf_path
        if mlz.exists() and (not chf.exists() or mlz.stat().st_mtime >= chf.stat().st_mtime - 1.0):
            return mlz
        if chf.exists():
            return chf
        return mlz if mlz.exists() else None

    def recover(self) -> Optional[Recovered]:
        """The autosaved project, or None when there is none."""
        first = self.available()
        if first is None:
            return None
        notes: list[str] = []
        if first == self.project_path:
            try:
                return Recovered(load_project(first), first, notes)
            except ProjectError as e:
                notes.append(f"{first.name} damaged ({e}); using {self.chf_path.name}")
                if not self.chf_path.exists():
                    raise
        from ..formats.chf import load_chf
        doc = load_chf(str(self.chf_path))
        return Recovered(Project(document=doc), self.chf_path, notes)
