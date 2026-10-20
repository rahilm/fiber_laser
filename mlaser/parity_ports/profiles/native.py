"""The emitter loop on the C kernel ``mlaser._native._emitter`` (docs/dev/NATIVE_KERNEL.md).

Since the C kernel became the default, this module does two things:

* **Default selection.**  The default profile ``"original"`` builds its emitter through
  :class:`DefaultEmitter` and runs :func:`emit_samples` from here.  At first use
  :func:`select` decides once per process: the kernel is used when the extension imports AND
  passes :func:`selfcheck` (a small built-in known-answer vector run through the kernel and
  through the Python port, compared bit for bit); otherwise the Python port runs, with a logged
  warning.  ``MLASER_NATIVE`` (or :func:`configure`) overrides it: ``0`` / ``off`` = always the
  Python port, ``1`` / ``on`` = the kernel is required (compiling a job raises
  :class:`NativeKernelMissing` / :class:`NativeSelfCheckFailed` if it cannot be used), unset /
  ``auto`` = as above.  :func:`kernel_status` reports what is active (About dialog,
  ``mlaser --selftest``, ``--bundle-check``, logs).
* **Explicit profiles.**  ``"native-sampling"`` always uses the kernel (raises when it is not
  built, no fallback); ``"original-python"`` (mlaser.parity_ports.profile) always the port.

:class:`NativeEmitter` (a subclass of the port's
:class:`~mlaser.parity_ports.mainapp.sampling.Emitter`) runs ``to`` and ``dwell`` in C, and
:func:`emit_samples` runs FUN_00462630's whole sample loop (speeds, SpeedCurve duty and
frequency, slow start / end, micro-joint gaps, quantisation with the pulse carry, the split of
large deltas, ``frames.encode_step``) in one call per run.  ``gap_flags`` and
``sample_speeds`` stay the port's.  The output is bit-identical to the port: same wire items,
markers, steps, carry and ``laser_on_length_mm`` (``tools/native_parity.py`` checks every
golden job, ``tests/test_native_kernel.py`` the kernel on random streams).

Inside the kernel, any input it does not handle exactly (non-float coordinates, values beyond
2^52, a step over int8, an exception in a callback) makes the call undo its output and the
port's Python code run instead, so errors are the port's errors.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from array import array
from dataclasses import asdict, dataclass
from typing import Any, Optional, Sequence

from ... import _native as _PKG
from ...protocol import frames as F
from ..mainapp import sampling as S
from ..profile import ORIGINAL, Implementation, register_profile

_K = _PKG._emitter   # None when the extension is not built

__port__ = {
    "source": "MainApp",
    "functions": {"FUN_00462630": "emit_samples / NativeEmitter.to (C kernel)",
                  "FUN_00473bd0": "NativeEmitter quantise (C kernel)"},
    "parity": ["jobmainapp", "jobnc"],
    "profile": "native-sampling",
}

NAME = "native-sampling"
_STEP_HEADER = F.item_header(F.OP_STEP, 2)
_NAN_TABLE = array("d", [math.nan]) * (255 * 255)
#: types whose instances the kernel evaluates in C (SpeedCurve, and the self-check's copy of it)
_SPEEDCURVES = {("mlaser.nc.process", "SpeedCurve"), (__name__, "_KatCurve")}

#: calls handled by the kernel / handed to the port, per entry point ("emit", "to", "dwell"):
#: what tools/native_parity.py reports as kernel coverage
STATS: dict[str, int] = {"emit": 0, "emit_port": 0, "emit_curves": 0, "emit_gaps": 0,
                         "emit_updown": 0, "to": 0, "to_port": 0, "to_path": 0, "dwell": 0,
                         "dwell_port": 0}


class NativeKernelMissing(ImportError):
    """The kernel was required (profile ``native-sampling``, ``MLASER_NATIVE=1``) but the
    extension ``mlaser._native._emitter`` is not importable."""


class NativeSelfCheckFailed(RuntimeError):
    """``MLASER_NATIVE=1`` but the kernel's output differs from the Python port's on the
    built-in self-check vector (a miscompiled or wrongly built extension)."""


def available() -> bool:
    """Is the extension importable?  (Not whether it passed the self-check: :func:`select`.)"""
    return _K is not None


def require() -> Any:
    if _K is None:
        raise NativeKernelMissing(
            f"the C emitter kernel 'mlaser._native._emitter' is not built "
            f"({_PKG.IMPORT_ERROR or 'not importable'}). It is compiled when mlaser is installed "
            "with a C compiler present (`uv sync`, `pip install .`; rebuild in place with "
            "`python tools/build_native.py`); or set MLASER_NATIVE=0 to use the Python port.")
    return _K


def _small_int(v: Any) -> Optional[int]:
    """``v`` as the kernel's int64 when it is a plain int of modest size, else None."""
    if type(v) is int and -(1 << 62) <= v <= (1 << 62):
        return v
    return None


