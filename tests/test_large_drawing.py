"""Large drawings stay responsive (UI evaluation F2): copies for undo and for the planner,
incremental canvas and part list updates, the import review off the GUI thread.

A synthetic drawing of a few thousand single-contour parts (made here, no file) stands in for
the evaluation's 2,700-contour DXF.  Time budgets are relative (a copy made the old way, a full
rebuild of the views) with clear headroom, so they hold on slow CI runners; the decisive
checks are counts (parts copied, contour paths recomputed) and equality with full rebuilds."""

from __future__ import annotations

import copy
import gc
import math
import pathlib
import time

import pytest

from mlaser.model import Arc, Contour, Craft, Document, Line, Part
from mlaser.services.document import DocumentService
from mlaser.services.undo import fast_deepcopy

SAMPLE = pathlib.Path(__file__).parent / "data" / "chf" / "sample_v9.chf"


def big_document(n: int = 2000, segs: int = 14) -> Document:
    """``n`` parts on a grid, each one closed contour of ``segs`` lines and one arc, with
    source glyphs (round-trip records the undo table shares)."""
    parts = []
    side = int(math.ceil(math.sqrt(n)))
    for k in range(n):
        x0, y0 = (k % side) * 12.0, (k // side) * 12.0
        pts = [(x0 + 5 + 4 * math.cos(2 * math.pi * i / segs), y0 + 5 + 4 * math.sin(2 * math.pi * i / segs))
               for i in range(segs)]
        lines: list = [Line(pts[i], pts[i + 1]) for i in range(segs - 1)]
        lines.append(Arc(pts[-1], pts[0], (x0 + 5, y0 + 5), True))
        c = Contour(lines, True)
        c.set_source_glyphs([])
        c.extra["note"] = {"k": k}
        parts.append(Part([c], Craft(layer=1 + k % 3), name=f"p{k}"))
    return Document(parts=parts)


def timed(fn, n: int = 1, gc_off: bool = False) -> float:
    """The best of ``n`` runs of ``fn``; ``gc_off``: each with the cyclic garbage collector
    paused (after a collection), for a comparison of two copies of the whole drawing: a full
    collection of the worker's heap (whatever the tests before left) landing inside one side
    measured the runner, not the code (macOS CI: a cold snapshot at 0.70 of the old copy
    instead of 0.30-0.35)."""
    best = math.inf
    for _ in range(n):
        if gc_off:
            gc.collect()
            gc.disable()
        try:
            t0 = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t0)
        finally:
            if gc_off:
                gc.enable()
    return best


#: seconds :func:`_reference_work` takes on the development host (Linux x86-64, Python 3.12)
REFERENCE_WORK_S = 0.020


def _reference_work() -> float:
    """Seconds a fixed mix of what a nudge does on the GUI thread (copying model objects, plain
    Python) takes now (best of three, as :func:`timed`): the speed of this runner under its
    current load."""
    d = big_document(150)

    def work() -> None:
        for _ in range(3):
            fast_deepcopy(d)
        acc = 0.0
        for i in range(60_000):
            acc += (i % 97) * 0.5 - (i % 31) * 0.25
        assert acc
    return timed(work, 3, gc_off=True)


def runner_budget(seconds: float) -> float:
    """``seconds`` (a bound with headroom on the development host) scaled by how much slower
    this runner is right now, never less (a shared Windows runner: 0.29 s for a ~0.05 s nudge).
    A real regression (the whole drawing copied or redrawn per nudge, 5-50x) stays far over."""
    slow = _reference_work() / REFERENCE_WORK_S
    return seconds * max(1.0, slow)


