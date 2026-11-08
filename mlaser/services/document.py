"""DocumentService: the open document, its path, edit transactions and undo.

Every change to the document goes through :meth:`DocumentService.edit` (a transaction: the
function runs, the change is recorded as one undo command, listeners are told) or
:meth:`DocumentService.replace` (a new document object).  Plugins reach it through
``PluginContext.edit`` (:mod:`mlaser.services.plugins`).  Qt-free.

``edit(fn, label, parts=...)``: ``parts`` is the scope, the parts ``fn`` may change in place
(default: all).  ``fn`` may also add, remove and reorder parts and change ``layers`` / ``extra``.
Parts outside the scope must not be changed in place (they are not copied).  When ``fn`` raises,
the scope is restored and nothing is recorded.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable, Iterable, Optional

from mlaser.model import Contour, Document, Part, _raw_extra, ensure_unique_ids

from .undo import PartsEdit, ReplaceDocument, RoundTripTable, UndoStack, part_cost, scope_uids

Listener = Callable[[str], None]  # kind: "edit" | "replace" | "undo" | "redo"


class DocumentService:
    def __init__(
        self, document: Optional[Document] = None, undo_limit: int = 100, undo_max_bytes: int = 256 * 2**20
    ) -> None:
        self.document: Document = document or Document()
        self.path: Optional[str] = None
        self.blobs = RoundTripTable()
        self.stack = UndoStack(undo_limit, undo_max_bytes)
        self.listeners: list[Listener] = []
        self.last_edit_ms = 0.0  # time of the last edit transaction (fn + record)
        # during a notification: uids of the parts an edit / undo / redo may have changed (the
        # edit's scope, added, removed and replaced parts; a command's swapped parts); None
        # when not known (a new document, an in-place change after checkpoint, no notification
        # running).  Parts outside it are the same objects, unchanged (the edit contract), so
        # views may keep what they built for them (canvas items, part list rows; UI eval F2).
        self.changed: Optional[frozenset[int]] = None
        self._pending: Optional[tuple[Document, Optional[str]]] = None
        self._depth = 0
        # plan_snapshot: part uid -> (live part, its copy); uids edited since they were copied
        self._plan_parts: dict[int, tuple[Part, Part]] = {}
        self._plan_dirty: set[int] = set()
        # earlier (live part, copy) per uid, for the undo / redo of an edit (see plan_snapshot)
        self._plan_prev: dict[int, tuple[Part, Part]] = {}
        self.plan_hits = self.plan_misses = 0
        ensure_unique_ids(self.document)
        self.blobs.register_document(self.document)

    # ------------------------------------------------------------------ events
    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def _notify(self, kind: str, changed: Optional[Iterable[int]] = None) -> None:
        """Tell the listeners; while they run, :attr:`changed` holds the uids of the parts the
        change may have touched (None: unknown, treat every part as changed)."""
        outer = self.changed
        self.changed = frozenset(changed) if changed is not None else None
        try:
            for fn in list(self.listeners):
                fn(kind)
        finally:
            self.changed = outer

    # ------------------------------------------------------------------ copies
    def lean_copy(self, obj: Any) -> Any:
        """Deep copy that shares the round-trip records (see :class:`RoundTripTable`)."""
        return self.blobs.copy(obj)

    def snapshot(self, doc: Optional[Document] = None) -> Document:
        """A private copy of the document: geometry, crafts and layers are copied, round-trip
        records shared (they are immutable).  The caller may change it."""
        return self.lean_copy(doc if doc is not None else self.document)

    def plan_snapshot(self, doc: Optional[Document] = None) -> Document:
        """A copy of the document for the planner (``jobs.build_request``), copy-on-write.

        Equal by value to :meth:`snapshot`, but each *part* is copied only when it changed since
        it was last copied: a replan after a one-part nudge copies one part instead of the whole
        drawing (review 2 P1-3: 1.2 s at 2,000 parts, 2.4 s at 10,000, on the GUI thread).  The
        part copies are therefore shared between plan snapshots and **must not be changed**;
        ``layers`` and ``extra`` are copied per snapshot.

        A cached copy is reused only when the live part is the same object it was copied from,
        is not in the scope of an edit made since (:meth:`edit` marks its scope), and still
        compares equal to the copy by value (``Part.__eq__`` plus the part and contour uids and
        the glyph fingerprints): an in-place change that bypassed :meth:`edit`, or a change to a
        copy, is detected and the part copied again (tests/test_services_undo.py).

        The copy replaced last for each part is kept as well (about one generation): an undo or
        redo puts back part objects that equal those copies by value, so they are reused
        (``_same_part`` decides, as for the current copy) instead of copying the whole drawing
        again after the undo of a select-all edit or of an open (UI evaluation F2)."""
        doc = doc if doc is not None else self.document
        cache = self._plan_parts
        prev = self._plan_prev
        dirty = self._plan_dirty
        parts = []
        for p in doc.parts:
            uid = p.uid
            hit = cache.get(uid)
            if hit is not None and hit[0] is p and uid not in dirty and _same_part(p, hit[1]):
                parts.append(hit[1])
                self.plan_hits += 1
                continue
            # an earlier copy of this part that the live part equals again (undo / redo swap
            # the pre-edit copies back in: the plan copies made before the edit still fit)
            old = prev.get(uid)
            if old is not None and _same_part(p, old[1]):
                c = old[1]
                self.plan_hits += 1
            else:
                c = self.blobs.copy_part(p)
                self.plan_misses += 1
            if hit is not None and hit[1] is not c:
                prev[uid] = hit
            cache[uid] = (p, c)
            parts.append(c)
        dirty.difference_update(p.uid for p in doc.parts)
        if doc is self.document and len(cache) > len(doc.parts):
            live = {p.uid for p in doc.parts}
            for u in [u for u in cache if u not in live]:
                prev[u] = cache.pop(u)       # a removed part: kept for its undo
        if len(prev) > max(len(cache), 1000):          # about one earlier generation
            for u in list(prev)[: len(prev) - max(len(cache), 1000)]:
                del prev[u]
        layers, extra = self.blobs.copy_doc_state(doc)
        return Document(parts=parts, layers=layers, version=doc.version, extra=extra)

    def _plan_invalidate(self, uids=None) -> None:
        if uids is None:
            # a new document: its parts are other objects; the old copies are kept as
            # candidates (undo of the open brings back the parts they were copied from)
            self._plan_prev.update(self._plan_parts)
            self._plan_parts.clear()
            self._plan_dirty.clear()
        else:
            self._plan_dirty.update(uids)

    # ------------------------------------------------------------------ whole document
    def replace(
        self, doc: Document, path: Optional[str] = None, label: str = "replace", record: bool = True
    ) -> None:
        """Make ``doc`` the document; ``record`` keeps the old one on the undo stack.  Inside an
        :meth:`edit` the replacement becomes part of that transaction."""
        if self._depth:
            self.document, self.path = doc, path
            ensure_unique_ids(doc)
            return
        if record:
            old, old_path = self._pending or (self.document, self.path)
            self.stack.push(ReplaceDocument(label, old, old_path, cost=sum(part_cost(p) for p in old.parts)))
        self._pending = None
        self.document, self.path = doc, path
        ensure_unique_ids(doc)
        self.blobs.register_document(doc)
        self._plan_invalidate()
        self._notify("replace")

    def adopt(self, doc: Document, path: Optional[str] = None) -> None:
        """Set the document without an undo entry, unless :meth:`checkpoint` was called first
        (then the change is recorded against the checkpoint)."""
        if self._pending is not None and doc is not self.document:
            self.replace(doc, path, "replace", record=True)
            return
        if self._pending is not None and doc is self.document:
            self._commit_pending()
        self.document, self.path = doc, path
        ensure_unique_ids(doc)
        self.blobs.register_document(doc)
        self._plan_invalidate()
        self._notify("replace")

    def checkpoint(self) -> None:
        """Old-style snapshot (``MainWindow._snapshot``): remember the document as it is now; the
        next :meth:`adopt` of another document, or :meth:`commit`, records the change."""
        self._pending = (self.lean_copy(self.document), self.path)

    def _commit_pending(self) -> None:
        if self._pending is None:
            return
        old, old_path = self._pending
        self._pending = None
        self.stack.push(ReplaceDocument("edit", old, old_path, cost=sum(part_cost(p) for p in old.parts)))

    def commit(self) -> None:
        """Record an in-place change made after :meth:`checkpoint` and notify."""
        if self._pending is not None:
            self._commit_pending()
            self.blobs.register_document(self.document)
            self._plan_invalidate()
            self._notify("edit")

    # ------------------------------------------------------------------ transactions
    def edit(
        self,
        fn: Callable[[Document], Any],
        label: str = "edit",
        parts: Optional[Iterable[Part]] = None,
        notify: bool = True,
    ) -> Any:
        """Run ``fn(document)`` as one undoable change; returns its result."""
        if self._depth:  # nested edit: part of the outer transaction
            return fn(self.document)
        t0 = time.perf_counter()
        doc, path = self.document, self.path
        live_before = list(doc.parts)
        order_before = [p.uid for p in live_before]
        scope = set(scope_uids(doc, parts))
        self._plan_invalidate(scope)
        by_uid = {p.uid: p for p in live_before}
        memo: dict = {}                      # one memo: sub-objects shared in the scope stay shared
        copies = {u: self.blobs.copy_part(by_uid[u], memo) for u in order_before if u in scope}
        doc_state = self.blobs.copy_doc_state(doc)
        self._depth += 1
        try:
            result = fn(doc)
        except BaseException:
            for u, c in copies.items():  # roll the scope back
                by_uid[u] = c
            doc.parts = [by_uid[u] for u in order_before]
            doc.layers, doc.extra = doc_state
            raise
        finally:
            self._depth -= 1
        if self.document is not doc:  # fn replaced the whole document
            self.stack.push(ReplaceDocument(label, doc, path, cost=sum(part_cost(p) for p in doc.parts)))
            self.blobs.register_document(self.document)
            self.last_edit_ms = (time.perf_counter() - t0) * 1e3
            if notify:
                self._notify("replace")
            return result
        ensure_unique_ids(doc, keep=live_before)
        order_after = [p.uid for p in doc.parts]
        after = set(order_after)
        swap: dict[int, Optional[Part]] = dict(copies)
        for u in order_before:  # removed parts outside the scope: keep the object
            if u not in after and u not in swap:
                swap[u] = by_uid[u]
        for p in doc.parts:  # added parts (or replaced objects)
            if p.uid not in by_uid:
                swap[p.uid] = None
                self.blobs.register_part(p)
            elif p.uid in scope:
                self.blobs.register_part(p)
        cost = sum(part_cost(p) for p in swap.values() if p is not None)
        self.stack.push(PartsEdit(label, order_before, order_after, swap, doc_state, cost=cost))
        self.last_edit_ms = (time.perf_counter() - t0) * 1e3
        if notify:
            changed = set(swap) | scope
            changed.update(p.uid for p in doc.parts if by_uid.get(p.uid) is not p)
            self._notify("edit", changed)
        return result

    # ------------------------------------------------------------------ undo
    def undo(self) -> Optional[str]:
        changed = _swapped(self.stack._undo[-1] if self.stack._undo else None)
        label = self.stack.undo(self)
        if label is not None:
            self._notify("undo", changed)
        return label

    def redo(self) -> Optional[str]:
        changed = _swapped(self.stack._redo[-1] if self.stack._redo else None)
        label = self.stack.redo(self)
        if label is not None:
            self._notify("redo", changed)
        return label

    @property
    def can_undo(self) -> bool:
        return self.stack.can_undo

    @property
    def can_redo(self) -> bool:
        return self.stack.can_redo


def _swapped(cmd: Any) -> Optional[set[int]]:
    """uids of the parts an undo / redo of ``cmd`` exchanges (None: the whole document)."""
    if isinstance(cmd, PartsEdit):
        return set(cmd.swap)
    return None


def _same_part(live: Part, copy_: Part) -> bool:
    """``copy_`` is still an exact copy of ``live``: equal by value (``Part.__eq__`` covers the
    geometry, crafts, flags and the raw ``extra``), the same contour uids, glyph fingerprints
    and attribute sets (the fields ``__eq__`` leaves out).

    ``Part.__eq__`` / ``Contour.__eq__`` spelled out: with equal glyph fingerprints and equal
    segments both contours' glyphs are current or both are not, so the comparison skips
    ``glyphs_current`` (two segment fingerprints per contour, half the cost of a replan's
    check of every part)."""
    if live.__dict__.keys() != copy_.__dict__.keys() or len(live.contours) != len(copy_.contours):
        return False
    for a, b in zip(live.contours, copy_.contours):
        if a.uid != b.uid or a.glyph_key != b.glyph_key or a.__dict__.keys() != b.__dict__.keys():
            return False
    if type(live) is not Part or type(copy_) is not Part:
        return live == copy_
    for a, b in zip(live.contours, copy_.contours):
        if a is b:
            continue
        if type(a) is not Contour or type(b) is not Contour:
            if a != b:
                return False
            continue
        if not (a.segments == b.segments and a.closed == b.closed and a.craft == b.craft
                and a.glyphs == b.glyphs and dict.__eq__(_raw_extra(a), _raw_extra(b))):
            return False
    return (live.craft == copy_.craft and live.name == copy_.name and live.selected == copy_.selected
            and live.raster is copy_.raster and dict.__eq__(_raw_extra(live), _raw_extra(copy_)))


def clone_parts(parts: Iterable[Part]) -> list[Part]:
    """Deep copies with fresh part and contour uids (clipboard, arrays)."""
    from mlaser.model import new_id

    out = [copy.deepcopy(p) for p in parts]
    for p in out:
        p.uid = new_id()
        for c in p.contours:
            c.uid = new_id()
    return out