def _curve_spec(c: Any) -> Any:
    """SpeedCurve -> the kernel's tuple spec; any other callable is called back as is."""
    t = type(c)
    if (t.__module__, t.__qualname__) in _SPEEDCURVES and "__call__" in t.__dict__:
        segs = tuple(tuple(s) for s in c.segs)
        if (type(c.threshold) is float and all(len(s) == 4 and all(type(e) is float for e in s)
                                                for s in segs)):
            return ("speedcurve", bool(c.enabled), c.threshold, c.base, segs)
    return c


class NativeEmitter(S.Emitter):
    """:class:`~mlaser.parity_ports.mainapp.sampling.Emitter` with ``to`` / ``dwell`` in C.

    The state stays in the port's attributes (``pos``, ``carry``, ``cur``, ``steps``,
    ``overspeed``, ``on_len``, ``wire``, ``starts``), so ``raw`` / ``group`` and the compiler
    work unchanged.  ``math.hypot`` values are memoised per (dx, dy) in ``_hcache``, computed by
    CPython's own ``math.hypot`` on first use.
    """

    def __init__(self, m: S.Machine, start: S.Point, dry_run: bool, carry: Sequence[float] = (0.0, 0.0)):
        self._k = require()
        super().__init__(m, start, dry_run, carry)
        self._hcache = array("d", _NAN_TABLE)
        self._hppm = self.ppm

    # ---------------------------------------------------------------- kernel plumbing
    def _hyp(self, dx: int, dy: int) -> float:
        return math.hypot(dx / self.ppm[0], dy / self.ppm[1])

    def _state(self) -> tuple:
        if self.ppm is not self._hppm:          # ppm replaced: the memo is stale
            self._hcache = array("d", _NAN_TABLE)
            self._hppm = self.ppm
        return (self.pos[0], self.pos[1], self.carry[0], self.carry[1], self.cur[0], self.cur[1],
                self.steps, self.overspeed, self.on_len)

    def _commit(self, st: tuple) -> None:
        self.pos = (st[0], st[1])
        self.carry[0] = st[2]
        self.carry[1] = st[3]
        self.cur[0] = st[4]
        self.cur[1] = st[5]
        self.steps = st[6]
        self.overspeed = st[7]
        self.on_len = st[8]

    #: compile_items may hand this emitter a packed builder backed by the kernel's C sink
    #: (mlaser.nc.packed.WireBuilder(native=True)): the steps are written into it directly
    PACKED_SINK = True

    def _common(self) -> Optional[tuple]:
        if type(self.carry) is not list or type(self.cur) is not list or _small_int(self.power) is None:
            return None
        wire: Any = self.wire
        starts: Any = self.starts
        if type(wire) is not list or type(starts) is not list:
            # a packed builder whose steps go to its C sink: ``starts`` must be its own recorder
            sink = getattr(wire, "native_sink", None)
            if sink is None or getattr(starts, "builder", None) is not wire:
                return None
            wire, starts = sink, None
        return (wire, starts, _STEP_HEADER, self.power, bool(self.dry), self._hcache, self._hyp)

    def _emit(self, pts: Sequence[S.Point], ss: Any, kspd: float, total: float, gap: Any, dc: Any,
              fc: Any, up: Any, down: Any, micro_pos: bool, micro_int: int, laser: bool, duty0: int,
              freq0: int, fclamp: bool) -> bool:
        ms = self.m.max_step
        common = self._common()
        if common is None or type(ms) not in (int, float) or isinstance(ms, bool):
            return False
        try:
            st = self._k.emit(self._state(), *common, self.ppu[0], self.ppu[1], float(ms), pts, ss,
                              kspd, total, gap, dc, fc, up, down, micro_pos, micro_int, laser,
                              duty0, freq0, fclamp)
        except (TypeError, OverflowError, ValueError):
            return False
        if st is None:
            return False
        self._commit(st)
        return True

    # ---------------------------------------------------------------- Emitter API
    def to(self, x: float, y: float, laser: bool, duty: Optional[int] = None,
           freq: Optional[int] = None, power: Optional[int] = None) -> None:
        if power is None:
            try:
                d = _small_int(self.duty if duty is None else int(duty))
                f = _small_int(self.freq if freq is None else int(freq))
            except (TypeError, ValueError, OverflowError):
                d = f = None
            if d is not None and f is not None and self._emit(
                    ((x, y),), None, 0.0, 0.0, None, None, None, None, None, False, 0, bool(laser),
                    d, f, freq is not None):
                STATS["to"] += 1
                return
        STATS["to_port"] += 1
        super().to(x, y, laser, duty, freq, power)

    def to_path(self, pts: Sequence[S.Point], laser: bool) -> None:
        """``for x, y in pts: self.to(x, y, laser)`` in one kernel call (a rapid's samples)."""
        if pts:
            d, f = _small_int(self.duty), _small_int(self.freq)
            if d is not None and f is not None and self._emit(
                    pts, None, 0.0, 0.0, None, None, None, None, None, False, 0, bool(laser), d, f, False):
                STATS["to_path"] += 1
                return
        for x, y in pts:
            self.to(x, y, laser)

    def dwell(self, seconds: float, laser: bool) -> None:
        n = int(round(seconds * 1000.0 / self.m.period_ms))
        if n <= 0:
            return
        common = self._common()
        d, f = _small_int(self.duty), _small_int(self.freq)
        if common is not None and d is not None and f is not None:
            st = self._k.steps(self._state(), *common, n, bool(laser), d, f)
            if st is not None:
                self._commit(st)
                STATS["dwell"] += 1
                return
        STATS["dwell_port"] += 1
        for _ in range(n):
            self._step(0, 0, laser)


