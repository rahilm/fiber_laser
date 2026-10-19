#!/usr/bin/env python3
"""Write the ``.chf`` test drawings in tests/data/chf/ from mlaser's own model (deterministic).

These files replace the two drawings that used to be copied from the original's install
(``File/autosave.chf``, ``File/Temp/tempGraph.chf``; docs/ASSETS.md).  They are made by
:func:`mlaser.formats.chf.dumps_chf` from documents built here, so they contain nothing of the
vendor's, and they exercise the same reader/writer paths and more:

``sample_v9.chf`` (format 9, what the original writes)
    1. CGlyScan (type 11): a raster engraving of a ring (rows of 20 mm-scale dashes, two per row
       through the hole), several hundred line elements, one serpentine scan path of line
       glyphs and cubic spline U-turns (GLY_SPLINE, 6 control points, the knot vector of the
       original's linker), PWM mode 3 with an explicit switch list, ``+0xe0`` = 4; a few
       elements carry arbitrary ``+0x354`` values (the field is uninitialised in the original).
    2. CGlyGroup (type 9): a plate with rounded corners (lines + CW/CCW arcs), circular holes
       (GLY_CIRCLE), a slot and a bulged polyline hole (GLY_LWPOLY); part craft with lead-in /
       lead-out, outer kerf, micro-joints with widths, overcut, start position and cooling
       points; the holes override the craft (arc lead, inner kerf).
    3. CGlyContour (type 8): a closed cubic spline (GLY_SPLINE), lead line+arc at a corner.
    4. CGlyContour: a full ellipse (GLY_ELLIPSE).
    5. CGlyContour: an open polyline with arcs, a gap (negative overcut).
    6. CGlyContour: a segment with tabs, a sub-segment table and the vision flag; the document's
       vision list names it.
    7. CGlyText (type 10): "MLASER" outlined with the bundled font (text group of contours).

``sample_v5.chf`` (format 5: no lead-out block, no ``+0x354`` / vision fields)
    a circle (closing tolerance 0.1, 5 mm lead-in), a plate with a hole and an arc polyline.

Usage::

    python tools/make_test_chf.py            # (re)write tests/data/chf/*.chf
    python tools/make_test_chf.py --check    # exit 1 if the files differ from what this makes

Re-running gives identical bytes.  Changing the files changes the golden cases built on them
(``chf/sample_v9``, ``chf/sample_v5`` in tools/make_goldens.py): regenerate those deliberately.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

OUT = ROOT / "tests" / "data" / "chf"

from mlaser.cam.flycut import SCAN_ENGRAVE, ScanPath, make_scan_part, switches_from_spans  # noqa: E402
from mlaser.cad.text import text_contours  # noqa: E402
from mlaser.formats import chf  # noqa: E402
from mlaser.model import (Arc, Contour, Craft, Document, Glyph, LayerParams, LeadParams, Line,  # noqa: E402
                          MicroJoint, Part)

SPLINE_KNOTS_UTURN = [0.0, 0.0, 0.0, 0.0, 0.05, 0.95, 1.0, 1.0, 1.0, 1.0]


def r3(v: float) -> float:
    """Coordinates on a 1 µm grid: the .chf text keeps 6 decimals, so these survive exactly."""
    return round(v, 3) + 0.0


def P(x: float, y: float) -> tuple[float, float]:
    return (r3(x), r3(y))


def glyph_contour(glys: list[Glyph], closed: bool, **extra) -> Contour:
    """A contour whose segments are what the reader makes of ``glys`` (so it saves as those
    glyphs and loads back to the same segments)."""
    segs = []
    for g in glys:
        segs += chf.gly_segments(chf.ChfGly(g.dir, g.kind, g.data))
    c = Contour(segs, closed, dict(extra))
    c.set_source_glyphs(glys)
    return c


# ----------------------------------------------------------------------------- 1. scan
def ring_rows(cx: float, cy: float, r_out: float, r_in: float,
              pitch: float) -> list[tuple[float, list[tuple[float, float]]]]:
    """(y, burn dashes (x0, x1)) per row of a ring, bottom to top."""
    rows = []
    n = int(round(2 * r_out / pitch))
    for k in range(1, n):
        y = -r_out + k * pitch
        xo = math.sqrt(max(r_out * r_out - y * y, 0.0))
        if abs(y) < r_in:
            xi = math.sqrt(r_in * r_in - y * y)
            dashes = [(cx - xo, cx - xi), (cx + xi, cx + xo)]
        else:
            dashes = [(cx - xo, cx + xo)]
        rows.append((r3(cy + y), [(r3(a), r3(b)) for a, b in dashes if b - a > 0.05]))
    return rows


def uturn(p0, t0, p3, t1) -> Glyph:
    """Cubic B-spline U-turn between two rows (control polygon as the original's linker:
    out along t0, back along t1, knots 0,0,0,0,.05,.95,1,1,1,1)."""
    d = math.dist(p0, p3)
    h = min(d if d >= 0.5 else 2.0, 2.0)
    b = min(0.4 * h, 2.0)
    a = 0.5 * b
    ctrl = [p0, P(p0[0] + t0[0] * a, p0[1] + t0[1] * a), P(p0[0] + t0[0] * (a + h), p0[1] + t0[1] * (a + h)),
            P(p3[0] - t1[0] * (b + h), p3[1] - t1[1] * (b + h)), P(p3[0] - t1[0] * b, p3[1] - t1[1] * b), p3]
    return Glyph(chf.GLY_SPLINE, {"u0": 0, "u1": 0, "ctrl": ctrl, "knots": list(SPLINE_KNOTS_UTURN)})


def scan_part() -> Part:
    side = 0.4                                   # beam-off run-in / run-out per row (mm)
    rows = ring_rows(30.0, 30.0, 25.0, 9.0, 0.25)
    elems: list[Contour] = []
    glys: list[tuple[Glyph, bool]] = []          # (glyph, beam on)
    prev = None
    for k, (y, dashes) in enumerate(rows):
        fwd = k % 2 == 0
        t = (1.0, 0.0) if fwd else (-1.0, 0.0)
        ds = dashes if fwd else [(b, a) for a, b in reversed(dashes)]
        for a, b in dashes:
            elems.append(Contour([Line((a, y), (b, y))], closed=False))
        start = P(ds[0][0] - t[0] * side, y)
        if prev is not None:
            glys.append((uturn(prev[0], prev[1], start, t), False))
        seg = lambda p, q: Glyph(chf.GLY_SEGMENT, {"start": p, "end": q, "tabs": []})   # noqa: E731
        glys.append((seg(start, (ds[0][0], y)), False))
        for i, (a, b) in enumerate(ds):
            if i:
                glys.append((seg((ds[i - 1][1], y), (a, y)), False))
            glys.append((seg((a, y), (b, y)), True))
        end = P(ds[-1][1] + t[0] * side, y)
        glys.append((seg((ds[-1][1], y), end), False))
        prev = (end, t)
    spans = []
    for g, on in glys:
        spans += [(s, on) for s in chf.gly_segments(chf.ChfGly(g.dir, g.kind, g.data))]
    init, sw = switches_from_spans(spans)
    path_c = glyph_contour([g for g, _ in glys], closed=False)
    path = ScanPath(path_c, [round(s, 6) for s in sw], init, SCAN_ENGRAVE, 3, list(range(len(elems))))
    part = make_scan_part([path], elems, Craft(layer=4), SCAN_ENGRAVE)
    rec = part.extra["chf"]
    for i, v in ((3, -1066022767), (40, 1048576), (41, -1), (200, 327680000)):  # +0x354 "garbage"
        rec.group.elems[i].seg_kind = v
    return part


# ----------------------------------------------------------------------------- 2. plate group
def rounded_rect(x0, y0, w, h, r) -> Contour:
    x1, y1 = x0 + w, y0 + h
    segs = [Line(P(x0 + r, y0), P(x1 - r, y0)), Arc(P(x1 - r, y0), P(x1, y0 + r), P(x1 - r, y0 + r), True),
            Line(P(x1, y0 + r), P(x1, y1 - r)), Arc(P(x1, y1 - r), P(x1 - r, y1), P(x1 - r, y1 - r), True),
            Line(P(x1 - r, y1), P(x0 + r, y1)), Arc(P(x0 + r, y1), P(x0, y1 - r), P(x0 + r, y1 - r), True),
            Line(P(x0, y1 - r), P(x0, y0 + r)), Arc(P(x0, y0 + r), P(x0 + r, y0), P(x0 + r, y0 + r), True)]
    return Contour(segs, True)


def circle(cx, cy, r, ccw=True) -> Contour:
    s = P(cx + r, cy)
    return Contour([Arc(s, s, P(cx, cy), ccw)], True)


def slot(x0, y0, length, r) -> Contour:
    """A slot traversed clockwise (CW arcs: stored as reversed CCW arc glyphs)."""
    a, b = P(x0, y0 + r), P(x0 + length, y0 + r)
    return Contour([Line(P(x0, y0 + 2 * r), P(x0 + length, y0 + 2 * r)),
                    Arc(P(x0 + length, y0 + 2 * r), P(x0 + length, y0), b, False),
                    Line(P(x0 + length, y0), P(x0, y0)),
                    Arc(P(x0, y0), P(x0, y0 + 2 * r), a, False)], True)


def bulged_hole(x0, y0) -> Contour:
    verts = [(P(x0, y0), 0.0), (P(x0 + 12, y0), 0.414214), (P(x0 + 12, y0 + 8), 0.0), (P(x0, y0 + 8), -0.25)]
    return glyph_contour([Glyph(chf.GLY_LWPOLY, {"flag": 1, "verts": verts})], True)


def plate_part() -> Part:
    outer = rounded_rect(70.0, 0.0, 120.0, 60.0, 6.0)
    holes = [circle(85.0, 15.0, 4.0), circle(175.0, 15.0, 4.0), circle(85.0, 45.0, 4.0),
             circle(175.0, 45.0, 2.5, ccw=False), slot(110.0, 25.0, 30.0, 4.0), bulged_hole(150.0, 26.0)]
    craft = Craft(layer=1, kerf_comp=1, kerf_width=0.15,
                  lead_in=LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=1.0),
                  lead_out=LeadParams(kind=1, angle_deg=60.0, length=1.5, radius=1.0),
                  micro_joints=[MicroJoint(40.0, 0.8), MicroJoint(200.0, 1.2)],
                  cool_points=[25.0], start_param=0.125, overcut=0.5)
    hole = Craft(layer=1, kerf_comp=0, kerf_width=0.15,
                 lead_in=LeadParams(kind=2, angle_deg=90.0, length=1.5, radius=1.0),
                 lead_out=LeadParams(kind=0))
    for i, h in enumerate(holes):
        if i != 3:                               # one hole keeps the part's craft
            h.craft = hole
    return Part([outer] + holes, craft, name="plate")


# ----------------------------------------------------------------------------- 3..6 contours
def spline_part() -> Part:
    ctrl = [P(10, 70), P(30, 66), P(48, 72), P(52, 90), P(36, 104), P(16, 100), P(4, 86), P(10, 70)]
    knots = [0.0, 0.0, 0.0, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.0, 1.0, 1.0]
    c = glyph_contour([Glyph(chf.GLY_SPLINE, {"u0": 0, "u1": 0, "ctrl": ctrl, "knots": knots})], True)
    cr = Craft(layer=2, lead_in=LeadParams(kind=3, angle_deg=45.0, length=2.0, radius=1.5, at_corner=True),
               lead_out=LeadParams(kind=2, length=1.0, radius=0.8))
    return Part([c], cr, name="spline")


def ellipse_part() -> Part:
    g = Glyph(chf.GLY_ELLIPSE, {"center": P(80, 85), "axis": P(14, 0), "ratio": 0.5,
                                "t0": 0.0, "t1": round(2 * math.pi, 6)})
    return Part([glyph_contour([g], True)], Craft(layer=2, kerf_comp=1, kerf_width=0.1), name="ellipse")


def open_part() -> Part:
    segs = [Line(P(100, 70), P(120, 70)), Arc(P(120, 70), P(130, 80), P(120, 80), True),
            Line(P(130, 80), P(130, 95)), Arc(P(130, 95), P(140, 105), P(140, 95), False),
            Line(P(140, 105), P(160, 105))]
    cr = Craft(layer=3, lead_in=LeadParams(kind=1, length=2.0), overcut=-0.8)
    return Part([Contour(segs, False)], cr, name="open")


def marked_part() -> Part:
    c = glyph_contour([Glyph(chf.GLY_SEGMENT, {"start": P(170, 70), "end": P(190, 70),
                                               "tabs": [(P(175, 70), P(176, 70)), (P(184, 70), P(185.5, 70))]})],
                      False)
    return Part([c], Craft(layer=3), name="mark")


# ----------------------------------------------------------------------------- 7. text
def text_part() -> Part:
    cs = text_contours("MLASER", height=8.0, origin=(100.0, 115.0), tol=0.02)
    cs = [Contour([Line(P(*s.start), P(*s.end)) for s in c.segments], True) for c in cs]
    cs = [Contour([s for s in c.segments if s.start != s.end], True) for c in cs]
    elems = [chf.chf_from_contour(c) for c in cs]
    for e in elems:
        chf._apply_craft(e, Craft(layer=5))
    grp = chf.ChfGroup(elems=elems, layer=5)
    chf._refresh_group(grp)
    grp.length = sum(e.length for e in elems)
    rec = chf.ChfText(pos=(100.0, 115.0), v140=8.0, v148=0.0, v150=0.0, text="MLASER",
                      font=chf_font_name(), group=grp)
    return chf.part_from_graph(rec)


def chf_font_name() -> str:
    from mlaser.cad.text import DEFAULT_FONT
    return str(DEFAULT_FONT).replace(" ", "")      # the original's reader strips spaces


# ----------------------------------------------------------------------------- documents
def doc_v9() -> Document:
    parts = [scan_part(), plate_part(), spline_part(), ellipse_part(), open_part(), marked_part(), text_part()]
    layers = {i: LayerParams(i, speed=s, power_pct=p)
              for i, s, p in ((1, 80.0, 90.0), (2, 120.0, 70.0), (3, 60.0, 50.0), (4, 300.0, 20.0), (5, 100.0, 60.0))}
    doc = Document(parts, layers, version=9)
    # sub-segment table and vision flag of graph 6 are record fields without a model counterpart
    doc = chf.loads_chf(chf.dumps_chf(doc, 9))
    rec = doc.parts[5].extra["chf"]
    rec.seg_table = [(9.0, True, False), (11.0, False, True)]
    rec.vision = True
    doc.extra["chf"]["vision_graphs"] = [5]
    return doc


def doc_v5() -> Document:
    c = circle(60.0, 60.0, 47.5)
    c.extra["close_tol"] = 0.1
    c1 = Part([c], Craft(layer=0, lead_in=LeadParams(kind=1, angle_deg=90.0, length=5.0)), name="circle")
    rect = Contour([Line(P(120, 20), P(180, 20)), Line(P(180, 20), P(180, 60)), Line(P(180, 60), P(120, 60)),
                    Line(P(120, 60), P(120, 20))], True)
    plate = Part([rect, circle(150.0, 40.0, 6.0, ccw=False)],
        Craft(layer=1, kerf_comp=1, kerf_width=0.2, lead_in=LeadParams(kind=2, length=2.0, radius=1.0)),
        name="plate")
    poly = Part([bulged_hole(200.0, 20.0)], Craft(layer=2), name="poly")
    return Document([c1, plate, poly], version=5)


def build() -> dict[str, bytes]:
    out = {}
    for name, doc, v in (("sample_v9.chf", doc_v9(), 9), ("sample_v5.chf", doc_v5(), 5)):
        b = chf.dumps_chf(doc, v)
        again = chf.dumps_chf(chf.loads_chf(b), v)
        if again != b:
            raise SystemExit(f"{name}: load + save does not reproduce the bytes")
        out[name] = b
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with tests/data/chf instead of writing")
    a = ap.parse_args(argv)
    files = build()
    bad = 0
    for name, b in files.items():
        p = OUT / name
        if a.check:
            same = p.exists() and p.read_bytes() == b
            print(f"{name}: {'OK' if same else 'DIFFERS'} ({len(b)} bytes)")
            bad += not same
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b)
            print(f"wrote {p.relative_to(ROOT)} ({len(b)} bytes)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
