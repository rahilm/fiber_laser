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
- ☑ `ConfigService` generated from the 1,129-entry registry: typed, units, ranges, platformdirs;
  the machine profile feeds `Machine` (`mlaser.services.config`, typed Options dialogs,
  HardPara/ManuPara XML import/export, a GUI option changes the compiled job); it hands the
  layer defaults to `parity_ports.mainapp.layer` by injection (Wave 3 Machine track; the last
  import-linter ignore `parity_ports.mainapp.layer -> formats.params` is gone, goldens identical)
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
- ☑ native kernel (C, `mlaser/_native`) for sample → quantise → encode, the default when built
  (self-check at first use, Python port as reference and fallback, `MLASER_NATIVE`); 0
  differences on 326 golden jobs; cibuildwheel wheels and bundles in package.yml, golden check
  with the kernel on and off on every OS in ci.yml; ☐ the workflows' first run on GitHub
- ☑ optional modern planner profile (Ruckig, `modern-ruckig`), off by default; ☐ hardware
  validation

## Wave 3: modern workflow (Stage 3, built on the services)
- ◐ task workspaces Design → Process → Nest → Run → Report (+ Machine), a persistent run bar,
  a Ctrl+K command palette, and an optional classic ribbon: ☑ the shell (`mlaser/ui/shell/`,
  docs/user/shell.md): rail with the six workspaces (Ctrl+1..6) hosting the Process, Nest, Run,
  Report and Machine workspaces of the other tracks, top bar (file + unsaved marker, command
  search, safety chips, user mode, language), Safety's run bar in every workspace,
  `ctx.add_workspace` with slots/options (docs/dev/PLUGINS.md §5), Ctrl+K palette over all
  commands (recents first), classic ribbon view without elided labels, reset layout, 1280×800
  and 150 % layouts, first-run welcome sheet; ☑ Machine workspace: `ui/workspaces/machine.py`,
  monitors as panels, typed hardware settings, alarm history
- ☑ material/technology database: material × thickness × gas × nozzle (× power class); versioned;
  diff against factory values; JSON and LayerPara.xml import/export; a labelled example set
  (`services/technology.py`, Process workspace `ui/workspaces/process.py`, docs/user/process.md);
  applying a technology compiles exactly like typing the values (tests/test_technology.py)
- ☑ properties panel instead of modal dialogs; units; live previews (`ui/properties/`: transform,
  layer, leads with a live diagram, kerf, micro-joints, start point, cooling points; every edit
  one undo step; the dialogs stay reachable)
- ☑ preview with a scrubber, plus a time and cost card (`services/preview.py` over the PlannedJob
  Start streams, `services/costing.py`, `ui/preview_scrubber.py`)
- ◐ touch-first Run screen; Operator, Engineer and Service modes (☑ Run screen:
  `ui/workspaces/run.py`, docs/user/run.md; ☑ the modes: `services/usermodes.py`, command
  `min_mode`, rail and palette filtered, no parameter editing and 48 px targets for Operator,
  optional PBKDF2 PINs set in Service mode)
- ☑ graphical nest workspace; job queue; production reports; remnant stock
  (`ui/workspaces/{nest,run,report}.py`, `services/{queue,remnants,nesting,operator}.py`,
  `report/ledger.py`, docs/user/{run,nest,report}.md; job lifecycle from the EventBus)
- ☑ guided edge-seek and calibration procedures (Machine track: `services.procedures` step
  machines with abort = `safe_halt` and a simulator dry run; edge seek 3/4-point, disc and
  two-point with outline review and undoable apply; follower calibration with the curve; focus
  homing; laser power test with arming; first-connection commissioning checklist; settings import
  from an original install; docs/user/machine.md)
- ◐ i18n: readable keys (Qt Linguist), cleaned-up translations, an importer for the original's
  files. Done: readable ids, the importer, Qt Linguist `.ts`/`.qm` round trip, `tools/i18n_check.py` in
  CI, the bundled cutting-text font (docs/dev/i18n.md); open: cleaned-up translations (none ship yet)
