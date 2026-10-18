"""Optional "modern" DXF import on ezdxf: what DxfParse drops, read the way CAD programs draw it.

Selected with ``import_dxf(path, mode="modern")`` or the ParityProfile ``"modern-dxf"``
(:mod:`mlaser.parity_ports.profiles.modern_dxf`: ORIGINAL with the ``dxf`` slot's ``collect`` and
``entity_glyphs`` swapped for the functions here).  The DxfParse-faithful default
(:mod:`mlaser.parity_ports.dxfparse.rules`) is unchanged.

The result goes through the same chaining (``chain_glyphs``), contour building, containment
tree, import sort, grouping and placement as the faithful path (:func:`mlaser.io.dxf_import.import_dxf`),
so the Document / Part / Contour shapes are the same: one Part per contour (or nested groups with
``group="nest"``), text as one Part per text entity on layer 10, source glyphs on every contour.

==================  ========================================================================
entity              modern import
==================  ========================================================================
INSERT              nested block references expanded by ezdxf (``Insert.virtual_entities``,
                    as ``ezdxf.disassemble.recursive_decompose`` does), with the full
                    transform: non-uniform scale, mirroring, rotation, OCS; arcs under
                    non-uniform scale become ellipses (flattened).  Entities on layer "0" take
                    the INSERT's layer, BYBLOCK colour the INSERT's colour
MINSERT             every row x column copy (``Insert.multi_insert``)
ATTRIB              text of the INSERT's attributes (invisible ones skipped); ATTDEF skipped
DIMENSION, LEADER,  their block geometry (``recursive_decompose``): lines, arcs, arrowheads
MLEADER, MLINE      (SOLID as closed contours), dimension text; POINTs on layer DEFPOINTS
                    skipped
LINE / ARC /        line / arc / circle glyphs (exact, no small-arc -> line rule); arcs and
CIRCLE              circles with a tilted extrusion are flattened
LWPOLYLINE /        one polyline glyph with bulges (arcs kept)
POLYLINE (2D, 3D)
SPLINE, ELLIPSE,    ``ezdxf.path.make_path`` flattened to within ``flatten_tol`` (one polyline
HELIX               glyph)
HATCH, MPOLYGON     boundary paths as closed cut contours: polyline paths with bulges, edge
                    paths as line / arc glyphs (ellipse and spline edges flattened); a path
                    whose associative source entities exist in the drawing is skipped (the
                    source entity is imported already)
SOLID, TRACE        closed contours (3DFACE, MESH, 3DSOLID, IMAGE, WIPEOUT, VIEWPORT skipped)
TEXT, ATTRIB        outlines from the text style's font: TrueType/OpenType through
                    :mod:`mlaser.cad.text` (fontTools + uharfbuzz: kerning, CJK, Arabic;
                    style font file -> style family -> SimSun/... -> bundled Noto Sans SC);
                    SHX / LFF fonts through ``ezdxf.fonts`` when ezdxf finds the font file.
                    Height = cap height (CAD semantics, not the faithful "outline height");
                    alignment, fit/aligned, width factor, oblique, mirroring and OCS as
                    ``ezdxf.addons.text2path`` places them
MTEXT               ``ezdxf.addons.MTextExplode`` (word wrap, inline formatting) into TEXT
                    lines, each drawn as above; plain lines when that fails
POINT               point glyph (a point graph), as the faithful path
==================  ========================================================================
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, Iterator, Optional

from ezdxf.math import Vec3

from ..geom import curves as geom
from ..model import (GLYPH_ARC, GLYPH_CIRCLE, GLYPH_LINE, GLYPH_LWPOLY, GLYPH_POINT, Contour,
                     Glyph)
from ..parity_ports.dxfparse.rules import _Chain, _contour, _Item

log = logging.getLogger(__name__)

FLATTEN_TOL = 0.01         # max distance of the flattened polyline from SPLINE/ELLIPSE/text curves
SKIP = {"3DFACE", "MESH", "3DSOLID", "BODY", "REGION", "SURFACE", "IMAGE", "WIPEOUT", "VIEWPORT",
        "XLINE", "RAY", "ATTDEF", "TOLERANCE", "UNDERLAY", "PDFUNDERLAY", "DWFUNDERLAY",
        "DGNUNDERLAY", "OLE2FRAME", "ACAD_TABLE"}
TEXT_TYPES = {"TEXT", "ATTRIB", "MTEXT"}
NONPLOT_LAYERS = {"defpoints"}


def _xy(v) -> tuple[float, float]:
    return (float(v[0]), float(v[1]))


def _line(a, b) -> Optional[Glyph]:
    return Glyph(GLYPH_LINE, {"start": a, "end": b, "tabs": []}) if math.dist(a, b) > 1e-9 else None


def _arc(c, r: float, a0: float, a1: float) -> Optional[Glyph]:
    """CCW arc glyph a0 -> a1 (radians, a1 > a0), a0 normalised to (-pi, pi]."""
    sw = a1 - a0
    if sw < 1e-9 or r <= 0:
        return None
    a0 = math.atan2(math.sin(a0), math.cos(a0))
    return Glyph(GLYPH_ARC, {"center": (float(c[0]), float(c[1])), "radius": float(r), "a0": a0, "a1": a0 + sw})


def _poly(pts: list, closed: bool) -> Optional[Glyph]:
    vs: list[tuple[tuple[float, float], float]] = []
    for p in pts:
        q = _xy(p)
        if not vs or math.dist(vs[-1][0], q) > 1e-9:
            vs.append((q, 0.0))
    if closed and len(vs) > 2 and math.dist(vs[0][0], vs[-1][0]) <= 1e-9:
        vs.pop()
    if len(vs) < 2:
        return None
    return Glyph(GLYPH_LWPOLY, {"flag": 1 if closed and len(vs) > 2 else 0, "verts": vs})


def _planar(extrusion) -> int:
    """+1 / -1 for an extrusion along +Z / -Z, 0 for a tilted one."""
    z = Vec3(extrusion).normalize()
    if abs(z.z) > 1 - 1e-9:
        return 1 if z.z > 0 else -1
    return 0


def _ocs_arc(ocs, center, r: float, a0d: float, a1d: float, sign: int) -> Optional[Glyph]:
    """CCW OCS arc (degrees) as a WCS arc glyph; a -Z extrusion mirrors it."""
    c = _xy(ocs.to_wcs(Vec3(center)))
    a0, a1 = math.radians(a0d), math.radians(a1d)
    while a1 <= a0:
        a1 += 2 * math.pi
    if sign < 0:       # x -> -x: the CCW arc a0..a1 becomes the CCW arc pi-a1..pi-a0
        a0, a1 = math.pi - a1, math.pi - a0
    return _arc(c, r, a0, a1)


def _flatten_path(p, tol: float) -> list[Glyph]:
    out = []
    for sp in (p.sub_paths() if p.has_sub_paths else [p]):
        pts = list(sp.flattening(distance=tol, segments=4))
        g = _poly(pts, sp.is_closed or (len(pts) > 2 and pts[0].isclose(pts[-1], abs_tol=1e-9)))
        if g:
            out.append(g)
    return out


def _flatten_entity(e, tol: float) -> list[Glyph]:
    from ezdxf import path
    try:
        return _flatten_path(path.make_path(e), tol)
    except Exception:   # noqa: BLE001
        return []


def _lwpoly_glyph(ocs, vs: list, closed: bool, elev: float, sign: int) -> Optional[Glyph]:
    out = []
    for x, y, b in vs:
        out.append((_xy(ocs.to_wcs(Vec3(x, y, elev))), float(b) * (-1 if sign < 0 else 1)))
    if len(out) < 2:
        return None
    return Glyph(GLYPH_LWPOLY, {"flag": 1 if closed else 0, "verts": out})


def entity_glyphs(e, weights_map: Optional[dict] = None, tol: float = FLATTEN_TOL) -> list[Glyph]:
    """Glyphs of one (non-INSERT, non-text) entity in WCS."""
    t = e.dxftype()
    if t == "LINE":
        g = _line(_xy(e.dxf.start), _xy(e.dxf.end))
        return [g] if g else []
    if t in ("ARC", "CIRCLE"):
        sign = _planar(e.dxf.extrusion)
        if sign == 0:
            return _flatten_entity(e, tol)
        ocs = e.ocs()
        r = float(e.dxf.radius)
        if t == "CIRCLE":
            return [Glyph(GLYPH_CIRCLE, {"center": _xy(ocs.to_wcs(Vec3(e.dxf.center))), "radius": r})] if r > 0 else []
        g = _ocs_arc(ocs, e.dxf.center, r, e.dxf.start_angle, e.dxf.end_angle, sign)
        return [g] if g else []
    if t == "LWPOLYLINE":
        sign = _planar(e.dxf.extrusion)
        if sign == 0:
            return _flatten_entity(e, tol)
        g = _lwpoly_glyph(e.ocs(), list(e.get_points("xyb")), bool(e.closed),
                          float(e.dxf.get("elevation", 0.0)), sign)
        return [g] if g else []
    if t == "POLYLINE":
        if e.is_poly_face_mesh or e.is_polygon_mesh:
            return []
        if e.is_2d_polyline:
            sign = _planar(e.dxf.extrusion)
            if sign == 0:
                return _flatten_entity(e, tol)
            elev = float(Vec3(e.dxf.get("elevation", (0, 0, 0))).z)
            vs = [(float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.get("bulge", 0.0)))
                  for v in e.vertices]
            if any(v.dxf.flags & 1 for v in e.vertices) or e.get_mode() != "AcDb2dPolyline":
                return _flatten_entity(e, tol)      # curve-fit / spline-fit vertices
            g = _lwpoly_glyph(e.ocs(), vs, bool(e.is_closed), elev, sign)
            return [g] if g else []
        g = _poly([v.dxf.location for v in e.vertices], bool(e.is_closed))   # 3D polyline
        return [g] if g else []
    if t in ("SPLINE", "ELLIPSE", "HELIX"):
        return _flatten_entity(e, tol)
    if t in ("SOLID", "TRACE"):
        return _flatten_entity(e, tol)
    if t == "POINT":
        return [Glyph(GLYPH_POINT, {"p": _xy(e.dxf.location)})]
    return []


# --------------------------------------------------------------------------- HATCH
def _associative_sources_exist(e, bp) -> bool:
    if not e.dxf.get("associative", 0):
        return False
    hs = list(getattr(bp, "source_boundary_objects", []) or [])
    db = e.doc.entitydb if e.doc is not None else None
    if not hs or db is None:
        return False
    for h in hs:
        s = db.get(h)
        if s is None or not s.is_alive:
            return False
    return True


def hatch_glyphs(e, tol: float = FLATTEN_TOL) -> list[list[Glyph]]:
    """One glyph list per boundary path of a HATCH / MPOLYGON (WCS)."""
    from ezdxf import path
    from ezdxf.entities.boundary_paths import ArcEdge, EdgePath, LineEdge, PolylinePath
    from ezdxf.math import NULLVEC
    sign = _planar(e.dxf.extrusion)
    ocs = e.ocs()
    elev = float(Vec3(e.dxf.get("elevation", (0, 0, 0))).z)
    off = Vec3(e.dxf.get("offset_vector", NULLVEC)) if e.dxftype() == "MPOLYGON" else Vec3()
    out: list[list[Glyph]] = []
    for bp in e.paths:
        if _associative_sources_exist(e, bp):
            continue
        gl: list[Glyph] = []
        if sign == 0 or off.magnitude > 0:
            try:
                p = path.from_hatch_boundary_path(bp, ocs, elev, offset=off)
                gl = _flatten_path(p, tol)
            except Exception:   # noqa: BLE001
                gl = []
        elif isinstance(bp, PolylinePath):
            g = _lwpoly_glyph(ocs, [(float(x), float(y), float(b)) for x, y, b in bp.vertices],
                              True, elev, sign)
            gl = [g] if g else []
        elif isinstance(bp, EdgePath):
            for ed in bp.edges:
                if isinstance(ed, LineEdge):
                    g = _line(_xy(ocs.to_wcs(Vec3(ed.start).replace(z=elev))),
                              _xy(ocs.to_wcs(Vec3(ed.end).replace(z=elev))))
                elif isinstance(ed, ArcEdge):
                    g = _ocs_arc(ocs, Vec3(ed.center).replace(z=elev), float(ed.radius),
                                 float(ed.start_angle), float(ed.end_angle), sign)
                else:              # ellipse / spline edge: flatten that edge alone
                    one = EdgePath()
                    one.edges = [ed]
                    try:
                        sp = path.from_hatch_edge_path(one, ocs, elev)
                        pts = list(sp.flattening(distance=tol, segments=4))
                        g = _poly(pts, False)
                    except Exception:   # noqa: BLE001
                        g = None
                if g:
                    gl.append(g)
        if gl:
            out.append(gl)
    return out


# --------------------------------------------------------------------------- text
def _font_spec(e) -> tuple[str, str]:
    """(font file name of the text style, family of its extended font data)."""
    name, fam = "", ""
    doc = e.doc
    style = e.dxf.get("style", "Standard")
    if doc is not None:
        try:
            st = doc.styles.get(style)
            name = str(st.dxf.get("font", "") or "")
            try:
                ext = st.get_extended_font_data()
                fam = ext[0] if ext and ext[0] else ""
            except Exception:   # noqa: BLE001
                fam = ""
        except Exception:   # noqa: BLE001
            pass
    return name, fam


def _is_shx(name: str) -> bool:
    n = name.lower()
    return n.endswith((".shx", ".shp", ".lff")) or ("." not in n and n != "")


def _ezdxf_shape_font(name: str):
    """An ezdxf shapefile / LibreCAD font when ezdxf finds the file, else None."""
    try:
        from ezdxf.fonts import fonts
        fn = name if name.lower().endswith((".shx", ".shp", ".lff")) else name + ".shx"
        if not fonts.font_manager.has_font(fn):
            return None
        f = fonts.make_font(fn, cap_height=1.0)
        if not isinstance(f, (fonts.ShapeFileFont, fonts.LibreCadFont)):
            return None
        return f
    except Exception:   # noqa: BLE001
        return None


def _local_text(text: str, e, H: float, tol: float):
    """Closed polylines of one text line at the origin (baseline, left), cap height ``H``, and
    the ezdxf FontMeasurements scaled to drawing units."""
    from ezdxf.fonts.font_measurements import FontMeasurements
    name, fam = _font_spec(e)
    if _is_shx(name):
        f = _ezdxf_shape_font(name)
        if f is not None:
            p = f.text_path_ex(text, cap_height=H).to_path()
            polys = [[_xy(v) for v in sp.flattening(distance=tol, segments=4)]
                     for sp in (p.sub_paths() if p.has_sub_paths else [p])]
            return [q for q in polys if len(q) >= 2], f.measurements.scale_from_baseline(H), False
    from ..cad import fonts as cfonts
    from ..cad.text import text_outlines
    ref = None
    if name and not _is_shx(name):
        ref = cfonts.find_by_filename(name)
    if ref is None and fam:
        ref = cfonts.locate(fam)
    if ref is None:                                  # the DXF TEXT default (SimSun, ... bundled)
        from .dxf_import import _text_family
        ref = cfonts.locate(_text_family())
    polys, m = text_outlines(text, ref=ref, tol_em=1e-4)
    cap = m.cap_height / m.cell                     # cap height in cell units
    k = H / max(cap, 1e-9)
    polys = [[(x * k, y * k) for x, y in q] for q in polys]
    fm = FontMeasurements(0.0, H, m.x_height / m.cell * k, m.descent / m.cell * k)
    return polys, fm, True


def text_entity_contours(e, tol: float = FLATTEN_TOL) -> list[Contour]:
    """Outline contours of a TEXT / ATTRIB entity placed as ezdxf.addons.text2path places it."""
    from ezdxf.addons.text2path import alignment_transformation
    from ezdxf.math import BoundingBox
    text = e.plain_text()
    H = float(e.dxf.get("height", 2.5) or 2.5)
    if not text.strip() or H <= 0:
        return []
    polys, fm, closed = _local_text(text, e, H, tol)
    if not polys:
        return []
    bb = BoundingBox(Vec3(x, y, 0) for q in polys for x, y in q)
    m = alignment_transformation(fm, bb, e.get_align_enum(), e.fit_length())
    m *= e.wcs_transformation_matrix()
    out = []
    for q in polys:
        pts = [_xy(v) for v in m.transform_vertices(Vec3(x, y, 0) for x, y in q)]
        g = _poly(pts, closed)
        if g is None:
            continue
        if closed and not g.data["flag"]:
            continue
        s0 = geom.glyph_endpoints(g)[0]
        c = _contour(_Chain(glyphs=[g], start=s0, end=s0, closed=bool(g.data["flag"])), tol)
        c.extra.update({"text": text, "font": _font_spec(e)[0] or "default"})
        out.append(c)
    return out


def mtext_contours(e, tol: float = FLATTEN_TOL) -> list[Contour]:
    """MTEXT through ezdxf's MTextExplode (into a scratch block of the same drawing)."""
    doc = e.doc
    if doc is not None:
        name = None
        try:
            from ezdxf.addons import MTextExplode
            blk = doc.blocks.new_anonymous_block("U")
            name = blk.name
            with MTextExplode(blk, doc=doc) as xpl:
                xpl.explode(e, destroy=False)
            out: list[Contour] = []
            for t in blk:
                if t.dxftype() == "TEXT":
                    out += text_entity_contours(t, tol)
            return out
        except Exception as ex:   # noqa: BLE001
            log.debug("MTextExplode failed (%s), plain MTEXT lines", ex)
        finally:
            if name is not None:
                try:
                    doc.blocks.delete_block(name, safe=False)
                except Exception:   # noqa: BLE001
                    pass
    return _mtext_plain(e, tol)


