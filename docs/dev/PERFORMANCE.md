# Performance: measurements before and after (Wave 2, performance and libraries track)

The "before" numbers are those of docs/review/architecture.md §4 (work/review/perf/, same
machine class). "After" was measured on this branch, CPython 3.12, 64-core x86-64 Linux,
offscreen Qt. The benchmark scripts are in `tools/bench/` (path-independent copies of the
review's scripts plus a few new ones). Every change below keeps `tools/make_goldens.py --check`
bit-identical on the default profile.

## 1. Headline numbers

| Measurement | Before | After | How |
|---|---|---|---|
| `build_toolpath`, 2,000 parts, sort 5 (default) | 2.2 s | **0.27 s** (0.29 s inside a MainWindow session) | `tools/bench/bench_toolpath.py 2000`, best of 3 |
| `build_toolpath`, 2,000 parts, sort 0 | 1.42 s | 0.14 s | same |
| `build_toolpath`, 2,000 parts, sort 6 / 20 / 21 | 2.24 / 3.9 / 1.45 s | 1.0 / 2.6 / 0.2 s | mode 6 (ring sort) and 20 (2 s time limit) not vectorised |
| `MainWindow._refresh` per edit, 2,000 parts | 3.6 s | **0.65 s** best of 3 (1.1 s in `bench_large.py`'s single run) | rest: canvas 0.2 s, overlay 0.13 s, part list 0.16 s (`ui/panels.py`, not this track) |
| `MainWindow.set_document`, 2,000 parts | 3.76 s | 1.0 s | `tools/bench/bench_large.py 2000` |
| `build_toolpath`, 30,000-part scan | 11.3 s | 1.35 s | `tools/bench/bench_scan_canvas.py 30000` |
| **Open the 30,000-part scan** (`MainWindow.set_document`) | **351 s** | **5.4 s** | same |
| Open an 800×600 dither engraving (184,627 burn runs) | > 454 s (canvas 6.9 s + sort-0 toolpath 447 s) | **1.18 s** as one raster object | `tools/bench/bench_raster.py` |
| Wire memory per hour of job | ~1 GB (792 MB measured) | **69 MB** | `tools/bench/bench_wire_memory.py 300` |
| Emitter (sample → quantise → encode) | 2.5–3.4 µs/step | 0.15–0.17 µs/step with the optional C kernel | `tools/bench_native.py` |
| Default compile (smoothing on), 1,000 parts, 5 M steps (Review 2, §7) | 50.96 s | **2.93 s** serial, **0.96 s** with 4 planning processes | `tools/bench/bench_throughput.py --parts 1000` |
| Memory of a compile with the C kernel (Review 2, §7) | 206 B/step peak, +437 MB RSS per M steps | **26 B/step**, **+26 MB** per M steps | `bench_throughput.py --mem` |
| 800×600 dither engraving compile, 54.6 M steps (Review 2, §7) | 119 s, +8.2 GB RSS | **35 s, +1.2 GB** | `bench_throughput.py --raster 800` |
| `build_toolpath`, 8,000 parts, sort 6 (ring sort, Review 2, §7) | 14.7 s | **1.42 s** | `bench_throughput.py --parts 8000 --order 6` |

## 2. Ordering kernels (P1-4, architecture.md §4.3 item 3)

Where the time went (review profile): `local_optimal_elems` O(n²) nearest neighbour (25 M
`approx_hypot` calls at 5,000 parts), `build_tree` O(n²) containment scan, `_cheapest_pos`
O(n) per insertion, `containers_last_faithful` O(n²).

What changed, all bit-identical:

* `parity_ports/cadmodule/ordering_fast.py`: drop-ins for `local_optimal_elems`,
  `containers_last_faithful` and `import_sort`, which `ORIGINAL` now points at. numpy, a
  uniform grid and an STRtree are used only as filters that provably keep every candidate the
  reference could pick. The decision is made by the reference's own scalar functions, in its
  order, with its first-match tie-breaks. The proof for each filter is in the docstrings. The
  statement-level references stay, reachable as the `"original-reference"` profile.
* `cam/ordering.build_tree`: STRtree candidates, exact tests unchanged (`build_tree_reference`).
* `geom/curves.flatten_points` / `flatten_cached`: `Contour.points` bit for bit, with each arc's
  angle, sweep and radius computed once, plus a cache keyed on segment values.
* `cam/toolpath.prepare_cached`: an unchanged contour reuses its prepared form across builds.
  The checks are the same object, segment values, extra entries, a craft snapshot, depth,
  CamParams, the kerf default and the profile.
* `build_toolpath` pauses the cyclic GC (about 20 % of a build in a large session).

Verification: hypothesis comparisons against the references (`tests/test_ordering_fast.py`:
random elements on grids and floats, ties, duplicates, zero-size boxes; 1,500–5,000-element
cases that run the grid rings, the brute-force fallback and the gap filters); the 660 order
goldens; the parity run; `tests/test_prepare_cache.py` (cached builds equal uncached ones,
also after in-place edits).

| Kernel (random input) | Reference | Fast |
|---|---|---|
| `local_optimal_elems`, 5,000 open | 7.9 s | 0.07 s |
| `local_optimal_elems`, 3,000 open, 3 clusters | 2.7 s | 0.10 s |
| `local_optimal_elems`, 3,000 closed | 1.1 s | 0.22 s |
| `build_tree`, 4,000 contours (2,000 parts) | 0.71 s | 0.10 s (0.075 s with a warm flatten cache) |

Not done: ring sort (modes 6/7, still O(n²) in a port), band sort (1–4) and the mode-20
local search; a background, debounced toolpath (needs the UI / `PlannedJob` worker of the
Services track; the toolpath is now under 300 ms, so it is no longer urgent).

## 3. Raster engraving object (P2-3)

`cam/raster_engraving.py`: one `RasterEngraving` (numpy arrays of runs: x0, x1, row, row_y,
power) carried by a single Part whose contour is a frame. It has its own toolpath emitter,
whose MotionItems are exactly those of today's per-segment Parts under sort 0, and its own
canvas item (one image). `expand_document` gives today's Parts for `.chf` save (byte-identical).

| 800×600 dither, width 100 mm, rho 10 | Per-segment Parts | Raster object |
|---|---|---|
| `canvas.set_document` | 6.9 s, 553,884 scene items | 47 ms, 7 items |
| `build_toolpath` | 447 s | 0.91 s (1.48 M items) |
| render | 2.9 s, then 1.7 s | 3.7 ms |
| undo snapshot (`deepcopy`) | 8.9 s | 0.2 ms |
| document memory | 234 MB | 4.2 MB |
| generation | 1.5–1.9 s + `parse_gcode` | 31–36 ms |

Identity: `compile_job(...).wire`, `.frames()`, markers and cut blocks equal for the
none / halftone / dither modes (`tests/test_raster_engraving.py`; `MLASER_SLOW=1` for
200×150 px; `bench_raster.py --compile 5000`: 1,533,462 items equal).

Canvas level of detail: above 3,000 contours no per-contour markers, labels, arrows or boxes,
and contours smaller than 3 px are drawn as a point. Unchanged contours reuse their display
path (`_contour_path` cache). The per-segment 185k-part document then opens its canvas in
2.6 s instead of 6.9 s.

**JumpAddTime settle.** It stays 200 one-period dwell steps on the wire. A single dwell item
(`OP_3002` / `OP_WAIT`) would be a different FIFO item and would also move the frame
boundaries, because each step is its own frame-size-test group. So the wire cannot be
identical. Instead, the packed wire (§4) stores those 200 identical steps as one item with a
count of 200. In a 5,000-run scan job they are 68 % of all steps.

## 4. Packed step arrays (P2-1)

`nc/packed.py`: `compile_items` collects the wire into run-length packed numpy arrays (words,
run offsets, counts, group-start flags). `Job.wire` is a `PackedWire`, an immutable sequence
of the same lists of ints. `Job.frames()` / `iter_frames()` build frames directly from the
arrays, identical to `build_fifo_frames` (hypothesis test plus the goldens, which hash
`Job.wire` and `Job.frames()`). `Job.to_shared()` / `Job.from_shared()` move the arrays through
`multiprocessing.shared_memory`.

| 300-part job (904,525 steps, 905 s of replay) | Plain lists | Packed |
|---|---|---|
| retained wire memory | 199 MB (229 B/item) | 17.3 MB (20 B/item) |
| per hour of job | 792 MB | 69 MB |
| peak during compile | 207 MB | 34 MB |
| `frames()` | 0.70 s | 0.17 s |
| hand-off to a child | pickle 150 ms, 18.8 MB | shared memory 35 ms (0.8 MB pickled metadata, 11.3 MB block) |
| compile time | 3.8–3.9 s | 4.0 s (+4 %) |

The streamer integration (shared memory to the supervised child) is a merge note: the
streamer belongs to the Safety track.

## 5. Native kernel (P2-2): the default

`mlaser/_native/_emitter.c` (CPython C API, MIT), an optional extension of the mlaser
distribution compiled by `uv sync` / `pip install` when a C compiler is present: the whole
`emit_samples` loop with identical IEEE operations (`-O2 -ffp-contract=off -fno-fast-math`,
MSVC `/O2 /fp:precise`, no `-march`; an objdump check refuses FMA instructions) and CPython's
own `math.hypot` called back. **The default profile `"original"` uses it** after a built-in
self-check against the Python port at first use (about 25 ms); without a compiler, or if the
check fails, the Python port runs with a logged warning. `MLASER_NATIVE=0` forces the port,
`=1` requires the kernel. `tools/native_parity.py` (port vs the default on the kernel): 326
golden jobs (26 samples + 300 random), 3,045,237 steps, 0 differences. Details in
[NATIVE_KERNEL.md](NATIVE_KERNEL.md).

**Smoothing off vs the default profile.** The default machine smooths corners before planning
(`Machine(smooth=True)`, CContoutSmooth), and that is what the application compiles. The first
figures of this section were measured with `smooth=False`, which isolates the emitter but
understates a real compile about 3x (Review 2, performance §2.1: about 70 % of a default
compile is the smoother). Both are given below; `tools/bench_native.py` measures the first,
`tools/bench_native.py --smooth` the second.

Re-measured 2026-10-19 on this branch: CPython 3.12.14, gcc 13.3, x86-64 container on a
shared 64-core host with a load average of about 250 (so absolute times are pessimistic; the
ratios held over repeated runs), `python tools/bench_native.py [--smooth] --repeat 3`, best of
3, `MLASER_NATIVE=1`; `identical=True` for every pair (Python port and kernel give the same
wire):

| Job | machine | `compile_job`, Python port (`MLASER_NATIVE=0`) | C kernel (default) | `compile_items` per step, Python / kernel |
|---|---|---|---|---|
| sample_v9.chf (142,155 steps) | **smoothing off** (`smooth=False`) | 0.76 s | 0.40 s (1.9x) | 5.19 / 2.71 µs/step |
| sample_v9.chf (148,619 steps) | **default profile** (smoothing on) | 1.99 s | 1.64 s (1.2x) | 13.08 / 10.74 µs/step |
| 300 parts (904,525 steps) | **smoothing off** | 3.86 s | 1.96 s (2.0x) | 4.25 / 2.01 µs/step |
| 300 parts (918,325 steps) | **default profile** | 6.92 s | 5.06 s (1.4x) | 7.44 / 5.31 µs/step |

The emitter alone (every `emit_samples` call of the job recorded and replayed): 3.0-3.9 µs/step
on the Python port, 0.16-0.20 µs/step on the kernel (16-19x), with either machine. So the
kernel's gain on a default compile is smaller (1.2-1.4x instead of about 2x): the smoother
(curve table, `crtmath`, `smooth.py`) and the duplicate planning of `estimate_time` dominate
it, not the emitter (ROADMAP Review 2, Wave B: native smoother and curve tables).

The earlier figures (2026-10-19, CPython 3.13, `smooth=False`, best of 5, load about 233) were:

| Default emitter, **smoothing off** | `compile_job` (document -> job) | first compile in a fresh process (incl. selection + self-check) |
|---|---|---|
| Python port (`MLASER_NATIVE=0`) | 0.64 s | 0.79 s |
| C kernel (default) | 0.35 s (1.8x faster) | 0.55 s |

| Job, **smoothing off** | compile_items, Python | native | emitter alone, Python | native |
|---|---|---|---|---|
| sample_v9.chf (142,155 steps) | 0.62 s (4.34 µs/step) | 0.33 s (2.28 µs/step) | 2.37 µs/step | 0.15 µs/step |
| 300 parts (904,525 steps) | 3.26 s (3.60 µs/step) | 1.65 s (1.82 µs/step) | 2.71 µs/step | 0.18 µs/step |

What remains of `compile_items` with the kernel is planning, `geom.at` sampling and the process
items, not the emitter.

## 6. Libraries and options

| Item | Measurement |
|---|---|
| SVG import (svgelements) | 167 icon SVGs: 0.15 s (old parser 0.09 s), true Arcs instead of flattened ones |
| Kerf pyclipper fallback (opt-in) | 67 golden fallback contours: 2,834 segments (1,836 Arcs) vs 26,411 lines from shapely; jobs_random 18.9 s vs 21.3 s |
| Text (fontTools + uharfbuzz) | "Mlaser" 0.9 ms, a CJK string 3.1 ms, Arabic 1.3 ms; no QApplication (Qt: 91 ms start + 8.9 ms) |
| Modern DXF import | same or faster than faithful on the 11 samples (spline_ellipse 9 vs 16 ms, text_points 102 vs 190 ms) |
| OR-Tools mode 20 (opt-in) | 117 contours: rapid 3,067 vs 3,126 mm (−1.9 %); 698 / 2,290 contours: no gain in 2 s (seeded with the built-in order, never worse; about 2× the time) |
| spyrrow nesting (opt-in) | last sheet 10–14 % shorter; full-sheet utilisation 62 % vs 66 % (raster); uses the whole time limit (30 s) vs 0.3–0.7 s |
| Ruckig planner (opt-in, not hardware-validated) | job time −5 % over 13 samples + 300 random jobs; corner-heavy paths −15 to −35 %; limits met exactly |
| Linux PyInstaller bundle | 357 MB / 139 MB tar.gz with all extras (slim 281 / 111 MB); clean build 49 s |

## Part list (Wave 3, run track)

`ui/panels.PartListPanel` caches each part's bounding box by `uid`, validated by the identity of
its segment objects (edits replace them), and reuses its tree items.  2,000 parts (a square and a
circle each), offscreen, machine load ~230 on 64 cores:

| | before | after |
|---|---|---|
| first fill | 298 ms | 248 ms |
| refresh after an edit of 10 parts | 306 ms | **22 ms** |
| refresh, nothing changed | 298 ms | **21 ms** |

The 250 ms coalescing timer in `MainWindow._document_changed` (shell) can go: the refresh is now
cheap enough to run inside each edit.  Benchmark: `tools/bench/bench_partlist.py`.


## 7. Compile throughput, Review 2 (throughput track)

Everything here is the **default profile with smoothing on** (`Machine()`, the default for
contours; the earlier sections measured `smooth=False`), on the review's drawing
(`tools/bench/bench_throughput.py`: N parts, half a 24 mm square with a 10 mm hole, half a
24-point star with two 4 mm holes, 2 mm line lead-ins, four layers, `CamParams()`).  Times are
`compile_items` (the toolpath is built once beforehand), best of 2-3, CPU time of the compiling
process (the wall time where a pool is involved); CPython 3.12, gcc 13.3, a shared 64-core
x86-64 host at load average 230-260, so absolute times are pessimistic (3-5x a quiet desktop):
compare within a column.  "Before" is commit f7a1ea0 (the start of the track) measured in the
same session.  Every row's output is bit-identical to the others: the same fingerprint of wire,
frames, markers, cut blocks, steps, carry, laser length and estimate, printed by the bench), the
goldens `--check --strict` with `MLASER_NATIVE=1` and `=0`, and `tools/native_parity.py`
(326 jobs, 0 differences, also with `--workers 4`).

### Compile time, one step at a time (C kernel, 200 parts, 996 249 steps)

| state | compile_items | us/step |
|---|---|---|
| before (f7a1ea0) | 10.14 s | 10.2 |
| 1. no duplicate estimate pass (P1-4) | 7.05 s | 7.1 |
| 2. packed arrays from the kernel, rapids in one call (P1-6) | 6.28 s | 6.3 |
| 3. crtmath, curve tables, path / plan sampling in C (P1-5) | 0.63 s | 0.63 |
| 4. + rapid samples in C and memoised per length | 0.58 s | 0.58 |
| 4. + a pool of 4 planning processes (P2-5), warm | 0.18 s wall | 0.18 |

### Before / after, both sizes, both implementations

| | 200 parts (1.0 M steps) | 1 000 parts (5.0 M steps) |
|---|---|---|
| C kernel, before | 10.14 s | 50.96 s |
| C kernel, after, serial | 0.58 s (17x) | 2.93 s (17x) |
| C kernel, after, 4 workers | 0.18 s wall (56x) | 0.96 s wall (53x) |
| C kernel, after, auto (8 workers from 600 chains) | 0.58 s (500 chains: serial) | 0.98 s wall |
| Python port (`MLASER_NATIVE=0`), before | 12.54 s | 62.86 s |
| Python port, after, serial | 9.40 s | 47.56 s |
| Python port, after, 8 workers | 3.82 s wall | 18.55 s wall |

The Python port's gain is the estimate pass (P1-4) and the rapid memo only; it stays the
reference.  With the kernel, what is left of a serial compile is the smoother's Python
structure (primitives, corners, blend control points, nodes), the velocity planner, the
estimate chains that differ from the compile chains (the lead-in joint), the process items and
the item loop; in parallel mode the sequential tail (emitter, rapids, process items, packing)
is about 0.9 s CPU per 5 M steps, which bounds the speed-up (4 and 8 workers give the same).
The first parallel compile of a process also starts the workers (about +0.3 s here).

### Memory per step (one compile, 200 parts; tracemalloc and RSS growth)

| | retained | peak (traced) | RSS growth |
|---|---|---|---|
| C kernel, before | 22.4 B/step | 206 B/step | +437 MB per million steps |
| C kernel, after | 22.4 B/step | **26 B/step** | **+26 MB per million steps** |
| Python port, before | 22.4 B/step | 47 B/step | +32 MB per million steps |
| Python port, after | 22.4 B/step | 48 B/step | +40 MB per million steps |

1 000 parts with the kernel: peak 25 B/step, +26 MB per million steps.  The steps go straight
into the packed arrays (the C `Sink`, uint32 words, no Python object per step).

### The 800 x 600 dither engraving (54.6 M steps, `--raster 800`, compile_job)

| | compile_job | RSS growth | max RSS |
|---|---|---|---|
| C kernel, before | 119.1 s | +8 240 MB | 11 897 MB |
| C kernel, after | 35.0 s | +1 229 MB | 1 323 MB |
| Python port, after | 159.2 s | +1 817 MB | 2 476 MB |

The +1.2 GB retained after the compile is the Job: 381 MB of packed wire (10.2 M runs for 56.7 M
items) and the 1.48 M toolpath items it keeps.

### Ring sort (sort modes 6 / 7)

| | 1 000 parts | 2 000 | 4 000 | 8 000 |
|---|---|---|---|---|
| `build_toolpath(sort_mode=6)`, before | 0.54 s | 1.44 s | 4.34 s | 14.70 s |
| after | 0.14 s | 0.30 s | 0.66 s | 1.42 s |
| `ring_sort` alone (2 500 / 5 000 / 10 000 nodes), before -> after | 1.16 s -> 0.04 s | 4.4 s -> 0.13 s | 18 s -> 0.5 s | |

Same order (order fingerprint, the order goldens with the kernel on and off, hypothesis against
the reference).  It is still O(n^2): the reference's spiral scan is sequential (about 100
direction changes per pick at 2 500 nodes, each depending on the last), so an exact
reproduction cannot skip it; the C scan only makes each candidate cheap (NATIVE_KERNEL.md,
"Ring sort").

### Parallel compile (mlaser.nc.parallel)

`MLASER_COMPILE_WORKERS` = `0` / `off`, `N`, or `auto` (default: `min(8, cpus - 1)` workers for
jobs of at least 600 cut chains, `MLASER_COMPILE_MIN_CHAINS`); `compile_items(workers=)`,
`compile_job(workers=)`, `parallel.configure()`.  Crossover measured here: a warm pool wins
from about 250 chains (100 parts: 0.29 s serial, 0.10 s with 8 workers), a cold one, which
starts the workers, from about 600.  Workers are spawned (like the streamer process), kept for
the process lifetime, run at nice 10 / below-normal priority, never start inside a
multiprocessing child other than the JobService's plan process (§8), and auto mode stays serial when the main script has no `__main__`
guard.  A broken pool finishes the compile serially (with a warning).
