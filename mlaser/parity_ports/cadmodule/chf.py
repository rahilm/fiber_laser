"""Reader/writer for Mlaser ``.chf`` drawing/job files (``autosave.chf``, ``Temp/tempGraph.chf``).

Everything here is reconstructed from CADModule.dll (Mlaser v0.0.1.51_SP1); see docs/formats.md
for the field-by-field table. Key evidence:

* document save ``FUN_100ebdd0`` -> version int (this+0x9a0, set to 9 in ctor ``FUN_100eb..`` line
  ``param_1[0x268] = 9``) -> graph list ``FUN_100b1a20``
* document load ``FUN_100f04a0``/``FUN_100ee650`` -> ``FUN_100bd0c0`` -> graph list ``FUN_100bcec0``
* graph factory ``FUN_100ad550``: 8 CGlyContour, 9 CGlyGroup, 10 CGlyText, 11 CGlyScan,
  12 CGlyContourEx
* glyph factory ``FUN_10062860``: 1 point, 2 segment, 3 arc, 4 circle, 5 ellipse arc,
  6 lwpolyline, 7 spline
* contour save/load ``FUN_100660d0`` / ``FUN_10072ad0``; group ``FUN_1007bf70`` / ``FUN_1007df90``;
  scan ``FUN_1009a080`` / ``FUN_1009c9c0``; text ``FUN_100aaea0`` / ``FUN_100acf50``;
  contourEx ``FUN_10077d70`` / ``FUN_10079a80`` (+ ``FUN_10077bc0``).

The loader keeps the complete record tree (``ChfContour`` etc.) in ``Part.extra['chf']`` /
``Contour.extra['chf']`` so that ``save_chf(load_chf(p))`` reproduces the file byte for byte.
Model-level edits (``Part.craft``, ``Contour.segments``) are detected against a snapshot taken at
load time and written back into the records.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional, Union

from ...geom import curves as _geom
from ...model import Arc, Contour, Craft, Document, Glyph, GlyphKind, LeadParams, Line, Part, Segment
from .scio import ScFormatError, ScReader, ScWriter
from .._registry import port

__port__ = {
    "source": "CADModule",
    "functions": {
        "FUN_100ebdd0": "dumps_chf (document save)", "FUN_100f04a0": "loads_chf (document load)",
        "FUN_100ee650": "loads_chf", "FUN_100bd0c0": "loads_chf (graph list)", "FUN_100bcec0": "_read_graph",
        "FUN_100b1a20": "_write_graph (graph list)", "FUN_100ad550": "_read_graph (graph factory)",
        "FUN_10062860": "_read_gly (glyph factory)", "FUN_100660d0": "_write_contour",
        "FUN_10072ad0": "_read_contour", "FUN_1007bf70": "_write_group_body", "FUN_1007df90": "_read_group_body",
        "FUN_1009a080": "ChfScan save", "FUN_1009c9c0": "ChfScan load", "FUN_100aaea0": "ChfText save",
        "FUN_100acf50": "ChfText load", "FUN_10077d70": "ChfContourEx save", "FUN_10079a80": "ChfContourEx load",
    },
    "parity": ["chf", "import_bytes"],
}

CURRENT_VERSION = 9


class GraphKind(IntEnum):
    """Graph record types of the .chf graph list (graph factory FUN_100ad550)."""
    CONTOUR = 8         # CGlyContour
    GROUP = 9           # CGlyGroup
    TEXT = 10           # CGlyText
    SCAN = 11           # CGlyScan
    CONTOUREX = 12      # CGlyContourEx


G_CONTOUR, G_GROUP, G_TEXT, G_SCAN, G_CONTOUREX = GraphKind
# glyph types (FUN_10062860): model.GlyphKind
GLY_POINT, GLY_SEGMENT, GLY_ARC, GLY_CIRCLE, GLY_ELLIPSE, GLY_LWPOLY, GLY_SPLINE = GlyphKind

P = tuple[float, float]


def _old(v: int) -> bool:
    """Versions 2..4 carry extra lines the current reader skips (``param - 2U < 3``)."""
    return 2 <= v <= 4


# --------------------------------------------------------------------------- records
@dataclass
class ChfGly:
    """One primitive inside a contour (``####Gly: n``).

    ``dir`` is the int stored next to the glyph pointer in the contour's (glyph*, int) vector;
    -1 means the glyph is traversed backwards (see FUN_10071xxx use ``piVar1[i*2+1] == -1``).
    """
    dir: int = 1
    kind: int = GLY_SEGMENT
    data: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)   # v2..4: 3 opaque lines after the glyph


@dataclass
class ChfLead:
    kind: int = 0
    angle: float = 90.0    # default DAT_1011d6c8
    length: float = 2.0    # default DAT_1011d6c0
    radius: float = 1.0    # default 1.0 (CGuideCurve ctor FUN_10068890)


@dataclass
class ChfContour:
    """CGlyContour (graph type 8, also every group element and scan path)."""
    tol: float = 0.01                      # +0xd8 closing tolerance (ctor arg DAT_1011d800)
    length: float = 0.0                    # +0x48 total length (IGraph)
    bbox_min: P = (0.0, 0.0)               # +0x28
    bbox_max: P = (0.0, 0.0)               # +0x38
    start: P = (0.0, 0.0)                  # +0x68 start point
    end: P = (0.0, 0.0)                    # +0x78 end point
    glys: list = field(default_factory=list)
    seg_kind: int = 0                      # +0x354 (v>6) uninitialised in ctor -> often garbage
    seg_table: list = field(default_factory=list)   # +0x344 (v>6): (length, flag, flag)
    vision: bool = False                   # +0xb1 (v>7) "<Vision Feature>"
    layer: int = 0                         # +0x0c IGraph (INFERRED: layer / colour index)
    flag88: int = 1                        # +0x88 IGraph (default 1, INFERRED: visible/enabled)
    kerf_side: int = -1                    # +0x100 (default -1)
    kerf_width: float = 0.0                # +0x108
    pwm_mode: int = 1                      # +0x114 (default 1)
    pwm_pairs: list = field(default_factory=list)     # +0x118/+0x128 (pos 0..1, width mm)
    pwm_switch: list = field(default_factory=list)    # +0x13c on/off positions 0..1
    start_param: float = 0.0               # +0x180 start position (fraction of length)
    overcut: float = 0.0                   # +0x198 overcut/overlap length (mm, INFERRED)
    lead_in: ChfLead = field(default_factory=ChfLead)      # +0x1b8..+0x1d0
    lead_corner: bool = False              # +0x1f8
    lead_out: ChfLead = field(default_factory=ChfLead)     # +0x1d8..+0x1f0 (v>=6)
    cool_pos: list = field(default_factory=list)           # +0x15c (v>2)
    skipped: dict = field(default_factory=dict)            # v2..4 opaque lines


@dataclass
class ChfGroup:
    """CGlyGroup (type 9); also the base of text/scan/contourEx."""
    length: float = 0.0
    bbox_min: P = (0.0, 0.0)
    bbox_max: P = (0.0, 0.0)
    start: P = (0.0, 0.0)
    end: P = (0.0, 0.0)
    elems: list = field(default_factory=list)    # list[ChfContour]
    layer: int = 0
    flag88: int = 1
    skipped: list = field(default_factory=list)


@dataclass
class ChfText:
    """CGlyText (type 10)."""
    pos: P = (0.0, 0.0)          # +0x130
    v140: float = 0.0            # +0x140 (INFERRED height)
    v148: float = 0.0            # +0x148 (INFERRED angle)
    v150: float = 0.0            # +0x150
    text: str = ""               # +0xc8 (ANSI line; original reader strips spaces)
    font: str = ""               # +0xe8
    p108: float = 1.0            # +0x108 (load default 1.0)
    p110: float = 20.0           # +0x110 (load default 20.0)
    p118: float = 0.0            # +0x118
    group: Optional[ChfGroup] = None   # v>=4
    layer: int = 0               # v<4 only
    flag88: int = 1              # v<4 only
    skipped: list = field(default_factory=list)


@dataclass
class ChfScan:
    """CGlyScan (type 11): group of elements + computed scan path contours."""
    group: ChfGroup = field(default_factory=ChfGroup)
    paths: list = field(default_factory=list)    # list[ChfContour]
    e0: int = 0                                  # +0xe0 (v>8)
    skipped: list = field(default_factory=list)


@dataclass
class ChfContourEx:
    """CGlyContourEx (type 12): group + link records (0x48 bytes each)."""
    group: ChfGroup = field(default_factory=ChfGroup)
    links: list = field(default_factory=list)    # dicts: a,b,d08,d10,d38,d40,p18,p28


ChfGraph = Union[ChfContour, ChfGroup, ChfText, ChfScan, ChfContourEx]
_TYPE_OF = {ChfContour: G_CONTOUR, ChfGroup: G_GROUP, ChfText: G_TEXT, ChfScan: G_SCAN,
            ChfContourEx: G_CONTOUREX}


# --------------------------------------------------------------------------- reading
def _read_gly(r: ScReader, kind: int, v: int) -> dict:
    if kind == GLY_POINT:                              # FUN_10091520
        return {"p": r.point()}
    if kind == GLY_SEGMENT:                            # FUN_10092db0
        d: dict[str, Any] = {"start": r.point(), "end": r.point()}
        if v > 6:
            n = r.int()
            d["tabs"] = [(r.point(), r.point()) for _ in range(n)]
        return d
    if kind == GLY_ARC:                                # FUN_100867e0 -> FUN_10001500
        return {"center": r.point(), "radius": r.double(), "a0": r.double(), "a1": r.double()}
    if kind == GLY_CIRCLE:                             # FUN_10088180
        return {"center": r.point(), "radius": r.double()}
    if kind == GLY_ELLIPSE:                            # FUN_1008a640
        return {"center": r.point(), "axis": r.point(), "ratio": r.double(),
                "t0": r.double(), "t1": r.double()}
    if kind == GLY_LWPOLY:                             # FUN_10090c50
        flag = r.int()
        n = r.int()
        return {"flag": flag, "verts": [(r.point(), r.double()) for _ in range(n)]}
    if kind == GLY_SPLINE:                             # FUN_10097450 (u0/u1 read and discarded)
        u0, u1 = r.int(), r.int()
        n = r.int()
        ctrl = [r.point() for _ in range(n)]
        m = r.int()
        knots = [r.double() for _ in range(m)]
        return {"u0": u0, "u1": u1, "ctrl": ctrl, "knots": knots}
    raise ScFormatError(f"line {r.pos}: unknown glyph type {kind}")


def _read_contour(r: ScReader, v: int) -> ChfContour:
    """FUN_10072ad0."""
    c = ChfContour()
    c.tol = r.double()
    r.tag("<Glyphs>")
    if _old(v):
        c.skipped["glyphs"] = r.skip(10)
    c.length = r.double()
    c.bbox_min, c.bbox_max, c.start, c.end = r.point(), r.point(), r.point(), r.point()
    n = r.int()
    for _ in range(n):
        r.tag()                                            # ####Gly: i
        g = ChfGly(dir=r.int(), kind=r.int())
        g.data = _read_gly(r, g.kind, v)
        if _old(v):
            g.skipped = r.skip(3)
        c.glys.append(g)
    if v > 6:
        c.seg_kind = r.int()
        m = r.int()
        c.seg_table = [(r.double(), r.bool(), r.bool()) for _ in range(m)]
    if v > 7:
        r.tag("<Vision Feature>")
        c.vision = r.bool()
        r.tag("<End Vision Feature>")
    r.tag("<End Glyphs>")
    c.layer = r.int()
    c.flag88 = r.int()
    r.tag("<Crafts>")
    if _old(v):
        c.skipped["crafts"] = r.skip(20)
    c.kerf_side = r.int()
    c.kerf_width = r.double()
    r.tag("<PWM Control>")
    c.pwm_mode = r.int()
    n = r.int()
    c.pwm_pairs = [(r.double(), r.double()) for _ in range(n)]
    m = r.int()
    c.pwm_switch = [r.double() for _ in range(m)]
    r.tag("<End PWM Control>")
    c.start_param = r.double()
    c.overcut = r.double()
    r.tag("<GuideCurve Para>")
    li = ChfLead(kind=r.int(), angle=r.double(), length=r.double())
    li.radius = r.double() if v >= 2 else 1.0
    c.lead_in = li
    c.lead_corner = r.bool()
    if v >= 6:
        c.lead_out = ChfLead(kind=r.int(), angle=r.double(), length=r.double(), radius=r.double())
    else:
        c.lead_out = ChfLead(kind=0)
    r.tag("<End GuideCurve Para>")
    if v > 2:
        r.tag("<coolPos Para>")
        n = r.int()
        c.cool_pos = [r.double() for _ in range(n)]
        r.tag("<End coolPos Para>")
    r.tag("<End Crafts>")
    return c


def _read_group_body(r: ScReader, v: int) -> ChfGroup:
    """FUN_1007df90."""
    g = ChfGroup()
    if _old(v):
        g.skipped = r.skip(3)
    g.length = r.double()
    g.bbox_min, g.bbox_max, g.start, g.end = r.point(), r.point(), r.point(), r.point()
    n = r.int()
    for _ in range(n):
        r.tag()                                            # ####Group elem NO:i
        g.elems.append(_read_contour(r, v))
    g.layer = r.int()
    g.flag88 = r.int()
    return g


def _read_graph(r: ScReader, gtype: int, v: int) -> ChfGraph:
    if gtype == G_CONTOUR:
        return _read_contour(r, v)
    if gtype == G_GROUP:
        return _read_group_body(r, v)
    if gtype == G_SCAN:                                    # FUN_1009c9c0
        s = ChfScan(group=_read_group_body(r, v))
        r.tag("<Scan path>")
        if _old(v):
            s.skipped = r.skip(5)
        n = r.int()
        for _ in range(n):
            r.tag()                                        # ####Path:i
            s.paths.append(_read_contour(r, v))
        if v >= 9:
            s.e0 = r.int()
        r.tag("<End Scan path>")
        return s
    if gtype == G_TEXT:                                    # FUN_100acf50
        t = ChfText()
        if _old(v):
            t.skipped = r.skip(5)
        t.pos = r.point()
        t.v140, t.v148, t.v150 = r.double(), r.double(), r.double()
        t.text = r.wstring_raw()
        r.tag("<Text para>")
        t.font = r.wstring_raw()
        t.p108, t.p110, t.p118 = r.double(), r.double(), r.double()
        r.tag("<End Text para>")
        if v < 4:
            t.layer, t.flag88 = r.int(), r.int()
        else:
            t.group = _read_group_body(r, v)
        return t
    if gtype == G_CONTOUREX:                               # FUN_10079a80 / FUN_10077bc0
        e = ChfContourEx(group=_read_group_body(r, v))
        n = r.int()
        for _ in range(n):
            r.tag()                                        # ####ContourEx link info:i
            lk = {"a": r.int(), "b": r.int(), "d08": r.double(), "d10": r.double(),
                  "d38": r.double(), "d40": r.double()}
            lk["p18"] = r.point() if lk["a"] == 1 else r.line()  # type: ignore[assignment]
            lk["p28"] = r.point() if lk["b"] == 1 else r.line()  # type: ignore[assignment]
            e.links.append(lk)
        return e
    raise ScFormatError(f"line {r.pos}: unknown graph type {gtype}")


# --------------------------------------------------------------------------- writing
def _write_gly(w: ScWriter, g: ChfGly, v: int) -> None:
    d = g.data
    k = g.kind
    if k == GLY_POINT:
        w.point(d["p"])
    elif k == GLY_SEGMENT:                                 # FUN_10091a10
        w.point(d["start"]); w.point(d["end"])
        if v > 6:
            tabs = d.get("tabs", [])
            w.int(len(tabs))
            for a, b in tabs:
                w.point(a); w.point(b)
    elif k == GLY_ARC:                                     # FUN_10084270
        w.point(d["center"]); w.double(d["radius"]); w.double(d["a0"]); w.double(d["a1"])
    elif k == GLY_CIRCLE:                                  # FUN_10086e10
        w.point(d["center"]); w.double(d["radius"])
    elif k == GLY_ELLIPSE:                                 # FUN_10088430
        w.point(d["center"]); w.point(d["axis"])
        w.double(d["ratio"]); w.double(d["t0"]); w.double(d["t1"])
    elif k == GLY_LWPOLY:                                  # FUN_1008bcf0
        w.int(d.get("flag", 0)); w.int(len(d["verts"]))
        for p, b in d["verts"]:
            w.point(p); w.double(b)
    elif k == GLY_SPLINE:                                  # FUN_10093260
        w.int(d.get("u0", 0)); w.int(d.get("u1", 0))
        w.int(len(d["ctrl"]))
        for p in d["ctrl"]:
            w.point(p)
        w.int(len(d["knots"]))
        for x in d["knots"]:
            w.double(x)
    else:
        raise ValueError(f"unknown glyph type {k}")


def _skip_out(w: ScWriter, lines: list, n: int) -> None:
    lines = list(lines) + ["0"] * (n - len(lines))
    for s in lines[:n]:
        w.str(s)


def _write_contour(w: ScWriter, c: ChfContour, v: int) -> None:
    """FUN_100660d0 (plus the version gates of the reader FUN_10072ad0)."""
    w.double(c.tol)
    w.str("<Glyphs>")
    if _old(v):
        _skip_out(w, c.skipped.get("glyphs", []), 10)
    w.double(c.length)
    w.point(c.bbox_min); w.point(c.bbox_max); w.point(c.start); w.point(c.end)
    w.int(len(c.glys))
    for i, g in enumerate(c.glys, 1):
        w.tag_int("####Gly: ", i)
        w.int(g.dir)
        w.int(g.kind)
        _write_gly(w, g, v)
        if _old(v):
            _skip_out(w, g.skipped, 3)
    if v > 6:
        w.int(c.seg_kind)
        w.int(len(c.seg_table))
        for ln, f1, f2 in c.seg_table:
            w.double(ln); w.bool(f1); w.bool(f2)
    if v > 7:
        w.str("<Vision Feature>")
        w.bool(c.vision)
        w.str("<End Vision Feature>")
    w.str("<End Glyphs>")
    w.int(c.layer)
    w.int(c.flag88)
    w.str("<Crafts>")
    if _old(v):
        _skip_out(w, c.skipped.get("crafts", []), 20)
    w.int(c.kerf_side)
    w.double(c.kerf_width)
    w.str("<PWM Control>")
    w.int(c.pwm_mode)
    w.int(len(c.pwm_pairs))
    for a, b in c.pwm_pairs:
        w.double(a); w.double(b)
    w.int(len(c.pwm_switch))
    for x in c.pwm_switch:
        w.double(x)
    w.str("<End PWM Control>")
    w.double(c.start_param)
    w.double(c.overcut)
    w.str("<GuideCurve Para>")
    w.int(c.lead_in.kind); w.double(c.lead_in.angle); w.double(c.lead_in.length)
    if v >= 2:
        w.double(c.lead_in.radius)
    w.bool(c.lead_corner)
    if v >= 6:
        lo = c.lead_out
        w.int(lo.kind); w.double(lo.angle); w.double(lo.length); w.double(lo.radius)
    w.str("<End GuideCurve Para>")
    if v > 2:
        w.str("<coolPos Para>")
        w.int(len(c.cool_pos))
        for x in c.cool_pos:
            w.double(x)
        w.str("<End coolPos Para>")
    w.str("<End Crafts>")


def _write_group_body(w: ScWriter, g: ChfGroup, v: int) -> None:
    """FUN_1007bf70."""
    if _old(v):
        _skip_out(w, g.skipped, 3)
    w.double(g.length)
    w.point(g.bbox_min); w.point(g.bbox_max); w.point(g.start); w.point(g.end)
    w.int(len(g.elems))
    for i, e in enumerate(g.elems, 1):
        w.tag_int("####Group elem NO:", i)
        _write_contour(w, e, v)
    w.int(g.layer)
    w.int(g.flag88)


def _write_graph(w: ScWriter, g: ChfGraph, v: int) -> None:
    if isinstance(g, ChfContour):
        _write_contour(w, g, v)
    elif isinstance(g, ChfGroup):
        _write_group_body(w, g, v)
    elif isinstance(g, ChfScan):                           # FUN_1009a080
        _write_group_body(w, g.group, v)
        w.str("<Scan path>")
        if _old(v):
            _skip_out(w, g.skipped, 5)
        w.int(len(g.paths))
        for i, p in enumerate(g.paths, 1):
            w.tag_int("####Path:", i)
            _write_contour(w, p, v)
        if v > 8:
            w.int(g.e0)
        w.str("<End Scan path>")
    elif isinstance(g, ChfText):                           # FUN_100aaea0
        if _old(v):
            _skip_out(w, g.skipped, 5)
        w.point(g.pos); w.double(g.v140); w.double(g.v148); w.double(g.v150)
        w.str(g.text)
        w.str("<Text para>")
        w.str(g.font)
        w.double(g.p108); w.double(g.p110); w.double(g.p118)
        w.str("<End Text para>")
        if v < 4:
            w.int(g.layer); w.int(g.flag88)
        else:
            _write_group_body(w, g.group or ChfGroup(layer=g.layer, flag88=g.flag88), v)
    elif isinstance(g, ChfContourEx):                      # FUN_10077d70
        _write_group_body(w, g.group, v)
        w.int(len(g.links))
        for i, lk in enumerate(g.links, 1):
            w.tag_int("####ContourEx link info:", i)
            w.int(lk["a"]); w.int(lk["b"])
            w.double(lk["d08"]); w.double(lk["d10"]); w.double(lk["d38"]); w.double(lk["d40"])
            for key in ("p18", "p28"):
                p = lk[key]
                w.str(p) if isinstance(p, str) else w.point(p)
    else:
        raise TypeError(type(g))


# --------------------------------------------------------------------------- geometry
def _polar(c: P, r: float, a: float) -> P:
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def _bspline_points(ctrl: list, knots: list, tol: float = _geom.SPLINE_TOL) -> list:
    """Polyline through a spline glyph (cubic, de Boor), adaptive (D-15).

    The original flattens a CEditableSpline with splineAnalyer (CADModule FUN_100953d0 ->
    splineAnalyer vtable+0x2c FUN_100bf350) to chords of at most sqrt(2*R*e) = 0.632 mm turning
    at most acos(1 - e/R) = 18.19 deg (e = 0.1, R = 2); the rebuild uses the same limits and also
    keeps every chord within ``tol`` of the curve (geom.curves.flatten_bspline)."""
    n = len(ctrl)
    k = len(knots) - n          # order
    if n == 0 or k < 1:
        return list(ctrl)
    if knots[n] <= knots[k - 1]:
        return [ctrl[0], ctrl[-1]]
    return _geom.flatten_bspline(ctrl, knots, tol)


def _bulge_arc(p0: P, p1: P, bulge: float) -> Segment:
    if abs(bulge) < 1e-12 or p0 == p1:
        return Line(p0, p1)
    theta = 4 * math.atan(bulge)
    chord = math.dist(p0, p1)
    r = chord / (2 * math.sin(abs(theta) / 2))
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    h = r * math.cos(abs(theta) / 2)
    dx, dy = (p1[0] - p0[0]) / chord, (p1[1] - p0[1]) / chord
    sgn = 1 if (bulge > 0) == (abs(theta) < math.pi) else -1
    c = (mx - dy * h * sgn, my + dx * h * sgn)
    return Arc(p0, p1, c, ccw=bulge > 0)


def gly_segments(g: ChfGly) -> list[Segment]:
    """Convert one glyph to model segments in contour traversal order."""
    d, k = g.data, g.kind
    segs: list[Segment]
    if k == GLY_POINT:
        segs = [Line(d["p"], d["p"])]
    elif k == GLY_SEGMENT:
        segs = [Line(d["start"], d["end"])]
    elif k == GLY_ARC:
        c, r, a0, a1 = d["center"], d["radius"], d["a0"], d["a1"]
        segs = [Arc(_polar(c, r, a0), _polar(c, r, a1), c, ccw=a1 >= a0)]
    elif k == GLY_CIRCLE:
        c, r = d["center"], d["radius"]
        s = (c[0] + r, c[1])
        segs = [Arc(s, s, c, True)]
    elif k == GLY_ELLIPSE:
        c, ax, ratio, t0, t1 = d["center"], d["axis"], d["ratio"], d["t0"], d["t1"]
        bx, by = -ax[1] * ratio, ax[0] * ratio
        n = max(8, int(abs(t1 - t0) / (math.pi / 32)))
        pts = [(c[0] + ax[0] * math.cos(t) + bx * math.sin(t), c[1] + ax[1] * math.cos(t) + by * math.sin(t))
               for t in (t0 + (t1 - t0) * i / n for i in range(n + 1))]
        segs = [Line(pts[i], pts[i + 1]) for i in range(n)]
    elif k == GLY_LWPOLY:
        vs = d["verts"]
        segs = [_bulge_arc(vs[i][0], vs[i + 1][0], vs[i][1]) for i in range(len(vs) - 1)]
        if d.get("flag") and len(vs) > 1:
            segs.append(_bulge_arc(vs[-1][0], vs[0][0], vs[-1][1]))
    elif k == GLY_SPLINE:
        pts = _bspline_points(d["ctrl"], d["knots"])
        segs = [Line(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    else:
        segs = []
    if g.dir == -1:
        segs = [s.reversed() for s in reversed(segs)]
    return segs


def contour_from_chf(c: ChfContour) -> Contour:
    segs: list[Segment] = []
    for g in c.glys:
        segs.extend(gly_segments(g))
    closed = bool(segs) and math.dist(segs[0].start, segs[-1].end) <= max(c.tol, 1e-9)
    ct = Contour(segments=segs, closed=closed, extra={"chf": c})
    ct.set_source_glyphs([Glyph(g.kind, g.data, g.dir) for g in c.glys])
    return ct


def _seg_bbox(segs: list[Segment]):
    """Exact bbox of Line/Arc segments (arc extremes at the quadrant angles)."""
    return _geom.segments_bbox(segs)


def glys_from_segments(segs: list[Segment]) -> list[ChfGly]:
    out = []
    for s in segs:
        if isinstance(s, Line):
            out.append(ChfGly(1, GLY_SEGMENT, {"start": tuple(s.start), "end": tuple(s.end), "tabs": []}))
            continue
        c, r = tuple(s.center), s.radius
        full = math.dist(s.start, s.end) < 1e-9
        a_s = math.atan2(s.start[1] - c[1], s.start[0] - c[0])
        if full and abs(a_s) < 1e-9:
            out.append(ChfGly(1 if s.ccw else -1, GLY_CIRCLE, {"center": c, "radius": r}))
            continue
        sw = s.sweep()
        if s.ccw:
            out.append(ChfGly(1, GLY_ARC, {"center": c, "radius": r, "a0": a_s, "a1": a_s + sw}))
        else:   # store as a CCW arc end->start and traverse it backwards
            a_e = math.atan2(s.end[1] - c[1], s.end[0] - c[0])
            out.append(ChfGly(-1, GLY_ARC, {"center": c, "radius": r, "a0": a_e, "a1": a_e - sw}))
    return out


def _f(v):
    """Plain Python floats/ints/tuples (no numpy scalars) for the text writer."""
    if isinstance(v, dict):
        return {k: _f(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        t = [_f(x) for x in v]
        return tuple(t) if isinstance(v, tuple) else t
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float) or hasattr(v, "__float__"):
        return float(v)
    return v


def chf_from_contour(ct: Contour, base: Optional[ChfContour] = None) -> ChfContour:
    """Build/refresh a ChfContour from model geometry (keeps non-geometry fields of ``base``).

    When the contour still carries its source glyphs (``Contour.source_glyphs``, e.g. from the
    DXF import or a loaded .chf), those are written, so a polyline stays one lwpoly glyph and a
    spline a spline glyph; otherwise the glyphs are rebuilt from the Line/Arc segments.  Contours
    without a ``base`` record take the closing tolerance and nesting depth the importer stored
    (``extra['close_tol']`` -> +0xd8, ``extra['depth']`` -> +0x88)."""
    c = copy.deepcopy(base) if base is not None else ChfContour()
    src = ct.source_glyphs()
    if src:
        c.glys = [ChfGly(int(g.dir), int(g.kind), _f(copy.deepcopy(g.data))) for g in src]
        for g in c.glys:
            if g.kind == GLY_SEGMENT:
                g.data.setdefault("tabs", [])
        c.length = sum(_geom.glyph_length(g) for g in src)
    else:
        c.glys = glys_from_segments(ct.segments)
        c.length = ct.length()
    if base is None:
        c.tol = float(ct.extra.get("close_tol", c.tol))
        c.flag88 = int(ct.extra.get("depth", c.flag88))
    c.bbox_min, c.bbox_max = _seg_bbox(ct.segments)
    if ct.segments:
        c.start, c.end = _f(tuple(ct.segments[0].start)), _f(tuple(ct.segments[-1].end))
    c.seg_table = []        # sub-segment table no longer matches the new geometry
    return c


# --------------------------------------------------------------------------- craft mapping
def _lead_model(l: ChfLead, corner: bool = False) -> LeadParams:
    return LeadParams(kind=l.kind, angle_deg=l.angle, length=l.length, radius=l.radius, at_corner=corner)


def craft_from_chf(c: ChfContour, layer: int) -> Craft:
    cr = Craft(layer=layer, kerf_comp=c.kerf_side, kerf_width=c.kerf_width,
               lead_in=_lead_model(c.lead_in, c.lead_corner), lead_out=_lead_model(c.lead_out))
    if c.pwm_mode == 1:
        cr.micro_joints = [p * c.length for p, _w in c.pwm_pairs]
    cr.extra = {"micro_joint_widths": [w for _p, w in c.pwm_pairs], "pwm_mode": c.pwm_mode,
                "cool_positions": list(c.cool_pos), "start_param": c.start_param, "overcut": c.overcut}
    return cr


def _apply_craft(c: ChfContour, cr: Craft) -> None:
    c.layer = cr.layer
    c.kerf_side, c.kerf_width = cr.kerf_comp, cr.kerf_width
    li, lo = cr.lead_in, cr.lead_out
    c.lead_in = ChfLead(li.kind, li.angle_deg, li.length, li.radius)
    c.lead_corner = li.at_corner
    c.lead_out = ChfLead(lo.kind, lo.angle_deg, lo.length, lo.radius)
    ex = cr.extra or {}
    widths = ex.get("micro_joint_widths", [])
    if cr.micro_joints or c.pwm_mode == 1:
        L = c.length or 1.0
        default_w = float(ex.get("micro_joint_len", 0.5))
        pairs = []
        for i, m in enumerate(cr.micro_joints):
            # joints are positions (mm) or (position, width) pairs (cam.microjoint / ext_craft)
            if isinstance(m, (tuple, list)):
                pos, w = float(m[0]), float(m[1])
            else:
                pos, w = float(m), (widths[i] if i < len(widths) else default_w)
            pairs.append((pos / L, w))
        c.pwm_pairs = pairs
    if "cool_positions" in ex:
        c.cool_pos = list(ex["cool_positions"])
    c.start_param = ex.get("start_param", c.start_param)
    c.overcut = ex.get("overcut", c.overcut)


def _craft_key(cr: Craft):
    return copy.deepcopy((cr.layer, cr.kerf_comp, cr.kerf_width, cr.lead_in, cr.lead_out,
                          list(cr.micro_joints), cr.cool_point, cr.extra))


# --------------------------------------------------------------------------- document
def _graph_contours(g: ChfGraph) -> list[ChfContour]:
    if isinstance(g, ChfContour):
        return [g]
    if isinstance(g, ChfGroup):
        return list(g.elems)
    if isinstance(g, (ChfScan, ChfContourEx)):
        return list(g.group.elems)
    if isinstance(g, ChfText):
        return list(g.group.elems) if g.group else []
    return []


def _graph_layer(g: ChfGraph) -> int:
    if isinstance(g, (ChfContour, ChfGroup)):
        return g.layer
    if isinstance(g, (ChfScan, ChfContourEx)):
        return g.group.layer
    if isinstance(g, ChfText):
        return g.group.layer if g.group else g.layer
    return 0


def part_from_graph(g: ChfGraph) -> Part:
    cs = _graph_contours(g)
    part = Part(contours=[contour_from_chf(c) for c in cs], name=type(g).__name__[3:].lower())
    if cs:
        part.craft = craft_from_chf(cs[0], _graph_layer(g))
    else:
        part.craft = Craft(layer=_graph_layer(g))
    layer = _graph_layer(g)
    for ct, c in zip(part.contours[1:], cs[1:]):
        # group elements are full CGlyContours with their own <Crafts> block: an element whose
        # craft differs from the first one becomes a per-contour override (cam.manual.contour_craft)
        cr = craft_from_chf(c, c.layer if c.layer != cs[0].layer else layer)
        if _craft_key(cr) != _craft_key(part.craft):
            ct.extra["craft"] = cr
            ct.extra["_chf_craft"] = _craft_key(cr)
    part.extra["chf_type"] = _TYPE_OF[type(g)]
    part.extra["chf"] = g
    part.extra["_chf_snapshot"] = {"craft": _craft_key(part.craft),
                                   "segs": [copy.deepcopy(c.segments) for c in part.contours]}
    return part


def graph_from_part(part: Part) -> ChfGraph:
    """Return the record to write for a part, folding model edits back in."""
    g = part.extra.get("chf")
    snap = part.extra.get("_chf_snapshot")
    if g is None:
        cs = [chf_from_contour(ct, ct.extra.get("chf")) for ct in part.contours]
        for c, ct in zip(cs, part.contours):
            _apply_craft(c, _contour_override(ct) or part.craft)
        if len(cs) == 1:
            return cs[0]
        grp = ChfGroup(elems=cs, layer=part.craft.layer)
        _refresh_group(grp)
        return grp
    g = copy.deepcopy(g)
    cs = _graph_contours(g)
    geom_changed = (snap is None or len(part.contours) != len(cs)
                    or any(ct.segments != s for ct, s in zip(part.contours, snap["segs"])))
    if geom_changed:
        cs = [chf_from_contour(ct, ct.extra.get("chf")) for ct in part.contours]
        _set_graph_contours(g, cs)
        cs = _graph_contours(g)
    part_changed = snap is None or _craft_key(part.craft) != snap["craft"]
    if part_changed:
        _set_graph_layer(g, part.craft.layer)
    for c, ct in zip(cs, part.contours):
        ov = _contour_override(ct) if len(part.contours) > 1 else None
        if ov is not None:
            if part_changed or geom_changed or ct.extra.get("_chf_craft") != _craft_key(ov):
                _apply_craft(c, ov)
        elif part_changed or ct.extra.get("_chf_craft") is not None:
            _apply_craft(c, part.craft)
    return g


def _contour_override(ct: Contour) -> Optional[Craft]:
    """Per-contour craft (``Contour.extra['craft']``, set by cam.manual / ui.docops.apply_lead for
    multi-contour parts).  The .chf stores the crafts of every group element separately
    (CGlyContour <Crafts>, formats.md §2.3/§2.4), so they are written per element."""
    ov = ct.extra.get("craft") if isinstance(ct.extra, dict) else None
    return ov if isinstance(ov, Craft) else None


def _refresh_group(grp: ChfGroup) -> None:
    es = grp.elems
    if not es:
        return
    grp.bbox_min = (min(e.bbox_min[0] for e in es), min(e.bbox_min[1] for e in es))
    grp.bbox_max = (max(e.bbox_max[0] for e in es), max(e.bbox_max[1] for e in es))
    grp.start, grp.end = es[0].start, es[-1].end


def _set_graph_contours(g: ChfGraph, cs: list[ChfContour]) -> None:
    if isinstance(g, ChfContour):
        if cs:
            new = cs[0]
            for k in vars(new):
                setattr(g, k, getattr(new, k))
    elif isinstance(g, ChfGroup):
        g.elems = cs; _refresh_group(g)
    elif isinstance(g, (ChfScan, ChfContourEx)):
        g.group.elems = cs; _refresh_group(g.group)
    elif isinstance(g, ChfText) and g.group is not None:
        g.group.elems = cs; _refresh_group(g.group)


def _set_graph_layer(g: ChfGraph, layer: int) -> None:
    if isinstance(g, (ChfContour, ChfGroup)):
        g.layer = layer
    elif isinstance(g, (ChfScan, ChfContourEx)):
        g.group.layer = layer
    elif isinstance(g, ChfText):
        g.layer = layer
        if g.group:
            g.group.layer = layer


@port("FUN_100f04a0", "FUN_100ee650", source="CADModule", parity=("chf",))
def loads_chf(data: bytes) -> Document:
    r = ScReader(data)
    try:
        version = r.int()                                  # FUN_100ee650 / FUN_100f04a0
    except ScFormatError:
        version = 1                                        # original defaults to 1 on failure
    doc = Document(version=version)
    ex: dict[str, Any] = {}
    r.tag("<Begin Graphs>")                                # FUN_100bcec0
    if _old(version):
        ex["graphs_skipped"] = r.skip(5)
    n = r.int()
    for _ in range(n):
        r.tag()                                            # ####graph NO:i
        gtype = r.int()
        doc.parts.append(part_from_graph(_read_graph(r, gtype, version)))
    r.tag("<End Graphs>")
    if version > 7:                                        # FUN_100bd0c0
        r.tag("<Begin Vision>")
        m = r.int()
        ex["vision_graphs"] = [r.int() for _ in range(m)]
        r.tag("<End Vision>")
    ex["rotated"] = r.bool()
    ex["rotate_angle"] = r.double()
    ex["rotate_center"] = r.point()                        # this+0x9d00
    ex["rotate_offset"] = r.point()                        # this+0x9d10
    r.eof()
    doc.extra["chf"] = ex
    return doc


@port("FUN_100f04a0", source="CADModule", parity=("chf",))
def load_chf(path) -> Document:
    """Load a .chf file into a :class:`Document` (one Part per top-level graph)."""
    return loads_chf(Path(path).read_bytes())


@port("FUN_100ebdd0", source="CADModule", parity=("chf", "import_bytes"))
def dumps_chf(doc: Document, version: int = CURRENT_VERSION) -> bytes:
    if not 1 <= version <= CURRENT_VERSION:
        raise ValueError(f"unsupported .chf version {version}")
    ex = doc.extra.get("chf", {})
    w = ScWriter()
    w.int(version)
    w.str("<Begin Graphs>")                                # FUN_100b1a20
    if _old(version):
        _skip_out(w, ex.get("graphs_skipped", []), 5)
    graphs = [graph_from_part(p) for p in doc.parts]
    w.int(len(graphs))
    for i, g in enumerate(graphs, 1):
        w.tag_int("####graph NO:", i)
        w.int(_TYPE_OF[type(g)])
        _write_graph(w, g, version)
    w.str("<End Graphs>")
    if version > 7:
        vg = ex.get("vision_graphs", [])
        w.str("<Begin Vision>")
        w.int(len(vg))
        for x in vg:
            w.int(x)
        w.str("<End Vision>")
    w.bool(ex.get("rotated", False))
    w.double(ex.get("rotate_angle", 0.0))
    w.point(ex.get("rotate_center", (0.0, 0.0)))
    w.point(ex.get("rotate_offset", (0.0, 0.0)))
    w.eof()
    return w.getvalue()


@port("FUN_100ebdd0", source="CADModule", parity=("chf", "import_bytes"))
def save_chf(doc: Document, path, version: int = CURRENT_VERSION) -> None:
    """Write ``doc`` as a .chf file of the given format version (default 9, as Mlaser writes)."""
    Path(path).write_bytes(dumps_chf(doc, version))
