"""Sort mode 20 ("shortest path", a rebuild extension) on OR-Tools routing (optional).

The pure-Python :func:`mlaser.cam.ordering.shortest_path` (greedy + 2-opt + or-opt) stays the
zero-dependency default.  This backend solves the same problem with OR-Tools' vehicle routing
solver (Apache-2.0, the ``ortools`` extra):

* one vehicle starting at the sheet origin (``start``), ending anywhere (a free end node);
* asymmetric travel cost ``|exit_i -> entry_j|`` in µm (each contour is entered at its lead
  start and left at its lead end);
* precedence "children first": every contour comes after its children (holes and parts in
  holes), through a visit-counter dimension, ``rank(child) < rank(parent)``;
* small first: the small top-level groups (and their descendants) come before the rest;
* the built-in order (its own ``time_limit``) as the first solution, then guided local search
  for ``time_limit`` seconds; the shorter of the two is returned (so about 2x the time).

Selected with the profile ``"modern-ortools"`` (``mlaser.parity_ports.profiles.modern_ortools``,
option ``shortest_path="ortools"``) or by calling :func:`shortest_path_ortools` directly.  If
OR-Tools is missing or finds no solution the pure-Python order is returned (with a log line).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

SCALE = 1000.0          # mm -> µm integer costs


class OrToolsMissing(ImportError):
    pass


def _import():
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:     # pragma: no cover - depends on the environment
        raise OrToolsMissing("sort mode 20 on OR-Tools needs the optional package ortools "
                             "(pip install 'mlaser[ortools]')") from exc
    return pywrapcp, routing_enums_pb2


def shortest_path_ortools(nodes: list, start=(0.0, 0.0), small_first: bool = False,
                          time_limit: float = 2.0, fallback: bool = True) -> list:
    """Order ``nodes`` (``mlaser.cam.ordering.Node`` with parent/children/entry/exit) for
    minimal rapid length with children before parents.  See the module docstring."""
    from .ordering import _small_split, shortest_path
    if len(nodes) < 2:
        return list(nodes)
    try:
        pywrapcp, enums = _import()
    except OrToolsMissing:
        if not fallback:
            raise
        log.warning("ortools not installed: sort mode 20 uses the built-in solver")
        return shortest_path(nodes, start, small_first, time_limit)
    n = len(nodes)
    idx = {id(x): i + 1 for i, x in enumerate(nodes)}        # 0 = start depot, n + 1 = free end
    pts_in = [tuple(start)] + [tuple(x.entry) for x in nodes]
    pts_out = [tuple(start)] + [tuple(x.exit) for x in nodes]
    end = n + 1
    mgr = pywrapcp.RoutingIndexManager(n + 2, 1, [0], [end])
    routing = pywrapcp.RoutingModel(mgr)

    def cost(fi: int, ti: int) -> int:
        a, b = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
        if b == end or a == end:
            return 0
        return int(round(math.dist(pts_out[a], pts_in[b]) * SCALE))

    cb = routing.RegisterTransitCallback(cost)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)
    one = routing.RegisterUnaryTransitCallback(lambda i: 1)
    routing.AddDimension(one, 0, n + 2, True, "rank")
    rank = routing.GetDimensionOrDie("rank")
    solver = routing.solver()
    for x in nodes:
        for ch in x.children:
            if id(ch) in idx:
                solver.Add(rank.CumulVar(mgr.NodeToIndex(idx[id(ch)])) < rank.CumulVar(mgr.NodeToIndex(idx[id(x)])))
    if small_first:
        small, _large = _small_split([x for x in nodes if x.parent is None])
        sm: set = set()
        stack = list(small)
        while stack:
            y = stack.pop()
            if id(y) not in sm:
                sm.add(id(y))
                stack.extend(y.children)
        k = sum(1 for x in nodes if id(x) in sm)
        if 0 < k < n:
            for x in nodes:
                v = rank.CumulVar(mgr.NodeToIndex(idx[id(x)]))
                if id(x) in sm:
                    solver.Add(v <= k)
                else:
                    solver.Add(v > k)
    prm = pywrapcp.DefaultRoutingSearchParameters()
    prm.local_search_metaheuristic = enums.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    prm.time_limit.FromMilliseconds(max(1, int(time_limit * 1000)))
    # start from the built-in order: it satisfies the precedence, which the first-solution
    # heuristics of the routing library do not see (they would report no solution)
    seed_order = shortest_path(nodes, start, small_first, time_limit)
    routing.CloseModelWithParameters(prm)
    init = routing.ReadAssignmentFromRoutes([[idx[id(x)] for x in seed_order]], True)
    sol = routing.SolveFromAssignmentWithParameters(init, prm) if init is not None else None
    if sol is None:
        if not fallback:
            raise RuntimeError("OR-Tools found no order")
        log.warning("OR-Tools found no order: sort mode 20 uses the built-in solver")
        return shortest_path(nodes, start, small_first, time_limit)
    out: list = []
    i = routing.Start(0)
    while not routing.IsEnd(i):
        node = mgr.IndexToNode(i)
        if 1 <= node <= n:
            out.append(nodes[node - 1])
        i = sol.Value(routing.NextVar(i))
    from .ordering import rapid_length
    if len(out) != n or rapid_length(seed_order, start) <= rapid_length(out, start):
        return seed_order                  # never worse than the built-in order
    return out


def ordering_backend() -> Optional[str]:
    """The sort-mode-20 backend of the active parity profile (None = built-in)."""
    from ..parity_ports import active_profile
    return active_profile().option("shortest_path")