def _mtext_plain(e, tol: float) -> list[Contour]:
    """Fallback: each line of the plain text as a left-aligned TEXT line, 5/3 cap height apart."""
    from ezdxf.entities import Text
    H = float(e.dxf.get("char_height", 2.5) or 2.5)
    ucs = e.ucs()
    out: list[Contour] = []
    step = H * 5 / 3 * float(e.dxf.get("line_spacing_factor", 1.0) or 1.0)
    for i, line in enumerate(e.plain_text(split=True)):
        t = Text.new(dxfattribs={"text": line, "height": H, "style": e.dxf.get("style", "Standard"),
                                 "insert": (0.0, -H - i * step, 0.0)}, doc=e.doc)
        for c in text_entity_contours(t, tol):
            pts = [ucs.to_wcs(Vec3(s.start[0], s.start[1], 0)) for s in c.segments]
            g = _poly(pts, True)
            if g:
                s0 = geom.glyph_endpoints(g)[0]
                cc = _contour(_Chain(glyphs=[g], start=s0, end=s0, closed=True), tol)
                cc.extra.update(c.extra)
                out.append(cc)
    return out


# --------------------------------------------------------------------------- traversal
def _walk(entities: Iterable, layer: Optional[str] = None, color: Optional[int] = None,
          depth: int = 0) -> Iterator[tuple]:
    """(entity, layer, colour) of every drawable entity, block references expanded (the
    traversal of ``ezdxf.disassemble.recursive_decompose``, plus layer "0" / BYBLOCK
    inheritance from the INSERT)."""
    from ezdxf.disassemble import recursive_decompose
    from ezdxf.entities import Insert
    from ezdxf.protocols import SupportsVirtualEntities, virtual_entities
    for e in entities:
        try:
            if e.dxf.get("invisible", 0):
                continue
            lay = str(e.dxf.get("layer", "0"))
            if lay == "0" and layer is not None:
                lay = layer
            col = int(e.dxf.get("color", 256))
            if col == 0 and color is not None:
                col = color
            if isinstance(e, Insert):
                if depth > 32:
                    continue
                if e.mcount > 1:
                    yield from _walk(e.multi_insert(), layer, color, depth)
                    continue
                for a in e.attribs:
                    if not a.is_invisible:
                        yield a, lay if str(a.dxf.get("layer", "0")) == "0" else str(a.dxf.layer), col
                if e.block() is None:
                    continue
                yield from _walk(virtual_entities(e), lay, col, depth + 1)
            elif isinstance(e, SupportsVirtualEntities):
                yield from _walk(recursive_decompose([e]), lay, col, depth + 1)  # type: ignore[list-item]
            else:
                yield e, lay, col
        except Exception as ex:   # noqa: BLE001 - one broken entity must not stop the import
            log.debug("modern DXF import: %s skipped (%s)", e, ex)