# --------------------------------------------------------------------------- the copier
def _shape(a, b, seen=None) -> None:
    """``b`` has the structure of ``a`` (types, values, which sub-objects are shared)."""
    assert type(a) is type(b)
    if isinstance(a, (int, float, str, bool, type(None))):
        assert a == b
        return
    if isinstance(a, (list, tuple)):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            _shape(x, y)
        return
    if isinstance(a, dict):
        assert list(dict.keys(a)) == list(dict.keys(b))
        for k in dict.keys(a):
            _shape(dict.__getitem__(a, k), dict.__getitem__(b, k))
        return
    if hasattr(a, "__dict__"):
        assert a.__dict__.keys() == b.__dict__.keys()
        for k in a.__dict__:
            if k == "extra":
                _shape(a.__dict__[k], b.__dict__[k])
            else:
                _shape(a.__dict__[k], b.__dict__[k])
        return
    assert a == b


def test_fast_deepcopy_matches_deepcopy_on_the_sample():
    """Same values, same types (``LegacyExtra`` views bound to the copy), shared records
    shared, sub-objects shared inside the original shared in the copy."""
    from mlaser.formats.chf import load_chf
    doc = load_chf(str(SAMPLE))
    docs = DocumentService(doc)
    shared = docs.blobs.ids()
    ref = copy.deepcopy(doc, dict(shared))
    new = fast_deepcopy(doc, dict(shared))
    assert new == ref == doc
    for a, b in zip(ref.parts, new.parts):
        _shape(a, b)
        assert type(b.__dict__["extra"]) is type(a.__dict__["extra"])
        if hasattr(b.__dict__["extra"], "_ref"):
            assert b.__dict__["extra"]._ref() is b
        for ca, cb in zip(a.contours, b.contours):
            assert cb.uid == ca.uid and cb.glyphs is ca.glyphs or cb.glyphs == ca.glyphs
    # the round-trip records are shared, not copied
    for p, c in zip(doc.parts, new.parts):
        if p.extra.get("chf") is not None:
            assert c.extra["chf"] is p.extra["chf"]
    # aliasing: one Craft object on two parts stays one object in the copy
    d = big_document(3)
    d.parts[1].craft = d.parts[0].craft
    cp = fast_deepcopy(d)
    assert cp.parts[1].craft is cp.parts[0].craft and cp.parts[0].craft is not d.parts[0].craft
    assert cp == copy.deepcopy(d)


def test_copy_part_shares_records_and_matches_lean_copy():
    from mlaser.formats.chf import load_chf
    docs = DocumentService(load_chf(str(SAMPLE)))
    for p in docs.document.parts:
        a, b = docs.blobs.copy_part(p), docs.lean_copy(p)
        assert a == b == p and a is not p
        for k in ("chf", "_chf_snapshot"):
            if p.extra.get(k) is not None:
                assert a.extra[k] is p.extra[k]


# --------------------------------------------------------------------------- snapshots and undo
@pytest.fixture(scope="module")
def big():
    return big_document(2000)


def test_plan_snapshot_of_a_large_drawing_is_cheap(big):
    """The cold snapshot (after an open) is linear and well under the old per-part deepcopy
    with a whole-table memo (5 s at 2,700 parts in the evaluation); a one-part nudge copies one
    part; undo and redo reuse the copies made before."""
    # the cold snapshot against the old copy, both with the collector paused (gc_off)
    doc = copy.deepcopy(big)
    docs = DocumentService(doc)
    shared = docs.blobs.ids()
    t_old = timed(lambda: [copy.deepcopy(p, dict(shared)) for p in doc.parts], gc_off=True)
    t_cold_nogc = timed(lambda: (docs._plan_invalidate(), docs.plan_snapshot()), gc_off=True)
    assert t_cold_nogc < 0.6 * t_old, (t_cold_nogc, t_old)
    # the first snapshot of a fresh service is the cold one (a second one after an invalidate
    # reuses what the first made): the nudge's reference, as a user's first plan after an open
    doc = copy.deepcopy(big)
    docs = DocumentService(doc)
    t_cold = timed(lambda: (docs._plan_invalidate(), docs.plan_snapshot()))
    snap = docs.plan_snapshot()
    assert snap == docs.snapshot()
    # a one-part nudge: one part copied for undo, one for the planner
    p = doc.parts[17]
    misses = docs.plan_misses
    t_nudge = timed(lambda: docs.edit(lambda d: [s.__dict__.update(start=(s.start[0] + 1, s.start[1]))
                                                 for s in p.contours[0].segments[:1]], "nudge", parts=[p]))
    assert set(docs.stack._undo[-1].swap) == {p.uid}
    t_warm = timed(lambda: docs.plan_snapshot())
    assert docs.plan_misses == misses + 1
    assert t_nudge + t_warm < 0.5 * t_cold, (t_nudge, t_warm, t_cold)
    # undo / redo: the live parts equal the copies made before the nudge / after it
    for step in (docs.undo, docs.redo, docs.undo):
        step()
        s = docs.plan_snapshot()
        assert s == docs.snapshot()
    assert docs.plan_misses == misses + 1


