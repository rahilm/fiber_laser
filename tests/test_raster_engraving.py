"""Raster engraving object (mlaser.cam.raster_engraving): identity with today's per-segment
Parts (``parse_gcode`` of the raster G-code) for the burn lines, the MotionItems, the compiled
wire and frames, and the .chf bytes; frame placement under document edits."""
import copy
import math
import os
import struct

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mlaser.cam import raster_engraving as R
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.engrave.raster import (RasterParams, format_f3, gcode_from_bitmap, process_image,
                                   raster_from_bitmap)
from mlaser.io.gcode import parse_gcode
from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
from mlaser.toolpath.items import Marker


def _image(h=40, w=60, seed=1):
    rng = np.random.default_rng(seed)
    img = (np.indices((h, w)).sum(0) * 4 % 255).astype(int)
    return np.clip(img + rng.integers(-40, 40, img.shape), 0, 255).astype(np.uint8)


CASES = [("none", 10, 6), ("halftone", 10, 6), ("dither", 10, 6), ("dither", 13, 7),
         ("halftone", 8, 10), ("none", 7, 3)]


def _both(mode, width, rho, power=20, img=None):
    p = RasterParams(mode=mode, width_mm=width, rho=rho, power=power)
    bmp = process_image(_image() if img is None else img, p)
    old = parse_gcode(gcode_from_bitmap(bmp, p.rho, p.power))
    r = raster_from_bitmap(bmp, p.rho, p.power, p)
    new = Document(parts=[R.make_raster_part(r)]) if len(r) else Document()
    return old, new, r


def _bits(x):
    """Bitwise-comparable form of MotionItems / segments (floats as hex, -0.0 kept)."""
    if isinstance(x, float):
        return struct.pack("<d", x).hex()
    if isinstance(x, (list, tuple)):
        return type(x).__name__, tuple(_bits(v) for v in x)
    if isinstance(x, dict):
        return tuple(sorted((k, _bits(v)) for k, v in x.items()))
    if hasattr(x, "__dict__"):
        return type(x).__name__, tuple(sorted((k, _bits(v)) for k, v in vars(x).items()))
    return x


def _norm(items, raster_pi=0):
    """Markers of the raster part (pi, k) -> today's (k, 0)."""
    out = []
    for m in items:
        if isinstance(m, Marker) and m.part == raster_pi and m.kind == "contour":
            m = Marker(m.kind, m.contour, 0, m.info)
        out.append(m)
    return out


@settings(max_examples=300, deadline=None)
@given(st.floats(min_value=-1e5, max_value=1e5, allow_nan=False, width=32))
def test_f3_values_match_format_f3(v):
    x = np.float32(v)
    got = R.f3_values(np.array([x]))[0]
    want = float(format_f3(x))
    assert struct.pack("<d", got) == struct.pack("<d", want)


def test_f3_values_half_cases():
    xs = np.array([0.0005, -0.0005, 1.0005, 2.4995, -0.0004, 12.3455, -5.0, 0.0], np.float32)
    assert [float(format_f3(x)) for x in xs] == R.f3_values(xs).tolist()
    assert math.copysign(1, R.f3_values(np.float32(-0.0004))) < 0


@pytest.mark.parametrize("mode,width,rho", CASES)
def test_burn_lines_identical(mode, width, rho):
    old, new, r = _both(mode, width, rho)
    assert len(r) == len(old.parts) > 0
    exp = R.expand_document(new)
    assert _bits([p.contours[0].segments for p in exp.parts]) == \
        _bits([p.contours[0].segments for p in old.parts])
    assert [p.contours[0].closed for p in exp.parts] == [p.contours[0].closed for p in old.parts]
    assert [p.name for p in exp.parts] == [p.name for p in old.parts]


def test_zero_and_negative_power_burn_nothing():
    for pw in (0, -3):
        old, new, r = _both("dither", 10, 6, power=pw)
        assert len(r) == 0 and not old.parts
        with pytest.raises(ValueError):
            R.make_raster_part(r)


@pytest.mark.parametrize("mode,width,rho", CASES)
def test_motion_items_identical(mode, width, rho):
    old, new, _ = _both(mode, width, rho)
    a = build_toolpath(old, CamParams(sort_mode=0))
    b = build_toolpath(new, CamParams(sort_mode=0))
    assert _bits(_norm(b)) == _bits(a)


@pytest.mark.parametrize("prm", [
    CamParams(sort_mode=0, start_delay_s=0.1, end_delay_s=0.05, pierce_time_s=0.3),
    CamParams(sort_mode=0, origin=(20.0, -3.0), return_to_origin=True),
    CamParams(sort_mode=0, leads_on_open=True),
])
def test_motion_items_identical_cam_params(prm):
    old, new, _ = _both("dither", 10, 6)
    for d in (old, new):
        d.layers[1] = LayerParams(index=1, speed=250.0, pierce={"time_s": 0.2, "stages": [{"time_s": 0.1}]})
    assert _bits(_norm(build_toolpath(new, prm))) == _bits(build_toolpath(old, prm))


