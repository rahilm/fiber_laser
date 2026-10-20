"""DXF import that reproduces what the original's "import drawing" command builds.

Two stages of the original are modelled (docs/re/cam.md §1):

1. **DxfParseDllvc100.dll** v1.4.93 (``newDxfFileParse`` 0x10004390, entity factory
   FUN_1000a4b0) reads the ENTITIES section in file order and expands INSERTs.
2. **CADModule.dll** turns the parser entities into glyphs (handler table FUN_10008f80), chains
   them into contours (FUN_100b57c0 -> FUN_10061c90), computes the nesting depth
   (FUN_10022270) and moves the result next to the existing drawing (FUN_100ecef0).

=============  ===========================  ==============================================
DXF entity     original                     here
=============  ===========================  ==============================================
LINE           FUN_10003100 segment glyph   line glyph
ARC            FUN_100032e0 arc glyph       arc glyph; an arc under 30 deg whose sagitta
                                            is below 0.01 mm becomes a line (as there)
CIRCLE         FUN_100031f0 circle glyph    circle glyph (starts at angle 0, CCW, also
                                            when mirrored)
LWPOLYLINE /   FUN_100061f0 one lwpoly      one lwpoly glyph with bulges
POLYLINE       glyph
SPLINE/HELIX   FUN_10008740                 cubic spline glyph(s), see :func:`_spline_glyphs`
ELLIPSE        FUN_10003500 -> FUN_10097600 cubic Bezier spline glyphs, one per quadrant
TEXT/MTEXT     FUN_10007bf0                 group of outline contours on layer 10, one
                                            group per text line (TrueType glyf records;
                                            fallback: fontTools + uharfbuzz, bundled font)
POINT          FUN_100062f0 point glyph     point glyph (a point graph)
3DFACE/SOLID/  handler 0 (ignored)          ignored (counted in ``doc.extra['faces']``)
TRACE, XLINE,
RAY
REGION         counted                      ``doc.extra['regions']``
=============  ===========================  ==============================================

Every contour keeps its source glyphs (``Contour.set_source_glyphs``) so that
``formats.chf.save_chf`` writes the same glyph kinds as the original; ``Contour.segments`` is
the flattened Line/Arc geometry the CAM uses.

Parser behaviour copied from DxfParse (O): entities with group 60 (invisible) set are skipped;
paper-space entities (group 67 = 1, the active layout's entities in the ENTITIES section) are
**kept**; SPLINE weights are honoured only when each 41 directly follows its control point's
10/20/30 (FUN_1000e5xx); INSERT scaling of ARC/CIRCLE keeps the radius unless |xscale| ==
|yscale| (FUN_10008c60 / FUN_10008ac0), so arcs stay circular under non-uniform scale; MINSERT
rows/columns are ignored.

After chaining, the original sorts the graphs (FUN_100b57c0: dispatcher 0x1003f010 with
IGP.AutoSortType = 5 "local optimal" and small_first = 1), which reorders them and reverses some
open contours; :func:`mlaser.cam.ordering.import_sort` is a statement-level port
(``sort_mode=0`` keeps document order, holes first).  ELLIPSEs in INSERTs are transformed as
entities before their Bezier cut (FUN_10008e60).  TEXT outlines follow GetGlyphOutlineW
(GGO_BEZIER) records: segment glyphs and one cubic spline glyph per off-curve run
(:func:`_tt_text_contours`).  With these, the .chf written for every non-TEXT parity sample is
byte-identical to MainApp's import + Save As except uninitialised ints (docs/re/parity.md D-12).
"""
from __future__ import annotations

import copy  # noqa: F401 - module namespace kept (the moved rules used it)
import math
from dataclasses import dataclass, field  # noqa: F401
from typing import Optional

import ezdxf
from ezdxf.math import Vec3  # noqa: F401

from ..model import (GLYPH_ARC, GLYPH_CIRCLE, GLYPH_LINE, GLYPH_LWPOLY, GLYPH_POINT, GLYPH_SPLINE,  # noqa: F401
                     Arc, Contour, Craft, Document, Glyph, Line, Part, Segment)
