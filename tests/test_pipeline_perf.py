"""The document pipeline of a large drawing (perf review 2026-11, findings 4, 6, 7, 8 and 12).

* Finding 6: a select-all nudge records its undo as the replaced segment lists
  (``DocumentService.edit_geometry`` / ``SegmentsEdit``): no part copies, and undo / redo put
  back the very coordinates (bit for bit, the objects themselves); the selection extent and the
  part list's boxes are not measured again.
* Finding 7: the plan cache is bounded by bytes and keeps the newest and the current plan.
* Finding 8: a preview plans the toolpath; the compile follows after an idle time, at ``settle``,
  or when Start asks for a plan of the same inputs (never a stale one).
* Finding 12: closing a drawing (open / new) gives back its plans, plan-codec entries and undo
  history.
* Finding 4: the import review of a large drawing runs in a child process with the same result.

Time bounds are relative to work measured in the same process (or scaled by the runner's speed,
``runner_budget``) with headroom; the decisive checks are counts and exact equality."""

from __future__ import annotations

import copy
import gc
import threading
import time
import weakref

import pytest

from mlaser.cad import arrange as AR
from mlaser.cam.toolpath import CamParams
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, Line, Part
from mlaser.nc.machine import Machine
from mlaser.services.document import ChangedParts, DocumentService
from mlaser.services.jobs import InlineExecutor, JobRequest, JobService, plan_nbytes
from mlaser.services.undo import SegmentsEdit

from test_large_drawing import big_document, runner_budget, timed


# --------------------------------------------------------------------------- helpers
def coords(doc: Document) -> list:
    """Every coordinate of the document as exact float hex strings, with the segment types."""
    out = []
    for p in doc.parts:
        for c in p.contours:
            for s in c.segments:
                pts = (s.start, s.end) + ((s.center,) if isinstance(s, Arc) else ())
                out.append((p.uid, c.uid, type(s).__name__, getattr(s, "ccw", None),
                            tuple(v.hex() for pt in pts for v in pt)))
    return out


def seg_ids(doc: Document) -> list:
    return [id(s) for p in doc.parts for c in p.contours for s in c.segments]


def nudge(docs: DocumentService, parts, dx: float, dy: float, coalesce: bool = True):
    return docs.edit_geometry(lambda d: AR.translate(parts, dx, dy), "nudge", parts=parts, coalesce=coalesce,
                              shift=(dx, dy))


def small_doc() -> Document:
    d = big_document(40)
    for p in d.parts:
        d.layers.setdefault(p.craft.layer, LayerParams(index=p.craft.layer, speed=120.0))
    return d


# --------------------------------------------------------------------------- finding 6: undo
def test_move_undo_restores_the_exact_coordinates():
    """Five coalesced nudges by steps that are not exact in binary, then undo: every coordinate
    is bit for bit what it was (``x + d - d`` is not, here), and the segment objects are the
    originals; redo gives the moved state bit for bit; the chain repeats."""
    docs = DocumentService(big_document(200))
    doc = docs.document
    before, before_ids = coords(doc), seg_ids(doc)
    sel = list(doc.parts)
    steps = [(0.1, 0.0), (0.1, 0.0), (0.0, 1 / 3), (0.7, -0.3), (1e-3, 0.2)]
    for dx, dy in steps:
        nudge(docs, sel, dx, dy)
    assert len(docs.stack) == 1 and isinstance(docs.stack._undo[-1], SegmentsEdit)    # one undo step
    after, after_ids = coords(doc), seg_ids(doc)
    # the arithmetic way back is not exact: the point of keeping the originals
    back = [x for p in doc.parts for c in p.contours for s in c.segments for x in (s.start[0],)]
    for dx, _dy in reversed(steps):
        back = [x - dx for x in back]
    orig = [s.start[0] for p in copy.deepcopy(DocumentService(big_document(200)).document).parts
            for c in p.contours for s in c.segments]
    assert any(a.hex() != b.hex() for a, b in zip(back, orig))
    for _ in range(3):
        assert docs.undo() == "nudge"
        assert coords(doc) == before and seg_ids(doc) == before_ids
        assert docs.redo() == "nudge"
        assert coords(doc) == after and seg_ids(doc) == after_ids


