# Roadmap (plan of record)

Source: docs/review/REVIEW.md (combined), with details in docs/review/{architecture,libraries,ux}.md.
Every stage keeps the **contract**: the golden artefacts in `tests/golden/` and the Wine parity run
(`../parity/run_parity.sh`). Bit-exact ports change only with a Wine parity re-run. Everything else
must keep the goldens bit-identical or declare a new `ParityProfile`.

Status: ☐ todo · ◐ in progress · ☑ done

## Wave 0: lock in (Stage 0)
- ☑ git repository, `.gitignore`, first commit of the current tree
- ☑ CI (GitHub Actions: Linux, Windows, macOS; unit, ui and sim suites), lock file
  (`.github/workflows/ci.yml`, `uv.lock`, `.pre-commit-config.yaml`; first run on GitHub pending)
- ☑ pytest markers (unit / ui / sim / parity / golden / safety). The default run excludes parity
  (and golden, which CI runs as its own step).
- ☑ golden artefacts (`tools/make_goldens.py`, `tests/golden/`):
  - wire streams and `Job.frames()` for the samples, plus seeded random jobs
  - toolpath orders per sort mode
  - `.chf` bytes
  - planner and smoother samples
- ☑ fix the flaky 1-pulse breakpoint-resume test (the resume rapid now lands on the restart pulse)
- ☑ vendor assets out of the package: an open icon set (MIT/Apache), and language files loaded
  from the user's own Mlaser install at runtime instead of being shipped (docs/ASSETS.md; the
  first commit still contains them, so rewrite the history before publishing)
