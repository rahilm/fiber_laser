"""The plan compile in a separate process (review 2 P1-1), and the request codec it uses.

A compile is CPU-bound Python (11.5 µs per step with the default smoothing: about two minutes for
a 2,000-part drawing).  In a thread of the GUI process it held the GIL: ``canvas.set_toolpath``
took 10.6 s instead of 0.2 s, the event loop waited up to 19 s, and a Stop pressed while the
operator edited during a job waited up to 12.7 s (review 2 §1.3, §3.2).  Here the compile runs
in one persistent child process (``multiprocessing`` *spawn*, so it works the same on Windows,
macOS and in a frozen build, where ``multiprocessing.freeze_support()`` turns the re-run
executable into the worker).  The child runs exactly the code the thread ran
(``plan_toolpath`` + ``compile_items`` under the request's ParityProfile), so the plan is the
same; the parent only moves data:

* **Request** (:class:`PlanCodec`): the document is pickled **per part**, and a part's bytes are
  cached while the planner snapshot keeps the same part copy (``DocumentService.plan_snapshot``
  is copy-on-write), so a replan after a one-part edit sends one part.  The immutable
  round-trip records (``services.undo.RoundTripTable``) go once by token and are shared in the
  child.  The same per-part cache holds each part's digest for ``jobs.inputs_hash``.
* **Result**: the toolpath (sent first, the canvas draws it while the compile runs) and the job's
  bookkeeping lists come back pickled in chunks (the parent unpickles them a chunk at a time, so
  its GIL is never held long); the packed wire comes back through shared memory
  (``Job.to_shared`` / ``Job.from_shared``, nc/packed.py), or through the pipe when no shared
  memory is available.
* **Progress and cancel**: the child reports progress about 10 times a second; a cancel is a
  shared counter (no lock) the child checks at every progress call (``compile_items`` calls it
  after each chain).

When the child cannot be started or dies, or cannot reproduce the parent's state (a ParityProfile
registered at run time), the JobService compiles in its thread as before, with the GIL switch
interval lowered meanwhile (``controller.stream.responsive_gil``).
"""

from __future__ import annotations

import hashlib
import io
import itertools
import logging
import os
import pickle
import threading
import time
from typing import Any, Callable, Optional

from mlaser.model import Document

log = logging.getLogger("mlaser.jobs")

#: items per pickled chunk of a result list (about 3-6 ms to unpickle on a loaded host)
CHUNK_ITEMS = 4000
#: parts per pickled chunk of a placed document that differs from the request's
CHUNK_PARTS = 200
#: requests a cached part encoding survives without being used
KEEP_REQUESTS = 8


class ChildSetupError(RuntimeError):
    """The child cannot reproduce the parent's compile state (the JobService compiles in-thread)."""


class ChildLost(RuntimeError):
    """The plan process died or stopped answering."""


# =============================================================================== codec
class _Entry:
    __slots__ = ("obj", "token", "data", "digest", "used")

    def __init__(self, obj: Any, token: int) -> None:
        self.obj = obj                   # strong reference: id(obj) cannot be reused meanwhile
        self.token = token
        self.data: Optional[bytes] = None
        self.digest: Optional[bytes] = None
        self.used = 0


class _BlobPickler(pickle.Pickler):
    """Pickles with the registered round-trip records replaced by tokens."""

    def __init__(self, f, blobs: dict[int, int]) -> None:
        super().__init__(f, protocol=5)
        self._tokens = blobs

    def persistent_id(self, obj):
        t = self._tokens.get(id(obj))
        return None if t is None else ("b", t)


class _BlobUnpickler(pickle.Unpickler):
    def __init__(self, f, blobs: dict[int, Any]) -> None:
        super().__init__(f)
        self._blobs = blobs

    def persistent_load(self, pid):
        kind, t = pid
        if kind != "b":
            raise pickle.UnpicklingError(f"unknown persistent id {pid!r}")
        return self._blobs[t]


def _loads(data: bytes, blobs: dict[int, Any]) -> Any:
    return _BlobUnpickler(io.BytesIO(data), blobs).load()


def _part_blobs(p) -> list:
    from .undo import PART_BLOB_KEYS, contour_blobs
    out = []
    ex = p.extra if isinstance(p.extra, dict) else {}
    for k in PART_BLOB_KEYS:
        v = ex.get(k)
        if v is not None:
            out.append(v)
    for c in p.contours:
        for _k, v in contour_blobs(c):
            if v is not None:
                out.append(v)
    return out


