"""Text to contours (draw.text mf55, CGlyText vtable 0x101211cc).

Observed chain: the view (MainApp FUN_004d1230, tool 13) opens CTextInputDlg (FUN_005df8b0:
text edit with prompt mv20, CBCGPFontComboBox set up for TrueType fonts, height ti0), stores
the font name at app+0xb474 and the height (int) at app+0xb490, then calls
ICADModule+0x84(13, text, &height, font).  CADModule builds a CGlyText (0x100ab590 /
0x100aca20) whose glyphs come from CTextGlyphAdapter::GetGlyph (0x100f51c0):
``CreateFontA(height, 0, 0, 0, 500, ...)`` (0x100e1e30, weight 500) and
``GetGlyphOutlineW(GGO_BEZIER)`` (format 3) - cubic Bezier outlines, advance = gmCellIncX (docs/cam.md §1, FUN_100f51c0).
Default face: gp79 (宋体 / "Segoe UI").

Rebuild: the outlines come from the font file through fontTools (glyph outlines via a pen) and
uharfbuzz (shaping: kerning, ligatures, Arabic joining, Indic reordering, CJK), no Qt and no
QApplication (:mod:`mlaser.cad.shaping`).  Curves are flattened to lines with ``tol``.  Height =
font cell height (usWinAscent + usWinDescent), matching ``CreateFont``'s positive cell-height
convention [I].  Font lookup (:mod:`mlaser.cad.fonts`): an explicit font file (``family`` given as
a path, or ``font_file=``) -> the installed family -> the bundled Noto Sans SC subset; characters
missing in the chosen font come from the bundled fonts (Chinese, Arabic).  Bold / italic use the
family's bold / italic face when installed, else a synthetic one (outline offset, 0.2 slant).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Optional

from mlaser.model import Contour, Line, Point

from . import fonts as _fonts

if TYPE_CHECKING:
    from .shaping import Metrics

DEFAULT_FONT = "SimSun"      # gp79 宋体
DEFAULT_HEIGHT = 10.0        # app+0xb490 (int); no registered default, value INFERRED
FONT_WEIGHT = 500            # CreateFontA weight
SYNTH_SLANT = 0.2            # synthetic italic: x += 0.2 y
SYNTH_BOLD = 1 / 48          # synthetic bold: outline grown by em/48 (FreeType FT_Outline_Embolden)


def text_outlines(text: str, family: Optional[str] = DEFAULT_FONT, tol_em: float = 0.0002,
                  bold: bool = False, italic: bool = False, font_file: Optional[str] = None,
                  features: Optional[dict] = None,
                  ref: Optional["_fonts.FontRef"] = None) -> tuple[list[list[Point]], "Metrics"]:
    """Closed polylines of ``text`` in units of the primary font's cell height (1.0 = one cell),
    first baseline at y = 0 (y up), and the :class:`~mlaser.cad.shaping.Metrics` of the primary
    font.  Lines (``\\n``) step down by the font's line spacing.  ``ref`` (a
    :class:`~mlaser.cad.fonts.FontRef`) bypasses the lookup of ``family`` / ``font_file``."""
    from . import shaping
    if ref is None:
        ref = _fonts.locate(family, bold, italic, font_file)
    synth_i = italic and (ref is None or not _style_has(ref, "italic", "oblique"))
    synth_b = bold and (ref is None or not _style_has(ref, "bold", "black", "heavy"))
    chain = shaping.font_chain(ref)
    m = chain[0].metrics
    cell = max(m.cell, 1e-9)
    polys: list[list[Point]] = []
    y = 0.0
    for line_text in text.split("\n"):
        if line_text:
            line = shaping.shape_line(line_text, chain, features)
            polys += shaping.line_outlines(line, tol_em * cell, y, SYNTH_SLANT if synth_i else 0.0)
        y -= m.line_step
    if synth_b and polys:
        polys = shaping.embolden(polys, SYNTH_BOLD * m.upem)
    return [[(x / cell, yy / cell) for x, yy in p] for p in polys], m


def _style_has(ref: "_fonts.FontRef", *words: str) -> bool:
    try:
        from fontTools.ttLib import TTFont
        nt = TTFont(ref.path, fontNumber=ref.index, lazy=True)["name"]
        sub = ((nt.getDebugName(17) or nt.getDebugName(2) or "")).lower()
    except Exception:   # noqa: BLE001
        return False
    return any(w in sub for w in words)


def text_contours(text: str, family: str = DEFAULT_FONT, height: float = DEFAULT_HEIGHT,
                  origin: Point = (0.0, 0.0), tol: float = 0.02,
                  bold: bool = False, italic: bool = False, *, font_file: Optional[str] = None,
                  features: Optional[dict] = None) -> list[Contour]:
    """Closed contours of ``text`` with the bottom-left of the text bbox at ``origin``.

    Multi-line text (``\\n``) is stacked with the font's line spacing.  ``font_file`` (or a
    ``family`` that is a path) selects a font file directly; ``features`` are extra OpenType
    features for HarfBuzz (``{"liga": False}``, ``{"smcp": True}``)."""
    if not text or height <= 0:
        return []
    polys, _m = text_outlines(text, family, tol / (4.0 * height), bold, italic, font_file, features)
    raw = []
    xs, ys = [], []
    for poly in polys:
        pts = _simplify([(x * height, y * height) for x, y in poly] + [(poly[0][0] * height, poly[0][1] * height)], tol)
        if len(pts) < 3:
            continue
        raw.append(pts)
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    if not raw:
        return []
    out: list[Contour] = []
    dx, dy = origin[0] - min(xs), origin[1] - min(ys)
    for pts in raw:
        q = [(x + dx, y + dy) for x, y in pts]
        if math.dist(q[0], q[-1]) < 1e-9:
            q = q[:-1]
        n = len(q)
        if n < 3:
            continue
        out.append(Contour([Line(q[i], q[(i + 1) % n]) for i in range(n)], True,
                           {"text": text, "font": family, "height": height}))
    return out


def _simplify(pts: list[Point], tol: float) -> list[Point]:
    """Drop consecutive duplicates and nearly collinear points (|deviation| < tol / 4)."""
    out: list[Point] = []
    for p in pts:
        if out and math.dist(out[-1], p) < 1e-9:
            continue
        out.append(p)
    if len(out) < 3:
        return out
    res = [out[0]]
    for i in range(1, len(out) - 1):
        a, b, c = res[-1], out[i], out[i + 1]
        L = math.dist(a, c)
        if L > 1e-12:
            dev = abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / L
            if dev < tol * 0.25:
                continue
        res.append(b)
    res.append(out[-1])
    return res


def available_fonts() -> list[str]:
    """Scalable (TrueType/OpenType) families, like CBCGPFontComboBox::Setup(TRUETYPE_FONTTYPE):
    the installed ones (font folders, fontTools name tables) and the bundled ones."""
    return _fonts.available_families()


def resolve_family(family: Optional[str]) -> str:
    """``family`` if it can be found (font file, installed or bundled), else the first installed
    of SimSun, Segoe UI, DejaVu Sans, Liberation Sans, Arial, else the bundled Noto Sans SC."""
    return _fonts.resolve_family(family)