- ☑ dark and high-contrast themes, SVG icons, accessibility (light / dark / high contrast with
  palette + QSS, persisted; canvas colours follow the theme with layer colours ≥ 3:1 and
  distinguishable; accessible names on rail/toolbar/top-bar controls, a fixed focus order,
  keys documented from the registry in docs/user/keyboard.md, banners and chips never colour
  alone)
- ☑ LAN monitor (phone/tablet, read-only by default, no vendor cloud) (`mlaser/web`, stdlib,
  token, GET only, off by default; docs/user/lan_monitor.md)
- ☑ typed model and versioned native project format; `.chf` kept as interchange (byte-exact):
  typed contour craft / micro-joints / cooling points / source glyphs / raster parts with
  `LegacyExtra` shims, `.mlz` with schema, migrations, atomic save, autosave and recovery
  (docs/dev/project-format.md)
- ☑ docs split into user / dev / re; packaged help (help browser with search, link check in CI)

## Outside software
- ☐ a hardware watchdog relay in the laser-enable chain (owner, with the documented wiring)
- ☐ the first hardware session, following docs/re/parity.md §8 and protocol.md §8

## Review 2 (2026-10-20): safety fixes, responsiveness, shop workflow
Source: the second review (features, performance, code health), summarised in the session. Quoting is
deliberately out of scope for now.

### Wave A: safety (first)
- ☐ one controller start gate: no Stop since the request, card idle, head at the plan start, no
  breakpoint conflict, interlocks clear (closes P0-2 Stop vs compiling Start, P0-3 stale head
  position, start-during-jog, Backward/Forward travel after a jog)
- ☐ P0-1 extended IO (ports 11–26): arming classification, FIFO emission detection, halt words;
  simulator models the IO split
- ☐ P0-4 interlocks (gas-low, door, laser alarm) and the heartbeat output configurable through
  ConfigService, Options and the commissioning wizard
- ☐ disarm after the queue finishes; breakpoint/document lock across fault reset;
  `replace_document` locked during a job; FIFO pause mode latches e-stop; resume re-pierce uses the
  layer table; laser-source guard per device (no simulator hijack); raw laser query guarded;
  guarded-output mask fails closed
- ☐ plugin trust model: no public bypass of the guard, audited arming, opt-in allowlist
- ☐ untrusted input limits: .mlz zip-bomb and depth limits, DXF/SVG expansion caps off the GUI
  thread, LAN monitor timeouts and connection cap
- ☐ the Run screen's controls never hidden by docks (the old Nest panel case)

### Wave B: responsiveness and throughput
- ☐ plan compile in a separate process; `sys.setswitchinterval` interim; scoped edit snapshots;
  linear selection sync; no duplicate estimate pass; LAN monitor SVG/bbox cache
- ☐ streamer: no automatic full GC during jobs, heartbeat from its own thread (motion bound kept)
- ☐ native smoother/curve tables/crtmath, packed arrays from the C kernel, parallel compile per
  chain (bit-identical), ring sort O(n log n)
- ☐ packaging: ortools.dll on Windows, no host CUPS/gnutls in the Linux bundle, size trims
- ☐ tests: pytest-xdist in CI (timing tests grouped), `.python-version` 3.12, README numbers

### Wave C: shop workflow (quoting excluded)
- ☐ part recognition and DXF repair on import
- ☐ order intake: multi-file and CSV (quantity, material, thickness, customer, due date)
- ☐ material check against the technology, "load this sheet" card
- ☐ nesting quality in the Nest workspace: common line, part-in-part, mirror/rotation, scrap
  cut-up, remnant separation cut
- ☐ lead-in collision check, automatic lead placement, tip-avoiding cut order
- ☐ simulator/practice runs excluded from production statistics; guided practice mode
- ☐ CLI (compile, nest, estimate) and an automation API whose queued jobs need operator approval
- ☐ notifications (sheet done, alarm, waiting for sheet, maintenance due): ntfy / e-mail / webhook
- ☐ part labels, part-ID etching, QR job tickets, remnant labels
- ☐ project hygiene: SECURITY.md (safety reports), CONTRIBUTING, CHANGELOG, CODEOWNERS, CI guard on
  golden and parity-port changes; command palette "no matches"
