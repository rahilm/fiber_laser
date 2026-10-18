"""DocumentService / command undo (mlaser.services, Qt-free): stable ids, scoped commands, the
round-trip side table, byte-exact .chf after edit/undo/redo, bounded cost."""
from __future__ import annotations

import copy
import pathlib
import time

import pytest

from mlaser import ops
from mlaser.model import Contour, Document, Line, Part, ensure_unique_ids
from mlaser.services.document import DocumentService, clone_parts
from mlaser.services.undo import UndoStack

SAMPLE = pathlib.Path(__file__).parent / "data" / "autosave.chf"


def rect(x, y, w=10.0, h=10.0):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def doc3():
    return Document(parts=[Part([rect(0, 0)], name="a"), Part([rect(20, 0)], name="b"),
                           Part([rect(40, 0)], name="c")])


def geometry(doc):
    return [(p.name, [list(c.segments) for c in p.contours], p.craft.layer, p.selected) for p in doc.parts]


def test_ids_unique_and_kept_by_deepcopy():
    d = doc3()
    uids = [p.uid for p in d.parts]
    assert len(set(uids)) == 3
    assert copy.deepcopy(d.parts[0]).uid == d.parts[0].uid      # a snapshot is the same object
    d.parts.append(copy.deepcopy(d.parts[0]))                    # ... a pasted copy is not
    assert ensure_unique_ids(d, keep=d.parts[:3]) >= 1
    assert d.parts[0].uid == uids[0] and len({p.uid for p in d.parts}) == 4
    assert clone_parts(d.parts[:1])[0].uid != d.parts[0].uid
    # uid takes no part in equality (file formats and comparisons unchanged)
    assert copy.deepcopy(d.parts[1]) == d.parts[1]
    c = copy.deepcopy(d.parts[1])
    c.uid = 0
    assert c == d.parts[1]


def test_edit_undo_redo_scoped():
    svc = DocumentService(doc3())
    before = geometry(svc.document)
    b = svc.document.parts[1]
    b.selected = True
    sel = [b]
    svc.edit(lambda d: ops.translate(d, 5, 0, sel), "move", parts=sel)
    after = geometry(svc.document)
    cmd = svc.stack._undo[-1]
    assert set(cmd.swap) == {b.uid}                 # only the scope was copied
    svc.document.parts[1].selected = False
    assert svc.undo() == "move"
    assert geometry(svc.document)[1][1] == before[1][1]
    assert svc.document.parts[0] is not None and svc.document.parts[0].name == "a"
    assert svc.redo() == "move"
    assert geometry(svc.document)[1][1] == after[1][1]
    assert svc.document.parts[1].uid == b.uid


def test_add_remove_reorder_and_rollback():
    svc = DocumentService(doc3())
    names = lambda: [p.name for p in svc.document.parts]  # noqa: E731
    svc.edit(lambda d: d.parts.append(Part([rect(60, 0)], name="d")), "add", parts=())
    svc.edit(lambda d: d.parts.pop(0), "remove", parts=())
    svc.edit(lambda d: d.parts.reverse(), "reorder", parts=())
    assert names() == ["d", "c", "b"]
    svc.undo()
    assert names() == ["b", "c", "d"]
    svc.undo()
    assert names() == ["a", "b", "c", "d"]
    svc.undo()
    assert names() == ["a", "b", "c"]
    svc.redo(); svc.redo(); svc.redo()
    assert names() == ["d", "c", "b"]

    def boom(d):
        d.parts[0].contours = []
        d.parts.append(Part(name="x"))
        raise RuntimeError("fail")
    n = len(svc.stack)
    with pytest.raises(RuntimeError):
        svc.edit(boom, "boom")
    assert names() == ["d", "c", "b"] and svc.document.parts[0].contours and len(svc.stack) == n


def test_replace_document_and_checkpoint():
    svc = DocumentService(doc3())
    first = svc.document
    svc.replace(Document(parts=[Part(name="new")]), label="open")
    assert [p.name for p in svc.document.parts] == ["new"]
    svc.undo()
    assert svc.document is first
    svc.redo()
    assert [p.name for p in svc.document.parts] == ["new"]
    # the MainWindow._snapshot pattern: checkpoint, then adopt another document
    svc.checkpoint()
    svc.adopt(Document(parts=[Part(name="other")]))
    svc.undo()
    assert [p.name for p in svc.document.parts] == ["new"]


def test_bounded_stack():
    st = UndoStack(limit=3)
    svc = DocumentService(doc3())
    svc.stack = st
    for i in range(6):
        svc.edit(lambda d: ops.translate(d, 1, 0), f"m{i}")
    assert len(st) == 3 and st.labels() == ["m3", "m4", "m5"]
    st2 = UndoStack(limit=100, max_cost=1)
    svc.stack = st2
    svc.edit(lambda d: ops.translate(d, 1, 0), "a")
    svc.edit(lambda d: ops.translate(d, 1, 0), "b")
    assert len(st2) == 1                             # over budget: only the newest is kept


@pytest.fixture(scope="module")
def sample_bytes():
    if not SAMPLE.exists():
        pytest.skip("sample .chf missing")
    return SAMPLE.read_bytes()


def test_chf_byte_exact_through_edit_undo_redo(sample_bytes):
    from mlaser.formats.chf import dumps_chf, loads_chf
    svc = DocumentService(loads_chf(sample_bytes))
    assert dumps_chf(svc.document) == sample_bytes
    edited = None
    for ang in (90, 90, -45):
        svc.edit(lambda d, a=ang: ops.rotate(d, a), "rotate", parts=ops.targets(svc.document))
        edited = dumps_chf(svc.document)
    assert edited != sample_bytes
    for _ in range(3):
        svc.undo()
    assert dumps_chf(svc.document) == sample_bytes
    for _ in range(3):
        svc.redo()
    assert dumps_chf(svc.document) == edited


def test_round_trip_blobs_are_shared_not_copied(sample_bytes):
    from mlaser.formats.chf import loads_chf
    svc = DocumentService(loads_chf(sample_bytes))
    p = svc.document.parts[0]
    assert len(svc.blobs) > 0 and svc.blobs.get(p.uid, "chf") is p.extra["chf"]
    svc.edit(lambda d: ops.rotate(d, 90), "rotate")
    saved = svc.stack._undo[-1].swap[p.uid]
    assert saved is not p
    assert saved.extra["chf"] is p.extra["chf"]                       # the record is shared
    assert saved.extra["_chf_snapshot"] is p.extra["_chf_snapshot"]
    assert saved.contours[0].extra["chf"] is p.contours[0].extra["chf"]
    assert saved.contours[0].segments is not p.contours[0].segments   # geometry is copied


def test_edit_cost_on_sample(sample_bytes):
    """Measured: a whole-document deepcopy (the old undo snapshot) against one edit command.
    The bound here is loose (shared CI machines); the measured numbers are in the report."""
    from mlaser.formats.chf import loads_chf
    doc = loads_chf(sample_bytes)
    t = time.perf_counter()
    copy.deepcopy(doc)
    old_ms = (time.perf_counter() - t) * 1e3
    svc = DocumentService(doc)
    best = 1e9
    for _ in range(3):
        t = time.perf_counter()
        svc.edit(lambda d: None, "noop")
        best = min(best, (time.perf_counter() - t) * 1e3)
    assert best < old_ms
    assert svc.stack.cost < 4 * 2 ** 20
