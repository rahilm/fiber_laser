# Testing

## Suites

```bash
uv run pytest -q -n auto --dist loadgroup   # default run in parallel (what CI runs), see below
uv run pytest -q                 # default run, serial: unit, ui, sim and safety tests
uv run pytest -q -m unit         # one suite: unit / ui / sim / safety
uv run pytest -q -m golden       # frozen artefacts (about 75 s on a loaded host), Python 3.12 only
uv run pytest -q -m parity       # Wine parity suite (needs the external harness)
uv run pytest -q -m "safety or parity"
```

Markers are assigned by `tests/conftest.py` from the directory and module name, so a new test
file needs no decorator: `parity` (tests/parity/), `golden` (tests/golden/), `safety`
(tests/safety/), `ui` (modules listed in `UI_MODULES`, Qt offscreen), `sim` (modules that start
the UDP card simulator), `unit` (everything else). Qt runs with `QT_QPA_PLATFORM=offscreen`.

The responsive-layout harness (`tests/test_responsive_layout.py`, a `ui` module) sweeps the real
window over sizes, dock widths, a larger font and 2x scale: see [Responsive layout](RESPONSIVE.md).

## Goldens

`tools/make_goldens.py` writes the frozen artefacts in `tests/golden/` (`.chf` round trips,
random and sample jobs, motion, orders). `tools/make_goldens.py --check` must stay bit
identical on CPython 3.12 after every change outside a declared new `ParityProfile`; regenerate
only after a reviewed behaviour change. 3.13 is checked as information
(`MLASER_GOLDEN_ANY_PYTHON=1`).

The default profile runs the emitter loop on the C kernel when it is built
([NATIVE_KERNEL.md](NATIVE_KERNEL.md)); the goldens pin both implementations, so CI runs them
twice, `MLASER_NATIVE=1 pytest -m golden` and `MLASER_NATIVE=0 pytest -m golden`, on Linux,
Windows and macOS, plus `tools/native_parity.py` (kernel vs Python port on all golden jobs,
exact). The CI test job sets `MLASER_NATIVE=1`, so a platform where the kernel did not build
or fails its self-check fails instead of silently testing the Python port.

## Goldens on Windows and macOS

The goldens are bit-exact on the reference platform: Linux x86-64, glibc, CPython 3.12. CI also
runs them on Windows and macOS (3.12, kernel on and off), where they are **reported, not
fatal**: the platform's C math library returns different last bits for some `sin`, `cos`, ...
results, and the Python port passes them on. Everything else is the same on every OS:
`tools/native_parity.py` (kernel = Python port on that platform, all 326 golden jobs, fatal
everywhere) and the default suite.

Measured on the CI runners (2026-10-19, CPython 3.12.10; Windows Server 2025 x64 UCRT, macOS 26.6
arm64 on Apple M2 Pro VMs) with `tools/golden_libm.py record` there and `replay` on the
reference platform. "Differ" counts the distinct calls (arguments) the golden computations
make whose result is not bit-identical to glibc's:

