"""Part labels, QR job tickets and remnant labels (mlaser.labels): the PDF files' contents."""
from __future__ import annotations

import re

import pytest

from mlaser.labels import labels as L
from mlaser.labels.pdf import MM, PdfDocument, text_width
from mlaser.model import Contour, Document, Line, Part


def rect(x0, y0, x1, y1):
    return Contour([Line((x0, y0), (x1, y0)), Line((x1, y0), (x1, y1)), Line((x1, y1), (x0, y1)),
                    Line((x0, y1), (x0, y0))])


# ------------------------------------------------------------------ a minimal PDF reader
def read_pdf(path):
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    # the xref offsets point at the objects
    xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref:xref + 4] == b"xref"
    n = int(re.search(rb"xref\n0 (\d+)", data).group(1))
    offs = [int(m) for m in re.findall(rb"(\d{10}) 00000 n", data[xref:])]
    assert len(offs) == n - 1
    for i, off in enumerate(offs, 1):
        assert data[off:].startswith(b"%d 0 obj" % i)
    pages = re.findall(rb"/Type /Page /Parent \d+ 0 R /MediaBox \[0 0 ([\d.]+) ([\d.]+)\].*?/Contents (\d+) 0 R", data)
    out = []
    for w, h, cid in pages:
        m = re.search(rb"\n%s 0 obj\n<< /Length (\d+) >>\nstream\n" % cid, data)
        ln = int(m.group(1))
        body = data[m.end():m.end() + ln].decode("latin-1")
        out.append({"size_mm": (float(w) / MM, float(h) / MM), "ops": body,
                    "texts": [_unesc(t) for t in re.findall(r"\(((?:\\.|[^\\)])*)\) Tj", body)]})
    return out


def _unesc(s):
    return re.sub(r"\\(.)", r"\1", s)


def qr_blocks(page):
    """The QR codes drawn on a page: list of filled-rect lists (x, y, w, h in points)."""
    out = []
    for blk in re.findall(r"% qr\n(.*?)% /qr", page["ops"], re.S):
        out.append([tuple(map(float, m)) for m in re.findall(r"([\d.-]+) ([\d.-]+) ([\d.-]+) ([\d.-]+) re f", blk)])
    return out


def matrix_of(rects):
    """Rebuild the module matrix from the drawn runs."""
    cell = min(r[3] for r in rects)
    x0 = min(r[0] for r in rects)
    ytop = max(r[1] + r[3] for r in rects)
    n = round((max(r[0] + r[2] for r in rects) - x0) / cell)
    m = [[False] * n for _ in range(n)]
    for x, y, w, h in rects:
        row = round((ytop - (y + h)) / cell)
        c0 = round((x - x0) / cell)
        for c in range(c0, c0 + round(w / cell)):
            m[row][c] = True
    return m


def nested_doc():
    """Two copies of a flange (outline + hole: two drawing parts per copy) and one bracket."""
    parts = []
    for k, (dx, name, pid) in enumerate(((0, "flange", 0), (120, "flange", 0), (240, "bracket", 1))):
        for c in ([rect(dx, 0, dx + 100, 50)], [rect(dx + 40, 15, dx + 60, 35)]) if name == "flange" else \
                ([rect(dx, 0, dx + 30, 30)],):
            p = Part(c, name=name)
            p.extra["nest"] = {"part_id": pid, "sheet_id": 0, "index": k, "x": dx, "y": 0, "angle": 0, "mirror": False}
            if name == "bracket":
                p.extra["order"] = {"order": "A-1042", "customer": "Müller GmbH", "reference": "PO 77"}
            parts.append(p)
    sheet = Part([rect(-5, -5, 400, 100)], name="sheet")
    sheet.craft.layer = 0
    sheet.extra["nest_sheet"] = 0
    parts.append(sheet)
    return Document(parts=parts)


