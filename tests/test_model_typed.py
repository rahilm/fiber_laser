"""Typed model fields and the LegacyExtra shims for the old ``extra`` keys (mlaser.model)."""
from __future__ import annotations

import copy
import pickle
import warnings

import pytest

from mlaser.model import (Contour, Craft, Glyph, LegacyExtra, LegacyExtraWarning, Line, MicroJoint, Part,
                          segment_key)


def _rect():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (0.0, 5.0)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def test_old_keys_read_and_write_the_typed_fields_with_a_warning():
    c = Contour([Line((0, 0), (1, 0))], False, {"chf": "record", "depth": 2})
    ov = Craft(layer=3)
    with pytest.warns(LegacyExtraWarning, match=r"Contour.extra\['craft'\] is deprecated: use Contour.craft"):
        c.extra["craft"] = ov
    assert c.craft is ov
    with pytest.warns(LegacyExtraWarning):
        assert c.extra.get("craft") is ov and "craft" in c.extra
    assert dict.__contains__(c.extra, "craft") is False        # stored once, in the typed field
    assert sorted(c.extra) == ["chf", "craft", "depth"] and len(c.extra) == 3
    with pytest.warns(LegacyExtraWarning):
        assert c.extra.pop("craft") is ov
    assert c.craft is None and "craft" not in dict(dict.items(c.extra))

    cr = Craft()
    with pytest.warns(LegacyExtraWarning):
        cr.extra["overcut"] = 0.4
        cr.extra["start_param"] = 0.5
        cr.extra["cool_points"] = [3.0]
        cr.extra["lead_error"] = True
        cr.extra["micro_joint_len"] = 0.9
    assert (cr.overcut, cr.start_param, cr.cool_points, cr.lead_error, cr.joint_width) == (0.4, 0.5, [3.0], True, 0.9)
    p = Part([_rect()])
    with pytest.warns(LegacyExtraWarning):
        p.extra["raster"] = "R"
    assert p.raster == "R"


def test_no_warning_for_the_typed_fields_and_opaque_keys():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        c = _rect()
        c.extra["chf"] = 1
        c.craft = Craft()
        assert c.extra.get("chf") == 1 and c.craft.layer == 1
        assert c.extra.materialize()["craft"] is c.craft


def test_parity_ports_use_the_old_keys_silently(tmp_path):
    from mlaser.formats.chf import load_chf, save_chf
    from mlaser.model import Document, LayerParams

    p = Part([_rect(), _rect()], Craft(layer=1, micro_joints=[2.0]))
    p.contours[1].craft = Craft(layer=2)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        save_chf(Document([p], {1: LayerParams(1)}), str(tmp_path / "a.chf"))
        back = load_chf(str(tmp_path / "a.chf"))
    assert back.parts[0].contours[1].craft.layer == 2
    (j,) = back.parts[0].craft.micro_joints
    assert isinstance(j, MicroJoint) and j.position == pytest.approx(2.0, abs=1e-4) and j.width == 0.5


def test_constructor_extra_with_old_keys_is_routed_after_init():
    cr = Craft(micro_joints=[1.0, (2.0, 0.3)], extra={"micro_joint_len": 0.7, "overcut": 1.0, "pwm_mode": 1})
    assert cr.joint_width == 0.7 and cr.overcut == 1.0
    assert cr.micro_joints == [MicroJoint(1.0, 0.7), MicroJoint(2.0, 0.3)]
    assert dict(dict.items(cr.extra)) == {"pwm_mode": 1}
    c = Contour([], True, {"craft": cr, "x": 1})
    assert c.craft is cr


def test_micro_joint_width_resolution_order():
    """own width > the .chf per-joint width > joint_width > 0.5 mm (cam.toolpath before Wave 3)."""
    cr = Craft(micro_joints=[1.0, 2.0, (3.0, 0.1)])
    assert [j.width for j in cr.micro_joints] == [0.5, 0.5, 0.1]
    cr.joint_width = 0.8
    assert [j.width for j in cr.micro_joints] == [0.8, 0.8, 0.1]
    with pytest.warns(LegacyExtraWarning):
        cr.extra["micro_joint_widths"] = [0.2]
    assert [j.width for j in cr.micro_joints] == [0.2, 0.8, 0.1]
    assert cr.joint_list() == [(1.0, 0.2), (2.0, 0.8), (3.0, 0.1)]
    assert MicroJoint(1.0, 0.2) == (1.0, 0.2)          # still a (pos, width) pair


def test_one_cool_point_list_and_the_deprecated_single_point():
    cr = Craft(cool_points=[4.0])
    assert cr.cool_point is None
    cr.cool_point = (1.0, 2.0)
    assert cr.cool_points == [4.0, (1.0, 2.0)] and cr.cool_point == (1.0, 2.0)
    cr.cool_point = None
    assert cr.cool_points == [4.0]


def test_source_glyphs_use_a_fingerprint_not_a_copy():
    c = _rect()
    c.set_source_glyphs([Glyph(6, {})])
    assert c.glyph_key == segment_key(c.segments) and c.source_glyphs()
    assert isinstance(c.glyph_key, tuple) and not any(isinstance(x, Line) for x in c.glyph_key)
    c.segments[0] = Line((0.0, 0.0), (10.0, 0.001))
    assert c.source_glyphs() is None and c.glyphs
    with pytest.warns(LegacyExtraWarning):
        assert c.extra["glyph_segments"] is None            # the old snapshot no longer matches
    d = Contour(list(c.segments), True)
    with pytest.warns(LegacyExtraWarning):
        d.extra["glyphs"] = [Glyph(2, {})]
        d.extra["glyph_segments"] = list(d.segments)
    assert d.source_glyphs() == [Glyph(2, {})]


def test_copies_pickles_and_equality():
    cr = Craft(layer=2, micro_joints=[1.0], cool_points=[2.0], overcut=0.3)
    c = _rect()
    c.craft = cr
    c.extra["chf"] = ("rec",)
    c.set_source_glyphs([Glyph(1, {"p": (0.0, 0.0)})])
    p = Part([c], Craft(), extra={"note": 1})
    for q in (copy.deepcopy(p), pickle.loads(pickle.dumps(p))):
        assert q == p and q.contours[0].craft is not cr and q.contours[0].craft == cr
        assert type(q.extra) is LegacyExtra and q.extra._ref() is q
        assert q.contours[0].source_glyphs() is not None
        q.contours[0].craft.layer = 5
        assert cr.layer == 2
    s = copy.copy(p)                                  # a shallow copy gets its own view
    s.extra["note"] = 2
    assert s.extra._ref() is s and p.extra["note"] == 1
    standalone = copy.deepcopy(c.extra)               # the pre-Wave-3 dict form
    assert type(standalone) is dict and standalone["craft"] == cr and standalone["chf"] == ("rec",)
    assert c.extra == standalone
    other = _rect()
    other.extra.update(c.extra)                       # dict.update copies the typed values too
    assert other.craft == cr
    assert Craft() == Craft() and Craft(overcut=0.1) != Craft()


def test_assigning_another_objects_extra_copies_its_typed_values():
    a = _rect()
    a.craft = Craft(layer=4)
    b = _rect()
    b.extra = a.extra
    assert b.craft is a.craft and b.extra is not a.extra
