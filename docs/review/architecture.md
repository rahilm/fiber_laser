# Architecture review: mlaser rebuild

Scope: `<workspace>/rebuild/` (131 modules, 44,452 lines in `mlaser/`; 9,471 lines of tests; 6,096 lines of docs) and the parity harness in `<workspace>/parity/`. This was a read-only review: no project file was edited.

Every number below comes from scripts run during this review:

| Directory | Scripts |
|---|---|
| `work/review/arch/` | `deps.py` (import graph), `rebind.py` (extension rebinding), `extra.py` (`extra` key census), `undo_bench.py` |
| `work/review/safety/` | `stop_latency.py`, `crash_test.py`, `crash_parent.py`, `restart_race.py`, `pause_test.py`, `stop_offline.py`, `softlimit.py` |
| `work/review/perf/` | `bench_compile.py`, `bench_smooth_fix.py`, `bench_large*.py`, `prof_*.py`, `bench_snap.py`, `bench_scan_canvas.py`, with `*.txt` outputs |
| `work/review/quality/` | `metrics.py` (ast size and complexity), `pytest1.txt`, `vulture80.txt`; a separate venv with ruff, vulture and mypy |

Safety results were measured against the rebuild's own simulator, never against real hardware.

---

## 0. Executive summary

The rebuild is broad: 246 of 246 ribbon commands are bound, parity scores 263 match / 14 differ, and `.chf` round-trips byte for byte. The domain code is mostly well documented with evidence; there are about 2,100 `FUN_`/address citations in the code.

The architecture, however, is what you get when 25 agents work under file-ownership rules:

- **Cyclic package graph.** Two strongly connected groups: {controller, nc, ui, devices, diag} and {cam, io, formats}.
- **The controller interface lives in the UI package**, and there are four overlapping controller abstractions.
- **The extension mechanism rewires the main window.** It rebinds 170 of 246 commands, silently overrides 10 core handlers and monkeypatches `set_controller` three times.
- **Too many duplicates:** 7 sorting paths, 8 or more flattening paths, 4 B-spline evaluators, 3 item-stream types, 3 representations of cool points, and about 9 configuration stores.
- **Undo deep-copies the whole document**, and 90% of that cost is round-trip blobs.

The most serious problems are safety, not structure. The happy paths work. **Every failure path is weak:**

- Disconnect or closing the window mid-job leaves the card cutting for about 5 s (527 mm) with PWM still on.
- Killing the UI leaves the streamer process feeding the whole job.
- Link loss takes about 5 s to detect and is not latched.
- The streamer re-starts the FIFO after stops it didn't issue.
- `--laser-off` is enforced only at compile time and is bypassed by 44 direct `mc` calls.
- There is no travel or soft-limit check of the job.

On performance:

- **Compile speed.** Compile runs at about 144x real time with smoothing off, but only **6.7x** with the default `Machine(smooth=True)`, because of one O(samples x pieces) bug in `ContourSmoother._locate`. Fixing it is a one-line change, gives 14x, and the output stays bit-identical.
- **Large documents.** These freeze the GUI because ordering (O(n²) pure Python) and the full toolpath rebuild run synchronously on the GUI thread: 3.6 s per edit at 2,000 parts, and **351 s** to open a 30,000-part engraving scan.

The project is not yet a maintainable open project:

- No git repository, licence or CI.
- Six imported packages are not declared in `pyproject.toml`.
- Proprietary icons and language files from the vendor are bundled.
- The docs are reverse-engineering notes plus an agent brief, and there is no user manual.
- The test suite has one flaky resume test and hard-codes paths outside the repository.

The fix is evolutionary, not a rewrite:

1. Put a **safety supervisor** under every card command.
2. Break the cycles by moving three modules.
3. Introduce a typed **machine API** and **plugin API**.
4. Make **one compiled `PlannedJob`** the unit that is previewed, cached and streamed.
5. Fence the bit-exact ports in a `parity` namespace behind interfaces, so modern features can evolve beside them.

---

## 1. Architecture

### 1.1 Layering and dependency direction

The intended direction is model → cam → motion → nc → controller → ui.

The actual import graph (`arch/deps.py`; counts are import statements per edge) has these upward or cross edges:

| Edge | Count | Where | Problem |
|---|---|---|---|
| controller → ui | 1 | `controller/ui_adapter.py:37` imports `ControllerFacade`, `MachineState`, `OUTPUTS` from `ui/facade.py` | The machine interface is defined in the UI package |
| diag → ui | 1 | `diag/batch.py:180` imports `ui.docops` | Domain code depends on the GUI package |
| nc → controller | 8 | `nc/compiler.py:49-50` (`frames`, `process`), `nc/stream.py:41-42`, `nc/machine.py:28`, `nc/offline.py`, `nc/resume.py` | Process items (`controller/process.py`, 1,040 lines: ManuItem, ProcessSequencer, ManuEncoder) are compile-time NC logic but live in controller |
| controller → nc | 10 | `ui_adapter.py` | Cycle with the row above |
| devices ↔ controller | 3 / 7 | `devices/codecs.py:43`, `follower.py`; `controller/devices.py` shim, `machine_ops` | Cycle |
| motion → cam | 1 | `motion/interp.py:23` imports `cam.items` | The planner depends on the CAM item types |
| cam ↔ io ↔ formats | 5 | `cam/geometry.py` re-exports `io.geom`; `io/dxf_import.py:955,1006` imports `cam.ordering`; `io/gcode.py` imports `cam.items`; `formats/chf.py:30` imports `io.geom`; `cam/ordering.py:1116` imports `formats.chf` | Cycle across three layers |

Strongly connected groups: **{controller, nc, ui, devices, diag}** and **{cam, io, formats}**. Only `model`, `cad`, `engrave`, `nest` and `report` are acyclic.

Three causes, each with a mechanical fix:

1. **The wire codec (`controller/frames.py`) is pure data, not I/O.** Move it to `mlaser/protocol/`.
2. **`controller/process.py` belongs to NC.** Move it to `nc/process.py`.
3. **The toolpath item types (`cam/items.py`) and the geometry helpers (`io/geom.py` plus `cam/geometry.py`) are shared vocabulary.** Move them to neutral `mlaser/toolpath/` and `mlaser/geom/` packages.

### 1.2 Business logic in UI code

- **`ui/docops.py`** (259 lines) holds geometry transforms, align, array, its own `sort_parts`, `apply_lead` and `apply_kerf`.
- **`ui/mainwindow.py`:**
  - `microjoint_auto` picks the longest contour and writes `craft.extra["micro_joint_len"]`.
  - It also does simulate/estimate orchestration, dispatches file formats by extension (`load_path`), and sets kerf defaults and paste offsets.
  - `_refresh` → `_update_toolpath` rebuilds the **whole toolpath synchronously after every edit**.
  - `file_save` writes in place, with no temp file and rename, and no error handling.
  - `_lazy()` is scaffolding left from "if the sibling package exists yet". It swallows every import error, so a broken `cam.toolpath` just shows "no toolpath".
- **The `ext_*.py` files** (6,185 lines) contain CAM handlers (ext_cam), nest application (ext_nest), and the production-count interlock and counters (ext_diag). Job start and end are detected by **regex over log text** (`diag/jobwatch.py`).
- **Shadowed duplicates:**
  - `docops.array` and `MainWindow.array_dialog` are overridden by ext_cam.
  - `sort.nearest/in_out/out_in/run` go through ext_nest, while `sort.left_right/...` still go through `MainWindow.sort`.
