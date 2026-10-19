"""The fast ordering drop-ins give the reference's output bit for bit.

* ``parity_ports.cadmodule.ordering_fast`` (default profile) vs the statement-level ports in
  ``parity_ports.cadmodule.ordering`` (profile ``"original-reference"``);
* ``cam.ordering.build_tree`` (STRtree filter) vs ``build_tree_reference``;
* ``geom.curves.flatten_points`` / ``flatten_cached`` vs ``Contour.points``.

Inputs come from hypothesis, with coordinates drawn from small grids as well as floats, so
ties, duplicates, shared bbox edges, degenerate (zero-size) boxes and collinear gaps occur.
The goldens (tests/golden/orders.json, 660 cases) and the parity run cover the real pipeline.
"""
from __future__ import annotations

import math
import random

import pytest

hyp = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from mlaser.cam.ordering import build_tree, build_tree_reference  # noqa: E402
from mlaser.geom.curves import flatten_cached, flatten_points  # noqa: E402
from mlaser.model import Arc, Contour, Line  # noqa: E402
from mlaser.parity_ports import ORIGINAL, get_profile  # noqa: E402
from mlaser.parity_ports.cadmodule import ordering as R  # noqa: E402
from mlaser.parity_ports.cadmodule import ordering_fast as F  # noqa: E402
import mlaser.parity_ports.profiles.reference  # noqa: E402,F401  (registers "original-reference")

SET = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

coord = st.one_of(st.integers(-6, 6).map(float), st.floats(-200, 200, allow_nan=False),
                  st.sampled_from([0.0, 0.5, 1e-9, -1e-9, 1e-6, 100.0]))
size = st.one_of(st.just(0.0), st.integers(0, 4).map(float), st.floats(0, 60, allow_nan=False))


@st.composite
def elems(draw, max_n=60):
    n = draw(st.integers(0, max_n))
    closed_p = draw(st.sampled_from([0.0, 0.5, 1.0]))
    out = []
    for i in range(n):
        x, y = draw(coord), draw(coord)
        w, h = draw(size), draw(size)
        closed = draw(st.floats(0, 1)) < closed_p
        ex, ey = x + draw(st.sampled_from([0.0, 1.0, -1.0])) * w, y + draw(st.sampled_from([0.0, 1.0, -1.0])) * h
        out.append(R.SortElem(i, (x - w, y - h, x + w, y + h), closed, (x, y), (ex, ey) if not closed else (0.0, 0.0)))
    return out


def _key(es):
    return [(e.index, e.start, e.end, e.reversed, e.closed, e.bbox) for e in es]


@SET
@given(elems(), st.booleans(), st.booleans(), st.integers(0, 3))
def test_local_optimal_elems_matches_reference(es, small_first, fix_first, seed):
    a = R.local_optimal_elems([e.copy() for e in es], small_first, fix_first, seed)
    b = F.local_optimal_elems([e.copy() for e in es], small_first, fix_first, seed)
    assert _key(a) == _key(b)


@SET
@given(elems())
def test_containers_last_matches_reference(es):
    assert [id(x) for x in R.containers_last_faithful(es)] == [id(x) for x in F.containers_last_faithful(es)]


@pytest.mark.parametrize("n,closed,clusters", [(1500, 1.0, 1), (3000, 0.0, 1), (3000, 0.0, 4), (2000, 0.5, 2)])
def test_large_inputs_match_reference(n, closed, clusters):
    """Sizes where the grid rings, the brute-force fallback and the gap filters all run."""
    rnd = random.Random(n + clusters)
    es = []
    for i in range(n):
        x, y = rnd.randrange(clusters) * 4000.0 + rnd.uniform(0, 300), rnd.uniform(0, 300)
        c = rnd.random() < closed
        es.append(R.SortElem(i, (x - 2, y - 2, x + 2, y + 2), c, (x, y), (x + rnd.uniform(-3, 3), y) if not c else (0.0, 0.0)))
    assert _key(R.local_optimal_elems(es, False)) == _key(F.local_optimal_elems(es, False))
    assert _key(R.local_optimal_elems(es, True)) == _key(F.local_optimal_elems(es, True))


