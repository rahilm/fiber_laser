"""Limits for untrusted input files, and the cancel / progress context of an import.

Customer drawings and project files come from outside: a small file must not be able to expand
into gigabytes of memory or minutes of CPU (the Review 2 probes: a 26 kB DXF of nested INSERTs,
a 16 kB MINSERT of 30000 x 30000 copies, a 612 kB SVG that uses one 200-path symbol 20000 times,
a 3.6 MB ``.mlz`` zip bomb).  The importers count what they produce against these budgets and
raise :class:`ImportLimitError` (a ``ValueError`` with a message for the operator) instead.

Limits (docs/dev/project-format.md "Limits", docs/user/design.md "Import"):

==============================  ===============  =================================================
limit                           value            where
==============================  ===============  =================================================
DXF entities after expansion    2 000 000        INSERT / MINSERT / block references expanded
                                                 (:func:`mlaser.io.dxf_import.check_expansion`,
                                                 and the modern import's traversal)
DXF copies of one MINSERT       100 000          rows x columns (modern import; the faithful
                                                 import draws one copy, like DxfParse)
DXF nested block depth          16 / 32          faithful / modern (unchanged)
SVG elements after ``<use>``    500 000          the elements in the file plus every element a
                                                 ``<use>`` copies (:mod:`mlaser.io.svg_import`)
==============================  ===============  =================================================

The ``.mlz`` limits (member size, total size, member count, compression ratio, JSON size and
depth) are in :mod:`mlaser.project.format`.

**Cancel and progress.**  The UI runs an import in a worker thread inside an
:class:`ImportContext` (:func:`context`).  The budget counters call :func:`tick`, which raises
:class:`ImportCancelled` once the operator pressed Cancel and publishes the running count for
the progress dialog.  Outside a context (scripts, tests) nothing changes except the limits.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

DXF_MAX_ENTITIES = 2_000_000
DXF_MAX_MINSERT = 100_000
SVG_MAX_ELEMENTS = 500_000
_TICK = 1024                    # counts between two cancel checks


class ImportLimitError(ValueError):
    """The file is larger than an import limit: the message is meant for the operator."""

    def __init__(self, message: str, *, limit: int = 0, what: str = ""):
        super().__init__(message)
        self.limit = limit
        self.what = what


class ImportCancelled(Exception):
    """The operator cancelled the import."""


class ImportContext:
    """Cancel flag and progress of one import (shared between the worker and the dialog)."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.count = 0                  # entities / elements / bytes counted so far
        self.what = ""                  # unit of ``count`` ("entities", "elements", "bytes")

    def cancel(self) -> None:
        self.cancelled.set()


_local = threading.local()


def current() -> Optional[ImportContext]:
    return getattr(_local, "ctx", None)


@contextmanager
def context(ctx: Optional[ImportContext] = None) -> Iterator[ImportContext]:
    """Run an import of this thread inside ``ctx`` (a new one by default)."""
    ctx = ctx or ImportContext()
    old = current()
    _local.ctx = ctx
    try:
        yield ctx
    finally:
        _local.ctx = old


def check_cancel() -> None:
    """Raise :class:`ImportCancelled` when the operator cancelled the import of this thread."""
    ctx = current()
    if ctx is not None and ctx.cancelled.is_set():
        raise ImportCancelled("import cancelled")


def tick(count: int, what: str) -> None:
    """Publish the running count and check for Cancel."""
    ctx = current()
    if ctx is not None:
        ctx.count, ctx.what = count, what
        if ctx.cancelled.is_set():
            raise ImportCancelled("import cancelled")


class Budget:
    """A running count with a limit: :meth:`add` raises :class:`ImportLimitError` above it."""

    __slots__ = ("limit", "n", "what", "message", "_next")

    def __init__(self, limit: int, what: str, message: str):
        self.limit = int(limit)
        self.n = 0
        self.what = what
        self.message = message           # format string with {limit} and {what}
        self._next = _TICK

    def add(self, k: int = 1) -> None:
        self.n += k
        if self.n > self.limit:
            raise ImportLimitError(self.message.format(limit=f"{self.limit:,}", what=self.what),
                                   limit=self.limit, what=self.what)
        if self.n >= self._next:
            self._next = self.n + _TICK
            tick(self.n, self.what)


DXF_TOO_MANY = ("This DXF file expands to more than {limit} {what} (block references / INSERT and "
                "MINSERT copies). It was not imported: it is either damaged or built to exhaust "
                "the computer. Explode or simplify the blocks in the CAD program and export again.")
SVG_TOO_MANY = ("This SVG file expands to more than {limit} {what} (<use> copies of symbols). It was "
                "not imported: it is either damaged or built to exhaust the computer. Convert the "
                "symbols to paths in the drawing program and export again.")


def too_deep(kind: str) -> ImportLimitError:
    """The error for a file nested deeper than the importer can follow (RecursionError)."""
    return ImportLimitError(f"This {kind} file is nested too deeply (groups or blocks inside each other "
                            "thousands of levels deep). It was not imported: it is either damaged or "
                            "built to exhaust the computer.", what="nesting depth")


def entity_budget(limit: int = 0) -> Budget:
    """The DXF budget of entities after block expansion (:data:`DXF_MAX_ENTITIES`)."""
    return Budget(limit or DXF_MAX_ENTITIES, "entities", DXF_TOO_MANY)


def check_minsert(copies: int, limit: int = 0) -> None:
    """Refuse a MINSERT with more than :data:`DXF_MAX_MINSERT` copies (rows x columns)."""
    lim = limit or DXF_MAX_MINSERT
    if copies > lim:
        raise ImportLimitError(f"This DXF file has a MINSERT block array of {copies:,} copies (more "
                               f"than {lim:,}). It was not imported: it is either damaged or built "
                               "to exhaust the computer.", limit=lim, what="MINSERT copies")


__all__ = ["entity_budget", "too_deep", "check_minsert", "DXF_MAX_ENTITIES", "DXF_MAX_MINSERT", "SVG_MAX_ELEMENTS", "Budget", "ImportCancelled",
           "ImportContext", "ImportLimitError", "check_cancel", "context", "current", "tick"]
