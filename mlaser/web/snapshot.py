"""What the LAN monitor shows: a JSON-safe status dict and an SVG thumbnail of the sheet.

Qt-free.  The application builds the snapshot on its own (GUI) thread and hands it to
:meth:`mlaser.web.server.MonitorServer.publish`; the server threads only read the last one, so
no request ever touches the controller or a widget.
"""
from __future__ import annotations

import html
import math
from typing import Any, Iterable, Optional

MAX_POINTS = 20000          # cap for the thumbnail (a phone renders it)
# colour + line weight (the current part is also drawn thicker: not colour alone)
STYLE = (".sheet{fill:#eceff1;stroke:#90a4ae}.done{stroke:#9e9e9e}.todo{stroke:#1565c0}"
         ".cur{stroke:#e65100}.head{fill:#d50000;stroke:none}")


def _contour_points(c: Any, tol: float) -> list[tuple[float, float]]:
    try:
        return [(float(x), float(y)) for x, y in c.points(tol)]
    except Exception:  # noqa: BLE001 - a bad contour is left out of the picture
        return []


def sheet_svg(document: Any, done: Iterable[int] = (), current: Optional[int] = None,
              head: Optional[tuple[float, float]] = None, width: int = 480,
              sheet: Optional[tuple[float, float, float, float]] = None) -> str:
    """The parts of ``document`` as an SVG: cut parts (indices in ``done``) grey, the
    ``current`` part highlighted, the others outlined; the head as a dot.  Y points up."""
    parts = list(getattr(document, "parts", []) or [])
    try:
        bb = document.bbox() if parts else (0.0, 0.0, 0.0, 0.0)
    except Exception:  # noqa: BLE001
        bb = (0.0, 0.0, 0.0, 0.0)
    if sheet is not None:
        bb = (min(bb[0], sheet[0]), min(bb[1], sheet[1]), max(bb[2], sheet[2]), max(bb[3], sheet[3]))
    x0, y0, x1, y1 = bb
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    pad = 0.03 * max(w, h)
    vx0, vy0, vw, vh = x0 - pad, y0 - pad, w + 2 * pad, h + 2 * pad
    height = max(60, int(round(width * vh / vw)))
    total = sum(len(getattr(c, "segments", ())) for p in parts for c in p.contours)
    tol = 0.5 if total < 2000 else max(0.5, max(w, h) / 500.0)
    done = set(done)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw:.2f} {vh:.2f}" '
           f'width="{width}" height="{height}" role="img" aria-label="sheet">',
           f"<style>{STYLE}</style>",
           f'<g transform="matrix(1 0 0 -1 {-vx0:.3f} {vy0 + vh:.3f})" fill="none" '
           f'stroke-linejoin="round">']
    if sheet is not None:
        sx0, sy0, sx1, sy1 = sheet
        out.append(f'<rect x="{sx0:.2f}" y="{sy0:.2f}" width="{sx1 - sx0:.2f}" height="{sy1 - sy0:.2f}" '
                   f'class="sheet"/>')
    n = 0
    stroke = max(w, h) / 400.0
    for i, p in enumerate(parts):
        cls = "done" if i in done else ("cur" if i == current else "todo")
        for c in p.contours:
            pts = _contour_points(c, tol)
            if len(pts) < 2 or n > MAX_POINTS:
                continue
            n += len(pts)
            d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
            tag = "polygon" if getattr(c, "closed", False) else "polyline"
            out.append(f'<{tag} class="{cls}" points="{d}" stroke-width="{stroke * (2.5 if cls == "cur" else 1):.3f}"/>')
    if head is not None and all(math.isfinite(v) for v in head):
        out.append(f'<circle class="head" cx="{head[0]:.2f}" cy="{head[1]:.2f}" r="{stroke * 4:.2f}"/>')
    out.append("</g></svg>")
    return "".join(out)


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


__all__ = ["sheet_svg", "esc"]
