"""MainApp's per-period sample loop and pulse quantisation, bit for bit (moved from
``mlaser.nc.compiler``, Wave 1; docs/nc.md, docs/parity.md §9.R).

* :class:`Emitter` quantises sampled positions to op-3000 step items like FUN_00462630 /
  FUN_00473bd0: one step per sample including sample 0, ``ftol((x*1e4 - x_prev*1e4) * ppu +
  carry)`` per axis with the remainder carried (globals 0x9f2358[0..1]);
* :func:`emit_samples` is FUN_00462630's loop: duty / frequency from the speed curves, slow
  start / slow end, micro-joint gap samples (:func:`gap_flags`, the FUN_00469950 inclusive rule).

``Machine`` (``mlaser.nc.machine``) is read by attribute only.
"""
from __future__ import annotations

import bisect
import math
from typing import Any, Optional, Sequence

from ...protocol import frames as F
from .._registry import port

Point = tuple[float, float]
Machine = Any       # mlaser.nc.machine.Machine (pulses_per_mm_x/y, max_step, period_ms)

__port__ = {
    "source": "MainApp",
    "functions": {"FUN_00462630": "emit_samples / sample_speeds / Emitter.to",
                  "FUN_00473bd0": "Emitter.quantise", "FUN_00458b60": "Emitter.ppu",
                  "FUN_00469950": "gap_flags", "0x0046297b": "SAMPLE_POWER",
                  "DAT_0084cc18": "PULSE_SCALE"},
    "parity": ["jobmainapp", "jobnc"],
}


SAMPLE_POWER = 0xFFFF   # ManuItem +8 of every sample (FUN_00462630 0x0046297b), parity JB-02
PULSE_SCALE = 10000.0   # DAT_0084cc18: mm -> 0.1 um before the pulse conversion


@port("FUN_00462630", "FUN_00473bd0", source="MainApp", parity=("jobmainapp",))
class Emitter:
    """Quantises sampled positions to step items and tracks laser state.

    ``pos`` is the nominal position (mm) the next delta is measured from; ``cur`` the pulse
    position the card reaches (start + sum of deltas); ``carry`` the per-axis remainder of
    FUN_00473bd0 (0x9f2358[0..1]).  The original never resets that global: here it starts at
    ``carry`` (0, 0 = a fresh program start) and is handed back as ``Job.pulse_carry``.
    """

    def __init__(self, m: Machine, start: Point, dry_run: bool, carry: Sequence[float] = (0.0, 0.0)):
        self.m = m
        self.ppm = (m.pulses_per_mm_x, m.pulses_per_mm_y)
        # FUN_00458b60: (WritePulse / SpeedRatio) / 1e4 pulses per 0.1 um
        self.ppu = (m.pulses_per_mm_x / PULSE_SCALE, m.pulses_per_mm_y / PULSE_SCALE)
        self.pos = (float(start[0]), float(start[1]))
        self.cur = [round(start[0] * self.ppm[0]), round(start[1] * self.ppm[1])]
        self.start_pulses = tuple(self.cur)
        self.carry = [float(carry[0]), float(carry[1])]
        self.wire: list[list[int]] = []
        self.starts: list[int] = []    # wire index of every ManuItem expansion / step / raw item
        self.dry = dry_run
        self.laser = False
        self.power = SAMPLE_POWER      # +8 of the sample ManuItems
        self.duty = 100                # laser duty byte while on (layer duty_pct)
        self.freq = 5000               # PWM frequency field (layer frequency_hz)
        self.overspeed = 0             # periods added because a delta exceeded int8
        self.steps = 0
        self.on_len = 0.0

    def _step(self, dx: int, dy: int, laser: bool, duty: Optional[int] = None,
              freq: Optional[int] = None, power: Optional[int] = None) -> None:
        on = laser and not self.dry
        d = self.duty if duty is None else int(duty)
        f = self.freq if freq is None else max(0, min(0xFFFF, int(freq)))   # 0 -> 5000 in encode_step
        pw = self.power if power is None else int(power)
        self.starts.append(len(self.wire))
        self.wire.append(F.encode_step(dx, dy, f, max(0, min(255, d)) if on else 0, pw if on else 0))
        self.steps += 1
        self.cur[0] += dx
        self.cur[1] += dy
        if on and d > 0:
            self.on_len += math.hypot(dx / self.ppm[0], dy / self.ppm[1])

    def quantise(self, x: float, y: float) -> tuple[int, int]:
        """FUN_00473bd0 on both axes: ``d = ftol(v + carry)``, ``carry = v + carry - d`` with
        ``v = (x * 1e4 - x_prev * 1e4) * ppu`` (FUN_00462630 0x004627ee..0x0046286a)."""
        out = []
        for k, (p, q) in enumerate(((x, self.pos[0]), (y, self.pos[1]))):
            v = (p * PULSE_SCALE - q * PULSE_SCALE) * self.ppu[k]
            t = v + self.carry[k]
            d = int(t)
            self.carry[k] = t - d
            out.append(d)
        self.pos = (x, y)
        return out[0], out[1]

    def to(self, x: float, y: float, laser: bool, duty: Optional[int] = None,
           freq: Optional[int] = None, power: Optional[int] = None) -> None:
        """One servo period ending at (x, y) mm (one sample ManuItem).

        A step item always lasts one period (it has no duration field), so a delta larger than
        int8 has to be spread over extra periods; that slows the move and is counted in
        ``overspeed``.  The planner should keep speeds under ``Machine.max_step_speed``.
        ``duty``/``freq``/``power`` override the layer values for this sample (power curve,
        slow start/end, micro joints; MainApp FUN_00462630).
        """
        dx, dy = self.quantise(x, y)
        n = max(1, math.ceil(max(abs(dx), abs(dy)) / self.m.max_step))
        self.overspeed += n - 1
        px = py = 0
        for k in range(1, n + 1):
            ex, ey = round(dx * k / n), round(dy * k / n)
            self._step(ex - px, ey - py, laser, duty, freq, power)
            px, py = ex, ey

    def dwell(self, seconds: float, laser: bool) -> None:
        n = int(round(seconds * 1000.0 / self.m.period_ms))
        for _ in range(n):
            self._step(0, 0, laser)

    def raw(self, item: Sequence[int]) -> None:
        self.starts.append(len(self.wire))
        self.wire.append(list(item))

    def group(self, items: Sequence[Sequence[int]]) -> None:
        """One ManuItem's FIFO expansion (op-3000 items in it count as steps)."""
        if not items:
            return
        self.starts.append(len(self.wire))
        for w in items:
            self.wire.append(list(w))
            if (w[0] & 0xFFFF) == F.OP_STEP:
                self.steps += 1
                dx, dy, _f, duty, _p = F.decode_step(w[1:3])
                self.cur[0] += dx
                self.cur[1] += dy
                if duty:
                    self.on_len += math.hypot(dx / self.ppm[0], dy / self.ppm[1])


