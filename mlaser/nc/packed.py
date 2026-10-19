"""Packed wire items: a compiled job's FIFO items in numpy arrays instead of Python lists.

A step item as a Python ``list`` of three ints costs about 280 bytes (docs/review/architecture.md
§4.1: ~1 GB per hour of job at 1 ms per step).  :class:`PackedWire` stores the same items as

* ``words``: the item words, concatenated (uint32 when every word fits, else int64);
* runs of identical consecutive items: ``woff`` (word offset of each run's item), ``cnt`` (how
  many times it repeats) and ``flag`` (whether those items start a ManuItem expansion, the
  frame-size-test granularity of FUN_10043c20).

Runs make dwells cheap: the 200 ms JumpAddTime settle after every rapid is 200 identical
zero-delta steps on the wire (one servo period each; a step has no duration field), stored here
as one item with a count of 200.  The wire is unchanged: iterating, indexing or slicing a
``PackedWire`` gives the same lists of ints as before, and :meth:`PackedWire.frames` gives the
frames ``protocol.frames.build_fifo_frames`` builds from the expanded groups, item for item
(tests/test_packed_wire.py; the goldens hash ``Job.wire`` and ``Job.frames()``).

:meth:`PackedWire.to_shared` / :meth:`PackedWire.from_shared` move the arrays through
``multiprocessing.shared_memory`` (the hand-off to the streamer process: a handle of a few
hundred bytes is pickled instead of every item).
"""
from __future__ import annotations

from array import array
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Union

import numpy as np

_FIELDS = ("words", "woff", "cnt", "flag")


class WireBuilder:
    """Append-only collector with the ``list`` interface the sample emitter uses
    (``append(item)``, ``len()``); :meth:`starts` is the matching recorder for the emitter's
    ``starts`` list.  :meth:`finish` returns the :class:`PackedWire` (or the plain list, if an
    item held something that is not a 64-bit int)."""

    def __init__(self) -> None:
        self._words = array("q")
        self._woff = array("q", [0])
        self._cnt = array("q")
        self._flag = bytearray()
        self._n = 0
        self._last: Optional[list] = None
        self._lastflag = False
        self._pending = False
        self._plain: Optional[list] = None      # fallback storage

    # the emitter's ``starts.append(len(wire))`` right before the item that starts a group
    def mark_start(self, index: int) -> None:
        if self._plain is not None:
            self._plain_starts.append(index)
        elif index == self._n:
            self._pending = True

    def starts(self) -> "_StartRecorder":
        return _StartRecorder(self)

    def __len__(self) -> int:
        return self._n if self._plain is None else len(self._plain)

    def append(self, item) -> None:
        if self._plain is not None:
            self._plain.append(item)
            return
        flag = self._pending
        self._pending = False
        if flag == self._lastflag and self._last is not None and item == self._last:
            self._cnt[-1] += 1
            self._n += 1
            return
        n0 = len(self._words)
        try:
            self._words.extend(item)
        except (TypeError, OverflowError):
            del self._words[n0:]
            self._to_plain(item, flag)
            return
        self._woff.append(len(self._words))
        self._cnt.append(1)
        self._flag.append(1 if flag else 0)
        self._last = list(item)
        self._lastflag = flag
        self._n += 1

    def _to_plain(self, item, flag: bool) -> None:
        pw = self._pack()
        starts = list(pw.starts())
        self._plain = pw.tolist()
        self._plain_starts = starts
        if flag:
            self._plain_starts.append(len(self._plain))
        self._plain.append(item)

    def _pack(self) -> "PackedWire":
        words = np.frombuffer(self._words, dtype=np.int64) if len(self._words) else np.zeros(0, np.int64)
        if words.size and words.min() >= 0 and words.max() <= 0xFFFFFFFF:
            words = words.astype(np.uint32)
        elif not words.size:
            words = words.astype(np.uint32)
        else:
            words = words.copy()
        return PackedWire(words, np.frombuffer(self._woff, dtype=np.int64).copy(),
                          np.frombuffer(self._cnt, dtype=np.int64).copy() if len(self._cnt) else np.zeros(0, np.int64),
                          np.frombuffer(bytes(self._flag), dtype=np.uint8).astype(bool))

    def finish(self) -> tuple[Union["PackedWire", list], Optional[list]]:
        """(wire, starts): a PackedWire and None (its starts are in the runs), or the plain
        list and its start indexes when packing was abandoned."""
        if self._plain is not None:
            return self._plain, self._plain_starts
        return self._pack(), None


