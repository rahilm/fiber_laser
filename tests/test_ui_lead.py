"""引线 (lead) command: dialog defaults, closed-only targets, start point stored at apply time
(parity items D-17, D-18, D-19; evidence: parity harness ``app_lead`` on basic_r2000.dxf and
CADModule FUN_10071f80)."""
from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from mlaser.cam.ordering import choose_start, contour_glyphs
from mlaser.formats.chf import GLY_LWPOLY, ChfContour, ChfGly, contour_from_chf
from mlaser.model import Arc, Contour, Document, LeadParams, Line, Part
from mlaser.ui import docops


def _lwpoly_contour() -> Contour:
    """One closed LWPOLYLINE glyph: 30 mm line, semicircle r 20 (62.8 mm), 30 mm, 40 mm."""
    verts = [((0.0, 0.0), 0.0), ((30.0, 0.0), 1.0), ((30.0, 40.0), 0.0), ((0.0, 40.0), 0.0)]
    rec = ChfContour(glys=[ChfGly(1, GLY_LWPOLY, {"verts": verts, "flag": 1})])
    return contour_from_chf(rec)


def _rect(x, y, w, h) -> Contour:
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], closed=True)


def test_lwpoly_glyph_starts_in_its_longest_piece():
    c = _lwpoly_contour()
    assert [k for k, _ in contour_glyphs(c)] == [GLY_LWPOLY] and len(c.segments) == 4
    # one glyph -> its 30 mm line is no "line glyph"; middle of the longest piece (the arc)
    assert choose_start(c, 0, 3.0) == pytest.approx(30.0 + math.pi * 20 / 2)
    # the same geometry as separate segments: first line longer than 2*lead
    flat = Contour(list(c.segments), closed=True)
    assert choose_start(flat, 0, 3.0) == pytest.approx(15.0)


def test_circle_and_rectangle_start():
    circ = Contour([Arc((10.0, 0.0), (10.0, 0.0), (0.0, 0.0), True)], closed=True)
    assert choose_start(circ, 0, 3.0) == 0.0
    r = _rect(0, 0, 100, 50)
    assert choose_start(r, 0, 3.0) / r.length() == pytest.approx(1 / 6)    # 0.166667 (O)


def test_lead_defaults_of_fresh_install():
    pytest.importorskip("PySide6")
    from mlaser.ui.mainwindow import default_lead_params
    li, lo = default_lead_params()
    for lp in (li, lo):
        assert (lp.kind, lp.angle_deg, lp.length, lp.radius) == (1, 90.0, 3.0, 2.0)


def test_apply_lead_closed_only_and_stores_start():
    doc = Document(parts=[Part([_rect(0, 0, 100, 50)]),
                          Part([Contour([Line((0, 0), (10, 0))], closed=False)]),
                          Part([_lwpoly_contour()])])
    li = lo = LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0)
    assert docops.apply_lead(doc, li, lo) == 2
    rect, open_, poly = doc.parts
    assert rect.craft.lead_in.kind == 1 and open_.craft.lead_in.kind == 0
    assert rect.craft.extra["start_param"] == pytest.approx(1 / 6)
    L = poly.contours[0].length()
    assert poly.craft.extra["start_param"] == pytest.approx((30.0 + math.pi * 10) / L)
    assert "start_param" not in open_.craft.extra
    # clearing leads applies to open contours too and keeps the stored start
    docops.apply_lead(doc, LeadParams(0), LeadParams(0), False)
    assert all(p.craft.lead_in.kind == 0 for p in doc.parts)
    assert rect.craft.extra["start_param"] == pytest.approx(1 / 6)


def test_apply_lead_multi_contour_part_uses_contour_crafts():
    from mlaser.cam.manual import contour_craft
    part = Part([_rect(0, 0, 100, 50), Contour([Line((0, 0), (5, 5))], closed=False)])
    doc = Document(parts=[part])
    docops.apply_lead(doc, LeadParams(kind=1, length=3.0), LeadParams(kind=1, length=3.0))
    assert contour_craft(part, 0).lead_in.kind == 1
    assert contour_craft(part, 0).extra["start_param"] == pytest.approx(1 / 6)
    assert contour_craft(part, 1).lead_in.kind == 0
