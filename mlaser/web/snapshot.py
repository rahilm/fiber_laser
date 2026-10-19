"""What the LAN monitor shows: a JSON-safe status dict and an SVG thumbnail of the sheet.

Qt-free.  The application builds the snapshot on its own (GUI) thread and hands it to
:meth:`mlaser.web.server.MonitorServer.publish`; the server threads only read the last one, so
no request ever touches the controller or a widget.

:class:`SheetSvgCache` keeps the flattened outlines and the bounding box of every part between
snapshots (performance review P1-7: rebuilding the SVG of a 30,000-part document took 1.8 s of
GUI time every 500 ms, 85 % of it the bounding box).  The parts are validated by the identity of
their segment objects (edits replace segments, they never change one in place: the
``PartListPanel`` scheme), and the SVG text is rebuilt only when the document, the cut parts,
the current part, the head or the sheet changed.
"""
from __future__ import annotations

import html
import math
import time
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


def _doc_bbox(document: Any, parts: list, part_bbox: Any) -> tuple:
    """``Document.bbox()`` from per-part boxes (the same union, so the same numbers)."""
    if not parts:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        if part_bbox is None:
            return document.bbox()
        bs = [part_bbox(i, p) for i, p in enumerate(parts) if p.contours]
        if not bs:
            return (0, 0, 0, 0)
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
    except Exception:  # noqa: BLE001
        return (0.0, 0.0, 0.0, 0.0)


def sheet_svg(document: Any, done: Iterable[int] = (), current: Optional[int] = None,
              head: Optional[tuple[float, float]] = None, width: int = 480,
              sheet: Optional[tuple[float, float, float, float]] = None) -> str:
    """The parts of ``document`` as an SVG: cut parts (indices in ``done``) grey, the
    ``current`` part highlighted, the others outlined; the head as a dot.  Y points up."""
    body, stroke = _render_body(document, done, current, width, sheet, None, None)
    return _finish(body, head, stroke)


def _finish(body: str, head: Optional[tuple[float, float]], stroke: float) -> str:
    tail = ""
    if head is not None and all(math.isfinite(v) for v in head):
        tail = f'<circle class="head" cx="{head[0]:.2f}" cy="{head[1]:.2f}" r="{stroke * 4:.2f}"/>'
    return body + tail + "</g></svg>"


def _render_body(document: Any, done: Iterable[int], current: Optional[int], width: int,
                 sheet: Optional[tuple[float, float, float, float]], part_bbox: Any,
                 part_points: Any, geo: Optional[list] = None) -> tuple[str, float]:
    """Everything of the SVG but the head and the closing tags; and the stroke width."""
    parts = list(getattr(document, "parts", []) or [])
    if geo:                                   # (document bbox, segment count) kept by the cache
        bb, total = geo
    else:
        bb = _doc_bbox(document, parts, part_bbox)
        total = sum(len(getattr(c, "segments", ())) for p in parts for c in p.contours)
        if geo is not None:
            geo.extend((bb, total))
    if sheet is not None:
        bb = (min(bb[0], sheet[0]), min(bb[1], sheet[1]), max(bb[2], sheet[2]), max(bb[3], sheet[3]))
    x0, y0, x1, y1 = bb
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    pad = 0.03 * max(w, h)
    vx0, vy0, vw, vh = x0 - pad, y0 - pad, w + 2 * pad, h + 2 * pad
    height = max(60, int(round(width * vh / vw)))
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
        if n > MAX_POINTS:
            break                     # nothing more is drawn (the cap): no flattening either
        pieces = part_points(i, p, tol) if part_points is not None else _part_pieces(p, tol)
        for tag, d, npts in pieces:
            if n > MAX_POINTS:
                continue
            n += npts
            out.append(f'<{tag} class="{cls}" points="{d}" stroke-width="{stroke * (2.5 if cls == "cur" else 1):.3f}"/>')
    return "".join(out), stroke


