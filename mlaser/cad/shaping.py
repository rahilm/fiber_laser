"""Shaped text outlines: uharfbuzz (shaping) + fontTools (glyph outlines), no Qt.

HarfBuzz applies the font's OpenType layout: kerning (GPOS ``kern``/``dist``), ligatures,
Arabic joining forms (``init``/``medi``/``fina``), Indic reordering, CJK ``locl``, mark
positioning.  The outlines of the shaped glyphs are drawn with a fontTools pen and flattened to
polylines with a chord tolerance.

Line layout is a reduced Unicode bidi: the paragraph direction is that of the first strong
character, the line is split into runs of one direction (neutral characters take the direction
of their neighbours when both agree, else the paragraph's; digits run left to right) and the
runs are placed in visual order.  Inside a run the characters a font lacks are taken from the
bundled fallback fonts (:mod:`mlaser.cad.fonts`); each (direction, font) run is shaped on its
own.  Vertical CJK layout is not done (``direction="ttb"`` would be the extension point).
"""
from __future__ import annotations

import functools
import math
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

from fontTools.pens.basePen import BasePen

from .fonts import FontRef, bundled_fonts

Point = tuple[float, float]
MAX_PIECES = 256


@dataclass
class Metrics:
    """Vertical metrics of a font in font units (y up, descender negative)."""
    upem: int
    ascent: float           # OS/2 usWinAscent (GDI cell ascent), else hhea
    descent: float          # OS/2 usWinDescent as a positive number, else -hhea.descent
    line_gap: float
    cap_height: float       # OS/2 sCapHeight, else the height of "H", else 0.7 em
    x_height: float

    @property
    def cell(self) -> float:
        """Cell height (ascent + descent): what a positive CreateFont height means."""
        return self.ascent + self.descent

    @property
    def line_step(self) -> float:
        return max(self.cell, self.ascent + self.descent + self.line_gap)


class LoadedFont:
    """A font face opened once for both libraries."""

    def __init__(self, ref: FontRef):
        import uharfbuzz as hb
        from fontTools.ttLib import TTFont
        self.ref = ref
        self.tt = TTFont(ref.path, fontNumber=ref.index, lazy=True)
        self.glyphset = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.cmap = self.tt.getBestCmap() or {}
        blob = hb.Blob.from_file_path(ref.path)
        self.hb_face = hb.Face(blob, ref.index)
        self.hb_font = hb.Font(self.hb_face)
        self.upem = int(self.tt["head"].unitsPerEm)
        self.metrics = self._metrics()

    def _metrics(self) -> Metrics:
        up = self.upem
        hh = self.tt["hhea"] if "hhea" in self.tt else None
        os2 = self.tt["OS/2"] if "OS/2" in self.tt else None
        if os2 is not None and (os2.usWinAscent or os2.usWinDescent):
            asc, desc = float(os2.usWinAscent), float(os2.usWinDescent)
        elif hh is not None:
            asc, desc = float(hh.ascent), float(-hh.descent)
        else:
            asc, desc = 0.8 * up, 0.2 * up
        gap = float(hh.lineGap) if hh is not None else 0.0
        cap = float(getattr(os2, "sCapHeight", 0) or 0) if os2 is not None else 0.0
        xh = float(getattr(os2, "sxHeight", 0) or 0) if os2 is not None else 0.0
        if cap <= 0:
            cap = self._glyph_height("H") or 0.7 * up
        if xh <= 0:
            xh = self._glyph_height("x") or 0.5 * up
        return Metrics(up, asc, desc, gap, cap, xh)

    def _glyph_height(self, ch: str) -> float:
        g = self.cmap.get(ord(ch))
        if g is None:
            return 0.0
        pen = FlattenPen(self.glyphset, 1.0)
        self.glyphset[g].draw(pen)
        ys = [p[1] for c in pen.contours for p in c]
        return max(ys) if ys else 0.0

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def glyph_name(self, gid: int) -> str:
        return self.order[gid] if 0 <= gid < len(self.order) else ".notdef"


@functools.lru_cache(maxsize=32)
def load_font(ref: FontRef) -> LoadedFont:
    return LoadedFont(ref)


