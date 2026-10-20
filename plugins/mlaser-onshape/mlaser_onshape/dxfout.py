"""Write a flattened face as an R12 DXF in millimetres (ezdxf; Qt-free).

Lines, arcs and circles are written as LINE, ARC and CIRCLE; evaluated ellipses, splines and
tessellated edges as 2-D POLYLINEs.  Everything is on layer 0.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from .geometry import FlatFace, iter_entities

class _NoR12Units(logging.Filter):
    """ezdxf says "units are not exported for DXF R12" on every save: known, mm by convention."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "INSUNITS" not in record.getMessage()


logging.getLogger("ezdxf").addFilter(_NoR12Units())

_UNSAFE = re.compile(r"[^A-Za-z0-9._ +=#@()\[\]-]+")


def safe_name(text: str, fallback: str = "part") -> str:
    """A file name stem from a part name or number (no path separators, no reserved chars)."""
    s = _UNSAFE.sub("_", str(text or "")).strip(" ._")
    s = re.sub(r"_+", "_", s)[:80]
    if s.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", s):
        s = "_" + s
    return s or fallback


def write_dxf(flat: FlatFace, path: os.PathLike | str) -> Path:
    import ezdxf

    doc = ezdxf.new("R12", setup=False, units=0)   # R12 has no $INSUNITS: mm by convention
    try:
        doc.header["$MEASUREMENT"] = 1      # metric
    except Exception:  # noqa: BLE001 - optional header
        pass
    msp = doc.modelspace()
    for p in iter_entities(flat):
        if p.kind == "line":
            msp.add_line(p.pts[0], p.pts[1])
        elif p.kind == "circle":
            msp.add_circle(p.center, p.radius)
        elif p.kind == "arc":
            msp.add_arc(p.center, p.radius, p.a0, p.a1)
        elif p.kind == "poly" and len(p.pts) >= 2:
            msp.add_polyline2d(p.pts)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dxf-", suffix=".dxf", dir=str(out.parent))
    os.close(fd)
    try:
        doc.saveas(tmp)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out
