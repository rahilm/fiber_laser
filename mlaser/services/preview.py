"""Preview timeline of a PlannedJob: what the scrubber plays (preview = cut).

:class:`Timeline` is built from the wire of the :class:`mlaser.services.jobs.PlannedJob` that
Start streams, not from a separate simulation: every op-3000 step item is one servo period of
motion with its pulse increments and laser byte; every process item (gas, pierce stages, delays,
Z moves) gets the card time the ManuItem encoder attributes to it (the same accounting that gives
``Job.process_time_s``).  So the total time is ``Job.total_time_s`` and the head position at any
time is the sum of the pulses streamed up to that point.

The wire is walked in runs of identical items (``nc.packed.PackedWire``): a run of equal steps
is a straight move at constant speed, so the position inside it is interpolated exactly at
whole periods.  Qt-free; numpy.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import numpy as np

from mlaser.protocol import frames as F
from mlaser.protocol.enums import ManuClass, SysSub

#: segment kinds
TRAVEL, CUT, DWELL, DWELL_ON, PROCESS, INSTANT = range(6)
KIND_NAMES = ("travel", "cut", "dwell", "dwell_on", "process", "instant")


@dataclass(frozen=True)
class HeadState:
    t: float               # s
    x: float               # mm: the position after the steps completed before ``t``
    y: float
    laser: bool
    kind: int              # segment kind
    wire: int              # wire index of the item being executed at ``t``
    segment: int
    pierce: bool = False


@dataclass
class Timeline:
    """Segments of a planned job in time order (numpy arrays of equal length)."""

    t0: np.ndarray          # start time of each segment, s
    dt: np.ndarray          # duration, s
    x0: np.ndarray          # head position at the segment start, mm
    y0: np.ndarray
    dx: np.ndarray          # displacement over the segment, mm
    dy: np.ndarray
    kind: np.ndarray        # TRAVEL / CUT / DWELL / DWELL_ON / PROCESS / INSTANT
    laser: np.ndarray       # beam on during the segment
    pierce: np.ndarray      # the segment holds a pierce stage start
    w0: np.ndarray          # first wire index of the segment
    w1: np.ndarray          # end wire index (exclusive)
    period_s: float
    total_s: float
    markers: list[tuple[float, int, int, int]] = field(default_factory=list)  # (t, wire, part, contour)
    blocks: list[tuple[float, float, int, float]] = field(default_factory=list)  # (t0, t1, layer, gas_s)
    key: str = ""

    # ---------------------------------------------------------------- lookup
    def __len__(self) -> int:
        return int(len(self.t0))

    def segment_at(self, t: float) -> int:
        if not len(self.t0):
            return -1
        t = min(max(float(t), 0.0), self.total_s)
        i = int(np.searchsorted(self.t0, t, side="right")) - 1
        i = max(0, min(i, len(self.t0) - 1))
        # zero-length segments at the same time: the last one that started (state after them)
        return i

    def state_at(self, t: float) -> HeadState:
        """Head position and laser state at time ``t`` (clamped to the job)."""
        i = self.segment_at(t)
        if i < 0:
            return HeadState(0.0, 0.0, 0.0, False, INSTANT, 0, -1)
        t = min(max(float(t), 0.0), self.total_s)
        d = float(self.dt[i])
        u = 0.0 if d <= 0 else min(1.0, max(0.0, (t - float(self.t0[i])) / d))
        k = int(self.kind[i])
        n = int(self.w1[i] - self.w0[i])
        if k in (TRAVEL, CUT, DWELL, DWELL_ON) and n > 0:
            # one step per period: the head is at the position after the m whole periods done,
            # executing step w0 + m
            m = min(n, int(np.floor(u * n + 1e-9)))
            u = m / n
            wire = int(self.w0[i]) + min(m, n - 1)
        else:
            wire = int(self.w0[i])
        return HeadState(t, float(self.x0[i] + self.dx[i] * u), float(self.y0[i] + self.dy[i] * u),
                         bool(self.laser[i]), k, wire, i, bool(self.pierce[i]))

    def time_of_wire(self, index: int) -> float:
        """Time at which wire item ``index`` starts executing."""
        if not len(self.w0):
            return 0.0
        i = int(np.searchsorted(self.w0, index, side="right")) - 1
        i = max(0, min(i, len(self.w0) - 1))
        n = int(self.w1[i] - self.w0[i])
        if n <= 0:
            return float(self.t0[i])
        u = min(1.0, max(0.0, (index - int(self.w0[i])) / n))
        return float(self.t0[i] + self.dt[i] * u)

    def wire_at(self, t: float) -> int:
        return self.state_at(t).wire

    def contour_times(self) -> list[tuple[float, int, int]]:
        """(time, part, contour) of every contour start (jump to contour)."""
        return [(t, p, c) for t, _, p, c in self.markers]

    def contour_at(self, t: float) -> Optional[tuple[int, int]]:
        ts = [m[0] for m in self.markers]
        k = bisect.bisect_right(ts, t + 1e-12) - 1
        if k < 0:
            return None
        return self.markers[k][2], self.markers[k][3]

    def next_contour_time(self, t: float, step: int = 1) -> float:
        ts = sorted({m[0] for m in self.markers})
        if not ts:
            return t
        if step > 0:
            k = bisect.bisect_right(ts, t + 1e-6)
            return ts[min(k, len(ts) - 1)]
        k = bisect.bisect_left(ts, t - 1e-6) - 1
        return ts[max(k, 0)]

    # ---------------------------------------------------------------- drawing
    def path_until(self, t: float, max_points: int = 60000) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(xs, ys, laser) of the head path from the start to ``t``: segment end points (moves
        only), thinned to about ``max_points`` while keeping every laser on/off change.  Point k
        is reached through the move from point k-1 with the beam ``laser[k]``."""
        i = self.segment_at(t)
        if i < 0:
            return np.zeros(0), np.zeros(0), np.zeros(0, dtype=bool)
        mv = (self.kind[: i + 1] == TRAVEL) | (self.kind[: i + 1] == CUT)
        idx = np.flatnonzero(mv)
        xs = self.x0[idx] + self.dx[idx]
        ys = self.y0[idx] + self.dy[idx]
        las = self.laser[idx].astype(bool)
        if len(idx) and idx[-1] == i:
            st = self.state_at(t)
            xs[-1], ys[-1] = st.x, st.y
        if len(xs) > max_points:
            keep = np.zeros(len(xs), dtype=bool)
            keep[:: max(1, len(xs) // max_points)] = True
            keep[-1] = True
            keep[1:] |= las[1:] != las[:-1]
            keep[:-1] |= las[1:] != las[:-1]
            xs, ys, las = xs[keep], ys[keep], las[keep]
        x_start, y_start = (float(self.x0[0]), float(self.y0[0])) if len(self.x0) else (0.0, 0.0)
        return (np.concatenate([[x_start], xs]), np.concatenate([[y_start], ys]),
                np.concatenate([[False], las]))

    # ---------------------------------------------------------------- statistics
    def stats(self) -> "TimeStats":
        dt = self.dt
        k = self.kind
        las = self.laser.astype(bool)
        L = np.hypot(self.dx, self.dy)
        cut = float(dt[k == CUT].sum())
        travel = float(dt[k == TRAVEL].sum())
        dwell = float(dt[(k == DWELL) | (k == DWELL_ON)].sum())
        process = float(dt[k == PROCESS].sum())
        return TimeStats(
            total_s=self.total_s, cut_s=cut, travel_s=travel, dwell_s=dwell, process_s=process,
            laser_on_s=float(dt[las].sum()), cut_length_mm=float(L[k == CUT].sum()),
            travel_length_mm=float(L[k == TRAVEL].sum()),
            gas_on_s=float(sum(b[3] for b in self.blocks)),
            gas_by_layer={ly: float(sum(b[3] for b in self.blocks if b[2] == ly)) for ly in {b[2] for b in self.blocks}},
            laser_on_by_layer=self._laser_by_layer())

    def _laser_by_layer(self) -> dict[int, float]:
        out: dict[int, float] = {}
        if not self.blocks:
            return out
        cum = np.concatenate([[0.0], np.cumsum(self.dt * self.laser.astype(bool))])
        prev_end = 0.0
        for t0, t1, layer, _ in self.blocks:
            a = self._laser_cum(cum, prev_end)       # pierce (beam on before the block) counts
            b = self._laser_cum(cum, t1)
            out[layer] = out.get(layer, 0.0) + max(0.0, b - a)
            prev_end = t1
        return out

    def _laser_cum(self, cum: np.ndarray, t: float) -> float:
        i = self.segment_at(t)
        if i < 0:
            return 0.0
        d = float(self.dt[i])
        u = 0.0 if d <= 0 else min(1.0, max(0.0, (t - float(self.t0[i])) / d))
        return float(cum[i] + (cum[i + 1] - cum[i]) * u)


@dataclass(frozen=True)
class TimeStats:
    """Time and length totals of a planned job (s, mm)."""

    total_s: float
    cut_s: float              # laser-on moves
    travel_s: float           # laser-off moves (rapids, micro-joint gaps, settle moves)
    dwell_s: float            # zero-motion steps (rapid settle, beam-on pierce dwell)
    process_s: float          # process items (gas delays, staged pierce, Z moves)
    laser_on_s: float         # beam on (cut moves, beam-on dwells, beam-on process time)
    cut_length_mm: float
    travel_length_mm: float
    gas_on_s: float           # cut blocks plus the process time before each (gas delay, pierce)
    gas_by_layer: dict = field(default_factory=dict)
    laser_on_by_layer: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- building
def _process_times(plan: Any) -> list[tuple[int, float, Any]]:
    """(wire index, card time s, ManuItem) of every ManuItem, from a fresh encoder run with the
    job's own configuration (the accounting of ``Job.process_time_s``)."""
    from mlaser.parity_ports import active_profile, use_profile

    job = plan.job
    if not job.manu:
        return []
    m = job.machine
    pcfg = m.process_config()
    cyc = int(round(m.period_ms * 1000.0))
    if pcfg.interp_cycle_us != cyc:
        pcfg = replace(pcfg, interp_cycle_us=cyc)
    layers = dict(plan.document.layers) if plan.document is not None else {}
    name = getattr(plan, "profile", "original")
    ctx = use_profile(name) if active_profile().name != name else None
    out = []
    try:
        if ctx is not None:
            ctx.__enter__()
        enc = active_profile().process.ManuEncoder(pcfg, layers, job.dry_run)
        for idx, mi in job.manu:
            before = enc.process_ms
            enc.encode(mi)
            out.append((int(idx), (enc.process_ms - before) * 1e-3, mi))
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    return out


def _packed(wire):
    from mlaser.nc.packed import PackedWire

    if isinstance(wire, PackedWire):
        return wire
    return PackedWire.from_items([list(w) for w in wire])


def build_timeline(plan: Any) -> Timeline:
    """The :class:`Timeline` of a PlannedJob (or anything with ``job``, ``document``, ``key``)."""
    job = plan.job
    m = job.machine
    period = float(m.period_ms) * 1e-3
    ppx, ppy = float(m.pulses_per_mm_x), float(m.pulses_per_mm_y)
    pw = _packed(job.wire)
    nrun = len(pw.cnt)
    woff = np.asarray(pw.woff, dtype=np.int64)
    words = np.asarray(pw.words, dtype=np.int64)
    cnt = np.asarray(pw.cnt, dtype=np.int64)
    cum = np.asarray(pw.cum, dtype=np.int64)
    if nrun:
        head = words[woff[:-1]] & 0xFFFF
        is_step = head == int(F.OP_STEP)
        w1 = np.where(is_step, words[np.minimum(woff[:-1] + 1, len(words) - 1)], 0) & 0xFFFFFFFF
        sdx = ((w1 & 0xFF) ^ 0x80) - 0x80
        sdy = (((w1 >> 8) & 0xFF) ^ 0x80) - 0x80
        duty = (w1 >> 16) & 0xFF
    else:
        is_step = np.zeros(0, dtype=bool)
        sdx = sdy = duty = np.zeros(0, dtype=np.int64)
    # process time and laser state per run, from the ManuItems
    pdt = np.zeros(nrun)
    pierce = np.zeros(nrun, dtype=bool)
    pl_on = np.full(nrun, -1, dtype=np.int8)        # laser state set by a process item in the run
    for idx, dt_s, mi in _process_times(plan):
        if nrun == 0:
            break
        r = int(np.searchsorted(cum, min(idx, int(cum[-1]) - 1), side="right")) - 1
        r = max(0, min(r, nrun - 1))
        pdt[r] += dt_s
        if mi.cls == ManuClass.PIERCE_START:
            pierce[r] = True
        if mi.cls == ManuClass.SYS and mi.a == SysSub.LASER:
            pl_on[r] = 1 if (mi.duty and not job.dry_run) else 0
    dt = np.where(is_step, cnt * period, pdt)
    dxp = np.where(is_step, sdx * cnt, 0).astype(np.float64)
    dyp = np.where(is_step, sdy * cnt, 0).astype(np.float64)
    x_start = float(job.start_pulses[0]) / ppx
    y_start = float(job.start_pulses[1]) / ppy
    x0 = x_start + np.concatenate([[0.0], np.cumsum(dxp)[:-1]]) / ppx if nrun else np.zeros(0)
    y0 = y_start + np.concatenate([[0.0], np.cumsum(dyp)[:-1]]) / ppy if nrun else np.zeros(0)
    moving = is_step & ((sdx != 0) | (sdy != 0))
    step_on = is_step & (duty != 0)
    # the beam state of process time: the last laser item before it (staged pierce = beam on)
    laser = step_on.copy()
    state = False
    for r in np.flatnonzero(~is_step).tolist():
        if pl_on[r] >= 0:
            state = bool(pl_on[r])
        laser[r] = state and pdt[r] > 0
    kind = np.full(nrun, INSTANT, dtype=np.int8)
    kind[moving & step_on] = CUT
    kind[moving & ~step_on] = TRAVEL
    kind[is_step & ~moving & ~step_on] = DWELL
    kind[is_step & ~moving & step_on] = DWELL_ON
    kind[~is_step & (pdt > 0)] = PROCESS
    t0 = np.concatenate([[0.0], np.cumsum(dt)[:-1]]) if nrun else np.zeros(0)
    total = float(dt.sum())
    tl = Timeline(t0=t0, dt=dt, x0=x0, y0=y0, dx=dxp / ppx, dy=dyp / ppy, kind=kind, laser=laser,
                  pierce=pierce, w0=cum[:-1].copy(), w1=cum[1:].copy(), period_s=period, total_s=total,
                  key=str(getattr(plan, "key", "")))
    tl.markers = [(tl.time_of_wire(w), int(w), int(p), int(c)) for w, p, c in job.markers]
    blocks = []
    prev_end = 0.0
    proc_cum = np.concatenate([[0.0], np.cumsum(np.where(kind == PROCESS, dt, 0.0))])
    for first, end, layer in job.cut_blocks:
        ta, tb = tl.time_of_wire(first), tl.time_of_wire(end)
        ra = tl.segment_at(prev_end)
        rb = tl.segment_at(ta)
        pre = float(proc_cum[max(rb, 0)] - proc_cum[max(ra, 0)]) if rb >= ra else 0.0
        blocks.append((ta, tb, int(layer), (tb - ta) + pre))
        prev_end = tb
    tl.blocks = blocks
    return tl