class FlattenPen(BasePen):
    """Collects the closed contours of glyph outlines as polylines, curves flattened to the chord
    tolerance ``tol`` (font units), with an affine ``(a, b, c, d, e, f)``:
    x' = a x + b y + e, y' = c x + d y + f."""

    def __init__(self, glyphset, tol: float, xform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)):
        super().__init__(glyphset)
        self.tol = max(tol, 1e-6)
        self.xf = xform
        self.contours: list[list[Point]] = []
        self._cur: list[Point] = []

    def _t(self, p) -> Point:
        a, b, c, d, e, f = self.xf
        return (a * p[0] + b * p[1] + e, c * p[0] + d * p[1] + f)

    def _moveTo(self, pt):
        self._flush()
        self._cur = [self._t(pt)]

    def _lineTo(self, pt):
        self._cur.append(self._t(pt))

    def _curveToOne(self, p1, p2, p3):
        p0 = self._cur[-1]
        q1, q2, q3 = self._t(p1), self._t(p2), self._t(p3)
        m = max(math.hypot(p0[0] - 2 * q1[0] + q2[0], p0[1] - 2 * q1[1] + q2[1]),
                math.hypot(q1[0] - 2 * q2[0] + q3[0], q1[1] - 2 * q2[1] + q3[1]))
        n = max(1, min(MAX_PIECES, math.ceil(math.sqrt(0.75 * m / self.tol))))
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            self._cur.append((u * u * u * p0[0] + 3 * u * u * t * q1[0] + 3 * u * t * t * q2[0] + t * t * t * q3[0],
                              u * u * u * p0[1] + 3 * u * u * t * q1[1] + 3 * u * t * t * q2[1] + t * t * t * q3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self._cur[-1]
        q1, q2 = self._t(p1), self._t(p2)
        m = math.hypot(p0[0] - 2 * q1[0] + q2[0], p0[1] - 2 * q1[1] + q2[1])
        n = max(1, min(MAX_PIECES, math.ceil(math.sqrt(0.25 * m / self.tol))))
        for i in range(1, n + 1):
            t = i / n
            u = 1 - t
            self._cur.append((u * u * p0[0] + 2 * u * t * q1[0] + t * t * q2[0],
                              u * u * p0[1] + 2 * u * t * q1[1] + t * t * q2[1]))

    def _closePath(self):
        self._flush()

    def _endPath(self):
        self._flush()

    def _flush(self):
        pts = self._cur
        self._cur = []
        if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-9:
            pts = pts[:-1]
        if len(pts) >= 3:
            self.contours.append(pts)


# --------------------------------------------------------------------------- layout
@dataclass
class ShapedGlyph:
    font: LoadedFont
    gid: int
    x: float                # pen position in units of the primary font (baseline y = 0)
    y: float
    scale: float            # primary upem / this font's upem
    cluster: int            # index of the first character in the line


@dataclass
class ShapedLine:
    glyphs: list[ShapedGlyph]
    advance: float          # total advance, primary font units


def _strong(ch: str) -> Optional[str]:
    b = unicodedata.bidirectional(ch)
    if b in ("R", "AL", "RLE", "RLO", "RLI"):
        return "rtl"
    if b in ("L", "LRE", "LRO", "LRI", "EN", "AN"):
        return "ltr"
    return None


def _sticky(ch: str) -> bool:
    """Characters that stay in the font run of the previous character (marks, joiners, spaces)."""
    cat = unicodedata.category(ch)
    return cat.startswith("M") or cat in ("Zs", "Cf", "Cc") or ch in "‌‍"


def _directions(text: str) -> tuple[str, list[str]]:
    strong = [_strong(c) for c in text]
    base = next((d for d in strong if d), "ltr")
    out: list[str] = []
    n = len(text)
    for i, d in enumerate(strong):
        if d:
            out.append(d)
            continue
        prev = next((strong[j] for j in range(i - 1, -1, -1) if strong[j]), None) or base
        nxt = next((strong[j] for j in range(i + 1, n) if strong[j]), None) or base
        out.append(prev if prev == nxt else base)
    return base, out


def _font_runs(text: str, start: int, end: int, chain: Sequence[LoadedFont]) -> list[tuple[int, int, LoadedFont]]:
    runs: list[tuple[int, int, LoadedFont]] = []
    cur: Optional[LoadedFont] = None
    s = start
    for i in range(start, end):
        ch = text[i]
        if cur is not None and (_sticky(ch) or cur.has(ch)):
            continue
        f = next((f for f in chain if f.has(ch)), chain[0])
        if cur is None:
            cur = f
            continue
        if f is not cur:
            runs.append((s, i, cur))
            s, cur = i, f
    if cur is not None and end > s:
        runs.append((s, end, cur))
    return runs


def shape_line(text: str, chain: Sequence[LoadedFont], features: Optional[dict] = None) -> ShapedLine:
    """Shape one line of ``text`` with the font ``chain`` (primary first, fallbacks after);
    positions in units of the primary font, pen starting at (0, 0) on the baseline."""
    import uharfbuzz as hb
    prim = chain[0]
    base, dirs = _directions(text)
    druns: list[tuple[int, int, str]] = []
    for i, d in enumerate(dirs):
        if druns and druns[-1][2] == d:
            druns[-1] = (druns[-1][0], i + 1, d)
        else:
            druns.append((i, i + 1, d))
    if base == "rtl":
        druns.reverse()
    glyphs: list[ShapedGlyph] = []
    pen = 0.0
    feats = {"kern": True, "liga": True}
    feats.update(features or {})
    for s, e, d in druns:
        fr = _font_runs(text, s, e, chain)
        if d == "rtl":
            fr.reverse()
        for a, b, f in fr:
            buf = hb.Buffer()
            buf.add_str(text, a, b - a)
            buf.direction = d
            buf.guess_segment_properties()
            hb.shape(f.hb_font, buf, feats)
            k = prim.upem / f.upem
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                glyphs.append(ShapedGlyph(f, info.codepoint, pen + pos.x_offset * k, pos.y_offset * k, k,
                                          info.cluster))
                pen += pos.x_advance * k
    return ShapedLine(glyphs, pen)


def font_chain(ref: Optional[FontRef]) -> list[LoadedFont]:
    """The primary font of ``ref`` (the bundled default when None) followed by the bundled
    fallback fonts that are not the primary."""
    refs = [r for r in bundled_fonts()]
    if ref is None:
        if not refs:
            raise FileNotFoundError("no font found and the bundled fonts are missing")
        ref = refs[0]
    out = [load_font(ref)]
    out += [load_font(r) for r in refs if r.path != ref.path]
    return out


def line_outlines(line: ShapedLine, tol: float, y0: float = 0.0, shear: float = 0.0) -> list[list[Point]]:
    """Closed polylines (primary font units) of a shaped line with its baseline at ``y0``;
    ``shear`` slants x by ``shear * y`` (synthetic italic)."""
    out: list[list[Point]] = []
    for g in line.glyphs:
        k = g.scale
        y = y0 + g.y
        xf = (k, k * shear, 0.0, k, g.x + shear * y, y)
        pen = FlattenPen(g.font.glyphset, tol / k, xf)
        g.font.glyphset[g.font.glyph_name(g.gid)].draw(pen)
        out += pen.contours
    return out


def embolden(polys: list[list[Point]], delta: float) -> list[list[Point]]:
    """Synthetic bold: grow the filled outline by ``delta`` (pyclipper; unchanged when missing)."""
    try:
        import pyclipper
    except ImportError:   # pragma: no cover - pyclipper is a dependency
        return polys
    sc = 1024.0 / max(delta, 1e-9)
    pc = pyclipper.Pyclipper()
    ints = [[(round(x * sc), round(y * sc)) for x, y in p] for p in polys if len(p) >= 3]
    try:
        pc.AddPaths(ints, pyclipper.PT_SUBJECT, True)
        uni = pc.Execute(pyclipper.CT_UNION, pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
        co = pyclipper.PyclipperOffset()
        co.AddPaths(uni, pyclipper.JT_MITER, pyclipper.ET_CLOSEDPOLYGON)
        res = co.Execute(delta * sc)
    except pyclipper.ClipperException:
        return polys
    return [[(x / sc, y / sc) for x, y in p] for p in res]
