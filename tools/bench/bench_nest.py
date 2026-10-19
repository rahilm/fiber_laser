"""Nesting engines compared: raster (built-in) vs sparrow (spyrrow) on a few synthetic jobs.

Usage: python -I tools/bench/bench_nest.py [time_limit_s=30] [jobs=all]
Prints per job and engine: wall time, sheets, placed/unplaced, mean utilisation, the
utilisation of the full sheets (all but the last) and the used length of the last sheet.
Every layout is verified with shapely (inside the usable region, spacing kept).
"""

import math
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from mlaser.model import Arc, Contour, Line, Part  # noqa: E402
from mlaser.nest import ENGINE_RASTER, ENGINE_SPYRROW, NestJob, NestParams, NestPart, NestSheet  # noqa: E402
from mlaser.nest import available_engines, nest  # noqa: E402
from mlaser.nest.shapes import parts_geometry, sheet_geometry, transform_part  # noqa: E402


def poly(pts):
    return Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)


def rect(x, y, w, h):
    return poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])


def circle(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def lshape(w, h, t):
    return poly([(0, 0), (w, 0), (w, t), (t, t), (t, h), (0, h)])


def star(r1, r2, n=5):
    return poly(
        [
            (
                (r1 if k % 2 == 0 else r2) * math.cos(math.pi * k / n),
                (r1 if k % 2 == 0 else r2) * math.sin(math.pi * k / n),
            )
            for k in range(2 * n)
        ]
    )


def job_mixed(engine, t):
    job = NestJob(params=NestParams(spacing=3, margin=5, rotation_step=90, engine=engine, time_limit_s=t))
    job.add_part(NestPart([Part([rect(0, 0, 300, 200), circle(150, 100, 70)])], name="plate", quantity=12))
    job.add_part(NestPart([Part([rect(0, 0, 120, 60)])], name="rect", quantity=60))
    job.add_part(NestPart([Part([lshape(200, 150, 40)])], name="L", quantity=10))
    job.add_sheet(NestSheet(1200, 800, quantity=4))
    return job


def job_irregular(engine, t):
    job = NestJob(params=NestParams(spacing=2, margin=5, rotation_step=45, engine=engine, time_limit_s=t))
    job.add_part(NestPart([Part([star(80, 35)])], name="star", quantity=25))
    job.add_part(NestPart([Part([lshape(160, 120, 30)])], name="L", quantity=20))
    job.add_part(NestPart([Part([poly([(0, 0), (150, 0), (75, 110)])])], name="tri", quantity=25))
    job.add_sheet(NestSheet(1500, 1000, quantity=4))
    return job


def job_one_sheet(engine, t):
    job = NestJob(params=NestParams(spacing=3, margin=5, rotation_step=90, engine=engine, time_limit_s=t))
    job.add_part(NestPart([Part([rect(0, 0, 300, 200), circle(150, 100, 70)])], name="plate", quantity=6))
    job.add_part(NestPart([Part([rect(0, 0, 120, 60)])], name="rect", quantity=20))
    job.add_part(NestPart([Part([lshape(200, 150, 40)])], name="L", quantity=10))
    job.add_sheet(NestSheet(1200, 800, quantity=2))
    return job


JOBS = {"one_sheet": job_one_sheet, "mixed": job_mixed, "irregular": job_irregular}


def verify(job, res):
    sp = job.params.effective_spacing()
    for lay in res.layouts:
        _, usable = sheet_geometry(job.sheet(lay.sheet_id), job.params.margin)
        gs = [
            parts_geometry([transform_part(p, pl) for p in job.part(pl.part_id).parts])
            for pl in lay.placements
        ]
        assert all(usable.buffer(1e-3).contains(g) for g in gs)
        for i in range(len(gs)):
            for j in range(i + 1, len(gs)):
                assert gs[i].distance(gs[j]) >= sp - 1e-3


def main(argv):
    t = float(argv[1]) if len(argv) > 1 else 30.0
    names = argv[2].split(",") if len(argv) > 2 else list(JOBS)
    engines = [e for e in (ENGINE_RASTER, ENGINE_SPYRROW) if e in available_engines()]
    print(f"time limit {t:g} s; engines {engines}")
    print(
        f"{'job':10s} {'engine':8s} {'time s':>7s} {'sheets':>6s} {'placed':>6s} {'unpl':>4s} "
        f"{'util':>6s} {'full':>6s} {'last mm':>8s}"
    )
    for name in names:
        for eng in engines:
            job = JOBS[name](eng, t)
            t0 = time.perf_counter()
            res = nest(job)
            dt = time.perf_counter() - t0
            verify(job, res)
            full = [lay.utilisation for lay in res.layouts[:-1]]
            print(
                f"{name:10s} {eng:8s} {dt:7.2f} {len(res.layouts):6d} {sum(res.placed.values()):6d} "
                f"{sum(res.unplaced.values()):4d} {100 * res.utilisation:5.1f}% "
                f"{(100 * sum(full) / len(full)) if full else float('nan'):5.1f}% "
                f"{res.layouts[-1].used_extent if res.layouts else 0:8.0f}"
            )


if __name__ == "__main__":
    main(sys.argv)
