"""Order intake: parts lists (CSV), multi-file import, orders feeding the nest (Qt-free).

A job shop thinks in orders ("customer A, 20 × bracket.dxf, 3 mm S235, due Thursday").  This
module turns that into nest parts with quantities and priorities, and keeps the customer and
order references on them through nesting, the job queue and the production report:

* :func:`parse_parts_csv` reads a parts list.  Columns (header names are matched loosely, see
  :data:`COLUMNS`; ``,`` ``;`` or tab separated, UTF-8 or Windows-1252):
  ``file, quantity, material, thickness, customer, order, due, priority`` and optionally
  ``notes`` and ``recognition`` (``nested`` / ``layer`` / ``block`` / ``file``, see
  :mod:`mlaser.services.parts`).  Bad rows are reported with their line number and reason;
  the good rows are kept.  :func:`template_csv` is the downloadable template.
* :class:`OrderBook` holds the open order lines (``<data dir>/production/orders.json``, atomic
  writes) and groups them by material × thickness (one nest run per group).
* :func:`nest_parts_for` loads each line's drawing, recognises its parts (a gear's bore stays in
  the gear) and returns :class:`~mlaser.nest.model.NestPart` s with ``quantity = line quantity ×
  copies in the file``, the line's priority and ``extra["order"]`` (customer, order, line id,
  due date, file).
* :func:`sheet_order_refs` lists the order references on a nested sheet document (the queue
  entry and the production record carry them); :func:`order_progress` sums the cut parts per
  order line from production records.

Quoting is deliberately not part of this (docs/ROADMAP.md, Review 2).
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from mlaser.model import Document

FORMAT = "mlaser-orders/1"
MAX_CSV_BYTES = 5_000_000            # untrusted input: a parts list is small
MAX_ROWS = 20_000
DRAWING_EXTS = (".dxf", ".svg", ".chf")

#: canonical column -> accepted header names (lower case, spaces/underscores/dots ignored)
COLUMNS: dict[str, tuple[str, ...]] = {
    "file": ("file", "path", "filename", "drawing", "dxf", "partfile", "part", "datei"),
    "quantity": ("quantity", "qty", "count", "pcs", "pieces", "amount", "menge", "anzahl", "stk"),
    "material": ("material", "mat", "werkstoff"),
    "thickness": ("thickness", "thk", "thicknessmm", "thick", "t", "staerke", "dicke"),
    "customer": ("customer", "client", "kunde"),
    "order": ("order", "orderref", "ref", "reference", "orderno", "ordernumber", "po", "job", "auftrag"),
    "due": ("due", "duedate", "deadline", "date", "delivery", "termin"),
    "priority": ("priority", "prio"),
    "notes": ("notes", "note", "comment", "remarks"),
    "recognition": ("recognition", "mode", "partmode", "grouping"),
}
TEMPLATE_COLUMNS = ("file", "quantity", "material", "thickness", "customer", "order", "due", "priority")
PRIORITY_WORDS = {"low": -1, "normal": 0, "medium": 0, "high": 1, "urgent": 2, "rush": 2}

__all__ = ["OrderLine", "RowError", "CsvResult", "OrderBook", "parse_parts_csv", "template_csv",
           "load_drawing", "nest_parts_for", "sheet_order_refs", "order_progress", "material_key",
           "lines_from_files", "COLUMNS", "FORMAT"]


def material_key(material: str, thickness_mm: float) -> tuple[str, float]:
    """Grouping key: material case- and space-insensitive, thickness to 0.01 mm."""
    return (" ".join(str(material or "").split()).casefold(), round(float(thickness_mm or 0.0), 2))


# ============================================================================== records
@dataclass
class OrderLine:
    file: str
    quantity: int = 1
    material: str = ""
    thickness_mm: float = 0.0
    customer: str = ""
    order: str = ""
    due: str = ""                    # ISO date (yyyy-mm-dd) or ""
    priority: int = 0
    notes: str = ""
    recognition: str = ""            # "" = the Nest workspace's rule
    id: str = ""
    added: float = 0.0
    source: str = ""                 # the CSV it came from, with the line number
    per_file: int = 0                # parts recognised in the file (0 = not loaded yet)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:10]

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.file))[0]

    @property
    def key(self) -> tuple[str, float]:
        return material_key(self.material, self.thickness_mm)

    def ref(self) -> dict:
        """What travels with the parts: customer, order, line id, due date, file."""
        return {"customer": self.customer, "order": self.order, "line": self.id, "due": self.due,
                "file": os.path.basename(self.file), "material": self.material,
                "thickness_mm": self.thickness_mm}

    @classmethod
    def from_json(cls, d: dict) -> "OrderLine":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass(frozen=True)
class RowError:
    line: int                        # 1-based line number in the file
    message: str
    raw: str = ""


@dataclass
class CsvResult:
    lines: list[OrderLine] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    columns: dict[str, int] = field(default_factory=dict)     # canonical -> column index
    unknown_columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        s = f"{len(self.lines)} line(s) read"
        if self.errors:
            s += f", {len(self.errors)} row(s) refused"
        return s


# ============================================================================== CSV
def template_csv() -> str:
    """The parts-list template (header plus two example rows)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TEMPLATE_COLUMNS)
    w.writerow(["bracket.dxf", 20, "Mild steel S235", 3, "Customer A", "PO-1234", "2026-10-29", 1])
    w.writerow(["flange.dxf", 4, "Mild steel S235", 3, "Customer B", "B-77", "", 0])
    return buf.getvalue()