- **`ext_machine._apply_edge`** rotates the document in place without an undo snapshot.

### 1.3 Extension mechanism

Each `ext_*.register(window)` disconnects `QAction.triggered` and connects its own handler (`arch/rebind.py`):

- **170 of 246 commands are rebound**, and **10 of the 80 core handlers are silently overridden**: 5 `machine.home*` by ext_card, 4 `sort.*` by ext_nest, and `tool.array` by ext_cam.
- **Copy-paste helpers:** `_bind`/`_rebind` is copied 7 times, and the edit-transaction logic (snapshot, mutate, refresh) 6 times.
- **Monkeypatching:**
  - `set_controller` is wrapped by ext_card:157, ext_machine:586 and ext_diag:1711, so behaviour depends on load order.
  - `set_mode` is replaced by ext_craft:769 and `nudge` by ext_draw:656.
- **Private access from extensions:** they reach `window._snapshot`, `_refresh`, `_edit` and `_pendant*`, and there are 71 `getattr(window, ...)` duck-typing sites.
- **Errors are hidden:** `_load_extensions` drops `ImportError` without logging it (mainwindow.py:138).
- **Tests don't exercise the real wiring:** they build a bare `MainWindow()` and register extensions themselves.

It is a plugin mechanism in name only. Two problems follow: there is no contract a third party could code against, and nothing detects conflicts between plugins.

### 1.4 Controller facades

There are four abstractions:

- **`ControllerFacade`** (ABC in `ui/facade.py`, 24 methods).
- **`DemoController`**: moves along point chains with infinite acceleration.
- **`UiController`** (`controller/ui_adapter.py`, 1,206 lines, 41 public methods, about 45 private fields).
  - 17 of its public methods are **not** in the ABC, including `laser_press/release/test`, `laser_off`, `move_by`, `return_zero`, `startup_homing`, `resume_saved`, `place` and `signals`.
  - The UI probes for them at 26 `hasattr`/`getattr` sites, so these features silently vanish in demo mode.
- **`MachineOps`**: wraps `ctl` again and calls `ctl._log`.

There is also a legacy `FifoStreamer` (constant speed, 5000 Hz, duty 100) that is still public, and `JobStreamer` inherits from it.

`ui/__main__.py:44-48` falls back to `DemoController` if building `UiController` raises, **even with `--real`**, and only prints to stderr. `facade.make_controller` does the same and is otherwise dead code.

### 1.5 Duplicated concepts

| Concept | Implementations |
|---|---|
| Sorting (7) | `cam/ordering` (`order_nodes`, `import_sort`, `toolpath_sort`, `ring_sort`, `_grid_sort`, `set_manual_order`), `docops.sort_parts`, `MainWindow.sort`, `dxf_import._sort_text_contours`, flycut sorts, the `canvas.contour_order` fallback, ext_nest. The order is stored in `doc.extra["order"]` as index pairs (18 sites) |
| Smoothing (4, each with a different meaning) | `cam/cleanup.smooth_contour`, `motion/smooth` (CContoutSmooth port), `curvetable._smooth_radii`, `ext_cam.smooth` |
| Flattening (8 or more) | `Contour.points` (segment count `ceil(L/sqrt(tol)/2)` capped at 720, which does not bound chord error), `cam.geometry.polyline` (0.02), `flycut._flatten_lines` (0.01), `cleanup._samples`, kerf `points(0.01)`, `FifoStreamer._sample_*`, `DemoController._doc_points` (0.5), `interp.path_points`, `nc.emit_samples` |
| B-spline evaluation (4) | `io/geom`, `chf._bspline_points`, `corners.bspline_points`, `curvetable`. None is marked as the parity-exact one |
| Item streams (3 plus the wire format) | `cam.items` (often duck-typed with `type(m).__name__ == "Pierce"`), `process.ManuItem`, `Job.wire` (list of int lists) / `WireJob` |
| Frame grouping (2) | `Job.frames()` tests the size once per ManuItem group, as the original does (parity JN-08). `JobStreamer.run_job` tests after every item. On the 300-part job both give 9,240 frames, but the frames **differ**, so what is streamed is not what parity verified |
| Craft (3 places) | `Part.craft`, per-contour `Contour.extra["craft"]` (chf.py:698/745, manual.py:121-128, toolpath.py:392), and loose `craft.extra` keys (overcut, start_param, micro_joint_len/widths, cool_points, cool_positions, pwm_mode, lead_error) |
| Cool points (3) | `Craft.cool_point` (a point); `craft.extra["cool_points"]` (mixes arc lengths and xy tuples); `craft.extra["cool_positions"]` (used by .chf). `nest/shapes.py:113` transforms only the first, and chf never writes the second, so UI-placed cool points are probably lost on save (needs a test) |
| Configuration (about 9) | see §1.8 |

`extra` sprawl: **46 distinct literal keys, 195 `.extra` references, 18 keys shared across packages.** Job placement (`job_offset`, `rotated`, `rotate_*`) is written into `Document.extra` by controller and nc code.

### 1.6 Data model fitness

`model.py` (about 200 lines) is small and clear: Line/Arc segments, Contour, Part, Craft, LeadParams, LayerParams, Document. Its weaknesses:

- **No identity.** Parts and contours are addressed by index, so ordering, selection, undo and nesting all break on reorder.
- **No validation and no caches.**
  - `Part.bbox()` flattens every arc on every call: `Document.bbox` on 419,500 segments took 414 ms.
  - The same arcs are flattened **1.2 M times** while building one 2,000-part toolpath.
- **Source glyphs** sit in `Contour.extra["glyphs"]` next to a deep copy of the segments (`set_source_glyphs`). That doubles memory, and `source_glyphs()` compares every segment to detect staleness. It is better than nothing, but in practice it means the model has two geometry representations.
- **Round-trip state is stored in the live model.** Fidelity relies on `part.extra["chf"]` plus per-contour `chf` records and a `_chf_snapshot` that edits are diffed against (chf.py:700-745).
- **Undo deep-copies the whole Document** on every edit and load, and keeps 50 copies. Measured (`arch/undo_bench.py`):

| Case | Cost |
|---|---|
| `autosave.chf` (1 part, 839 contours), one deepcopy | 89.7 ms |
| Same document with all `extra` dicts cleared | **8.9 ms** |
| `autosave.chf` pickled size | 652 KB, of which about 533 KB is `extra["chf"]` blobs and 85 KB is geometry |
| 2,000 synthetic parts, one snapshot | 178 ms |
| 500 copies of the sample part, one deepcopy | 71.9 s |

  About 90% of the copy cost is round-trip blobs. Nothing caps snapshot memory, and CamParams and settings are not undoable at all.
- **There is no native format.** Rebuild-only data (arrays, text sources, groups, typed craft) survives only if it fits into `extra` and `.chf` happens to carry it. Unknown `.chf` graph or glyph types raise an error instead of being preserved.

### 1.7 Threading and process model

