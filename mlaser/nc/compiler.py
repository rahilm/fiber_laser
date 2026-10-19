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

import bisect  # noqa: F401 - kept in the module namespace (moved code used it)
import math
from dataclasses import dataclass, field, fields, replace
from typing import Any, Iterable, Optional, Sequence, Union

from ..toolpath import items as I
from ..cam.toolpath import CamParams, build_toolpath
from ..protocol import frames as F
from . import process as P
from ..model import Document, LayerParams
from ..motion.interp import (_PathGeom, _move_len, _move_point, _rapid_profile, estimate_time,
                             lead_in_index, plan_path)
from ..parity_ports import active_profile, dispatch
from .machine import Machine
from .packed import PackedWire, SharedWireHandle, WireBuilder

#: compile into packed numpy wire storage (nc/packed.py); False keeps plain lists
PACK_WIRE = True

Point = tuple[float, float]


@dataclass
class Job:
    """A compiled job: wire items plus bookkeeping for progress and checks."""
    # encoded FIFO items (header + params): a PackedWire (nc/packed.py, the same items in numpy
    # arrays) from compile_items, or a plain list
    wire: Union[list[list[int]], PackedWire] = field(default_factory=list)
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
        if isinstance(g[2], str):                      # "packed": the starts are in the runs
            return self.wire.groups()
        starts = list(g[2]) + [len(self.wire)]
        return [self.wire[a:b] for a, b in zip(starts, starts[1:]) if b > a]

    def frames(self, first_seq: int = 1) -> list[list[int]]:
        """FIFO frames for write(102, ...) starting at sequence ``first_seq`` (RO[15] + 1), with
        the original's frame size test once per ManuItem (``frames.build_fifo_frames``)."""
        g = self._groups
        if isinstance(self.wire, PackedWire) and g is not None and g[0] is self.wire and isinstance(g[2], str):
            return self.wire.frames(first_seq)
        return F.build_fifo_frames(self.groups(), first_seq)

    def iter_frames(self, first_seq: int = 1):
        """:meth:`frames` one frame at a time (no full list of frames in memory)."""
        g = self._groups
        if isinstance(self.wire, PackedWire) and g is not None and g[0] is self.wire and isinstance(g[2], str):
            return self.wire.iter_frames(first_seq)
        return iter(self.frames(first_seq))

    # ------------------------------------------------------------------ shared memory
    def to_shared(self, include_toolpath: bool = False) -> "SharedJob":
        """Hand-off to another process: the wire goes into shared memory (packed arrays), the
        rest of the Job is pickled small.  The caller owns the block: keep the returned
        handle until the reader has attached, then ``release()`` it."""
        wire = self.wire if isinstance(self.wire, PackedWire) else PackedWire.from_items(
            self.wire, self._starts_for_packing())
        handle, shm = wire.to_shared()
        meta = {f.name: getattr(self, f.name) for f in fields(self)
                if f.init and f.name not in ("wire", "toolpath")}
        if include_toolpath:
            meta["toolpath"] = self.toolpath
        return SharedJob(handle, meta, _shm=shm)

    def _starts_for_packing(self):
        g = self._groups
        if g is not None and g[0] is self.wire and g[1] == len(self.wire) and not isinstance(g[2], str):
            return g[2]
        return None

    @classmethod
    def from_shared(cls, shared: "SharedJob", copy: bool = False) -> "Job":
        """The Job of :meth:`to_shared` in this process (wire as zero-copy views of the shared
        block unless ``copy``); ``frames()`` and ``wire`` are identical to the original's."""
        job = cls(**shared.meta)
        job.wire = PackedWire.from_shared(shared.handle, copy=copy)
        job._groups = (job.wire, len(job.wire), "packed")
        return job

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
        return out  # type: ignore[return-value]

    def summary(self) -> str:
        e = self.estimate or {}
        return (f"{self.steps} steps, {len(self.wire)} items, {len(self.frames())} frames; "
                f"replay {self.duration_s:.2f} s (planner estimate {e.get('total', 0.0):.2f} s); "
                f"cut {self.laser_on_length_mm:.1f} mm, rapid {self.rapid_length_mm:.1f} mm, "
                f"{self.pierces} pierces, {self.process_items} process items "
                f"(~{self.process_time_s:.2f} s)" + (" [DRY RUN: laser disabled]" if self.dry_run else ""))