@port("FUN_00469950", source="MainApp", parity=("jobmainapp",))
def gap_flags(ss: Sequence[float], gaps: Sequence[tuple[float, float]]) -> list[bool]:
    """FUN_00469950 rule: a gap ``[a, b]`` covers samples ``idx(a) .. idx(b)`` INCLUSIVE, with
    ``idx(p)`` = the first sample whose arc length is >= p.  A gap starting after the last
    sample is dropped; one ending after it runs to the last sample (the odd-count fix-up at
    0x00462e33 pushes the last item index)."""
    n = len(ss)
    out = [False] * n
    for a, b in gaps:
        ia = bisect.bisect_left(ss, a)
        if ia >= n:
            continue
        ib = min(bisect.bisect_left(ss, b), n - 1)
        for i in range(ia, ib + 1):
            out[i] = True
    return out


@port("FUN_00462630", source="MainApp", parity=("jobmainapp",))
def sample_speeds(ss: Sequence[float], period_ms: float) -> list[float]:
    """Per-sample speeds MainApp hands CADModule's curves: (s_i - s_(i-1)) * 1000 / period,
    speed[0] = speed[1] (FUN_00462630 0x00462758..0x004627b1)."""
    k = 1000.0 / period_ms
    sp = [0.0] * len(ss)
    for i in range(1, len(ss)):
        sp[i] = (ss[i] - ss[i - 1]) * k
    if len(ss) > 1:
        sp[0] = sp[1]
    return sp


@port("FUN_00462630", source="MainApp", parity=("jobmainapp",))
def emit_samples(em: "Emitter", pts: Sequence[Point], ss: Sequence[float], *, total: Optional[float] = None,
                 gaps: Sequence[tuple[float, float]] = (), dcurve=None, fcurve=None, up=None, down=None,
                 micro_pct: int = 0, period_ms: Optional[float] = None) -> None:
    """FUN_00462630's sample loop: one step per sample (sample 0 included), duty/frequency from
    the speed curves (``dcurve``/``fcurve`` callables, or the layer values when None), the slow
    start / slow end overrides, micro-joint gap samples (``gap_flags``) with duty 0, or with
    MC.EnableAdvMicoLink ``duty * pct / 100`` (``micro_pct``); +8 stays 0xFFFF."""
    n = len(ss)
    if n == 0:
        return
    total = ss[-1] if total is None else total
    gap = gap_flags(ss, gaps) if gaps else [False] * n
    sp = sample_speeds(ss, em.m.period_ms if period_ms is None else period_ms)
    for i in range(n):
        s = ss[i]
        if dcurve is not None:
            duty, freq = dcurve(sp[i]), fcurve(sp[i])
        else:
            duty, freq = em.duty, em.freq
        if up and up[4] and s < up[0]:
            duty, freq = up[2], up[3]
        if down and down[4] and total - s < down[0]:
            duty, freq = down[2], down[3]
        on = em.laser
        if gap[i]:
            if em.laser and micro_pct > 0:
                duty = int(duty) * int(micro_pct) // 100      # (byte * pct) / 100, +8 kept
            else:
                on = False
        x, y = pts[i]
        em.to(x, y, on, duty=duty, freq=freq)