def test_select_all_edit_undo_redo_copies_once(big, monkeypatch):
    """A select-all edit copies every part once for undo, with the fast copier and a memo of
    the part's own records (no whole-table memo, no fallback to ``copy.deepcopy`` for the
    model's objects: what made a 2,700-part copy quadratic); undo and redo copy nothing.

    The decisive checks are those counts; the time bound is a backstop with CI headroom
    (macOS runners are 2-3x slower and the edit also moves every part), relative to the old
    copies measured in this process."""
    from mlaser import ops
    from mlaser.services import undo as U
    doc = copy.deepcopy(big)
    docs = DocumentService(doc)
    docs.plan_snapshot()
    misses = docs.plan_misses
    counts = {"copy_part": 0, "memo": 0, "deepcopy": 0}
    real_part, real_memo, real_deep = U.RoundTripTable.copy_part, U.RoundTripTable.memo, U.copy.deepcopy

    def count(name, fn):
        def wrapped(*a, **k):
            counts[name] += 1
            return fn(*a, **k)
        return wrapped
    monkeypatch.setattr(U.RoundTripTable, "copy_part", count("copy_part", real_part))
    monkeypatch.setattr(U.RoundTripTable, "memo", count("memo", real_memo))
    monkeypatch.setattr(U.copy, "deepcopy", count("deepcopy", real_deep))
    t_edit = timed(lambda: docs.edit(lambda d: ops.translate(d, 1.0, 0.0, list(d.parts)), "nudge"))
    monkeypatch.undo()
    assert counts == {"copy_part": len(doc.parts), "memo": 0, "deepcopy": 0}, counts
    assert set(docs.stack._undo[-1].swap) == {p.uid for p in doc.parts}
    docs.plan_snapshot()
    assert docs.plan_misses == misses + len(doc.parts)       # every part changed: copied once
    before = docs.plan_misses
    for step in (docs.undo, docs.redo, docs.undo, docs.redo):
        step()
        assert docs.plan_snapshot() == docs.snapshot()
    assert docs.plan_misses == before                        # undo / redo reuse the copies
    # backstop: the edit (undo copies plus moving every part) stays within a few times the
    # old per-part deepcopy alone (it was several times slower than that with the
    # whole-table memo per part)
    shared = docs.blobs.ids()
    t_old = timed(lambda: [copy.deepcopy(p, dict(shared)) for p in doc.parts])
    assert t_edit < 3 * t_old, (t_edit, t_old)


def test_changed_parts_are_reported_to_listeners(big):
    doc = copy.deepcopy(big)
    docs = DocumentService(doc)
    seen = []
    docs.subscribe(lambda kind: seen.append((kind, docs.changed)))
    p, q = doc.parts[3], doc.parts[4]
    from mlaser import ops
    docs.edit(lambda d: ops.translate(d, 1.0, 0.0, [p]), "nudge", parts=[p])
    docs.undo()
    docs.redo()
    docs.edit(lambda d: d.parts.remove(q), "delete", parts=[q])
    docs.replace(big_document(2))
    assert seen == [("edit", frozenset({p.uid})), ("undo", frozenset({p.uid})), ("redo", frozenset({p.uid})),
                    ("edit", frozenset({q.uid})), ("replace", None)]
    assert docs.changed is None                              # only during a notification


