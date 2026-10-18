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
- ☐ break the cycles:
  - `mlaser/protocol/` (frame codec, enums)
  - `mlaser/geom/` (one flatten/spline/bbox library)
  - `process.py` → `nc/`
  - item types → `toolpath/`
  - `ControllerFacade` → `machine/api.py`
  - re-export shims left at the old paths
- ☐ `parity_ports/` fence with `@port` markers and `ParityProfile` selection
- ☐ import-linter contracts in CI
- ☐ IntEnum/IntFlag for opcodes, kinds and alarm bits; mypy on the core

## Wave 2: in parallel
### Safety (Stage 1, highest priority)
- ☐ `machine/SafetySupervisor`: an FSM that enforces arming on every outgoing command; connect
  starts disarmed; `--real` fails loudly instead of falling back
- ☐ `safe_halt()` on every exit path: worker `finally`, disconnect, closeEvent, atexit, signals,
  excepthook
- ☐ streamer process supervision: heartbeat, parent check, self-halt, `freeze_support`
- ☐ link loss detected within about 150 ms, then a latched FAULT state; prefill capped at 1–1.5 s;
  no FIFO auto-restart (recovery goes through breakpoint/resume with a re-pierce)
- ☐ pre-flight check before Start/Frame/Resume/move: travel bounds, homed, armed, layers defined
  (undefined layer is an error), open contours, card limits read back
- ☐ interlock monitor for follower, gas and laser-source alarms; hold-to-jog dead-man; an
  asynchronous Stop that sends the halt first; global Hold/Stop shortcuts
- ☐ "PC alive" heartbeat output for an external watchdog relay (documented wiring)
- ☐ rotating log file, structured audit events, crash handler; swallowed safety failures become
  FAULT plus a banner
- ☐ UX safety P0s:
  - ARMED / DRY-RUN / SIMULATOR banner
  - alarm banner with remedy text
  - no file open or new document during a job
  - run controls always visible
  - startup prompts as dialogs, not log lines
- ☐ pessimistic simulator mode plus a fault-injection API; `tests/safety/`

### Services (Stage 2b)
- ☐ typed `MachineAPI` plus an event bus (job, alarm, link events); split UiController; Demo and
  real controllers implement the full API (removes the import-linter ignore
  `controller.ui_adapter -> PySide6`: the Qt signal bridge moves to the app)
- ☐ immutable `PlannedJob` compiled in a worker (progress, cancel, cache); the preview renders it
  and Start streams exactly it; the streamer uses `Job.frames()`
- ☐ `ConfigService` generated from the 1,129-entry registry: typed, units, ranges, platformdirs;
  the machine profile feeds `Machine`
- ☐ command-based undo with stable IDs; round-trip blobs kept in an immutable side table
- ☐ `PluginContext` API; port ext_* onto it; command coverage keeps all 246 commands bound
- ☐ business logic out of the UI (docops, ext handlers → services; removes the import-linter
  ignore `diag.batch -> ui.docops`)

### Performance and libraries (Stage 4)
- ☐ background, debounced toolpath; numpy ordering kernels with the same formulas (goldens)
- ☐ raster engraving as a first-class object; level-of-detail on the canvas
- ☐ packed numpy step arrays plus shared memory to the streamer
- ☐ library swaps and options:
  - svgelements for SVG
  - ezdxf "modern import" mode
  - pyclipper kerf fallback
  - OR-Tools ordering option
  - fontTools + uharfbuzz text (removes the import-linter ignores `cad.text -> PySide6` and
    `io.dxf_import -> PySide6`, the Qt font-engine fallback for TEXT outlines)
  - modern nesting engine (licence permitting)
- ☐ PyInstaller one-folder builds per OS in CI, signing hooks, first-run network self-test
- ☐ optional native kernel (Cython/Rust) for sample → quantise → encode, Python kept as reference
- ☐ optional modern planner profile (Ruckig), off by default, hardware validation required

## Wave 3: modern workflow (Stage 3, built on the services)
- ☐ task workspaces Design → Process → Nest → Run → Report (+ Machine), a persistent run bar,
  a Ctrl+K command palette, and an optional classic ribbon
- ☐ material/technology database: material × thickness × gas × nozzle; versioned; diff against
  factory values
- ☐ properties panel instead of modal dialogs; units; live previews
- ☐ preview with a scrubber, plus a time and cost card
- ☐ touch-first Run screen; Operator, Engineer and Service modes
- ☐ graphical nest workspace; job queue; production reports; remnant stock
- ☐ guided edge-seek and calibration procedures
- ☐ i18n: readable keys (Qt Linguist), cleaned-up translations, an importer for the original's
  files
- ☐ dark and high-contrast themes, SVG icons, accessibility
- ☐ LAN monitor (phone/tablet, read-only by default, no vendor cloud)
- ☐ typed model and versioned native project format; `.chf` kept as interchange (byte-exact)
- ☐ docs split into user / dev / re; packaged help

## Outside software
- ☐ a hardware watchdog relay in the laser-enable chain (owner, with the documented wiring)
- ☐ the first hardware session, following docs/parity.md §8 and protocol.md §8
