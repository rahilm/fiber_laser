"""QR code (draw.qr A260420_0, cmd 0x3502, MainApp FUN_004b67d0).

Observed: CQRCodeCreateDlg (FUN_00423040 / OnInitDialog FUN_00423600, title A260420_1 "QR code
create") edits ``QR.QRCodeNote`` (text, app+0x47d4), ``QR.QRCodeSize`` (A260420_2, mm,
default 25, range 10..100, app+0x47c8) and ``QR.QRCodeLineDensity`` (A260420_4, int, default
12, range 3..30, app+0x47d0).  On OK the code is rendered as a *scan-line engraving* G-code
file ``<dir>\\QR_Code_Transform.nc`` with line spacing ``1/density`` mm, which is then imported
through the scan G-code importer (A251104_0, dialog FUN_004310c0, worker mode 2) - i.e. the
QR code is engraved with horizontal fill lines, not cut as squares.  Empty text -> A260420_5,
failure -> A260420_6.

Rebuild: ``segno`` encodes the text; :func:`qr_scan_lines` produces the same kind of
horizontal scan lines (one open contour per dark run per scan line, alternating direction);
:func:`qr_squares` gives closed outlines of the dark modules (rebuild option).
"""
from __future__ import annotations

from typing import Optional

from mlaser.model import Contour, Line, Point

DEFAULT_SIZE = 25.0
DEFAULT_DENSITY = 12


def qr_matrix(text: str, error: str = "m", border: int = 0) -> list[list[bool]]:
    import segno
    q = segno.make(text, error=error, micro=False)
    return [[bool(v) for v in row] for row in q.matrix_iter(scale=1, border=border)]


def qr_scan_lines(text: str, size: float = DEFAULT_SIZE, density: int = DEFAULT_DENSITY,
                  origin: Point = (0.0, 0.0)) -> list[Contour]:
    """Scan lines ``1/density`` mm apart over a ``size`` x ``size`` square (bottom-left at
    ``origin``); serpentine direction (bottom to top, GRP.GCodeScanDirection 0)."""
    if not text:
        return []
    m = qr_matrix(text)
    n = len(m)
    cell = size / n
    gap = 1.0 / max(1, int(density))
    out: list[Contour] = []
    y = gap / 2
    k = 0
    while y < size:
        row = n - 1 - min(n - 1, int(y / cell))         # matrix row 0 is the top
        runs = []
        x = 0
        while x < n:
            if m[row][x]:
                x0 = x
                while x < n and m[row][x]:
                    x += 1
                runs.append((x0 * cell, x * cell))
            else:
                x += 1
        if k % 2:
            runs = [(b, a) for a, b in reversed(runs)]
        for a, b in runs:
            out.append(Contour([Line((origin[0] + a, origin[1] + y), (origin[0] + b, origin[1] + y))], False,
                               {"qr": True}))
        y += gap
        k += 1
    return out


def qr_squares(text: str, size: float = DEFAULT_SIZE, origin: Point = (0.0, 0.0)) -> list[Contour]:
    """Closed outlines of the dark regions (union of modules), rebuild option."""
    if not text:
        return []
    from shapely.geometry import box
    from shapely.ops import unary_union
    m = qr_matrix(text)
    n = len(m)
    cell = size / n
    boxes = [box(origin[0] + x * cell, origin[1] + (n - 1 - r) * cell,
                 origin[0] + (x + 1) * cell, origin[1] + (n - r) * cell)
             for r in range(n) for x in range(n) if m[r][x]]
    u = unary_union(boxes)
    polys = list(getattr(u, "geoms", [u]))
    out = []
    for p in polys:
        for ring in [p.exterior] + list(p.interiors):
            pts = list(ring.coords)[:-1]
            if len(pts) >= 3:
                k = len(pts)
                out.append(Contour([Line(tuple(pts[i]), tuple(pts[(i + 1) % k])) for i in range(k)], True,
                                   {"qr": True}))
    return out


def qr_contours(text: str, size: float = DEFAULT_SIZE, density: int = DEFAULT_DENSITY,
                origin: Point = (0.0, 0.0), mode: str = "scan") -> Optional[list[Contour]]:
    if mode == "squares":
        return qr_squares(text, size, origin)
    return qr_scan_lines(text, size, density, origin)
