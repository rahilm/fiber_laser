"""``native-sampling``: the emitter loop on the optional C kernel ``mlaser_native`` (docs/dev/NATIVE_KERNEL.md).

The profile replaces the ``sampling`` slot only: :class:`NativeEmitter` (a subclass of the
port's :class:`~mlaser.parity_ports.mainapp.sampling.Emitter`) runs ``to`` and ``dwell`` in C,
and :func:`emit_samples` runs FUN_00462630's whole sample loop (speeds, SpeedCurve duty and
frequency, slow start / end, micro-joint gaps, quantisation with the pulse carry, the split of
large deltas, ``frames.encode_step``) in one call per run.  ``gap_flags`` and
``sample_speeds`` stay the port's.  The output is bit-identical to the port: same wire items,
markers, steps, carry and ``laser_on_length_mm`` (``tools/native_parity.py`` checks every
golden job, ``tests/test_native_kernel.py`` the kernel on random streams).

Selecting it::

    import mlaser.parity_ports.profiles.native as native
    native.available()                 # False when mlaser_native is not installed
    with use_profile("native-sampling"):
        job = compile_job(doc)

Without ``mlaser_native`` the module still imports (the profile is registered), but building
a :class:`NativeEmitter`, i.e. compiling a job under this profile, raises
:class:`NativeKernelMissing` (an ImportError) with the build instructions; there is no silent
fallback to Python.  Inside the kernel, any input it does not handle exactly (non-float
coordinates, values beyond 2^52, a step over int8, an exception in a callback) makes the call
undo its output and the port's Python code run instead, so errors are the port's errors.
"""
from __future__ import annotations

import math
from array import array
from typing import Any, Optional, Sequence

from ...protocol import frames as F
from ..mainapp import sampling as S
from ..profile import ORIGINAL, Implementation, register_profile

try:  # the optional distribution native/ (mlaser-native)
    from mlaser_native import _emitter as _K
except ImportError:  # pragma: no cover - depends on the environment
    _K = None

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
_SPEEDCURVE = ("mlaser.nc.process", "SpeedCurve")

#: calls handled by the kernel / handed to the port, per entry point ("emit", "to", "dwell"):
#: what tools/native_parity.py reports as kernel coverage
STATS: dict[str, int] = {"emit": 0, "emit_port": 0, "emit_curves": 0, "emit_gaps": 0,
                         "emit_updown": 0, "to": 0, "to_port": 0, "dwell": 0, "dwell_port": 0}


class NativeKernelMissing(ImportError):
    """The ``native-sampling`` profile was used but ``mlaser_native`` is not importable."""


def available() -> bool:
    return _K is not None


def require() -> Any:
    if _K is None:
        raise NativeKernelMissing(
            f"ParityProfile {NAME!r} needs the optional C kernel 'mlaser_native', which is not "
            "installed. Build it with `python tools/build_native.py` (needs a C compiler), or "
            "install the mlaser-native wheel, or use the default profile 'original'.")
    return _K


def _small_int(v: Any) -> Optional[int]:
    """``v`` as the kernel's int64 when it is a plain int of modest size, else None."""
    if type(v) is int and -(1 << 62) <= v <= (1 << 62):
        return v
    return None


def _curve_spec(c: Any) -> Any:
    """SpeedCurve -> the kernel's tuple spec; any other callable is called back as is."""
    t = type(c)
    if (t.__module__, t.__qualname__) == _SPEEDCURVE and "__call__" in t.__dict__:
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

    def _common(self) -> Optional[tuple]:
        if (type(self.carry) is not list or type(self.cur) is not list or type(self.wire) is not list
                or type(self.starts) is not list or _small_int(self.power) is None):
            return None
        return (self.wire, self.starts, _STEP_HEADER, self.power, bool(self.dry), self._hcache, self._hyp)

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
    domain it runs the port."""
    n = len(ss)
    if n == 0:
        return
    if isinstance(em, NativeEmitter) and len(pts) == n:
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
    S.emit_samples(em, pts, ss, total=total, gaps=gaps, dcurve=dcurve, fcurve=fcurve, up=up,
                   down=down, micro_pct=micro_pct, period_ms=period_ms)


SAMPLING = Implementation(
    "sampling",
    Emitter=NativeEmitter,
    emit_samples=emit_samples,
    gap_flags=ORIGINAL.sampling.target("gap_flags"),
    sample_speeds=ORIGINAL.sampling.target("sample_speeds"),
)

NATIVE = register_profile(ORIGINAL.derive(
    NAME, "original, with the emitter loop on the optional C kernel mlaser_native (bit-identical)",
    sampling=SAMPLING))
