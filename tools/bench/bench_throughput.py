"""Compile throughput on the review's synthetic drawing, default machine (smoothing ON).

    python tools/bench/bench_throughput.py --parts 200 [--repeat 3] [--mem] [--profile]
    MLASER_NATIVE=0 python tools/bench/bench_throughput.py --parts 200     # Python port
    MLASER_COMPILE_WORKERS=8 python tools/bench/bench_throughput.py --parts 1000

The drawing is docs/review's ``bench_interactive.make_doc``: N parts, half a 24 mm square with a
10 mm hole, half a 24-point star with two 4 mm holes, line lead-ins of 2 mm, four layers.  The
machine is ``Machine()`` (smoothing on, the default profile) and the CAM parameters
``CamParams()`` (sort mode 5).  Printed: ``plan_toolpath`` and ``compile_items`` times (best of
``--repeat``), steps, us/step, and a fingerprint of everything a compile produces (wire,
frames, markers, cut blocks, steps, carry, laser-on length and the estimate, floats as
``float.hex``) so runs of different variants can be compared for bit identity.

``--mem`` measures one compile with tracemalloc (peak and retained bytes per step) and the
process RSS growth; run it in a fresh process per variant.
"""
from __future__ import annotations

import argparse
import cProfile
import gc
import hashlib
import math
import os
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mlaser.cam.toolpath import CamParams  # noqa: E402
from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part  # noqa: E402
from mlaser.nc import compiler as C  # noqa: E402
from mlaser.nc.machine import Machine  # noqa: E402


def rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def circ(cx, cy, r):
    s = (cx + r, cy)
    return Contour([Arc(s, s, (cx, cy), True)], True)