- ☑ licence file (owner's decision: MIT)

## Wave 1: structure moves (Stage 2a, mechanical, guarded by the goldens)
Layout, rules and checks: [docs/dev/ARCHITECTURE.md](dev/ARCHITECTURE.md).  Every step kept the
goldens bit-identical, the default and parity suites green, command coverage at 0 / 0 and the Wine
parity run at 728 match / 17 differ / 0 errors.
- ☑ break the cycles (both are gone; the last one, diag ↔ ui through the lazy
  `diag.batch -> ui.docops` import, went with Wave 2 "business logic out of the UI"):
  - `mlaser/protocol/` (frame codec, ManuItem, enums)
  - `mlaser/geom/` (one flatten/spline/bbox library: `curves` + `contour`)
  - `process.py` → `nc/`; the job streamer → `controller/stream.py`
  - item types → `toolpath/`
  - `ControllerFacade` / `MachineState` → `machine/api.py`
  - also: `formats.lang` → `i18n.langfile` (formats ↔ i18n cycle), the follower card simulator →
    `controller.follower_sim`
  - deprecated aliases left at the old paths (`mlaser/_compat.py`, `tests/test_compat_shims.py`)
- ☑ `parity_ports/` fence with `@port` markers and `ParityProfile` selection: planner, profiles,
  segInterp/arcInterp, smoother + curve table + crtmath, exact splines, quantisation /
  emit_samples, ManuEncoder, import sort / local optimal / ring sort / choose_start, CircleFit,
  DXF import rules, `.chf` codec; default profile `"original"`, nine slots;
  `tests/test_parity_ports.py`
- ☑ import-linter contracts in CI (downward-only layers, Qt-free domain, parity_ports only on
  geom/protocol/model; ignores listed in docs/dev/ARCHITECTURE.md §1, each with a Wave 2 item)
- ☑ IntEnum/IntFlag for opcodes, ManuItem classes, glyph kinds and alarm bits
  (`protocol.enums`, `model.GlyphKind`); mypy on the core (protocol, geom, parity_ports, nc) in CI,
  with a `# type: ignore` baseline
- ☑ Python ≥ 3.12 (PySide6 6.12 aborts on 3.11), CI on 3.12 / 3.13; goldens pinned to 3.12's
  `sum()` (3.13 matched too)

## Wave 2: in parallel
### Safety (Stage 1, highest priority)
Done (docs/SAFETY.md, `tests/safety/`); the typed MachineAPI and event bus of the Services list came with it.
- ☑ `machine/SafetySupervisor`: an FSM that enforces arming on every outgoing command; connect
  starts disarmed; `--real` fails loudly instead of falling back
- ☑ `safe_halt()` on every exit path: worker `finally`, disconnect, closeEvent, atexit, signals,
  excepthook
- ☑ streamer process supervision: heartbeat, parent check, self-halt, `freeze_support`
- ☑ link loss detected within about 150 ms, then a latched FAULT state; prefill capped at 1–1.5 s;
  no FIFO auto-restart (recovery goes through breakpoint/resume with a re-pierce)
- ☑ pre-flight check before Start/Frame/Resume/move: travel bounds, homed, armed, layers defined
  (undefined layer is an error), open contours, card limits read back
- ☑ interlock monitor for follower, gas and laser-source alarms; hold-to-jog dead-man; an
  asynchronous Stop that sends the halt first; global Hold/Stop shortcuts
- ☑ "PC alive" heartbeat output for an external watchdog relay (documented wiring)
- ☑ rotating log file, structured audit events, crash handler; swallowed safety failures become
  FAULT plus a banner
- ☑ UX safety P0s:
  - ARMED / DRY-RUN / SIMULATOR banner
  - alarm banner with remedy text
  - no file open or new document during a job
  - run controls always visible
  - startup prompts as dialogs, not log lines
- ☑ pessimistic simulator mode plus a fault-injection API; `tests/safety/`

### Services (Stage 2b)
- ☑ typed `MachineAPI` plus an event bus (job, alarm, link events); split UiController; Demo and
  real controllers implement the full API (done in the Safety track) (removes the import-linter ignore
  `controller.ui_adapter -> PySide6`: the Qt signal bridge moves to the app)
- ☑ immutable `PlannedJob` compiled in a worker (progress, cancel, cache); the preview renders it
  and Start streams exactly it; the streamer uses `Job.frames()` (`mlaser.services.jobs`; the
  run bar, run controls, pendant and batch runner hand the plan to `start_job`, which runs the
  pre-flight on it; tests/test_services_jobs.py)
- ◐ `ConfigService` generated from the 1,129-entry registry: typed, units, ranges, platformdirs;
  the machine profile feeds `Machine` (done: `mlaser.services.config`, typed Options dialogs,
  HardPara/ManuPara XML import/export, a GUI option changes the compiled job); still open: it
  hands the layer defaults to `parity_ports.mainapp.layer` (the import-linter ignore
  `parity_ports.mainapp.layer -> formats.params` stays)
- ☑ command-based undo with stable IDs; round-trip blobs kept in an immutable side table
  (`mlaser.services.undo`; bounded by count and bytes; byte-exact .chf through edit/undo/redo)
- ☑ `PluginContext` API; port ext_* onto it; command coverage keeps all 246 commands bound
  (`mlaser.services.plugins`, docs/dev/PLUGINS.md, `examples/mlaser_hello`, entry points
  `mlaser.plugins`; coverage: every command bound, 0 raising, 0 duplicate registrations)
- ☑ business logic out of the UI (docops, ext handlers → services; removes the import-linter
  ignore `diag.batch -> ui.docops`) (`mlaser.ops`, `services.production`; the ignore is gone)

### Code health
- ☐ mypy on the rest of the package (cam, motion, io, formats, controller, devices, ui ...;
  architecture.md §3.2) and burn down the `# type: ignore[...]` baseline of the typed core
  (protocol, geom, parity_ports, nc)
- ☐ remove the Wave 1 compatibility shims (old import paths, docs/dev/ARCHITECTURE.md) after one
  release

### Performance and libraries (Stage 4)
Measurements before → after: [docs/dev/PERFORMANCE.md](dev/PERFORMANCE.md); licences:
[docs/ASSETS.md](ASSETS.md) §7.  Every step kept the goldens bit-identical (default profile).
- ◐ background, debounced toolpath; numpy ordering kernels with the same formulas (goldens)
  - ☑ bit-identical drop-ins for the mode-5 kernels (`parity_ports/cadmodule/ordering_fast.py`,
    reference profile `original-reference`), STRtree `build_tree`, flatten and prepare caches:
    toolpath at 2,000 parts 2.2 s → 0.27 s, per-edit refresh 3.6 s → 0.65 s, opening the
    30,000-part scan 351 s → 5.4 s
  - ☐ background / debounced worker: belongs with the Services `PlannedJob` worker
  - ☐ ring sort (modes 6/7) and band sort still O(n²) / unvectorised
- ☑ raster engraving as a first-class object (`cam/raster_engraving.py`, one Part, its own
  toolpath and canvas item, wire identical); level-of-detail on the canvas.  JumpAddTime settle
  stays 200 steps on the wire (a single dwell item changes the wire), stored as one run in the
  packed wire
- ◐ packed numpy step arrays plus shared memory to the streamer: ☑ `nc/packed.py`
  (69 MB instead of ~0.8–1 GB per hour of job, frames identical), `Job.to_shared()` /
  `from_shared()`; ☐ the streamer integration (Safety's supervised streamer, merge note)
- ☑ library swaps and options:
  - ☑ svgelements for SVG
  - ☑ ezdxf "modern import" mode (`import_dxf(mode="modern")`, profile `modern-dxf`)
  - ☑ pyclipper kerf fallback (opt-in `CamParams.kerf_fallback="clipper"`: the goldens use the
    shapely fallback, so it cannot be the default)
  - ☑ OR-Tools ordering option (profile `modern-ortools`)
  - ☑ fontTools + uharfbuzz text, bundled Noto Sans SC / Arabic subsets (the import-linter
    ignores `cad.text -> PySide6` and `io.dxf_import -> PySide6` are removed)
  - ☑ modern nesting engine: spyrrow (MIT, licence verified), `NestParams.engine="spyrrow"`
- ◐ PyInstaller one-folder builds per OS in CI, signing hooks, first-run network self-test:
  ☑ `packaging/`, `.github/workflows/package.yml`, `mlaser/netcheck.py`, LGPL notices, Linux
  bundle built and smoke-tested locally; ☐ the workflow's first run on GitHub, installers
- ☑ optional native kernel (C, `native/`, profile `native-sampling`) for sample → quantise →
  encode, Python kept as reference; 0 differences on 326 golden jobs; ☐ CI wheels
- ☑ optional modern planner profile (Ruckig, `modern-ruckig`), off by default; ☐ hardware
  validation

## Wave 3: modern workflow (Stage 3, built on the services)
- ☐ task workspaces Design → Process → Nest → Run → Report (+ Machine), a persistent run bar,
  a Ctrl+K command palette, and an optional classic ribbon
- ☐ material/technology database: material × thickness × gas × nozzle; versioned; diff against
  factory values
- ☐ properties panel instead of modal dialogs; units; live previews
- ☐ preview with a scrubber, plus a time and cost card
- ◐ touch-first Run screen; Operator, Engineer and Service modes (☑ Run screen:
  `ui/workspaces/run.py`, docs/user/run.md; ☐ the modes)
- ☑ graphical nest workspace; job queue; production reports; remnant stock
  (`ui/workspaces/{nest,run,report}.py`, `services/{queue,remnants,nesting,operator}.py`,
  `report/ledger.py`, docs/user/{run,nest,report}.md; job lifecycle from the EventBus)
- ☐ guided edge-seek and calibration procedures
- ☐ i18n: readable keys (Qt Linguist), cleaned-up translations, an importer for the original's
  files
- ☐ dark and high-contrast themes, SVG icons, accessibility
- ☑ LAN monitor (phone/tablet, read-only by default, no vendor cloud) (`mlaser/web`, stdlib,
  token, GET only, off by default; docs/user/lan_monitor.md)
- ☐ typed model and versioned native project format; `.chf` kept as interchange (byte-exact)
- ☐ docs split into user / dev / re; packaged help

## Outside software
- ☐ a hardware watchdog relay in the laser-enable chain (owner, with the documented wiring)
- ☐ the first hardware session, following docs/parity.md §8 and protocol.md §8