from ..geom import curves as geom
from ..parity_ports import active_profile
# font files without Qt (fontTools name tables); the old private names are kept for callers
from ..cad.fonts import (_FONT_FILES, _FONT_HINTS, find_font_file as _font_file,  # noqa: F401
                         font_dirs as _font_dirs, font_names as _font_names)

# parser and import rules: a port in the parity fence (mlaser.parity_ports.dxfparse.rules)
from ..parity_ports.dxfparse.rules import (  # noqa: F401 - re-exported, used below
    CHAIN_TOL, PLACEMENT, TEXT_LAYER, TEXT_LINE_SPACING, IMPORT_SORT_MODE, ARC_MIN_SWEEP,
    ARC_MIN_SAGITTA, QUAD_TO_CUBIC, STRAIGHT_TOL, UNIFORM_TOL, MIRROR_TOL, SKIP_TYPES,
    FACE_TYPES, _xy, _norm_pi, _arc_glyph, _Level, _mul,
    _insert_levels, _ELLIPSE_SRC, _norm_t0, _expand_ellipses, _xform, _acad_weights,
    _straight, _norm_knots, _spline_piece, _spline_glyphs, _ellipse_glyphs, _ocs_lin,
    _entity_glyphs, _Item, _collect, _near, _Chain, _is_closed_glyph,
    _try_append, _update_closed, _merge, chain_glyphs, _snap, _contour,
    _glyph_bbox, _graph_bbox, reverse_chain_contour, _part_bbox, place_document, _move)
from . import limits as LIM


# --------------------------------------------------------------------------- limits
def _faithful_rules(rules) -> bool:
    from ..parity_ports import ORIGINAL
    return getattr(rules, "collect", None) is getattr(ORIGINAL.dxf, "collect", object())


def check_expansion(layouts, *, max_depth: int = 32, multi_insert: bool = True,
                    limit: int = 0, max_minsert: int = 0) -> int:
    """Count the entities the collectors would visit with every block reference expanded,
    without expanding anything (one pass over each block, memoised per block and depth), and
    raise :class:`~mlaser.io.limits.ImportLimitError` above ``limit``
    (:data:`~mlaser.io.limits.DXF_MAX_ENTITIES`).  ``max_depth``: the collector's INSERT depth
    cut-off (faithful 16, modern 32); ``multi_insert``: MINSERT rows x columns count (modern;
    the faithful import draws one copy).  Returns the count."""
    budget = LIM.entity_budget(limit)
    memo: dict[tuple, int] = {}

    def count(entities, depth: int) -> int:
        n = 0
        for e in entities:
            n += 1
            if e.dxftype() != "INSERT" or e.dxf.get("invisible", 0) or depth > max_depth:
                continue
            try:
                blk = e.block()
            except Exception:   # noqa: BLE001 - the collectors skip it the same way
                blk = None
            if blk is None:
                continue
            copies = 1
            if multi_insert:
                try:
                    copies = max(1, int(e.dxf.get("row_count", 1))) * max(1, int(e.dxf.get("column_count", 1)))
                except (TypeError, ValueError):
                    copies = 1
                LIM.check_minsert(copies, max_minsert)
            key = (str(getattr(blk, "name", id(blk))), depth + 1)
            sub = memo.get(key)
            if sub is None:
                sub = memo[key] = count(blk, depth + 1)
            n += copies * sub
            if n > budget.limit:
                budget.add(n)          # raises
        return n

    total = 0
    for lay in layouts:
        if lay is None:
            continue
        total += count(lay, 0)
        if total > budget.limit:
            budget.add(total)
    LIM.check_cancel()
    return total


class _Items(list):
    """The collector's item list: every append counts against the entity budget (and checks
    for Cancel); otherwise a plain list."""

    def __init__(self, budget: LIM.Budget):
        super().__init__()
        self._budget = budget

    def append(self, item) -> None:      # type: ignore[override]
        self._budget.add()
        super().append(item)