def test_fast_path_equals_generic_pipeline():
    _, new, _ = _both("halftone", 10, 6)
    prm = CamParams(sort_mode=0)
    fast, pos_f, lay_f = R.raster_motion_items(new, 0, prm, (0.0, 0.0), None)
    gen, pos_g, lay_g = R._generic_items(new, 0, prm, (0.0, 0.0), None)
    assert _bits(fast) == _bits(gen) and pos_f == pos_g and lay_f == lay_g


def test_non_plain_craft_uses_generic_pipeline():
    old, new, _ = _both("none", 10, 6)
    craft = Craft(layer=2, micro_joints=[0.5])
    craft.extra["overcut"] = -0.2
    new.parts[0].craft = copy.deepcopy(craft)
    for p in old.parts:
        p.craft = copy.deepcopy(craft)
    assert R._plain(craft, CamParams()) is False
    assert _bits(_norm(build_toolpath(new, CamParams(sort_mode=0)))) == \
        _bits(build_toolpath(old, CamParams(sort_mode=0)))


def test_mixed_document_raster_after_contours():
    """Other parts first, raster outside them: the same as today's document order (sort 0)."""
    old, new, _ = _both("dither", 10, 6)
    sq = Part(contours=[Contour([Line((-30, 0), (-20, 0)), Line((-20, 0), (-20, 10)),
                                 Line((-20, 10), (-30, 10)), Line((-30, 10), (-30, 0))])])
    old.parts.insert(0, copy.deepcopy(sq))
    new.parts.insert(0, copy.deepcopy(sq))
    a = build_toolpath(old, CamParams(sort_mode=0))
    b = build_toolpath(new, CamParams(sort_mode=0))
    nb = [Marker(m.kind, m.contour + 1, 0, m.info) if isinstance(m, Marker) and m.part == 1 else m
          for m in b]
    assert _bits(nb) == _bits(a)


def test_raster_order_independent_of_sort_mode():
    _, new, _ = _both("dither", 10, 6)
    a = build_toolpath(new, CamParams(sort_mode=0))
    for mode in (1, 5, 20):
        assert _bits(build_toolpath(new, CamParams(sort_mode=mode))) == _bits(a)


@pytest.mark.parametrize("mode,width,rho", [("none", 10, 6), ("halftone", 10, 6), ("dither", 9, 5)])
def test_compiled_wire_and_frames_identical(mode, width, rho):
    from mlaser.nc import compile_job
    from mlaser.nc.machine import Machine
    old, new, _ = _both(mode, width, rho, img=_image(24, 36))
    ja = compile_job(old, CamParams(sort_mode=0), Machine())
    jb = compile_job(new, CamParams(sort_mode=0), Machine())
    assert ja.steps == jb.steps > 0
    assert ja.wire == jb.wire
    assert ja.frames() == jb.frames()
    assert [m[0] for m in ja.markers] == [m[0] for m in jb.markers]
    assert ja.cut_blocks == jb.cut_blocks


@pytest.mark.skipif(not os.environ.get("MLASER_SLOW"), reason="slow: set MLASER_SLOW=1")
def test_compiled_identity_large_image():
    from mlaser.nc import compile_job
    from mlaser.nc.machine import Machine
    img = (np.indices((150, 200)).sum(0) % 255).astype(np.uint8)
    for mode in ("none", "halftone", "dither"):
        old, new, _ = _both(mode, 25, 8, img=img)
        assert _bits(_norm(build_toolpath(new, CamParams(sort_mode=0)))) == \
            _bits(build_toolpath(old, CamParams(sort_mode=0)))
        ja = compile_job(old, CamParams(sort_mode=0), Machine())
        jb = compile_job(new, CamParams(sort_mode=0), Machine())
        assert ja.wire == jb.wire and ja.frames() == jb.frames()


def test_chf_bytes_identical_after_expand():
    from mlaser.formats.chf import dumps_chf
    old, new, _ = _both("dither", 10, 6)
    assert dumps_chf(R.expand_document(new)) == dumps_chf(old)