| Context | What runs there |
|---|---|
| GUI thread | Canvas, panels; a 100 ms timer reads `ctl.state()`. **Also:** `compile_job` inside `_start_doc` (35 s for `autosave.chf` with smoothing on), the toolpath rebuild after every edit, and ext_diag monitor QTimers calling `registers.read_snapshot(mc)` directly (ext_diag.py:238, 432, 733, 868, 916, 991), which block on card time-outs |
| Poll thread (50 ms) | `read_status`, alarms, mode; writes `MachineState` under `_slock` |
| Job worker thread | Runs `JobStreamer` or supervises `ProcessStreamer`; runs `job_start_hooks` and `after` callbacks on this thread |
| Per-alarm thread | One new thread per alarm that triggers a pause (ui_adapter:424) |
| `MachineOps.run_async` | Ad-hoc threads |
| Pendant reader | HID loop |
| `ProcessStreamer` child (spawn) | Its own socket and sequence numbers. It talks to the card at the same time as the parent's poll thread, while the original has one hardware lock |

Problems:

- **`state()` returns the live object without the lock**, so the GUI reads fields and the alarm list while the poll thread changes them.
- **Three different ways of getting back onto the GUI thread:** `_Bridge` signals, ext_machine `bridge.call`/`gui()`, and direct listener calls.
- **Real cards use `ProcessStreamer` by default, but the simulator path uses the in-thread `JobStreamer`.** Only 2 tests force the process path, so the default real-card path is the least tested.
- **The child is not supervised:** no heartbeat, no `getppid()` check, and no `freeze_support()` (a frozen Windows build would relaunch the GUI in the child). `os.nice(-5)` fails silently.
- **The whole job is pickled to the child:** 608 ms and 18.8 MB for 905k steps.
- **What you see is not what you cut:**
  - `compile_job` re-runs `build_toolpath` with the head position as the sort origin (compiler.py:623), while the canvas uses `cam_params.origin`, so the cut order can differ from the preview.
  - The placement offset from `place()` is not shown in the preview.

### 1.8 Configuration and parameters

| # | Store | Notes |
|---|---|---|
| 1 | `formats/data/mlaser_params.json` | 1,129 typed entries (type, min, max, unit, options, struct offset), extracted from `FUN_00635f90`. Used by `MachineParams`, `Machine.from_params`, `ProcessConfig` and diag. **This is the good one.** |
| 2 | `ui/resources/params.json` | 862 scraped entries with a separate `ParamDef` class; 859 overlap with #1. No types, ranges or units, `section` sometimes junk (`"%.3f"`), booleans guessed from the name |
| 3 | `window.settings` | Edited by `ParamGridDialog`: free text, int/float guessed, no validation. **The controller and nc never read it, so Hardware options changed in the GUI do not affect motion.** Persisted only for some key prefixes, by ext_diag |
| 4 | `nc.Machine` | The app always builds it with defaults. There is no `--dat` CLI option; `from_dat_dir` is reachable only from scripts. Axis scale comes from card read-back or defaults |
| 5 | `CamParams` | Accepted as an object, a dict or None |
| 6 | Ad-hoc | `mj_params`, `lead_*`, `ext_machine.values` |
| 7 | `TransportConfig` | From `ipAdd.ini` |
| 8 | `LayerParams` | A missing layer silently becomes `power_pct=100`, 5000 Hz (`cam/toolpath.py:418, 452`, `compiler.py:702`, which swallows the error). **On a laser this default is unsafe** |
| 9 | App-data directories | `~/.mlaser` (machine_ops.py:29, ext_diag.py:1638, report/window.py:259); `%LOCALAPPDATA%`/`$XDG_DATA_HOME`/`NexCut` (backup.py:82-84, wrong on macOS); `MLASER_APP_DIR`; two temp-dir schemes. No QSettings or platformdirs, and the language resets to English on each start |

### 1.9 Error handling and logging

- No bare `except:`, but **123 `except Exception` and 71 except blocks whose body is only `pass`**. Hotspots: ext_diag 12, ui_adapter 12, dxf_import 11, ext_machine 10, mainwindow 6.
- **Swallowed on safety paths:**
  - `JobStreamer._safe_laser_off` swallows a failed PWM-off.
  - ui_adapter:994 swallows a failed `fifo_stop` on abort.
  - ui_adapter:416 drops exceptions from alarm listeners.
  - ui_adapter:181 drops Qt emit errors.
  - In `_child_main` the pause path does `mc.fifo_stop()` inside `except ControllerError: pass`, so the pause silently doesn't happen.
  - `closeEvent` swallows `disconnect()`.
- **Logging:**
  - The `logging` module appears in only 3 controller modules (3 calls). There are nine or more ad-hoc `log()` helpers that write only to the in-memory UI panel (22 call sites), mixing lang keys and English.
  - There is **no log file, no crash handler, no `sys.excepthook`, and no audit trail** of commands, alarms, arming or link loss. After an incident there is nothing to examine.

---

## 2. Safety architecture

### 2.1 Measured behaviour

The simulator turns the laser off and stops instantly whenever the FIFO halts or runs dry, so every number below is a best case.

| Scenario | Result |
|---|---|
| Stop (in-thread / process streamer) | returns in 0.007 / 0.052 s; the head moves 0.6 / 5.0 mm after the request |
| Pause | 0.0 / 1.0 mm |
| E-stop pulse (300 ms) during a job | stops within 3.9-4.3 mm; mode becomes pause, breakpoint and alarm recorded. **Good.** |
| `disconnect()` mid-job (also run by window close, Reconnect, `set_controller`, `connect_simulator`) | **the card keeps cutting for 5.27 s / 527 mm; PWM left on** |
| Link loss mid-job (simulator drops replies) | **cutting continues for 3.5-5.3 s / 355-528 mm with the laser on.** Abort is logged after about 5 s, then the mode returns to "ready" with no fault kept |
| UI killed with SIGKILL while `ProcessStreamer` runs | **the orphaned child streams the whole job** (still running after 12 s) |
| `stop()` while the link is down | UI thread blocked for **24.5 s** |
| FIFO stop or program stop sent by a second client | **`JobStreamer` re-starts the FIFO both times**; the head keeps moving |
| Job placed at x = 50,000 mm or -5,000 mm | compiles and would run, with no warning; `MS.EnableSoftLimit` defaults to False |
| Dry run | 0 laser-on steps, process items suppressed. **Good.** |

### 2.2 Ranked gaps

**C1. No guaranteed safe state on disconnect, link loss or exception.**
- `UiController.disconnect()` (ui_adapter.py:316-334) sets cancel, joins the worker, stops polling and closes the socket. It never sends program stop, PWM 0, FIFO stop or outputs off.
- `JobStreamer.run_job` just `break`s on cancel.
- The worker's `except` catches only ControllerError and OSError, and sends only `fifo_stop`, never PWM-off. Any other exception sends nothing.
- No atexit or signal handler.
- `MotionController.stop_manu` skips PWM-off if `stop_motion` raises.

**C2. The streamer process is unsupervised.**
- The child never checks that its parent is alive, so after a crash it keeps streaming to the end.
- When the parent sees the child die, it sends only `fifo_stop`.
- Whether the MCC100 has a comms watchdog of its own is undocumented (protocol.md), and the simulator has none.

**C3. Link loss is detected slowly and not latched.**
- `transact` retries 3 sends × 3 waits × 500-600 ms, so detection takes about 4-5 s. That is as long as the prefilled FIFO lasts: the default 64 KiB is about 5.4 s, or about 500 mm at 100 mm/s.
- `_poll_loop` marks the controller "offline" after 3 failures but does not cancel the job, raise an alarm or block Start.
- The FIFO underrun watchdog (40 ms threshold, 10 ms poll) runs inside the loop it guards and needs `read_status` to succeed, so it cannot detect link loss.

