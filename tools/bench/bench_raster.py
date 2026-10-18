"""Raster engraving object vs per-segment Parts on the 800x600 dithered test image
(architecture.md §4.2 / §4.3 item 7).

    QT_QPA_PLATFORM=offscreen .venv/bin/python tools/bench/bench_raster.py [--old K] [--compile K]

New path (full image): raster_from_bitmap, build_toolpath (sort 0), CanvasView.set_document +
set_toolpath + render, deepcopy (undo snapshot), memory.  ``--old K``: the same for today's
per-segment document cut to its first K parts (the full one is out of reach: build_tree is
O(n^2)).  ``--compile K``: compile_job of the first K burn runs both ways, asserting identical
wire and frames, plus the JumpAddTime settle share of the steps.
"""

import argparse
import copy
import gc
import resource
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np  # noqa: E402

from mlaser.cam import raster_engraving as R  # noqa: E402
from mlaser.cam.toolpath import CamParams, build_toolpath  # noqa: E402
from mlaser.engrave.raster import RasterParams, gcode_from_bitmap, process_image, raster_from_bitmap  # noqa: E402
from mlaser.io.gcode import parse_gcode  # noqa: E402
from mlaser.model import Document  # noqa: E402


def T(label, fn, *a, **kw):
    t = time.perf_counter()
    r = fn(*a, **kw)
    print(f"{label:58s} {(time.perf_counter() - t) * 1000:10.1f} ms", flush=True)
    return r


def mem(label, fn, *a):
    gc.collect()
    tracemalloc.start()
    r = fn(*a)
    cur, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"{label:58s} {cur / 2**20:10.1f} MB", flush=True)
    return r


def first_runs(r, k):
    """RasterEngraving of the first k runs."""
    k = min(k, len(r))
    return R.RasterEngraving(
        r.x0[:k], r.x1[:k], r.row[:k], r.row_y, r.power[:k], rho=r.rho, overscan=r.overscan, params=r.params
    )


def canvas_open(doc, label):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from mlaser.ui.canvas import CanvasView

    app = QApplication.instance() or QApplication([])
    v = CanvasView()
    v.resize(1600, 1000)
    v.show()
    app.processEvents()
    t0 = time.perf_counter()
    T(f"[{label}] canvas.set_document", v.set_document, doc)
    items = T(f"[{label}] build_toolpath sort 0", build_toolpath, doc, CamParams(sort_mode=0))
    T(f"[{label}] canvas.set_toolpath", v.set_toolpath, items)
    print(
        f"[{label}] open total (set_document + toolpath + set_toolpath) "
        f"{(time.perf_counter() - t0) * 1000:10.1f} ms; {len(items)} items, "
        f"{len(v.scene().items())} scene items",
        flush=True,
    )
    img = QImage(1600, 1000, QImage.Format_ARGB32)

    def paint():
        p = QPainter(img)
        v.render(p)
        p.end()

    T(f"[{label}] render", paint)
    T(f"[{label}] render again", paint)
    T(f"[{label}] deepcopy (undo snapshot)", copy.deepcopy, doc)
    v.deleteLater()
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=int, default=0)
    ap.add_argument("--compile", type=int, default=0)
    a = ap.parse_args()
    img = (np.indices((600, 800)).sum(0) % 255).astype(np.uint8)
    p = RasterParams(mode="dither", width_mm=100, rho=10)
    bmp = T("process_image (dither)", process_image, img, p)
    text = T("gcode_from_bitmap", gcode_from_bitmap, bmp, p.rho, p.power)
    r = T("raster_from_bitmap", raster_from_bitmap, bmp, p.rho, p.power, p)
    print(f"{len(r)} burn runs, {len(r.row_y)} rows, arrays {r.nbytes() / 2**20:.1f} MB")
    new = Document(parts=[R.make_raster_part(r)])
    T("expand_document (per-segment Parts)", R.expand_document, new)
    if a.old:
        full = mem("parse_gcode (per-segment Parts) retained", parse_gcode, text)
        print(f"{len(full.parts)} parts")
        old = Document(parts=full.parts[: a.old])
        canvas_open(old, f"old {a.old} parts")
    canvas_open(new, "new raster part")
    if a.compile:
        from mlaser.nc import compile_job
        from mlaser.nc.machine import Machine
        from mlaser.protocol import frames as F

        sub = first_runs(r, a.compile)
        dn = Document(parts=[R.make_raster_part(sub)])
        do = R.expand_document(dn)
        jo = T(f"compile_job old ({len(do.parts)} parts)", compile_job, do, CamParams(sort_mode=0), Machine())
        jn = T(f"compile_job new ({len(sub)} runs)", compile_job, dn, CamParams(sort_mode=0), Machine())
        assert jo.wire == jn.wire and jo.frames() == jn.frames(), "wire / frames differ"
        print(f"identical wire ({len(jn.wire)} items) and frames ({len(jn.frames())})")
        m = Machine()
        settle = int(round(m.jump_add_time_ms / m.period_ms))
        # runs of identical zero-delta, laser-off step words right after a rapid
        runs, cur, prev = 0, 0, None
        for w in jn.wire:
            if (w[0] & 0xFFFF) == F.OP_STEP:
                dx, dy, _f, duty, _p = F.decode_step(w[1:3])
                if dx == 0 and dy == 0 and duty == 0 and (prev is None or w == prev):
                    cur += 1
                    prev = w
                    continue
            if cur >= settle:
                runs += 1
            cur, prev = 0, None
        print(
            f"steps {jn.steps}, replay {jn.duration_s:.0f} s; >= {settle}-step idle runs: {runs} "
            f"(~{runs * settle} steps, {100.0 * runs * settle / max(1, jn.steps):.0f}% of the steps)"
        )
    print(f"maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")


if __name__ == "__main__":
    main()