# --------------------------------------------------------------------------- text
#: the original's DXF TEXT faces ("system" text-font mode only, mlaser.cad.fonts.text_font_mode)
TEXT_FAMILIES = ("SimSun", "宋体", "NSimSun", "WenQuanYi Micro Hei")


def _text_family() -> Optional[str]:
    """The font family of DXF TEXT outlines.  Default: the bundled Noto Sans SC subset
    (:func:`mlaser.cad.fonts.default_text_family`), so the outlines do not depend on the fonts
    installed on this machine.  In the "system" text-font mode (``MLASER_TEXT_FONT=system``,
    for comparing with the original): FUN_10007bf0 uses DAT_1011d850 = "宋体" (SimSun), which the
    Wine prefix of the parity harness maps to WenQuanYi Micro Hei; the first of these that is
    installed, else the first installed Draw -> Text fallback (SimSun, Segoe UI, DejaVu Sans,
    Liberation Sans, Arial), else the bundled font.  No Qt."""
    from ..cad import fonts as cfonts
    if cfonts.text_font_mode() == "bundled":
        return cfonts.default_text_family()
    for f in TEXT_FAMILIES:
        if _font_file(f) is not None:
            return f
    from ..cad.text import resolve_family
    return resolve_family(None)


def _text_contours(t: dict, family: str, flatten_tol: float = geom.SPLINE_TOL) -> list[Contour]:
    """Fallback of :func:`_tt_text_contours` when ``family`` has no TrueType (glyf) file: the
    line shaped with uharfbuzz and drawn with fontTools from the family's font file (CFF/OTF
    too), else the bundled font (mlaser.cad.text).  As FUN_10007bf0: scaled so that the outline
    height equals the text height, rotated by the text angle, the font origin (pen start on the
    baseline) on the insertion point.  Line segments only (no GGO_BEZIER records)."""
    from ..cad.text import _simplify, text_outlines
    txt, H = t["text"], t["height"]
    if not txt.strip() or H <= 0:
        return []
    polys, _m = text_outlines(txt, family, tol_em=max(flatten_tol, 1e-4) / (4.0 * H))
    ys = [p[1] for poly in polys for p in poly]
    if not ys:
        return []
    h0 = max(ys) - min(ys)
    if h0 <= 1e-9:
        return []
    k = H / h0
    a = math.radians(t.get("rotation", 0.0))
    ca, sa = math.cos(a), math.sin(a)
    ix, iy = t["insert"]
    out = []
    for poly in polys:
        q = [(ix + (x * ca - y * sa) * k, iy + (x * sa + y * ca) * k) for x, y in poly]
        q = _simplify(q + [q[0]], max(flatten_tol, 1e-4))
        if len(q) > 1 and math.dist(q[0], q[-1]) < 1e-9:
            q = q[:-1]
        n = len(q)
        if n < 3:
            continue
        out.append(Contour([Line(q[i], q[(i + 1) % n]) for i in range(n)], True,
                           {"text": txt, "font": family}))
    return out


# --------------------------------------------------------------------------- text (TrueType)
TEXT_CLOSE_TOL = 0.01      # FUN_100ab710: text contours are made with FUN_1006c2d0(DAT_1011d800 = 0.01)
TEXT_MIN_STEP = 1e-4       # DAT_1011f290: a line piece is kept when |dx| or |dy| >= 1e-4


def _tt_font(family: str, cff: bool = False):
    """The TrueType (glyf) font of ``family`` (bundled file for a bundled family name, else the
    installed one); with ``cff`` an OpenType/CFF font is returned too (cubic outlines)."""
    from ..cad.fonts import text_font_file
    ff = text_font_file(family)
    if ff is None:
        return None
    from fontTools.ttLib import TTFont
    font = TTFont(ff[0], fontNumber=ff[1], lazy=True)
    if "glyf" not in font and not (cff and "CFF " in font):
        return None              # CFF outlines: not what GDI's TrueType path sees
    return font


