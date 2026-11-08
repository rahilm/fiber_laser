"""Native project files and autosave (plugin ``ext_project``, docs/dev/project-format.md).

Commands (Ctrl+K palette; the Shell routes File > Open / Save through the providers below):

* ``file.open_project``: open a ``.mlz`` project (or a ``.chf``);
* ``file.save_project``: save the document and the project sections as ``.mlz``;
* ``file.export_chf``: export the interchange ``.chf`` (raster parts as burn segments).

Providers (``window.hooks.provider(name)``): ``project.open(path) -> bool``,
``project.save(path) -> bool`` and ``project.current() -> Project`` (the document plus every
registered section, what autosave writes).

Sections: other plugins keep their data in the project with
``window.extension("ext_project").register_section(name, get, set)``: ``get()`` returns
JSON-able data (or model / nest objects) to save, ``set(value)`` receives it on open.
``technology`` and ``queue`` are the well-known names (the technology database and the job queue).

Autosave: every ``autosave_interval_s`` seconds (setting, 0 = off) when the document changed, the
project goes to ``autosave.mlz`` (+ ``autosave.chf``) in the machine's File folder
(:class:`mlaser.project.Autosave`, the store ext_machine uses for System Recovery and the
crash prompt).  The periodic autosave takes a cheap snapshot on the GUI thread and serialises
and writes it in a worker (:mod:`mlaser.ui.save_worker`), as Save does: a large drawing no
longer freezes the window for a second every two minutes (UI evaluation F3).  A state older
than the one on disk never replaces it (:meth:`Autosave.reserve`); closing the window waits
for a write in progress.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from mlaser.project import Autosave, Project, ProjectError, export_chf, load_project, save_project
from mlaser.services.config import Setting

log = logging.getLogger("mlaser.ui.project")


class ProjectExtension:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.w = ctx.window
        self.sections: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}
        self.extra_meta: dict = {}
        self._dirty = False
        self._autosave_busy = False
        self._autosave_idle = threading.Event()
        self._autosave_idle.set()
        self.settings = ctx.settings_schema([
            Setting("autosave_interval_s", "int", 120, unit="s", min=0, max=3600,
                    label="project.autosave_interval"),
        ])
        self.timer = QTimer(self.w)
        self.timer.timeout.connect(self.autosave_async)
        self._arm()
        ctx.on_document_changed(self._changed)
        # a change of the interval (Settings > General) re-arms the timer at once
        ctx.config.subscribe(lambda key: self._arm() if key in ("*", "plugin.ext_project.autosave_interval_s") else None)

    # ------------------------------------------------------------------ helpers
    def tr(self, key: str, **kw) -> str:
        t = self.ctx.tr
        return t.format(key, **kw) if kw else t.tr(key)

    def _arm(self) -> None:
        try:
            s = int(self.settings["autosave_interval_s"] or 0)
        except Exception:  # noqa: BLE001
            s = 120
        if s > 0:
            self.timer.start(s * 1000)
        else:
            self.timer.stop()

    def _changed(self, kind: str) -> None:
        self._dirty = True

    def register_section(self, name: str, get: Callable[[], Any], set: Callable[[Any], None]) -> None:
        """Keep a plugin's data in the project (see the module docstring)."""
        self.sections[name] = (get, set)

    # ------------------------------------------------------------------ project
    def current(self, document=None, copy_sections: bool = False) -> Project:
        """The project: ``document`` (default: the live one) and every registered section
        (``copy_sections``: private copies, for a worker thread to serialise)."""
        pr = Project(document=self.ctx.document if document is None else document)
        docs = getattr(self.ctx, "docs", None)
        for name, (get, _set) in self.sections.items():
            try:
                v = get()
            except Exception:  # noqa: BLE001 - one broken section must not stop a save
                log.exception("project section %s", name)
                continue
            if v is None:
                continue
            if copy_sections and docs is not None:
                try:
                    v = docs.lean_copy(v)
                except Exception:  # noqa: BLE001 - not copyable: serialised as it is
                    log.warning("project section %s not copied for the save worker", name)
            if name == "technology":
                pr.technology = v
            elif name == "queue":
                pr.queue = v
            elif name == "nest":
                pr.nest = v
            else:
                pr.sections[name] = v
        return pr

    def apply(self, pr: Project) -> None:
        values = dict(pr.sections, technology=pr.technology, queue=pr.queue, nest=pr.nest)
        for name, (_get, set_) in self.sections.items():
            if name in values and values[name] not in (None, {}, []):
                try:
                    set_(values[name])
                except Exception:  # noqa: BLE001
                    log.exception("project section %s", name)

    def _job_running(self) -> bool:
        if getattr(self.w, "safety_ui", None) is None:
            return False
        from . import safety_ui
        return safety_ui.job_running(self.w)

    def open(self, path: str) -> bool:
        from mlaser.io.limits import ImportCancelled
        from .import_worker import ImportBusy, run_import, show_error
        try:
            # off the GUI thread with progress and Cancel; .mlz limits in mlaser.project.format
            pr = run_import(self.w, lambda: load_project(path), self.tr("import.progress.opening_file",
                                                                         name=os.path.basename(path)))
        except ProjectError as e:
            self.ctx.log(self.tr("project.error", error=str(e)))
            show_error(self.w, self.tr("project.not_opened"), str(e))
            return False
        except ImportCancelled:
            self.ctx.log(self.tr("project.log.cancelled", name=os.path.basename(path)))
            return False
        except ImportBusy as e:
            self.ctx.log(self.tr("project.log.busy", name=os.path.basename(path), error=e))
            return False
        if self._job_running():                          # a job started while the file was read
            self.ctx.log(self.tr("project.log.job_running", name=os.path.basename(path)))
            return False
        self.ctx.replace_document(pr.document, "open project", path=path)
        self.apply(pr)
        for n in pr.notes:
            self.ctx.log(n)
        self.ctx.log(self.tr("project.opened", path=path))
        return True

    def save(self, path: str) -> bool:
        """Save as ``path`` (``.mlz``): a snapshot here, serialised and written atomically in a
        worker while the window stays live (:mod:`mlaser.ui.save_worker`); True when written."""
        if not path.lower().endswith(".mlz"):
            path += ".mlz"
        from .save_worker import run_in_worker, snapshot_now
        try:
            pr = self.current(snapshot_now(self.ctx.docs, self.w), copy_sections=True)
            run_in_worker(self.w, lambda: save_project(path, pr),
                          self.tr("project.saving", name=os.path.basename(path)), self.tr("project.save"))
        except Exception as e:  # noqa: BLE001 - shown to the user; the document stays modified
            self.ctx.log(self.tr("project.save_error", error=f"{type(e).__name__}: {e}"))
            from .import_worker import show_error
            show_error(self.w, self.tr("app.save_failed"), f"{os.path.basename(path)}\n\n{e}")
            return False
        if pr.notes:
            self.ctx.log(self.tr("project.notes", notes="; ".join(pr.notes)))
        self.ctx.docs.path = path
        shell = getattr(self.w, "shell", None)
        if shell is not None:
            shell.mark_saved()
        from .save_worker import seal_undo
        seal_undo(self.ctx.docs)
        self.ctx.log(self.tr("project.saved", path=path))
        return True

    # ------------------------------------------------------------------ commands
    def open_dialog(self) -> None:
        confirm = getattr(self.w, "confirm_unsaved", None)
        if confirm is not None and not confirm():      # Save / Don't Save / Cancel first
            return
        path, _ = QFileDialog.getOpenFileName(self.w, self.tr("project.open"), "", self.tr("project.filter"))
        if path:
            if path.lower().endswith(".mlz"):
                self.open(path)
            elif hasattr(self.w, "load_path"):
                self.w.load_path(path, confirm=False)

    def save_dialog(self) -> None:
        cur = self.ctx.docs.path or ""
        start = os.path.splitext(cur)[0] + ".mlz" if cur else ""
        path, _ = QFileDialog.getSaveFileName(self.w, self.tr("project.save"), start, self.tr("project.filter"))
        if path:
            self.save(path)

    def export_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.w, self.tr("project.export_chf"), "",
                                              self.tr("project.chf_filter"))
        if not path:
            return
        if not path.lower().endswith(".chf"):
            path += ".chf"
        try:
            from mlaser.project import atomic_write
            atomic_write(path, export_chf(self.ctx.document))
        except Exception as e:  # noqa: BLE001
            self.ctx.log(self.tr("project.save_error", error=f"{type(e).__name__}: {e}"))
            return
        self.ctx.log(self.tr("project.saved", path=path))

    # ------------------------------------------------------------------ autosave
    def autosave_store(self) -> Optional[Autosave]:
        prov = self.w.hooks.provider("project.autosave")
        if prov is not None:
            return prov()
        return None

    def autosave_now(self, force: bool = False) -> Optional[list]:
        """Autosave now, on this thread (the periodic one is :meth:`autosave_async`)."""
        self._arm()
        if not (self._dirty or force):
            return None
        store = self.autosave_store()
        if store is None:
            return None
        doc = self.ctx.document
        if not doc.parts and not force:
            return None
        try:
            out = store.save(self.current(), ticket=store.reserve())
        except Exception as e:  # noqa: BLE001 - logged, the next tick retries
            self.ctx.log(self.tr("project.autosave_failed", error=f"{type(e).__name__}: {e}"))
            return None
        self._dirty = False
        from .save_worker import seal_undo
        seal_undo(self.ctx.docs)
        return out

    def autosave_async(self) -> bool:
        """The periodic autosave: a snapshot in event-loop slices, then serialised and written in
        a worker.  False when there is nothing to do (unchanged, empty, no store, one running)."""
        self._arm()
        if not self._dirty or self._autosave_busy:
            return False
        store = self.autosave_store()
        if store is None or not self.ctx.document.parts:
            return False
        from .save_worker import SlicedSnapshot, start_worker
        self._autosave_busy = True
        self._autosave_idle.clear()
        self._dirty = False                  # a change from now on is the next tick's
        ticket = store.reserve()

        def job() -> list:
            try:
                return store.save(pr_box[0], ticket=ticket)
            finally:
                self._autosave_idle.set()    # here, not on the GUI thread: closing waits for it
        pr_box: list = []

        def write(doc) -> None:
            self._snap = None
            pr_box.append(self.current(doc, copy_sections=True))
            start_worker(job, done, "mlaser-autosave")

        def done(_res, err) -> None:
            self._autosave_busy = False
            self._autosave_idle.set()
            if err is not None:
                self._dirty = True           # the next tick retries
                self.ctx.log(self.tr("project.autosave_failed", error=f"{type(err).__name__}: {err}"))
            else:
                from .save_worker import seal_undo
                seal_undo(self.ctx.docs)         # no undo step coalesced across the save point

        self._snap = SlicedSnapshot(self.ctx.docs, write, self.timer, on_error=lambda e: done(None, e))
        self._snap.start()
        return True

    _snap = None

    def wait_idle(self, timeout_s: float = 10.0) -> bool:
        """Closing: a snapshot not finished yet is dropped (the document is saved or discarded
        by the close question), a write in progress is waited for (its file completes)."""
        snap, self._snap = self._snap, None
        if snap is not None and not snap.finished:
            snap.cancel()
            self._autosave_busy = False
            self._autosave_idle.set()
        return self._autosave_idle.wait(timeout_s)


def register(ctx) -> ProjectExtension:
    ext = ProjectExtension(ctx)
    ctx.register_command("file.open_project", ext.open_dialog, label="project.open")
    ctx.register_command("file.save_project", ext.save_dialog, label="project.save")
    ctx.register_command("file.export_chf", ext.export_dialog, label="project.export_chf")
    ctx.provide("project.open", ext.open)
    ctx.provide("project.save", ext.save)
    ctx.provide("project.current", ext.current)
    ctx.on_close(lambda: ext.wait_idle())
    return ext