def _norm_header(h: str) -> str:
    return re.sub(r"[\s_.\-()/]+", "", str(h or "").strip().lower()).replace("mm", "mm")


def _map_header(row: list[str]) -> tuple[dict[str, int], list[str]]:
    cols: dict[str, int] = {}
    unknown = []
    for i, h in enumerate(row):
        n = _norm_header(h)
        hit = None
        for canon, names in COLUMNS.items():
            if n in names or (canon == "thickness" and n.startswith("thickness")):
                hit = canon
                break
        if hit is None or hit in cols:
            if n:
                unknown.append(str(h).strip())
            continue
        cols[hit] = i
    return cols, unknown


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "windows-1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _number(s: str) -> float:
    t = str(s).strip().lower().replace("mm", "").replace(" ", "")
    if t.count(",") == 1 and "." not in t:
        t = t.replace(",", ".")                # decimal comma
    return float(t)


def _date(s: str) -> str:
    t = str(s).strip()
    if not t:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%y", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"due date {t!r} is not a date (use yyyy-mm-dd or dd.mm.yyyy)")


def _priority(s: str) -> int:
    t = str(s).strip().lower()
    if not t:
        return 0
    if t in PRIORITY_WORDS:
        return PRIORITY_WORDS[t]
    v = float(t)
    if v != int(v):
        raise ValueError
    return int(v)


def parse_parts_csv(source: Any, base_dir: Optional[str] = None, check_files: bool = True) -> CsvResult:
    """Read a parts list (``source``: a path, bytes or text).  Relative file names are resolved
    against ``base_dir`` (default: the CSV's folder).  Bad rows go to ``errors``."""
    res = CsvResult()
    name = ""
    if isinstance(source, (str, os.PathLike)) and not (isinstance(source, str) and "\n" in source):
        p = Path(source)
        name = str(p)
        if p.stat().st_size > MAX_CSV_BYTES:
            res.errors.append(RowError(0, f"the file is larger than {MAX_CSV_BYTES // 1_000_000} MB: not a parts list"))
            return res
        text = _decode(p.read_bytes())
        base_dir = base_dir or str(p.parent)
    elif isinstance(source, bytes):
        if len(source) > MAX_CSV_BYTES:
            res.errors.append(RowError(0, "the data is too large for a parts list"))
            return res
        text = _decode(source)
    else:
        text = str(source)
    base_dir = base_dir or os.getcwd()
    lines = text.splitlines()
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header: Optional[dict[str, int]] = None
    for lineno, row in enumerate(reader, 1):
        if lineno > MAX_ROWS + 1:
            res.errors.append(RowError(lineno, f"more than {MAX_ROWS} rows: the rest is ignored"))
            break
        cells = [c.strip() for c in row]
        if not any(cells) or cells[0].startswith("#"):
            continue
        raw = delim.join(row)
        if header is None:
            cols, unknown = _map_header(cells)
            if "file" in cols:
                header = cols
                res.columns, res.unknown_columns = cols, unknown
                continue
            if cells[0].lower().endswith(DRAWING_EXTS):            # no header: template order
                header = {c: i for i, c in enumerate(TEMPLATE_COLUMNS)}
                res.columns = dict(header)
            else:
                res.errors.append(RowError(lineno, "no 'file' column in the header (see the template)", raw))
                return res
        get = lambda c: cells[header[c]] if c in header and header[c] < len(cells) else ""  # noqa: E731
        try:
            line = _row_line(get, base_dir, check_files)
        except ValueError as exc:
            res.errors.append(RowError(lineno, str(exc), raw))
            continue
        line.source = f"{os.path.basename(name) or 'csv'}:{lineno}"
        res.lines.append(line)
    return res