@dataclass
class SharedJob:
    """Picklable hand-off of a :class:`Job` (``Job.to_shared`` / ``Job.from_shared``): the
    shared-memory handle of the packed wire plus the other fields."""
    handle: SharedWireHandle
    meta: dict
    _shm: Any = field(default=None, repr=False, compare=False)

    def __getstate__(self):
        return {"handle": self.handle, "meta": self.meta, "_shm": None}

    def release(self) -> None:
        """Free the shared block (creator side, once every reader has attached or copied)."""
        shm, self._shm = self._shm, None
        if shm is not None:
            shm.close()
            shm.unlink()


# The per-period sample loop and pulse quantisation (MainApp FUN_00462630 / FUN_00473bd0) are
# ports in the parity fence (mlaser.parity_ports.mainapp.sampling).  compile_items takes the
# emitter class and emit_samples from the active ParityProfile (``sampling`` slot); the names
# below are the port's (``_Emitter`` is the old name of ``Emitter``).
from ..parity_ports.mainapp.sampling import (PULSE_SCALE, SAMPLE_POWER, Emitter,  # noqa: E402,F401
                                             gap_flags, sample_speeds)
from ..parity_ports.mainapp.sampling import Emitter as _Emitter  # noqa: E402,F401

emit_samples = dispatch("sampling", "emit_samples")


def gap_intervals(moves, geom) -> list[tuple[float, float]]:
    """Arc-length intervals ``[a, b]`` of the ``laser=False`` moves (micro-joint gaps) of a run,
    merged when adjacent, in the arc length of ``geom`` (FUN_00462630 param_9; the positions
    are I).  For a smoothed geometry the raw move fractions are scaled to its length."""
    raw, acc = [], 0.0  # type: ignore[var-annotated]
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
        head.curve, tail.curve = ref.sub(0.0, u), ref.sub(u, 1.0)  # type: ignore[attr-defined]
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
    prof = active_profile()
    em = prof.sampling.Emitter(m, start, dry_run, carry)
    builder = None
    # the port's emitter appends through the list interface: collect straight into packed
    # arrays; another emitter (e.g. the native kernel, which needs real lists) is packed after
    if PACK_WIRE and type(em) is Emitter and isinstance(getattr(em, "wire", None), list) and not em.wire:
        builder = WireBuilder()              # packed numpy storage, same items (nc/packed.py)
        em.wire, em.starts = builder, builder.starts()  # type: ignore[assignment]
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
    enc = prof.process.ManuEncoder(pcfg, layers, dry_run)
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
        if not all(math.isfinite(u) for u in us):
            # sub-micrometre rapid (about 0.5-1.3 um): the S-curve is degenerate (a phase of
            # ~1e-19 s) and the port's normalisation divides by a zero first sample, giving
            # NaN, which used to abort the compile.  Guard outside the bit-exact port: jump in
            # one step (the move is below one pulse).  Only jobs that crashed before change.
            us = [1.0]
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
                up, down = P.slow_start_end(lv)  # type: ignore[arg-type]
                moves = _slow_moves(moves, up, down)
            if li:
                li = lead_in_index(moves, 1, lead_len)
            plan, geom = plan_path(moves, cfg, return_geom=True, lead_index=li)
            total = geom.cum[-1]
            ss = plan.sample(period)
            pts = [geom.at(s)[0] for s in ss]
            gaps = gap_intervals(moves, geom)
            if proc:
                dcurve, fcurve = P.layer_curves(lv)  # type: ignore[arg-type]
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
            rapid_to(tuple(it.end))  # type: ignore[arg-type]
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

    if builder is not None:
        job.wire, plain_starts = builder.finish()
        job._groups = (job.wire, len(job.wire), "packed" if plain_starts is None else tuple(plain_starts))
    elif PACK_WIRE and isinstance(em.wire, list):
        try:
            job.wire = PackedWire.from_items(em.wire, em.starts)
            job._groups = (job.wire, len(job.wire), "packed")
        except (TypeError, OverflowError):
            job.wire = em.wire
            job._groups = (job.wire, len(job.wire), tuple(em.starts))
    else:
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
                    new.append(Line(fn(sg.start), fn(sg.end)))  # type: ignore[arg-type]
            c.segments = new  # type: ignore[assignment]
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

    A layer the UI only shows with defaults (``LayerParams.extra["placeholder"]``) counts as
    missing (docs/SAFETY.md: an undefined layer never cuts with the 100 % default).

    ``layers`` may be a MachineParams (``layer_params(i)``), a dict, or None (no change).
    """
    if layers is None:
        return doc
    used = {p.craft.layer for p in doc.parts}
    new = dict(doc.layers)
    for li in used:
        if li in new and not (getattr(new[li], "extra", None) or {}).get("placeholder"):
            continue                       # a UI placeholder layer takes the machine's values
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
    return active_profile().process.ManuEncoder(cfg, layers, job.dry_run).encode_all(seq.start_cut(layer))
