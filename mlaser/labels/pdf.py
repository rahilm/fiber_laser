"""A small PDF writer for labels and tickets (standard library only).

Pages are given in millimetres with the origin at the **top left** and y pointing down (the way
a label is laid out); the writer converts to PDF points (y up).  Text uses the two standard
fonts every PDF viewer has, Helvetica and Helvetica-Bold, in WinAnsi encoding (characters
outside it become ``?``).  Content streams are written uncompressed: a label PDF is small, and
the tests (and anyone curious) can read the text in the file.

>>> doc = PdfDocument()
>>> pg = doc.page(62, 29)
>>> pg.text(2, 6, "P3 #1/6", size=9, bold=True)
>>> pg.rect(1, 1, 60, 27)
>>> doc.save("labels.pdf")
"""
from __future__ import annotations

import os
from typing import Iterable, Optional, Sequence

MM = 72.0 / 25.4              # PDF points per millimetre

# Helvetica advance widths (1/1000 em) for ASCII 32..126 (the standard AFM metrics)
_HELV = (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)
_BOLD_FACTOR = 1.07           # Helvetica-Bold is a little wider (approximation for layout)


def winansi(text: str) -> bytes:
    """``text`` in WinAnsi (cp1252); characters it lacks become ``?``."""
    return str(text).encode("windows-1252", errors="replace")


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Approximate width of ``text`` in millimetres at font ``size`` (points)."""
    w = 0
    for ch in str(text):
        o = ord(ch)
        w += _HELV[o - 32] if 32 <= o <= 126 else 556
    return w / 1000.0 * size / MM * (_BOLD_FACTOR if bold else 1.0)


def fit_text(text: str, width_mm: float, size: float, bold: bool = False) -> str:
    """``text`` shortened with ``...`` so it fits ``width_mm``."""
    text = str(text)
    if text_width(text, size, bold) <= width_mm:
        return text
    while text and text_width(text + "...", size, bold) > width_mm:
        text = text[:-1]
    return text + "..." if text else ""


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"\\r") \
        .replace(b"\n", b"\\n")


def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


class PdfPage:
    """One page; every coordinate in mm from the top-left corner."""

    def __init__(self, width_mm: float, height_mm: float) -> None:
        self.width = float(width_mm)
        self.height = float(height_mm)
        self.ops: list[str] = []
        self.texts: list[str] = []              # every text drawn (tests, search)

    # -------------------------------------------------------------- helpers
    def _x(self, x: float) -> str:
        return _num(x * MM)

    def _y(self, y: float) -> str:
        return _num((self.height - y) * MM)

    # -------------------------------------------------------------- drawing
    def text(self, x: float, y: float, text: str, size: float = 9.0, bold: bool = False,
             max_width: Optional[float] = None, align: str = "left") -> float:
        """Text with its baseline at ``y``; returns the width drawn (mm).  ``max_width``
        shortens it with ``...``; ``align`` left / center / right relative to ``x``."""
        s = fit_text(text, max_width, size, bold) if max_width else str(text)
        if not s:
            return 0.0
        w = text_width(s, size, bold)
        if align == "center":
            x -= w / 2
        elif align == "right":
            x -= w
        font = "F2" if bold else "F1"
        raw = _esc(winansi(s)).decode("latin-1")
        self.ops.append(f"BT /{font} {_num(size)} Tf {self._x(x)} {self._y(y)} Td ({raw}) Tj ET")
        self.texts.append(s)
        return w

    def line(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.2) -> None:
        self.ops.append(f"{_num(width * MM)} w {self._x(x0)} {self._y(y0)} m {self._x(x1)} {self._y(y1)} l S")

    def rect(self, x: float, y: float, w: float, h: float, width: float = 0.2, fill: bool = False) -> None:
        """A rectangle with its top-left corner at (x, y); ``fill`` paints it black."""
        r = f"{self._x(x)} {self._y(y + h)} {_num(w * MM)} {_num(h * MM)} re"
        self.ops.append(f"{r} f" if fill else f"{_num(width * MM)} w {r} S")

    def polyline(self, pts: Sequence[tuple[float, float]], closed: bool = False, width: float = 0.2,
                 gray: float = 0.0) -> None:
        if len(pts) < 2:
            return
        path = [f"{self._x(pts[0][0])} {self._y(pts[0][1])} m"]
        path += [f"{self._x(x)} {self._y(y)} l" for x, y in pts[1:]]
        if closed:
            path.append("h")
        g = f"{_num(gray)} G " if gray else ""
        reset = " 0 G" if gray else ""
        self.ops.append(f"{g}{_num(width * MM)} w " + " ".join(path) + " S" + reset)

    def qr(self, x: float, y: float, size: float, matrix: Sequence[Sequence[bool]]) -> float:
        """The QR ``matrix`` (rows of dark modules, quiet zone excluded) as filled squares in a
        ``size`` mm square at (x, y); each row is drawn as runs of dark modules.  Returns the
        module size (mm)."""
        n = len(matrix)
        if not n:
            return 0.0
        cell = size / n
        self.ops.append("% qr")
        for r, row in enumerate(matrix):
            c = 0
            while c < n:
                if row[c]:
                    c0 = c
                    while c < n and row[c]:
                        c += 1
                    self.rect(x + c0 * cell, y + r * cell, (c - c0) * cell, cell, fill=True)
                else:
                    c += 1
        self.ops.append("% /qr")
        return cell

    def content(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("latin-1")


class PdfDocument:
    """Pages, then :meth:`save` / :meth:`to_bytes`."""

    def __init__(self, title: str = "", author: str = "mlaser") -> None:
        self.pages: list[PdfPage] = []
        self.title = title
        self.author = author

    def page(self, width_mm: float, height_mm: float) -> PdfPage:
        pg = PdfPage(width_mm, height_mm)
        self.pages.append(pg)
        return pg

    def to_bytes(self) -> bytes:
        objs: list[bytes] = []                       # object n is objs[n - 1]

        def add(body: bytes) -> int:
            objs.append(body)
            return len(objs)

        catalog = add(b"")                           # filled below
        pages_id = add(b"")
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        kids = []
        pages = self.pages or [PdfPage(210.0, 297.0)]
        for pg in pages:
            data = pg.content()
            cid = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"endstream")
            box = f"[0 0 {_num(pg.width * MM)} {_num(pg.height * MM)}]".encode()
            kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox " % pages_id + box
                            + b" /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
                            % (f1, f2, cid)))
        objs[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        objs[pages_id - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                              + b"] /Count %d >>" % len(kids))
        info = add(b"<< /Producer (mlaser) /Title (" + _esc(winansi(self.title)) + b") /Author ("
                   + _esc(winansi(self.author)) + b") >>")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, catalog, info, xref))
        return bytes(out)

    def save(self, path: str | os.PathLike) -> str:
        data = self.to_bytes()
        tmp = f"{os.fspath(path)}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return os.fspath(path)


def draw_outlines(page: PdfPage, rings: Iterable[Sequence[tuple[float, float]]], box: tuple[float, float, float, float],
                  bounds: Optional[tuple[float, float, float, float]] = None, width: float = 0.15,
                  closed: bool = True) -> float:
    """Draw polylines given in drawing coordinates (y up) scaled into ``box`` = (x, y, w, h) on
    the page (aspect kept, centred).  Returns the scale (page mm per drawing mm)."""
    rings = [list(r) for r in rings if len(r) >= 2]
    if not rings:
        return 0.0
    if bounds is None:
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        bounds = (min(xs), min(ys), max(xs), max(ys))
    bx, by, bw, bh = box
    w = max(1e-9, bounds[2] - bounds[0])
    h = max(1e-9, bounds[3] - bounds[1])
    s = min(bw / w, bh / h)
    ox = bx + (bw - w * s) / 2
    oy = by + (bh - h * s) / 2
    for r in rings:
        pts = [(ox + (x - bounds[0]) * s, oy + (bounds[3] - y) * s) for x, y in r]
        page.polyline(pts, closed=closed, width=width)
    return s


__all__ = ["PdfDocument", "PdfPage", "MM", "text_width", "fit_text", "winansi", "draw_outlines"]