def part_digest(p) -> bytes:
    """sha256 of the part's inputs-hash pickle (``jobs._KeyPickler``: value based, no uid or
    selection, round-trip records by identity)."""
    from .jobs import _KeyPickler
    buf = io.BytesIO()
    _KeyPickler(buf, {id(b) for b in _part_blobs(p)}).dump(p)
    return hashlib.sha256(buf.getvalue()).digest()


class PlanCodec:
    """Per-part encodings and digests of planner snapshots (see the module docstring).

    Entries are keyed by the identity of the snapshot's part copy (held, so the id stays
    valid) and dropped after :data:`KEEP_REQUESTS` requests without use.  Thread safe."""

    def __init__(self) -> None:
        self._parts: dict[int, _Entry] = {}
        self._blobs: dict[int, _Entry] = {}
        self._tokens = itertools.count(1)
        self._gen = 0
        self._lock = threading.RLock()
        self.encoded = self.reused = 0          # statistics (tests, benchmarks)

    # ------------------------------------------------------------------ digests (inputs_hash)
    def digest(self, p) -> bytes:
        with self._lock:
            e = self._part(p)
            if e.digest is None:
                e.digest = part_digest(p)
            return e.digest

    def _part(self, p) -> _Entry:
        e = self._parts.get(id(p))
        if e is None or e.obj is not p:
            e = self._parts[id(p)] = _Entry(p, next(self._tokens))
        e.used = self._gen
        return e

    def _blob(self, b) -> _Entry:
        e = self._blobs.get(id(b))
        if e is None or e.obj is not b:
            e = self._blobs[id(b)] = _Entry(b, next(self._tokens))
            e.data = pickle.dumps(b, protocol=5)
        e.used = self._gen
        return e

    # ------------------------------------------------------------------ requests
    def encode(self, req, have: Optional[set] = None) -> dict:
        """The picklable message for ``req``.  Part and record bytes are included only for the
        tokens not in ``have`` (what the receiver already holds; None = include all); ``keep``
        lists every token the receiver should keep."""
        with self._lock:
            self._gen += 1
            doc = req.document
            parts, new_parts, new_blobs = [], {}, {}
            for p in doc.parts:
                e = self._part(p)
                if e.data is None:
                    tokens = {id(b): self._blob(b).token for b in _part_blobs(p)}
                    buf = io.BytesIO()
                    _BlobPickler(buf, tokens).dump(p)
                    e.data = buf.getvalue()
                    self.encoded += 1
                else:
                    self.reused += 1
                    for b in _part_blobs(p):
                        self._blob(b)
                parts.append(e.token)
                if have is None or e.token not in have:
                    new_parts[e.token] = e.data
            ex = doc.extra if isinstance(doc.extra, dict) else {}
            head_blobs = {id(ex["chf"]): self._blob(ex["chf"]).token} if ex.get("chf") is not None else {}
            buf = io.BytesIO()
            _BlobPickler(buf, head_blobs).dump((doc.layers, doc.version, doc.extra))
            for e in self._blobs.values():
                if e.used == self._gen and (have is None or e.token not in have):
                    new_blobs[e.token] = e.data
            self._prune()
            rest = pickle.dumps((req.mode, req.machine, tuple(req.start), req.cam_params, req.layers,
                                 bool(req.dry_run), req.profile), protocol=5)
            return {"parts": parts, "part_data": new_parts, "blob_data": new_blobs,
                    "head": buf.getvalue(), "rest": rest,
                    "keep": [e.token for e in self._parts.values()] + [e.token for e in self._blobs.values()]}

    def _prune(self) -> None:
        old = self._gen - KEEP_REQUESTS
        for d in (self._parts, self._blobs):
            for k in [k for k, e in d.items() if e.used < old]:
                del d[k]

    def decode_local(self, msg: dict) -> dict:
        """The request of ``msg`` decoded in this process: a private copy of the document that
        shares the round-trip records with the original (what ``DocumentService.lean_copy``
        does), for an in-thread compile."""
        with self._lock:
            by_token = {e.token: e for e in self._parts.values()}
            blobs = {e.token: e.obj for e in self._blobs.values()}
            datas = [msg["part_data"].get(t) or by_token[t].data for t in msg["parts"]]
        return _decode(msg, datas, blobs)


