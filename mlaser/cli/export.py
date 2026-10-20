"""DXF and SVG output of a Document for ``mlaser convert`` (geometry only).

* DXF (R2010, millimetres, ezdxf): one layer per process layer (``L<n>``), lines as ``LINE``,
  arcs as ``ARC`` (clockwise arcs written with swapped angles), every contour of a part as its
  own entities.  Leads, kerf, micro-joints and parameters are CAM data and are not written
  (DXF has no place for them): the file is the drawing, as a customer would send it.
* SVG: one ``<path>`` per contour (``M``/``L``/``A`` commands, exact arcs), grouped per part,
  Y flipped so the drawing looks as on the canvas (Y up), size in millimetres.

``.chf`` and ``.mlz`` keep everything; use those to keep the CAM settings.
"""
from __future__ import annotations

import html
import math
from typing import Any

from mlaser.model import Arc, Document, Line


def _layer_name(n: int) -> str:
    return f"L{int(n)}"


def write_dxf(doc: Document, path: str) -> str:
    import ezdxf
    from ezdxf import units

    d = ezdxf.new("R2010", setup=False)
    d.units = units.MM
    d.header["$INSUNITS"] = units.MM
    msp = d.modelspace()
    for p in doc.parts:
        layer = _layer_name(p.craft.layer)
        if layer not in d.layers:
            d.layers.add(layer)
        for c in p.contours:
            for s in c.segments:
                if isinstance(s, Arc):
                    cx, cy = s.center
                    rad = s.radius
                    a0 = math.degrees(math.atan2(s.start[1] - cy, s.start[0] - cx))
                    a1 = math.degrees(math.atan2(s.end[1] - cy, s.end[0] - cx))
                    if not s.ccw:
                        a0, a1 = a1, a0
                    if math.isclose(a0 % 360.0, a1 % 360.0, abs_tol=1e-9) and abs(s.sweep()) > math.pi:
                        msp.add_circle((cx, cy), rad, dxfattribs={"layer": layer})
                    else:
                        msp.add_arc((cx, cy), rad, a0, a1, dxfattribs={"layer": layer})
                elif isinstance(s, Line):
                    msp.add_line(tuple(s.start), tuple(s.end), dxfattribs={"layer": layer})
                else:                                   # any other segment: as its polyline
                    pts = [tuple(s.start), tuple(s.end)]
                    msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
    d.saveas(path)
    return path


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def contour_path(c: Any, flip_y: float) -> str:
    """SVG path data of one contour (Y mirrored about ``flip_y``)."""
    segs = list(c.segments)
    if not segs:
        return ""

    def P(pt) -> str:
        return f"{_fmt(pt[0])} {_fmt(flip_y - pt[1])}"

    out = [f"M {P(segs[0].start)}"]
    for s in segs:
        if isinstance(s, Arc):
            rad = s.radius
            sweep = s.sweep()
            large = 1 if abs(sweep) > math.pi else 0
            # Y is flipped: a counter-clockwise arc becomes clockwise on screen (sweep-flag 0)
            sf = 0 if s.ccw else 1
            if abs(abs(sweep) - 2 * math.pi) < 1e-9:     # a full circle: two halves
                mid = s.point_at(0.5)
                out.append(f"A {_fmt(rad)} {_fmt(rad)} 0 1 {sf} {P(mid)}")
                out.append(f"A {_fmt(rad)} {_fmt(rad)} 0 1 {sf} {P(s.end)}")
            else:
                out.append(f"A {_fmt(rad)} {_fmt(rad)} 0 {large} {sf} {P(s.end)}")
        else:
            out.append(f"L {P(s.end)}")
    if c.closed:
        out.append("Z")
    return " ".join(out)


def svg_text(doc: Document, margin: float = 2.0) -> str:
    try:
        x0, y0, x1, y1 = doc.bbox()
    except Exception:  # noqa: BLE001
        x0 = y0 = x1 = y1 = 0.0
    w, h = max(1e-3, x1 - x0) + 2 * margin, max(1e-3, y1 - y0) + 2 * margin
    flip = y1 + y0                       # y' = (y0 + y1) - y keeps the drawing in its box
    vb = f"{_fmt(x0 - margin)} {_fmt(y0 - margin)} {_fmt(w)} {_fmt(h)}"
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(w)}mm" height="{_fmt(h)}mm" viewBox="{vb}">',
             '<g fill="none" stroke="#000" stroke-width="0.1">']
    for i, p in enumerate(doc.parts):
        name = html.escape(p.name or f"part {i + 1}", quote=True)
        lines.append(f'<g id="part{i + 1}" data-name="{name}" data-layer="{int(p.craft.layer)}">')
        for c in p.contours:
            d = contour_path(c, flip)
            if d:
                lines.append(f'<path d="{d}"/>')
        lines.append("</g>")
    lines += ["</g>", "</svg>", ""]
    return "\n".join(lines)


def write_svg(doc: Document, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg_text(doc))
    return path


__all__ = ["write_dxf", "write_svg", "svg_text", "contour_path"]