def emit_samples(em: S.Emitter, pts: Sequence[S.Point], ss: Sequence[float], *, total: Optional[float] = None,
                 gaps: Sequence[tuple[float, float]] = (), dcurve=None, fcurve=None, up=None, down=None,
                 micro_pct: int = 0, period_ms: Optional[float] = None) -> None:
    """:func:`mlaser.parity_ports.mainapp.sampling.emit_samples` in one kernel call (same
    signature, same result); on a plain :class:`Emitter` or an input outside the kernel's exact
    domain it runs the port.  ``pts`` may also be a flat ``array('d')`` x0, y0, x1, y1, ...
    (a native geometry's samples)."""
    n = len(ss)
    if n == 0:
        return
    flat = type(pts) is array
    if isinstance(em, NativeEmitter) and len(pts) == (2 * n if flat else n):
        try:
            tot = ss[-1] if total is None else total
            kspd = 1000.0 / (em.m.period_ms if period_ms is None else period_ms)
            gap = S.gap_flags(ss, gaps) if gaps else None
            ok = type(tot) is float and type(kspd) is float
            upk = dnk = None
            if up and up[4]:
                upk = (up[0], int(up[2]), int(up[3]))
            if down and down[4]:
                dnk = (down[0], int(down[2]), int(down[3]))
            mi = int(micro_pct)
            pos = bool(micro_pct > 0)
            d0, f0 = _small_int(em.duty), _small_int(em.freq)
            if dcurve is not None:
                dc, fc = _curve_spec(dcurve), _curve_spec(fcurve)
            else:
                dc = fc = None
                ok = ok and d0 is not None and f0 is not None
        except (TypeError, ValueError, OverflowError, IndexError, ZeroDivisionError):
            ok = False
        if ok and em._emit(pts, ss, kspd, tot, gap, dc, fc, upk, dnk, pos, mi, bool(em.laser),
                           d0 or 0, f0 or 0, True):
            STATS["emit"] += 1
            STATS["emit_curves"] += type(dc) is tuple
            STATS["emit_gaps"] += gap is not None
            STATS["emit_updown"] += upk is not None or dnk is not None
            return
    STATS["emit_port"] += 1
    if flat:
        pts = tuple_points(pts)  # type: ignore[arg-type]
    S.emit_samples(em, pts, ss, total=total, gaps=gaps, dcurve=dcurve, fcurve=fcurve, up=up,
                   down=down, micro_pct=micro_pct, period_ms=period_ms)