**C4. The streamer overrides stops.**
- The "underrun or stopped with data left (INFERRED)" branch in `JobStreamer.run_job` and the watchdog's `auto_resume` call `fifo_start` whenever the card is idle with frames left.
- Today this is masked only because `stop()` joins first and the simulator refuses `fifo_start` while an alarm is latched.
- It is not masked when `pause()` sends the program stop before setting cancel, when a card-side stop doesn't latch, or when a real card accepts a FIFO start after an e-stop is released.
- Auto-resume restarts motion and the beam mid-contour, without a pierce and without operator consent.

**H1. `--laser-off` is not enforced at the lowest layer.**
- `laser_enabled` is checked only in `_compile` (dry run), `laser_press` and `laser_test`.
- **44 direct `ctl.mc` / `_mc()` uses** in ui, diag and devices bypass it:
  - ext_diag `apply_da_pwm` and `_set_do` (any output, including the laser output, even during a job);
  - `diag/lighttest.py` (PWM);
  - `diag/customio.py`;
  - the ext_devices laser panel (source emission on);
  - `set_laser_power`;
  - machine_ops and burn-in.
- The UI never shows the armed state (`laser_enabled` does not occur in `ui/`).
- Controllers created by `connect_simulator()` or `facade.py:342` default to laser enabled, and `--real` enables the laser by default.

**H2. Real hardware is one click away.**
- `__main__` always builds a `UiController` aimed at 10.1.1.168, even without `--real`, so Reconnect reaches a real card with the laser enabled.
- If construction fails, the app silently falls back to the demo controller.
- Start, Frame and homing have no confirmation. `_start_doc` checks only alarms and an empty document.

**H3. No travel check.**
- `compile_job` and `frame` accept any coordinates.
- Jog is clamped only when `MS.EnableSoftLimit` is set, which is off by default. Continuous jog commands the full stroke.
- The card's own soft limits, e-stop input mapping and servo-alarm wiring are sent only by `startup(card_params)`. `UiController.card_params` is always None, so the app relies on whatever the card already holds and never displays it.

**H4. Device alarms don't stop the job.**
- Only the motion card's two alarm words are decoded.
- Follower collision or height alarms, gas pressure and laser-source alarms are never polled during a job. `MachineState.ftc` is hard-coded "offline".
- There is no door interlock input.

**H5. Stop blocks the UI and runs in the wrong order.**
- `stop()` cancels, waits up to 10 s (plus 5 s for the streamer process), and only then sends the stop.
- `transact` holds a lock for up to about 4.5 s per request, and the poll thread contends for it.
- There is no global Stop shortcut; Esc only deselects.

**H6. Continuous jog has no dead-man.**
- Only the button release stops a full-stroke jog.
- In `devices/pendant.py` a HID read error sleeps 0.4 s and never calls `jog_stop`.
- Focus loss, a lost datagram or a UI hang lets the axis run to the end of travel.

**M1. The simulator is optimistic.**
- Laser off and zero velocity happen instantly on halt or drain.
- Fault injection covers only dropping the first N replies, e-stop, limits, servo alarm, soft limit and starvation.
- Missing: permanent link loss, latency and jitter, "laser stays on during hold", FIFO start accepted after an alarm, and duplicate frames from retried writes.

**M2. Tests cover only the happy path.** Nothing tests disconnect or link loss mid-job, streamer or parent death, worker exceptions, stop ordering, laser-off bypasses or job bounds. The scripts in `work/review/safety/` can become those tests directly.

**M3. Watchdog details.** PWM-off uses a fixed 5000 Hz instead of `LC.PtLaserFreq`.

**M4. Resume is unchecked.** No position check (head moved or re-homed), no confirmation and no bounds check on the rapid back. `resume_saved` trusts the index mapping after a recompile. The resume test is flaky: one pulse off, (6000,1) vs (6001,1), in 1 of several runs. Resume should be deterministic, so treat this as a possible real bug.

**M5. No persistent safety or audit log.**

**M6. INFERRED commands on safety paths.** Follower-on `[118,4,..]`, zero position `[9999,8,0]`, the step format, and whether a FIFO stop holds the beam are all inferred. They are flagged only in docs, not in the app.

Ranking: C1 > C2 > C3 > C4 > H1 > H3 > H2 > H5 > H4 > H6 > M1 > M2 > M4 > M3 > M5 > M6.

None of these fixes changes how a job is encoded on the wire. Where the original's command order on stop is checked by parity (`test_stop_sequence`), keep it as the first part of the sequence and add the extra commands after it.

### 2.3 Proposed safety design

A **`SafetySupervisor`** sits between every caller and `MotionController`. No other code holds a raw `mc`.

```
DISCONNECTED -> CONNECTED_DISARMED -> ARMED -> RUNNING/PAUSED
       ^               ^                 |          |
       +---------------+---- FAULT (latched) <------+   reset requires operator + state check
```

- **Arming enforced on every outgoing command.**
  - While disarmed it refuses: step items with duty > 0, PWM duty > 0, DA on the laser port, laser and gate outputs, and source emission.
  - The UI connects disarmed every time and shows a permanent ARMED/DISARMED banner.
- **`safe_halt()`**: program stop with PWM 0, process outputs off, follower stop, FIFO stop, FIFO clear. Each command is sent in its own try block, so one failure doesn't skip the rest.
  - It is idempotent, and the parity-checked stop order comes first.
  - It is called from `disconnect`, every worker exit (`except BaseException`/`finally`), `closeEvent`, atexit, SIGINT/SIGTERM and `sys.excepthook`.
- **Link health:**
  - A streaming deadline of about 150 ms without a successful status read means FAULT.
  - Prefill is capped at about 1-1.5 s of motion instead of the full FIFO.
  - Halt commands go over a dedicated short-timeout path that doesn't wait behind the transact lock.
- **Streamer child supervision:**
  - The child halts itself if no heartbeat arrives within about 200 ms or `getppid()` changes, and on any exit other than finishing the job.
  - The parent calls `safe_halt()` when the child dies.
  - There is one socket owner during a job.
- **The streamer never starts the FIFO on its own.** Any unexpected idle is a FAULT, and recovery goes through breakpoint/resume with a re-pierce.
- **Start pre-flight:**
  - Checks: job bounds inside travel, homed, armed, layers defined, card limits read back, Frame done (optional policy).
  - A single confirmation that shows these checks.
  - Refuse to run an undefined layer instead of defaulting to 100%.
- **Interlock aggregation:** card alarms, follower, laser source, gas pressure and a configurable door input, polled at about 50 ms during a job.
- **Jog dead-man:** incremental jog re-sent while held, and `jog_stop` on HID error, focus loss or window deactivation.
- **Pessimistic simulator mode plus fault-injection API**, and a `tests/safety/` suite. These are release-blocking tests.
- **Hardware recommendation for the owner:** wire a "PC alive" watchdog output, or a relay driven by a toggling output, into the laser-enable chain, and bench-test the MCC100's link-loss behaviour with the laser physically off. Software alone cannot guarantee a safe state when the PC dies.

---

## 3. Code quality

### 3.1 Size and complexity (`quality/metrics.py`)

- 131 files, 44,452 lines, 2,790 functions.
- **346 functions (12%) have no type hints** and **1,729 (62%) have no docstring**: ui 84%, devices 78%, diag 76%.
- 9 functions are over 100 lines, 100 have cyclomatic complexity (CC) over 15, and 22 have CC over 30.
- Largest files: `ui/ext_diag.py` 1,714; `parity/capture.py` 1,238; `controller/ui_adapter.py` 1,206; `cam/ordering.py` 1,185; `io/dxf_import.py` 1,128; `motion/smooth.py` 1,098; `controller/process.py` 1,040; `client.py` 994; `cam/flycut.py` 994.

