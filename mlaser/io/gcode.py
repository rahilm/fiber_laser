"""G-code export/import in the dialect of ``$ORIG/SVG/Output.nc``.

Output.nc is produced by SVG2GCode.exe, a renamed build of the open-source
Haskell tool *juicy-gcode 1.0.0.0* (GHC 8.10.7).  MainApp.exe runs
``SVG2GCode.exe "<file.svg>" -o "<appdir>\\SVG\\Output.nc"`` (routine near
0x4d45a0, CreateProcessW + WaitForSingleObject) and then imports Output.nc as an
NC file.  Dialect (observed in Output.nc, juicy-gcode default flavor)::

    G17
    G90
    G0 Z1                 header: tool up
    G0 X0 Y0
    G00 Z1                per contour: tool (laser) off
    G00 X.. Y..           rapid to start
    G01 Z0 F10.00         tool down == laser on (only feed word in the file)
    G01 X.. Y..           cut moves, no F
    G02/G03 X.. Y.. I.. J..   arcs, I/J relative to arc start
    G0 Z1                 footer (no M2/M30, no M3/M5)

All X/Y/I/J use ``%.4f``; lines end with CRLF.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

from ..toolpath.items import CutArc, CutLine, LaserOff, LaserOn, Rapid
from ..model import Arc, Contour, Document, Line, Part

EOL = "\r\n"


def _f(v: float) -> str:
    s = "%.4f" % v
    return "0.0000" if s == "-0.0000" else s


def _move(seg) -> str:
    x, y = seg.end
    if isinstance(seg, Line):
        return f"G01 X{_f(x)} Y{_f(y)}"
    i = seg.center[0] - seg.start[0]
    j = seg.center[1] - seg.start[1]
    g = "G03" if seg.ccw else "G02"
    return f"{g} X{_f(x)} Y{_f(y)} I{_f(i)} J{_f(j)}"


def _split_full_circles(segs):
    """G02/G03 with start == end is ambiguous for many readers: split in two halves."""
    for s in segs:
        if isinstance(s, Arc) and math.dist(s.start, s.end) < 1e-9:
            mid = s.point_at(0.5)
            yield Arc(s.start, mid, s.center, s.ccw)
            yield Arc(mid, s.end, s.center, s.ccw)
        else:
            yield s


def document_to_gcode(doc: Document, feed: float = 10.0) -> str:
    """Export every contour of every part in document order (Output.nc style)."""
    contours = [c for p in doc.parts for c in p.contours if c.segments]
    return contours_to_gcode(contours, feed)


def contours_to_gcode(contours: Iterable[Contour], feed: float = 10.0) -> str:
    out = ["G17", "G90", "G0 Z1", "G0 X0 Y0"]
    for c in contours:
        x, y = c.segments[0].start
        out += ["G00 Z1", f"G00 X{_f(x)} Y{_f(y)}", "G01 Z0 F%.2f" % feed]
        out += [_move(s) for s in _split_full_circles(c.segments)]
    out.append("G0 Z1")
    return EOL.join(out) + EOL


def toolpath_to_gcode(items, feed_scale: float = 60.0) -> str:
    """Export a MotionItem stream.  Feeds are written in mm/min (mm/s * 60) on
    the first cut move after each laser-on; laser on/off map to Z0/Z1 like Output.nc."""
    out = ["G17", "G90", "G0 Z1", "G0 X0 Y0"]
    cur_feed = None
    on = False
    for it in items:
        if isinstance(it, Rapid):
            if on:
                out.append("G00 Z1")
                on = False
            out.append(f"G00 X{_f(it.end[0])} Y{_f(it.end[1])}")
        elif isinstance(it, LaserOn):
            out.append("G01 Z0")
            on = True
        elif isinstance(it, LaserOff):
            out.append("G00 Z1")
            on = False
        elif isinstance(it, (CutLine, CutArc)):
            if it.laser and not on:
                out.append("G01 Z0")
                on = True
            elif not it.laser and on:
                out.append("G00 Z1")
                on = False
            seg = Line(it.start, it.end) if isinstance(it, CutLine) else Arc(it.start, it.end, it.center, it.ccw)
            parts = list(_split_full_circles([seg]))
            for k, sg in enumerate(parts):
                line = _move(sg)
                if k == 0 and it.feed != cur_feed:
                    line += " F%.2f" % (it.feed * feed_scale)
                    cur_feed = it.feed
                out.append(line)
    out.append("G0 Z1")
    return EOL.join(out) + EOL


_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def parse_gcode(text: str) -> Document:
    """Import G-code (G0/G1/G2/G3, G90/G91, I/J incremental, Z<=0 or M3/M7 = laser on).

    Each laser-on run becomes one contour/part (closed if it ends at its start).
    """
    x = y = 0.0
    z = 1.0
    absolute = True
    mode = 0
    laser = False
    cur: list = []
    contours: list[Contour] = []

    def flush():
        nonlocal cur
        if cur:
            closed = math.dist(cur[0].start, cur[-1].end) < 1e-3
            contours.append(Contour(cur, closed))
        cur = []

    for raw in text.splitlines():
        line = re.sub(r"\(.*?\)|;.*", "", raw).strip()
        if not line:
            continue
        words = [(w.upper(), float(v)) for w, v in _WORD.findall(line)]
        d = {}
        for w, v in words:
            if w == "G":
                g = int(round(v))
                if g in (0, 1, 2, 3):
                    mode = g
                elif g == 90:
                    absolute = True
                elif g == 91:
                    absolute = False
            elif w == "M":
                m = int(round(v))
                if m in (3, 4, 7):
                    laser = True
                elif m in (5, 9):
                    laser = False
                    flush()
            else:
                d[w] = v
        if "Z" in d:
            z = d["Z"] if absolute else z + d["Z"]
            new_laser = z <= 0.0
            if laser and not new_laser:
                flush()
            laser = new_laser
        if "X" not in d and "Y" not in d:
            continue
        nx = d.get("X", x if absolute else 0.0)
        ny = d.get("Y", y if absolute else 0.0)
        if not absolute:
            nx, ny = x + nx, y + ny
        if mode == 0 or not laser:
            flush()
        elif mode == 1:
            if math.dist((x, y), (nx, ny)) > 1e-12:
                cur.append(Line((x, y), (nx, ny)))
        else:
            if "R" in d and "I" not in d and "J" not in d:
                r = d["R"]
                ch = math.dist((x, y), (nx, ny))
                h = math.sqrt(max(r * r - ch * ch / 4, 0))
                mx, my = (x + nx) / 2, (y + ny) / 2
                ux, uy = -(ny - y) / ch, (nx - x) / ch
                sgn = 1 if (mode == 3) == (r > 0) else -1
                c = (mx + ux * h * sgn, my + uy * h * sgn)
            else:
                c = (x + d.get("I", 0.0), y + d.get("J", 0.0))
            cur.append(Arc((x, y), (nx, ny), c, ccw=(mode == 3)))
        x, y = nx, ny
    flush()
    doc = Document()
    for i, c in enumerate(contours):
        doc.parts.append(Part(contours=[c], name=f"nc#{i}"))
    return doc


def read_gcode(path: str) -> Document:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_gcode(f.read())


def write_gcode(path: str, doc: Document, feed: float = 10.0) -> None:
    with open(path, "w", newline="") as f:
        f.write(document_to_gcode(doc, feed))