# --------------------------------------------------------------------------- the real window
@pytest.fixture
def win(app):
    from wiring import dispose, real_window
    w = real_window()
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _scene_state(canvas) -> list:
    """(part, contour, the item's path bounds) of every contour item, and that each item is in
    the scene."""
    out = []
    for pi, ci, it, pts in canvas._items:
        assert it.scene() is canvas._scene
        r = it.path().boundingRect()
        out.append((pi, ci, round(r.x(), 6), round(r.y(), 6), round(r.width(), 6), round(r.height(), 6),
                    len(pts)))
    return out


def _expected(doc) -> list:
    from mlaser.ui.canvas import _make_contour_path
    out = []
    for pi, p in enumerate(doc.parts):
        for ci, c in enumerate(p.contours):
            path, pts = _make_contour_path(c)
            r = path.boundingRect()
            out.append((pi, ci, round(r.x(), 6), round(r.y(), 6), round(r.width(), 6), round(r.height(), 6),
                        len(pts)))
    return out


def test_canvas_and_part_list_update_incrementally(win, app, monkeypatch):
    """A nudge, its undo and redo recompute the paths of the changed part only; the scene items
    and the part list rows then match the document exactly (and a full rebuild)."""
    import mlaser.ui.canvas as C
    win.set_document(big_document(1500))
    app.processEvents()
    canvas = win.canvas
    calls = []
    real = C._contour_path
    monkeypatch.setattr(C, "_contour_path", lambda c: (calls.append(c), real(c))[1])
    win.canvas.select_parts([5])

    def drawing_items() -> int:
        # every scene item but the overlay (toolpath, markers, labels): the overlay is built in
        # time slices over later event-loop turns and changes when the plan arrives, so its
        # size at any moment depends on the runner's speed (macOS CI: +106 items)
        overlay = set(canvas._overlay)
        return sum(1 for it in canvas._scene.items() if it not in overlay)
    n_items = drawing_items()
    contour_items = [it for _, _, it, _ in canvas._items]
    for step in (lambda: win.nudge(1, 0), win.undo, win.redo, lambda: win.nudge(0, 1)):
        calls.clear()
        step()
        assert len(calls) <= 1, len(calls)                   # the changed part's one contour
        assert _scene_state(canvas) == _expected(win.document)
    # the unchanged parts' contour items are the same objects (kept, not re-created; the
    # nudged part's item may be new when a plan's toolpath triggered a full rebuild meanwhile);
    # nothing but the overlay was added (a few items of the machine head / track at most)
    assert len(canvas._items) == len(contour_items)
    assert all(a is b for a, (pi, _, b, _) in zip(contour_items, canvas._items) if pi != 5)
    assert drawing_items() <= n_items + 5, (drawing_items(), n_items)
    rows = [tuple(win.part_list.topLevelItem(i).text(c) for c in range(5))
            for i in range(win.part_list.topLevelItemCount())]
    win.part_list.refresh()                                  # a full refresh shows the same
    assert rows == [tuple(win.part_list.topLevelItem(i).text(c) for c in range(5))
                    for i in range(win.part_list.topLevelItemCount())]
    win.refresh_views()                                      # a full rebuild: the same scene
    assert _scene_state(canvas) == _expected(win.document)
    # deleting a part (the part list changes): a full rebuild, still exact
    win.delete_selected()
    assert _scene_state(canvas) == _expected(win.document)
    win.undo()
    assert _scene_state(canvas) == _expected(win.document)


#: GUI-thread budget of one arrow-key nudge / its undo at 1,500 parts: the target is 100 ms
#: (input to visible response); about 50 ms here, so a CI runner has 5x headroom
NUDGE_BUDGET_S = 0.25