def test_pack_parts_roundtrip():
    old, _, r = _both("halftone", 10, 6)
    q = R.raster_from_parts(old.parts)
    assert q is not None and len(q) == len(r)
    assert _bits(q.x0.tolist()) == _bits(r.x0.tolist()) and _bits(q.y.tolist()) == _bits(r.y.tolist())
    doc = Document(parts=[R.make_raster_part(q)])
    assert _bits(_norm(build_toolpath(doc, CamParams(sort_mode=0)))) == \
        _bits(build_toolpath(old, CamParams(sort_mode=0)))
    bad = copy.deepcopy(old.parts)
    bad[0].craft.layer = 3
    assert R.raster_from_parts(bad) is None


def test_deepcopy_shares_arrays_and_survives():
    _, new, r = _both("dither", 10, 6)
    d2 = copy.deepcopy(new)
    r2 = R.raster_of_part(d2.parts[0])
    assert r2 is not r and r2.x0 is r.x0 and not r.x0.flags.writeable
    assert d2.parts[0].contours[0] is not new.parts[0].contours[0]
    assert _bits(build_toolpath(d2, CamParams(sort_mode=0))) == _bits(build_toolpath(new, CamParams(sort_mode=0)))
    import pickle
    r3 = pickle.loads(pickle.dumps(r))
    assert np.array_equal(r3.x1, r.x1) and not r3.x1.flags.writeable


def test_bbox_is_frame():
    _, new, r = _both("dither", 10, 6)
    x0, y0, x1, y1 = new.bbox()
    assert (x0, y0, x1, y1) == r.frame
    assert x0 == min(r.x0.min(), r.x1.min()) and y1 == r.row_y.max()


def _old_transformed(old, fn):
    from mlaser.ui import docops
    d = copy.deepcopy(old)
    fn(docops, d)
    return [(s.start, s.end) for p in d.parts for s in p.contours[0].segments]


@pytest.mark.parametrize("op", ["translate", "rotate", "mirror_h", "mirror_v", "scale", "reverse"])
def test_docops_transforms(op):
    from mlaser.ui import docops
    old, new, r = _both("halftone", 10, 6)
    ops = {
        "translate": lambda D, d: D.translate(d, 12.5, -3.25, list(d.parts)),
        "rotate": lambda D, d: D.rotate(d, 30.0),
        "mirror_h": lambda D, d: D.mirror(d, True),
        "mirror_v": lambda D, d: D.mirror(d, False),
        "scale": lambda D, d: D.scale(d, 1.5),
        "reverse": lambda D, d: D.reverse(d),
    }
    fn = ops[op]
    d = copy.deepcopy(new)
    if op in ("rotate", "mirror_h", "mirror_v", "scale"):
        # docops transforms about the bbox centre: the per-segment document has the same bbox
        # as the frame except possibly the frame's single-row padding (not the case here)
        assert old.bbox() == new.bbox()
    fn(docops, d)
    sx, sy, ex, ey = R.segment_arrays(d.parts[0])
    if op == "reverse":          # reversing the frame does not change the engraving
        assert _bits(sx.tolist()) == _bits(r.x0.tolist())
        return
    want = _old_transformed(old, fn)
    got = list(zip(zip(sx.tolist(), sy.tolist()), zip(ex.tolist(), ey.tolist())))
    assert len(got) == len(want)
    err = max(max(math.dist(a, c), math.dist(b, e)) for (a, b), (c, e) in zip(got, want))
    assert err < 1e-9
    if op == "translate":
        items = build_toolpath(d, CamParams(sort_mode=0))
        assert sum(1 for m in items if type(m).__name__ == "CutLine") == len(r)


def test_restarted_frame_keeps_placement():
    _, new, r = _both("dither", 10, 6)
    c = new.parts[0].contours[0]
    c.segments = c.segments[2:] + c.segments[:2]      # start point moved to another vertex
    assert R.placement(new.parts[0]) is None


def test_edited_frame_is_refused():
    _, new, _ = _both("dither", 10, 6)
    c = new.parts[0].contours[0]
    s = c.segments[2]
    c.segments[2] = Line(s.start, (s.end[0] + 3.0, s.end[1] + 1.0))
    c.segments[3] = Line((s.end[0] + 3.0, s.end[1] + 1.0), c.segments[3].end)
    with pytest.raises(R.RasterFrameError):
        build_toolpath(new, CamParams(sort_mode=0))
    new.parts[0].contours.append(Contour([Line((0, 0), (1, 0))], False))
    with pytest.raises(R.RasterFrameError):
        R.placement(new.parts[0])


def test_single_row_frame_has_height():
    img = np.full((1, 30), 255, np.uint8)
    img[0, 5:20] = 0
    bmp = process_image(img, RasterParams(width_mm=5, height_mm=1, rho=1))
    bmp = bmp[:1]
    r = raster_from_bitmap(bmp, 1, 20)
    part = R.make_raster_part(r)
    x0, y0, x1, y1 = part.bbox()
    assert y1 - y0 == 1.0 and R.placement(part) is None
