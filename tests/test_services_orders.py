"""Order intake (mlaser.services.orders): CSV parts lists (good and bad rows), the template,
the order book, nest parts with references, sheet references and progress."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mlaser.nest import NestJob, NestSheet, nest
from mlaser.nest.apply import layout_parts
from mlaser.model import Document
from mlaser.services import orders as O

DXF = Path(__file__).parent / "data" / "dxf"


@pytest.fixture
def drawings(tmp_path):
    for n in ("gear", "inserts", "basic_r2000"):
        shutil.copy(DXF / f"{n}.dxf", tmp_path / f"{n}.dxf")
    return tmp_path


def write(tmp_path, text, name="parts.csv", enc="utf-8"):
    p = tmp_path / name
    p.write_bytes(text.encode(enc))
    return p


def test_template_parses_as_a_parts_list(tmp_path):
    t = O.template_csv()
    assert t.splitlines()[0] == "file,quantity,material,thickness,customer,order,due,priority"
    res = O.parse_parts_csv(write(tmp_path, t), check_files=False)
    assert res.ok and len(res.lines) == 2
    a = res.lines[0]
    assert (a.name, a.quantity, a.material, a.thickness_mm, a.customer, a.order, a.due, a.priority) == \
        ("bracket", 20, "Mild steel S235", 3.0, "Customer A", "PO-1234", "2026-10-29", 1)


def test_good_rows_and_aliases(drawings):
    csv = ("Datei;Menge;Werkstoff;Thickness (mm);Kunde;PO;Due date;Prio;Notes\n"
           "gear.dxf;6;Mild steel  S235;3,0;ACME;4711;29.10.2026;high;deburr\n"
           "# a comment line\n"
           "\n"
           "inserts.dxf;10;Mild steel S235;3;ACME;4711;;2;\n")
    res = O.parse_parts_csv(write(drawings, csv))
    assert res.ok, res.errors
    g, i = res.lines
    assert g.file == str(drawings / "gear.dxf") and g.quantity == 6 and g.thickness_mm == 3.0
    assert g.material == "Mild steel S235" and g.due == "2026-10-29" and g.priority == 1 and g.notes == "deburr"
    assert i.priority == 2 and i.due == "" and g.key == i.key
    assert g.source == "parts.csv:2"


def test_bad_rows_are_reported_and_good_rows_kept(drawings):
    csv = ("file,qty,material,thickness,customer,order,due,priority\n"
           "gear.dxf,2,S235,3,A,1,,\n"              # 2 good
           "missing.dxf,1,S235,3,A,1,,\n"            # 3 file not found
           "gear.dxf,two,S235,3,A,1,,\n"             # 4 quantity
           "gear.dxf,0,S235,3,A,1,,\n"               # 5 quantity < 1
           "gear.dxf,1.5,S235,3,A,1,,\n"             # 6 not whole
           "gear.dxf,1,S235,-3,A,1,,\n"              # 7 thickness
           "gear.dxf,1,S235,abc,A,1,,\n"             # 8 thickness
           "gear.dxf,1,S235,3,A,1,31.02.2026,\n"     # 9 date
           "gear.dxf,1,S235,3,A,1,,soon\n"           # 10 priority
           "notes.txt,1,S235,3,A,1,,\n"              # 11 not a drawing
           ",1,S235,3,A,1,,\n"                        # 12 empty file
           "inserts.dxf,3,S235,3,B,2,2026-11-01,0\n")  # 13 good
    res = O.parse_parts_csv(write(drawings, csv))
    assert [x.name for x in res.lines] == ["gear", "inserts"]
    lines = {e.line: e.message for e in res.errors}
    assert sorted(lines) == list(range(3, 13))
    assert "not found" in lines[3] and "quantity" in lines[4] and "at least 1" in lines[5]
    assert "whole" in lines[6] and "between 0 and 100" in lines[7] and "not a number" in lines[8]
    assert "not a date" in lines[9] and "priority" in lines[10] and "not a drawing" in lines[11]
    assert "empty" in lines[12]
    assert res.summary() == "2 line(s) read, 10 row(s) refused"


def test_no_header_uses_the_template_order(drawings):
    res = O.parse_parts_csv(write(drawings, "gear.dxf,4,S235,2\ninserts.dxf\n"))
    assert res.ok and [(x.name, x.quantity, x.thickness_mm) for x in res.lines] == [("gear", 4, 2.0), ("inserts", 1, 0.0)]


def test_header_without_a_file_column_is_refused(tmp_path):
    res = O.parse_parts_csv(write(tmp_path, "name,qty\nbracket,2\n"))
    assert not res.lines and "no 'file' column" in res.errors[0].message


def test_windows_1252_and_tabs(drawings):
    res = O.parse_parts_csv(write(drawings, "file\tqty\tcustomer\ngear.dxf\t1\tMüller\n", enc="cp1252"))
    assert res.ok and res.lines[0].customer == "Müller"


def test_too_large_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "MAX_CSV_BYTES", 10)
    res = O.parse_parts_csv(write(tmp_path, "file,qty\ngear.dxf,1\n"))
    assert not res.lines and res.errors[0].line == 0


def test_bad_recognition_value(drawings):
    res = O.parse_parts_csv(write(drawings, "file,qty,recognition\ngear.dxf,1,magic\ngear.dxf,1,file\n"))
    assert len(res.lines) == 1 and res.lines[0].recognition == "file" and "recognition" in res.errors[0].message


def test_order_book_persists_and_groups(tmp_path, drawings):
    book = O.OrderBook(tmp_path / "prod" / "orders.json")
    seen = []
    book.subscribe(lambda: seen.append(1))
    book.add([O.OrderLine(str(drawings / "gear.dxf"), 2, "S235", 3, "A", "1", due="2026-11-02"),
              O.OrderLine(str(drawings / "inserts.dxf"), 1, "s235", 3.0, "B", "2", due="2026-11-01", priority=1),
              O.OrderLine(str(drawings / "gear.dxf"), 1, "Aluminium", 2, "C", "3")])
    assert seen
    again = O.OrderBook(tmp_path / "prod" / "orders.json")
    assert [x.id for x in again.lines] == [x.id for x in book.lines]
    groups = again.groups()
    assert [(k, [x.customer for x in v]) for k, v in groups] == [(("s235", 3.0), ["B", "A"]), (("aluminium", 2.0), ["C"])]
    assert again.remove([book.lines[0].id]) == 1 and len(again.lines) == 2


def test_order_book_bad_file_is_kept_aside(tmp_path):
    p = tmp_path / "orders.json"
    p.write_text("{not json")
    book = O.OrderBook(p)
    assert book.lines == [] and book.load_error and (tmp_path / "orders.json.bad").exists()


def test_nest_parts_recognise_and_carry_the_references(drawings):
    lines = [O.OrderLine(str(drawings / "gear.dxf"), 6, "S235", 3, "ACME", "4711", due="2026-10-29", priority=2),
             O.OrderLine(str(drawings / "inserts.dxf"), 5, "S235", 3, "Beta", "B-1"),
             O.OrderLine(str(drawings / "basic_r2000.dxf"), 2, "S235", 3, "Beta", "B-2", recognition="file"),
             O.OrderLine(str(drawings / "nope.dxf"), 1)]
    parts, problems = O.nest_parts_for(lines)
    names = [(p.name, p.quantity, p.priority) for p in parts]
    assert names[0] == ("gear", 6, 2)                        # the bore stays in the gear
    assert len(parts[0].parts) == 2
    assert ("inserts-1", 10, 0) in names and ("inserts-2", 10, 0) in names   # 2 copies per file x 5
    assert ("basic_r2000", 2, 0) in names
    assert parts[0].extra["order"] == {"customer": "ACME", "order": "4711", "line": lines[0].id, "due": "2026-10-29",
                                       "file": "gear.dxf", "material": "S235", "thickness_mm": 3}
    assert lines[1].per_file == 4 and O.parts_needed(lines[1]) == 20
    assert any("nope.dxf" in p for p in problems)
    assert any("inserts.dxf: 2 open contour" in p for p in problems)


def test_references_through_nesting_and_progress(drawings):
    lines = [O.OrderLine(str(drawings / "gear.dxf"), 2, "S235", 3, "ACME", "4711"),
             O.OrderLine(str(drawings / "inserts.dxf"), 1, "S235", 3, "Beta", "B-1")]
    parts, _ = O.nest_parts_for(lines)
    job = NestJob()
    for p in parts:
        job.add_part(p)
    job.add_sheet(NestSheet(600, 400, 1))
    job.params.time_limit_s = 5
    res = nest(job)
    assert not res.unplaced
    doc = Document(parts=layout_parts(job, res.layouts[0]))
    refs = O.sheet_order_refs(doc, job)
    assert {(r["order"], r["part"], r["qty"]) for r in refs} == {("4711", "gear", 2), ("B-1", "inserts-1", 2),
                                                                 ("B-1", "inserts-2", 2)}

    class Rec:
        def __init__(self, ok, orders):
            self.ok, self.orders = ok, orders
    prog = O.order_progress(lines, [Rec(True, refs), Rec(False, refs)])
    assert prog == {lines[0].id: 2, lines[1].id: 4}


def test_lines_from_files_and_folders(drawings):
    out = O.lines_from_files([str(drawings / "gear.dxf"), str(drawings)], quantity=3, material="S235")
    assert [x.name for x in out] == ["gear", "basic_r2000", "gear", "inserts"]
    assert all(x.quantity == 3 and x.material == "S235" for x in out)