def star(cx, cy, r0, r1, n=12):
    pts = []
    for k in range(2 * n):
        r = r0 if k % 2 == 0 else r1
        a = math.pi * k / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return Contour([Line(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], True)


def make_doc(n: int) -> Document:
    """The review's test drawing (bench_interactive.make_doc)."""
    cols = int(math.ceil(math.sqrt(n * 2)))
    parts = []
    for k in range(n):
        x, y = (k % cols) * 30.0, (k // cols) * 30.0
        if k % 2 == 0:
            cs = [circ(x + 12, y + 12, 5), rect(x, y, 24, 24)]
        else:
            cs = [circ(x + 8, y + 12, 2), circ(x + 16, y + 12, 2), star(x + 12, y + 12, 12, 7)]
        parts.append(Part(cs, Craft(layer=1 + k % 4, lead_in=LeadParams(kind=1, length=2.0))))
    return Document(parts=parts)


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except OSError:  # not Linux
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def fingerprint(job) -> str:
    """Hash of everything the compile produced (bit identity across variants)."""
    from mlaser.nc.packed import PackedWire
    h = hashlib.sha256()
    w = job.wire
    if not isinstance(w, PackedWire):
        w = PackedWire.from_items(w, job._starts_for_packing())
    h.update(repr(len(w)).encode())
    for r in range(len(w.cnt)):
        h.update(repr((w.words[w.woff[r]:w.woff[r + 1]].tolist(), int(w.cnt[r]), bool(w.flag[r]))).encode())
    for fr in job.iter_frames():
        h.update(repr(fr).encode())
    h.update(repr((job.markers, job.cut_blocks, job.steps, job.overspeed_periods, job.start_pulses,
                   job.end_pulses, [float(x).hex() for x in job.pulse_carry],
                   float(job.laser_on_length_mm).hex(), float(job.rapid_length_mm).hex(),
                   sorted((k, float(v).hex()) for k, v in job.estimate.items()),
                   [(i, repr(mi)) for i, mi in job.manu])).encode())
    return h.hexdigest()[:16]


def raster(w: int) -> None:
    import numpy as np
    from mlaser.cam import raster_engraving as R
    from mlaser.engrave.raster import RasterParams, process_image, raster_from_bitmap
    from mlaser.nc import compile_job
    h = w * 3 // 4
    img = (np.indices((h, w)).sum(0) % 255).astype(np.uint8)
    p = RasterParams(mode="dither", width_mm=100 * w / 800, rho=10)
    r = raster_from_bitmap(process_image(img, p), p.rho, p.power, p)
    doc = Document(parts=[R.make_raster_part(r)])
    gc.collect()
    r0 = rss_mb()
    t, c = time.perf_counter(), time.process_time()
    job = compile_job(doc, CamParams(), Machine())
    t, c = time.perf_counter() - t, time.process_time() - c
    gc.collect()
    r1 = rss_mb()
    import resource
    print(f"raster {w}x{h}: {len(r)} runs, {job.steps} steps, compile_job {t:.1f} s wall / {c:.1f} s CPU, "
          f"RSS +{r1 - r0:.0f} MB (+{(r1 - r0) / job.steps * 1e6:.0f} MB per million steps), "
          f"maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB, "
          f"wire {job.wire!r}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", type=int, default=200)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--mem", action="store_true", help="tracemalloc peak / retained and RSS growth")
    ap.add_argument("--profile", action="store_true", help="cProfile of one compile_items")
    ap.add_argument("--no-smooth", action="store_true", help="Machine(smooth=False)")
    ap.add_argument("--no-fingerprint", action="store_true")
    ap.add_argument("--raster", type=int, default=0, metavar="W",
                    help="instead: compile_job of a W x 3/4 W px dither engraving (the review's "
                         "800 x 600 case: 54.6 M steps); prints the RSS growth")
    ap.add_argument("--order", type=int, default=None, metavar="MODE",
                    help="instead: build_toolpath with CamParams(sort_mode=MODE) (best of --repeat), "
                         "e.g. 6 / 7 = ring sort")
    a = ap.parse_args()
    if a.order is not None:
        doc = make_doc(a.parts)
        from mlaser.cam.toolpath import build_toolpath
        build_toolpath(make_doc(4), CamParams(sort_mode=a.order))     # warm (kernel selection)
        ts = []
        for _ in range(a.repeat):
            t = time.perf_counter()
            items = build_toolpath(doc, CamParams(sort_mode=a.order))
            ts.append(time.perf_counter() - t)
        h = hashlib.sha256(repr([(type(i).__name__, getattr(i, "start", None), getattr(i, "end", None))
                                 for i in items]).encode()).hexdigest()[:16]
        print(f"N={a.parts} sort_mode={a.order}: build_toolpath best {min(ts):.2f} s, {len(items)} items, "
              f"order fingerprint {h}")
        return
    if a.raster:
        raster(a.raster)
        return

    from mlaser.parity_ports import kernel_status
    doc = make_doc(a.parts)
    m = Machine(smooth=not a.no_smooth)
    t = time.perf_counter()
    placed, prm, items = C.plan_toolpath(doc, CamParams(), (0.0, 0.0), None)
    t_tp = time.perf_counter() - t
    C.compile_items(items[:40], m, prm.origin, placed, False)       # warm: kernel selection
    print(kernel_status().text())
    print(f"N={a.parts} smooth={m.smooth} items={len(items)} plan_toolpath {t_tp:.2f} s "
          f"workers={os.environ.get('MLASER_COMPILE_WORKERS', 'default')}", flush=True)
    if a.mem:
        import tracemalloc
        gc.collect()
        r0 = rss_mb()
        tracemalloc.start()
        job = C.compile_items(items, m, prm.origin, placed, False)
        gc.collect()
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        r1 = rss_mb()
        print(f"  steps {job.steps}: retained {cur / 2**20:.1f} MB ({cur / job.steps:.1f} B/step), "
              f"peak {peak / 2**20:.1f} MB ({peak / job.steps:.0f} B/step), RSS +{r1 - r0:.0f} MB "
              f"(+{(r1 - r0) / job.steps * 1e6:.0f} MB per million steps); wire {type(job.wire).__name__}")
        return
    if a.profile:
        pr = cProfile.Profile()
        pr.enable()
        job = C.compile_items(items, m, prm.origin, placed, False)
        pr.disable()
        pstats.Stats(pr).sort_stats("tottime").print_stats(30)
        return
    ts, cs = [], []
    job = None
    for _ in range(a.repeat):
        gc.collect()
        t, c = time.perf_counter(), time.process_time()
        job = C.compile_items(items, m, prm.origin, placed, False)
        ts.append(time.perf_counter() - t)
        cs.append(time.process_time() - c)
    best = min(ts)
    # CPU time of this process (the pool workers of a parallel compile are not included):
    # steadier than the wall time on a loaded host
    print(f"  compile_items best {best:.2f} s wall (runs {', '.join(f'{x:.2f}' for x in ts)}), "
          f"{min(cs):.2f} s CPU of this process; {job.steps} steps, {best / job.steps * 1e6:.2f} us/step",
          flush=True)
    if not a.no_fingerprint:
        print(f"  fingerprint {fingerprint(job)}")


if __name__ == "__main__":
    main()
