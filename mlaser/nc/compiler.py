"""Document -> sampled, pulse-quantised MCC100 FIFO job.

Pipeline (original data flow, docs/cam.md §10 and docs/motion.md §1):

1. ``mlaser.cam.toolpath.build_toolpath``: ordered MotionItems (rapids, leads, cuts, pierce...).
2. Velocity planning of every connected cut run (``mlaser.motion`` look-ahead planner, a port of
   CVelocityPlanning) and of every rapid (stop-to-stop S-curve).
3. Sampling one XY point per servo period (``Machine.period_ms``, 1 ms default): CInterpMrg
   (CADModule 0x10105260) hands MainApp one sample per period.
4. Pulse quantisation like MainApp FUN_00462630 / FUN_00473bd0: one step per sample
   **including sample 0** (delta 0 from the element start), each delta
   ``ftol((x*1e4 - x_prev*1e4) * ppu + carry)`` (truncation, ppu = pulses/mm / 1e4 as
   FUN_00458b60) with the remainder carried per axis (globals 0x9f2358[0..1]; shared by cut
   samples and rapids, never reset).  There is no step to the exact end of a cut run: the next
   element's deltas start from the run's nominal end, so the card position stays within one
   pulse of the nominal path (it may end one pulse short).  Deltas that do not fit the signed
   int8 of the step item are split into several items (rebuild only, ``overspeed_periods``).
5. Wire step items (op 3000, ``frames.encode_step``): duty byte from the speed curves / slow
   start-end / micro-joint gaps (FUN_00469950 inclusive gap rule), power word +8 = 0xFFFF for
   every sample (0x0046297b).
6. Frames for ``write(102, [seq, items...])`` (``frames.build_fifo_frames``: size test once per
   ManuItem expansion, about 1200 bytes).

Rapids are emitted with the laser off; Delay items become dwell steps with zero deltas (the
current laser state is held).  After each rapid the JumpAddTime settle time is emitted as a
laser-off dwell.

Process items (``Machine.process``, :mod:`mlaser.nc.process`, docs/protocol.md
"Process items"): LaserOn -> MainApp start-cut sequence (gas select, follower item, gas delay,
laser PWM, GP.LaserOnDelay); the job loop's laser sync on is pushed into the vector that the
start cut then clears (0x0045a101 / 0x0045a166 -> 0x0046546a resize(0)), so it never reaches the
card (parity JB-04); LaserOff -> laser sync off + end-cut sequence (delays,
laser off, gas off unless kept for a short hop, lift); Pierce -> staged/smooth pierce from the
layer's GP.ManuType (a layer without stages but with ``pierce['time_s']`` keeps the beam-on
dwell); Rapid -> pre-jump item when a cut follows.  The ManuItems are encoded like NCModule
FUN_10043c20 (follower ops 103..109, waits 2001, DA/PWM/outputs 9999).  Per sample the duty and
frequency follow the layer's speed curves and slow start / slow end blocks (FUN_00462630).
Follow items are hints only: the follower is driven by the start/end-cut sequences.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Sequence

from ..toolpath import items as I
from ..cam.toolpath import CamParams, build_toolpath
from ..protocol import frames as F
from . import process as P
from ..model import Document, LayerParams
from ..motion.interp import (_PathGeom, _move_len, _move_point, _rapid_profile, estimate_time,
                             lead_in_index, plan_path)
from .machine import Machine

Point = tuple[float, float]


@dataclass
class Job:
    """A compiled job: wire items plus bookkeeping for progress and checks."""
    wire: list[list[int]] = field(default_factory=list)   # encoded FIFO items (header + params)
    toolpath: list = field(default_factory=list)          # source MotionItems
    machine: Machine = field(default_factory=Machine)
    start_mm: Point = (0.0, 0.0)
    start_pulses: tuple[int, int] = (0, 0)
    end_pulses: tuple[int, int] = (0, 0)
    end_mm: Point = (0.0, 0.0)
    steps: int = 0                     # number of op-3000 items
    duration_s: float = 0.0            # steps * period (= replay time on the card)
    overspeed_periods: int = 0         # extra periods inserted for deltas beyond int8
    laser_on_length_mm: float = 0.0    # path length of steps with the laser byte set
    rapid_length_mm: float = 0.0
    pierces: int = 0
    estimate: dict = field(default_factory=dict)   # mlaser.motion.estimate_time result
    markers: list[tuple[int, int, int]] = field(default_factory=list)  # (wire index, part, contour)
    dry_run: bool = False
    process_items: int = 0             # FIFO items produced from process ManuItems
    process_time_s: float = 0.0        # estimated card time in timed waits / Z moves (not in duration_s)
    manu: list = field(default_factory=list)   # (wire index, process.ManuItem) in emission order
    cut_blocks: list = field(default_factory=list)   # (first wire index, end wire index, layer) per LaserOn..LaserOff
    pulse_carry: tuple[float, float] = (0.0, 0.0)    # FUN_00473bd0 remainder after the last motion item
    # wire index where each ManuItem's FIFO expansion starts (frame size test granularity,
    # FUN_10043c20); only valid for this Job's own ``wire`` list (not copied by dataclasses.replace)
    _groups: Optional[tuple] = field(default=None, init=False, repr=False, compare=False)

    @property
    def total_time_s(self) -> float:
        """Replay time of the steps plus the estimated process time."""
        return self.duration_s + self.process_time_s

    # ------------------------------------------------------------------ helpers
    def groups(self) -> list[list[list[int]]]:
        """``wire`` split into one group per ManuItem expansion (a step or a raw item is its own
        group).  Falls back to one group per item when the wire was replaced or extended."""
        g = self._groups
        if g is None or g[0] is not self.wire or g[1] != len(self.wire):
            return [[w] for w in self.wire]
        starts = list(g[2]) + [len(self.wire)]
        return [self.wire[a:b] for a, b in zip(starts, starts[1:]) if b > a]

    def frames(self, first_seq: int = 1) -> list[list[int]]:
        """FIFO frames for write(102, ...) starting at sequence ``first_seq`` (RO[15] + 1), with
        the original's frame size test once per ManuItem (``frames.build_fifo_frames``)."""
        return F.build_fifo_frames(self.groups(), first_seq)

    def wire_bytes(self) -> list[int]:
        """Cumulative byte count after each wire item (for progress from FIFO occupancy)."""
        out, acc = [], 0
        for w in self.wire:
            acc += 4 * len(w)
            out.append(acc)
        return out

    def step_deltas(self) -> list[tuple[int, int, int, bool, int]]:
        """Decoded (dx, dy, freq, duty, power) of every step item (laser on == duty != 0)."""
        out = []
        for w in self.wire:
            if w and (w[0] & 0xFFFF) == F.OP_STEP:
                out.append(F.decode_step(w[1:3]))
        return out

    def summary(self) -> str:
        e = self.estimate or {}
        return (f"{self.steps} steps, {len(self.wire)} items, {len(self.frames())} frames; "
                f"replay {self.duration_s:.2f} s (planner estimate {e.get('total', 0.0):.2f} s); "
                f"cut {self.laser_on_length_mm:.1f} mm, rapid {self.rapid_length_mm:.1f} mm, "
                f"{self.pierces} pierces, {self.process_items} process items "
                f"(~{self.process_time_s:.2f} s)" + (" [DRY RUN: laser disabled]" if self.dry_run else ""))


