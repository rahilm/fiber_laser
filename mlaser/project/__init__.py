"""Native project format ``.mlz`` (docs/dev/project-format.md).

``.mlz`` is the rebuild's own file: the typed document model plus everything saved with it
(layers, technology assignments, nest job and result, job queue, raster engravings, embedded
fonts) and the opaque ``.chf`` records, so a ``.chf`` opened and exported again stays
byte-identical.  ``.chf`` stays the import / export interchange format
(:mod:`mlaser.formats.chf`).  Qt-free.

>>> from mlaser.project import Project, save_project, load_project
>>> save_project("job.mlz", Project(document=doc, technology={"1": {"material": "S235"}}))
>>> load_project("job.mlz").document
"""
from .autosave import Autosave
from .format import (FORMAT, MIMETYPE, SUFFIX, VERSION, Project, ProjectError, atomic_write, dumps_project,
                     export_chf, is_project_file, load_project, loads_project, open_document, save_project)

__all__ = ["FORMAT", "MIMETYPE", "SUFFIX", "VERSION", "Autosave", "Project", "ProjectError", "atomic_write",
           "dumps_project", "export_chf", "is_project_file", "load_project", "loads_project", "open_document",
           "save_project"]