def _part_pieces(p: Any, tol: float) -> list[tuple[str, str, int]]:
    """(tag, points text, point count) per drawable contour of a part at ``tol``."""
    out = []
    for c in p.contours:
        pts = _contour_points(c, tol)
        if len(pts) < 2:
            continue
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        out.append(("polygon" if getattr(c, "closed", False) else "polyline", d, len(pts)))
    return out


MISSING_HEAD = object()


def _segments(p: Any) -> tuple:
    return tuple(s for c in p.contours for s in c.segments)


def _same(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


class SheetSvgCache:
    """:func:`sheet_svg` with the per-part work kept between calls (see the module doc).

    ``render(...)`` returns the same string object as long as nothing it depends on changed (the
    head dot alone only re-joins the text); ``builds`` counts the SVG bodies built."""

    def __init__(self, verify_s: float = 2.0) -> None:
        # with the same document, cut parts and current part, the segment identities are
        # re-checked at most every ``verify_s`` (an edit of the open document shows within it)
        self.verify_s = float(verify_s)
        self._verified = 0.0
        self._doc_ref: Any = None
        self._parts: dict[int, tuple[Any, tuple, dict]] = {}   # index -> (part, segments, data)
        self._key: Any = None
        self._body: Optional[tuple[str, float]] = None
        self._geo: list = []
        self._svg = ""
        self._svg_head: Any = MISSING_HEAD
        self.builds = 0          # SVG bodies built
        self.misses = 0          # parts (re)entered: new or edited since the last call

    def _entry(self, i: int, p: Any) -> dict:
        segs = _segments(p)
        hit = self._parts.get(i)
        if hit is not None and hit[0] is p and _same(hit[1], segs):
            return hit[2]
        self.misses += 1
        data: dict = {}
        self._parts[i] = (p, segs, data)
        return data

    def _data(self, i: int, p: Any) -> dict:
        # validated by the pass at the top of render() (or within verify_s)
        hit = self._parts.get(i)
        return hit[2] if hit is not None and hit[0] is p else self._entry(i, p)

    def _bbox(self, i: int, p: Any) -> tuple:
        data = self._data(i, p)
        if "bbox" not in data:
            data["bbox"] = p.bbox()
        return data["bbox"]

    def _points(self, i: int, p: Any, tol: float) -> list:
        data = self._data(i, p)
        hit = data.get("pts")
        if hit is None or hit[0] != tol:
            hit = data["pts"] = (tol, _part_pieces(p, tol))
        return hit[1]

    def render(self, document: Any, done: Iterable[int] = (), current: Optional[int] = None,
               head: Optional[tuple[float, float]] = None, width: int = 480,
               sheet: Optional[tuple[float, float, float, float]] = None) -> str:
        parts = list(getattr(document, "parts", []) or [])
        if document is not self._doc_ref:
            self._doc_ref, self._parts, self._key, self._body = document, {}, None, None
        done_key = tuple(sorted(set(done)))
        key = (len(parts), done_key, current, width, None if sheet is None else tuple(sheet))
        now = time.monotonic()
        before = self.misses
        if key != self._key or self._body is None or now - self._verified >= self.verify_s:
            self._verified = now
            for i in [k for k in self._parts if k >= len(parts)]:
                del self._parts[i]
            for i, p in enumerate(parts):       # segment identity: ~30 ms at 30,000 parts
                self._entry(i, p)
        if self.misses != before or self._body is None or key[0] != (self._key or (None,))[0]:
            self._geo = []                    # the geometry changed: bbox and counts again
        if self.misses != before or key != self._key or self._body is None:
            self._body = _render_body(document, done_key, current, width, sheet, self._bbox, self._points,
                                      self._geo)
            self._key = key
            self.builds += 1
            self._svg_head = MISSING_HEAD
        hk = None if head is None else tuple(head)
        if hk != self._svg_head:
            self._svg = _finish(self._body[0], head, self._body[1])
            self._svg_head = hk
        return self._svg


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


__all__ = ["sheet_svg", "SheetSvgCache", "esc"]
