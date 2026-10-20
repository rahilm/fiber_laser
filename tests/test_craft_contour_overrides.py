"""Kerf, micro-joints and cooling points reach contours that have their own craft.

A lead on a part with holes gives every contour its own craft (a copy of the part craft, so
each keeps its start); the toolpath uses that craft instead of the part's.  ops.apply_kerf,
auto_microjoints and cool_auto set only the part craft, so a kerf (or joints, cooling points)
set after the leads did nothing on such parts: one reason "compensate doesn't work".
"""
from __future__ import annotations

from mlaser import ops
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.model import Contour, Craft, Document, LeadParams, Line, Part
from mlaser.ops.cam import CoolParams, cool_auto, cool_clear


def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def plate() -> Document:
    d = Document([Part([_rect(20, 10, 20, 20), _rect(0, 0, 100, 60)], Craft(layer=1))])
    for p in d.parts:
        p.selected = True
    return d


def _cut_bbox(doc):
    xs = [v[0] for m in build_toolpath(doc, CamParams()) if type(m).__name__ == "CutLine"
          and getattr(m, "lead", None) is None for v in (m.start, m.end)]
    return min(xs), max(xs)


def test_kerf_after_leads_reaches_every_contour():
    d = plate()
    ops.apply_lead(d, LeadParams(kind=1, length=3.0), LeadParams(kind=0))
    assert all(c.craft is not None for c in d.parts[0].contours)       # per-contour crafts
    before = _cut_bbox(d)
    ops.apply_kerf(d, 1, 0.5)
    assert all(c.craft.kerf_comp == 1 and c.craft.kerf_width == 0.5 for c in d.parts[0].contours)
    after = _cut_bbox(d)
    assert after[1] - after[0] > before[1] - before[0]                  # the outer contour grew


def test_joints_and_cooling_after_leads_reach_the_contour_craft():
    d = plate()
    ops.apply_lead(d, LeadParams(kind=1, length=3.0), LeadParams(kind=0))
    n = ops.auto_microjoints(d, ops.MicroJointParams(count=4, length=0.8))
    outer = d.parts[0].contours[1]
    assert n == 4 and len(outer.craft.micro_joints) == 4
    gaps = [m for m in build_toolpath(d, CamParams()) if type(m).__name__ == "CutLine" and not m.laser]
    assert gaps
    ops.clear_joints(d)
    assert not outer.craft.micro_joints and not d.parts[0].craft.micro_joints
    d.parts[0].contours.reverse()                    # cool_auto works on the first contour
    assert cool_auto(d, CoolParams(sharp=True, max_angle=120.0)) == 4
    assert len(d.parts[0].contours[0].craft.cool_points) == 4
    cool_clear(d)
    assert not d.parts[0].contours[0].craft.cool_points