| Function | CC | Lines |
|---|---|---|
| `parity/capture.py:compare_job` | 86 | 221 |
| `nc/compiler.py:compile_items` (core compile) | 62 | 173 |
| `nc/stream.py:JobStreamer.run_job` (safety-critical) | 50 | 126 |
| `cam/cleanup.py:merge_connected` | 50 | 93 |
| `cam/cleanup.py:_mainfit` | 44 | 97 |
| `cam/toolpath.py:build_toolpath` | 41 | 102 |
| `controller/frames.py:describe_item` | 39 | 49 |
| `cam/cleanup.py:remove_overlaps` | 38 | 102 |
| `controller/simulator.py:_run_fifo` | 37 | 86 |
| `controller/ui_adapter.py:_apply_status` (alarm handling) | 36 | 65 |
| `motion/interp.py:arc_interp` | 33 | 119 |
| `ui/ext_cam.py:_install_feature_handlers` | 31 | 148 |

### 3.2 Static analysis

- **Ruff, pyflakes rules: 77 findings.**
  - 66 unused imports and 9 unused variables.
  - 2 undefined names for `CardParameters` in `client.py:546` and `:602`, in string annotations only.
- **Ruff, wider rule set:** 856 magic-value comparisons, 284 imports inside functions, 108 semicolons, 76 `zip()` calls without `strict`, 4,449 long lines.
- **Two likely bugs:** a closure capturing a loop variable at `cam/array.py:306`, and a loop variable overriding its iterable at `nest/engine.py:378`.
- **No enums at all.** Modes, item kinds, registers and alarm bits are bare ints. Literal counts: `frames.py` 247, `pendant.py` 153, `process.py` 150, `client.py` 149, `startup.py` 144.
- **Dead code (vulture):**
  - `ui/dialogs.py:295 AlarmDialog` is never used, so **alarms are never shown in a dialog**.
  - `facade.make_controller`, `SerialTransport` (pyserial isn't even installed), `read_gcode`, `fit_line`, `seg_time` and `all_commands` are unused.
- **Mypy errors:** model 0, nc 24, motion 36, controller 63, cam 90. Mostly misused Optionals, e.g. `self.sock: socket | None` used without a check in `client.py:423-449`, and `int | None` used in arithmetic at `frames.py:218/275`. Bringing the core to clean typing is cheap.
- **Docstrings that cite evidence are a real strength:** about 2,100 `FUN_`/address citations in code and 1,047 in docs. But inferred items use three notations ("INFERRED", "(I)", "U"), and rebuild-only features are marked inconsistently.

### 3.3 Tests

- **Full run: 669 passed, 1 failed (flaky), 177.5 s** on a shared machine.
  - **Parity tests take about 123 s** of that: job_mainapp 54.6 s, machine 42.3 s, cad_motion 9.9 s, job_nc 4.8 s.
  - The 630 non-parity tests take about 54 s.
- **Flaky:** `test_card_resume.py::test_ui_pause_resume_from_breakpoint` was one pulse off. 17 test files start the UDP simulator, and 10 use `time.sleep`.
- **No pytest markers**, so a unit, UI, simulator or parity subset can't be selected.
- **Hard-coded external paths in 10 test files:**
  - original install: `test_cam.py:17`, `test_devices_follower.py:10`, `test_camx_flycut.py:11`, `test_machine_ops.py:21`;
  - parity directory: `test_io_dxf_sort.py:20`, `test_io_dxf_original.py:21`, `parity_helpers.py:24` (that one can be overridden);
  - `test_ui.py:17` writes a screenshot to `<workspace>/work/ui/`.
- Parity tests need captures from Wine runs of the proprietary binaries, so they can't run in public CI.
- `conftest.py` works around PySide6 crashing at interpreter exit.

### 3.4 Packaging

- **Undeclared imports:** segno (QR), fontTools (DXF TEXT), Pillow (engraving), hidapi (pendant), matplotlib (parity reports), pyserial. Everything installs, but those features fail at runtime.
- **Versioning and repository hygiene:**
  - No upper version bounds and no lock file. Development runs on PySide6 6.12, but the declared minimum (6.5) is never tested.
  - Static version 0.1.0 with no `__version__`.
  - `mlaser.egg-info` and `.pytest_cache` sit in the source tree.
- **`mlaser.parity`** (about 2,300 lines plus matplotlib) ships inside the runtime package.
- **Resources (5.5 MB):** 478 icons extracted from the original's PE resources and the 10 vendor language files. **This is a licensing blocker for distributing the project to others.**
- **Translation and help:**
  - The i18n system is custom and keyed by opaque ids (`mf459`, `gp83`).
  - `diag/helpdocs.py:18` reads `../../docs`, which is not packaged, and that content is reverse-engineering notes rather than user help.
- **No git, LICENSE, CONTRIBUTING, CHANGELOG, CI or pre-commit** anywhere under `<workspace>`.

### 3.5 Cross-platform

- **ProcessStreamer:** spawn is used, which is good, but `freeze_support()` is missing (needed for PyInstaller on Windows) and `os.nice` fails silently.
- **Sockets:** plain UDP with TCP_NODELAY and SO_REUSEADDR only; no privileged features. The host needs a NIC on 10.1.1.x, and Windows and macOS firewalls will prompt on first run. Document this, and add a network self-test (`controller/nettest.py` exists).
- **App-data paths:** four schemes (see §1.8). Use `platformdirs` or QStandardPaths.
- **Encodings:** GBK and UTF-16 are handled explicitly in 17 places, but text-mode `open()` without an encoding remains at `gcode.py:203` and `ext_diag.py:555/566`, which depends on the locale on Windows.
- **Fonts:** SimSun is hard-coded (`cad/text.py:24`, `ext_draw.py:48`, `report/printing.py:36`), with a font-directory scan as fallback. **DXF TEXT geometry therefore depends on which fonts are installed, so the same file can cut differently on two machines.**
- **HiDPI:** nothing explicit. The vendor's bitmap icons will blur, and `QFont("Sans", 8)` is fixed in the canvas.

---

## 4. Performance (`work/review/perf/`)

### 4.1 Compile path

On `autosave.chf` (839 contours, 237,166 step items, 237 s of machine time, 2,998 mm of cut):

| Phase | smooth off | smooth on (`Machine()` default) |
|---|---|---|
| Load .chf | 150 ms | |
| `build_toolpath` | 153 ms | 154 ms |
| `compile_items` | 1.30 s (5.5 µs/step) | **35-38 s (147 µs/step)** |
| Ratio to real time | 144x | **6.7x** |
| After the `_locate` fix (bit-identical) | | **2.48 s, 96x** |

- **The P0 bug.** `ContourSmoother._locate` (`motion/smooth.py:905`) builds a list of all non-sentinel pieces on every call, just to test that it isn't empty. It runs once per 1 ms sample, so the cost is O(samples × pieces), and it accounts for 27.9 of 37.9 s. Caching that flag gives 14x with bit-identical output (`perf/bench_smooth_fix.py`).
- **Remaining cost per step:** about 4.7 µs in the emitter (`_Emitter.to` → `quantise` → `_step` → `frames.encode_step`) and 0.6 µs in `_PathGeom.at`.
- **The next hotspot is the curve-table build:** 1.15 ms per corner blend, 1.83 s on the sample.
- **Memory:** about 280 bytes per step, because every step is a Python list. A 1-hour job is about 3.6 M steps, roughly 1 GB.
  - A 300-part job (905k steps) compiles in 4.3 s with smoothing off and 8.4 s with it on.
  - A 5,000-segment engraving scan compiles in 5.6 s to 1.48 M steps and **747 MB**. About 1,000 s of its 1,478 s run time is the 200 ms JumpAddTime settle after each rapid, sent as 200 separate one-period steps.
- **Hand-off to the streamer child:** pickle takes 608 ms and 18.8 MB, versus 80 ms and 10.5 MB as a numpy array.
- **Start, Dry Cut and Sim each recompile from scratch on the GUI thread.** Nothing is cached.

### 4.2 Large documents and canvas

| | 500 parts | 2,000 parts | 5,000-part scan | 30,000-part scan |
|---|---|---|---|---|
| `build_toolpath` (sort 0/1) | 229 ms | 1.42 s | 0.58 s | 11.3 s |
| `build_toolpath` (sort 20) | 2.37 s | 3.9 s | | |
| `MainWindow.set_document` | 0.66 s | 3.76 s | 10.1 s | **351 s** |
| `_refresh` after **every** edit | 0.55 s | **3.6 s** | | |
| Undo snapshot | 47 ms | 178 ms | | |
| Render, zoom or pan | 8-38 ms | 16-43 ms | | |
| `pick()` per click | 2.6 ms | 8.5 ms | | |
| Rubber-band select | 60 ms | 389 ms | | |
| Object snap per mouse move | | 6.9 ms (20k parts: 78.5 ms) | | |

- **Painting is not the bottleneck; ordering is.**
  - In the 5,000-part profile, `local_optimal_elems` takes 18.8 s: its O(n²) nearest-neighbour step makes 25 M calls to `approx_hypot`.
  - `containers_last_faithful` takes 3.0 s and the canvas 0.56 s.
  - At 2,000 parts, `build_tree` (an O(n²) containment scan) takes 1.9 s and `_cheapest_pos` 1.3 s.
- **Other costs:**
  - An 810 ms selection hiccup at 2,000 parts is most likely a full GC pass over millions of small lists.
  - Each contour adds 3 scene items, so 90,004 items at 30,000 parts.
  - A full 800x600 dithered image becomes 184,627 parts, which the current UI cannot handle.
  - Pick, rubber-band select and snap are linear scans and don't use the scene's spatial index.
- **Nester:** acceptable. 200 parts in 1.44 s, using numpy and shapely.
- **Smoother:** 1,000 edges take 478 ms, dominated by `_locate`. Text layout of 200 characters takes 18 ms. Raster G-code takes 34 ms plain, 641 ms halftone, 2.25 s dithered.

### 4.3 Where acceleration pays off, in order

1. **Fix `_locate`.** S effort, zero risk, 14x.
2. **Move compile and toolpath work off the GUI thread, debounced, and cache the result.** Key the cache on the hash of doc, CAM params, machine, layers and dry-run.
3. **Vectorise the ordering loops with numpy**, keeping exactly the same float formulas and first-match tie-breaks, and filter `build_tree` candidates with an STRtree. Expect 20-200x on those functions. Verify against golden orders from the parity samples plus randomised old-versus-new tests.
4. **Cache each contour's flattened polyline and bbox**, invalidated when its segments change, for about 3-5x in CAM setup. Add a spatial index for pick, select and snap.
5. **Store the step stream in packed numpy arrays**: 20x less memory, no GC pauses, and a shared-memory hand-off to the child. `gc.freeze()` after compile works as a stopgap.
6. **Compile the per-sample emitter loop with Cython or Rust (PyO3)**, keeping the pulse carry as a sequential loop with identical float operations. Expect about 0.1 s per million steps instead of 1.3-4.3 s, gated on the golden wire tests.
7. **Engraving as a first-class raster object** with its own compile path, instead of one Part per burn segment. Hide labels and markers above a few thousand contours.
8. **Port the curve table and smoother to C or Rust** only after 1 and 6, and against the Python reference.

---

## 5. Long-term maintainability

**What a new contributor faces:**
- 44 k lines in 17 packages with cyclic imports.
- An extension mechanism whose behaviour depends on load order.
- 9 configuration stores.
- Docs that are reverse-engineering logbooks: 6,096 lines, about 1,000 address citations, and BRIEF.md, which is the agents' work brief with file-ownership and tool rules.
- No architecture overview, contributor guide, user manual, git history or CI.

**Parity and modern code are mixed in the same files.**
- `cam/ordering.py` holds the original's modes 1-7 next to the rebuild's shortest-path and grid sorts.
- The motion code carries emulation details such as `x != x` NaN checks and the x87 53-bit assumptions.
- The B-spline evaluators aren't marked as to which one must stay bit-exact.
- So any refactor risks breaking parity silently, and every modern improvement is constrained by bit-exactness.

**Recommended isolation:**
- Put bit-exact ports in `mlaser/parity_ports/<original module>/`: motion planner/interp, smoother, curve table, ordering modes 1-7, the DXF→chf importer quirks, the `.chf` codec, the wire codec, process sequencing. Each function carries a `@port("CADModule", "FUN_1003f010", status="observed")` marker. The parity tests stay pinned to them, and changing them needs a parity re-run.
- Modern code depends on **interfaces** (`Orderer`, `VelocityPlanner`, `Smoother`, `Flattener`, `Importer`) selected by a `ParityProfile`, with the original's behaviour as the `"mlaser-0.0.1.51"` profile and new algorithms as other profiles. Golden job tests then guarantee that the default profile stays bit-exact while new profiles evolve freely.
- Move `mlaser.parity` (capture, pcap, report, synth) out of the runtime package into `tools/parity/` or a separate distribution.

**Documentation split:**
- `docs/user/`: install, safety, first cut, machine setup.
- `docs/dev/`: architecture, plugin API, testing, release.
- `docs/re/`: the current protocol, format and decomp notes, with one observed / inferred / unknown notation.
- Archive BRIEF.md.

**Versioning and compatibility:**
- Use SemVer for the app.
- `.chf` is an **interchange format**, read for versions 1-9 and written at version 9 byte-exactly for unchanged content. Preserve unknown records as opaque blobs instead of failing.
- Add a **native versioned project format**, e.g. zip + JSON + optional embedded original `.chf` blobs, with a schema version and migrations. Rebuild-only features (typed per-contour craft, arrays, text sources, raster engravings, IDs) live there instead of in `extra`.
- Keep the wire protocol behind `protocol/` with a `card_firmware` capability table.

---

## 6. Proposed target architecture

```
                         +-----------------------------------------------+
  apps                   |  mlaser.app (Qt)      mlaser.cli    plugins/* |
                         |  views, dialogs,      batch compile, (3rd     |
                         |  canvas, i18n         sim, export    party)   |
                         +---------------+-------------------+-----------+
                                         | PluginContext / commands / undo
                         +---------------v-------------------------------+
  application services   |  mlaser.services                              |
                         |  DocumentService (commands, QUndoStack-like,  |
                         |   IDs), JobService (plan->compile->cache,     |
                         |   worker pool), MachineService (MachineAPI),  |
                         |   ConfigService (typed registry, platformdirs)|
                         |   EventBus (job/alarm/link events), AuditLog  |
                         +---+-----------+------------+------------------+
                             |           |            |
         +-------------------v--+  +-----v------+  +--v----------------------------+
 domain  | mlaser.model         |  | mlaser.cam |  | mlaser.machine                |
         |  geometry (Line/Arc, |  | toolpath,  |  |  SafetySupervisor (FSM, arm,  |
         |  cached flatten/bbox)|  | leads,kerf |  |   safe_halt, interlocks,      |
         |  Part/Contour IDs,   |  | ordering   |  |   link deadlines, preflight)  |
         |  typed Craft +       |  | (iface)    |  |  JobRunner / Streamer (child  |
         |  overrides, Layer    |  +-----+------+  |   proc w/ heartbeat)          |
         +----------+-----------+        |         |  devices: follower, laser,    |
                    |             +------v------+  |   AF, EC, pendant             |
                    |             | mlaser.motion|  +--+----------------------------+
                    |             | planner,     |     |
                    |             | smoother     |     |
                    |             +------+-------+     |
                    |             +------v-------+     |
                    |             | mlaser.nc     |    |
                    |             | process seq,  |    |
                    |             | sampler/      |----+  PlannedJob (immutable:
                    |             | emitter (opt. |       toolpath + packed wire
                    |             | native kernel)|       arrays + bounds + hash)
                    |             +------+--------+
 foundation  +------v----------------------v------------------------------+
             | mlaser.geom (one flatten/spline/bbox lib)                  |
             | mlaser.protocol (MCC100 frame codec, item encoders, enums) |
             | mlaser.formats (chf, nestproj, xml params, lang import,    |
             |  native .mlz project)  mlaser.io (dxf/svg/gcode)           |
             | mlaser.parity_ports (bit-exact ports, @port markers,       |
             |  selected via ParityProfile)                               |
             +------------------------------------------------------------+
 outside the runtime: tools/parity (Wine harness client, capture/pcap compare),
          mlaser-cardsim (simulator + fault injection), tests/{unit,sim,ui,safety,parity,golden}
```

Rules, enforced with import-linter in CI:

- Dependencies point downward only.
- `app` never imports `machine` internals, only `services`.
- Nothing except `machine.SafetySupervisor` holds a transport or `MotionController`.
- `parity_ports` imports only `geom`, `protocol` and `model`.
- Domain packages import no Qt.

---

## 7. Prioritised recommendations

Effort: S < 2 days, M < 2 weeks, L > 2 weeks. Risk means the risk of regressing behaviour or parity.

### P0: before anyone connects a real machine, or another person uses it

| # | Problem (evidence) | Change | Effort | Risk |
|---|---|---|---|---|
| P0-1 | Nothing reaches a safe state on disconnect, close, exception or crash. `disconnect()` (ui_adapter.py:316) sends no stop, and the card cut **527 mm with PWM on** after it | `safe_halt()` (stops, PWM 0, outputs off, FIFO stop/clear, each command independent) on every exit path: worker `finally`, disconnect, closeEvent, atexit, signals, excepthook. Tests using the simulator | M | Low. Stop order kept from parity |
| P0-2 | The orphaned `ProcessStreamer` streams the whole job after a UI crash. The parent sends only `fifo_stop` when the child dies | Heartbeat and `getppid()` check in `_child_main` with self-halt; the parent halts when the child dies; one socket owner; `freeze_support()` | S-M | Low |
| P0-3 | Link loss is detected in 4-5 s, the same as the 5.4 s FIFO prefill; not latched; the mode returns to "ready" | Streaming deadline about 150 ms, then FAULT; a latched FAULT state that blocks Start, Resume and jog; prefill capped at 1-1.5 s; separate halt channel | M | Medium (underrun tuning on real hardware) |
| P0-4 | The streamer calls `fifo_start` after stops it didn't issue, and watchdog auto-resume re-fires the beam mid-contour (`JobStreamer.run_job`) | Remove auto-restart: an unexpected idle is a FAULT, and recovery goes through breakpoint/resume with a re-pierce; pause sets hold before it sends the stop | S | Low |
| P0-5 | `--laser-off` is compile-time only, with 44 direct `mc` bypasses; armed state not shown; `--real` defaults to laser on; app silently falls back to Demo | `SafetySupervisor` with arming enforced on every outgoing command; connect disarmed; ARMED banner; `--real` fails loudly instead of falling back; remove `mc` from UI and diag | M | Low |
| P0-6 | No travel check: a job at x = 50,000 mm compiles and runs; card limits never downloaded or shown; an undefined layer defaults to **100% power** | Start/Frame/Resume/move pre-flight (bounds vs travel, homed, armed, layers defined, card limits read back) plus a confirmation dialog; an undefined layer is an error | S-M | Low |
| P0-7 | Compile is 6.7x real time and Start freezes the UI for 35 s, because of the O(samples × pieces) `ContourSmoother._locate` (smooth.py:905) | Cache the non-sentinel flag. **14x, bit-identical.** Add a compile-time regression test | S | ~0 |
| P0-8 | No git, licence or CI; 6 undeclared dependencies; vendor icons and language files bundled; one flaky resume test | `git init`, licence decision, GitHub Actions (Linux, Windows, macOS; non-parity suite), declare segno/fontTools/Pillow plus extras, lock file; replace or remove proprietary resources before any public release; investigate the 1-pulse resume error | S-M | Low |
| P0-9 | No persistent log or audit trail; safety failures swallowed (`_safe_laser_off`, ui_adapter:994/416, the pause path in the child); `AlarmDialog` never used | `logging` everywhere with a rotating file, structured events for commands, arming, alarms and link loss, a crash handler; turn swallowed safety failures into FAULT plus a banner; show alarms | S-M | Low |

### P1: structural work that makes the codebase maintainable

| # | Problem | Change | Effort | Risk |
|---|---|---|---|---|
| P1-1 | Cyclic package graph: {controller, nc, ui, devices, diag} and {cam, io, formats}; controller imports `ui.facade` | Move `frames` to `protocol/`, `process.py` to `nc/`, `cam/items` to `toolpath/`, merge `io/geom` and `cam/geometry` into `geom/`, move `ControllerFacade` and `MachineState` to `machine/api.py`; import-linter in CI | M | Low (moves plus re-export shims) |
| P1-2 | Four controller abstractions; 17 methods outside the ABC probed via 26 `hasattr` sites; demo silently loses features; job detection by log regex | A typed `MachineAPI` protocol plus an event bus (job started/finished, alarm, link lost); Demo and the real controller implement all of it; split UiController (1,206 lines) into connection/poller, motion, job runner and IO | M | Low-medium |
| P1-3 | What you see is not what you cut: the preview uses a different sort origin and no placement; Start, Dry Cut and Sim each recompile on the GUI thread; the streamed frames differ from `Job.frames()` | An immutable `PlannedJob` (toolpath + packed wire + bounds + hash), compiled in a worker with progress and cancel, cached; the canvas renders it, Start streams exactly it; the streamer uses `Job.frames()`, with a test that they are equal | M | Medium (cut order may change; parity flag) |
| P1-4 | GUI freezes on large documents: 3.6 s per edit at 2,000 parts, 351 s to open a 30,000-part scan; O(n²) ordering (`local_optimal_elems`, `build_tree`, `_cheapest_pos`) | Debounced background toolpath; vectorise the ordering kernels with numpy keeping the same float formulas and tie-breaks; STRtree filter; golden order tests | M | Medium (parity of order) |
| P1-5 | The extension mechanism rebinds 170 commands, silently overrides 10, monkeypatches `set_controller` three times, copy-pastes helpers 7 and 6 times | `PluginContext` API: `register_command` (with duplicate detection), `edit(fn, label)` transactions, controller/mode hooks, `add_dock`, settings schema; tests load the app's real wiring | M | Low-medium |
| P1-6 | About 9 configuration stores; GUI hardware options never reach `nc.Machine`; no `--dat`; scraped `params.json` duplicates the 1,129-entry registry | One `ConfigService` generated from `mlaser_params.json` (typed, ranges, units), validated dialogs, platformdirs persistence, the machine profile feeds `Machine`; delete `ui/params` and `ui/resources/params.json` | M | Low-medium |
| P1-7 | Undo deep-copies the whole document: 89.7 ms per edit on the sample (8.9 ms without blobs), 178 ms at 2,000 parts, 50 copies, unbounded memory | Command-based undo (QUndoStack) with stable IDs; round-trip blobs in an immutable side table keyed by ID, never copied | M | Medium (byte-exact .chf tests guard it) |
| P1-8 | Parity ports and modern code mixed; 4 B-spline evaluators, 7 sorters and 8 flatteners with nothing saying which are bit-exact | `parity_ports/` namespace with `@port(...)` markers; interfaces selected by `ParityProfile`; one modern `geom` library for everything else; move `mlaser.parity` out of the runtime | M | Low (tests pin behaviour) |
| P1-9 | Interlocks: follower, gas and laser-source alarms unpolled; jog has no dead-man; Stop blocks the UI for up to 24.5 s offline | Interlock monitor (about 50 ms) feeding the supervisor; incremental hold-to-jog plus stop on focus loss or HID error; asynchronous stop that sends the halt first; global Stop shortcut | M | Low |
| P1-10 | Tests: 177 s, no markers, parity in the default run, hard-coded paths, timing sleeps, safety failure paths untested | Markers (unit, ui, sim, parity, golden, safety); env-var fixtures; default run without parity (about 54 s, about 30 s with xdist); pessimistic simulator mode and fault-injection API; `tests/safety/` built from the review scripts | S-M | Low |
| P1-11 | Business logic in UI (`docops`, `microjoint_auto`, ext_cam handlers, the ext_diag interlock and counters); shadowed duplicate sort and array paths | Move into `services`/domain operations with unit tests; delete shadowed paths | L | Low |
| P1-12 | Typing and complexity: mypy errors in nc 24, motion 36, controller 63, cam 90; CC 62 `compile_items`, CC 50 `run_job`; no enums; two likely bugs (array.py:306, engine.py:378) | mypy in CI starting with the core; IntEnum/IntFlag for ops, kinds and alarm bits; split functions with CC over 40 under golden tests; fix the two bugs | M | Low-medium |

### P2: longer term

| # | Problem | Change | Effort | Risk |
|---|---|---|---|---|
| P2-1 | About 280 bytes per step, about 1 GB for a 1-hour job; 608 ms pickle to the child | Packed numpy step arrays plus a shared-memory hand-off; `gc.freeze()` as a stopgap | M | Medium |
| P2-2 | Emitter costs about 4.7 µs per step in pure Python | Cython or Rust kernel for sample → quantise → encode, with sequential carry and identical float operations; the Python version stays as reference | M-L | Medium |
| P2-3 | Engraving scans become one Part per segment (184k parts for 800x600); JumpAddTime settle sent as 200 separate steps | A first-class raster engraving object with its own draw and compile paths; level-of-detail for labels and markers | M | Low |
| P2-4 | No native format; 46 `extra` keys; three cool-point representations; per-contour craft in `extra`; unknown .chf records fail | Typed model (IDs, contour craft overrides, one cool-point form), versioned native project format with migrations, `.chf` as interchange with opaque preservation; test that craft survives save/load and nesting | L | Medium |
| P2-5 | Flattening doesn't bound chord error; no geometry caching; linear pick and snap | Chord-error flattening (outside parity paths), per-contour cached polyline and bbox, spatial index | S-M | Low |
| P2-6 | Docs are reverse-engineering logbooks; BRIEF.md in the repo; help reads unpackaged `../../docs` | Split into `docs/user`, `docs/dev`, `docs/re`; one observed/inferred/unknown notation; archive BRIEF; packaged or online help | M | None |
| P2-7 | i18n keyed by opaque vendor ids; vendor strings; HiDPI-blurry bitmaps; SimSun hard-coded so cut text differs between machines | Qt Linguist/gettext with readable keys plus an importer for the original's language files; SVG icons; bundle an open CJK font for cutting text | M-L | Low |
| P2-8 | Curve-table build 1.15 ms per blend | Native port after P2-2, tested against the Python reference | L | Medium |
| P2-9 | Frozen-app packaging untested | PyInstaller or Briefcase builds per OS in CI, signed; first-run network and firewall self-test | M | Low |

---

## 8. Migration path that keeps the parity guarantees

**Step 0: lock in the current behaviour.**
- Put the project under git and add CI.
- Before any refactor, capture **golden artifacts** from the current code into `tests/golden/`:
  - wire step streams and `Job.frames()` for the sample `.chf` and the DXF samples, plus 300 seeded random jobs;
  - toolpath orders for each sort mode;
  - `.chf` round-trip bytes;
  - planner samples.
- These, plus the existing Wine parity results (`parity/out/results.json`, re-run nightly on the owner's machine), become the contract. Every later step must keep them bit-identical unless a change is declared and reviewed.

**Step 1: safety, in about 2-3 weeks (P0-1 to P0-6, P0-9).**
- Add the `SafetySupervisor` and `safe_halt` as a layer on top of the existing `MotionController`, without changing the wire encoding of jobs.
- Add the safety test suite and the pessimistic simulator.
- In parallel: the `_locate` fix (P0-7) and packaging and CI (P0-8).

**Step 2: break the cycles (P1-1).**
- Pure moves, with re-export shims left at the old paths for one release; the golden tests prove nothing changed.
- Turn on import-linter.

**Step 3: fence the ports (P1-8).**
- Move bit-exact code into `parity_ports/` behind interfaces.
- The default `ParityProfile` reproduces today's behaviour exactly, as the golden tests show.
- From then on, modern algorithms (faster ordering, chord-error flattening, a new planner) are added as **new profiles** and never edit the ports.

**Step 4: services (P1-2, P1-3, P1-5, P1-6, P1-7).**
- Add `MachineAPI`, `PlannedJob`, `ConfigService`, command undo and `PluginContext`.
- Port the ext_* modules onto `PluginContext` one at a time; the command-coverage tool keeps all 246 commands bound.

**Step 5: performance (P1-4, P2-1, P2-2).**
- Vectorised or native kernels land behind the same interfaces.
- Each is validated by old-versus-new randomised equivalence tests and the goldens before the old Python path becomes the reference only.

**Step 6: model and native format (P2-4).**
- Stable IDs and typed craft; the `.chf` codec becomes an adapter; the native format is versioned with migrations.
- Byte-exact `.chf` round-trips are kept by storing the original records in the side table.

At every step the rule is the same: **parity ports change only with a Wine parity re-run; everything else must keep the goldens bit-identical or declare a new profile.** This keeps the original-compatible behaviour available to the owner for checking against their own machine (`tools/compare_capture.py`), while new behaviour can evolve.