def _cff_records(glyph, pen_x: float) -> list[tuple]:
    """GGO_BEZIER-style records of an OpenType/CFF glyph (cubic outlines, as GDI returns them
    for a PostScript-flavoured font): [(pfxStart, [(kind, points)])] per contour, kind 1 = a run
    of lines, 3 = a run of cubic pieces (c1, c2, p per piece), shifted by the pen position."""
    from fontTools.pens.recordingPen import RecordingPen
    rp = RecordingPen()
    glyph.draw(rp)
    out: list[tuple] = []
    start = None
    recs: list = []
    cur = None

    def sh(p):
        return (float(p[0]) + pen_x, float(p[1]))

    def add(kind, pts):
        if recs and recs[-1][0] == kind:
            recs[-1][1].extend(pts)
        else:
            recs.append((kind, list(pts)))

    for op, args in rp.value:
        if op == "moveTo":
            start = cur = sh(args[0]); recs = []
        elif op == "lineTo":
            cur = sh(args[0]); add(1, [cur])
        elif op == "curveTo":
            pts = [sh(p) for p in args]
            for i in range(0, len(pts) - 2, 3):
                add(3, pts[i:i + 3])
            cur = pts[-1]
        elif op == "qCurveTo":          # TrueType-style quadratic (rare in CFF): as cubics
            from fontTools.pens.basePen import decomposeQuadraticSegment
            for q, p in decomposeQuadraticSegment(list(args)):
                q, p = sh(q), sh(p)
                add(3, [(cur[0] + 2 / 3 * (q[0] - cur[0]), cur[1] + 2 / 3 * (q[1] - cur[1])),
                        (p[0] + 2 / 3 * (q[0] - p[0]), p[1] + 2 / 3 * (q[1] - p[1])), p])
                cur = p
        elif op in ("closePath", "endPath"):
            if start is not None and recs:
                out.append((start, recs))
            start, recs = None, []
    return out


def _tt_records(pts, on) -> tuple[tuple, list]:
    """One TrueType contour as GetGlyphOutline(GGO_BEZIER) records (Wine
    get_bezier_glyph_outline, the behaviour the parity run sees): (pfxStart, [(kind, points)])
    with kind 1 = TT_PRIM_LINE (on-curve points) and 3 = TT_PRIM_CSPLINE (a run of off-curve
    points; each implied quadratic piece becomes a cubic with control points 2/3 of the way to
    the off-curve point; the run ends on the next on-curve point, or on the start point at the
    contour end)."""
    n = len(pts)
    k0 = next((i for i in range(n) if on[i]), None)
    if k0 is None:                   # all off-curve: start at the first implied point
        start = ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2)
        seq = [(p, False) for p in pts]
    else:
        start = pts[k0]
        seq = [(pts[(k0 + i) % n], on[(k0 + i) % n]) for i in range(1, n)]
    recs: list = []
    cur = start
    i = 0
    m = len(seq)
    while i < m:
        if seq[i][1]:
            run = []
            while i < m and seq[i][1]:
                run.append(seq[i][0]); i += 1
            recs.append((1, run))
            cur = run[-1]
        else:
            offs = []
            while i < m and not seq[i][1]:
                offs.append(seq[i][0]); i += 1
            end = seq[i][0] if i < m else start
            if i < m:
                i += 1
            ctrl = []
            p0 = cur
            for j, q in enumerate(offs):
                p3 = end if j == len(offs) - 1 else ((q[0] + offs[j + 1][0]) / 2, (q[1] + offs[j + 1][1]) / 2)
                ctrl += [(p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1])),
                         (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1])), p3]
                p0 = p3
            recs.append((3, ctrl))
            cur = end
    return start, recs