def tuple_points(flat: Sequence[float]) -> list[S.Point]:
    """A flat x0, y0, x1, y1, ... sequence as the list of (x, y) tuples the port takes."""
    return list(zip(flat[0::2], flat[1::2]))


# the same original function as the port's emit_samples (the kernel is a port of it in C); a
# plain attribute, not an @port registration: the registry lists the Python reference
emit_samples.__port__ = S.emit_samples.__port__  # type: ignore[attr-defined]


SAMPLING = Implementation(
    "sampling",
    Emitter=NativeEmitter,
    emit_samples=emit_samples,
    gap_flags=ORIGINAL.sampling.target("gap_flags"),
    sample_speeds=ORIGINAL.sampling.target("sample_speeds"),
)

SMOOTHER = ORIGINAL.smoother.with_(
    smooth_path_points="mlaser.parity_ports.profiles.native_smooth:native_smooth_path_points")

NATIVE = register_profile(ORIGINAL.derive(
    NAME, "original, with the emitter loop and the smoother always on the C kernel mlaser._native "
          "(no fallback)", sampling=SAMPLING, smoother=SMOOTHER))


# =========================================================================== default selection
ENV = "MLASER_NATIVE"
_MODES = {"": "auto", "auto": "auto", "1": "on", "on": "on", "true": "on", "yes": "on",
          "require": "on", "native": "on", "0": "off", "off": "off", "false": "off", "no": "off",
          "python": "off"}
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KernelStatus:
    """Which emitter the default profile runs, and why (:func:`kernel_status`)."""
    active: str              # "native" | "python" | "error" (required but unusable)
    mode: str                # "auto" | "on" | "off"
    source: str              # where the mode came from: "default", "MLASER_NATIVE=..", "configure()"
    reason: str              # one line for people
    selfcheck: str           # "passed" | "failed: ..." | "not run"
    module: str = ""         # file of the extension, "" when not importable
    kernel_version: int = _PKG.KERNEL_VERSION

    @property
    def native(self) -> bool:
        return self.active == "native"

    def text(self) -> str:
        what = {"native": "native C kernel", "python": "Python port",
                "error": "UNAVAILABLE"}.get(self.active, self.active)
        return f"Emitter kernel: {what} (mode {self.mode}, {self.source}; {self.reason})"

    def to_dict(self) -> dict:
        return asdict(self)


_lock = threading.Lock()
_status: Optional[KernelStatus] = None
_mode_override: Optional[str] = None


def _mode() -> tuple[str, str]:
    if _mode_override is not None:
        return _mode_override, "configure()"
    raw = os.environ.get(ENV)
    if raw is None:
        return "auto", "default"
    mode = _MODES.get(raw.strip().lower())
    if mode is None:
        _log.warning("%s=%r is not one of 0/off, 1/on, auto: using auto", ENV, raw)
        return "auto", f"{ENV}={raw} (invalid)"
    return mode, f"{ENV}={raw}"


