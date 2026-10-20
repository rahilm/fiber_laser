"""Block references of a DXF file, for part recognition by block (``services.parts``).

A separate, explicit step after the import: it reads the file again with ezdxf and returns the
bounding box of every INSERT in model space.  It does not touch the DxfParse-faithful import
(:mod:`mlaser.io.dxf_import`, :mod:`mlaser.parity_ports.dxfparse`), whose output stays exactly
the original program's.

The boxes are in DXF coordinates; the importer moves the drawing (``doc.extra
["placement_offset"]``), :func:`insert_boxes` applies that offset when it is given.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import ezdxf

from .dxf_import import check_expansion

__all__ = ["InsertBox", "insert_boxes"]


@dataclass(frozen=True)
class InsertBox:
    block: str                                   # block name
    bbox: tuple[float, float, float, float]      # x0, y0, x1, y1 (placed coordinates)
    layer: str = ""
    handle: str = ""


def insert_boxes(path: str, offset: Optional[tuple[float, float]] = None,
                 max_inserts: int = 100_000) -> list[InsertBox]:
    """The model-space INSERTs of ``path`` with their extents (empty blocks are skipped).

    Untrusted input: the same expansion limits as the import apply (``io.limits``), and at most
    ``max_inserts`` references are measured."""
    from ezdxf import bbox as _bbox
    try:
        dxf = ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        from ezdxf import recover
        dxf, _ = recover.readfile(path)
    msp = dxf.modelspace()
    check_expansion([msp], max_depth=32, multi_insert=True)
    dx, dy = offset or (0.0, 0.0)
    out: list[InsertBox] = []
    for e in msp.query("INSERT"):
        if len(out) >= max_inserts:
            break
        try:
            ext = _bbox.extents([e], fast=True)
        except Exception:  # noqa: BLE001 - a broken block is simply not a part boundary
            continue
        if not ext.has_data:
            continue
        x0, y0 = ext.extmin.x + dx, ext.extmin.y + dy
        x1, y1 = ext.extmax.x + dx, ext.extmax.y + dy
        out.append(InsertBox(str(e.dxf.name), (x0, y0, x1, y1), str(e.dxf.get("layer", "")),
                             str(e.dxf.get("handle", ""))))
    return out