def test_nudge_of_one_part_keeps_the_gui_thread_budget(win, app):
    """A one-part nudge (edit, undo copy, views, the replan's snapshot) of a 1,500-part drawing
    and its undo stay within :data:`NUDGE_BUDGET_S` (114-294 ms at 2,700 parts before, plus a
    snapshot of the whole drawing after a select-all nudge)."""
    win.set_document(big_document(1500))
    app.processEvents()
    win.docs.plan_snapshot()
    win.canvas.select_parts([7])
    t_nudge = timed(lambda: (win.nudge(1, 0), win.docs.plan_snapshot()), 3)
    t_undo = timed(lambda: (win.undo(), win.docs.plan_snapshot()), 3)
    budget = runner_budget(NUDGE_BUDGET_S)
    assert t_nudge < budget, (t_nudge, budget)
    assert t_undo < budget, (t_undo, budget)


def test_import_review_of_a_large_drawing_runs_off_the_gui_thread(win, app, monkeypatch):
    import threading

    from mlaser.services import repair as R
    threads = []
    real = R.analyze
    monkeypatch.setattr(R, "analyze", lambda doc, opts=None, **kw: (threads.append(threading.current_thread()),
                                                                    real(doc, opts, **kw))[1])
    doc = big_document(400)
    doc.parts[0].contours[0].closed = False                  # something to report
    win.set_document(doc)
    orders = win.extension("orders")
    shown = []
    monkeypatch.setattr(orders, "show_review", lambda: shown.append(1))
    win._test_no_dialogs = False
    monkeypatch.setattr("mlaser.ui.workspaces.orders._dialogs", lambda w: True)
    win.hooks.emit("document_imported", "big.dxf", win.document)
    t0 = time.time()
    while not shown and time.time() - t0 < 30:
        app.processEvents()
        time.sleep(0.01)
    assert shown and threads and threads[0] is not threading.main_thread()


# --------------------------------------------------------------------------- follow-up: panels, open
def test_effective_many_equals_one_call_per_part():
    """The properties panel's per-layer evaluation gives exactly ``inheritance.effective`` per
    part, with part, layer and registry sources mixed."""
    from mlaser.services import inheritance as I
    from mlaser.ui.properties.panel import _effective_many
    from mlaser.model import LayerParams
    doc = big_document(30)
    doc.layers = {li: LayerParams(index=li) for li in (1, 2, 3)}
    I.set_override(doc, None, "layer", "craft.kerf_width", 0.3, layer=2)
    for p in doc.parts[::4]:
        I.set_override(doc, None, "part", "craft.kerf_width", 0.15, part=p)
        I.set_override(doc, None, "part", "craft.lead_in.length", 3.5, part=p)
    vids = [*I.CRAFT_IDS, "GP.speed"]
    for vid in vids:
        marks = [I.craft_overrides(p.craft) for p in doc.parts]
        assert _effective_many(doc, None, vid, doc.parts, marks) == \
            [I.effective(doc, None, vid, part=p) for p in doc.parts], vid
        assert _effective_many(doc, None, vid, doc.parts, None, upto=2) == \
            [I.effective(doc, None, vid, part=p, upto=2) for p in doc.parts], vid


def test_select_all_refresh_of_the_properties_panel_is_cheap(win, app):
    """With every part of a 1,500-part drawing selected, a properties refresh evaluates each
    value once per layer, not once per part (54,000 inheritance calls, about 1 s, before)."""
    from mlaser.services import inheritance as I
    win.set_document(big_document(1500))
    win.run("select.all")
    app.processEvents()
    ext = win.extension("ext_process")
    ext = ext[0] if isinstance(ext, tuple) else ext
    calls = []
    real = I.effective
    I.effective = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        ext.properties.refresh()
    finally:
        I.effective = real
    assert 0 < len(calls) <= 4 * 3 * len(ext.properties.inherit), len(calls)


def test_open_primes_the_planner_snapshot(win, app, tmp_path):
    """File > Open makes the planner's part copies in the import worker: the first replan after
    the open copies nothing on the GUI thread."""
    from mlaser.cli.export import write_dxf
    path = tmp_path / "grid.dxf"
    write_dxf(big_document(300), str(path))
    assert win.load_path(str(path))
    misses = win.docs.plan_misses
    snap = win.docs.plan_snapshot()
    assert win.docs.plan_misses == misses, win.docs.plan_misses - misses
    assert snap == win.docs.snapshot()