def configure(mode: Optional[str] = None) -> KernelStatus:
    """Choose the default profile's emitter: ``"auto"``, ``"on"`` (required), ``"off"`` (Python
    port), or None to follow ``MLASER_NATIVE`` again.  Takes effect for the next compile (jobs
    already running keep their emitter); returns the new status (``on`` with an unusable
    kernel gives ``active == "error"``; the next compile then raises)."""
    global _mode_override, _status
    if mode is not None and mode not in ("auto", "on", "off"):
        mode = _MODES.get(str(mode).strip().lower())
        if mode is None:
            raise ValueError("native mode must be 'auto', 'on' or 'off'")
    with _lock:
        _mode_override = mode
        _status = None
    return kernel_status()


def kernel_status() -> KernelStatus:
    """The selection (made now if it has not been made yet); never raises."""
    global _status
    st = _status
    if st is not None:
        return st
    with _lock:
        if _status is None:
            _status = _decide()
            log = _log.warning if _status.active != "native" and _status.mode != "off" else _log.info
            log("%s", _status.text())
        return _status


def select() -> KernelStatus:
    """:func:`kernel_status`, raising when the kernel is required (``on``) but unusable."""
    st = kernel_status()
    if st.active == "error":
        if st.module and st.selfcheck.startswith("failed"):
            raise NativeSelfCheckFailed(f"{ENV}: {st.reason}")
        raise NativeKernelMissing(f"{ENV}: {st.reason}")
    return st


def native_selected() -> bool:
    """Does the default profile run the C kernel?  (Raises like :func:`select`.)"""
    st = _status
    if st is None or st.active == "error":
        st = select()
    return st.active == "native"


def _decide() -> KernelStatus:
    mode, source = _mode()
    if mode == "off":
        return KernelStatus("python", mode, source, "Python port forced", "not run",
                            getattr(_K, "__file__", "") or "")
    if _K is None:
        reason = f"C kernel not built ({_PKG.IMPORT_ERROR or 'not importable'})"
        return KernelStatus("error" if mode == "on" else "python", mode, source, reason, "not run")
    path = getattr(_K, "__file__", "") or ""
    try:
        err = selfcheck()
    except Exception as e:  # noqa: BLE001 - any failure means "do not use the kernel"
        err = f"{type(e).__name__}: {e}"
    if err:
        reason = f"C kernel self-check failed, {err}"
        if mode == "auto":
            reason += "; falling back to the Python port"
        return KernelStatus("error" if mode == "on" else "python", mode, source, reason,
                            f"failed: {err}", path)
    return KernelStatus("native", mode, source, "self-check passed", "passed", path)


class DefaultEmitter(S.Emitter):
    """The emitter of the default profile: a :class:`NativeEmitter` when :func:`select` chose
    the kernel, else the port's :class:`~mlaser.parity_ports.mainapp.sampling.Emitter` (the
    object built is an instance of one of those two, never of this class)."""

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        klass = NativeEmitter if native_selected() else S.Emitter
        return klass(*args, **kwargs)


# --------------------------------------------------------------------------- self-check
class _KatMachine:
    """The machine attributes the emitter reads (mlaser.nc.machine.Machine)."""

    def __init__(self, ppm_x: float, ppm_y: float, max_step: Any, period_ms: float):
        self.pulses_per_mm_x = ppm_x
        self.pulses_per_mm_y = ppm_y
        self.max_step = max_step
        self.period_ms = period_ms


class _KatCurve:
    """mlaser.nc.process.SpeedCurve.__call__, transcribed (parity_ports cannot import nc):
    the kernel evaluates it in C (``_SPEEDCURVES``), the port by calling it."""

    def __init__(self, enabled: bool, threshold: float, base: int,
                 segs: Sequence[tuple[float, float, float, float]]):
        self.enabled, self.threshold, self.base, self.segs = enabled, threshold, base, list(segs)

    def __call__(self, v: float) -> int:
        if not self.enabled or v > self.threshold:
            return self.base
        seg = self.segs[0]
        for s in self.segs:
            if s[0] < v <= s[1]:
                seg = s
                break
        return int((v - seg[0]) * seg[3] + (seg[2] + 0.5))


