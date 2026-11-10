"""The import review of a large drawing in a child process (perf review finding 4).

``repair.analyze`` is CPU-bound Python (about 2 s at 2,700 parts).  In a thread of the GUI process
it held the GIL in 5 ms slices while the GUI thread needed it for thousands of small calls during
the first polish and paint of the new drawing: the window froze 3.8-4.3 s right after the file
appeared (0.15-1.2 s without the review).  Here the analysis runs in a short-lived child process
(``multiprocessing`` *spawn*, as the plan process); the GUI process only pickles the planner
snapshot's parts one at a time (the plan codec's per-part encodings, shared with the replan that
follows the open) and waits.

The report is the one ``repair.analyze`` gives in the GUI process, finding for finding: the
child runs the same function on a decoded copy of the same snapshot, and the messages, which
depend on the GUI's language and display units, are not built there.  The child records each
``i18n.tr`` / ``i18n.plural`` / ``units.mm_text`` call of ``repair`` with its arguments, and the
parent makes those calls (:func:`_render`) with its own language and units.

When no child can be used (``MLASER_PLAN_PROCESS=0``, a main script without a ``__main__`` guard,
a child that fails or dies) the review runs in the calling thread as before (``fallback``).
"""

from __future__ import annotations

import dataclasses
import logging
import pickle
import time
from typing import Any, Callable, Optional

log = logging.getLogger("mlaser.review")

#: tags of the recorded calls (tuples ``(tag, *args)``; never a text a finding could carry)
_TR, _PLURAL, _MM = "\x00i18n.tr", "\x00i18n.plural", "\x00units.mm_text"


class Cancelled(Exception):
    """The caller cancelled the review while the child ran (the child is ended)."""


def available() -> bool:
    """Whether a child process may be used (the plan process's rule, ``MLASER_PLAN_PROCESS``)."""
    from .jobs import process_compile_default
    return process_compile_default()


# --------------------------------------------------------------------------- child
class _Recorder:
    """Stands in for a module in ``repair``'s namespace: the listed functions return a record of
    the call, everything else is the real module's."""

    def __init__(self, real: Any, calls: dict[str, str]) -> None:
        self._real = real
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        tag = self._calls.get(name)
        real = getattr(self._real, name)
        if tag is None:
            return real
        if tag == _MM:
            def mm_text(value, spec="g", kind="length", units=None):
                return real(value, spec, kind, units) if units is not None else (_MM, value, spec, kind)
            return mm_text
        if tag == _PLURAL:
            return lambda base, n, **kw: (_PLURAL, base, n, kw)
        return lambda key, **kw: (_TR, key, kw)


def _child_main(conn, msg: dict, opts: Any) -> None:
    """Entry point of the review process: decode the snapshot, analyse, send the report."""
    try:
        from mlaser import i18n
        from mlaser.services import repair as R
        from mlaser.services import units

        from .planproc import decode_document
        R._i18n = _Recorder(i18n, {"tr": _TR, "plural": _PLURAL})          # type: ignore[assignment]
        R._units = _Recorder(units, {"mm_text": _MM})                      # type: ignore[assignment]
        doc = decode_document(msg)
        rep = R.analyze(doc, opts)
        data = pickle.dumps(("ok", rep), protocol=5)
    except BaseException as e:  # noqa: BLE001 - reported; the parent falls back to its thread
        data = pickle.dumps(("error", f"{type(e).__name__}: {e}"), protocol=5)
    try:
        conn.send_bytes(data)
    except OSError:                    # the parent went away
        pass


# --------------------------------------------------------------------------- parent
def _render(v: Any) -> Any:
    """A recorded call made in this process (its language and units), recursively."""
    if type(v) is tuple and v and v[0] in (_TR, _PLURAL, _MM):
        from mlaser import i18n
        from mlaser.services import units
        if v[0] == _MM:
            return units.mm_text(v[1], v[2], v[3])
        if v[0] == _PLURAL:
            return i18n.plural(v[1], v[2], **{k: _render(x) for k, x in v[3].items()})
        return i18n.tr(v[1], **{k: _render(x) for k, x in v[2].items()})
    return v


def render(rep: Any) -> Any:
    """``rep`` (a RepairReport from the child) with its findings' messages made here."""
    rep.findings = [dataclasses.replace(f, message=_render(f.message)) if type(f.message) is tuple else f
                    for f in rep.findings]
    return rep


def analyze_in_child(msg: dict, opts: Any, cancelled: Callable[[], bool] = lambda: False,
                     timeout_s: float = 600.0) -> Any:
    """``repair.analyze`` of the document in ``msg`` (``PlanCodec.encode_document``) in a child
    process; the report with its messages made here.  Raises ``Cancelled`` (the child is
    ended), ``RuntimeError`` when the child failed or died."""
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    a, b = ctx.Pipe(duplex=False)
    # a daemon: ended with the application; a lost parent ends it at its send (broken pipe)
    p = ctx.Process(target=_child_main, args=(b, msg, opts), name="mlaser-review", daemon=True)
    p.start()
    b.close()
    try:
        end = time.monotonic() + timeout_s
        while not a.poll(0.05):
            if cancelled():
                raise Cancelled()
            if not p.is_alive() and not a.poll(0):
                raise RuntimeError(f"the review process ended (exit code {p.exitcode})")
            if time.monotonic() > end:
                raise RuntimeError("the review process did not answer")
        try:
            kind, val = pickle.loads(a.recv_bytes())
        except (EOFError, OSError) as e:
            raise RuntimeError(f"the review process ended: {e}") from e
        if kind != "ok":
            raise RuntimeError(f"the review process failed: {val}")
        return render(val)
    finally:
        a.close()
        if p.is_alive():
            p.join(1.0)
        if p.is_alive():
            p.kill()
            p.join(2.0)


def analyze(snap: Any, opts: Any, codec: Any = None, cancelled: Callable[[], bool] = lambda: False,
            fallback: Optional[Callable[[], Any]] = None) -> Any:
    """The review of ``snap`` (a planner snapshot: its parts are never changed, so this may run
    off the GUI thread) in a child process when one may be used, else ``fallback()`` (default:
    ``repair.analyze`` of a private copy, here).  None when ``cancelled()`` turned true."""
    def here() -> Any:
        if fallback is not None:
            return fallback()
        from mlaser.services import repair as R

        from .undo import fast_deepcopy
        return R.analyze(fast_deepcopy(snap), opts)

    if available():
        try:
            if codec is None:
                from .planproc import PlanCodec
                codec = PlanCodec()
            msg = codec.encode_document(snap)
            if cancelled():
                return None
            return analyze_in_child(msg, opts, cancelled)
        except Cancelled:
            return None
        except Exception as exc:  # noqa: BLE001 - the thread does it, as before
            log.warning("import review: no child process (%s); reviewing in this thread", exc)
    if cancelled():
        return None
    return here()