class _StartRecorder:
    """Stands in for the emitter's ``starts`` list (only ``append`` is used)."""

    def __init__(self, builder: WireBuilder) -> None:
        self._b = builder

    def append(self, index: int) -> None:
        self._b.mark_start(index)


@dataclass
class SharedWireHandle:
    """Picklable description of a :class:`PackedWire` in shared memory."""
    name: str
    layout: tuple                     # (field, dtype str, length, byte offset) per array
    size: int


class PackedWire(Sequence):
    """Immutable sequence of FIFO items (each a ``list[int]``) stored in numpy arrays."""

    __slots__ = ("words", "woff", "cnt", "flag", "cum", "_shm")

    def __init__(self, words: np.ndarray, woff: np.ndarray, cnt: np.ndarray, flag: np.ndarray,
                 _shm: Any = None) -> None:
        self.words, self.woff, self.cnt, self.flag = words, woff, cnt, flag
        self.cum = np.zeros(len(cnt) + 1, dtype=np.int64)
        if len(cnt):
            np.cumsum(cnt, out=self.cum[1:])
        self._shm = _shm

    @classmethod
    def from_items(cls, items, starts: Optional[Sequence[int]] = None) -> "PackedWire":
        """Pack a list of items; ``starts`` = group start indexes (default: every item)."""
        b = WireBuilder()
        st = set(range(len(items))) if starts is None else set(int(s) for s in starts)
        for i, it in enumerate(items):
            if i in st:
                b.mark_start(i)
            b.append(it)
        wire, _ = b.finish()
        if not isinstance(wire, PackedWire):
            raise TypeError("items are not lists of 64-bit ints")
        return wire

    # ------------------------------------------------------------------ sequence
    def __len__(self) -> int:
        return int(self.cum[-1])

    def __bool__(self) -> bool:
        return len(self) > 0

    def _run_item(self, r: int) -> list:
        return self.words[self.woff[r]:self.woff[r + 1]].tolist()

    def _run_of(self, i: int) -> int:
        return int(np.searchsorted(self.cum, i, side="right")) - 1

    def __getitem__(self, i):
        if isinstance(i, slice):
            start, stop, step = i.indices(len(self))
            if step != 1:
                return [self[k] for k in range(start, stop, step)]
            return list(self._iter_range(start, stop))
        n = len(self)
        i = int(i)
        if i < 0:
            i += n
        if not 0 <= i < n:
            raise IndexError("wire index out of range")
        return self._run_item(self._run_of(i))

    def _iter_range(self, start: int, stop: int) -> Iterator[list]:
        if start >= stop:
            return
        r = self._run_of(start)
        k = start
        while k < stop:
            item = self._run_item(r)
            end = min(stop, int(self.cum[r + 1]))
            for _ in range(end - k):
                yield list(item)
            k = end
            r += 1

    def __iter__(self) -> Iterator[list]:
        return self._iter_range(0, len(self))

    def __eq__(self, other) -> bool:
        if isinstance(other, PackedWire):
            if len(self) != len(other):
                return False
            if (np.array_equal(self.cnt, other.cnt) and np.array_equal(self.woff, other.woff)
                    and np.array_equal(self.words.astype(np.int64), other.words.astype(np.int64))):
                return True
            return all(a == b for a, b in zip(self, other))
        if isinstance(other, (list, tuple)):
            return len(self) == len(other) and all(a == list(b) for a, b in zip(self, other))
        return NotImplemented

    def __ne__(self, other) -> bool:
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"PackedWire({len(self)} items, {len(self.cnt)} runs, {self.nbytes} bytes)"

    def tolist(self) -> list[list[int]]:
        return list(self)

    @property
    def nbytes(self) -> int:
        return int(sum(getattr(self, f).nbytes for f in _FIELDS) + self.cum.nbytes)

    # ------------------------------------------------------------------ groups and frames
    def starts(self) -> Iterator[int]:
        """Indexes of the items that start a ManuItem expansion (the emitter's ``starts``)."""
        for r in np.flatnonzero(self.flag).tolist():
            yield from range(int(self.cum[r]), int(self.cum[r + 1]))

    def _item_arrays(self):
        run = np.repeat(np.arange(len(self.cnt)), self.cnt)
        ln = np.diff(self.woff)[run]
        ws = np.zeros(len(run) + 1, dtype=np.int64)
        np.cumsum(ln, out=ws[1:])
        return run, ln, ws

    def groups(self) -> list[list[list[int]]]:
        """``Job.groups()`` of the emitter's wire: one group per start, items before the first
        start dropped (as the list version does)."""
        st = list(self.starts()) + [len(self)]
        return [self[a:b] for a, b in zip(st, st[1:]) if b > a]

    def iter_frames(self, first_seq: int = 1, max_bytes: int = 0x4B0) -> Iterator[list[int]]:
        """``build_fifo_frames(groups(), first_seq, max_bytes)``, one frame at a time."""
        n = len(self)
        if n == 0:
            return
        run, ln, ws = self._item_arrays()
        is_start = self.flag[run]
        gs = np.flatnonzero(is_start)
        if gs.size == 0:
            return
        # group end word offsets: the next group's start, the last group ends with the stream
        ge = np.append(ws[gs[1:]], ws[n])
        lmin = 0
        while not (lmin + 4) * 4 > max_bytes - 1:
            lmin += 1
        src0 = self.woff[run] - ws[:-1]          # word index in ``words`` = src0[item] + stream offset
        b = int(ws[gs[0]])
        end = int(ws[n])
        seq = first_seq
        while b < end:
            e = int(np.searchsorted(ge, b + lmin, side="left"))
            stop = int(ge[e]) if e < len(ge) else end
            k0 = int(np.searchsorted(ws, b, side="right")) - 1
            k1 = int(np.searchsorted(ws, stop, side="left"))
            idx = np.repeat(src0[k0:k1], ln[k0:k1]) + np.arange(b, stop)
            yield [seq & 0xFFFFFFFF, *self.words[idx].tolist()]
            seq += 1
            b = stop

    def frames(self, first_seq: int = 1, max_bytes: int = 0x4B0) -> list[list[int]]:
        return list(self.iter_frames(first_seq, max_bytes))

    # ------------------------------------------------------------------ shared memory
    def to_shared(self) -> tuple[SharedWireHandle, Any]:
        """Copy the arrays into a new ``multiprocessing.shared_memory`` block.  Returns the
        picklable handle and the ``SharedMemory`` object; the creator owns the block: keep the
        object alive while a reader uses it, then ``close()`` and ``unlink()`` it."""
        from multiprocessing import shared_memory
        arrs = [(f, np.ascontiguousarray(getattr(self, f))) for f in _FIELDS]
        layout, off = [], 0
        for f, a in arrs:
            off = (off + 15) & ~15
            layout.append((f, a.dtype.str, int(a.size), off))
            off += a.nbytes
        shm = shared_memory.SharedMemory(create=True, size=max(off, 16))
        for (f, a), (_f, dt, size, o) in zip(arrs, layout):
            np.ndarray((size,), dtype=dt, buffer=shm.buf, offset=o)[:] = a
        return SharedWireHandle(shm.name, tuple(layout), max(off, 16)), shm

    @classmethod
    def from_shared(cls, handle: SharedWireHandle, copy: bool = False) -> "PackedWire":
        """Attach to a block made by :meth:`to_shared` (zero-copy views unless ``copy``).  The
        returned wire keeps the mapping open; call :meth:`close` when done with it."""
        from multiprocessing import shared_memory
        shm = shared_memory.SharedMemory(name=handle.name)
        arrs = {}
        for f, dt, size, o in handle.layout:
            a = np.ndarray((size,), dtype=dt, buffer=shm.buf, offset=o)
            arrs[f] = a.copy() if copy else a
        if copy:
            pw = cls(arrs["words"], arrs["woff"], arrs["cnt"], arrs["flag"])
            shm.close()
            return pw
        return cls(arrs["words"], arrs["woff"], arrs["cnt"], arrs["flag"], _shm=shm)

    def close(self) -> None:
        """Release a shared-memory mapping (the arrays become unusable)."""
        shm, self._shm = self._shm, None
        if shm is not None:
            for f in _FIELDS:
                setattr(self, f, getattr(self, f).copy()[:0])
            self.cum = np.zeros(1, dtype=np.int64)
            shm.close()