def _record_glyphs(start, recs) -> list[Glyph]:
    """FUN_100ac060 + FUN_100ab710: every record's point list starts with the current point; a
    closing line record to pfxStart is added.  Line records give one segment glyph per piece
    (pieces shorter than 1e-4 in both axes dropped), a curve record with n = 3P + 1 points one
    cubic spline glyph (u0 = 0, u1 = 3 from the piece record) with knots
    0, (i/P x3 for i = 0..P), 1."""
    out: list[Glyph] = []
    cur = start
    for kind, pts in list(recs) + [(1, [start])]:
        lst = [cur] + list(pts)
        if kind == 1:
            for a, b in zip(lst, lst[1:]):
                if abs(b[0] - a[0]) >= TEXT_MIN_STEP or abs(b[1] - a[1]) >= TEXT_MIN_STEP:
                    out.append(Glyph(GLYPH_LINE, {"start": a, "end": b, "tabs": []}))
        elif len(lst) == 2:
            out.append(Glyph(GLYPH_LINE, {"start": lst[0], "end": lst[1], "tabs": []}))
        else:
            P = (len(lst) - 1) // 3
            knots = [0.0] + [i / P for i in range(P + 1) for _ in range(3)] + [1.0]
            out.append(Glyph(GLYPH_SPLINE, {"u0": 0, "u1": 3, "ctrl": lst, "knots": knots}))
        cur = lst[-1]
    return out


def _tt_text_contours(t: dict, family: str, flatten_tol: float = geom.SPLINE_TOL) -> Optional[list[Contour]]:
    """FUN_10007bf0 / CGlyText (FUN_100aca20 -> FUN_100ac060): the text line's glyph outlines
    as GetGlyphOutlineW(GGO_BEZIER) gives them (FUN_100f51c0 passes format 3): straight runs as
    segment glyphs and each run of off-curve points as ONE cubic spline glyph, as in the
    original's .chf.  Outlines come from the font's glyf table (fontTools), unhinted (the
    original's are hinted by GDI at the CreateFont size: coordinates differ slightly, D-16);
    the pen advances by the unhinted advance width (GDI: gmCellIncX).  Scaled so that the
    outline height equals the text height, rotated, font origin on the insertion point.
    None when no TrueType file of ``family`` is found (the caller falls back to
    :func:`_text_contours`, shaped outlines from fontTools + uharfbuzz)."""
    font = _tt_font(family, cff=True)
    if font is None:
        return None
    txt, H = t["text"], t["height"]
    if not txt.strip() or H <= 0:
        return []
    cmap = font.getBestCmap() or {}
    hmtx = font["hmtx"]
    raw: list[tuple] = []
    pen = 0.0
    if "glyf" not in font:              # OpenType/CFF (the bundled Noto Sans SC): cubic records
        gs = font.getGlyphSet()
        for ch in txt:
            gname = cmap.get(ord(ch)) or ".notdef"
            raw += _cff_records(gs[gname], pen)
            pen += hmtx[gname][0]
        glyf = None
    else:
        glyf = font["glyf"]
    for ch in (txt if glyf is not None else ""):
        gname = cmap.get(ord(ch))
        if gname is None:
            gname = ".notdef"
        g = glyf[gname]
        coords, ends, flags = g.getCoordinates(glyf)
        k = 0
        for e in ends:
            pts = [(float(x) + pen, float(y)) for x, y in coords[k:e + 1]]
            on = [bool(f & 1) for f in flags[k:e + 1]]
            k = e + 1
            if len(pts) >= 2:
                raw.append(_tt_records(pts, on))
        pen += hmtx[gname][0]
    if not raw:
        return []
    cs0 = [_record_glyphs(st, recs) for st, recs in raw]
    ys = [y for gl in cs0 for g in gl for s in geom.glyph_segments(g, flatten_tol) for y in (s.start[1], s.end[1])]
    h0 = (max(ys) - min(ys)) if ys else 0.0
    if h0 <= 1e-9:
        return []
    kk = H / h0
    a = math.radians(t.get("rotation", 0.0))
    ca, sa = math.cos(a), math.sin(a)
    ix, iy = t["insert"]

    def tr(p):
        x, y = p[0] * kk, p[1] * kk
        return (ix + x * ca - y * sa, iy + x * sa + y * ca)

    out = []
    for gl in cs0:
        tg = []
        for g in gl:
            d = dict(g.data)
            if g.kind == GLYPH_LINE:
                d["start"], d["end"] = tr(d["start"]), tr(d["end"])
            else:
                d["ctrl"] = [tr(p) for p in d["ctrl"]]
            tg.append(Glyph(g.kind, d, 1))
        if not tg:
            continue
        s0 = geom.glyph_endpoints(tg[0])[0]
        ch = _Chain(glyphs=tg, start=s0, end=s0, closed=True)
        c = _contour(ch, flatten_tol)
        c.extra.update({"close_tol": TEXT_CLOSE_TOL, "depth": 1, "text": txt, "font": family})
        out.append(c)
    return out