# ----------------------------------------------------------------------------- contours
def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def _circ(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


@st.composite
def contours(draw):
    out = []
    for _ in range(draw(st.integers(0, 40))):
        x, y = draw(st.integers(0, 6)) * 5.0, draw(st.integers(0, 6)) * 5.0
        kind = draw(st.integers(0, 3))
        if kind == 0:
            out.append(_rect(x, y, draw(st.sampled_from([1.0, 5.0, 10.0, 20.0])), draw(st.sampled_from([1.0, 5.0, 10.0, 30.0]))))
        elif kind == 1:
            out.append(_circ(x, y, draw(st.sampled_from([0.5, 2.0, 5.0, 12.0]))))
        elif kind == 2:
            out.append(Contour([Line((x, y), (x + draw(coord) % 7, y + draw(coord) % 7))], False))
        else:                       # zero-size and duplicate boxes
            out.append(_rect(x, y, 0.0, 0.0) if draw(st.booleans()) else _rect(0.0, 0.0, 10.0, 10.0))
    return out


# (empty contours are excluded: the reference raises on them, build_toolpath never passes any)
def _tree_sig(nodes):
    ix = {id(n): i for i, n in enumerate(nodes)}
    return [(n.bbox, n.area, n.depth, n.entry, n.exit, ix.get(id(n.parent)), [ix[id(c)] for c in n.children])
            for n in nodes]


@SET
@given(contours())
def test_build_tree_matches_reference(cs):
    assert _tree_sig(build_tree(cs)) == _tree_sig(build_tree_reference(cs))


@SET
@given(contours())
def test_import_sort_matches_reference(cs):
    ref = get_profile("original-reference").ordering.import_sort
    a_nodes, b_nodes = build_tree(cs), build_tree(cs)
    a, fa = ref(a_nodes, True, 0)
    b, fb = ORIGINAL.ordering.import_sort(b_nodes, True, 0)
    ia = {id(n): i for i, n in enumerate(a_nodes)}
    ib = {id(n): i for i, n in enumerate(b_nodes)}
    assert [ia[id(n)] for n in a] == [ib[id(n)] for n in b]
    assert sorted(ia[i] for i in fa) == sorted(ib[i] for i in fb)


@SET
@given(st.lists(st.tuples(coord, coord, coord, coord, st.booleans(), st.booleans()), max_size=6),
       st.sampled_from([0.02, 0.01, 0.05, 1e-9, 2.0]))
def test_flatten_matches_contour_points(raw, tol):
    segs = []
    for x0, y0, dx, dy, is_arc, ccw in raw:
        if is_arc:
            c = (x0 + dx % 9, y0 + dy % 9)
            r = math.dist(c, (x0, y0))
            segs.append(Arc((x0, y0), (c[0] - r, c[1]) if dx > 0 else (x0, y0), c, ccw))
        else:
            segs.append(Line((x0, y0), (x0 + dx, y0 + dy)))
    c = Contour(segs, True)
    ref = list(c.points(tol))
    assert flatten_points(c, tol) == ref
    assert flatten_cached(c, tol) == ref
    assert flatten_cached(c, tol) == ref            # cache hit


def test_flatten_cache_sees_in_place_edits():
    c = _circ(0, 0, 5)
    first = list(flatten_cached(c, 0.02))
    c.segments[0].center = (1.0, 0.0)               # in-place edit of the segment
    c.segments[0].start = c.segments[0].end = (6.0, 0.0)
    assert flatten_cached(c, 0.02) == list(c.points(0.02)) != first


def test_default_profile_runs_the_drop_ins():
    assert ORIGINAL.ordering.local_optimal_elems is F.local_optimal_elems
    assert get_profile("original-reference").ordering.local_optimal_elems is R.local_optimal_elems
