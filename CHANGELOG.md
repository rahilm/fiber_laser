# Changelog

All notable changes to mlaser are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.1.0rc1`, `0.1.0rc2`, ..., `0.1.0`; tags
`v0.1.0-rc1` style, see [docs/dev/RELEASE.md](docs/dev/RELEASE.md)).

Changes to safety behaviour, to the compiled output (the goldens) or to the parity fence are
always listed, with the reason. Machine-safety fixes are marked **Safety**.

## [Unreleased]

### Added

- Command line: `mlaser compile | estimate | nest | validate | convert | queue add | queue list`
  (also `python -m mlaser ...`, `mlaser-cli`), JSON output with `--json`, documented exit codes,
  the GUI's services, no card connection (docs/user/automation.md).
- Automation API (off by default): local REST + WebSocket, read-only status and events, queue
  intake whose jobs wait for the operator's approval on the Run screen; token, localhost by
  default, the LAN monitor's limits, OpenAPI description; no remote start, stop, hold, arm,
  move or approval (docs/dev/API.md).
- **Safety**: jobs submitted from outside (API, CLI) wait in a separate approval list
  (`pending.json`) and become queue entries only when the operator approves them; a submission
  carries no cutting parameters.
- Notifications: sheet done, queue waiting, alarm / fault, job finished, maintenance due over
  ntfy, e-mail (SMTP) and webhook, per-event settings, rate limits, secrets in the keyring or a
  0600 file (docs/user/notifications.md).
- Labels: part labels on roll and A4 sheet sizes, QR job tickets per queued sheet, remnant
  labels (PDF), part-ID etching on a marking layer (docs/user/labels.md).
- Practice mode: six guided lessons on the simulator with progress (docs/user/practice.md).

### Changed

- Report: every job record has a source (real / simulator / dry run / practice); the figures
  count real jobs by default (Runs filter), dry runs are recorded too. The device counters
  (processing hours, maintenance), the planned quantity and `TotalReport.txt` count real jobs
  only (review 2: a simulator sheet was recorded as production).

- `SECURITY.md`: private reporting of security and machine-safety issues (GitHub private
  vulnerability reporting).
- `CONTRIBUTING.md`: setup, the contract checked on every commit, parity and bit-exact rules,
  when goldens may be regenerated, rules for safety-relevant changes, recommended branch
  protection.
- `.github/CODEOWNERS`: owner review for the parity fence, the goldens, the controller, the
  machine safety layer and docs/SAFETY.md.
- CI: the parity guard (`.github/workflows/parity-guard.yml`, `tools/parity_guard.py`): a pull
  request that changes `tests/golden/` or `mlaser/parity_ports/` needs the label
  `parity-change` or a `Parity-Change:` commit trailer.
- CI: the default suite runs on pytest-xdist (`-n auto --dist loadgroup`); the safety and
  simulator timing tests stay together on one worker (xdist group `timing`).
- `.python-version` (3.12, the reference interpreter of the goldens).
- `tools/bench_native.py --smooth`: compile figures with the default machine (smoothing on).
- **Safety**: the gas-low, door and laser-source-alarm interlocks and the PC-alive heartbeat are
  configurable (`safety.*` in the ConfigService; Machine > Hardware settings > Safety I/O,
  Engineer and Service mode) and reach the controller at start-up and after every change (a
  change waits while the machine is armed or a job runs).  Unconfigured interlocks are named on
  the Run screen, warned about in the pre-flight of a laser job on a real machine, and flagged
  by the commissioning checklist, which also has a wiring test.  Defaults stay off.
- **Safety**: third-party (entry-point) plugins are opt-in (Options+ > Plugins, Service mode,
  `plugins.allowlist`), load after the safety UI, cannot register `run.*` / `shell.*` /
  `workspace.*` commands, and need granted machine authority to arm, reset faults or change
  safety settings.  Trust model in docs/dev/PLUGINS.md §6.
- **Safety**: limits for untrusted files: `.mlz` (unlisted members ignored, per-member and total
  size, entry count, compression ratio, JSON depth), DXF (INSERT / MINSERT expansion), SVG
  (`<use>` expansion); imports run off the GUI thread with progress and Cancel.
- LAN monitor: socket timeouts, a connection cap (per client too), a per-client rate limit; the
  sheet picture is cached and rebuilt only on change.
- Command palette: "No command matches" instead of an empty list.

### Changed

- README and docs/dev/PERFORMANCE.md: suite times re-measured; compile figures labelled
  "smoothing off" (the emitter in isolation) and "default profile" (smoothing on, what the
  application compiles).

### Fixed

- **Safety**: the Run screen's STOP, HOLD, START and Arm can no longer be squeezed out of view by
  docked panels (the classic Nest dock at 1440 x 900): the Run workspace hides other docks.
- A plugin pre-claiming `run.stop` no longer stops the application from starting.

## [0.1.0rc1]

First release candidate. **Not validated on any real machine**: every part that talks to the
card, the laser, the gas or the follower is tested against mlaser's own simulator only, and the
file formats and computations against the original program under Wine (README, "Safety
warning").

### Added

- **File formats**: `.chf` drawings read and written byte-exact (v5 / v9), machine and layer
  parameter XML (`HardPara.xml`, `ManuPara.xml`, `LayerPara.xml`), DXF (original rules and an
  ezdxf "modern" mode), SVG (svgelements), G-code; the native `.mlz` project format with a
  schema, migrations, atomic save, autosave and recovery (docs/dev/project-format.md).
- **CAM**: leads, kerf (shapely; opt-in pyclipper fallback), micro-joints, cooling points,
  ordering (the original's sort modes as bit-exact ports; opt-in OR-Tools mode 20), common
  edge, fly-cut, raster engraving as one object.
- **Motion and NC**: the original's look-ahead velocity planner and contour smoother as
  bit-exact ports (opt-in Ruckig profile, not hardware-validated), 1 ms sampling, pulse
  quantisation, step items and the MCC100 FIFO frames; packed wire storage; an optional C
  kernel for the emitter loop (bit-identical to the Python port, self-checked at first use).
- **Machine control**: MCC100 card client (UDP/TCP), a card simulator with a pessimistic mode
  and fault injection, job streaming in a supervised child process, breakpoint / resume,
  homing, jog, coordinate systems, edge seek, follower and laser-source devices, XHC pendant.
- **Safety** (docs/SAFETY.md): connect disarmed; an arming state machine that classifies every
  outgoing card command; `safe_halt()` on every exit path; link-loss detection with a latched
  FAULT; pre-flight checks (travel, homed, armed, undefined layers, geometry); follower, gas,
  laser-source and door interlock monitor; hold-to-jog dead-man; asynchronous Stop; global
  Hold / Stop keys; "PC alive" heartbeat output for an external watchdog relay; audit log and
  crash handler.
- **Application**: task workspaces (Design, Process, Nest, Run, Report, Machine), a persistent
  run bar, Ctrl+K command palette, optional classic ribbon; Operator / Engineer / Service
  modes; properties panel; preview with scrubber and a time and cost card; technology
  database; graphical nesting (built-in and spyrrow), job queue, production reports and
  remnant stock; guided procedures (edge seek, follower calibration, focus homing, laser
  power test, commissioning checklist, settings import from an original install); read-only
  LAN monitor; light, dark and high-contrast themes; packaged help.
- **Configuration**: `ConfigService`, typed from the original's 1,129-entry parameter registry,
  with platform directories and XML import / export.
- **Plugins**: `PluginContext` API and `mlaser.plugins` entry points (docs/dev/PLUGINS.md).
- **Languages**: English catalogue; other languages are imported from the user's own Mlaser
  install (none are shipped).
- **Packaging**: wheels (CPython 3.12 / 3.13; Linux x86_64 and aarch64, Windows AMD64, macOS
  arm64) with the C kernel, an sdist, and PyInstaller one-folder bundles for Windows, Linux and
  macOS.
- **Tests and CI**: unit, ui, sim and safety suites; frozen goldens checked bit-exactly on
  CPython 3.12 with the C kernel on and off; the Wine parity suite (local, needs the external
  harness); import-layer contracts, mypy on the typed core, the i18n check and a documentation
  link check.

[Unreleased]: https://github.com/rahilm/fiber_laser/compare/v0.1.0-rc1...HEAD
[0.1.0rc1]: https://github.com/rahilm/fiber_laser/releases/tag/v0.1.0-rc1
