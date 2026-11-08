"""Parts on the system clipboard and in drag and drop (Edit > Cut / Copy / Paste, part-list drags).

Copied parts go to :class:`QClipboard` in several representations, richest first:

* ``application/x-mlaser-parts``: the parts as a small ``.mlz`` project (the native format:
  geometry, crafts, leads, kerf, micro-joints, layers, round-trip records).  Paste between two
  mlaser windows or instances gives the exact parts back;
* ``image/svg+xml`` (also offered as ``text/plain``): the drawing as SVG, for drawing programs,
  browsers and text editors (``mlaser.cli.export.svg_text``);
* ``image/vnd.dxf`` / ``application/dxf``: the drawing as DXF R2010 (``write_dxf``), for CAD
  programs that read DXF from the clipboard.

Paste takes the private type first, then DXF, then SVG (the MIME types or SVG / DXF text in
``text/plain``), then file URLs (a copied file in Finder / Explorer).  Foreign data is untrusted
input: it goes through the same importers and limits as a file (``mlaser.io.limits``), off the
GUI thread when it is a file.

A drag of selected rows from the part list carries the same data plus a ``.dxf`` file URL, so
the parts can be dropped into Finder / Explorer / a file manager or another program.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Iterable, Optional

from PySide6.QtCore import QMimeData, QUrl

from mlaser.model import Document, Part

log = logging.getLogger("mlaser.ui.clipboard")

MIME_PARTS = "application/x-mlaser-parts"
MIME_SVG = "image/svg+xml"
MIME_DXF = ("image/vnd.dxf", "application/dxf", "application/x-dxf")
#: largest clipboard payload read (bytes); a bigger one is refused like an oversized file
MAX_BYTES = 64 * 2**20
#: files a drop or paste can bring in, by extension: open (replace) or import (add)
OPEN_EXTS = (".mlz", ".chf")
IMPORT_EXTS = (".dxf", ".svg", ".nc", ".cnc", ".g", ".txt")


class ClipboardError(ValueError):
    """The clipboard holds data that cannot be read as parts."""


# ---------------------------------------------------------------------------- writing
def parts_document(doc: Document, parts: Iterable[Part]) -> Document:
    """A document of copies of ``parts`` with the layers they use (fresh uids)."""
    from mlaser.services.document import clone_parts
    out = clone_parts(parts)
    for p in out:
        p.selected = False
    used = {p.craft.layer for p in out}
    import copy
    layers = {li: copy.deepcopy(lp) for li, lp in (doc.layers or {}).items() if li in used}
    return Document(parts=out, layers=layers)


def dxf_bytes(doc: Document) -> bytes:
    from mlaser.cli.export import write_dxf
    fd, path = tempfile.mkstemp(suffix=".dxf", prefix="mlaser-clip-")
    os.close(fd)
    try:
        write_dxf(doc, path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def svg_text(doc: Document) -> str:
    from mlaser.cli.export import svg_text as _svg
    return _svg(doc)


def mime_for(doc: Document, parts: Iterable[Part], file_dir: Optional[str] = None) -> QMimeData:
    """The clipboard / drag data of ``parts`` (see the module docstring).  ``file_dir``: also
    write a ``.dxf`` there and offer its URL (drag to a file manager)."""
    from mlaser.project.format import dumps_project
    sub = parts_document(doc, parts)
    md = QMimeData()
    md.setData(MIME_PARTS, dumps_project(sub, embed_fonts=False))
    svg = svg_text(sub)
    md.setData(MIME_SVG, svg.encode("utf-8"))
    md.setText(svg)
    try:
        dxf = dxf_bytes(sub)
    except Exception as exc:  # noqa: BLE001 - DXF is a courtesy format; the native one is there
        log.info("clipboard: no DXF (%s)", exc)
        dxf = b""
    if dxf:
        for m in MIME_DXF:
            md.setData(m, dxf)
    if file_dir and dxf:
        name = _safe_name(sub.parts[0].name if len(sub.parts) == 1 and sub.parts[0].name else "parts")
        path = os.path.join(file_dir, f"{name}.dxf")
        with open(path, "wb") as f:
            f.write(dxf)
        md.setUrls([QUrl.fromLocalFile(path)])
    return md


def native_mime(md: QMimeData) -> QMimeData:
    """A copy of ``md`` allocated by Qt itself, for :meth:`QClipboard.setMimeData`.

    A ``QMimeData()`` made in Python is PySide's C++ subclass whose virtual methods call back
    into Python.  The clipboard owns what it is given and keeps it until the application is
    destroyed; destroying the Python-made object there crashed the interpreter on exit
    (SIGSEGV after the last test, every platform; ``tests/test_clipboard_undo.py``,
    ``test_exit_is_clean_after_copy``).  A ``QMimeData`` created by Qt (the item model's
    ``mimeData`` factory) is a plain one: same formats, no Python in its destructor."""
    from PySide6.QtGui import QStandardItem, QStandardItemModel
    model = QStandardItemModel()
    model.appendRow(QStandardItem(""))
    out = model.mimeData([model.index(0, 0)])
    for f in list(out.formats()):
        out.removeFormat(f)
    for f in md.formats():
        out.setData(f, md.data(f))
    return out


def set_clipboard(md: QMimeData) -> None:
    """Put ``md`` on the system clipboard (as a Qt-made copy: :func:`native_mime`)."""
    from PySide6.QtWidgets import QApplication
    QApplication.clipboard().setMimeData(native_mime(md))


def _safe_name(name: str) -> str:
    s = re.sub(r"[^\w\- ]+", "_", name).strip(" ._") or "parts"
    return s[:60]


# ---------------------------------------------------------------------------- reading
def has_parts(md: Optional[QMimeData]) -> bool:
    """``md`` offers something :func:`parts_from_mime` can read (cheap: no parsing)."""
    if md is None:
        return False
    if md.hasFormat(MIME_PARTS) or md.hasFormat(MIME_SVG) or any(md.hasFormat(m) for m in MIME_DXF):
        return True
    if md.hasText() and _text_kind(md.text()):
        return True
    return bool(drop_files(md))


def _text_kind(text: str) -> str:
    head = text.lstrip()[:400]
    if head.startswith("<svg") or (head.startswith("<?xml") and "<svg" in text[:4000]):
        return "svg"
    if re.match(r"0\s*\r?\n\s*SECTION\b", head) or re.match(r"999\s*\r?\n", head):
        return "dxf"
    return ""


def drop_files(md: Optional[QMimeData]) -> list[str]:
    """Local files in ``md`` that mlaser can open or import."""
    if md is None or not md.hasUrls():
        return []
    out = []
    for u in md.urls():
        if u.isLocalFile():
            p = u.toLocalFile()
            if os.path.splitext(p)[1].lower() in OPEN_EXTS + IMPORT_EXTS and os.path.isfile(p):
                out.append(p)
    return out


def _import_bytes(data: bytes, suffix: str) -> Document:
    if len(data) > MAX_BYTES:
        raise ClipboardError(f"the clipboard data is too large ({len(data) // 2**20} MB)")
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="mlaser-paste-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if suffix == ".svg":
            from mlaser.io.svg_import import import_svg
            return import_svg(path)
        from mlaser.io.dxf_import import import_dxf
        return import_dxf(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def document_from_mime(md: Optional[QMimeData]) -> Optional[Document]:
    """The parts in ``md`` as a document (private type, DXF, SVG, in that order), None when it
    holds none.  Raises :class:`ClipboardError` (or an import error) for unreadable data."""
    if md is None:
        return None
    if md.hasFormat(MIME_PARTS):
        from mlaser.project.format import ProjectError, loads_project
        data = bytes(md.data(MIME_PARTS).data())
        if len(data) > MAX_BYTES:
            raise ClipboardError("the clipboard data is too large")
        try:
            return loads_project(data, name="clipboard").document
        except ProjectError as exc:
            raise ClipboardError(str(exc)) from exc
    for m in MIME_DXF:
        if md.hasFormat(m):
            return _import_bytes(bytes(md.data(m).data()), ".dxf")
    if md.hasFormat(MIME_SVG):
        return _import_bytes(bytes(md.data(MIME_SVG).data()), ".svg")
    if md.hasText():
        text = md.text()
        kind = _text_kind(text)
        if kind:
            return _import_bytes(text.encode("utf-8"), "." + kind)
    return None


__all__ = ["MIME_PARTS", "MIME_SVG", "MIME_DXF", "mime_for", "parts_document", "has_parts", "document_from_mime",
           "drop_files", "ClipboardError", "OPEN_EXTS", "IMPORT_EXTS"]