def _decode(msg: dict, part_datas: list, blobs: dict) -> dict:
    parts = [_loads(d, blobs) for d in part_datas]
    layers, version, extra = _loads(msg["head"], blobs)
    mode, machine, start, cam, layer_src, dry, profile = pickle.loads(msg["rest"])
    return {"document": Document(parts=parts, layers=layers, version=version, extra=extra),
            "mode": mode, "machine": machine, "start": start, "cam_params": cam, "layers": layer_src,
            "dry_run": dry, "profile": profile}


# =============================================================================== the compile
def _chunks(seq: list, n: int) -> list[bytes]:
    return [pickle.dumps(seq[i:i + n], protocol=5) for i in range(0, len(seq), n)] or [pickle.dumps([], protocol=5)]


def run_compile(r: dict, progress: Callable[[float], None],
                on_toolpath: Callable[[list, Document], None]) -> tuple:
    """``JobService._compile``'s computation for a decoded request ``r`` (in the child, or in a
    thread for the fallback): returns ``(items, placed, start, job)``."""
    from mlaser.nc.compiler import compile_items, frame_items, placed_document, plan_toolpath
    from mlaser.parity_ports import active_profile, use_profile

    import contextlib
    ctx = use_profile(r["profile"]) if active_profile().name != r["profile"] else contextlib.nullcontext()
    with ctx:
        if r["mode"] == "frame":
            placed = placed_document(r["document"])
            items = list(frame_items(placed.bbox(), r["machine"].frame_speed))
            start = (float(r["start"][0]), float(r["start"][1]))
            on_toolpath(items, placed)
            job = compile_items(items, r["machine"], start, dry_run=True, progress=progress)
        else:
            placed, prm, items = plan_toolpath(r["document"], r["cam_params"], r["start"], r["layers"])
            start = prm.origin
            on_toolpath(items, placed)
            job = compile_items(items, r["machine"], start, placed, r["dry_run"], progress=progress)
    return items, placed, start, job


_CHUNKED = ("manu", "markers", "cut_blocks")


def _job_meta(job) -> tuple[dict, dict]:
    from dataclasses import fields
    meta, big = {}, {}
    for f in fields(job):
        if not f.init or f.name in ("wire", "toolpath"):
            continue
        v = getattr(job, f.name)
        if f.name in _CHUNKED and isinstance(v, list):
            big[f.name] = _chunks(v, CHUNK_ITEMS)
        else:
            meta[f.name] = v
    return meta, big


def _shm_room(nbytes: int) -> bool:
    """Room for ``nbytes`` in POSIX shared memory (a full /dev/shm makes the writer SIGBUS)."""
    if os.name == "nt":
        return True
    for d in ("/dev/shm",):
        try:
            st = os.statvfs(d)
        except OSError:
            continue
        return st.f_bavail * st.f_frsize > nbytes * 1.2 + (16 << 20)
    return True


# =============================================================================== child
def _child_main(conn, cancel) -> None:
    """Entry point of the plan process (spawned): decode, compile, send the result."""
    import mlaser.model as M
    from mlaser.toolpath import items as _I  # noqa: F401 - warm the imports before "ready"
    import mlaser.nc.compiler  # noqa: F401
    # objects the child creates must not take ids the parent hands out (uids are process-unique)
    M._ids = itertools.count(1 << 52)
    from mlaser.nc import parallel
    parallel.allow_pool_in_this_child()      # the per-chain pool, as the main process would
    if hasattr(os, "setpgid"):
        try:   # own process group: the parent can end this process and its pool workers at once
            os.setpgid(0, 0)
        except OSError:
            pass
    try:
        _child_loop(conn, cancel)
    except OSError:                          # the parent went away (a broken pipe): just end
        pass
    finally:
        parallel.shutdown()                  # a spawned child exits without atexit handlers


