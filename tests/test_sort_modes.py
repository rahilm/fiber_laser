"""Cutting-order modes 0, 5, 6/7, 20 (shortest), 21 (grid), rapid avoidance and doc reorders."""
import math
import random

import pytest
from shapely.geometry import LineString, Polygon

from mlaser.cam import ordering as O
from mlaser.cam.ordering import build_tree, order_nodes, rapid_length
from mlaser.cam.toolpath import CamParams, Rapid, build_toolpath
from mlaser.model import Arc, Contour, Craft, Document, Line, Part


def rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def circle(cx, cy, r, ccw=True):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), ccw)], True)


def scattered(n=60, seed=3):
    rnd = random.Random(seed)
    cs = []
    for _ in range(n):
        x, y = rnd.uniform(0, 1000), rnd.uniform(0, 600)
        cs.append(rect(x, y, 10, 10))
    return cs


def valid_precedence(order):
    pos = {id(n): i for i, n in enumerate(order)}
    return all(pos[id(c)] < pos[id(n)] for n in order for c in n.children)


def plates():
    """Three plates with holes and a part inside one hole."""
    cs = []
    for k in range(3):
        x = 300 * k
        cs += [rect(x, 0, 200, 150), circle(x + 50, 75, 20, False), circle(x + 150, 75, 30, False)]
    cs.append(circle(150, 75, 8))          # part inside the second hole of plate 0
    return cs


def test_rapid_length_improves_vs_document_order():
    cs = scattered()
    naive = rapid_length(order_nodes(build_tree(cs), O.MODE_KEEP))
    for mode in (O.MODE_LOCAL, O.MODE_SHORTEST, O.MODE_GRID, 1, 3):
        L = rapid_length(order_nodes(build_tree(cs), mode))
        assert L < 0.6 * naive, (mode, L, naive)
    nn = rapid_length(order_nodes(build_tree(cs), 99))         # unknown mode -> nearest neighbour
    best = rapid_length(order_nodes(build_tree(cs), O.MODE_SHORTEST))
    assert best <= nn + 1e-9


@pytest.mark.parametrize("mode", [0, 1, 2, 3, 4, 5, 6, 7, 20, 21])
def test_inner_before_outer_all_modes(mode):
    nodes = build_tree(plates())
    order = order_nodes(nodes, mode)
    assert len(order) == len(nodes)
    assert valid_precedence(order)


def test_mode_keep_preserves_document_order():
    cs = scattered(10)
    order = [n.key for n in order_nodes(build_tree(cs), O.MODE_KEEP)]
    assert order == list(range(10))


def test_local_optimal_accept_check_keeps_good_input():
    # already optimal left-to-right chain: mode 5 keeps it
    cs = [rect(20 * i, 0, 5, 5) for i in range(8)]
    order = [n.key for n in order_nodes(build_tree(cs), O.MODE_LOCAL)]
    assert rapid_length(order_nodes(build_tree(cs), O.MODE_LOCAL)) <= rapid_length(order_nodes(build_tree(cs), 0)) + 1e-9
    assert sorted(order) == list(range(8))


def test_containers_last():
    # a big sibling whose bbox surrounds a small one (not geometric containment: an L shape)
    L = Contour([Line((0, 0), (100, 0)), Line((100, 0), (100, 10)), Line((100, 10), (10, 10)),
                 Line((10, 10), (10, 100)), Line((10, 100), (0, 100)), Line((0, 100), (0, 0))], True)
    small = rect(50, 50, 10, 10)
    far = [rect(300 + 30 * i, 0, 10, 10) for i in range(3)]
    nodes = build_tree([L, small] + far)
    assert all(n.parent is None for n in nodes)
    order = [n.key for n in order_nodes(nodes, O.MODE_LOCAL)]
    assert order.index(1) < order.index(0)