def _lcg(seed: int):
    """Deterministic floats in [0, 1) from a 48-bit LCG (the same on every platform)."""
    x = seed
    while True:
        x = (x * 0x5DEECE66D + 11) & ((1 << 48) - 1)
        yield x / float(1 << 48)


def _kat_run(em: S.Emitter, emit: Any) -> None:
    """The self-check vector: every kind of call the compiler makes, with the arithmetic that a
    wrong build changes (FMA contraction in quantise / the curve, x87 rounding, round half to
    even in the split, the frequency clamp, floor division of negative duties)."""
    r = _lcg(0x6D6C61736572)
    x0, y0 = em.pos
    em.laser = True
    em.duty, em.freq = 77, 20000
    # a cut run: an arc-like path at varying speed, cumulative arc lengths
    pts, ss, s = [], [], 0.0
    px, py = x0, y0
    for i in range(160):
        a = i * 0.0731
        nx = x0 + 3.7 * math.cos(a) + 0.013 * next(r)
        ny = y0 + 2.9 * math.sin(a) - 0.011 * next(r)
        s += math.hypot(nx - px, ny - py)
        pts.append((nx, ny))
        ss.append(s)
        px, py = nx, ny
    emit(em, pts, ss)
    # the same with speed curves, slow start / end, micro-joint gaps at 40 %
    dc = _KatCurve(True, 1e9, 200, [(0.0, 40.0, 30.0, 1.37), (40.0, 900.0, 85.0, -0.0731),
                                     (900.0, 1e9, 20.0, 0.0)])
    fc = _KatCurve(True, 1e9, 9000, [(0.0, 500.0, 1200.0, 13.3), (500.0, 1e9, 70000.0, 1.0)])
    pts2 = [(x + 0.5, y - 0.25) for x, y in pts]
    emit(em, pts2, ss, total=s, gaps=[(1.0, 1.4), (s - 0.6, s + 1.0)], dcurve=dc, fcurve=fc,
         up=(0.7, 0, 55, 3000, True), down=(0.9, 0, -3, 70000, True), micro_pct=40)
    em.duty = -9                      # negative duty under a micro-joint: Python floor division
    emit(em, pts[:40], ss[:40], gaps=[(0.0, 0.5)], micro_pct=33)
    em.duty = 77
    # rapids: long moves split over several periods (round half to even), explicit clamped
    # frequency / duty values
    em.laser = False
    for i in range(40):
        em.to(x0 + 9.0 * next(r) - 4.5, y0 + 9.0 * next(r) - 4.5, False)
    # a rapid's samples in one call (compile_items' rapid_to on the native emitter)
    rp = [(x0 + 3.3 * next(r) - 1.6, y0 - 2.2 * next(r) + 1.0) for _ in range(30)]
    if isinstance(em, NativeEmitter):
        em.to_path(rp, False)
    else:
        for x, y in rp:
            em.to(x, y, False)
    for d, f in ((300, 70000), (-5, -1), (128, 0), (255, 65535)):
        em.to(em.pos[0] + 0.0371, em.pos[1] - 0.0517, True, duty=d, freq=f)
    em.dwell(0.0105, True)
    em.dwell(0.003, False)


class _SinkWire:
    """The interface of ``mlaser.nc.packed.WireBuilder(native=True)`` that the emitter and the
    kernel use (parity_ports cannot import nc): a wire whose steps go into a C ``Sink``."""

    def __init__(self, sink: Any) -> None:
        self.native_sink = sink

    def __len__(self) -> int:
        return len(self.native_sink)

    def append(self, item: Any) -> None:
        self.native_sink.append(item)


class _SinkStarts:
    def __init__(self, wire: _SinkWire) -> None:
        self.builder = wire

    def append(self, index: int) -> None:
        self.builder.native_sink.mark_start(index)


def _pack_reference(wire: Sequence, starts: Sequence[int]) -> tuple:
    """The run-length packing of nc/packed.py (WireBuilder), in plain Python."""
    st = set(starts)
    words: list = []
    woff, cnt, flag = [0], [], []  # type: ignore[var-annotated]
    last, lastflag = None, False
    for i, it in enumerate(wire):
        f = i in st
        if cnt and f == lastflag and last is not None and list(it) == last:
            cnt[-1] += 1
            continue
        words += list(it)
        woff.append(len(words))
        cnt.append(1)
        flag.append(f)
        last, lastflag = list(it), f
    wide = any(not 0 <= w <= 0xFFFFFFFF for w in words)
    return words, wide, woff, cnt, flag


