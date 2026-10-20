"""True-shape raster nester (bottom-left fill with rotations, mirror and part-in-part).

This is a cross-platform replacement for the original's engines (SmartNest.dll /
AutoNest.dll, see docs/re/nest.md). It keeps their inputs and outputs (part list with quantity,
priority, angle list, mirror; sheets/remnants; spacing, edge margin; result = per-sheet
placements part id / x / y / angle / mirror) but uses its own algorithm:

1. Every allowed orientation of a part is rasterised **conservatively** on a grid of cell
   size ``res``: a cell is "body" if the part may touch it (cell centre inside
   ``geom.buffer(res*sqrt(2)/2)``), "fit" if it lies within ``spacing`` of the part. Holes
   stay free, so small parts can be nested inside large ones (part-in-part).
2. The sheet grid marks cells not fully inside the usable region (sheet minus margin, minus
   remnant defects) as blocked; the static feasibility of every orientation against the
   region is computed once per sheet with an FFT cross-correlation.
3. Placement loop: for the next part, the occupancy of the already placed parts is
   cross-correlated with the "fit" mask of every orientation (one FFT per orientation and
   step); the feasible offset nearest the gravity corner wins (bottom-left fill).
   Disjoint conservative masks guarantee the real outlines keep ``spacing``.
4. Optional exact compaction slides the placed outline towards the corner with shapely
   distance checks, recovering most of the raster quantisation loss, then the exact outline
   is rasterised into the occupancy grid.

Part order: priority (high first), then area (large first). Required copies (``quantity``)
are placed first over the sheets in list order; leftover space on the last used sheet is
filled up to ``max_quantity``.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import Polygon

from .model import (ENGINE_RASTER, ENGINE_SPYRROW, GRAVITY_AUTO, GRAVITY_BOTTOM, GRAVITY_LEFT, GRAVITY_RIGHT,
                    GRAVITY_TOP, NestJob, NestPart, NestResult, NestSheet, Placement, SheetLayout)
from .shapes import fit_geometry, oriented, parts_geometry, sheet_geometry

ProgressFn = Callable[[float, str], Optional[bool]]   # (fraction, message) -> False to cancel


def _good_size(n: int) -> int:
    """Smallest 2^a 3^b 5^c >= n (fast FFT sizes)."""
    best = 1 << max(0, (n - 1).bit_length())
    a = 1
    while a < 2 * n:
        b = a
        while b < 2 * n:
            c = b
            while c < n:
                c *= 5
            best = min(best, c)
            b *= 3
        a *= 2
    return best


def _filled(geom):
    """Outline without holes (used when part-in-part is off)."""
    polys = [g for g in getattr(geom, "geoms", [geom]) if isinstance(g, Polygon)]
    return shapely.union_all([Polygon(p.exterior) for p in polys]) if polys else geom


@dataclass
class _Orient:
    part_id: int
    angle: float
    mirror: bool
    geom: object                 # oriented exact outline in local coords
    ox: float = 0.0              # local coordinate of mask cell (0, 0) corner
    oy: float = 0.0
    fit: np.ndarray = None       # bool mask (rows = y, cols = x)
    body_w: int = 0              # body extent in cells (for scoring)
    body_h: int = 0
    bx0: int = 0                 # body bbox inside mask (cells)
    by0: int = 0
    fft_conj: Optional[np.ndarray] = None
    area: float = 0.0


@dataclass
class _Sheet:
    sh: NestSheet
    full: object
    usable: object
    res: float
    x0: float
    y0: float
    H: int
    W: int
    pad: int
    blocked: np.ndarray          # static: region cells not usable
    occ: np.ndarray              # placed-part occupancy (float32 0/1)
    static_ok: dict = field(default_factory=dict)   # orient key -> feasible mask
    placed: list = field(default_factory=list)      # exact geoms (for compaction)
    bounds: list = field(default_factory=list)
    layout: SheetLayout = None
    dead: set = field(default_factory=set)          # part ids that no longer fit here
    area: float = 0.0


class Nester:
    """Run a :class:`NestJob`. ``progress(fraction, message)`` may return False to cancel."""

    def __init__(self, job: NestJob, progress: Optional[ProgressFn] = None):
        self.job = job
        self.p = job.params
        self.progress = progress
        self.spacing = self.p.effective_spacing()
        self._cache: OrderedDict = OrderedDict()
        self._exhausted: set = set()
        self.cancelled = False

    # ------------------------------------------------------------------ setup
    def _resolution(self) -> float:
        if self.p.resolution:
            return float(self.p.resolution)
        areas = []
        for sh in self.job.sheets:
            full, _ = sheet_geometry(sh, 0.0)
            areas.append(full.area)
        a = max(areas) if areas else 1.0
        return max(0.25, math.sqrt(a / 160000.0))

    def _part_geoms(self):
        self.geoms: dict[int, object] = {}
        for np_ in self.job.parts:
            g = parts_geometry(np_.parts, tol=max(0.01, min(self.p.accuracy, 1.0) * 0.1))
            if g.is_empty:
                continue
            c = g.centroid
            g = affinity.translate(g, -c.x, -c.y)      # rotate about the centroid
            np_.extra["_origin"] = (c.x, c.y)
            self.geoms[np_.id] = fit_geometry(g, np_.fit)

    def _orientations(self, np_: NestPart) -> list[tuple[float, bool]]:
        angles = self.p.part_angles(np_)
        mirrors = [False, True] if (np_.mirror or self.p.allow_mirror) else [False]
        return [(float(a) % 360.0, m) for m in mirrors for a in angles]

    def _orient(self, np_: NestPart, angle: float, mirror: bool, res: float) -> _Orient:
        key = (np_.id, angle, mirror)
        o = self._cache.get(key)
        if o is not None:
            return o
        g = oriented(self.geoms[np_.id], angle, mirror)
        h = res * math.sqrt(2) / 2 * 1.001
        reach = self.spacing + h
        bx0, by0, bx1, by1 = g.bounds
        o = _Orient(np_.id, angle, mirror, g, area=g.area)
        pad = int(math.ceil(reach / res)) + 1
        o.ox = math.floor(bx0 / res) * res - pad * res
        o.oy = math.floor(by0 / res) * res - pad * res
        w = int(math.ceil((bx1 - o.ox) / res)) + pad + 1
        hh = int(math.ceil((by1 - o.oy) / res)) + pad + 1
        xs = o.ox + (np.arange(w) + 0.5) * res
        ys = o.oy + (np.arange(hh) + 0.5) * res
        X, Y = np.meshgrid(xs, ys)
        fit_g = g.buffer(reach, quad_segs=4) if reach > 0 else g
        o.fit = shapely.contains_xy(fit_g, X, Y)
        body = shapely.contains_xy(g.buffer(h, quad_segs=4), X, Y)
        rows = np.nonzero(body.any(axis=1))[0]
        cols = np.nonzero(body.any(axis=0))[0]
        o.by0, o.bx0 = int(rows[0]), int(cols[0])
        o.body_h, o.body_w = int(rows[-1] - rows[0] + 1), int(cols[-1] - cols[0] + 1)
        o.body = body
        self._cache[key] = o
        return o

    def _new_sheet(self, sh: NestSheet, res: float) -> _Sheet:
        margin = self.p.margin if sh.margin is None else sh.margin
        full, usable = sheet_geometry(sh, margin)
        x0, y0, x1, y1 = full.bounds
        pad = int(math.ceil((self.spacing + res) / res)) + 2
        # the mask of the largest part must fit too (it may overhang into the pad)
        H = _good_size(int(math.ceil((y1 - y0) / res)) + 2 * pad)
        W = _good_size(int(math.ceil((x1 - x0) / res)) + 2 * pad)
        gx0, gy0 = x0 - pad * res, y0 - pad * res
        xs = gx0 + (np.arange(W) + 0.5) * res
        ys = gy0 + (np.arange(H) + 0.5) * res
        X, Y = np.meshgrid(xs, ys)
        h = res * math.sqrt(2) / 2 * 1.001
        inner = usable.buffer(-h, quad_segs=4) if not usable.is_empty else usable
        blocked = ~shapely.contains_xy(inner, X, Y) if not inner.is_empty else np.ones((H, W), bool)
        s = _Sheet(sh, full, usable, res, gx0, gy0, H, W, pad, blocked,
                   np.zeros((H, W), np.float32), area=full.area)
        s.layout = SheetLayout(sh.id)
        s.prep_usable = usable
        shapely.prepare(usable)
        s.blocked_fft = np.fft.rfft2(blocked.astype(np.float32))
        return s

    # ------------------------------------------------------------------ feasibility
    def _mask_fft(self, o: _Orient, s: _Sheet, which: str) -> np.ndarray:
        attr = f"_fft_{which}_{s.H}x{s.W}"
        f = getattr(o, attr, None)
        if f is None:
            m = np.zeros((s.H, s.W), np.float32)
            src = o.fit if which == "fit" else o.body
            hh, ww = src.shape
            m[:hh, :ww] = src
            f = np.conj(np.fft.rfft2(m))
            setattr(o, attr, f)
        return f

    def _static_ok(self, o: _Orient, s: _Sheet) -> Optional[np.ndarray]:
        key = (o.part_id, o.angle, o.mirror)
        if key in s.static_ok:
            return s.static_ok[key]
        hh, ww = o.fit.shape
        if hh > s.H or ww > s.W:
            s.static_ok[key] = None
            return None
        c = np.fft.irfft2(s.blocked_fft * self._mask_fft(o, s, "body"), s=(s.H, s.W))
        ok = c < 0.5
        ok[s.H - hh + 1:, :] = False
        ok[:, s.W - ww + 1:] = False
        if not ok.any():
            ok = None
        s.static_ok[key] = ok
        return ok

    def _score(self, i: int, j: int, o: _Orient) -> tuple:
        g = self.p.gravity
        top = i + o.by0 + o.body_h
        right = j + o.bx0 + o.body_w
        if g == GRAVITY_BOTTOM:
            return (top, j + o.bx0)
        if g == GRAVITY_TOP:
            return (-(i + o.by0), j + o.bx0)
        if g == GRAVITY_RIGHT:
            return (-(j + o.bx0), i + o.by0)
        return (right, i + o.by0)

    def _best_offset(self, ok: np.ndarray, o: _Orient):
        g = self.p.gravity
        if g in (GRAVITY_LEFT, GRAVITY_RIGHT):
            cols = np.nonzero(ok.any(axis=0))[0]
            if not len(cols):
                return None
            j = int(cols[0] if g == GRAVITY_LEFT else cols[-1])
            i = int(np.nonzero(ok[:, j])[0][0])
        else:
            rows = np.nonzero(ok.any(axis=1))[0]
            if not len(rows):
                return None
            i = int(rows[0] if g == GRAVITY_BOTTOM else rows[-1])
            j = int(np.nonzero(ok[i, :])[0][0])
        return i, j

    def _try_place(self, s: _Sheet, np_: NestPart) -> Optional[Placement]:
        if np_.id in s.dead:
            return None
        occ_fft = np.fft.rfft2(s.occ) if s.placed else None
        best = None
        for angle, mirror in self._orientations(np_):
            o = self._orient(np_, angle, mirror, s.res)
            ok = self._static_ok(o, s)
            if ok is None:
                continue
            if occ_fft is not None:
                c = np.fft.irfft2(occ_fft * self._mask_fft(o, s, "fit"), s=(s.H, s.W))
                ok = ok & (c < 0.5)
            pos = self._best_offset(ok, o)
            if pos is None:
                continue
            sc = self._score(pos[0], pos[1], o)
            if best is None or sc < best[0]:
                best = (sc, o, pos)
        if best is None:
            s.dead.add(np_.id)
            return None
        _, o, (i, j) = best
        tx = s.x0 + j * s.res - o.ox
        ty = s.y0 + i * s.res - o.oy
        g = affinity.translate(o.geom, tx, ty)
        if self.p.compact:
            g, tx, ty = self._compact(s, g, tx, ty)
        self._commit(s, g)
        ox, oy = np_.extra.get("_origin", (0.0, 0.0))
        # placement maps original part coords: p -> R M (p - origin) + t
        pl = _placement_from(np_.id, o.angle, o.mirror, (ox, oy), (tx, ty))
        return pl

    # ------------------------------------------------------------------ exact steps
    def _feasible(self, s: _Sheet, g) -> bool:
        if not s.prep_usable.contains(g):
            return False
        if not s.placed:
            return True
        b = g.bounds
        sp = self.spacing
        arr = s.bounds_arr
        m = ((arr[:, 0] <= b[2] + sp) & (arr[:, 2] >= b[0] - sp) &
             (arr[:, 1] <= b[3] + sp) & (arr[:, 3] >= b[1] - sp))
        idx = np.nonzero(m)[0]
        if not len(idx):
            return True
        cand = s.placed_arr[idx]
        d = shapely.distance(cand, g)
        if sp > 1e-9:
            return bool((d >= sp - 1e-6).all())
        touch = cand[d <= 1e-9]
        if len(touch):
            return bool((shapely.area(shapely.intersection(touch, g)) <= 1e-6).all())
        return True

    def _compact(self, s: _Sheet, g, tx: float, ty: float):
        gr = self.p.gravity
        primary = {GRAVITY_LEFT: (-1, 0), GRAVITY_RIGHT: (1, 0),
                   GRAVITY_BOTTOM: (0, -1), GRAVITY_TOP: (0, 1)}[gr]
        secondary = (0, -1) if primary[1] == 0 else (-1, 0)
        # common line: parts must end up touching (edges coincident within 1e-3)
        min_step = 1e-3 if self.p.common_line else s.res / 32
        for _ in range(2):
            moved = False
            for d in (secondary, primary):
                step = 1.5 * s.res
                while step > min_step:
                    cand = affinity.translate(g, d[0] * step, d[1] * step)
                    if self._feasible(s, cand):
                        g, tx, ty = cand, tx + d[0] * step, ty + d[1] * step
                        moved = True
                    else:
                        step /= 2
            if not moved:
                break
        return g, tx, ty

    def _commit(self, s: _Sheet, g) -> None:
        res = s.res
        h = res * math.sqrt(2) / 2 * 1.001
        body = g if self.p.part_in_part else _filled(g)
        bx0, by0, bx1, by1 = g.bounds
        j0 = max(0, int(math.floor((bx0 - h - s.x0) / res)))
        i0 = max(0, int(math.floor((by0 - h - s.y0) / res)))
        j1 = min(s.W, int(math.ceil((bx1 + h - s.x0) / res)) + 1)
        i1 = min(s.H, int(math.ceil((by1 + h - s.y0) / res)) + 1)
        xs = s.x0 + (np.arange(j0, j1) + 0.5) * res
        ys = s.y0 + (np.arange(i0, i1) + 0.5) * res
        X, Y = np.meshgrid(xs, ys)
        m = shapely.contains_xy(body.buffer(h, quad_segs=4), X, Y)
        s.occ[i0:i1, j0:j1] = np.maximum(s.occ[i0:i1, j0:j1], m.astype(np.float32))
        s.placed.append(g)
        s.bounds.append(g.bounds)
        s.placed_arr = np.array(s.placed, dtype=object)
        s.bounds_arr = np.array(s.bounds, dtype=float)
        s.used_area = getattr(s, "used_area", 0.0) + g.area

    # ------------------------------------------------------------------ main loop
    def _tick(self, frac: float, msg: str) -> None:
        if self.progress is not None and self.progress(frac, msg) is False:
            self.cancelled = True

    def run(self) -> NestResult:
        t0 = time.monotonic()
        job = self.job
        res = self._resolution()
        self._part_geoms()
        parts = [p for p in job.parts if p.id in self.geoms]
        key = {"area": lambda p: -self.geoms[p.id].area,
               "bbox": lambda p: -shapely.area(shapely.box(*self.geoms[p.id].bounds)),
               "length": lambda p: -max(self.geoms[p.id].bounds[2] - self.geoms[p.id].bounds[0],
                                        self.geoms[p.id].bounds[3] - self.geoms[p.id].bounds[1])}
        parts.sort(key=lambda p: (-p.priority, key.get(self.p.sort_by, key["area"])(p)))
        need = {p.id: max(0, p.quantity) for p in parts}
        extra = {p.id: max(0, (p.max_quantity if p.max_quantity is not None else p.quantity) - p.quantity)
                 for p in parts}
        total = max(1, sum(need.values()))
        result = NestResult(resolution=res, gravity=self.p.gravity)
        placed = {p.id: 0 for p in parts}
        done = 0
        for sh, first in ((sh, k == 0) for sh in job.sheets for k in range(max(0, sh.quantity))):
            if not any(need.values()) or self.cancelled:
                break
            if not first and sh.id in self._exhausted:
                continue
            s = self._new_sheet(sh, res)
            for np_ in parts:
                while need[np_.id] > 0 and not self.cancelled:
                    if time.monotonic() - t0 > self.p.time_limit_s:
                        self.cancelled = True
                        break
                    pl = self._try_place(s, np_)
                    if pl is None:
                        break
                    s.layout.placements.append(pl)
                    need[np_.id] -= 1
                    placed[np_.id] += 1
                    done += 1
                    self._tick(done / total, np_.name)
            if self.p.fill_sheets and not any(need.values()):
                for np_ in parts:
                    while extra[np_.id] > 0 and not self.cancelled:
                        if time.monotonic() - t0 > self.p.time_limit_s:
                            self.cancelled = True
                            break
                        pl = self._try_place(s, np_)
                        if pl is None:
                            break
                        s.layout.placements.append(pl)
                        extra[np_.id] -= 1
                        placed[np_.id] += 1
            if not s.layout.placements:
                # nothing fits on an empty sheet of this kind: skip its other copies
                self._exhausted.add(sh.id)
                continue
            self._finish_layout(s)
            result.layouts.append(s.layout)
        result.placed = {k: v for k, v in placed.items()}
        result.unplaced = {k: v for k, v in need.items() if v > 0}
        result.elapsed_s = time.monotonic() - t0
        job.result = result
        return result

    def _finish_layout(self, s: _Sheet) -> None:
        finish_layout(s.layout, s.full.bounds, s.bounds, s.used_area, s.area, self.spacing, self.p.gravity)


def finish_layout(lay: SheetLayout, full_bounds, part_bounds, used_area: float, area: float,
                  spacing: float, gravity: str) -> None:
    """Fill ``utilisation``, ``used_extent`` and ``remnant`` of a finished sheet layout
    (shared by all engines). ``part_bounds`` are the bounds of the placed exact outlines."""
    lay.utilisation = used_area / area if area else 0.0
    x0, y0, x1, y1 = full_bounds
    ux0 = min(b[0] for b in part_bounds); uy0 = min(b[1] for b in part_bounds)
    ux1 = max(b[2] for b in part_bounds); uy1 = max(b[3] for b in part_bounds)
    sp = spacing
    g = gravity
    if g == GRAVITY_LEFT:
        lay.used_extent = ux1 - x0
        lay.remnant = (min(x1, ux1 + sp), y0, x1, y1) if ux1 + sp < x1 else None
    elif g == GRAVITY_RIGHT:
        lay.used_extent = x1 - ux0
        lay.remnant = (x0, y0, max(x0, ux0 - sp), y1) if ux0 - sp > x0 else None
    elif g == GRAVITY_BOTTOM:
        lay.used_extent = uy1 - y0
        lay.remnant = (x0, min(y1, uy1 + sp), x1, y1) if uy1 + sp < y1 else None
    else:
        lay.used_extent = y1 - uy0
        lay.remnant = (x0, y0, x1, max(y0, uy0 - sp)) if uy0 - sp > y0 else None


def _placement_from(pid: int, angle: float, mirror: bool, origin, t) -> Placement:
    """Fold the centroid shift into the placement: p' = R M (p - c) + t = R M p + (t - R M c)."""
    probe = Placement(pid, 0.0, 0.0, angle, mirror).apply(origin)
    return Placement(pid, t[0] - probe[0], t[1] - probe[1], angle, mirror)


