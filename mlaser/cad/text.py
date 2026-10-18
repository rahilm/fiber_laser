"""Text to contours (draw.text mf55, CGlyText vtable 0x101211cc).

Observed chain: the view (MainApp FUN_004d1230, tool 13) opens CTextInputDlg (FUN_005df8b0:
text edit with prompt mv20, CBCGPFontComboBox set up for TrueType fonts, height ti0), stores
the font name at app+0xb474 and the height (int) at app+0xb490, then calls
ICADModule+0x84(13, text, &height, font).  CADModule builds a CGlyText (0x100ab590 /
0x100aca20) whose glyphs come from CTextGlyphAdapter::GetGlyph (0x100f51c0):
``CreateFontA(height, 0, 0, 0, 500, ...)`` (0x100e1e30, weight 500) and
``GetGlyphOutlineW(GGO_BEZIER)`` (format 3) - cubic Bezier outlines, advance = gmCellIncX (docs/cam.md §1, FUN_100f51c0).
Default face: gp79 (宋体 / "Segoe UI").

Rebuild: Qt's font engine supplies the same TrueType outlines (``QPainterPath.addText``);
the quadratic/cubic curves are flattened to lines with ``tol``.  Height = font cell height
(ascent + descent), matching ``CreateFont``'s positive cell-height convention [I].
Requires a QGuiApplication (the UI always has one).
"""
from __future__ import annotations

import math
from typing import Optional

from mlaser.model import Contour, Line, Point

DEFAULT_FONT = "SimSun"      # gp79 宋体
DEFAULT_HEIGHT = 10.0        # app+0xb490 (int); no registered default, value INFERRED
FONT_WEIGHT = 500            # CreateFontA weight


def text_contours(text: str, family: str = DEFAULT_FONT, height: float = DEFAULT_HEIGHT,
                  origin: Point = (0.0, 0.0), tol: float = 0.02,
                  bold: bool = False, italic: bool = False) -> list[Contour]:
    """Closed contours of ``text`` with the bottom-left of the text bbox at ``origin``.

    Multi-line text (``\\n``) is stacked with the font's line spacing."""
    from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath, QTransform
    if not text or height <= 0:
        return []
    ref = 100.0                         # build at a fixed size, then scale (hinting-free)
    f = QFont(family)
    f.setPixelSize(int(ref))
    f.setWeight(QFont.Weight(min(1000, max(1, FONT_WEIGHT if not bold else 700))))
    f.setItalic(italic)
    f.setHintingPreference(QFont.PreferNoHinting)
    fm = QFontMetricsF(f)
    path = QPainterPath()
    y = 0.0
    for line_text in text.split("\n"):
        path.addText(0.0, y, f, line_text)
        y += fm.lineSpacing()
    if path.isEmpty():
        return []
    s = height / max(fm.height(), 1e-9)
    tr = QTransform()
    tr.scale(s, -s)                     # Qt is y-down
    polys = path.toSubpathPolygons(tr)
    out: list[Contour] = []
    xs, ys = [], []
    raw = []
    for poly in polys:
        pts = [(poly.at(i).x(), poly.at(i).y()) for i in range(poly.size())]
        pts = _simplify(pts, tol)
        if len(pts) < 3:
            continue
        raw.append(pts)
        xs += [p[0] for p in pts]
        ys += [p[1] for p in pts]
    if not raw:
        return []
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
    """Scalable (TrueType/OpenType) families, like CBCGPFontComboBox::Setup(TRUETYPE_FONTTYPE)."""
    from PySide6.QtGui import QFontDatabase
    return [f for f in QFontDatabase.families() if QFontDatabase.isScalable(f)]


def resolve_family(family: Optional[str]) -> str:
    """Use ``family`` if installed, else the first scalable family."""
    try:
        fams = available_fonts()
    except Exception:  # noqa: BLE001
        return family or DEFAULT_FONT
    if family and family in fams:
        return family
    for f in ("SimSun", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Arial"):
        if f in fams:
            return f
    return fams[0] if fams else (family or DEFAULT_FONT)