def test_ring_sort_spiral():
    # 5x5 grid of squares: outside->inside starts on the hull and ends near the centre
    cs = [rect(100 * i, 100 * j, 10, 10) for j in range(5) for i in range(5)]
    out = [n.key for n in order_nodes(build_tree(cs), O.MODE_RING_OUT)]
    inn = [n.key for n in order_nodes(build_tree(cs), O.MODE_RING_IN)]
    assert inn == out[::-1]
    hull = {k for k in range(25) if k % 5 in (0, 4) or k // 5 in (0, 4)}
    assert set(out[:16]) == hull          # the whole outer ring first
    assert out[-1] == 12                  # centre last
    assert out[0] == 0                    # starts at the lower-left corner
    assert out[1] == 1                    # walks counter-clockwise along the bottom edge


def test_shortest_path_respects_holes_and_time():
    cs = plates() + scattered(40)
    nodes = build_tree(cs)
    order = order_nodes(nodes, O.MODE_SHORTEST)
    assert valid_precedence(order)
    assert rapid_length(order) <= rapid_length(order_nodes(build_tree(cs), 99)) + 1e-9


def test_route_rapid_avoids_hole():
    hole = Polygon([(40, -20), (60, -20), (60, 20), (40, 20)])
    path = O.route_rapid((0, 0), (100, 0), [hole], clearance=2.0)
    assert len(path) > 2
    assert not LineString(path).intersects(hole.buffer(1.0))
    assert O.polyline_length(path) > 100
    assert O.route_rapid((0, 50), (100, 50), [hole], 2.0) == [(0, 50), (100, 50)]


def test_plan_rapids_avoids_cut_holes():
    # plate with a big hole cut first, then a small hole on the far side; the straight rapid
    # from the left hole to the right one would cross the big middle hole
    cs = [rect(0, 0, 300, 100), circle(20, 50, 5, False), rect(100, 10, 100, 80), circle(280, 50, 5, False)]
    nodes = build_tree(cs)
    order = order_nodes(nodes, O.MODE_KEEP)
    paths = O.plan_rapids(order, (0, 0), clearance=2.0)
    mid = Polygon([(100, 10), (200, 10), (200, 90), (100, 90)])
    for pts, n in zip(paths, order):
        if n.key == 3:
            assert not LineString(pts).intersects(mid.buffer(-0.1))   # hugs the opening at most
            assert len(pts) > 2


def test_layer_priority_and_manual_order():
    doc = Document(parts=[Part([rect(10 * i, 0, 5, 5)], Craft(layer=1 + i % 2), name=str(i)) for i in range(6)])
    O.set_layer_priority(doc, [2])
    assert [p.name for p in doc.parts] == ["1", "3", "5", "0", "2", "4"]
    O.set_layer_priority(doc, [2], last=True)
    assert [p.name for p in doc.parts] == ["0", "2", "4", "1", "3", "5"]
    step = 1
    for name in ("5", "0"):
        idx = [p.name for p in doc.parts].index(name)
        step = O.manual_pick(doc, idx, step)
    assert [p.name for p in doc.parts][:2] == ["5", "0"] and step == 3
    assert O.manual_pick(doc, 0, step) == step         # already-ordered head cannot be hit
    O.set_manual_order(doc, [3, 2])
    assert [p.name for p in doc.parts][:2] == ["4", "2"]
    O.move_first_last(doc, 0, last=True)
    assert doc.parts[-1].name == "4"
    # mode 0 cuts in document order
    tp = build_toolpath(doc, CamParams(sort_mode=O.MODE_KEEP))
    from mlaser.cam.toolpath import Marker
    seq = [m.part for m in tp if isinstance(m, Marker) and m.kind == "contour"]
    assert seq == list(range(len(doc.parts)))


def test_toolpath_accepts_new_modes():
    doc = Document(parts=[Part(plates())])
    for mode in (0, 6, 7, 20, 21):
        tp = build_toolpath(doc, CamParams(sort_mode=mode))
        assert any(isinstance(i, Rapid) for i in tp)
