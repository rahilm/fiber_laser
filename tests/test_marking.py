"""Part-ID etching (mlaser.labels.marking): single-stroke text inside each part, clear of the
outline and the holes, on the marking layer, etched before the part is cut free."""
from __future__ import annotations

import pytest
from shapely.geometry import LineString, Polygon

from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.labels import marking as MK
from mlaser.model import Arc, Contour, Document, LayerParams, Line, Part

LAYER = 10


def rect(x0, y0, x1, y1):
    return Contour([Line((x0, y0), (x1, y0)), Line((x1, y0), (x1, y1)), Line((x1, y1), (x0, y1)),
                    Line((x0, y1), (x0, y0))])


def circle(cx, cy, r):
    return Contour([Arc((cx + r, cy), (cx - r, cy), (cx, cy), True), Arc((cx - r, cy), (cx + r, cy), (cx, cy), True)])


def flange():
    """100 x 60 plate with a big bore and four bolt holes."""
    return Part([rect(0, 0, 100, 60), circle(50, 30, 18), circle(10, 10, 4), circle(90, 10, 4), circle(10, 50, 4),
                 circle(90, 50, 4)], name="fl-7")


def marks(part):
    return [c for c in part.contours if MK.is_marking(c, LAYER)]


def test_font_covers_the_id_alphabet_and_is_single_stroke():
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./#:?":
        strokes = MK.FONT[ch]
        assert strokes, ch
        for st in strokes:
            assert len(st) >= 2 and all(-0.5 <= x <= 4.5 and -0.5 <= y <= 6.5 for x, y in st), ch
    assert MK.normalise("ab-12 é") == "AB-12 ?"
    w, h = MK.text_size("AB", 6.0)
    assert (w, h) == (10.0, 6.0)


def test_text_is_inside_the_part_and_clear_of_holes():
    part = flange()
    doc = Document(parts=[part])
    res = MK.etch_document(doc, LAYER, margin=1.5, min_height=2, max_height=12)
    assert [m[:2] for m in res.marked] == [(0, "FL-7")] and not res.skipped
    ms = marks(part)
    assert ms and all(not c.closed and c.craft.layer == LAYER and c.craft.lead_in.kind == 0 and c.craft.kerf_comp == -1
                      for c in ms)
    outer = Polygon(list(part.contours[-6].points(0.05)))
    holes = [Polygon(list(c.points(0.05))) for c in part.contours[-5:]]
    for c in ms:
        ln = LineString(list(c.points()))
        assert outer.buffer(-1.5 + 1e-6).contains(ln)
        for h in holes:
            assert ln.distance(h) >= 1.5 - 1e-6
    # the largest height that fits beside the bore (not the minimum)
    assert res.marked[0][2] > 2.0


def test_parts_without_room_are_skipped_and_reported():
    tiny = Part([rect(200, 0, 204, 4)], name="tiny")
    ring = Part([circle(300, 0, 10), circle(300, 0, 9)], name="ring")      # a 1 mm ring: no room
    doc = Document(parts=[flange(), tiny, ring])
    res = MK.etch_document(doc, LAYER, margin=1.0, min_height=2.0, max_height=8.0)
    assert [m[0] for m in res.marked] == [0]
    assert sorted((s[0], s[2]) for s in res.skipped) == [(1, "no room"), (2, "no room")]
    assert not marks(tiny) and not marks(ring)


def test_rotates_into_a_tall_part():
    tall = Part([rect(0, 0, 12, 120)], name="BAR-123")
    res = MK.etch_document(Document(parts=[tall]), LAYER, margin=1.0, min_height=3.0, max_height=8.0)
    assert res.marked and res.marked[0][3] == 90.0


def test_marking_is_cut_before_the_part_is_free_and_replaced_not_doubled():
    part = flange()
    doc = Document(parts=[part], layers={1: LayerParams(1), LAYER: LayerParams(LAYER, power_pct=20.0)})
    MK.etch_document(doc, LAYER)
    n = len(part.contours)
    MK.etch_document(doc, LAYER)                       # again: the old marks are replaced
    assert len(part.contours) == n
    outer_index = n - 6
    for mode in (5, 0, 20):
        tp = build_toolpath(doc, CamParams(sort_mode=mode))
        order = [(m.part, m.contour) for m in tp if type(m).__name__ == "Marker" and m.kind == "contour"]
        assert order[-1] == (0, outer_index), mode            # the outline is cut last
        cut_layers = {m.layer for m in tp if type(m).__name__ in ("CutLine", "CutArc") and m.laser}
        assert LAYER in cut_layers and 1 in cut_layers
    assert MK.remove_marking(doc, LAYER) == n - 6 and len(part.contours) == 6


def test_sheet_outline_and_layer_params():
    sheet = Part([rect(-10, -10, 500, 500)], name="sheet")
    sheet.craft.layer = 0
    lp = LayerParams(LAYER, power_pct=15.0)
    doc = Document(parts=[sheet, flange()])
    res = MK.etch_document(doc, LAYER, layer_params=lp, text_of=lambda i, p: "A-1042")
    assert [m[:2] for m in res.marked] == [(1, "A-1042")]
    assert doc.layers[LAYER] is lp and not marks(sheet)


@pytest.mark.parametrize("text", ["0123456789", "ORDER-77/B#2"])
def test_strokes_scale_with_height(text):
    s1 = MK.text_strokes(text, 6.0)
    s2 = MK.text_strokes(text, 3.0)
    assert len(s1) == len(s2)
    assert s2[0][1] == pytest.approx((s1[0][1][0] / 2, s1[0][1][1] / 2))


# ------------------------------------------------------------------ the application command
@pytest.fixture(scope="module")
def app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_etch_command_is_one_undoable_edit(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    from wiring import real_window
    w = real_window()
    try:
        w.set_document(Document(parts=[flange(), Part([rect(200, 0, 260, 40)], name="b-2")],
                                layers={1: LayerParams(1)}))
        before = sum(len(p.contours) for p in w.document.parts)
        w.commands.run("labels.etch_part_ids")
        ext = w.extension("ext_labels")
        res = ext.last_result
        assert res is not None and len(res.marked) == 2
        layer = int(ext.settings["mark_layer"])
        lp = w.document.layers[layer]
        assert lp.extra.get("marking") and lp.extra["technology"]["key"]["nozzle"] == "marking"
        assert lp.power_pct <= 30 and lp.duty_pct <= 30            # the low-power example fallback
        assert sum(len(p.contours) for p in w.document.parts) == before + res.contours
        w.docs.undo()
        assert sum(len(p.contours) for p in w.document.parts) == before and layer not in w.document.layers
        # part labels of the open document, written without a dialog
        path = ext.part_labels(str(tmp_path / "labels.pdf"))
        assert path and open(path, "rb").read(5) == b"%PDF-"
    finally:
        w.close()