# ------------------------------------------------------------------ data
def test_part_labels_one_per_placed_copy():
    labs = L.part_labels(nested_doc(), "Mild steel S235", 3.0, "1000 x 500 mm", "q42",
                         order_info={"flange": {"order": "A-1001", "customer": "ACME"}})
    assert [(x.part_id, x.name, x.copy, x.copies) for x in labs] == [("0", "flange", 1, 2), ("0", "flange", 2, 2),
                                                                    ("1", "bracket", 1, 1)]
    assert labs[0].order == "A-1001" and labs[0].customer == "ACME"
    assert labs[2].order == "A-1042" and labs[2].customer == "Müller GmbH" and labs[2].reference == "PO 77"
    assert labs[0].size_mm == (100.0, 50.0)
    assert labs[2].qr_payload == "mlaser:part:1;n=1;o=A-1042;q=q42"
    # a drawing without nest data: one label per part, ids are the part numbers
    plain = L.part_labels(Document(parts=[Part([rect(0, 0, 10, 10)], name="a"), Part([rect(20, 0, 30, 10)])]))
    assert [(x.part_id, x.name) for x in plain] == [("1", "a"), ("2", "part 2")]


@pytest.mark.parametrize("key", [s.key for s in L.SIZES])
def test_label_sizes_pages_and_texts(tmp_path, key):
    s = L.label_size(key)
    labs = L.part_labels(nested_doc(), "Mild steel S235", 3.0, "1000 x 500 mm", "q42") * 9   # 27 labels
    path = L.render_part_labels(str(tmp_path / "l.pdf"), labs, key)
    pages = read_pdf(path)
    assert len(pages) == -(-27 // s.per_page)
    w, h = pages[0]["size_mm"]
    assert (w, h) == pytest.approx(s.page_size, abs=0.01)
    # every label is on the page
    for i in range(s.per_page):
        x, y = s.origin(i)
        assert x >= -1e-6 and y >= -1e-6 and x + s.width <= w + 0.01 and y + s.height <= h + 0.01
    texts = [t for p in pages for t in p["texts"]]
    assert sum(1 for t in texts if t.startswith("0 #1/2")) == 9 and sum(1 for t in texts if t.startswith("1 #1/1")) == 9
    assert any("Mild steel S235 3 mm" in t or t.startswith("Mild steel") for t in texts)
    assert sum(len(qr_blocks(p)) for p in pages) == 27


def test_qr_drawn_equals_segno_matrix(tmp_path):
    lab = L.part_labels(nested_doc(), queue_id="q42")[2]
    pages = read_pdf(L.render_part_labels(str(tmp_path / "l.pdf"), [lab], "100x50"))
    (blk,) = qr_blocks(pages[0])
    assert matrix_of(blk) == L.qr_matrix(lab.qr_payload)
    import segno
    assert L.qr_matrix(lab.qr_payload) == [[bool(v) for v in r] for r in segno.make(lab.qr_payload, error="m").matrix_iter(
        scale=1, border=0)]


def test_non_latin_text_is_replaced_not_lost(tmp_path):
    doc = PdfDocument()
    pg = doc.page(50, 20)
    pg.text(1, 5, "Müller 中文 (x)")
    doc.save(tmp_path / "a.pdf")
    (p,) = read_pdf(tmp_path / "a.pdf")
    assert p["texts"] == ["M\xfcller ?? (x)"]
    assert text_width("iiii", 10) < text_width("WWWW", 10)


def test_long_text_is_shortened_to_the_label(tmp_path):
    lab = L.PartLabel("7", "a very long part name that cannot fit on a small roll label at all", material="S235")
    pages = read_pdf(L.render_part_labels(str(tmp_path / "l.pdf"), [lab], "57x32"))
    assert any(t.endswith("...") and t.startswith("a very") for t in pages[0]["texts"])


# ------------------------------------------------------------------ tickets and remnants
class Entry:
    id = "e1b2c3"
    name = "nest #1"
    material = "Mild steel S235"
    thickness_mm = 3.0
    sheet = "1000 x 500 mm"
    remnant_id = ""
    quantity = 3
    done = 1
    est_s = 420.0
    note = ""
    path = "nest_sheet1.chf"
    extra = {"sheet_bounds": [-5, -5, 400, 100]}


@pytest.mark.parametrize("page", ["A4", "A5"])
def test_job_ticket(tmp_path, page):
    t = L.ticket_from_entry(Entry(), nested_doc())
    assert t.qr_payload == "mlaser:queue:e1b2c3"
    assert t.parts == [("flange", 2), ("bracket", 1)]
    pages = read_pdf(L.render_tickets(str(tmp_path / "t.pdf"), [t, t], page))
    assert len(pages) == 2
    assert pages[0]["size_mm"] == pytest.approx((210, 297) if page == "A4" else (148, 210), abs=0.01)
    texts = pages[0]["texts"]
    for want in ("nest #1", "e1b2c3", "Mild steel S235 3 mm", "1000 x 500 mm", "3 sheet(s), 1 done", "2 x  flange",
                 "1 x  bracket", "mlaser:queue:e1b2c3"):
        assert want in texts, want
    (blk,) = qr_blocks(pages[0])
    assert matrix_of(blk) == L.qr_matrix("mlaser:queue:e1b2c3")
    # the sheet preview: one polyline per contour (sheet frame + 5 contours + the sheet part)
    assert pages[0]["ops"].count(" S") >= 7


def test_remnant_labels(tmp_path):
    class Rem:
        id = "R-0007"
        rings = [[(0, 0), (664, 0), (664, 500), (0, 500)]]
        material = "Mild steel S235"
        thickness_mm = 3.0
        location = "rack B2"
        source = "nest #1 (queue:e1b2c3)"
        created = 1760000000.0
        size = (664.0, 500.0)
        area_mm2 = 332000.0

    r = L.remnant_label(Rem())
    assert r.qr_payload == "mlaser:remnant:R-0007" and (r.width_mm, r.height_mm) == (664.0, 500.0)
    pages = read_pdf(L.render_remnant_labels(str(tmp_path / "r.pdf"), [r] * 5, "a4_l7160"))
    assert len(pages) == 1 and len(qr_blocks(pages[0])) == 5
    texts = pages[0]["texts"]
    assert "Remnant R-0007" in texts and "664 x 500 mm" in texts and any("rack B2" in t for t in texts)
    assert matrix_of(qr_blocks(pages[0])[0]) == L.qr_matrix("mlaser:remnant:R-0007")


def test_unknown_size_is_refused():
    with pytest.raises(KeyError):
        L.label_size("nope")


# ------------------------------------------------------------------ the application plugin
@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_plugin_tickets_and_remnant_labels(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        ext = w.extension("ext_labels")
        queue = w.hooks.provider("queue")()
        queue.add(str(tmp_path / "missing.chf"), 2, name="order 77", material="S235", thickness_mm=3.0)
        p = ext.job_tickets(str(tmp_path / "t.pdf"))
        (page,) = read_pdf(p)
        assert "order 77" in page["texts"] and any(t.startswith("mlaser:queue:") for t in page["texts"])
        # a saved remnant gets its label automatically (setting remnant_auto)
        ext.settings["remnant_auto"] = True
        store = w.hooks.provider("remnants")()
        r = store.add([[(0, 0), (300, 0), (300, 200), (0, 200)]], "S235", 3.0)
        auto = tmp_path / "labels" / f"remnant-{r.id}.pdf"
        assert auto.exists()
        assert f"Remnant {r.id}" in read_pdf(auto)[0]["texts"]
        p2 = ext.remnant_labels(str(tmp_path / "r.pdf"))
        assert len(qr_blocks(read_pdf(p2)[0])) == 1
        # every command runs headless (dialogs stubbed by the test window: no file chosen)
        from PySide6.QtWidgets import QFileDialog
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
        for cid in ("labels.part_labels", "labels.job_tickets", "labels.remnant_labels", "labels.remove_marking"):
            w.commands.run(cid)
    finally:
        w.close()