def _sort_text_contours(cs: list[Contour], sort_mode: int, seed: int) -> list[Contour]:
    """FUN_100aca20: the letter contours of a text line go through the same dispatcher, mode 5
    with small_first (0x1003f010(..., 5, 1)), flattened post-order (holes first)."""
    if sort_mode != 5 or len(cs) < 2:
        return cs
    from ..cam.ordering import build_tree, import_sort
    nodes = build_tree(cs, list(range(len(cs))))
    for n in nodes:
        n.bbox = _graph_bbox(n.contour)
    order, _rev = import_sort(nodes, small_first=True, seed=seed)
    return [n.contour for n in order]


# --------------------------------------------------------------------------- main
IMPORT_MODES = ("faithful", "modern")


def dxf_rules(mode: Optional[str] = None):
    """The ``dxf`` slot implementation for ``mode`` (see :func:`import_dxf`)."""
    if mode is None:
        return active_profile().dxf
    if mode in ("faithful", "original"):
        from ..parity_ports import ORIGINAL
        return ORIGINAL.dxf
    if mode == "modern":
        from ..parity_ports.profiles.modern_dxf import MODERN_DXF
        return MODERN_DXF.dxf
    raise ValueError(f"unknown DXF import mode {mode!r} (one of {IMPORT_MODES})")


