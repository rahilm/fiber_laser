"""Command-based undo (QUndoStack style) with stable ids and a side table for round-trip blobs.

Before Wave 2 every edit deep-copied the whole document and kept 50 copies (89.7 ms per edit on
``tests/data/autosave.chf``, about 90 % of it the ``.chf`` round-trip records; architecture.md
§1.6, P1-7).  Here:

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
* **Bounded.**  :class:`UndoStack` drops the oldest commands beyond ``limit`` commands or beyond
  ``max_cost`` estimated bytes.

No Qt: the stack is a plain object, the application's ``edit.undo`` / ``edit.redo`` commands call
it through :class:`mlaser.services.document.DocumentService`.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from mlaser.model import Document, Part

# --------------------------------------------------------------------------- side table
PART_BLOB_KEYS = ("chf", "_chf_snapshot")
CONTOUR_BLOB_KEYS = ("chf", "glyphs", "glyph_segments")
DOC_BLOB_KEYS = ("chf",)


class RoundTripTable:
    """Immutable round-trip records keyed by (owner uid, key).

    The records stay referenced from the model's ``extra`` dicts (the ``.chf`` codec and the
    parity ordering read them there), but they are owned here: a record is never mutated after
    it is registered, and :meth:`memo` lets ``copy.deepcopy`` share it instead of copying it.
    Registering again under the same key replaces the entry (an edit that rebuilt the record);
    the old record lives on only inside undo snapshots that still refer to it."""

    def __init__(self) -> None:
        self._blobs: dict[tuple[int, str], Any] = {}

    def __len__(self) -> int:
        return len(self._blobs)

    def get(self, uid: int, key: str) -> Any:
        return self._blobs.get((uid, key))

    def register_part(self, part: Part) -> None:
        ex = part.extra if isinstance(part.extra, dict) else {}
        for k in PART_BLOB_KEYS:
            v = ex.get(k)
            if v is not None:
                self._blobs[(part.uid, k)] = v
            else:
                self._blobs.pop((part.uid, k), None)
        for c in part.contours:
            cx = c.extra if isinstance(c.extra, dict) else {}
            for k in CONTOUR_BLOB_KEYS:
                v = cx.get(k)
                if v is not None:
                    self._blobs[(c.uid, k)] = v
                else:
                    self._blobs.pop((c.uid, k), None)

    def register_document(self, doc: Document) -> None:
        """(Re)build the table from ``doc`` (open / replace)."""
        self._blobs.clear()
        for p in doc.parts:
            self.register_part(p)
        ex = doc.extra if isinstance(doc.extra, dict) else {}
        for k in DOC_BLOB_KEYS:
            if ex.get(k) is not None:
                self._blobs[(0, k)] = ex[k]

    def memo(self) -> dict:
        """A fresh ``copy.deepcopy`` memo in which every record maps to itself (shared)."""
        return {id(v): v for v in self._blobs.values()}

    def copy(self, obj):
        """``copy.deepcopy(obj)`` that shares the registered records."""
        return copy.deepcopy(obj, self.memo())


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
    """Bounded command history: at most ``limit`` commands and about ``max_cost`` bytes of
    retained copies (the oldest commands are dropped first)."""

    def __init__(self, limit: int = 100, max_cost: int = 256 * 2**20) -> None:
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
        """Record an already applied command; clears the redo history."""
        self._undo.append(cmd)
        self._redo.clear()
        self._trim()

    def _trim(self) -> None:
        while len(self._undo) > self.limit:
            self._undo.pop(0)
        while len(self._undo) > 1 and self.cost > self.max_cost:
            self._undo.pop(0)

    def undo(self, target: Any) -> Optional[str]:
        if not self._undo:
            return None
        cmd = self._undo.pop()
        cmd.undo(target)
        self._redo.append(cmd)
        return cmd.label

    def redo(self, target: Any) -> Optional[str]:
        if not self._redo:
            return None
        cmd = self._redo.pop()
        cmd.redo(target)
        self._undo.append(cmd)
        return cmd.label

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


def scope_uids(doc: Document, parts: Optional[Iterable[Part]]) -> list[int]:
    """uids of the parts an edit may change (all parts when ``parts`` is None)."""
    if parts is None:
        return [p.uid for p in doc.parts]
    return [p.uid for p in parts]