def _quality(res: NestResult) -> tuple:
    rem = res.layouts[-1].remnant if res.layouts else None
    rem_area = (rem[2] - rem[0]) * (rem[3] - rem[1]) if rem else 0.0
    return (sum(res.unplaced.values()), len(res.layouts), -rem_area)


class NestEngineUnavailable(RuntimeError):
    """The engine selected in ``NestParams.engine`` cannot run (optional package missing or
    unknown engine name). The message says what to install."""


def engine_class(name: str):
    """Engine class for ``NestParams.engine``. Raises :class:`NestEngineUnavailable`."""
    if not name or name == ENGINE_RASTER:
        return Nester
    if name == ENGINE_SPYRROW:
        from .sparrow_engine import SpyrrowNester, spyrrow_available
        if not spyrrow_available():
            raise NestEngineUnavailable(
                "The 'spyrrow' nesting engine needs the optional package spyrrow "
                "(pip install 'mlaser[nest]' or pip install spyrrow). "
                "Choose the raster engine in the nest parameters to nest without it.")
        return SpyrrowNester
    raise NestEngineUnavailable(f"Unknown nesting engine {name!r} (use {ENGINE_RASTER!r} or {ENGINE_SPYRROW!r})")


def available_engines() -> list[str]:
    """Engine names that can run in this installation (the raster engine always can)."""
    from .sparrow_engine import spyrrow_available
    return [ENGINE_RASTER] + ([ENGINE_SPYRROW] if spyrrow_available() else [])


def nest(job: NestJob, progress: Optional[ProgressFn] = None) -> NestResult:
    """Nest ``job`` (parts onto sheets) and store the result in ``job.result``.

    The engine is ``job.params.engine`` (default: the raster engine); an engine that cannot
    run raises :class:`NestEngineUnavailable` before anything is changed.
    With ``gravity == "auto"`` both directions (left, bottom) are tried and the result with
    fewer unplaced parts, then fewer sheets, then the shorter last-sheet extent is kept."""
    cls = engine_class(getattr(job.params, "engine", ENGINE_RASTER))
    if job.params.gravity != GRAVITY_AUTO:
        return cls(job, progress).run()
    best = None
    for k, g in enumerate((GRAVITY_LEFT, GRAVITY_BOTTOM)):
        job.params.gravity = g
        prog = None if progress is None else (lambda f, m, k=k: progress(0.5 * (k + f), m))
        try:
            r = cls(job, prog).run()
        finally:
            job.params.gravity = GRAVITY_AUTO
        r.gravity = g
        if best is None or _quality(r) < _quality(best):
            best = r
    job.result = best
    return best
