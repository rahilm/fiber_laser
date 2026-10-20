"""Optional nesting engine on sparrow (strip packing) through the ``spyrrow`` bindings.

Selected with ``NestParams.engine = "spyrrow"`` (``ENGINE_SPYRROW``); the raster engine in
:mod:`.engine` stays the default and the fallback. Install with the extra ``nest``
(``pip install 'mlaser[nest]'``). Licences: spyrrow MIT, sparrow MIT, jagua-rs MPL-2.0
(compiled unmodified into the spyrrow wheel; see docs/ASSETS.md).

sparrow solves *strip packing*: one strip of fixed height, minimise its length. This module
is the driver that turns it into sheet nesting:

* Frame: the strip runs along the gravity direction (left: +x, bottom: +y, right: -x,
  top: -y). Its height is the usable sheet width across that direction, its maximum length
  the usable extent along it. Placements are rotated back into sheet coordinates.
* Spacing: every part outline is grown by half the spacing (plus the simplification
  tolerance), so two grown outlines that do not overlap keep the full spacing. The strip is
  the usable region (sheet minus margin) grown by half the spacing, so the true outlines stay
  inside the margin.
* Sheets one at a time: a fast constructive solve (sparrow's left-bottom-fill, no
  optimisation) of the remaining copies picks the subset that fits the sheet length; the
  timed optimisation then compacts that subset, and while it leaves room more copies are
  added and the strip is re-optimised. Copies that do not fit go to the next sheet.
  Quantities, ``max_quantity`` fill of the last sheet, priorities (higher-priority copies
  are offered first) and per-part angle lists are honoured.
* Every accepted layout is verified with shapely against the exact outlines (inside the
  usable region, pairwise distance >= spacing); a copy that fails is dropped and retried on
  the next sheet.

Not supported (the result's ``notes`` say so when the job asks for them): part-in-part
(spyrrow 0.9 items have no holes, so outlines are filled), mirrored copies, common-line
touching (the kerf is used as an ordinary spacing), non-rectangular remnants are nested in
their bounding box and copies landing on a defect are rejected by the verification.
Parts made of several disjoint pieces are nested by their convex hull.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import shapely
from shapely import affinity
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from .engine import ProgressFn, _filled, _placement_from, finish_layout
from .model import (
    ENGINE_SPYRROW,
    GRAVITY_BOTTOM,
    GRAVITY_LEFT,
    GRAVITY_RIGHT,
    GRAVITY_TOP,
    NestJob,
    NestResult,
    NestSheet,
    Placement,
    SheetLayout,
)
from .shapes import fit_geometry, parts_geometry, sheet_geometry, transform_geom

_PHI = {GRAVITY_LEFT: 0.0, GRAVITY_BOTTOM: 90.0, GRAVITY_RIGHT: 180.0, GRAVITY_TOP: 270.0}
_VERIFY_TOL = 1e-3  # mm: tolerance of the shapely verification
_FLOAT_PAD = 0.005  # mm: extra growth against sparrow's f32 coordinates
_FIRST_AREA = 0.85  # first subset: grown area / strip area
_SEED = 0  # fixed seed: same job -> (nearly) same layout


def spyrrow_available() -> bool:
    try:
        import spyrrow  # noqa: F401
    except Exception:
        return False
    return True


def _rot(p, deg: float):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)


@dataclass
class _Shape:
    """One part definition prepared for sparrow (local coords: centroid at the origin)."""

    pid: int
    exact: Any  # exact outline (with holes) in local coords
    grown: Polygon  # filled, grown by spacing/2, simplified: sent to sparrow
    coords: list  # exterior ring of ``grown`` (no closing point)
    origin: tuple  # centroid of the original part coords
    angles: list  # allowed sheet-frame angles (deg)
    area: float  # grown area (capacity estimates)
    net_area: float  # exact area (utilisation)


@dataclass
class _Frame:
    """Strip frame of one sheet: sheet = R(phi) * strip + off."""

    sh: NestSheet
    full: Any
    usable: Any
    phi: float
    off: tuple
    height: float  # strip height
    length: float  # maximum strip length (the sheet)
    feasible: dict = field(default_factory=dict)  # pid -> strip-frame angles that fit


class SpyrrowNester:
    """Run a :class:`NestJob` with sparrow. Same interface as :class:`.engine.Nester`:
    ``SpyrrowNester(job, progress).run() -> NestResult``."""

    def __init__(self, job: NestJob, progress: Optional[ProgressFn] = None):
        import spyrrow

        self.sp = spyrrow
        self.job = job
        self.p = job.params
        self.progress = progress
        self.spacing = self.p.effective_spacing()
        self.cancelled = False
        self.solves = 0
        self.rejected = 0

    # ------------------------------------------------------------------ setup
    def _shapes(self) -> dict[int, _Shape]:
        tol = max(0.01, min(self.p.accuracy, 1.0) * 0.1)
        simp = min(0.2, max(0.02, self.p.accuracy * 0.2))
        grow = self.spacing / 2.0 + simp + _FLOAT_PAD
        out: dict[int, _Shape] = {}
        for np_ in self.job.parts:
            g = parts_geometry(np_.parts, tol=tol)
            if g.is_empty:
                continue
            c = g.centroid
            g = fit_geometry(affinity.translate(g, -c.x, -c.y), np_.fit)
            filled = _filled(g)
            if filled.geom_type != "Polygon":
                filled = filled.convex_hull
            grown = filled.buffer(grow, quad_segs=3, join_style="mitre", mitre_limit=2.0)
            grown = grown.simplify(simp, preserve_topology=True)
            if grown.geom_type != "Polygon":
                grown = grown.convex_hull
            grown = orient(Polygon(grown.exterior), 1.0)
            coords = [(float(x), float(y)) for x, y in grown.exterior.coords[:-1]]
            angles = self.p.part_angles(np_)
            angles = sorted({round(float(a) % 360.0, 6) for a in angles}) or [0.0]
            out[np_.id] = _Shape(np_.id, g, grown, coords, (c.x, c.y), angles, grown.area, g.area)
        return out

    def _frame(self, sh: NestSheet) -> Optional[_Frame]:
        margin = self.p.margin if sh.margin is None else sh.margin
        full, usable = sheet_geometry(sh, margin)
        if usable.is_empty:
            return None
        h = self.spacing / 2.0
        bx0, by0, bx1, by1 = usable.bounds
        bx0, by0, bx1, by1 = bx0 - h, by0 - h, bx1 + h, by1 + h
        phi = _PHI.get(self.p.gravity, 0.0)
        along_x = phi in (0.0, 180.0)
        length, height = (bx1 - bx0, by1 - by0) if along_x else (by1 - by0, bx1 - bx0)
        corners = [_rot(q, phi) for q in ((0, 0), (length, 0), (length, height), (0, height))]
        off = (bx0 - min(q[0] for q in corners), by0 - min(q[1] for q in corners))
        fr = _Frame(sh, full, usable, phi, off, height, length)
        shapely.prepare(usable)
        return fr

    def _feasible_angles(self, fr: _Frame, s: _Shape) -> list[float]:
        got = fr.feasible.get(s.pid)
        if got is None:
            got = []
            for a in s.angles:
                r = (a - fr.phi) % 360.0
                x0, y0, x1, y1 = affinity.rotate(s.grown, r, origin=(0, 0)).bounds
                if y1 - y0 <= fr.height - 1e-6 and x1 - x0 <= fr.length - 1e-6:
                    got.append(r)
            fr.feasible[s.pid] = got
        return got

    # ------------------------------------------------------------------ solving
    def _solve(self, fr: _Frame, counts: dict[int, int], seconds: int):
        """Strip-pack ``counts`` (pid -> copies). ``seconds == 0``: constructive only.
        Returns a list of (pid, strip angle, translation, grown strip geom) or None."""
        sp = self.sp
        items = [
            sp.Item(
                str(pid),
                self.shapes[pid].coords,
                demand=n,
                allowed_orientations=self._feasible_angles(fr, self.shapes[pid]),
            )
            for pid, n in counts.items()
            if n > 0
        ]
        if not items:
            return []
        inst = sp.StripPackingInstance(self.job.name or "mlaser", float(fr.height), items)
        cfg = sp.StripPackingConfig(
            early_termination=True,
            total_computation_time=None,
            exploration_time=int(round(seconds * 0.8)),
            compression_time=max(0, seconds - int(round(seconds * 0.8))),
            seed=_SEED,
        )
        self.solves += 1
        try:
            sol = inst.solve(cfg)
        except BaseException as e:  # pyo3 maps Rust panics to PanicException(BaseException)
            if isinstance(e, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            return None
        out = []
        for it in sol.placed_items:
            pid = int(it.id)
            r = float(it.rotation) % 360.0
            g = affinity.translate(
                affinity.rotate(self.shapes[pid].grown, r, origin=(0, 0)),
                it.translation[0],
                it.translation[1],
            )
            out.append((pid, r, (float(it.translation[0]), float(it.translation[1])), g))
        return out

    def _cut(self, fr: _Frame, sol) -> list:
        """Copies of a strip solution that lie inside the sheet length."""
        lim = fr.length + 1e-6
        return [t for t in sol if t[3].bounds[2] <= lim and t[3].bounds[0] >= -1e-6]

    @staticmethod
    def _counts(sol) -> dict[int, int]:
        c: dict[int, int] = {}
        for t in sol:
            c[t[0]] = c.get(t[0], 0) + 1
        return c

    def _length(self, sol) -> float:
        return max((t[3].bounds[2] for t in sol), default=0.0)

    def _take(
        self,
        rest: list[int],
        fr: _Frame,
        cap_area: float,
        n_max: Optional[int] = None,
        skip: frozenset = frozenset(),
    ) -> dict[int, int]:
        """Copies from ``rest`` (priority order) whose grown area sums to about ``cap_area``
        (at least one), at most ``n_max``; part types in ``skip`` or without a fitting angle
        are passed over."""
        counts: dict[int, int] = {}
        a = 0.0
        n = 0
        for pid in rest:
            if pid in skip or not self._feasible_angles(fr, self.shapes[pid]):
                continue
            if counts and a + self.shapes[pid].area > cap_area:
                break
            counts[pid] = counts.get(pid, 0) + 1
            a += self.shapes[pid].area
            n += 1
            if n_max is not None and n >= n_max:
                break
        return counts

    def _fill(self, fr: _Frame, pool: list[int], base: list, budget: float) -> list:
        """Best strip solution for this sheet that contains every copy of ``base`` (an
        accepted earlier solution of this sheet; may be empty) plus as many copies of
        ``pool`` (priority order) as fit within ``budget`` seconds.

        Search: start from the copies whose grown area is ~85 % of the strip, optimise
        briefly; a strip that is too long is cut to the copies inside the sheet. Then add
        copies (as many as the free length suggests), halving the step after a failure and
        skipping part types that no longer fit, until nothing more fits or time is up."""
        strip_area = fr.height * fr.length
        base_counts = self._counts(base)
        t_end = time.monotonic() + budget
        secs = 0 if budget < 1.0 else int(min(5.0, max(1.0, budget / 8.0)))

        def merged(a: dict[int, int], b: dict[int, int]) -> dict[int, int]:
            c = dict(a)
            for k, v in b.items():
                c[k] = c.get(k, 0) + v
            return c

        def keeps_base(sol) -> bool:
            got = self._counts(sol)
            return all(got.get(k, 0) >= v for k, v in base_counts.items())

        def rest_of(target: dict[int, int]) -> list[int]:
            used = {k: v - base_counts.get(k, 0) for k, v in target.items()}
            out = []
            for pid in pool:
                if used.get(pid, 0) > 0:
                    used[pid] -= 1
                else:
                    out.append(pid)
            return out

        used_area = sum(t[3].area for t in base)
        best = base
        target = dict(base_counts)
        step: Optional[int] = None  # copies to add next (None: by free area)
        skip: set[int] = set()
        first = True
        while not self.cancelled:
            if not first and (time.monotonic() >= t_end - (secs if secs else 0.05)):
                break
            rest = rest_of(target)
            if first:
                add = self._take(rest, fr, max(0.0, _FIRST_AREA * strip_area - used_area))
            else:
                length = self._length(best)
                dens = sum(t[3].area for t in best) / max(1e-9, length * fr.height) if best else 0.8
                free = (fr.length - length) * fr.height * max(0.5, min(0.95, dens))
                add = self._take(rest, fr, free, step, frozenset(skip))
            if not add:
                break
            cand = merged(target, add)
            sol = self._solve(fr, cand, secs) or []
            self._tick(self.done_frac(), f"spyrrow: sheet {fr.sh.name}, {sum(cand.values())} parts")
            fits = len(sol) == sum(cand.values()) and self._length(sol) <= fr.length + 1e-6
            left = t_end - time.monotonic()
            if not fits and secs and sol and self._length(sol) <= fr.length * 1.03 and left >= 3 * secs:
                # nearly fits: one longer optimisation before giving up on these copies
                sol = self._solve(fr, cand, int(min(left / 2.0, 6.0 * secs))) or []
                fits = len(sol) == sum(cand.values()) and self._length(sol) <= fr.length + 1e-6
            if fits:
                best, target, step = sol, cand, None
            else:
                cut = self._cut(fr, sol)
                if keeps_base(cut) and len(cut) > len(best):
                    best, target = cut, self._counts(cut)
                n_add = sum(add.values())
                if first or n_add > 1:
                    step = max(1, n_add // 2) if not first else 1
                else:
                    skip.update(add)  # this part type no longer fits here
                    step = None
            first = False
        # time left and the sheet holds the whole pool: compact it (shorter used length, larger
        # remnant), as long as nothing is lost
        left = t_end - time.monotonic()
        if secs and best and not rest_of(self._counts(best)) and left >= 1.0 and not self.cancelled:
            sol = self._solve(fr, self._counts(best), int(left)) or []
            if len(sol) == len(best) and self._length(sol) < self._length(best):
                best = sol
        return best

    # ------------------------------------------------------------------ verification
    def _exact(self, fr: _Frame, pid: int, r: float, t) -> tuple[Placement, Any]:
        s = self.shapes[pid]
        angle = (fr.phi + r) % 360.0
        for a in s.angles:  # snap float noise to the allowed angle
            if abs(((angle - a + 180.0) % 360.0) - 180.0) < 1e-3:
                angle = a
                break
        rt = _rot(t, fr.phi)
        T = (rt[0] + fr.off[0], rt[1] + fr.off[1])
        pl = _placement_from(pid, angle, False, s.origin, T)
        g = transform_geom(s.exact, Placement(pid, T[0], T[1], angle, False))
        return pl, g

    def _verify(self, fr: _Frame, sol) -> tuple[list, list]:
        """Exact placements of a strip solution; copies that violate the sheet or spacing are
        returned separately (their part ids go back to the pool)."""
        ok: list = []
        bad: list = []
        inside = fr.usable.buffer(_VERIFY_TOL)
        shapely.prepare(inside)
        geoms: list = []
        for pid, r, t, _ in sol:
            pl, g = self._exact(fr, pid, r, t)
            good = inside.contains(g)
            if good and geoms:
                tree = shapely.STRtree(geoms)
                near = tree.query(
                    g, predicate="dwithin", distance=max(self.spacing - _VERIFY_TOL, 0.0) + 1e-9
                )
                for k in near:
                    d = g.distance(geoms[k])
                    if d < self.spacing - _VERIFY_TOL or (
                        self.spacing <= _VERIFY_TOL and g.intersection(geoms[k]).area > 1e-6
                    ):
                        good = False
                        break
            if good:
                geoms.append(g)
                ok.append((pl, g))
            else:
                bad.append(pid)
        return ok, bad

    # ------------------------------------------------------------------ main loop
    def _tick(self, frac: float, msg: str) -> None:
        if self.progress is not None and self.progress(frac, msg) is False:
            self.cancelled = True

    def done_frac(self) -> float:
        return self._done / max(1, self._total)

    def _notes(self) -> list[str]:
        p = self.p
        notes = []
        if p.part_in_part:
            notes.append("spyrrow: part-in-part is not supported (outlines nested filled)")
        if p.allow_mirror or any(np_.mirror for np_ in self.job.parts):
            notes.append("spyrrow: mirrored copies are not supported (placed unmirrored)")
        if p.common_line:
            notes.append("spyrrow: common line is not supported (kerf used as part spacing)")
        if any(sh.outline for sh in self.job.sheets):
            notes.append("spyrrow: remnant outlines are nested in their bounding box")
        return notes

    def run(self) -> NestResult:
        t0 = time.monotonic()
        job = self.job
        self.shapes = self._shapes()
        parts = [p for p in job.parts if p.id in self.shapes]
        parts.sort(key=lambda p: (-p.priority, -self.shapes[p.id].net_area))
        need = {p.id: max(0, p.quantity) for p in parts}
        extra = {
            p.id: max(0, (p.max_quantity if p.max_quantity is not None else p.quantity) - p.quantity)
            for p in parts
        }
        self._total = sum(need.values())
        self._done = 0
        placed = {p.id: 0 for p in parts}
        result = NestResult(gravity=self.p.gravity, engine=ENGINE_SPYRROW, notes=self._notes())
        exhausted: set[int] = set()
        sheets = [(sh, k == 0) for sh in job.sheets for k in range(max(0, sh.quantity))]
        for n_sheet, (sh, first) in enumerate(sheets):
            if not any(need.values()) or self.cancelled:
                break
            if not first and sh.id in exhausted:
                continue
            fr = self._frame(sh)
            if fr is None:
                exhausted.add(sh.id)
                continue
            pool = [p.id for p in parts for _ in range(need[p.id])]
            # time for this sheet: the rest of the budget over the sheets still expected
            left = self.p.time_limit_s - (time.monotonic() - t0)
            rem_area = sum(self.shapes[pid].area for pid in pool)
            est = max(
                1, min(len(sheets) - n_sheet, math.ceil(rem_area / max(1e-9, 0.8 * fr.height * fr.length)))
            )
            # the first of several sheets decides most: give it the larger share
            sol = self._fill(fr, pool, [], max(0.0, left if est == 1 else left * 0.6))
            ok, bad = self._verify(fr, sol)
            self.rejected += len(bad)
            lay = SheetLayout(sh.id)
            for pl, _ in ok:
                lay.placements.append(pl)
                need[pl.part_id] -= 1
                placed[pl.part_id] += 1
                self._done += 1
            # leftover space of the sheet that completes the job: fill up to max_quantity
            if self.p.fill_sheets and not any(need.values()) and any(extra.values()) and not self.cancelled:
                base = [t for t in sol if t[0] not in bad] if not bad else []
                if base:
                    left = self.p.time_limit_s - (time.monotonic() - t0)
                    xpool = [p.id for p in parts for _ in range(extra[p.id])]
                    sol2 = self._fill(fr, xpool, base, max(0.0, left))
                    if len(sol2) > len(base):
                        ok2, bad2 = self._verify(fr, sol2)
                        if len(ok2) > len(ok) and not bad2:
                            got = self._counts([(pl.part_id,) for pl, _ in ok2])
                            for pid, n in got.items():
                                add = n - self._counts([(pl.part_id,) for pl, _ in ok]).get(pid, 0)
                                extra[pid] -= add
                                placed[pid] += add
                            ok = ok2
                            lay.placements = [pl for pl, _ in ok]
            self._tick(self.done_frac(), f"spyrrow: sheet {sh.name}")
            if not lay.placements:
                exhausted.add(sh.id)
                continue
            gs = [g for _, g in ok]
            finish_layout(
                lay,
                fr.full.bounds,
                [g.bounds for g in gs],
                sum(g.area for g in gs),
                fr.full.area,
                self.spacing,
                self.p.gravity,
            )
            lay.extra["engine"] = ENGINE_SPYRROW
            if bad:
                lay.extra["rejected"] = len(bad)
            result.layouts.append(lay)
        result.placed = dict(placed)
        result.unplaced = {k: v for k, v in need.items() if v > 0}
        result.elapsed_s = time.monotonic() - t0
        if self.cancelled:
            result.notes.append("spyrrow: cancelled or time limit reached")
        job.result = result
        return result


def strip_frame_box(fr: _Frame):
    """The strip rectangle of a frame in sheet coordinates (diagnostics / tests)."""
    corners = [_rot(q, fr.phi) for q in ((0, 0), (fr.length, 0), (fr.length, fr.height), (0, fr.height))]
    xs = [q[0] + fr.off[0] for q in corners]
    ys = [q[1] + fr.off[1] for q in corners]
    return box(min(xs), min(ys), max(xs), max(ys))


__all__ = ["SpyrrowNester", "spyrrow_available", "strip_frame_box"]
