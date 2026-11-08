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

Writers may run in different threads (the periodic autosave serialises in a worker, the
job-start autosave runs where the job is prepared): the writes are serialised, and a state
older than the one on disk never replaces it.  A writer takes a :meth:`Autosave.reserve`
ticket when it takes its snapshot and passes it to :meth:`Autosave.save`; a save whose ticket
is older than the last one written is skipped.
"""
from __future__ import annotations

import threading
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
        self._seq_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._issued = 0                 # tickets handed out (reserve)
        self._written = 0                # the ticket of the state on disk

    def reserve(self) -> int:
        """A ticket for a snapshot taken now (see the module docstring)."""
        with self._seq_lock:
            self._issued += 1
            return self._issued

    @property
    def project_path(self) -> Path:
        return self.dir / PROJECT_NAME

    @property
    def chf_path(self) -> Path:
        return self.dir / CHF_NAME

    def save(self, project: Union[Project, Document], chf: bool = True,
             ticket: Optional[int] = None) -> list[Path]:
        """Write ``autosave.mlz`` (and ``autosave.chf``); returns the files written (none when
        ``ticket``, from :meth:`reserve`, is older than the state already written; without one
        the state is taken as of now).  Errors are raised (the caller logs them); a failed write
        leaves the previous files intact."""
        if ticket is None:
            ticket = self.reserve()
        if isinstance(project, Document):
            project = Project(document=project)
        mlz = dumps_project(project, embed_fonts=False)
        data = export_chf(project.document) if chf else None
        with self._write_lock:              # serialised outside: a waiting writer waits for the I/O only
            if ticket < self._written:
                return []
            self.dir.mkdir(parents=True, exist_ok=True)
            # the .chf first: the project of the same save is then never older than it, however
            # long the writes take (recover() takes a .chf newer than the project by more than
            # 1 s for one of another program; written second, a .chf a slow disk or a virus
            # scanner held for over a second made System Recovery drop to it and lose what only
            # the project keeps - Windows CI).  A failed .chf write still writes the project.
            chf_error: Optional[BaseException] = None
            if data is not None:
                try:
                    atomic_write(self.chf_path, data)
                except OSError as e:
                    chf_error = e
            atomic_write(self.project_path, mlz)
            if chf_error is not None:
                raise chf_error
            out = [self.project_path] + ([self.chf_path] if data is not None else [])
            self._written = ticket
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
