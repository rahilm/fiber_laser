# Architecture: package layout and rules

This is the developer map of `mlaser/` after the Wave 1 structure moves (docs/ROADMAP.md). The
target it moves towards is docs/review/architecture.md §6; the reasons are in §1 and §5 there.

The rules below are checked, not just written down:

| Check | Command | Where |
|---|---|---|
| layering and Qt-free domain | `lint-imports` (import-linter, `[tool.importlinter]` in pyproject.toml) | CI job *architecture*, and `tests/test_architecture.py` in the default suite |
| typed core | `mypy` (`[tool.mypy]`) | CI job *architecture* |
| ports reachable through the profile | `tests/test_parity_ports.py` | default suite |
| old import paths still work | `tests/test_compat_shims.py` | default suite |
| enum values unchanged | `tests/test_protocol_enums.py` | default suite |
| behaviour unchanged | `python tools/make_goldens.py --check` (Python 3.12), `pytest -m parity`, `../parity/run_parity.sh` | CI (goldens), locally (Wine parity) |

## 1. Layers

Dependencies point downward only. Packages on one line are siblings that do not import each
other. Lazy (function-level) imports count like top-level ones.

```
 apps          ui            parity (capture/compare tooling)
 services      services      (documents + undo, commands, plugins, config, planned jobs)
 app support   diag          report          engrave
 machine       controller    (card client, simulator, job streamer, UI adapter)
               devices       machine (api.py: ControllerFacade, MachineState)
 operations    ops           (document operations: transforms, craft, ribbon CAM ops)
 NC            nc            (job compiler, process sequences, machine config, resume)
               project       (.mlz project files, autosave; sibling of nc)
 import        io            nest
 CAM           cam           (toolpath, leads, kerf, micro-joints, ordering, cleanup)
 motion        motion        (path planning over MotionItems, simulation, time estimate)
 formats       formats       cad
 parity fence  parity_ports  (bit-exact ports, ParityProfile)
 foundation    geom          protocol        toolpath        i18n
 model         model
```

