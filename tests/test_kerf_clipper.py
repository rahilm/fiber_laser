"""Kerf fallback through pyclipper + CircleFit arc re-fit (``cam.kerf``, ``fallback="clipper"``).

The default fallback stays shapely (the goldens' jobs_random reach it); these tests cover the
opt-in clipper path: direct cases and hypothesis properties (closed in -> closed out, distance
|d| within tolerance, correct side, no exceptions on self-intersecting / degenerate input).
"""
from __future__ import annotations

import math

import pytest

pytest.importorskip("pyclipper")
shapely_geometry = pytest.importorskip("shapely.geometry")
hyp = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from mlaser.cam import kerf as K  # noqa: E402
from mlaser.cam.toolpath import CamParams  # noqa: E402
from mlaser.geom.contour import is_ccw  # noqa: E402
from mlaser.model import Arc, Contour, Line  # noqa: E402

Polygon = shapely_geometry.Polygon
LineString = shapely_geometry.LineString
SPoint = shapely_geometry.Point

#: flattening chord error + Clipper arc tolerance + 2 * fit verification bound + int rounding
TOL = K.CLIPPER_TOL + 2 * 2 * K.CLIPPER_FIT_ERR + 1e-5


def poly(pts, ccw=True):
    pts = list(pts) if ccw else list(reversed(pts))
    return Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)


def rrect(w, h, r):
    return Contour([
        Line((r, 0), (w - r, 0)), Arc((w - r, 0), (w, r), (w - r, r), True),
        Line((w, r), (w, h - r)), Arc((w, h - r), (w - r, h), (w - r, h - r), True),
        Line((w - r, h), (r, h)), Arc((r, h), (0, h - r), (r, h - r), True),
        Line((0, h - r), (0, r)), Arc((0, r), (r, 0), (r, r), True)], True)


SLOT = poly([(0, 0), (20, 0), (20, 10), (10.25, 10), (10.25, 3), (9.75, 3), (9.75, 10), (0, 10)])


def samples(c: Contour, step: float = 0.05):
    out = []
    for s in c.segments:
        n = max(1, int(math.ceil(s.length() / step)))
        out.extend(s.point_at(i / n) if isinstance(s, Arc) else
                   (s.start[0] + (s.end[0] - s.start[0]) * i / n,
                    s.start[1] + (s.end[1] - s.start[1]) * i / n) for i in range(n))
    return out


def assert_closed_chain(c: Contour):
    assert c.closed and c.segments
    for a, b in zip(c.segments, c.segments[1:] + c.segments[:1]):
        assert math.dist(a.end, b.start) < 1e-6


def check_offset(c: Contour, d: float, res: Contour):
    """Every sample of ``res`` lies |d| from the input boundary, on the side d asks for."""
    assert_closed_chain(res)
    assert is_ccw(res) == is_ccw(c)
    P = Polygon(list(c.points(0.0005)))
    ring = P.exterior
    grow = (d > 0) == is_ccw(c)
    for q in samples(res):
        dist = ring.distance(SPoint(q))
        assert abs(dist - abs(d)) <= TOL, (q, dist, d)
        assert P.contains(SPoint(q)) != grow, q


# ----------------------------------------------------------------- selection
def test_default_fallback_is_shapely_and_cam_flag_off():
    assert K.DEFAULT_FALLBACK == "shapely"
    assert CamParams().kerf_fallback is None           # -> DEFAULT_FALLBACK
    with pytest.raises(ValueError):
        K.compensate(SLOT, 3, 0.4, fallback="bogus")


def test_fallback_unused_when_analytic_offset_succeeds():
    sq = poly([(0, 0), (10, 0), (10, 10), (0, 10)])
    a = K.compensate(sq, 1, 0.1)
    b = K.compensate(sq, 1, 0.1, fallback="clipper")
    assert a == b


# ----------------------------------------------------------------- direct cases
def test_clipper_slot_closes_with_arcs():
    """Growing a 0.5 mm slot by 0.4 makes the analytic offset fail; clipper keeps round joins
    as Arcs (shapely gives a polyline of ~80 lines)."""
    shp = K.compensate(SLOT, 3, 0.4, fallback="shapely")
    res = K.compensate(SLOT, 3, 0.4, fallback="clipper")
    assert all(isinstance(s, Line) for s in shp.segments)
    arcs = [s for s in res.segments if isinstance(s, Arc)]
    assert arcs                                        # round joins re-fitted (biarcs)
    assert len(res.segments) < len(shp.segments) / 2
    check_offset(SLOT, 0.4, res)
    # start: nearest the analytic start (parent start moved by d along the right normal)
    assert res.segments[0].start == pytest.approx((0.0, -0.4), abs=1e-6)


