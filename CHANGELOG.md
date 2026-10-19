# Changelog

All notable changes to mlaser are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`0.1.0rc1`, `0.1.0rc2`, ..., `0.1.0`; tags
`v0.1.0-rc1` style, see [docs/dev/RELEASE.md](docs/dev/RELEASE.md)).

Changes to safety behaviour, to the compiled output (the goldens) or to the parity fence are
always listed, with the reason. Machine-safety fixes are marked **Safety**.

## [Unreleased]

### Added

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

### Changed

- README and docs/dev/PERFORMANCE.md: suite times re-measured; compile figures labelled
  "smoothing off" (the emitter in isolation) and "default profile" (smoothing on, what the
  application compiles).

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
