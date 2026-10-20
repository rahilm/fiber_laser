"""Part labels, QR job tickets and remnant labels as PDF (Qt-free; review 2 features P2-4).

* **Part labels**: one label per placed part copy, with the part id and name, the order /
  customer reference, material and thickness, the sheet or queue entry, and a QR code.
* **Job tickets**: one page per queued sheet (A4 or A5) with the queue entry, material, sheet,
  quantity, the parts list, a vector preview of the sheet and a QR code of the queue entry.
* **Remnant labels**: id, size, material, thickness, rack location and source, QR code.

QR payloads (plain text, so a USB barcode scanner that types like a keyboard reproduces them):

=========================================  ===========================================
``mlaser:part:<id>;n=<copy>;o=<order>;q=<queue entry>``   a part label (empty fields left out; values URL-quoted)
``mlaser:queue:<entry id>``                 a job ticket (the queue entry)
``mlaser:remnant:<remnant id>``             a remnant label
=========================================  ===========================================

Order data (merge point with the order intake): a part's order fields are read from
``Part.extra["order"]`` (a dict) and, for nested parts, from ``Part.extra["nest"]`` (placement)
and the caller's ``order_info`` (by part name: the nest part's ``extra``).  Keys:
``order``, ``customer``, ``reference``, ``part_number``, ``due``.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote

from .pdf import PdfDocument, PdfPage, draw_outlines

ORDER_KEYS = ("order", "customer", "reference", "part_number", "due")


# ============================================================================== sizes
@dataclass(frozen=True)
class LabelSize:
    """A label size and how labels sit on the page.  A roll label (``cols == rows == 1``,
    no page size) is one label per page."""

    key: str
    name: str
    width: float                     # label, mm
    height: float
    page: Optional[tuple[float, float]] = None   # sheet labels: the page (mm)
    cols: int = 1
    rows: int = 1
    left: float = 0.0                # first label's top-left corner on the page
    top: float = 0.0
    pitch_x: float = 0.0             # label to label (0 = the label size)
    pitch_y: float = 0.0

    @property
    def per_page(self) -> int:
        return self.cols * self.rows

    @property
    def page_size(self) -> tuple[float, float]:
        return self.page or (self.width, self.height)

    def origin(self, i: int) -> tuple[float, float]:
        """Top-left corner of label ``i`` (0-based, on its page)."""
        k = i % self.per_page
        r, c = divmod(k, self.cols)
        return (self.left + c * (self.pitch_x or self.width), self.top + r * (self.pitch_y or self.height))


A4 = (210.0, 297.0)
A5 = (148.0, 210.0)
SIZES: tuple[LabelSize, ...] = (
    LabelSize("62x29", "62 x 29 mm (roll, Brother DK-11209)", 62.0, 29.0),
    LabelSize("62x100", "62 x 100 mm (roll, Brother DK-11202)", 62.0, 100.0),
    LabelSize("57x32", "57 x 32 mm (roll)", 57.0, 32.0),
    LabelSize("100x50", "100 x 50 mm (roll)", 100.0, 50.0),
    LabelSize("102x152", "102 x 152 mm (4 x 6 in, roll)", 102.0, 152.0),
    LabelSize("a4_3x8", "A4 sheet, 3 x 8 labels 70 x 37 mm", 70.0, 37.0, A4, 3, 8, 0.0, 0.5),
    LabelSize("a4_l7163", "A4 sheet, 2 x 7 labels 99.1 x 38.1 mm (L7163)", 99.1, 38.1, A4, 2, 7, 4.65, 15.15,
              101.6, 38.1),
    LabelSize("a4_l7160", "A4 sheet, 3 x 7 labels 63.5 x 38.1 mm (L7160)", 63.5, 38.1, A4, 3, 7, 7.21, 15.15,
              66.04, 38.1),
)
DEFAULT_SIZE = "62x29"


def label_size(key: str) -> LabelSize:
    for s in SIZES:
        if s.key == key:
            return s
    raise KeyError(f"unknown label size {key!r} (one of {', '.join(s.key for s in SIZES)})")


# ============================================================================== data
@dataclass
class PartLabel:
    part_id: str                     # the part type (nest part id, or the part's index)
    name: str = ""
    copy: int = 1                    # this copy, 1-based
    copies: int = 1                  # copies of this part type on the sheet
    order: str = ""
    customer: str = ""
    reference: str = ""
    part_number: str = ""
    due: str = ""
    material: str = ""
    thickness_mm: float = 0.0
    sheet: str = ""                  # sheet size / remnant id
    queue_id: str = ""
    size_mm: tuple[float, float] = (0.0, 0.0)

    @property
    def qr_payload(self) -> str:
        bits = [f"mlaser:part:{quote(str(self.part_id), safe='-_.')}", f"n={self.copy}"]
        if self.order:
            bits.append("o=" + quote(self.order, safe="-_."))
        if self.queue_id:
            bits.append("q=" + quote(self.queue_id, safe="-_."))
        return ";".join(bits)


@dataclass
class TicketData:
    entry_id: str
    name: str = ""
    material: str = ""
    thickness_mm: float = 0.0
    sheet: str = ""                  # "3000 x 1500 mm" or a remnant id
    remnant_id: str = ""
    quantity: int = 1
    done: int = 0
    est_s: float = 0.0
    note: str = ""
    path: str = ""
    parts: list[tuple[str, int]] = field(default_factory=list)          # (name, count)
    preview: list[list[tuple[float, float]]] = field(default_factory=list)   # outlines (y up)
    sheet_bounds: Optional[tuple[float, float, float, float]] = None

    @property
    def qr_payload(self) -> str:
        return f"mlaser:queue:{self.entry_id}"


@dataclass
class RemnantLabel:
    id: str
    width_mm: float = 0.0
    height_mm: float = 0.0
    material: str = ""
    thickness_mm: float = 0.0
    location: str = ""
    source: str = ""
    created: float = 0.0
    area_mm2: float = 0.0
    rings: list[list[tuple[float, float]]] = field(default_factory=list)

    @property
    def qr_payload(self) -> str:
        return f"mlaser:remnant:{self.id}"


def _mat(material: str, thickness: float) -> str:
    return " ".join(x for x in (material, f"{thickness:g} mm" if thickness else "") if x)


def _order_fields(part: Any, order_info: Optional[Mapping[str, Mapping]]) -> dict:
    out: dict = {}
    extra = getattr(part, "extra", None) or {}
    for src in ((order_info or {}).get(getattr(part, "name", "") or "", {}), extra.get("nest") or {},
                extra.get("order") or {}):
        if isinstance(src, Mapping):
            for k in ORDER_KEYS:
                v = src.get(k)
                if v not in (None, ""):
                    out[k] = str(v)
    return out


def _is_sheet(part: Any) -> bool:
    extra = getattr(part, "extra", None) or {}
    return "nest_sheet" in extra or getattr(getattr(part, "craft", None), "layer", 1) == 0


def part_labels(document: Any, material: str = "", thickness_mm: float = 0.0, sheet: str = "",
                queue_id: str = "", order_info: Optional[Mapping[str, Mapping]] = None) -> list[PartLabel]:
    """One :class:`PartLabel` per placed part copy of ``document``.  Nested parts are grouped by
    placement (a nest part made of several drawing parts is one copy); a drawing without nest
    data gets one label per part.  The sheet outline (layer 0) gets none."""
    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for pi, p in enumerate(getattr(document, "parts", []) or []):
        if not getattr(p, "contours", None) or _is_sheet(p):
            continue
        nest = (getattr(p, "extra", None) or {}).get("nest")
        if isinstance(nest, Mapping) and "part_id" in nest:
            key: tuple = ("n", nest.get("sheet_id"), nest.get("index"))
            pid = str(nest.get("part_id"))
        else:
            key, pid = ("p", pi), str(pi + 1)
        if key not in groups:
            groups[key] = [pid, p, []]
            order.append(key)
        groups[key][2].append(p)
    counts: dict[str, int] = {}
    for key in order:
        counts[groups[key][0]] = counts.get(groups[key][0], 0) + 1
    seen: dict[str, int] = {}
    out = []
    for key in order:
        pid, p, members = groups[key]
        seen[pid] = seen.get(pid, 0) + 1
        bbs = [m.bbox() for m in members]
        w = max(b[2] for b in bbs) - min(b[0] for b in bbs)
        h = max(b[3] for b in bbs) - min(b[1] for b in bbs)
        of = _order_fields(p, order_info)
        out.append(PartLabel(pid, getattr(p, "name", "") or f"part {pid}", seen[pid], counts[pid],
                             of.get("order", ""), of.get("customer", ""), of.get("reference", ""),
                             of.get("part_number", ""), of.get("due", ""), material, float(thickness_mm or 0.0),
                             sheet, queue_id, (round(w, 1), round(h, 1))))
    return out


def ticket_from_entry(entry: Any, document: Any = None) -> TicketData:
    """A :class:`TicketData` from a queue entry (``mlaser.services.queue.QueueEntry``, duck
    typed) and optionally its loaded document (parts list and preview)."""
    extra = getattr(entry, "extra", None) or {}
    t = TicketData(str(getattr(entry, "id", "")), str(getattr(entry, "name", "")), str(getattr(entry, "material", "")),
                   float(getattr(entry, "thickness_mm", 0.0) or 0.0), str(getattr(entry, "sheet", "")),
                   str(getattr(entry, "remnant_id", "")), int(getattr(entry, "quantity", 1) or 1),
                   int(getattr(entry, "done", 0) or 0), float(getattr(entry, "est_s", 0.0) or 0.0),
                   str(getattr(entry, "note", "")), str(getattr(entry, "path", "")))
    sb = extra.get("sheet_bounds")
    if sb and len(sb) == 4:
        t.sheet_bounds = tuple(float(v) for v in sb)        # type: ignore[assignment]
    if document is not None:
        counts: dict[str, int] = {}
        for lab in part_labels(document):
            counts[lab.name] = counts.get(lab.name, 0) + 1
        t.parts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        rings = []
        for p in getattr(document, "parts", []) or []:
            for c in getattr(p, "contours", []) or []:
                pts = list(c.points(0.2))
                if len(pts) >= 2:
                    rings.append(pts)
        t.preview = rings
    return t


def remnant_label(rem: Any) -> RemnantLabel:
    """A :class:`RemnantLabel` from a remnant (``mlaser.services.remnants.Remnant``, duck typed)."""
    size = getattr(rem, "size", (0.0, 0.0))
    try:
        area = float(getattr(rem, "area_mm2", 0.0))
    except Exception:  # noqa: BLE001 - a remnant without a polygon
        area = 0.0
    rings = [[(float(x), float(y)) for x, y in r] for r in (getattr(rem, "rings", None) or [])]
    return RemnantLabel(str(getattr(rem, "id", "")), float(size[0]), float(size[1]), str(getattr(rem, "material", "")),
                        float(getattr(rem, "thickness_mm", 0.0) or 0.0), str(getattr(rem, "location", "")),
                        str(getattr(rem, "source", "")), float(getattr(rem, "created", 0.0) or 0.0), area, rings)


def qr_matrix(payload: str) -> list[list[bool]]:
    """The QR matrix (quiet zone excluded) segno makes for ``payload``."""
    import segno
    q = segno.make(payload, error="m", micro=False)
    return [[bool(v) for v in row] for row in q.matrix_iter(scale=1, border=0)]


# ============================================================================== rendering
def _label_frame(pg: PdfPage, x: float, y: float, s: LabelSize, frame: bool) -> None:
    if frame:
        pg.rect(x + 0.3, y + 0.3, s.width - 0.6, s.height - 0.6, width=0.1)


def _lines(pg: PdfPage, x: float, y: float, w: float, h: float, rows: Sequence[tuple[str, bool]],
           max_size: float, min_size: float = 6.0) -> None:
    """Text rows in the box (bold rows a little larger); the font shrinks so the rows fit the
    height and, down to ``min_size``, the width (longer rows are shortened with ``...``)."""
    from .pdf import text_width
    rows = [r for r in rows if r[0]]
    if not rows:
        return
    lines = sum(1.15 if b else 1.0 for _t, b in rows)
    size = min(max_size, h / lines / 0.40)        # pt; about 0.40 mm of line height per point
    for text, bold in rows[:2]:                   # the id and the name should be readable whole
        while size > min_size and text_width(text, size * (1.15 if bold else 1.0), bold) > w:
            size -= 0.25
    size = max(min_size, size)
    lh = size * 0.40
    yy = y + lh * 0.85
    for text, bold in rows:
        if yy > y + h + 0.1:
            break
        pg.text(x, yy, text, size=size * (1.15 if bold else 1.0), bold=bold, max_width=w)
        yy += lh * (1.15 if bold else 1.0)


def _layout(x: float, y: float, s: LabelSize) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    """(QR x, y, size) and the text box (x, y, w, h) of a label at (x, y): QR on the right of a
    wide label, below the text on a tall one."""
    pad = max(1.5, min(s.width, s.height) * 0.06)
    if s.height > s.width * 0.9:
        q = min(s.width - 2 * pad, s.height * 0.45)
        return ((x + (s.width - q) / 2, y + s.height - pad - q, q),
                (x + pad, y + pad, s.width - 2 * pad, s.height - q - 3 * pad))
    q = min(s.height - 2 * pad, s.width * 0.36)
    return ((x + s.width - pad - q, y + (s.height - q) / 2, q), (x + pad, y + pad, s.width - q - 3 * pad,
                                                                 s.height - 2 * pad))


def render_part_labels(path: str, labels: Sequence[PartLabel], size: str | LabelSize = DEFAULT_SIZE,
                       frame: bool = True, title: str = "Part labels") -> str:
    """Write ``labels`` to ``path`` as PDF on label size ``size``; returns the path."""
    s = size if isinstance(size, LabelSize) else label_size(size)
    doc = PdfDocument(title)
    pg: Optional[PdfPage] = None
    for i, lab in enumerate(labels):
        if i % s.per_page == 0:
            pg = doc.page(*s.page_size)
        assert pg is not None
        x, y = s.origin(i)
        _label_frame(pg, x, y, s, frame and s.page is not None)
        (qx, qy, q), box = _layout(x, y, s)
        pg.qr(qx, qy, q, qr_matrix(lab.qr_payload))
        rows = [(f"{lab.part_id} #{lab.copy}/{lab.copies}", True), (lab.name, True),
                (" · ".join(v for v in (lab.order, lab.customer) if v), False),
                (" · ".join(v for v in (lab.reference, lab.part_number, lab.due) if v), False),
                (_mat(lab.material, lab.thickness_mm), False),
                (" · ".join(v for v in (lab.sheet, f"queue {lab.queue_id}" if lab.queue_id else "") if v), False),
                (f"{lab.size_mm[0]:g} x {lab.size_mm[1]:g} mm" if any(lab.size_mm) else "", False)]
        _lines(pg, *box, rows, max_size=10.0 if s.height < 60 else 14.0)
    if not labels:
        doc.page(*s.page_size)
    return doc.save(path)


def render_remnant_labels(path: str, items: Sequence[RemnantLabel], size: str | LabelSize = "62x29",
                          frame: bool = True) -> str:
    s = size if isinstance(size, LabelSize) else label_size(size)
    doc = PdfDocument("Remnant labels")
    pg: Optional[PdfPage] = None
    for i, r in enumerate(items):
        if i % s.per_page == 0:
            pg = doc.page(*s.page_size)
        assert pg is not None
        x, y = s.origin(i)
        _label_frame(pg, x, y, s, frame and s.page is not None)
        (qx, qy, q), box = _layout(x, y, s)
        pg.qr(qx, qy, q, qr_matrix(r.qr_payload))
        when = _dt.datetime.fromtimestamp(r.created).strftime("%Y-%m-%d") if r.created else ""
        rows = [(f"Remnant {r.id}", True), (f"{r.width_mm:.0f} x {r.height_mm:.0f} mm", True),
                (_mat(r.material, r.thickness_mm), False),
                (f"area {r.area_mm2 / 1e6:.3f} m²" if r.area_mm2 else "", False),
                (" · ".join(v for v in (r.location, when) if v), False), (r.source, False)]
        _lines(pg, *box, rows, max_size=10.0 if s.height < 60 else 14.0)
    if not items:
        doc.page(*s.page_size)
    return doc.save(path)


def render_tickets(path: str, tickets: Sequence[TicketData], page: str = "A4") -> str:
    """One job ticket page per queued sheet."""
    pw, ph = A5 if page.upper() == "A5" else A4
    doc = PdfDocument("Job tickets")
    k = pw / 210.0                           # A5 scales the layout down
    for t in tickets:
        pg = doc.page(pw, ph)
        m = 12 * k
        q = 45 * k
        pg.qr(pw - m - q, m, q, qr_matrix(t.qr_payload))
        pg.text(m, m + 9 * k, t.name or t.entry_id, size=20 * k, bold=True, max_width=pw - 3 * m - q)
        y = m + 17 * k
        rows = [("Queue entry", t.entry_id), ("Material", _mat(t.material, t.thickness_mm) or "-"),
                ("Sheet", t.remnant_id and f"remnant {t.remnant_id}" or t.sheet or "-"),
                ("Quantity", f"{t.quantity} sheet(s)" + (f", {t.done} done" if t.done else "")),
                ("Estimated", f"{t.est_s / 60:.1f} min per sheet" if t.est_s else "-")]
        if t.note:
            rows.append(("Note", t.note))
        for a, b in rows:
            pg.text(m, y, a, size=10 * k, bold=True)
            pg.text(m + 32 * k, y, b, size=10 * k, max_width=pw - 3 * m - q - 32 * k)
            y += 6 * k
        y = max(y, m + q) + 6 * k
        pg.text(m, y, f"mlaser:queue:{t.entry_id}", size=8 * k)
        y += 4 * k
        box_h = ph * 0.42
        pg.rect(m, y, pw - 2 * m, box_h, width=0.2)
        if t.preview:
            bounds = t.sheet_bounds
            if bounds is not None:
                x0, y0, x1, y1 = bounds
                draw_outlines(pg, [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]], (m + 2, y + 2, pw - 2 * m - 4,
                              box_h - 4), bounds, width=0.3)
            draw_outlines(pg, t.preview, (m + 2, y + 2, pw - 2 * m - 4, box_h - 4), bounds, width=0.12, closed=False)
        y += box_h + 8 * k
        pg.text(m, y, "Parts", size=12 * k, bold=True)
        y += 6 * k
        col_w = (pw - 2 * m) / 2
        n_rows = max(1, int((ph - m - y) // (5 * k)))
        for i, (name, n) in enumerate(t.parts):
            c, r = divmod(i, n_rows)
            if c > 1:
                pg.text(m, ph - m, f"... {len(t.parts) - i} more", size=9 * k)
                break
            pg.text(m + c * col_w, y + r * 5 * k, f"{n} x  {name}", size=9 * k, max_width=col_w - 4)
    if not tickets:
        doc.page(pw, ph)
    return doc.save(path)


def labels_of(items: Iterable[Any]) -> list[RemnantLabel]:
    return [remnant_label(r) for r in items]


__all__ = ["LabelSize", "SIZES", "DEFAULT_SIZE", "label_size", "PartLabel", "TicketData", "RemnantLabel",
           "part_labels", "ticket_from_entry", "remnant_label", "labels_of", "qr_matrix", "render_part_labels",
           "render_remnant_labels", "render_tickets", "ORDER_KEYS", "A4", "A5"]