| Package | Content |
|---|---|
| `model` | the document model (Line/Arc, Contour, Part, Craft, layers, glyphs, `GlyphKind`); typed fields with `extra` kept for opaque round-trip data ([project-format.md](project-format.md#typed-model)) |
| `geom` | one geometry library: `curves` (bulge arcs, splines, glyph expansion, chaining, bboxes), `contour` (arc-length points/tangents, sub-paths, start rotation, orientation, point-in-polygon) |
| `protocol` | the MCC100 wire protocol: `frames` (CExtModbus frame codec, FIFO item encoders, register map), `manu` (MainApp's ManuItem record), `enums` (IntEnum/IntFlag codes) |
| `toolpath` | `items`: the MotionItem types CAM produces and motion/NC/G-code/UI consume |
| `i18n` | string catalogue, `langfile` (the original's Lang/*.txt reader), importer |
| `parity_ports` | the bit-exact ports and `ParityProfile` (§3) |
| `formats`, `cad` | .chf (facade of the port), parameter XML/registry, ini and aux files; drawing primitives and text |
| `motion` | `interp` (plan_path, simulate, estimate_time); `planner`, `profile`, `smooth`, `curvetable`, `crtmath` are facades of the ports |
| `cam` | toolpath building, leads, kerf, micro-joints, cleanup, the rebuild's own ordering modes and the containment tree |
| `io`, `nest` | DXF/SVG/G-code import and export; nesting |
| `nc` | `compiler` (document -> FIFO job), `process` (ProcessConfig, ProcessSequencer), `machine`, `offline`, `resume` |
| `project` | the native `.mlz` project format: codec, schema, migrations, atomic save, `Autosave` ([project-format.md](project-format.md)) |
| `machine`, `devices` | `machine.api` (`MachineAPI`, `ControllerFacade`), `events` (event bus), `safety` (FSM, command policy), `preflight`, `demo`, `audit`/`logsetup`/`lifecycle`; peripheral device clients |
| `controller` | card client, simulator (+ `follower_sim`), `supervisor` (SafetySupervisor), `stream` (JobStreamer / ProcessStreamer), `interlocks`, alarms, startup, UI adapter (`ui_adapter` + `parts/`) |
| `diag`, `report`, `engrave` | diagnostics tools, production report window, bitmap engraving (they may use Qt) |
| `services` | the application services, Qt-free (§7): `document` (DocumentService, undo), `undo`, `commands`, `plugins` (PluginContext), `config` (ConfigService), `jobs` (PlannedJob, JobService), `production` |
| `ops` | Qt-free document operations the UI, services and diag share (`document`, `craft`, `cam`) |
| `ui`, `parity` | the PySide6 application; the parity capture/compare tooling |

### Rules (import-linter contracts)

1. **Dependencies point downward only** (the layers above).
2. **Domain packages import no Qt**: model, geom, protocol, toolpath, i18n, parity_ports, formats,
   cad, motion, cam, io, nest, nc, project, ops, machine, devices, controller and services must not import
   PySide6 or shiboken6.
3. **`parity_ports` imports only `geom`, `protocol` and `model`** (plus the standard library,
   numpy and ezdxf).

Not yet enforced by import-linter: "the app imports only services, never machine internals"
(the UI still talks to the controller object for jog/IO).  "Only the SafetySupervisor holds a
transport" is checked by `tests/safety/test_policy.py` (no `MotionController(` outside the
supervisor and the streamer, no `ctl.mc` in ui/diag/devices); see docs/SAFETY.md.

### Explicit ignores

None are left.  The last one, `parity_ports.mainapp.layer -> formats.params` (LayerView read the
layer defaults from the parameter registry), went in Wave 3: the defaults are injected through
`parity_ports.mainapp.layer.set_layer_defaults(provider)`.  `ConfigService` installs
`ConfigService.layer_defaults` when it is created; `nc.process` installs the plain registry
reader (`formats.params.layer_default_values`) when nothing else has.  Both give the registry
defaults exactly, so the goldens are bit-identical (`tests/test_layer_defaults_injection.py`).

## 2. Moved modules and compatibility shims

Every old import path still works for one release. A whole-module move leaves a two-line alias
(`mlaser/_compat.py`, `alias_module`): the old name is *the same module object* as the new one,
so private names, module globals and monkeypatching behave as before, and the first import emits
a `DeprecationWarning` naming the new path (silenced in the test suite, see `filterwarnings` in
pyproject.toml; `python -W error::DeprecationWarning` finds remaining users). Partial moves
forward the moved names through a module `__getattr__` with the same warning. The package itself
imports only new paths (checked by `tests/test_compat_shims.py`).

| Old path | New path | Why |
|---|---|---|
| `controller.frames` | `protocol.frames` | pure wire codec, used by nc, devices, diag |
| `controller.process` | `nc.process` | compile-time NC logic |
| `nc.stream` | `controller.stream` | the streamer drives the card client |
| `cam.items` | `toolpath.items` | shared vocabulary (motion and io depended on cam for it) |
| `io.geom` | `geom.curves` | one geometry library |
| `cam.geometry` (contour helpers) | `geom.contour` | one geometry library |
| `ui.facade.ControllerFacade`, `MachineState`, `OUTPUTS` | `machine.api` | controller imported the UI for its own interface |
| `devices.follower.make_follower_card_simulator` | `controller.follower_sim` | it subclasses the card simulator |
| `formats.lang` | `i18n.langfile` | formats and i18n imported each other |

The modules whose code moved into the parity fence (`motion.planner`, `motion.profile`,
`motion.smooth`, `motion.curvetable`, `motion.crtmath`, `formats.chf`, `formats.scio`, and the
ported parts of `motion.interp`, `cam.ordering`, `cam.cleanup`, `cam.geometry`, `nc.compiler`,
`nc.process`, `io.dxf_import`) are **not** deprecated: they stay the domain-facing names. They
re-export the port (private names too) and their entry points dispatch through the active
`ParityProfile`.

## 3. The parity fence (`mlaser/parity_ports/`)

A *port* reproduces a function of Mlaser v0.0.1.51_SP1 operation by operation. Its output is
pinned by the Wine parity run (docs/re/parity.md) and the goldens (tests/golden/). Rules:

* a port changes only together with a Wine parity re-run;
* new behaviour is a new `ParityProfile`, never an edit of a port;
* every ported entry point carries `@port("FUN_...", source="CADModule", ...)`
  (`parity_ports/_registry.py`), every port module a module-level `__port__` record listing the
  original functions it covers (helpers included);
* every `@port` symbol is reachable through the default profile, and the default profile points
  only at `@port` symbols (`tests/test_parity_ports.py`).

### Inventory

| Module | Original | Entry points (`@port`) | Profile slot |
|---|---|---|---|
| `cadmodule/planner.py` | CADModule CVelocityPlanning v1.3.23 (and the MotionCtrl copy) | `plan_velocity` | `motion` |
| `cadmodule/profile.py` | S-curve / trapezoid / accel profiles (FUN_101064b0, FUN_10106d40, ...) | `scurve_profile`, `make_profile` | `motion` |
| `cadmodule/interp.py` | MotionCtrl segInterp / segInterp_time / arcInterp (+ CADModule copies) | `seg_interp`, `seg_interp_time`, `arc_interp` | `motion` |
| `cadmodule/smooth.py` | CContoutSmooth (factory 0x100fa680) and CInterpMrg::Interp's load | `ContourSmoother`, `smooth_path_points` | `smoother` |
| `cadmodule/curvetable.py` | curve pieces, splineAnalyer samples, openNURBS evaluation | `CurveTable` | `smoother` |
| `crtmath.py` | msvcr100 (Wine) acos / atan / atan2 / hypot | (module record) | used by the smoother |
| `cadmodule/spline.py` | exact spline glyphs in the cutting pipeline (FUN_10093390, parity JC-07) | `SplineCurve`, `CurveRef` | `spline` |
| `mainapp/sampling.py` | MainApp sample loop and pulse quantisation (FUN_00462630 / FUN_00473bd0) | `Emitter`, `emit_samples`, `gap_flags`, `sample_speeds` | `sampling` |
| `mainapp/layer.py` | the GP.* layer parameter block (`LayerView`, `Stage`); defaults injected (`set_layer_defaults`) | (module record) | used by the encoder |
| `ncmodule/manu.py` | NCModule ManuItem -> FIFO items (FUN_10043c20) | `ManuEncoder` | `process` |
| `cadmodule/ordering_fast.py` | bit-identical drop-ins of `local_optimal_elems`, `containers_last_faithful`, `import_sort` (grid / numpy / STRtree filters, reference decisions; docs/dev/PERFORMANCE.md §2) | `import_sort`, `local_optimal_elems`, `containers_last_faithful` | `ordering` (default) |
| `cadmodule/ordering.py` | sort dispatcher modes 5/6/7 (FUN_100b57c0, 0x1003d300, 0x1003c480, CRingSort 0x1003f690), start point 0x10071f80 | `import_sort`, `local_optimal_elems`, `containers_last_faithful`, `ring_sort`, `choose_start` | `ordering` |
| `circlefit/fit.py` | CircleFitDLL CCircleFit::mainFit | `fit_arcs` | `arcfit` |
| `dxfparse/rules.py` | DxfParse parser rules, CADModule entity -> glyph handlers, chaining, placement | `collect`, `entity_glyphs`, `chain_glyphs`, `contour_of_chain` | `dxf` |
| `cadmodule/chf.py`, `cadmodule/scio.py` | the .chf codec (document load/save, graph and glyph records) and CSerialEng | `loads_chf`, `dumps_chf`, `load_chf`, `save_chf` | `chf` |

`ring_sort` is marked `status="observed"`: the spiral is the binary's, but by default its
tolerance is the dimensionally consistent `0.1*sqrt(W*H/n)` (set `RING_TOL_FAITHFUL` on the port
module for the binary's `0.1*W*H/n`).

Two bit-exact pieces live in the foundation because the ports build on them; they carry
`__port__` records too: `protocol.frames` (NCModule CExtModbus codec) and
`geom.curves.flatten_bspline_params` (splineAnalyer sample table FUN_100953d0). The ManuItem
record (`protocol.manu`) and the codes in `protocol.enums` are data, not algorithms.

Still outside the fence (candidates for a later wave): the band sort of modes 1-4, the containment
tree (`cam.ordering.build_tree`), the MainApp process sequences (`nc.process.ProcessSequencer`)
and the speed curves.

### ParityProfile

`parity_ports/profile.py` defines one `Implementation` per slot (motion, smoother, spline,
sampling, process, ordering, arcfit, dxf, chf). Each slot has a `Protocol` interface listing the
names it must provide; targets are `"module:attribute"` strings resolved on first use. The default
profile is `ORIGINAL` (name `"original"`).

The domain code asks the active profile wherever an algorithm is chosen, e.g.
`motion.interp.plan_path` (`plan_velocity`, `smooth_path_points`), `nc.compiler.compile_items`
(`Emitter`, `emit_samples`, `ManuEncoder`), `io.dxf_import.import_dxf` (`collect`,
`chain_glyphs`, `contour_of_chain`). The old names of entry points (`cam.ordering.choose_start`,
`formats.chf.load_chf`, `motion.planner.plan_velocity`, ...) are `dispatch()` functions that call
the active profile, so existing callers go through it too.

Adding a modern algorithm:

```python
from mlaser.parity_ports import ORIGINAL, register_profile, use_profile

fast = ORIGINAL.derive(
    "fast-order", "numpy ordering kernels",
    ordering=ORIGINAL.ordering.with_(import_sort="mypkg.order:import_sort"))
register_profile(fast)
with use_profile("fast-order"):
    job = compile_job(doc)
```

Optional profiles live in `parity_ports/profiles/` (one module each, registered on import;
`get_profile(name)` imports the package on demand): `original-reference` (the statement-level
ordering references the default runs as fast drop-ins), `modern-dxf`, `modern-ruckig`
(`warning=`: logged when activated, not hardware-validated), `modern-ortools` (an extension
*option*, `ParityProfile.options` / `option()`, for sort mode 20 which has no original),
`native-sampling` (the optional C kernel in `native/`). They are not ports, so
tests/test_parity_ports.py does not ask them for `__port__` records.

The active profile is a process-wide global (worker threads see it); `use_profile` switches it
for a block. The goldens and the parity run always use `"original"`.

## 4. Typing and enums

* `[tool.mypy]` checks `protocol`, `geom`, `parity_ports` and `nc` (the other packages are not
  checked yet; `follow_imports = "silent"`). The remaining findings carry
  `# type: ignore[code]` comments: the baseline to burn down (ROADMAP "Code health").
* `protocol.enums` names the protocol codes: `Func`, `Op`, `MailboxOp`, `SysCmd`, `WaitMode`,
  `ZfState`, `FifoCtrl`, `ManuClass`, `SysSub`, `PierceStage`, `AxisAlarmBit` / `AxisAlarm`
  (IntFlag), `AlarmWord1Bit`, `AlarmWord2Bit`; `model.GlyphKind` and `chf.GraphKind` the glyph
  and graph kinds. The old constants (`OP_STEP`, `CLS_PEAK`, `GLYPH_SPLINE`, ...) are these
  members, so the values on the wire and in files are unchanged.

## 5. Python version

Python 3.12 and 3.13 are supported and tested in CI. PySide6 6.12 aborts on 3.11 ("Fatal Python
error: none_dealloc": it releases one reference to `None` too many, which 3.12's immortal `None`
hides), so 3.11 is not supported.

The goldens are pinned to CPython 3.12: they depend on 3.12's float `sum()` (compensated
summation since 3.12; 3.11's plain `sum()` made a random job plan differently). 3.13 keeps the same
`sum()` and matched bit for bit when this was written, but the reference interpreter stays 3.12
(tests/golden/manifest.json); CI runs the golden check on 3.13 as informative only.

## 6. Checklist for a change

1. `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q` (includes the import contracts)
2. `.venv/bin/mypy` and `.venv/bin/lint-imports`
3. `python tools/make_goldens.py --check` on Python 3.12 (bit-identical, or a reviewed new profile)
4. a change under `mlaser/parity_ports/`: `pytest -m parity` and `../parity/run_parity.sh`
5. `python tools/command_coverage.py`: every ribbon command bound, none raising, no duplicate
   registration (0 / 0 / 0)

## 7. Services layer (Wave 2)

`mlaser.services` sits between the Qt application and the domain.  It imports no Qt (import
contract), so every service has unit tests without a display; the application injects the two
Qt-specific pieces (a "post to the GUI thread" callable and a timer).

| Service | What it owns | Replaces |
|---|---|---|
| `DocumentService` (`document.py`, `undo.py`) | the open document and its path; `edit(fn, label, parts=scope)` transactions with rollback; `replace(doc)`; command undo/redo | `MainWindow._snapshot` / `_edit` / `_refresh` and the 50 deep copies |
| `CommandRegistry` (`commands.py`) | one handler per command id; duplicates raise and are recorded; failures logged | `QAction.triggered` rebinding by the extensions |
| `PluginContext` (`plugins.py`) | the plugin API, hooks, providers, entry-point discovery | `register(window)` and monkeypatching (docs/dev/PLUGINS.md) |
| `ConfigService` (`config.py`) | the typed parameter store generated from the registry, platformdirs persistence, XML import/export, the machine profile | `window.settings`, `Machine()` defaults, `diag_state.json`, `machine_ops` dict, data-dir schemes |
| `JobService` (`jobs.py`) | `PlannedJob`s: compiled in a worker, debounced, cached by input hash; the preview and Start / Dry Cut / Frame use the same plan | the canvas' own `build_toolpath`, compiles on the GUI thread in the controller |
| `ProductionService` (`production.py`) | job statistics, counters, planned-quantity interlock (also a start guard), maintenance | the logic in `ui/ext_diag.py` |
| procedures (`procedures/`) | guided procedures as step machines over the MachineAPI (edge seek, follower calibration, focus homing, laser test, commissioning); abort = `safe_halt`; simulator dry-run rigs | the edge-seek forms, the FTC dialog buttons |
| `settings_import.py` | preview / apply of an original install's settings (never writes the install) | – |
| `AlarmHistory` (`alarm_history.py`) | alarms and faults with raise / clear times, persisted | the alarm tab's log lines |

### Undo

An edit is a `PartsEdit` command: copies of the parts in its scope taken before `fn` runs, the
part order (by uid) before and after, and the document's `layers` / `extra`.  Undo and redo swap
the stored objects with the live ones (no copy).  Parts and contours carry a stable `uid`
(`mlaser.model`, no part in equality).  The `.chf` round-trip records (`Part.extra['chf']`,
`['_chf_snapshot']`, `Contour.extra['chf']`, source glyphs, `Document.extra['chf']`) are
registered in the `RoundTripTable` side table and shared by every copy (a deepcopy memo), never
copied per edit; the codec copies before it writes, so they are immutable.  The stack is bounded
by count (100) and estimated retained bytes (256 MiB).

### Planned jobs

`build_request(mode, docs, ctl, machine, cam_params)` captures on the GUI thread: a private
snapshot of the document, the controller's placement (`place`), the head position (the start and
sort origin), the layers, the laser-enabled state and the ParityProfile.  The worker runs
`nc.compiler.plan_toolpath` (the first half of `compile_job`, published to the canvas at once)
and `compile_items` (with a `progress` hook that cancels).  `PlannedJob.frames()` is
`Job.frames()`, and `controller.stream.WireJob` now carries the ManuItem grouping so the streamer
sends exactly these frames.  `ControllerSubmitter` implements the `JobSubmitter` protocol
(`start_job(plan)`) for today's controllers; the Safety track's MachineAPI takes it over.

### Configuration

`ConfigService.schema` has one `Setting` per registry entry (type, unit, range, options,
default, section, file); `values` holds only what the user set.  `machine()` builds the
`nc.Machine` from the profile: `bench` (default: `Machine()` plus every changed parameter,
applied the way `Machine.from_params` applies it; nothing changed = exactly `Machine()`) or
`params` (everything from the parameter set, chosen after `import_dat`).  The window's
`settings` is `ConfigService.mapping()`, a dict view, so the Options dialogs, diag `ParamSet`
and the plugins write validated values into the one store.
