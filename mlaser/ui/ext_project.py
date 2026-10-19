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
crash prompt).
"""
from __future__ import annotations

import logging
import os
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
        self.settings = ctx.settings_schema([
            Setting("autosave_interval_s", "int", 120, unit="s", min=0, max=3600,
                    label="project.autosave_interval"),
        ])
        self.timer = QTimer(self.w)
        self.timer.timeout.connect(self.autosave_now)
        self._arm()
        ctx.on_document_changed(self._changed)

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
    def current(self) -> Project:
        pr = Project(document=self.ctx.document)
        for name, (get, _set) in self.sections.items():
            try:
                v = get()
            except Exception:  # noqa: BLE001 - one broken section must not stop a save
                log.exception("project section %s", name)
                continue
            if v is None:
                continue
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
            pr = run_import(self.w, lambda: load_project(path), f"Opening {os.path.basename(path)}...")
        except ProjectError as e:
            self.ctx.log(self.tr("project.error", error=str(e)))
            show_error(self.w, "Project not opened", str(e))
            return False
        except ImportCancelled:
            self.ctx.log(f"{os.path.basename(path)}: opening cancelled")
            return False
        except ImportBusy as e:
            self.ctx.log(f"{os.path.basename(path)}: not opened ({e})")
            return False
        if self._job_running():                          # a job started while the file was read
            self.ctx.log(f"{os.path.basename(path)}: not opened (a job is running)")
            return False
        self.ctx.replace_document(pr.document, "open project", path=path)
        self.apply(pr)
        for n in pr.notes:
            self.ctx.log(n)
        self.ctx.log(self.tr("project.opened", path=path))
        return True

    def save(self, path: str) -> bool:
        if not path.lower().endswith(".mlz"):
            path += ".mlz"
        pr = self.current()
        try:
            save_project(path, pr)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.ctx.log(self.tr("project.save_error", error=f"{type(e).__name__}: {e}"))
            return False
        if pr.notes:
            self.ctx.log(self.tr("project.notes", notes="; ".join(pr.notes)))
        self.ctx.docs.path = path
        self.ctx.log(self.tr("project.saved", path=path))
        return True

    # ------------------------------------------------------------------ commands
    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.w, self.tr("project.open"), "", self.tr("project.filter"))
        if path:
            if path.lower().endswith(".mlz"):
                self.open(path)
            elif hasattr(self.w, "load_path"):
                self.w.load_path(path)

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
            out = store.save(self.current())
        except Exception as e:  # noqa: BLE001 - logged, the next tick retries
            self.ctx.log(self.tr("project.autosave_failed", error=f"{type(e).__name__}: {e}"))
            return None
        self._dirty = False
        return out


def register(ctx) -> ProjectExtension:
    ext = ProjectExtension(ctx)
    ctx.register_command("file.open_project", ext.open_dialog, label="project.open")
    ctx.register_command("file.save_project", ext.save_dialog, label="project.save")
    ctx.register_command("file.export_chf", ext.export_dialog, label="project.export_chf")
    ctx.provide("project.open", ext.open)
    ctx.provide("project.save", ext.save)
    ctx.provide("project.current", ext.current)
    return ext