def _row_line(get: Callable[[str], str], base_dir: str, check_files: bool) -> OrderLine:
    f = get("file")
    if not f:
        raise ValueError("the file name is empty")
    path = f if os.path.isabs(f) else os.path.normpath(os.path.join(base_dir, f))
    if not path.lower().endswith(DRAWING_EXTS):
        raise ValueError(f"{f}: not a drawing (.dxf, .svg or .chf)")
    if check_files and not os.path.isfile(path):
        raise ValueError(f"{f}: file not found")
    q = get("quantity")
    try:
        qty = _number(q) if q else 1.0
    except ValueError:
        raise ValueError(f"quantity {q!r} is not a number") from None
    if qty != int(qty) or qty < 1:
        raise ValueError(f"quantity {q!r} must be a whole number of at least 1")
    if qty > 100_000:
        raise ValueError(f"quantity {q!r} is too large")
    t = get("thickness")
    th = 0.0
    if t:
        try:
            th = _number(t)
        except ValueError:
            raise ValueError(f"thickness {t!r} is not a number") from None
        if not (0.0 < th <= 100.0):
            raise ValueError(f"thickness {t!r} must be between 0 and 100 mm")
    pr = get("priority")
    try:
        prio = _priority(pr)
    except ValueError:
        raise ValueError(f"priority {pr!r} is not a whole number (or low / normal / high / urgent)") from None
    rec = get("recognition").strip().lower()
    if rec:
        from mlaser.services.parts import MODES
        if rec not in MODES:
            raise ValueError(f"recognition {rec!r} is not one of {', '.join(MODES)}")
    return OrderLine(file=path, quantity=int(qty), material=" ".join(get("material").split()), thickness_mm=th,
                     customer=get("customer"), order=get("order"), due=_date(get("due")), priority=prio,
                     notes=get("notes"), recognition=rec, added=time.time())


def lines_from_files(paths: Iterable[str], quantity: int = 1, material: str = "", thickness_mm: float = 0.0,
                     **kw: Any) -> list[OrderLine]:
    """Order lines for a multi-file import (one line per drawing; folders are searched for
    drawings, not recursively)."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            items = sorted(os.path.join(p, n) for n in os.listdir(p) if n.lower().endswith(DRAWING_EXTS))
        else:
            items = [p]
        for f in items:
            if f.lower().endswith(DRAWING_EXTS):
                out.append(OrderLine(file=os.path.abspath(f), quantity=max(1, int(quantity)), material=material,
                                     thickness_mm=float(thickness_mm), added=time.time(), **kw))
    return out


# ============================================================================== store
class OrderBook:
    """Open order lines, persisted as JSON (``path``; None keeps them in memory)."""

    def __init__(self, path: Optional[os.PathLike | str] = None) -> None:
        self.path = Path(path) if path is not None else None
        self.lines: list[OrderLine] = []
        self.listeners: list[Callable[[], None]] = []
        self.load_error = ""
        if self.path is not None:
            self.load()

    def load(self) -> None:
        self.lines = []
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("lines", []) if isinstance(data, dict) else []
            self.lines = [OrderLine.from_json(d) for d in items if isinstance(d, dict) and d.get("file")]
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            try:
                os.replace(self.path, str(self.path) + ".bad")
            except OSError:
                pass

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": FORMAT, "saved": time.time(), "lines": [asdict(x) for x in self.lines]}
        fd, tmp = tempfile.mkstemp(prefix=".orders-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def changed(self) -> None:
        self.save()
        for fn in list(self.listeners):
            fn()

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def add(self, lines: Iterable[OrderLine]) -> list[OrderLine]:
        new = list(lines)
        self.lines.extend(new)
        if new:
            self.changed()
        return new

    def remove(self, ids: Iterable[str]) -> int:
        ids = set(ids)
        n0 = len(self.lines)
        self.lines = [x for x in self.lines if x.id not in ids]
        if len(self.lines) != n0:
            self.changed()
        return n0 - len(self.lines)

    def get(self, line_id: str) -> OrderLine:
        for x in self.lines:
            if x.id == line_id:
                return x
        raise KeyError(line_id)

    def groups(self) -> list[tuple[tuple[str, float], list[OrderLine]]]:
        """Lines by material × thickness, in first-seen order; inside a group by due date then
        priority (urgent first)."""
        out: dict[tuple[str, float], list[OrderLine]] = {}
        for x in self.lines:
            out.setdefault(x.key, []).append(x)
        for v in out.values():
            v.sort(key=lambda x: (x.due or "9999-99-99", -x.priority))
        return list(out.items())

    def to_json(self) -> list[dict]:
        return [asdict(x) for x in self.lines]

    def set_json(self, items: Any) -> None:
        self.lines = [OrderLine.from_json(d) for d in (items or []) if isinstance(d, dict) and d.get("file")]
        self.changed()


# ============================================================================== to the nest
def load_drawing(path: str) -> Document:
    """Read a drawing the way File > Open does (DXF with the active import profile, SVG, chf);
    the untrusted-input limits of :mod:`mlaser.io.limits` apply."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        from mlaser.io.dxf_import import import_dxf
        return import_dxf(path)
    if ext == ".svg":
        from mlaser.io.svg_import import import_svg
        return import_svg(path)
    if ext == ".chf":
        from mlaser.formats.chf import load_chf
        return load_chf(path)
    raise ValueError(f"{os.path.basename(path)}: not a drawing")


