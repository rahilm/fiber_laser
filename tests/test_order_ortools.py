"""Sort mode 20 on OR-Tools (optional profile "modern-ortools")."""
from __future__ import annotations

import math
import random

import pytest

pytest.importorskip("ortools")

from mlaser.cam.order_ortools import shortest_path_ortools  # noqa: E402
from mlaser.cam.ordering import _small_split, build_tree, order_nodes, rapid_length, shortest_path  # noqa: E402
from mlaser.cam.toolpath import CamParams, build_toolpath  # noqa: E402
from mlaser.model import Arc, Contour, Document, Line, Part  # noqa: E402
from mlaser.parity_ports import use_profile  # noqa: E402


def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def _circ(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def _nodes(seed=0, n=40):
    rnd = random.Random(seed)
    cs = []
    for _ in range(n):
        x, y = rnd.uniform(0, 400), rnd.uniform(0, 300)
        cs += [_rect(x, y, 20, 20), _circ(x + 10, y + 10, 4)]
        if rnd.random() < 0.3:
            cs.append(_circ(x + 10, y + 10, 1.5))      # a part inside the hole
    return build_tree(cs)


def _children_first(order):
    seen = set()
    for x in order:
        assert all(id(c) in seen for c in x.children), "a contour came before one of its children"
        seen.add(id(x))


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_precedence_and_quality(seed):
    nodes = _nodes(seed)
    out = shortest_path_ortools(nodes, (0.0, 0.0), time_limit=1.0, fallback=False)
    assert sorted(map(id, out)) == sorted(map(id, nodes))
    _children_first(out)
    ref = shortest_path(nodes, (0.0, 0.0), time_limit=1.0)
    assert rapid_length(out) <= rapid_length(ref) + 1e-6


def test_small_first_partition():
    nodes = _nodes(3)
    big = _rect(-500, -500, 2000, 10)                  # one long item, the rest are "small"
    nodes = build_tree([n.contour for n in nodes] + [big])
    out = shortest_path_ortools(nodes, small_first=True, time_limit=0.5, fallback=False)
    _children_first(out)
    small, _large = _small_split([x for x in nodes if x.parent is None])
    assert small
    sm = set()
    st = list(small)
    while st:
        y = st.pop(); sm.add(id(y)); st.extend(y.children)
    flags = [id(x) in sm for x in out]
    assert flags == sorted(flags, reverse=True)       # all small ones first


def test_profile_selects_ortools(monkeypatch):
    import mlaser.cam.order_ortools as O
    calls = []
    real = O.shortest_path_ortools
    monkeypatch.setattr(O, "shortest_path_ortools", lambda *a, **k: calls.append(1) or real(*a, **k))
    doc = Document(parts=[Part([_circ(10 * i + 5, 5, 2), _rect(10 * i, 0, 9, 9)]) for i in range(6)])
    base = build_toolpath(doc, CamParams(sort_mode=20))
    assert not calls
    with use_profile("modern-ortools"):
        items = build_toolpath(doc, CamParams(sort_mode=20))
    assert calls and len(items) == len(base)
    assert order_nodes(_nodes(4, 5), 20)                # default profile: built-in solver


def test_missing_ortools_falls_back(monkeypatch):
    import mlaser.cam.order_ortools as O
    def boom():
        raise O.OrToolsMissing("no")
    monkeypatch.setattr(O, "_import", boom)
    nodes = _nodes(5, 8)
    out = O.shortest_path_ortools(nodes, time_limit=0.2)
    _children_first(out)
    with pytest.raises(O.OrToolsMissing):
        O.shortest_path_ortools(nodes, fallback=False)
    assert math.isfinite(rapid_length(out))