| function | distinct calls | differ on Windows | differ on macOS | size |
|---|---:|---:|---:|---|
| `math.sin` | ~1 178 000 | 33 836 | 40 469 | 1 ulp |
| `math.cos` | ~1 193 000 | 35 565 | 48 124 | 1 ulp |
| `math.tan` | 568 | 41 | 227 | 1 ulp (macOS: some 2 ulp) |
| `math.acos` | ~15 780 | 179 | 1 064 | 1 ulp |
| `math.atan2` | ~15 760 | 35 | 53 | 1 ulp |
| `math.pow` (the planner's cube root) | ~2 975 | 1 | 4 | 1 ulp |
| `a ** b` (C `pow`, e.g. `v ** 3` in the spline radius) | ~37 000 | 37 | 53 | 1 ulp |
| `math.atan`, `math.fmod` | 14 | 0 | 0 | |
| `math.hypot`, `math.dist`, `math.sqrt` (CPython's own / correctly rounded) | ~1 360 000 | 0 | 0 | |

Golden records that differ from the frozen ones there (`make_goldens.py --check`; orders and chf
are identical everywhere):

| group | Windows | macOS |
|---|---|---|
| jobs_samples (26 cases) | 1: `dxf/r12_polyline#bands_nosmooth_ppm1000` (wire, frames, approx) | 2: the same and `dxf/inserts#bands_nosmooth_ppm1000` |
| jobs_random (300) | 1: `seed196` (wire, frames, approx) | 1: `seed185` (wire, frames, items, steps, cut_blocks) |
| motion (189) | 94: 89 planner cases (xy of the sampled path, 13 also samples / point_vel), 5 smoothing node sets | 105: 91 planner cases (29 also samples / point_vel), 14 node sets |

So a 1-ulp `sin` can move a sampled point across a step quantisation boundary and change a wire
item: the goldens cannot be bit-exact across C libraries, and the reference stays Linux.

**Proof that nothing else differs.** `tools/golden_libm.py replay` recomputes every golden group
on the reference platform with the recorded platform's results of the functions above
substituted (numpy, shapely / GEOS, CPython's hypot / dist / sqrt computed locally): every
record of the Windows run and of the macOS run is reproduced bit for bit, with no call missing
from the recordings. The investigation found one difference that was **not** libm and fixed it:
`numpy.linalg.solve` in the short-line spline fit (`smooth._fit_polyline`) returned different
bits on macOS (numpy's arm64 wheels use Apple Accelerate, the others OpenBLAS; 17 records); the
fit now uses its own Gaussian elimination in Python floats (`smooth._solve`).

To repeat the measurement: run CI with workflow_dispatch (`gh workflow run ci.yml --ref <branch>`;
the "Golden libm recording" step then uploads `golden-libm-Windows` / `golden-libm-macOS`), and
on the reference platform `python tools/golden_libm.py replay golden-libm-macOS.json.gz`
(`record --trace jobs_random/seedNNN` keeps the ordered calls of chosen cases; `replay` then
prints where a replay first diverges).

## Test data and the original's files

The repository contains no file from the original's install (docs/ASSETS.md). The test data is
our own: `tests/data/chf/*.chf` are written by `tools/make_test_chf.py` from mlaser's model (a v9
drawing with a scan graph, groups, crafts, splines, arcs and text, and a v5 drawing; `--check`
verifies they are current), `tests/data/appdir/` holds hand-written state files in the
original's formats (same layout as the install folder), `tests/data/lang_fixture/` a synthetic
language folder.

Tests that check the ORIGINAL program's own files byte for byte (its autosave drawings, state
files, calibration table, firmware, G-code sample) read them from a local copy of the install:
`MLASER_VENDOR_SAMPLES=<install folder>` (default: the extracted download used for the
reconstruction, if it exists; empty disables them). Without it they skip, like the parity
tests. `tests/vendor_samples.py` has the helpers (`requires("File/autosave.chf")`,
`vendor_file(rel)`).

## Contract for every commit

* `tools/make_goldens.py --check` bit identical (3.12; with the C kernel, and with
  `MLASER_NATIVE=0` after a change to the emitter loop or the kernel);
* after a change to the emitter loop or `mlaser/_native`: `tools/native_parity.py` 0 differences;
* the default suite green;
* `pytest -m "safety or parity"` green (a timing test may flake on a loaded host: rerun once);
* `tools/command_coverage.py`: no unimplemented, raising or duplicate command;
* `lint-imports` (layer rules, [Architecture](ARCHITECTURE.md)) and `mypy` green;
* `uvx ruff@0.17.1 check` on the changed files;
* `tests/test_docs_links.py`: documentation links resolve.

## Writing tests

* Domain logic is Qt-free and tested as `unit`; widget tests build the window through
  `tests/wiring.py`, the application's real wiring.
* Safety behaviour is tested against the simulator in pessimistic mode
  (`CardSimulator(pessimistic=True)`, `tests/safety/safety_helpers.py`).
* Randomised equivalence tests (hypothesis) compare a new implementation with the reference one
  before the reference is retired.
* Qt object lifetimes: give every widget handed to a Qt setter a Qt parent (PySide does not
  always transfer ownership; `QMessageBox.setCheckBox` does not, and the freed checkbox crashed
  the next re-layout of the box), and delete dialogs after use instead of leaving them as
  children of the window. A crash only on one OS is usually such a lifetime bug: CI's macOS jobs
  print faulthandler's stacks and the command trace of the coverage test after a crash, and
  `lldb` on the runner (SIP is disabled there) gives the native backtrace.
* Windows a test builds do not outlive it: `tests/wiring.py`'s `dispose(w)` closes a window as
  the application does and deletes it now, and `tests/conftest.py` closes and deletes every
  parentless window an earlier module left (only parentless ones: a menu or a combo box's
  popup deleted on its own leaves its owner a dangling pointer, a segfault). Without that a CI
  worker kept hundreds of windows alive, and everything that reaches every widget slowed down
  with them: a style change (`test_native_style_per_platform`, 31 min on macOS CI), the display
  units signal (6 min), every `processEvents`, and their timers and compile threads starved
  the timing tests.
* Screenshots of new screens: offscreen, at 1280×800 and at 150 % of 1920×1080, light and dark.


## Parallel runs (pytest-xdist)

CI runs the default suite with `pytest -n auto --dist loadgroup` (pytest-xdist, in the `dev`
dependency group). `tests/conftest.py` puts every test marked `safety` or `sim` (and a single
test marked `timing`) into a **timing group**. With `--dist loadgroup` all tests of one group run
on one worker, one after another in one process, exactly as in a serial run, so the real-time
bounds below keep their meaning. The other tests (unit, ui) spread over the remaining workers.
Every marker of the default run is still collected and run; the golden and parity steps are
unchanged. Without `-n` (or without pytest-xdist installed) nothing changes.

The groups (`TIMING_GROUP`, `TIMING_GROUPS` in `tests/conftest.py`):

| group | what | why it can run next to the others |
|---|---|---|
| `timing` | `tests/safety/` (and `tests/test_web_hardening.py`, every module not named elsewhere) | |
| `timing-sim` | the card simulator without a window: streaming, jobs, procedures, practice lessons, devices | own `CardSimulator` per test on an ephemeral port (`port=0`), own config directory |
| `timing-gui` | the window: GUI-thread latency bounds, workspaces on the simulator, whole-app subprocesses | the same; no file or port is shared with the other groups |

`tests/safety/test_ux_audit_safety.py` has its own group as well (`timing_ux_audit_safety`, since
2026-11-08). So up to four workers carry timing tests, each group serial in itself. Two timing
tests of different groups can now run at the same time in different processes; a CI runner
already ran every timing test next to busy ui workers on all its other CPUs, so the load a
timing test sees changes little. A bound that fails only since the split is a real finding about
that bound's margin: report it, do not move the test back silently and never loosen the bound.

**Out of every group** (`UNTIMED_MODULES`): modules marked `safety`/`sim` whose tests have no
wall-clock measurement or bound, no sleep that orders events, no card link (`connect_simulator`,
a connected `UiController`, `make_ctl`) and no streaming, and no `flaky` rerun:
`tests/safety/test_policy.py`, `test_remedy_texts.py`, `test_plugin_trust.py`,
`test_touch_targets_mode_switch.py`, `tests/test_ui.py` (the demo controller on an injected
clock). Each was read; `tests/test_timing_groups.py` checks their source for those signs, so a
timing test added to one of them fails there until it is marked `@pytest.mark.timing` (or the
module leaves the set). `tests/test_close_save.py` is a `ui` module with two GUI-latency tests
marked `timing`. A module that uses a simulator over a raw transport with sub-second reply
timeouts (`test_controller_startup.py`, `test_devices_*.py`, `test_card_scaling.py`) stays in
`timing-sim`: moving it out is a candidate for after a few CI runs, not done blind.

`tests/test_xdist_groups.py` checks the grouping (the conftest hook must run `tryfirst`, before
xdist's worker turns the group into the `@timing` node id suffix; each group on one worker; an
untimed module spread). When you add a module that starts a simulator, add it to `SIM_MODULES`
(and to `timing-sim` or `timing-gui` if it belongs there; otherwise it is in `timing`).

### Measured

2026-11-10, this repository's development host (64 cores, shared, load average 1-12 during the
runs; CPython 3.12.14), the 53 files that held timing tests (`tests/safety/*`, the sim modules,
`test_close_save.py`, `test_responsive_layout.py`, ...), `pytest -n N --dist loadgroup`:

| run | before (one `timing` group) | after |
|---|---|---|
| `-n 4` | 506 s (654 tests) | 238 s (698 tests: the sweeps parametrised) |
| `-n 8` | 510 s | 156 s |
| `tests/test_responsive_layout.py` alone, `-n 8` | 106 s (19 tests) | 54 s (63 tests) |
| `tests/test_practice.py`, serial | 130 s (review, loaded host) | 57 s |

Before, the wall time was the serial chain of the one group and did not fall with more workers.
**Not measured: the CI runners.** Windows and macOS runners have 3-4 vCPUs, slower single cores
and late timers (macOS); the numbers there are expected to fall in proportion but must be read
from the CI logs (`--durations=15`) after the change. Older numbers (2026-10-19, 1,230 tests):
serial 320 s, `-n auto` 163 s, the timing group alone 155 s.

### What made it shorter

* **Parametrised layout sweeps** (`tests/test_responsive_layout.py`): the width × height sweep is
  one case per workspace and height row (`SWEEP_WORKSPACES` × the minimum, 800, 875 and 1169 px;
  a workspace not named there, e.g. a plugin's, is swept by the `other` case), the dock sweep one
  per size and workspace, the live resize one per workspace, the sample, imperial and selection
  runs one per size or theme. Same sizes, same assertions; the window is the module fixture,
  built once per worker. Each case starts its width grid at its own workspace's minimum (before,
  the grid started at whatever minimum the previous test had left). The reports under
  `MLASER_LAYOUT_SHOTS` are one file per case (`sweep_<ws>_<row>.json`, ...).
* **Practice lessons on a smaller sheet** (`tests/test_practice.py`): the lessons that cut the
  sheet in real time (simulator at 1x, see below) cut it scaled by `SHEET_SCALE` (0.3): the same
  parts, contours, order and speed, 14 s instead of 35 s per lesson.
  `tests/test_practice_sheet.py` compiles the real sheet and checks the scaled one is the same
  sheet. Mutation-checked: inverting the lesson's "finished ok", "breakpoint" or e-stop release
  checks, or making the controller's Hold a no-op, still fails the lesson tests.
* The split of the timing group (above).

### Not done (yet)

* `tests/test_command_feedback.py`'s registry walk (one test of 35-75 s here, 135-178 s on CI) is
  NOT split: run in four parts on fresh windows, `sort.grid` and `nest.run` came out silent,
  that is the walk's verdict depends on the commands run before on the same window. That needs a
  look at those two commands before the walk can be split.
* `tests/ui/test_one_home.py`'s module fixture (30 s set-up) and `tests/test_close_save.py`'s two
  2,700-part GUI-latency tests (13 s each, each builds its own large window) are unchanged.
* **A seam under the job runner** (architecture review A11). `mlaser/controller/parts/jobs.py`
  reads `time.monotonic()` directly and starts its worker threads itself, and the streamer talks
  UDP to the simulator in real time, so a job test costs the job's real duration. No small
  additive parameter fixes that: the clock would have to reach the streamer, the supervisor's
  deadlines and the simulator together. The seam to add with the job-lifecycle refactor (A1): a
  `clock` (monotonic, sleep) passed to `UiController` and on to the job runner, streamer and
  supervisor (default `time`); an in-memory transport that calls `CardSimulator.handle(bytes)`
  directly; and stepping the simulator with `CardSimulator.advance(dt)` instead of its thread.
  The threaded tests stay as the acceptance set; the phase logic gets fast unit tests.

With xdist, `MLASER_FAULT_LOG=<file>` writes one file per worker (`<file>.gw0`, ...); CI's
macOS crash step prints them all.

## Timing-sensitive tests

The safety suite asserts real-time bounds (stop latency, link-loss detection, motion after a loss).
Two rules keep those assertions meaningful on busy CI or development hosts:

- **The in-process simulator gets its own link deadline.** `connect_simulator()` streams with
  `SIM_LINK_DEADLINE_MS` (1 s) because the simulated card shares the Python process (and GIL)
  with the streamer. A real card always gets the configured deadline (150 ms default), and a test
  proves the simulator value never leaks to a real connection.
- **Tests of the real 150 ms deadline may rerun once.** They are marked `@pytest.mark.flaky(reruns=1)`
  (pytest-rerunfailures). A test that fails twice is a real failure. Bounds are never loosened to
  make a test pass. (The link-loss test `[False]` failed twice on the CI runners: the streamer
  prepared a 28 000-item job inside the 150 ms after the last poll; `JobStreamer.prepare` now does
  it before the streaming deadline applies, docs/SAFETY.md §7.)
- **Simulator speed leaves real-time margin.** A test that streams to the in-process simulator at
  `time_scale` k has 1.2 s / k of real time in the FIFO; at 5-50x a stall of the test process
  (CI runners) let the card run dry. Streaming tests use 1-2x (10x for the synthetic captures of
  `mlaser.parity.synth`).
- **Garbage-collector pauses.** By the middle of the suite the test process holds ~2 million
  objects of earlier tests, and a full collection held the GIL for 0.4-0.6 s (more on the CI
  runners) inside timing tests. `tests/conftest.py` collects and then freezes the heap
  (`gc.freeze`) at each new test module (largest pause in a full run then ~0.1 s, measured
  2026-10-19), and the streamers freeze it while a job streams (`stream.quiet_gc`, also in the
  application).
- **Platform clocks.** On Windows before Python 3.13 `time.monotonic()` / `time.time()` tick in
  15.6 ms steps: measure short intervals with `time.perf_counter()` and select logged requests by
  position, not by timestamp. The macOS runners are VMs whose timers fire late (a 2 ms sleep
  takes 8-25 ms, a 16-50 ms wait 50-150 ms; measured in CI 2026-10-19 and 2026-11-08): the
  safety monitor ends each wait before its deadline (link tick, heartbeat edge) by the worst
  lateness it has seen and sleeps the rest in 2 ms slices, so an edge is never more than a short
  sleep's lateness late (`tests/safety/test_arming_and_preflight.py`
  `test_heartbeat_keeps_its_rate_with_late_timers`).