def import_dxf(path: str, join_tol: float = CHAIN_TOL, flatten_tol: float = geom.SPLINE_TOL,
               group: str = "contour", placement: Optional[tuple] = PLACEMENT,
               text_outlines: bool = True, sort_mode: int = IMPORT_SORT_MODE,
               sort_seed: int = 0, mode: Optional[str] = None) -> Document:
    """Read a DXF file into a Document the way the original's import command does.

    ``group='contour'``: one Part per contour (each graph of the original), text lines as one
    Part (a .chf group) each.  ``group='nest'``: holes are grouped with their enclosing outer
    contour (rebuild extension for nesting).

    ``placement``: lower-left corner of the imported drawing's bounding box (the original puts
    it 20 mm right of the existing drawing, i.e. at (20, 0) for an empty one); ``None`` keeps the
    DXF coordinates.  ``text_outlines=False`` skips the TEXT/MTEXT outlines (they are always
    listed in ``doc.extra['texts']``).

    ``mode``: ``None`` uses the active ParityProfile's ``dxf`` rules (the default profile
    ``"original"`` is the DxfParse-faithful import); ``"faithful"`` forces those;
    ``"modern"`` reads the drawing with ezdxf the way CAD programs draw it (HATCH boundaries,
    DIMENSION/LEADER geometry, ATTRIB and MINSERT, full INSERT transforms, TEXT/MTEXT via
    fontTools + uharfbuzz or SHX fonts; :mod:`mlaser.io.dxf_modern`, profile ``"modern-dxf"``).
    """
    try:
        dxf = ezdxf.readfile(path)
    except IOError:
        raise
    except ezdxf.DXFStructureError:
        from ezdxf import recover
        dxf, _ = recover.readfile(path)
    LIM.check_cancel()
    rules = dxf_rules(mode)            # DxfParse + CADModule import rules (parity fence)
    try:
        psp = dxf.paperspace()        # the active layout's entities are in ENTITIES (group 67 = 1)
    except Exception:   # noqa: BLE001
        psp = None
    # untrusted input: refuse a block-expansion bomb before expanding anything
    # (mlaser.io.limits; the faithful collector cuts INSERTs off below depth 16 and draws one
    # MINSERT copy, the modern one at depth 32 with every copy)
    faithful = _faithful_rules(rules)
    check_expansion([dxf.modelspace(), psp], max_depth=16 if faithful else 32, multi_insert=not faithful)
    weights_map = _acad_weights(path)
    budget = LIM.entity_budget()
    stats: dict = {"faces": 0, "regions": 0, "budget": budget,
                   "options": {"flatten_tol": flatten_tol, "text_outlines": text_outlines}}
    items: list[_Item] = _Items(LIM.entity_budget())   # the collector's output
    rules.collect(dxf.modelspace(), weights_map, [], items, stats)
    if psp is not None:
        rules.collect(psp, weights_map, [], items, stats)
    LIM.check_cancel()

    texts = [it.text for it in items if it.text]
    ready = [t.pop("contours") if "contours" in t else None for t in texts]   # modern: outlines made
    points = [g.data["p"] for it in items for g in it.glyphs if g.kind == GLYPH_POINT]
    chains = rules.chain_glyphs([it for it in items if it.glyphs], join_tol)
    contours = [(ch.key, rules.contour_of_chain(ch, flatten_tol), ch.kind) for ch in chains]
    contours = [c for c in contours if c[1].segments]
    LIM.check_cancel()

    doc = Document()
    doc.extra.update({"source": path, "texts": texts, "points": points, "regions": stats["regions"],
                      "faces": stats["faces"], "layers": sorted({k[0] for k, _c, _t in contours})})
    from ..cam.ordering import build_tree, group_nested, import_sort, order_nodes
    if group == "nest":
        for key, cs in group_nested([(k, c) for k, c, _t in contours]):
            doc.parts.append(Part(contours=cs, craft=Craft(layer=0), name=key[0],
                                  extra={"layer": key[0], "color": key[1]}))
    else:
        nodes = build_tree([c for _k, c, _t in contours], list(range(len(contours))))
        for n in nodes:
            n.contour.extra["depth"] = n.depth          # graph +0x88 (FUN_10022270)
            n.bbox = _graph_bbox(n.contour)               # graph bbox +0x28
        if sort_mode == 5:
            # FUN_100b57c0: the dispatcher 0x1003f010 with IGP.AutoSortType (default 5) and
            # small_first = 1 (MainApp FUN_004d35c0 sets +0x24 = 1)
            order, rev = import_sort(nodes, small_first=True, seed=sort_seed)
        else:
            order, rev = order_nodes(nodes, mode=0), set()   # inner before outer, document order
        for n in order:
            key, c, t = contours[n.key]
            craft = Craft(layer=0)
            if id(n) in rev:
                c = reverse_chain_contour(c)
                # the reverse (0x10073980) also toggles the lead side flag +0x1f8 (.chf: the bool
                # after the lead-in radius, model LeadParams.at_corner)
                craft.lead_in.at_corner = True
            doc.parts.append(Part(contours=[c], craft=craft, name=f"{key[0]}#{n.key}",
                                  extra={"layer": key[0], "color": key[1], "dxf_type": t}))
    if text_outlines and any(r is not None for r in ready):
        for t, cs in zip(texts, ready):
            cs = _sort_text_contours(cs or [], sort_mode, sort_seed)
            if cs:
                doc.parts.append(Part(contours=cs, craft=Craft(layer=TEXT_LAYER), name="text",
                                      extra={"layer": t["layer"], "dxf_type": t["type"],
                                             "text": t["text"]}))
    elif text_outlines and texts:
        fam = _text_family()
        if fam:
            for t in texts:
                try:
                    cs = _tt_text_contours(t, fam, flatten_tol)
                    if cs is None:                  # no TrueType (glyf) file: shaped outline, lines
                        cs = _text_contours(t, fam, flatten_tol)
                    else:
                        cs = _sort_text_contours(cs, sort_mode, sort_seed)
                except Exception:   # noqa: BLE001
                    cs = []
                if cs:
                    doc.parts.append(Part(contours=cs, craft=Craft(layer=TEXT_LAYER), name="text",
                                          extra={"layer": t["layer"], "dxf_type": t["type"],
                                                 "text": t["text"]}))
    if placement is not None and doc.parts:
        place_document(doc, placement)
    return doc
