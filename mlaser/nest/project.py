"""Nest project files (``*.nestproj``, filter "NestProject File|*.nestproj", MainApp 0x85a474).

Layout (observed, CADModule writer FUN_100da580 / reader FUN_100df500, saved by
FUN_100ebef0). It is the "scFlie" CSerialEng text container (:mod:`mlaser.formats.scio`) and
graph bodies use the .chf graph serialiser (type int + body, :mod:`mlaser.formats.chf`)::

    scFlie
    <int version>                (doc+0x9a0, 9)
    " "                          (reader skips the line)
    <Begin Parts>
    <int nParts>
      Part_<i+1>                 per part: int id (+0x34), int qty (+0x38), int nested (+0x3c),
      <id> <qty> <nested>        wstring name (+4)
      <name>
      Part Graphs
      <int nGraphs> then per graph: <int type> <graph body>
    <End Parts>
    <Begin Sheets>
    <double stdW> <double stdH>  (sheet 0, the standard "Rect Sheet", rebuilt by FUN_100da3a0)
    <int nSheets>                (including sheet 0)
      Sheet_<i+1>                per sheet i = 1..n-1: int id, int qty, int used, wstring name,
      ...                        bool isRect (+0x44), double W (+0x48), double H (+0x50),
      Sheet Graphs               int nGraphs, graphs
    <End Sheets>
    <Begin NestResults>
    <int nResults>
      Result_<i+1>               int sheetId (+0x44), wstring name (+4), int nPlacements,
      ...                        per placement: int partId, double angle, point "x,y";
      Result Graphs              int nGraphs, graphs (already transformed copies)
    <End NestResults>
    eof

Rebuild mapping: the first rectangular, non-remnant :class:`NestSheet` is sheet 0; its
quantity is not stored (the original passes 10000 copies to the engine, i.e. unlimited), so
it loads with ``quantity = STD_SHEET_QTY``. Rebuild-only fields (priority, max quantity,
angle list, mirror, nest parameters) are not part of the format and are not saved; the nest
parameters live in the NestParam section of ManuPara.xml in the original.
Placement (x, y) follows :class:`~mlaser.nest.model.Placement`; because the original's
rotation centre is unknown, the loader re-derives (x, y) from the stored transformed
graphs when their count matches.
"""
from __future__ import annotations

from pathlib import Path

from ..formats.chf import _TYPE_OF, _read_graph, _write_graph, graph_from_part, part_from_graph
from ..formats.scio import ScReader, ScWriter
from .apply import layout_parts
from .model import NestJob, NestPart, NestResult, NestSheet, Placement, SheetLayout
from .shapes import parts_geometry

VERSION = 9
STD_SHEET_QTY = 10000


def _write_graphs(w: ScWriter, tag: str, parts, v: int) -> None:
    w.str(tag)
    graphs = [graph_from_part(p) for p in parts]
    w.int(len(graphs))
    for g in graphs:
        w.int(_TYPE_OF[type(g)])
        _write_graph(w, g, v)


def _read_graphs(r: ScReader, v: int):
    r.tag()
    n = r.int()
    out = []
    for _ in range(n):
        gtype = r.int()
        out.append(part_from_graph(_read_graph(r, gtype, v)))
    return out


def _std_index(job: NestJob) -> int:
    for i, s in enumerate(job.sheets):
        if not s.outline and not s.is_remnant:
            return i
    return -1


def dumps_project(job: NestJob, version: int = VERSION) -> bytes:
    w = ScWriter()
    w.int(version)
    w.str(" ")
    res = job.result
    w.str("<Begin Parts>")
    w.int(len(job.parts))
    for i, p in enumerate(job.parts):
        w.tag_int("Part_", i + 1)
        w.int(p.id)
        w.int(p.quantity)
        w.int(res.placed.get(p.id, 0) if res else 0)
        w.str(p.name)
        _write_graphs(w, "Part Graphs", p.parts, version)
    w.str("<End Parts>")
    w.str("<Begin Sheets>")
    si = _std_index(job)
    std = job.sheets[si] if si >= 0 else NestSheet(1500.0, 3000.0)
    w.double(std.width)
    w.double(std.height)
    others = [s for k, s in enumerate(job.sheets) if k != si]
    w.int(len(others) + 1)
    used = {}
    if res:
        for lay in res.layouts:
            used[lay.sheet_id] = used.get(lay.sheet_id, 0) + 1
    for i, s in enumerate(others, 1):
        w.tag_int("Sheet_", i + 1)
        w.int(s.id)
        w.int(s.quantity)
        w.int(used.get(s.id, 0))
        w.str(s.name)
        is_rect = not s.outline
        w.bool(is_rect)
        W, H = s.width, s.height
        if s.outline:
            x0, y0, x1, y1 = parts_geometry(s.outline).bounds
            W, H = x1 - x0, y1 - y0
        w.double(W)
        w.double(H)
        _write_graphs(w, "Sheet Graphs", s.outline or [], version)
    w.str("<End Sheets>")
    w.str("<Begin NestResults>")
    lays = res.layouts if res else []
    w.int(len(lays))
    for i, lay in enumerate(lays):
        w.tag_int("Result_", i + 1)
        w.int(lay.sheet_id)
        w.str(lay.extra.get("name") or f"Result_{i + 1}")
        w.int(len(lay.placements))
        for pl in lay.placements:
            w.int(pl.part_id)
            w.double(pl.angle)
            w.point((pl.x, pl.y))
        _write_graphs(w, "Result Graphs", layout_parts(job, lay), version)
    w.str("<End NestResults>")
    w.eof()
    return w.getvalue()