def _child_loop(conn, cancel) -> None:
    from .jobs import Cancelled, toolpath_bounds
    from mlaser.nc import parallel
    try:
        os.nice(5)            # below the GUI and the streaming child
    except Exception:  # noqa: BLE001
        pass
    part_cache: dict[int, bytes] = {}
    blob_cache: dict[int, Any] = {}
    held: list = []                       # SharedJob blocks until the parent has copied them
    try:
        conn.send(("ready", os.getpid()))
    except OSError:                       # the parent went away while this process started
        return
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break
        kind = msg[0]
        if kind == "quit":
            break
        if kind == "ack":
            for sj in held:
                sj.release()
            held.clear()
            continue
        if kind != "plan":
            continue
        rid, payload = msg[1], msg[2]
        for sj in held:                   # an unacknowledged block: the parent moved on
            sj.release()
        held.clear()
        try:
            for t, b in payload["blob_data"].items():
                blob_cache[t] = pickle.loads(b)
            part_cache.update(payload["part_data"])
            keep = set(payload["keep"])
            for d in (part_cache, blob_cache):
                for k in [k for k in d if k not in keep]:
                    del d[k]
            try:
                r = _decode(payload, [part_cache[t] for t in payload["parts"]], blob_cache)
            except KeyError as e:
                raise ChildSetupError(f"plan process cache out of step: {e}") from e
            from mlaser.parity_ports.profile import get_profile
            try:
                get_profile(r["profile"])
            except KeyError as e:
                raise ChildSetupError(str(e)) from e
            last = [0.0]

            def progress(frac: float) -> None:
                if cancel.value >= rid:
                    raise Cancelled()
                now = time.monotonic()
                if now - last[0] >= 0.1:
                    last[0] = now
                    conn.send(("progress", rid, float(frac)))

            def on_toolpath(items: list, placed: Document) -> None:
                if cancel.value >= rid:
                    raise Cancelled()
                doc = r["document"]
                same = placed.parts is doc.parts or (len(placed.parts) == len(doc.parts) and
                                                     all(a is b for a, b in zip(placed.parts, doc.parts)))
                conn.send(("toolpath", rid, _chunks(items, CHUNK_ITEMS), {
                    "same_parts": same,
                    "parts": None if same else _chunks(placed.parts, CHUNK_PARTS),
                    "layers": pickle.dumps(placed.layers, protocol=5),
                    "extra": None if placed.extra is doc.extra else pickle.dumps(placed.extra, protocol=5),
                    "version": placed.version}))

            items, placed, start, job = run_compile(r, progress, on_toolpath)
            meta, big = _job_meta(job)
            tp_same = job.toolpath is items or job.toolpath == items
            info = {"start": (float(start[0]), float(start[1])),
                    "bounds": toolpath_bounds(job.toolpath),
                    "cut_bounds": toolpath_bounds(job.toolpath, ("CutLine", "CutArc")),
                    "toolpath": None if tp_same else _chunks(list(job.toolpath), CHUNK_ITEMS),
                    "meta": pickle.dumps(meta, protocol=5), "big": big,
                    "parallel": dict(parallel.STATS)}
            from mlaser.nc.packed import PackedWire
            wire: Any = job.wire if isinstance(job.wire, PackedWire) else None
            packed = wire is not None and job._groups is not None and job._groups[0] is wire \
                and isinstance(job._groups[2], str)
            shared = None
            if packed and _shm_room(wire.nbytes):
                try:
                    shared = job.to_shared()
                except Exception as e:  # noqa: BLE001 - no shared memory: through the pipe
                    log.debug("plan process: shared memory unavailable (%s)", e)
            if shared is not None:
                held.append(shared)
                conn.send(("job", rid, info, ("shm", shared.handle)))
            elif packed:
                conn.send(("job", rid, info, ("packed", pickle.dumps(
                    (wire.words, wire.woff, wire.cnt, wire.flag), protocol=5))))
            else:                          # a plain list wire (or packed without run starts)
                g = job._groups
                starts = g[2] if g is not None and g[0] is job.wire and g[1] == len(job.wire) else None
                conn.send(("job", rid, info, ("list", pickle.dumps((list(job.wire), starts,
                                                                     isinstance(job.wire, PackedWire)),
                                                                    protocol=5))))
        except Cancelled:
            conn.send(("cancelled", rid))
        except ChildSetupError as e:
            conn.send(("setup", rid, str(e)))
        except OSError as e:
            if isinstance(e, (BrokenPipeError, ConnectionResetError, EOFError)) or getattr(e, "errno", None) == 32:
                raise                          # the parent is gone (handled in _child_main)
            conn.send(("error", rid, pickle.dumps(RuntimeError(f"{type(e).__name__}: {e}"), protocol=5)))
        except BaseException as e:  # noqa: BLE001 - reported to the parent
            try:
                data = pickle.dumps(e, protocol=5)
                pickle.loads(data)
            except Exception:  # noqa: BLE001 - an unpicklable exception: its text
                data = pickle.dumps(RuntimeError(f"{type(e).__name__}: {e}"), protocol=5)
            conn.send(("error", rid, data))
    for sj in held:
        sj.release()