def collect(layout, weights_map, levels, out: list, stats: dict, depth: int = 0) -> None:
    """DxfRules.collect for the modern import: ``out`` gets one :class:`_Item` per entity (or
    hatch boundary path); text items carry their outline contours in ``text["contours"]``.
    ``stats["options"]`` may hold ``flatten_tol`` and ``text_outlines``."""
    opts = stats.get("options", {}) if isinstance(stats.get("options"), dict) else {}
    tol = float(opts.get("flatten_tol", FLATTEN_TOL) or FLATTEN_TOL)
    with_text = bool(opts.get("text_outlines", True))
    stats.setdefault("hatches", 0)
    for e, lay, col in _walk(layout):
        t = e.dxftype()
        key = (lay, col)
        if t in SKIP:
            if t == "REGION":
                stats["regions"] = stats.get("regions", 0) + 1
            elif t == "3DFACE":
                stats["faces"] = stats.get("faces", 0) + 1
            continue
        try:
            if t in TEXT_TYPES:
                cs = []
                if with_text:
                    cs = mtext_contours(e, tol) if t == "MTEXT" else text_entity_contours(e, tol)
                txt = e.plain_text()
                ins = e.dxf.get("insert", Vec3())
                out.append(_Item([], key, t, {"type": t, "text": txt, "insert": _xy(ins),
                                              "height": float(e.dxf.get("char_height" if t == "MTEXT" else "height", 2.5)),
                                              "rotation": float(e.dxf.get("rotation", 0.0)), "layer": lay,
                                              "contours": cs}))
                continue
            if t in ("HATCH", "MPOLYGON"):
                stats["hatches"] += 1
                for gl in hatch_glyphs(e, tol):
                    out.append(_Item(gl, key, t))
                continue
            if t == "POINT" and lay.lower() in NONPLOT_LAYERS:
                continue
            gl = entity_glyphs(e, weights_map, tol)
        except Exception as ex:   # noqa: BLE001
            log.debug("modern DXF import: %s skipped (%s)", t, ex)
            continue
        if gl:
            out.append(_Item(gl, key, t))