def test_move_undo_chains_with_other_edits_by_uid():
    """A move, then an edit that copies its part and changes it in place, then another move:
    undoing all of them gives the original exactly and redoing them the final state exactly
    (the move finds its parts by uid after the edit's undo put other part objects in place)."""
    from mlaser import ops
    docs = DocumentService(big_document(60))
    doc = docs.document
    states = [coords(doc)]
    nudge(docs, list(doc.parts), 0.1, 0.2)
    states.append(coords(doc))
    p = doc.parts[5]

    def bend(d):                      # in place, as a tool may: the edit copied the part first
        s = p.contours[0].segments[0]
        s.__dict__.update(start=(s.start[0] + 0.37, s.start[1]))
        ops.set_layer(d, 2)
    docs.edit(bend, "bend", parts=[p])
    states.append(coords(doc))
    nudge(docs, doc.parts[3:9], -0.3, 0.7, coalesce=False)
    states.append(coords(doc))
    snap_final = docs.snapshot()
    for k in (2, 1, 0):
        docs.undo()
        assert coords(doc) == states[k], k
    for k in (1, 2, 3):
        docs.redo()
        assert coords(doc) == states[k], k
    assert docs.document == snap_final


def test_move_copies_nothing_and_saves_the_same(monkeypatch):
    """The move's undo holds lists, not part copies; the saved file, the planner snapshot and
    the plan's inputs hash after a move and its undo are those before (autosave and project
    save read the same document)."""
    from mlaser.formats.chf import dumps_chf
    from mlaser.services import undo as U
    from mlaser.services.jobs import inputs_hash
    docs = DocumentService(small_doc())
    doc = docs.document
    data0 = dumps_chf(doc)
    snap0 = docs.plan_snapshot()
    key0 = inputs_hash(JobRequest("process", snap0, Machine(), cam_params=CamParams()))
    calls = []
    monkeypatch.setattr(U.RoundTripTable, "copy_part", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(U, "fast_deepcopy", lambda *a, **k: calls.append(1))
    nudge(docs, list(doc.parts), 0.1, 0.1)
    monkeypatch.undo()
    assert calls == []
    assert docs.stack.cost < sum(U.part_cost(p) for p in doc.parts)
    moved = dumps_chf(doc)
    assert moved != data0
    docs.undo()
    assert dumps_chf(doc) == data0
    snap = docs.plan_snapshot()
    assert snap == snap0 and snap == docs.snapshot()
    assert inputs_hash(JobRequest("process", snap, Machine(), cam_params=CamParams())) == key0
    docs.redo()
    assert dumps_chf(doc) == moved


def test_edit_geometry_refuses_more_than_a_move():
    docs = DocumentService(small_doc())
    doc = docs.document
    before = coords(doc)
    p = doc.parts[0]

    def remove(d):
        AR.translate([p], 1.0, 0.0)
        d.parts.remove(p)
    with pytest.raises(RuntimeError):
        docs.edit_geometry(remove, "nudge", parts=[p])
    doc.parts.insert(0, p)
    assert coords(doc) == before and len(docs.stack) == 0

    def boom(d):
        AR.translate([p], 1.0, 0.0)
        raise ValueError("no")
    with pytest.raises(ValueError):
        docs.edit_geometry(boom, "nudge", parts=[p])
    assert coords(doc) == before and len(docs.stack) == 0


def test_move_tells_the_listeners_the_shift():
    docs = DocumentService(small_doc())
    seen = []
    docs.subscribe(lambda kind: seen.append((kind, docs.changed, getattr(docs.changed, "shift", None))))
    p = docs.document.parts[3]
    nudge(docs, [p], 0.5, -1.0)
    docs.undo()
    docs.redo()
    assert [(k, set(c), s) for k, c, s in seen] == [("edit", {p.uid}, (0.5, -1.0)), ("undo", {p.uid}, (-0.5, 1.0)),
                                                    ("redo", {p.uid}, (0.5, -1.0))]
    assert all(isinstance(c, ChangedParts) for _k, c, _s in seen)


# --------------------------------------------------------------------------- finding 12: closing
def test_open_drops_the_history_of_the_closed_drawing():
    from mlaser import ops
    docs = DocumentService(small_doc())
    a = docs.document
    for k in range(4):
        docs.edit(lambda d: ops.translate(d, 1.0, 0.0, d.parts[:3]), f"e{k}", parts=a.parts[:3])
    nudge(docs, list(a.parts), 1.0, 0.0)
    assert len(docs.stack) == 5
    docs.checkpoint()                                         # File > Open / New
    b = big_document(5)
    docs.adopt(b)
    assert len(docs.stack) == 1 and docs.stack.undo_text == "replace"
    assert docs.undo() == "replace" and docs.document == a    # the open itself can be undone
    assert not docs.can_undo


# --------------------------------------------------------------------------- finding 7: the cache
def _req(doc, start):
    # the same document object: its round-trip records are hashed by identity (as the planner
    # snapshot's shared copies are)
    return JobRequest(mode="process", document=doc, machine=Machine(smooth=False), start=start,
                      cam_params=CamParams())


def test_plan_cache_is_bounded_by_bytes_and_keeps_the_newest_and_current():
    doc = small_doc()
    svc = JobService(executor=InlineExecutor(), debounce_s=0)
    first = svc.plan_sync(_req(doc, (0.0, 0.0)))
    one = plan_nbytes(first)
    assert one > 0 and svc.cache_nbytes == one
    # room for two plans: the oldest go, the newest stays
    svc = JobService(executor=InlineExecutor(), debounce_s=0, cache_bytes=int(2.5 * one))
    plans = [svc.plan_sync(_req(doc, (float(k), 0.0))) for k in range(5)]
    assert list(svc.cache) == [plans[3].key, plans[4].key]
    # room for less than one: the newest plan always stays
    svc.cache_bytes = one // 2
    svc.plan_sync(_req(doc, (9.0, 0.0)))
    assert len(svc.cache) == 1
    # the current preview plan stays while other plans come and go
    svc = JobService(executor=InlineExecutor(), debounce_s=0, cache_bytes=int(2.5 * one))
    svc.request_preview(_req(doc, (0.0, 1.0)))
    cur = svc.current
    assert cur is not None
    for k in range(4):
        svc.plan_sync(_req(doc, (float(k), 2.0)))
    assert cur.key in svc.cache and len(svc.cache) == 2
    # the count bound still holds for small plans
    svc = JobService(executor=InlineExecutor(), debounce_s=0)
    for k in range(9):
        svc.plan_sync(_req(doc, (float(k), 3.0)))
    assert len(svc.cache) == svc.cache_size == 6


def test_release_document_forgets_plans_and_part_encodings():
    svc = JobService(executor=InlineExecutor(), debounce_s=0)
    req = _req(small_doc(), (0.0, 0.0))
    svc.request_preview(req)
    assert svc.cache and svc.codec._parts and svc.current is not None
    ref = weakref.ref(req.document.parts[0])
    svc.release_document()
    assert not svc.cache and not svc.codec._parts and svc.current is None
    svc.preview = None
    del req
    gc.collect()
    assert ref() is None


# --------------------------------------------------------------------------- finding 8: two stages
def _threaded(idle: float, **kw) -> JobService:
    return JobService(debounce_s=0, compile_idle_s=idle, process=kw.pop("process", False), **kw)


def _same_plan(a, b) -> None:
    assert a.key == b.key and a.start == b.start
    assert list(a.toolpath) == list(b.toolpath)
    assert a.job.wire == b.job.wire and a.frames() == b.frames()
    assert a.job.estimate == b.job.estimate and a.bounds == b.bounds and a.cut_bounds == b.cut_bounds


def test_preview_plans_the_toolpath_first_and_compiles_when_idle():
    doc = small_doc()
    svc = _threaded(0.4)
    try:
        t = svc.request_preview(_req(doc, (1.0, 2.0)))
        assert t.wait_toolpath(60) is not None
        time.sleep(0.1)
        assert not t.done_event.is_set() and t.deferred and svc.stats["compiles"] == 0
        assert t.wait(60) is not None
        assert svc.stats["compiles"] == 1
        _same_plan(t.plan, JobService(executor=InlineExecutor()).plan_sync(_req(doc, (1.0, 2.0))))
    finally:
        svc.shutdown()


def test_edits_in_a_row_plan_toolpaths_and_compile_once(monkeypatch):
    from mlaser.services import planproc
    doc = small_doc()
    svc = _threaded(1.5)
    stages = []
    real = planproc.run_toolpath
    monkeypatch.setattr(planproc, "run_toolpath", lambda r: (stages.append(1), real(r))[1])
    try:
        tickets = []
        for k in range(4):                      # four edits, each within the idle time of the last
            t = svc.request_preview(_req(doc, (float(k), 0.0)))
            assert t.wait_toolpath(60) is not None
            tickets.append(t)
        last = tickets[-1]
        assert last.wait(60) is not None
        assert svc.stats["compiles"] == 1 and len(stages) == 4    # each toolpath once, one compile
        assert all(t.cancelled and t.done_event.is_set() for t in tickets[:-1])
    finally:
        svc.shutdown()


class _Submitter:
    def __init__(self) -> None:
        self.plans: list = []
        self.done = threading.Event()

    def start_job(self, plan):
        self.plans.append(plan)
        self.done.set()
        return True


def test_start_compiles_the_deferred_preview_once(monkeypatch):
    """Start with the inputs of a preview whose compile waits: that preview's toolpath is
    compiled (once), and the same plan starts."""
    from mlaser.services import planproc
    doc = small_doc()
    svc = _threaded(600.0)                       # the idle compile never comes by itself
    stages = []
    real = planproc.run_toolpath
    monkeypatch.setattr(planproc, "run_toolpath", lambda r: (stages.append(1), real(r))[1])
    try:
        t = svc.request_preview(_req(doc, (3.0, 4.0)))
        assert t.wait_toolpath(60) is not None
        time.sleep(0.1)
        assert t.deferred
        sub, results = _Submitter(), []
        svc.run(_req(doc, (3.0, 4.0)), sub, lambda ok, msg: results.append(ok))
        assert sub.done.wait(60)
        assert sub.plans[0] is t.wait(5) and sub.plans[0].start == (3.0, 4.0)
        assert svc.stats["compiles"] == 1 and len(stages) == 1
    finally:
        svc.shutdown()


def test_start_from_another_head_position_never_runs_the_preview_plan():
    """The preview was planned from another head position: Start compiles a plan from the head
    position it was given (the inputs hash decides, as before)."""
    doc = small_doc()
    svc = _threaded(600.0)
    try:
        t = svc.request_preview(_req(doc, (3.0, 4.0)))
        assert t.wait_toolpath(60) is not None
        sub = _Submitter()
        svc.run(_req(doc, (50.0, 4.0)), sub)
        assert sub.done.wait(60)
        plan = sub.plans[0]
        assert plan.start == (50.0, 4.0) and plan.key != t.key
        _same_plan(plan, JobService(executor=InlineExecutor()).plan_sync(_req(doc, (50.0, 4.0))))
        assert t.deferred                       # the preview's own compile still waits
    finally:
        svc.shutdown()


def test_settle_compiles_a_deferred_preview_now():
    doc = small_doc()
    svc = _threaded(600.0)
    try:
        t = svc.request_preview(_req(doc, (0.0, 0.0)))
        t0 = time.monotonic()
        plan = svc.settle(t, timeout=60)
        assert plan is not None and t.plan is plan and time.monotonic() - t0 < 60
        assert svc.current is plan
    finally:
        svc.shutdown()


def test_two_stage_preview_in_the_plan_process_gives_the_same_plan():
    """In the plan process the toolpath of the first stage is kept and compiled as it is; when
    the child planned something else meanwhile, the compile starts over.  Both give the plan a
    single-stage compile gives."""
    doc = small_doc()
    svc = _threaded(600.0, process=True)
    try:
        assert svc.process_mode
        ref = JobService(executor=InlineExecutor()).plan_sync(_req(doc, (2.0, 2.0)))
        t = svc.request_preview(_req(doc, (2.0, 2.0)))
        assert t.wait_toolpath(120) is not None
        _same_plan(svc.settle(t, timeout=120), ref)
        # the child's kept toolpath is gone (another plan ran): planned again, the same plan
        t2 = svc.request_preview(_req(doc, (5.0, 2.0)))
        assert t2.wait_toolpath(120) is not None
        other = svc.plan(_req(doc, (7.0, 7.0))).wait(120)
        assert other is not None
        _same_plan(svc.settle(t2, timeout=120),
                   JobService(executor=InlineExecutor()).plan_sync(_req(doc, (5.0, 2.0))))
        assert svc.stats["process_compiles"] >= 3
    finally:
        svc.shutdown()


# --------------------------------------------------------------------------- the real window
@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    from wiring import dispose, real_window
    w = real_window()
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    yield w
    dispose(w)


#: GUI-thread budget of a select-all nudge of this 2,000-part drawing: 0.11 s on the development
#: host (0.25 s before; the canvas's rebuild of the moved items and the properties panel are most
#: of what is left), so a runner has about 3x headroom.  A backstop: the counts decide
SELECT_ALL_NUDGE_S = 0.3


def test_select_all_nudge_copies_nothing_and_measures_nothing(win, app, monkeypatch):
    """A select-all arrow-key nudge of 2,000 parts: no part copied for undo, no bounding box
    measured (the step's extent and the part list's boxes are carried along; 0.89 s at 2,700
    parts before, 41 % of it undo copies and 45 % two bounding-box passes), within
    :data:`SELECT_ALL_NUDGE_S` on this host's scale; undo and redo of the coalesced step are
    exact."""
    from mlaser.services import undo as U
    win.set_document(big_document(2000))
    app.processEvents()
    win.run("select.all")
    app.processEvents()
    doc = win.document
    orig = coords(doc)
    win.nudge(1, 0)                                         # the first: the extent is measured
    app.processEvents()
    calls = {"copy": 0, "bbox": 0}
    real_copy, real_bbox = U.RoundTripTable.copy_part, Part.bbox
    monkeypatch.setattr(U.RoundTripTable, "copy_part",
                        lambda *a, **k: (calls.__setitem__("copy", calls["copy"] + 1), real_copy(*a, **k))[1])
    monkeypatch.setattr(Part, "bbox", lambda self: (calls.__setitem__("bbox", calls["bbox"] + 1), real_bbox(self))[1])
    before = coords(doc)
    steps = len(win.docs.stack)
    t_nudge = timed(lambda: win.nudge(1, 0), 3, gc_off=True)
    monkeypatch.undo()
    assert calls == {"copy": 0, "bbox": 0}, calls
    assert win.part_list.bbox_moved >= len(doc.parts)
    assert len(win.docs.stack) == steps                     # coalesced into the first nudge's step
    assert isinstance(win.docs.stack._undo[-1], SegmentsEdit)
    budget = runner_budget(SELECT_ALL_NUDGE_S)
    assert t_nudge < budget, (t_nudge, budget)
    after = coords(doc)
    assert after != before
    for _ in range(2):
        win.undo()                                          # all four nudges: one step
        assert coords(doc) == orig
        win.redo()
        assert coords(doc) == after
    rows = [win.part_list.topLevelItem(i).text(4) for i in (0, 999, 1999)]
    win.part_list.refresh()                                 # measured again: the same sizes
    assert rows == [win.part_list.topLevelItem(i).text(4) for i in (0, 999, 1999)]


def test_closing_a_drawing_gives_back_its_plans_and_parts(win, app, tmp_path):
    """Open, plan, then open another file: the plans, the plan codec's part encodings and the
    closed drawing's history are gone; its part objects are freed."""
    from mlaser.formats.chf import save_chf
    pa, pb = tmp_path / "a.chf", tmp_path / "b.chf"
    save_chf(small_doc(), str(pa))
    save_chf(big_document(7), str(pb))
    assert win.load_path(str(pa), confirm=False)
    from mlaser import ops
    win.docs.edit(lambda d: ops.translate(d, 1.0, 0.0, d.parts[:2]), "e", parts=win.document.parts[:2])
    plan = win.jobs.settle(timeout=120)
    assert plan is not None and win.jobs.cache
    refs = [weakref.ref(p) for p in win.document.parts] + [weakref.ref(p) for p in plan.document.parts]
    del plan
    assert win.load_path(str(pb), confirm=False)
    app.processEvents()
    assert len(win.docs.stack) == 1                          # the open; the edit is gone
    assert not win.jobs.cache and win.plan is None
    uids = {p.uid for p in win.document.parts}
    assert all(e.obj.uid in uids for e in win.jobs.codec._parts.values())
    gc.collect()
    assert sum(r() is not None for r in refs[:len(refs) // 2]) == 0     # the drawing's own parts


# --------------------------------------------------------------------------- finding 4: the review
def _review_doc(n: int) -> Document:
    d = big_document(n)
    d.parts[0].contours[0].closed = False
    d.parts[1].contours[0].segments.append(Line((1.0, 1.0), (1.0, 1.0)))
    d.parts.append(Part([Contour([Line((0.0, 0.0), (0.05, 0.0))], False)], Craft(layer=1)))
    return d


@pytest.mark.parametrize("system", ["metric", "imperial"])
def test_review_in_the_child_equals_the_review_here(system):
    """Finding for finding, message for message: the child's messages are made here, in this
    process's units (and language)."""
    from mlaser.services import repair as R
    from mlaser.services import review_process as RP
    from mlaser.services import units
    from mlaser.services.planproc import PlanCodec
    units.set_system(system)
    docs = DocumentService(_review_doc(300))
    snap = docs.plan_snapshot()
    here = R.analyze(docs.snapshot(), R.RepairOptions())
    assert {f.kind for f in here.findings} >= {"open_contour", "zero_length", "tiny_object"}
    child = RP.analyze_in_child(PlanCodec().encode_document(snap), R.RepairOptions())
    assert child == here


def test_import_review_runs_in_a_child_and_the_gui_keeps_the_interpreter(win, app, monkeypatch):
    """The review of a large drawing runs in a child process: no analysis in this process, the
    same report, and while the child works the GUI thread gets the interpreter at once (a
    CPU-bound review thread made every hand-over wait up to the 5 ms switch interval: the 3-4 s
    freeze after an open)."""
    import sys

    from mlaser.services import repair as R
    from mlaser.services import review_process as RP
    monkeypatch.setattr(RP, "available", lambda: True)
    here = []
    real = R.analyze
    monkeypatch.setattr(R, "analyze", lambda *a, **k: (here.append(1), real(*a, **k))[1])
    in_child = threading.Event()
    real_child = RP.analyze_in_child
    monkeypatch.setattr(RP, "analyze_in_child", lambda *a, **k: (in_child.set(), real_child(*a, **k))[1])
    doc = _review_doc(800)
    win.set_document(doc)
    assert win.jobs.settle(timeout=300) is not None          # no plan work while the review runs
    expected = real(win.docs.snapshot(), win.extension("orders").repair_options())
    orders = win.extension("orders")
    shown = []
    monkeypatch.setattr(orders, "show_review", lambda: shown.append(1))
    win._test_no_dialogs = False
    monkeypatch.setattr("mlaser.ui.workspaces.orders._dialogs", lambda w: True)
    reports = []
    real_reviewed = orders._reviewed
    monkeypatch.setattr(orders, "_reviewed", lambda rep, d: (reports.append(rep), real_reviewed(rep, d))[1])
    win.hooks.emit("document_imported", "big.dxf", win.document)
    task = orders._review_task
    assert task is not None and in_child.wait(60)
    # hand-overs of the interpreter while the child analyses: 100 x sleep(0) per batch
    worst = 0.0
    batches = 0
    while not task.done.is_set() and batches < 400:
        t0 = time.perf_counter()
        for _ in range(100):
            time.sleep(0)
        worst = max(worst, time.perf_counter() - t0)
        batches += 1
    t_end = time.time() + 60
    while not shown and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert shown and reports and reports[0] == expected
    assert here == []                                        # nothing analysed in this process
    # a CPU-bound thread makes 100 hand-overs cost about 100 x the switch interval (0.5 s)
    assert batches >= 1 and worst < runner_budget(100 * sys.getswitchinterval() / 5), (worst, batches)