# =============================================================================== parent
import weakref  # noqa: E402

_LIVE: "weakref.WeakSet[PlanProcess]" = weakref.WeakSet()


def _close_all() -> None:
    """At exit, before multiprocessing joins its non-daemon children: end the plan processes."""
    for pp in list(_LIVE):
        try:
            pp.close()
        except Exception:  # noqa: BLE001
            pass


_atexit_done = [False]


class PlanProcess:
    """The parent's handle of the plan process (used by one JobService worker thread)."""

    STARTUP_S = 120.0          # interpreter start + imports, on a loaded host or a slow PC
    CANCEL_GRACE_S = 60.0      # a cancelled compile that does not stop is killed

    def __init__(self) -> None:
        import multiprocessing as mp
        self.ctx = mp.get_context("spawn")
        self.cancel = self.ctx.Value("q", 0, lock=False)
        self.proc: Any = None
        self.conn: Any = None
        self.pid: Optional[int] = None
        self.have: set = set()             # tokens the child holds
        self.rid = 0
        self.failures = 0
        self._ready = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Spawn the child (returns at once; :meth:`compile` waits for it to be ready)."""
        with self._lock:
            if self.proc is not None and self.proc.is_alive():
                return
            a, b = self.ctx.Pipe(duplex=True)
            # not a daemon: a daemonic process may not start the per-chain compile pool
            # (nc/parallel.py); _close_all (atexit) ends it, and it exits by itself when the
            # parent's end of the pipe closes
            p = self.ctx.Process(target=_child_main, args=(b, self.cancel), name="mlaser-plan", daemon=False)
            p.start()
            b.close()
            self.proc, self.conn, self.have, self._ready = p, a, set(), False
            _LIVE.add(self)
            if not _atexit_done[0]:
                # registered after multiprocessing's own exit handler (imported by the start
                # above), so it runs first (atexit is last-in, first-out)
                import atexit
                atexit.register(_close_all)
                _atexit_done[0] = True

    def _wait_ready(self) -> None:
        if self._ready:
            return
        t_end = time.monotonic() + self.STARTUP_S
        while time.monotonic() < t_end:
            try:
                ready = self.conn.poll(0.1)
                msg: Any = self.conn.recv() if ready else None
            except (EOFError, OSError):
                break
            if ready:
                if msg[0] == "ready":
                    self.pid = msg[1]
                    self._ready = True
                    return
            elif not self.proc.is_alive():
                break
        raise ChildLost("the plan process did not start")

    def ensure_ready(self) -> None:
        """Start the child if it is not running and wait until it can take a request."""
        if not self.alive():
            self.start()
        try:
            self._wait_ready()
        except ChildLost:
            raise self._lost("the plan process did not start") from None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def close(self) -> None:
        self.cancel.value = self.rid + 1          # a compile in progress stops at its next chain
        with self._lock:
            p, c = self.proc, self.conn
            self.proc = self.conn = None
        if c is not None:
            try:
                c.send(("quit",))
            except Exception:  # noqa: BLE001
                pass
        if p is not None:
            p.join(5.0)                    # it shuts its compile pool down first
            if p.is_alive():
                self._kill(p)
        if c is not None:
            c.close()

    def _kill(self, p) -> None:
        """End the plan process and its compile pool workers (its process group on POSIX)."""
        if p is None:
            return
        try:
            if hasattr(os, "killpg") and p.pid and os.getpgid(p.pid) == p.pid:
                import signal
                os.killpg(p.pid, signal.SIGKILL)
            elif p.is_alive():
                p.kill()
        except (OSError, ProcessLookupError):
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        p.join(2.0)

    def _lost(self, why: str) -> ChildLost:
        self.failures += 1
        try:
            self._kill(self.proc)
        except Exception:  # noqa: BLE001
            pass
        self.proc = None
        return ChildLost(why)

    # ------------------------------------------------------------------ one compile
    def compile(self, msg: dict, cancelled: Callable[[], bool], progress: Callable[[float], None],
                on_toolpath: Callable[[list, dict], None]) -> tuple[dict, Any]:
        """Run one encoded request.  Calls ``on_toolpath(items, placed_info)`` when the toolpath
        stage is done and ``progress(frac)`` while compiling (both on this thread; they may
        raise ``Cancelled``).  Returns ``(info, job)``."""
        from mlaser.nc.compiler import Job, SharedJob
        from mlaser.nc.packed import PackedWire

        from .jobs import Cancelled
        if not self.alive():
            raise self._lost("the plan process is not running")
        self._wait_ready()
        self.rid += 1
        rid = self.rid
        conn = self.conn
        try:
            conn.send(("plan", rid, msg))
        except (OSError, EOFError, BrokenPipeError) as e:
            raise self._lost(f"plan process gone: {e}") from e
        self.have = set(msg["keep"]) & (self.have | set(msg["part_data"]) | set(msg["blob_data"]))
        cancel_sent: Optional[float] = None
        while True:
            if cancel_sent is None and cancelled():
                self.cancel.value = rid
                cancel_sent = time.monotonic()
            if cancel_sent is not None and time.monotonic() - cancel_sent > self.CANCEL_GRACE_S:
                raise self._lost("the plan process did not stop after a cancel")
            try:
                ready = conn.poll(0.05)
            except (OSError, EOFError) as e:
                raise self._lost(f"plan process gone: {e}") from e
            if not ready:
                if not self.proc.is_alive():
                    raise self._lost(f"the plan process died (exit code {self.proc.exitcode})")
                continue
            try:
                ev = conn.recv()
            except (OSError, EOFError) as e:
                raise self._lost(f"plan process gone: {e}") from e
            kind = ev[0]
            if len(ev) > 1 and ev[1] != rid:
                continue                   # a late message of an earlier request
            if kind == "progress":
                if cancel_sent is None:
                    try:
                        progress(ev[2])
                    except Cancelled:
                        self.cancel.value = rid
                        cancel_sent = time.monotonic()
            elif kind == "toolpath":
                items: list = []
                for ch in ev[2]:
                    items.extend(pickle.loads(ch))
                if cancel_sent is None:
                    try:
                        on_toolpath(items, ev[3])
                    except Cancelled:
                        self.cancel.value = rid
                        cancel_sent = time.monotonic()
            elif kind == "cancelled":
                raise Cancelled()
            elif kind == "setup":
                raise ChildSetupError(ev[2])
            elif kind == "error":
                raise pickle.loads(ev[2])
            elif kind == "job":
                info, (how, data) = ev[2], ev[3]
                meta = pickle.loads(info["meta"])
                for name, chunks in info["big"].items():
                    lst: list = []
                    for ch in chunks:
                        lst.extend(pickle.loads(ch))
                    meta[name] = lst
                if how == "shm":
                    try:
                        job = Job.from_shared(SharedJob(data, meta), copy=True)
                    finally:
                        try:
                            conn.send(("ack",))
                        except Exception:  # noqa: BLE001
                            pass
                elif how == "packed":
                    job = Job(**meta)
                    job.wire = PackedWire(*pickle.loads(data))
                    job._groups = (job.wire, len(job.wire), "packed")
                else:
                    wire, starts, was_packed = pickle.loads(data)
                    job = Job(**meta)
                    if was_packed:
                        job.wire = PackedWire.from_items(wire, starts)
                        job._groups = (job.wire, len(job.wire), "packed")
                    else:
                        job.wire = wire
                        job._groups = (wire, len(wire), tuple(starts)) if starts is not None else None
                if cancel_sent is not None:
                    raise Cancelled()
                return info, job


def placed_from_info(req_doc: Document, info: dict) -> Document:
    """The placed document the child compiled from, rebuilt in the parent: the request's own
    part objects when placement changed no part (the usual case), else the child's parts."""
    if info["same_parts"]:
        parts = list(req_doc.parts)
    else:
        parts = []
        for ch in info["parts"]:
            parts.extend(pickle.loads(ch))
    extra = req_doc.extra if info["extra"] is None else pickle.loads(info["extra"])
    return Document(parts=parts, layers=pickle.loads(info["layers"]), version=info["version"], extra=extra)