def nest_parts_for(lines: Iterable[OrderLine], rules: Any = None,
                   loader: Callable[[str], Document] = load_drawing,
                   repair: Optional[Callable[[Document], Any]] = None) -> tuple[list, list[str]]:
    """NestParts for the order lines (see the module docstring) and the problems found
    (unreadable files, files without a closed contour)."""
    from mlaser.services.parts import RecognitionRules, recognize
    base = rules or RecognitionRules()
    cache: dict[str, Document] = {}
    parts, problems = [], []
    for ln in lines:
        try:
            doc = cache.get(ln.file)
            if doc is None:
                doc = loader(ln.file)
                if repair is not None:
                    repair(doc)
                cache[ln.file] = doc
        except Exception as exc:  # noqa: BLE001 - reported per line
            problems.append(f"{os.path.basename(ln.file)}: {exc}")
            continue
        r = base
        if ln.recognition and ln.recognition != base.mode:
            r = RecognitionRules.from_dict({**base.to_dict(), "mode": ln.recognition})
        ps = recognize(doc, r, base=ln.name)
        if not ps.groups:
            problems.append(f"{os.path.basename(ln.file)}: no closed contour, nothing to nest")
            continue
        ln.per_file = ps.count
        if ps.loose:
            problems.append(f"{os.path.basename(ln.file)}: {len(ps.loose)} open contour(s) in no part were left out")
        for np_ in ps.nest_parts(doc, multiplier=ln.quantity, extra={"order": ln.ref()}):
            np_.priority = ln.priority
            parts.append(np_)
    return parts, problems


def sheet_order_refs(doc: Document, job: Any) -> list[dict]:
    """The order references on a nested sheet: one dict per (order line, part) with the
    number of copies on the sheet.  ``job`` is the NestJob (part id -> NestPart)."""
    copies: dict[int, set] = {}
    for p in doc.parts:
        nest = (p.extra or {}).get("nest") if hasattr(p.extra, "get") else None
        if isinstance(nest, dict) and "part_id" in nest:
            copies.setdefault(int(nest["part_id"]), set()).add(nest.get("index"))
    out = []
    for pid, idx in sorted(copies.items()):
        try:
            np_ = job.part(pid)
        except KeyError:
            continue
        ref = (np_.extra or {}).get("order")
        if not isinstance(ref, dict):
            continue
        out.append({**ref, "part": np_.name, "qty": len(idx)})
    return out


def order_progress(lines: Iterable[OrderLine], records: Iterable[Any]) -> dict[str, int]:
    """Parts cut per order line id, from production records whose ``orders`` list the refs
    (only completed sheets count)."""
    want = {x.id for x in lines}
    done: dict[str, int] = {k: 0 for k in want}
    for r in records:
        if not getattr(r, "ok", False):
            continue
        for ref in getattr(r, "orders", None) or []:
            lid = ref.get("line") if isinstance(ref, dict) else None
            if lid in done:
                done[lid] += int(ref.get("qty", 0) or 0)
    return done


def parts_needed(line: OrderLine) -> int:
    """Parts the line asks for: its quantity × the parts in its file (1 until it was loaded)."""
    return int(line.quantity) * max(1, int(line.per_file or 1))