SAMPLE_POWER = 0xFFFF   # ManuItem +8 of every sample (FUN_00462630 0x0046297b), parity JB-02
PULSE_SCALE = 10000.0   # DAT_0084cc18: mm -> 0.1 um before the pulse conversion


class _Emitter:
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


def gap_intervals(moves, geom) -> list[tuple[float, float]]:
    """Arc-length intervals ``[a, b]`` of the ``laser=False`` moves (micro-joint gaps) of a run,
    merged when adjacent, in the arc length of ``geom`` (FUN_00462630 param_9; the positions
    are I).  For a smoothed geometry the raw move fractions are scaled to its length."""
    raw, acc = [], 0.0
    for mv in moves:
        L = _move_len(mv)
        if not getattr(mv, "laser", True):
            if raw and abs(raw[-1][1] - acc) <= 1e-9:
                raw[-1] = (raw[-1][0], acc + L)
            else:
                raw.append((acc, acc + L))
        acc += L
    if not raw:
        return []
    total = geom.cum[-1] if getattr(geom, "cum", None) else acc
    if isinstance(geom, _PathGeom) or acc <= 0 or abs(total - acc) <= 1e-12:
        return raw
    k = total / acc
    return [(a * k, b * k) for a, b in raw]


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


def emit_samples(em: "_Emitter", pts: Sequence[Point], ss: Sequence[float], *, total: Optional[float] = None,
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


def _layer(doc: Optional[Document], idx: int) -> LayerParams:
    if doc is not None and idx in doc.layers:
        return doc.layers[idx]
    return LayerParams(index=idx)


def _chains(items):
    """Yield ('path', [moves]) for connected cut runs and ('item', it) for everything else.

    A cut run is broken by any non-cut item except Marker/LayerChange/Follow, because laser and
    dwell changes happen at rest in the original (start/end-cut sequences, MainApp 0x00465460 /
    0x004657e0).
    """
    cur: list = []
    for it in items:
        if isinstance(it, (I.CutLine, I.CutArc)):
            if cur and math.dist(cur[-1].end, it.start) > 1e-3:
                yield ("path", cur)
                cur = []
            cur.append(it)
            continue
        if isinstance(it, (I.Marker, I.LayerChange, I.Follow)) and not cur:
            yield ("item", it)
            continue
        if cur and not isinstance(it, (I.Marker, I.LayerChange, I.Follow)):
            yield ("path", cur)
            cur = []
        yield ("item", it)
    if cur:
        yield ("path", cur)


def _lookahead(items: list) -> dict:
    """id(item) -> (next cut layer, next cut start) for every Rapid / LaserOff.

    The original scans forward for the next cut element (FUN_004657e0) and emits the pre-jump
    item before a rapid only when a cut follows (job loop 0x0045b331)."""
    out: dict = {}
    nxt = None              # (layer, start) of the next LaserOn
    pending_start = None
    for it in reversed(items):
        if isinstance(it, (I.CutLine, I.CutArc)):
            pending_start = tuple(it.start)
        elif isinstance(it, I.LaserOn):
            nxt = (int(it.layer), pending_start)
        elif isinstance(it, (I.Rapid, I.LaserOff)):
            out[id(it)] = nxt
            if isinstance(it, I.Rapid) and nxt is not None and nxt[1] is None:
                nxt = (nxt[0], tuple(it.end))
    return out


def _split_move(mv, d: float):
    """Split a CutLine/CutArc at arc length ``d`` -> (head, tail)."""
    L = _move_len(mv)
    u = min(max(d / L, 0.0), 1.0) if L > 0 else 0.0
    p = _move_point(mv, u)
    if isinstance(mv, I.CutArc):
        return (I.CutArc(mv.start, p, mv.center, mv.ccw, mv.feed, mv.layer, mv.laser),
                I.CutArc(p, mv.end, mv.center, mv.ccw, mv.feed, mv.layer, mv.laser))
    head, tail = I.CutLine(mv.start, p, mv.feed, mv.layer, mv.laser), I.CutLine(p, mv.end, mv.feed, mv.layer, mv.laser)
    ref = getattr(mv, "curve", None)
    if ref is not None:         # a piece of a spline glyph keeps its exact curve (parity JC-07)
        head.curve, tail.curve = ref.sub(0.0, u), ref.sub(u, 1.0)
    return head, tail


def _slow_moves(moves: list, up, down) -> list:
    """Apply the slow start / slow end speeds (GP.UD_UpSpeed / GP.UD_DownSpeed over
    GP.UD_UpLen / GP.UD_DownLen; MainApp hands them to the planner at 0x00459701) by splitting
    the run and lowering the feed of the first / last pieces."""
    def limit(ms, length, speed, from_end):
        seq = list(reversed(ms)) if from_end else list(ms)
        out, left = [], length
        for mv in seq:
            L = _move_len(mv)
            if left <= 1e-9:
                out.append(mv)
                continue
            if L <= left + 1e-9:
                out.append(replace(mv, feed=min(mv.feed, speed)))
                left -= L
                continue
            if from_end:
                head, tail = _split_move(mv, L - left)
                out += [replace(tail, feed=min(mv.feed, speed)), head]
            else:
                head, tail = _split_move(mv, left)
                out += [replace(head, feed=min(mv.feed, speed)), tail]
            left = 0.0
        return list(reversed(out)) if from_end else out
    if up and up[0] > 0 and up[1] > 0:
        moves = limit(moves, up[0], up[1], False)
    if down and down[0] > 0 and down[1] > 0:
        moves = limit(moves, down[0], down[1], True)
    return moves


def laser_on_sequence(seq: "P.ProcessSequencer", layer: int, laser: bool = True) -> list:
    """ManuItems of a LaserOn (cut element start) as the job loop leaves them: it pushes laser
    sync on (FUN_004590a0(1), 0x0045a101) into the vector [ebp-0x194] and then calls the start
    cut FUN_00465460 on the same vector (0x0045a166), whose first action is ``resize(0)``
    (0x0046546a).  So only the start-cut items remain: no 9999/18 1000 (parity JB-04)."""
    return seq.start_cut(layer, laser)


def compile_items(items: Iterable, machine: Optional[Machine] = None, start: Point = (0.0, 0.0),
                  doc: Optional[Document] = None, dry_run: bool = False,
                  carry: Sequence[float] = (0.0, 0.0)) -> Job:
    """Compile a MotionItem stream (mm, mm/s, s) into a :class:`Job`.

    With ``machine.process.enabled`` (default) LaserOn/LaserOff/Pierce/Rapid produce the
    original's process items (docs/protocol.md "Process items"); otherwise the phase-1
    behaviour (laser-on dwell for the pierce, optional gas output) is used.  ``carry`` is the
    initial pulse remainder (FUN_00473bd0's never-reset globals; (0, 0) = fresh program start,
    pass a previous ``Job.pulse_carry`` to chain jobs as one session would).
    """
    m = machine or Machine()
    cfg = m.motion_config()
    items = list(items)
    em = _Emitter(m, start, dry_run, carry)
    job = Job(toolpath=items, machine=m, start_mm=start, dry_run=dry_run)
    period = m.period_ms
    rapid_len = 0.0
    pierces = 0
    pcfg = m.process_config()
    cyc = int(round(period * 1000.0))
    if pcfg.interp_cycle_us != cyc:       # the servo period is AX.InterpolationCycle (us)
        pcfg = replace(pcfg, interp_cycle_us=cyc)
    proc = pcfg.enabled
    layers = dict(doc.layers) if doc is not None else {}
    seq = P.ProcessSequencer(pcfg, layers, dry_run)
    enc = P.ManuEncoder(pcfg, layers, dry_run)
    ahead = _lookahead(items) if proc else {}
    cur_layer = 1
    last_steps = [0]
    block: list = []          # open cut block [first wire index, layer]

    def manu(seq_items) -> None:
        if em.steps != last_steps[0]:
            enc.prev_cls = 0            # motion samples (class 0) were emitted in between
        for mi in seq_items:
            job.manu.append((len(em.wire), mi))
            words = enc.encode(mi)
            em.group(words)
            job.process_items += sum(1 for w in words if (w[0] & 0xFFFF) != F.OP_STEP)
        last_steps[0] = em.steps

    def rapid_to(p: Point) -> None:
        nonlocal rapid_len
        a = em.pos
        L = math.dist(a, p)
        if L <= 1e-9:
            return
        pr = _rapid_profile(I.Rapid(a, p), cfg)
        us = pr.sample(period) if pr is not None else [1.0]
        for u in us[1:] if len(us) > 1 else us:
            em.to(a[0] + (p[0] - a[0]) * u, a[1] + (p[1] - a[1]) * u, False)
        em.pos = (float(p[0]), float(p[1]))
        rapid_len += L
        em.dwell(m.jump_add_time_ms * 1e-3, False)

    for kind, obj in _chains(items):
        if kind == "path":
            moves = obj
            if math.dist(em.pos, moves[0].start) > 1e-6:
                rapid_to(moves[0].start)     # cut run not preceded by a rapid: reposition
            layer = int(getattr(moves[0], "layer", cur_layer))
            lv = P.LayerView(_layer(doc, layer)) if proc else None
            up = down = None
            # line lead-in joint (moves tagged .lead == "in" by build_toolpath): the original
            # blends it with lead_tol (type-4 marker, parity JC-08); the slow-start split below
            # may cut the lead into several moves, so the joint is found again by its length
            li = lead_in_index(moves)
            lead_len = sum(_move_len(m) for m in moves[:li]) if li else None
            if proc:
                up, down = P.slow_start_end(lv)
                moves = _slow_moves(moves, up, down)
            if li:
                li = lead_in_index(moves, 1, lead_len)
            plan, geom = plan_path(moves, cfg, return_geom=True, lead_index=li)
            total = geom.cum[-1]
            ss = plan.sample(period)
            pts = [geom.at(s)[0] for s in ss]
            gaps = gap_intervals(moves, geom)
            if proc:
                dcurve, fcurve = P.layer_curves(lv)
                micro = pcfg.adv_micro_link_pct if pcfg.adv_micro_link else 0
                emit_samples(em, pts, ss, total=total, gaps=gaps, dcurve=dcurve, fcurve=fcurve,
                             up=up, down=down, micro_pct=micro)
            else:
                emit_samples(em, pts, ss, total=total, gaps=gaps)
            # no step to the exact end (the last sample is the last tick t <= T, docs/motion.md
            # §4): the next element is measured from the run's nominal end, as the job loop
            # starts the next jump at the element end point (FUN_0045f5b0 local_a0 = start * 1e4)
            end = moves[-1].end
            em.pos = (float(end[0]), float(end[1]))
            continue
        it = obj
        if isinstance(it, I.Marker):
            if it.kind == "contour":
                job.markers.append((len(em.wire), it.part, it.contour))
        elif isinstance(it, I.Rapid):
            if proc:
                nx = ahead.get(id(it))
                if nx is not None:
                    manu(seq.pre_jump(nx[0]))
            rapid_to(tuple(it.end))
        elif isinstance(it, I.LayerChange):
            cur_layer = int(it.layer)
            lp = _layer(doc, it.layer)
            em.duty = max(1, min(100, int(round(lp.duty_pct))))
            em.freq = max(1, min(0xFFFF, int(round(lp.frequency_hz))))
            if m.emit_layer_commands and not dry_run:
                da = max(0, min(0xFFFF, int(round(lp.power_pct * m.power_scale))))
                em.raw(F.sys_item(F.SYS_DA, 0, max(50, min(10000, da))))
                em.raw(F.sys_item(F.SYS_PWM, int(lp.frequency_hz), int(round(lp.duty_pct))))
        elif isinstance(it, I.LaserOn):
            cur_layer = int(it.layer)
            if proc:
                manu(laser_on_sequence(seq, cur_layer))
            block = [len(em.wire), cur_layer]     # the cut block starts after the start cut
            em.laser = True
        elif isinstance(it, I.LaserOff):
            if proc:
                nx = ahead.get(id(it))
                hop = math.dist(em.pos, nx[1]) if nx is not None and nx[1] is not None else math.inf
                manu(seq.laser_sync(False) + seq.end_cut(cur_layer, nx[0] if nx else None, hop))
            em.laser = False
            if block:
                job.cut_blocks.append((block[0], len(em.wire), block[1]))
                block = []
        elif isinstance(it, I.Pierce):
            pierces += 1
            staged = seq.pierce(int(it.layer)) if proc else []
            if staged:
                manu(staged)
                continue
            t = float(it.time_s or 0.0)
            if t <= 0 and it.stages:
                t = sum(float(s.get("time_s", 0.0) or 0.0) for s in it.stages if isinstance(s, dict))
            if proc:
                if t > 0 and not dry_run:      # timed pierce without stages: gas, then beam-on dwell
                    lv = P.LayerView(_layer(doc, it.layer))
                    manu(seq.gas_select(lv.i("CutGasType"), lv.f("CutAirPressure", 5.0))
                         + seq.gas_delay(lv.i("CutGasType")))
            elif m.gas_output is not None and not dry_run:
                em.raw(_io_item(m.gas_output, True))
            em.dwell(t, True)          # beam on while piercing (rebuild choice, I)
        elif isinstance(it, I.Delay):
            em.dwell(float(it.seconds), em.laser)
        elif isinstance(it, I.IO):
            em.raw(_io_item(int(it.port), bool(it.value)))
        elif isinstance(it, I.Follow):
            if not proc and not it.enable and m.gas_output is not None and not dry_run:
                em.raw(_io_item(m.gas_output, False))
        elif isinstance(it, (list, tuple)) and it and isinstance(it[0], int):
            em.raw(it)
    if block:
        job.cut_blocks.append((block[0], len(em.wire), block[1]))

    job.wire = em.wire
    job._groups = (job.wire, len(job.wire), tuple(em.starts))
    job.steps = em.steps
    job.duration_s = em.steps * m.period_ms * 1e-3
    job.process_time_s = enc.process_ms * 1e-3
    job.overspeed_periods = em.overspeed
    job.laser_on_length_mm = em.on_len
    job.rapid_length_mm = rapid_len
    job.pierces = pierces
    job.start_pulses = em.start_pulses
    job.end_pulses = (em.cur[0], em.cur[1])
    job.end_mm = em.pos
    job.pulse_carry = (em.carry[0], em.carry[1])
    try:
        job.estimate = estimate_time(items, cfg)
    except Exception:   # noqa: BLE001 - the estimate is informative only
        job.estimate = {}
    return job


def _io_item(port: int, on: bool) -> list[int]:
    if 1 <= port <= 10:
        return F.sys_item(F.SYS_OUTPUTS, 1 << (port - 1), int(on) << (port - 1))
    if 11 <= port <= 26:
        return F.sys_item(F.SYS_OUTPUTS_EXT, 1 << (port - 11), int(on) << (port - 11))
    raise ValueError(f"output {port} out of range 1..26")


def compile_job(doc: Document, cam_params=None, machine: Optional[Machine] = None,
                start: Optional[Point] = None, dry_run: bool = False, layers=None) -> Job:
    """Toolpath -> planning -> 1 ms sampling -> pulses -> wire items -> :class:`Job`.

    ``cam_params`` is a :class:`mlaser.cam.toolpath.CamParams` (or dict / None).  ``start`` is the
    current machine position in mm (defaults to ``cam_params.origin``); it is also used as the
    sort origin.  ``dry_run`` clears the laser byte of every step and skips laser/gas items.
    ``layers`` (a ``mlaser.formats.params.MachineParams`` or ``dict[int, LayerParams]``) supplies
    the speed/power of layers the document does not define (LayerPara.xml, docs/formats.md).
    """
    doc = placed_document(with_layers(doc, layers))
    if cam_params is None:
        prm = CamParams()
    elif isinstance(cam_params, dict):
        prm = CamParams(**{k: v for k, v in cam_params.items() if k in CamParams.__dataclass_fields__})
    else:
        prm = CamParams(**cam_params.__dict__)
    if start is not None:
        prm.origin = (float(start[0]), float(start[1]))
    items = build_toolpath(doc, prm)
    return compile_items(items, machine, prm.origin, doc, dry_run)


APPLY_MIN_ANGLE = 0.0015     # CADModule FUN_100bd2bc: |angle| > 0.0015 rad -> rotation applied


def _xform_doc(doc: Document, fn) -> Document:
    import copy
    from ..model import Arc, Line
    d = copy.deepcopy(doc)
    for part in d.parts:
        for c in part.contours:
            new = []
            for sg in c.segments:
                if isinstance(sg, Arc):
                    new.append(Arc(fn(sg.start), fn(sg.end), fn(sg.center), sg.ccw))
                else:
                    new.append(Line(fn(sg.start), fn(sg.end)))
            c.segments = new
        if part.craft.cool_point is not None:
            part.craft.cool_point = fn(part.craft.cool_point)
    return d


def placed_document(doc: Document) -> Document:
    """The drawing as it is cut on the machine (copy; ``doc`` is not changed).

    1. Job placement (coordinate systems, MainApp FUN_0045cc20, O): when
       ``doc.extra["job_offset"] = (dx, dy)`` is set (by :mod:`mlaser.controller.coords`), the
       drawing is translated so its dock point lands on the chosen origin.
    2. Whole-drawing rotation (the .chf trailer ``rotated / rotate_angle / rotate_center /
       rotate_offset``, CADModule FUN_100bf4a0, O; set by the edge seek, MainApp CAD +0x1b8 with
       the anchor cfg+0x4980 as centre): rotate by the angle about the centre, then add the
       offset, when ``rotated`` and |angle| > 0.0015.
    """
    out = doc
    off = doc.extra.get("job_offset")
    if off and (abs(off[0]) > 1e-12 or abs(off[1]) > 1e-12):
        ox, oy = float(off[0]), float(off[1])
        out = _xform_doc(out, lambda p: (p[0] + ox, p[1] + oy))
    ang = float(doc.extra.get("rotate_angle", 0.0) or 0.0)
    if doc.extra.get("rotated") and abs(ang) > APPLY_MIN_ANGLE:
        cx, cy = doc.extra.get("rotate_center", (0.0, 0.0))
        tx, ty = doc.extra.get("rotate_offset", (0.0, 0.0))
        c, s_ = math.cos(ang), math.sin(ang)

        def rot(p):
            x, y = p[0] - cx, p[1] - cy
            return (cx + c * x - s_ * y + tx, cy + s_ * x + c * y + ty)
        out = _xform_doc(out, rot)
    return out


def frame_items(bbox: tuple[float, float, float, float], speed: float) -> list:
    """'Frame' (mp58): rapid to the bbox corner, then trace the rectangle with the laser off."""
    x0, y0, x1, y1 = bbox
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    out: list = [I.Rapid((x0, y0), (x0, y0))]
    out += [I.CutLine(a, b, speed, 1, False) for a, b in zip(pts, pts[1:]) if math.dist(a, b) > 1e-9]
    return out


def with_layers(doc: Document, layers) -> Document:
    """Shallow copy of ``doc`` whose missing layer parameters come from ``layers``.

    ``layers`` may be a MachineParams (``layer_params(i)``), a dict, or None (no change).
    """
    if layers is None:
        return doc
    used = {p.craft.layer for p in doc.parts}
    new = dict(doc.layers)
    for li in used:
        if li in new:
            continue
        try:
            new[li] = layers.layer_params(li) if hasattr(layers, "layer_params") else layers[li]
        except Exception:   # noqa: BLE001 - unknown layer index: keep defaults
            continue
    return Document(parts=doc.parts, layers=new, version=doc.version, extra=doc.extra)


def resume_items(job: Job, wire_index: int, doc: Optional[Document] = None) -> list[list[int]]:
    """Process items that restore the cutting state before replaying ``job.wire[wire_index:]``.

    Breakpoint resume (owner: ``nc.resume``) restarts the FIFO in the middle of a cut block, where
    the original state (gas open, follower on, laser PWM) was set up by items before the
    breakpoint.  This returns a fresh start-cut sequence (gas, follow, gas delay, laser on) for
    the layer of the cut block (``Job.cut_blocks``, LaserOn .. LaserOff) containing
    ``wire_index``, otherwise ``[]``.  Like the job loop it sends no laser sync on (parity
    JB-04).  INFERRED: the original's resume path was not traced.
    """
    if not job.machine.process_config().enabled:
        return []
    layer = None
    for a, b, ly in job.cut_blocks:
        if a <= wire_index < b:
            layer = ly
            break
    if layer is None:
        return []
    layers = dict(doc.layers) if doc is not None else {}
    cfg = job.machine.process_config()
    seq = P.ProcessSequencer(cfg, layers, job.dry_run)
    return P.ManuEncoder(cfg, layers, job.dry_run).encode_all(seq.start_cut(layer))
