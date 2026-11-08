"""Command-based undo (QUndoStack style) with stable ids and a side table for round-trip blobs.

Before Wave 2 every edit deep-copied the whole document and kept 50 copies (89.7 ms per edit on
the original's 839-element ``autosave.chf`` sample, about 90 % of it the ``.chf`` round-trip
records; architecture.md §1.6, P1-7).  Here:

* **Commands.**  An edit is a :class:`PartsEdit` command that holds only what it needs to swap
  back: copies of the parts in its *scope* (the selection for most ribbon commands), the uids of
  the part list before and after, and the document's ``layers`` / ``extra``.  Undo and redo swap
  the stored objects with the live ones, so neither copies anything.  A whole-document change
  (open, new, nest result to main view) is a :class:`ReplaceDocument` command.
* **Stable ids.**  Parts and contours carry ``uid`` (:mod:`mlaser.model`); commands address parts
  by uid, so reordering, nesting or selection never confuse undo.
* **Side table.**  The round-trip records the ``.chf`` codec keeps in the model
  (``Part.extra['chf']``, ``['_chf_snapshot']``, ``Contour.extra['chf']``, source glyphs,
  ``Document.extra['chf']``) are immutable once created: the writer copies before it edits.
  :class:`RoundTripTable` registers them by (uid, key) and every snapshot copy *shares* them
  (a deepcopy memo seeded with the table), so they are never deep-copied per edit.
* **Bounded by memory.**  :class:`UndoStack` drops the oldest commands beyond ``max_cost``
  estimated bytes (and beyond ``limit`` commands when a limit is given).
* **Coalesced.**  Edits pushed with the same ``merge_key`` in quick succession (arrow-key
  nudges of one selection) merge into one command (:meth:`UndoStack.push`).

No Qt: the stack is a plain object, the application's ``edit.undo`` / ``edit.redo`` commands call
it through :class:`mlaser.services.document.DocumentService`.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol, cast

from mlaser.model import Document, Part

# --------------------------------------------------------------------------- side table
PART_BLOB_KEYS = ("chf", "_chf_snapshot")
CONTOUR_BLOB_KEYS = ("chf",)
CONTOUR_BLOB_ATTRS = ("glyphs", "glyph_key")  # typed source glyphs and their segment fingerprint
DOC_BLOB_KEYS = ("chf",)


def contour_blobs(c) -> list[tuple[str, Any]]:
    """(key, record) of the immutable round-trip records a contour holds."""
    cx = c.extra if isinstance(c.extra, dict) else {}
    out = [(k, cx.get(k)) for k in CONTOUR_BLOB_KEYS]
    out += [(k, getattr(c, k, None)) for k in CONTOUR_BLOB_ATTRS]
    return out


def _raw_extra_get(o: Any, key: str) -> Any:
    """``o.extra[key]`` read from the stored dict (no LegacyExtra wrapping, no warning)."""
    ex = o.__dict__.get("extra")
    return dict.get(ex, key) if isinstance(ex, dict) else None


class RoundTripTable:
    """Immutable round-trip records keyed by (owner uid, key).

    The records stay referenced from the model's ``extra`` dicts (the ``.chf`` codec and the
    parity ordering read them there), but they are owned here: a record is never mutated after
    it is registered, and :meth:`memo` lets ``copy.deepcopy`` share it instead of copying it.
    Registering again under the same key replaces the entry (an edit that rebuilt the record);
    the old record lives on only inside undo snapshots that still refer to it.

    Copies (:meth:`copy`, :meth:`copy_part`) use :func:`fast_deepcopy`: the same result as
    ``copy.deepcopy`` with the records shared, several times faster on the model's plain
    dataclasses.  :meth:`copy_part` seeds its memo with the part's own records only: one
    whole-table memo per part made a 2,700-part copy quadratic (5 s on the GUI thread after an
    import, UI evaluation F2)."""

    def __init__(self) -> None:
        self._blobs: dict[tuple[int, str], Any] = {}
        self._ids: Optional[dict[int, Any]] = None     # id(record) -> record (lazy)

    def __len__(self) -> int:
        return len(self._blobs)

    def get(self, uid: int, key: str) -> Any:
        return self._blobs.get((uid, key))

    def _set(self, key: tuple[int, str], v: Any) -> None:
        if v is not None:
            if self._blobs.get(key) is not v:
                self._blobs[key] = v
                self._ids = None
        elif self._blobs.pop(key, None) is not None:
            self._ids = None

    def register_part(self, part: Part) -> None:
        ex = part.extra if isinstance(part.extra, dict) else {}
        for k in PART_BLOB_KEYS:
            self._set((part.uid, k), ex.get(k))
        for c in part.contours:
            for k, v in contour_blobs(c):
                self._set((c.uid, k), v)

    def register_document(self, doc: Document) -> None:
        """(Re)build the table from ``doc`` (open / replace)."""
        self._blobs.clear()
        self._ids = None
        for p in doc.parts:
            self.register_part(p)
        ex = doc.extra if isinstance(doc.extra, dict) else {}
        for k in DOC_BLOB_KEYS:
            if ex.get(k) is not None:
                self._blobs[(0, k)] = ex[k]

    def ids(self) -> dict[int, Any]:
        """id(record) -> record of every registered record (cached until the table changes)."""
        if self._ids is None:
            self._ids = {id(v): v for v in self._blobs.values()}
        return self._ids

    def memo(self) -> dict:
        """A fresh ``copy.deepcopy`` memo in which every record maps to itself (shared)."""
        return dict(self.ids())

    def copy(self, obj):
        """``copy.deepcopy(obj)`` that shares the registered records."""
        return fast_deepcopy(obj, self.memo())

    def part_memo(self, part: Part) -> dict:
        """The deepcopy memo of one part: its registered records (under its own and its
        contours' uids, and those its ``extra`` slots hold) map to themselves."""
        ids = self.ids()
        blobs = self._blobs
        memo: dict = {}

        def add(v: Any) -> None:
            if v is not None and id(v) in ids:
                memo[id(v)] = v

        for k in PART_BLOB_KEYS:
            add(blobs.get((part.uid, k)))
            add(_raw_extra_get(part, k))
        for c in part.contours:
            for k in CONTOUR_BLOB_KEYS:
                add(blobs.get((c.uid, k)))
                add(_raw_extra_get(c, k))
            for k in CONTOUR_BLOB_ATTRS:
                add(blobs.get((c.uid, k)))
                add(c.__dict__.get(k))
        return memo

    def copy_part(self, part: Part, memo: Optional[dict] = None) -> Part:
        """A copy of one part that shares its registered records (equal by value to
        :meth:`copy`; ``memo`` continues a copy of several parts, keeping shared sub-objects
        shared as one ``deepcopy`` of all of them would)."""
        if memo is None:
            return fast_deepcopy(part, self.part_memo(part))
        memo.update(self.part_memo(part))
        return fast_deepcopy(part, memo)

    def copy_parts(self, parts: Iterable[Part]) -> list[Part]:
        """Copies of ``parts`` made with one memo (sub-objects shared between them stay shared)."""
        memo: dict = {}
        return [self.copy_part(p, memo) for p in parts]

    def copy_doc_state(self, doc: Document) -> tuple:
        """Copies of ``doc.layers`` and ``doc.extra`` (the document's own record shared)."""
        memo: dict = {}
        for k in DOC_BLOB_KEYS:
            v = self._blobs.get((0, k))
            if v is not None:
                memo[id(v)] = v
            v = doc.extra.get(k) if isinstance(doc.extra, dict) else None
            if v is not None and id(v) in self.ids():
                memo[id(v)] = v
        return fast_deepcopy(doc.layers, memo), fast_deepcopy(doc.extra, memo)


# --------------------------------------------------------------------------- fast deep copy
_ATOMIC = frozenset({int, float, bool, str, bytes, complex, type(None), type})
_PLAIN: Optional[frozenset] = None


def _plain_classes() -> frozenset:
    """The model classes ``copy.deepcopy`` copies by ``cls.__new__`` + a deep copy of
    ``__dict__`` (no slots, no custom reduce / getstate / setstate / deepcopy): for those
    :func:`fast_deepcopy` does the same directly."""
    global _PLAIN
    if _PLAIN is None:
        from mlaser.model import Arc, Contour, Craft, Glyph, LayerParams, LeadParams, Line

        ok = []
        for cls in (Part, Contour, Craft, Line, Arc, LeadParams, Glyph, LayerParams, Document):
            if ("__slots__" in cls.__dict__ or getattr(cls, "__deepcopy__", None) is not None
                    or getattr(cls, "__setstate__", None) is not None
                    or cls.__reduce_ex__ is not object.__reduce_ex__
                    or cls.__reduce__ is not object.__reduce__
                    or getattr(cls, "__getstate__", object.__getstate__) is not object.__getstate__):
                continue
            ok.append(cls)
        _PLAIN = frozenset(ok)
    return _PLAIN


def fast_deepcopy(x: Any, memo: Optional[dict] = None) -> Any:
    """``copy.deepcopy(x, memo)`` with fast paths for the document model.

    The model's plain dataclasses (:func:`_plain_classes`), exact lists, dicts and tuples, and
    the ``extra`` views (``LegacyExtra``) are copied here exactly as ``copy.deepcopy`` copies
    them (same memo protocol: shared and cyclic references stay shared; objects in ``memo``
    are returned as they are); every other object goes to ``copy.deepcopy`` with the same memo.
    deepcopy's keep-alive list is left out: every original is reachable from ``x``, which the
    caller holds while the memo is in use.
    The result is equal by value and by structure to ``copy.deepcopy`` (tests/test_services_undo.py)."""
    from mlaser.model import LegacyExtra

    if memo is None:
        memo = {}
    plain = _plain_classes()
    atomic = _ATOMIC
    deep = copy.deepcopy
    get = memo.get

    def flat(v: Any) -> bool:
        # a tuple of atomic values: deepcopy returns the tuple itself (and memoizes nothing)
        for a in v:
            if type(a) not in atomic:
                return False
        return True

    def dc(o: Any) -> Any:
        t = type(o)
        if t in atomic:
            return o
        if t is tuple and flat(o):
            return o
        i = id(o)
        y = get(i, memo)
        if y is not memo:
            return y
        if t in plain:
            y = cast(Any, t).__new__(t)
            memo[i] = y
            d = y.__dict__
            for k, v in o.__dict__.items():
                tv = type(v)
                d[k] = v if tv in atomic or (tv is tuple and flat(v)) else dc(v)
            return y
        if t is list:
            y = []
            memo[i] = y
            append = y.append
            for v in o:
                tv = type(v)
                append(v if tv in atomic else dc(v))
            return y
        if t is dict:
            y = {}
            memo[i] = y
            for k, v in o.items():
                y[k if type(k) in atomic else dc(k)] = v if type(v) in atomic else dc(v)
            return y
        if t is tuple:
            vals = [dc(v) for v in o]
            if i in memo:                     # a cycle through the tuple (deepcopy's rule)
                return memo[i]
            for a, b in zip(vals, o):
                if a is not b:
                    y = tuple(vals)
                    memo[i] = y
                    return y
            return o
        if t is LegacyExtra:
            owner = o._ref()
            new_owner = memo.get(id(owner)) if owner is not None else None
            if new_owner is not None:
                y = LegacyExtra(new_owner)
                memo[i] = y
                for k, v in dict.items(o):
                    dict.__setitem__(y, k, dc(v))
                return y
        return deep(o, memo)

    return dc(x)


def part_cost(p: Part) -> int:
    """Rough retained size of a part copy in bytes (segments dominate; blobs are shared)."""
    return 600 + sum(200 + 160 * len(c.segments) for c in p.contours)


# --------------------------------------------------------------------------- commands
class Command(Protocol):
    """An undoable change that has already been applied when it is pushed."""

    label: str
    cost: int

    def undo(self, target: "UndoTarget") -> None: ...
    def redo(self, target: "UndoTarget") -> None: ...


class UndoTarget(Protocol):
    document: Document
    blobs: RoundTripTable


@dataclass
class PartsEdit:
    """Swap command for an edit of some parts (plus the part order, layers and doc extra).

    ``swap[uid]`` holds the part object of the *other* state (None = the part does not exist
    there): before the first undo that is the pre-edit copy; each undo/redo exchanges it with the
    live object.  ``order_before`` / ``order_after`` are uid lists.  Parts outside the swap set
    are untouched by the edit and stay the same live objects."""

    label: str
    order_before: list[int]
    order_after: list[int]
    swap: dict[int, Optional[Part]]
    doc_state: tuple  # (layers, extra) of the other state
    cost: int = 0
    stamp: float = field(default_factory=time.time)
    #: edits with the same key that follow each other closely are one undo step (nudges)
    merge_key: Any = None

    def can_absorb(self, newer: Any) -> bool:
        return (isinstance(newer, PartsEdit) and self.merge_key is not None and newer.merge_key == self.merge_key
                and newer.order_before == self.order_after)

    def absorb(self, newer: "PartsEdit") -> None:
        """Take ``newer`` (applied after this one, not undone) into this command: the states
        before this one stay, the state after is ``newer``'s.  Only the parts ``newer`` touched
        that this one did not are added (their pre-``newer`` copy is their state before this)."""
        for uid, other in newer.swap.items():
            if uid not in self.swap:
                self.swap[uid] = other
        self.order_after = newer.order_after
        self.cost = sum(part_cost(p) for p in self.swap.values() if p is not None)
        self.stamp = newer.stamp

    def _apply(self, t: UndoTarget, order: list[int]) -> None:
        doc = t.document
        live = {p.uid: p for p in doc.parts}
        swap: dict[int, Optional[Part]] = {}
        for uid, other in self.swap.items():
            swap[uid] = live.get(uid)
            if other is None:
                live.pop(uid, None)
            else:
                live[uid] = other
        self.swap = swap
        doc.parts = [live[u] for u in order if u in live]
        layers, extra = self.doc_state
        self.doc_state = (doc.layers, doc.extra)
        doc.layers, doc.extra = layers, extra
        for uid, p in live.items():
            if uid in swap:
                t.blobs.register_part(p)

    def undo(self, t: UndoTarget) -> None:
        self._apply(t, self.order_before)

    def redo(self, t: UndoTarget) -> None:
        self._apply(t, self.order_after)


@dataclass
class ReplaceDocument:
    """The document object itself was replaced (open, new, nest to main view, ...)."""

    label: str
    other: Document
    other_path: Optional[str] = None
    cost: int = 0
    stamp: float = field(default_factory=time.time)

    def _swap(self, t: Any) -> None:
        cur, cur_path = t.document, getattr(t, "path", None)
        t.document = self.other
        if hasattr(t, "path"):
            t.path = self.other_path
        self.other, self.other_path = cur, cur_path
        t.blobs.register_document(t.document)

    undo = _swap
    redo = _swap


# --------------------------------------------------------------------------- stack
class UndoStack:
    """Command history bounded by memory: about ``max_cost`` bytes of retained copies, and at
    most ``limit`` commands when one is given (None: no count limit); the oldest commands are
    dropped first.

    **Coalescing.**  A pushed command whose ``merge_key`` equals the top command's, within
    :attr:`merge_window` seconds of it, is merged into it (``absorb``): five arrow-key nudges
    of the same selection are one undo step.  Any other push, an undo, a redo or :meth:`seal`
    ends the run."""

    #: seconds between two edits of one coalesced step
    merge_window = 1.5

    def __init__(self, limit: Optional[int] = None, max_cost: int = 256 * 2**20) -> None:
        self.limit = limit
        self.max_cost = max_cost
        self._undo: list[Any] = []
        self._redo: list[Any] = []

    def __len__(self) -> int:
        return len(self._undo)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_text(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_text(self) -> str:
        return self._redo[-1].label if self._redo else ""

    @property
    def cost(self) -> int:
        return sum(c.cost for c in self._undo) + sum(c.cost for c in self._redo)

    def labels(self) -> list[str]:
        return [c.label for c in self._undo]

    def push(self, cmd: Any) -> None:
        """Record an already applied command; clears the redo history.  Merged into the top
        command when both have the same ``merge_key`` and follow each other closely."""
        top = self._undo[-1] if self._undo else None
        if (top is not None and not self._redo and getattr(cmd, "merge_key", None) is not None
                and getattr(top, "can_absorb", None) is not None and top.can_absorb(cmd)
                and 0.0 <= cmd.stamp - top.stamp <= self.merge_window):
            top.absorb(cmd)
            self._trim()
            return
        self._undo.append(cmd)
        self._redo.clear()
        self._trim()

    def seal(self) -> None:
        """End a coalescing run: the next edit is a step of its own (call it on save, so the
        saved state is a step boundary)."""
        if self._undo and getattr(self._undo[-1], "merge_key", None) is not None:
            self._undo[-1].merge_key = None

    def _trim(self) -> None:
        while self.limit is not None and len(self._undo) > self.limit:
            self._undo.pop(0)
        while len(self._undo) > 1 and self.cost > self.max_cost:
            self._undo.pop(0)

    def undo(self, target: Any) -> Optional[str]:
        if not self._undo:
            return None
        cmd = self._undo.pop()
        cmd.undo(target)
        self._redo.append(cmd)
        self.seal()
        return cmd.label

    def redo(self, target: Any) -> Optional[str]:
        if not self._redo:
            return None
        cmd = self._redo.pop()
        cmd.redo(target)
        self._undo.append(cmd)
        self.seal()
        return cmd.label

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


def scope_uids(doc: Document, parts: Optional[Iterable[Part]]) -> list[int]:
    """uids of the parts an edit may change (all parts when ``parts`` is None)."""
    if parts is None:
        return [p.uid for p in doc.parts]
    return [p.uid for p in parts]