def loads_project(data: bytes) -> NestJob:
    r = ScReader(data)
    v = r.int()
    if r.peek() == "":
        r.line()                     # the " " line (spaces are dropped by ReadLine)
    job = NestJob()
    job.extra["version"] = v
    r.tag("<Begin Parts>")
    nested: dict[int, int] = {}
    for _ in range(r.int()):
        r.tag()
        pid, qty, nst = r.int(), r.int(), r.int()
        name = r.wstring_raw()
        parts = _read_graphs(r, v)
        job.parts.append(NestPart(parts, name=name, quantity=qty, id=pid))
        nested[pid] = nst
    r.tag("<End Parts>")
    r.tag("<Begin Sheets>")
    sw, sh_ = r.double(), r.double()
    std = NestSheet(sw, sh_, STD_SHEET_QTY, name="Rect Sheet")
    n = r.int()
    sheets = []
    for _ in range(max(0, n - 1)):
        r.tag()
        sid, qty, used = r.int(), r.int(), r.int()
        name = r.wstring_raw()
        is_rect = r.bool()
        W, H = r.double(), r.double()
        graphs = _read_graphs(r, v)
        sheet = NestSheet(W, H, qty, name=name, id=sid)
        sheet.extra["used"] = used
        if not is_rect and graphs:
            sheet.outline = graphs
            sheet.is_remnant = True
            x0, y0, x1, y1 = parts_geometry(graphs).bounds
            sheet.origin, sheet.width, sheet.height = (x0, y0), x1 - x0, y1 - y0
        sheets.append(sheet)
    r.tag("<End Sheets>")
    std.id = 0 if all(s.id != 0 for s in sheets) else max(s.id for s in sheets) + 1
    job.sheets = [std] + sheets
    r.tag("<Begin NestResults>")
    nres = r.int()
    result = NestResult(placed={p.id: nested.get(p.id, 0) for p in job.parts})
    for _ in range(nres):
        r.tag()
        sid = r.int()
        name = r.wstring_raw()
        lay = SheetLayout(sid)
        lay.extra["name"] = name
        for _ in range(r.int()):
            pid = r.int()
            ang = r.double()
            x, y = r.point()
            lay.placements.append(Placement(pid, x, y, ang))
        graphs = _read_graphs(r, v)
        lay.extra["graphs"] = graphs
        _rederive(job, lay, graphs)
        result.layouts.append(lay)
    r.tag("<End NestResults>")
    r.eof()
    if nres:
        _utilisation(job, result)
        job.result = result
    return job


def _rederive(job: NestJob, lay: SheetLayout, graphs) -> None:
    """Fix (x, y) of each placement from the stored transformed graphs (rotation centre of
    the original is unknown, the graphs are authoritative)."""
    ids = {p.id for p in job.parts}
    if any(pl.part_id not in ids for pl in lay.placements):
        return
    counts = [len(job.part(pl.part_id).parts) for pl in lay.placements]
    if sum(counts) != len(graphs):
        return
    k = 0
    for pl, c in zip(lay.placements, counts):
        stored = parts_geometry(graphs[k:k + c])
        k += c
        mine = parts_geometry(layout_parts(job, SheetLayout(lay.sheet_id, [pl])))
        if stored.is_empty or mine.is_empty:
            continue
        sx, sy = (stored.bounds[0] + stored.bounds[2]) / 2, (stored.bounds[1] + stored.bounds[3]) / 2
        mx, my = (mine.bounds[0] + mine.bounds[2]) / 2, (mine.bounds[1] + mine.bounds[3]) / 2
        pl.x += sx - mx
        pl.y += sy - my


def _utilisation(job: NestJob, result: NestResult) -> None:
    from .shapes import sheet_geometry
    for lay in result.layouts:
        try:
            full, _ = sheet_geometry(job.sheet(lay.sheet_id), 0.0)
        except KeyError:
            continue
        area = sum(parts_geometry(job.part(pl.part_id).parts).area for pl in lay.placements
                   if any(p.id == pl.part_id for p in job.parts))
        lay.utilisation = area / full.area if full.area else 0.0


def save_project(job: NestJob, path) -> None:
    Path(path).write_bytes(dumps_project(job))


def load_project(path) -> NestJob:
    return loads_project(Path(path).read_bytes())