def test_clipper_rounded_rect_shrink_past_corner_radius():
    rr = rrect(20, 20, 0.5)
    assert K.offset_contour(rr, -1.0) is None           # corner arcs vanish
    res = K.compensate(rr, 2, 1.0, fallback="clipper")
    check_offset(rr, -1.0, res)
    assert res.length() == pytest.approx(72.0, abs=0.05)
    assert res.segments[0].start == pytest.approx((1.0, 1.0), abs=1e-6)


def test_clipper_circle_stays_one_arc():
    c = Contour([Arc((5, 0), (5, 0), (0, 0), True)], True)
    res = K._clipper_offset(c, 0.3)
    assert len(res.segments) == 1 and isinstance(res.segments[0], Arc)
    assert res.segments[0].radius == pytest.approx(5.3, abs=K.CLIPPER_TOL + K.CLIPPER_FIT_ERR)
    cw = Contour([Arc((5, 0), (5, 0), (0, 0), False)], True)
    res = K._clipper_offset(cw, 0.3)                   # right of a CW loop = inside
    assert res.segments[0].radius == pytest.approx(4.7, abs=K.CLIPPER_TOL + K.CLIPPER_FIT_ERR)
    assert not is_ccw(res)


def test_clipper_cw_input_keeps_direction():
    cw = poly([(0, 0), (20, 0), (20, 10), (10.25, 10), (10.25, 3), (9.75, 3), (9.75, 10), (0, 10)],
              ccw=False)
    res = K.compensate(cw, 3, 0.4, fallback="clipper")
    check_offset(cw, -0.4, res)


def test_clipper_vanishing_shrink_returns_none():
    sq = poly([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert K._clipper_offset(sq, -0.6) is None


def test_toolpath_uses_cam_flag(monkeypatch):
    from mlaser.cam import toolpath as T
    from mlaser.model import Craft
    seen = []
    real = K.compensate

    def spy(*a, **kw):
        seen.append(kw.get("fallback"))
        return real(*a, **kw)
    monkeypatch.setattr(T, "compensate", spy)
    craft = Craft(kerf_comp=3, kerf_width=0.4)
    pc = T.prepare_contour(SLOT, craft, 1, CamParams(kerf_fallback="clipper"))
    assert seen == ["clipper"] and pc is not None
    assert any(isinstance(s, Arc) for s, _ in pc.pieces)


# ----------------------------------------------------------------- properties
def _star(n, radii, phase, cx=0.0, cy=0.0):
    return [(cx + r * math.cos(phase + 2 * math.pi * k / n), cy + r * math.sin(phase + 2 * math.pi * k / n))
            for k, r in zip(range(n), radii)]


@st.composite
def star_polygons(draw):
    n = draw(st.integers(3, 14))
    radii = draw(st.lists(st.floats(2.0, 20.0), min_size=n, max_size=n))
    phase = draw(st.floats(0, 2 * math.pi))
    ccw = draw(st.booleans())
    return poly(_star(n, radii, phase), ccw)


@st.composite
def rounded_shapes(draw):
    w = draw(st.floats(5, 40))
    h = draw(st.floats(5, 40))
    r = draw(st.floats(0.05, min(w, h) / 2 - 0.1))
    c = rrect(w, h, r)
    if draw(st.booleans()):
        c = Contour([s.reversed() for s in reversed(c.segments)], True)
    return c


PROP = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROP
@given(star_polygons(), st.floats(0.02, 1.5), st.booleans())
def test_prop_star_polygons(c, w, grow):
    d = w if grow == is_ccw(c) else -w
    res = K._clipper_offset(c, d)
    if res is None:
        assert not grow                                # only a shrink may vanish
        return
    check_offset(c, d, res)


@PROP
@given(rounded_shapes(), st.floats(0.02, 2.0), st.booleans())
def test_prop_rounded_shapes(c, w, grow):
    d = w if grow == is_ccw(c) else -w
    res = K._clipper_offset(c, d)
    if res is None:
        assert not grow
        return
    check_offset(c, d, res)


@PROP
@given(st.lists(st.tuples(st.floats(-10, 10), st.floats(-10, 10)), min_size=0, max_size=12),
       st.floats(-2.0, 2.0), st.integers(0, 3))
def test_prop_self_intersecting_and_degenerate(pts, d, mode):
    """Random (often self-intersecting, collinear or repeated) point loops: never raises, and a
    result is a closed continuous Line/Arc chain."""
    if len(pts) >= 2:
        c = Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)
    else:
        c = Contour([], True)
    if c.segments:
        res = K._clipper_offset(c, d) if abs(d) > 1e-6 else None
        if res is not None:
            assert_closed_chain(res)
            assert all(isinstance(s, (Line, Arc)) for s in res.segments)
    res2 = K.compensate(c, mode, abs(d), fallback="clipper")
    if res2 is not None:
        # the analytic offset may answer first; its tangent joins keep each piece's own ends
        # (gaps up to 0.06 |d|, parity JC-04), so only the closed flag is checked here
        assert res2.closed and res2.segments