def _sink_contents(sink: Any) -> tuple:
    wb, wide, woff, cnt, flag = sink.finish()
    words = memoryview(wb).cast("q" if wide else "I").tolist()
    return (words, bool(wide), memoryview(woff).cast("q").tolist(), memoryview(cnt).cast("q").tolist(),
            [bool(b) for b in bytes(flag)])


def _kat_fingerprint(em: S.Emitter, wire: bool = True) -> tuple:
    return (([list(w) for w in em.wire], list(em.starts)) if wire else ()) + (
            em.pos[0].hex(), em.pos[1].hex(),
            float(em.carry[0]).hex(), float(em.carry[1]).hex(), tuple(em.cur), em.steps,
            em.overspeed, float(em.on_len).hex())


#: (ppm x, ppm y, max_step, period_ms, start, carry, dry run)
_KAT_MACHINES = (
    (2500.0 / 3.0, 1234.5678, 127, 1.0, (12.3456, -7.89), (0.3, -0.7), False),
    (1000.0, 1000.0, 40.0, 0.5, (-0.1, 0.2), (-0.4999999999999999, 0.9999999999999999), False),
    (199.99, 3001.7, 127, 1.0, (0.0, 0.0), (0.0, -0.0), True),
)


def selfcheck() -> str:
    """Run the known-answer vector through the kernel and through the Python port and compare
    every output bit for bit; "" when they agree, else what differs.  Also fails when the
    kernel handed a call of the vector back to the port (it must run all of them itself)."""
    if _K is None:
        return "kernel not importable"
    saved = dict(STATS)
    try:
        for ppx, ppy, ms, period, start, carry, dry in _KAT_MACHINES:
            m = _KatMachine(ppx, ppy, ms, period)
            ref = S.Emitter(m, start, dry, carry)
            _kat_run(ref, S.emit_samples)
            for k in STATS:
                STATS[k] = 0
            nat = NativeEmitter(m, start, dry, carry)
            _kat_run(nat, emit_samples)
            handed = {k: v for k, v in STATS.items() if k.endswith("_port") and v}
            if handed:
                return f"kernel declined calls of the vector {handed}"
            a, b = _kat_fingerprint(ref), _kat_fingerprint(nat)
            if a != b:
                names = ("wire", "starts", "pos x", "pos y", "carry x", "carry y", "cur", "steps",
                         "overspeed", "laser-on length")
                diff = [n for n, u, v in zip(names, a, b) if u != v]
                return f"output differs from the Python port in {', '.join(diff)} (ppm {ppx:g})"
            if not a[0]:
                return "the vector produced no steps"
            # the same vector with the steps written into the packed C sink
            sink_cls = getattr(_K, "Sink", None)
            if sink_cls is None:
                return "kernel has no packed sink"
            for k in STATS:
                STATS[k] = 0
            pk = NativeEmitter(m, start, dry, carry)
            w = _SinkWire(sink_cls())
            pk.wire, pk.starts = w, _SinkStarts(w)  # type: ignore[assignment]
            _kat_run(pk, emit_samples)
            handed = {k: v for k, v in STATS.items() if k.endswith("_port") and v}
            if handed:
                return f"kernel declined calls of the vector with the packed sink {handed}"
            if _sink_contents(w.native_sink) != _pack_reference(ref.wire, ref.starts):
                return f"packed sink differs from the Python port's packed wire (ppm {ppx:g})"
            if _kat_fingerprint(pk, wire=False) != a[2:]:
                return f"emitter state with the packed sink differs from the Python port (ppm {ppx:g})"
    finally:
        STATS.clear()
        STATS.update(saved)
    from . import native_smooth
    err = native_smooth.selfcheck()
    return f"smoother: {err}" if err else ""
