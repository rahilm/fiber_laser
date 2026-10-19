# Mlaser rebuild: architecture, library and UX review (combined)

Detailed reports, with evidence, measurements and file/line references:
- [architecture.md](architecture.md): layering, safety, code quality, performance, target architecture, migration path
- [libraries.md](libraries.md): every subsystem against mature open-source options, licences, whether to build on another platform
- [ux.md](ux.md): workflows driven with click counts, 56 screenshots (in `work/review/shots/`), competitive matrix, wireframes

Goal: modern, well-supported, feature-rich software to control the owner's fiber-laser cutter,
maintainable over years and usable by others, while keeping the parity guarantees against the
original (docs/re/parity.md) available.

## Verdict

The core pipeline is now a faithful, verified port. The original's own code was run under Wine
and compared at every stage outside the licence-gated loop. The rebuild is not yet safe to run a
real machine unattended, and its structure won't scale to a maintained product without a
refactor. The good news: none of this needs the verified ports to change. Safety, services and
UX layer on top of them.

## What to do, in order

### Stage 0: lock in what exists (S, days)
1. **git + CI + licence decision.** Put the project under git. Add GitHub Actions on Linux,
   Windows and macOS (non-parity suite) and a lock file.
   *Done in this session:* dependencies declared (core segno/fontTools/Pillow; extras devices,
   plots, test, all).
2. **Golden artefacts.** Capture wire streams, `Job.frames()`, toolpath orders, `.chf` bytes and
   planner samples from today's code into `tests/golden/`. Together with the Wine parity run they
   become the contract every later change must keep.
3. **Test hygiene.** Add pytest markers (unit/ui/sim/parity/golden/safety). The default run skips
   parity (about 54 s instead of 177 s). Fix the flaky 1-pulse resume test.
4. **Vendor assets.** 478 icons and 10 language files come from the vendor. Replace them before
   any public release; this blocks redistribution.

### Stage 1: safety, before anyone connects a real machine (M, 2–3 weeks), architecture §2
All of these were measured on the simulator (architecture.md §2.1):
- **safe_halt on every exit path.** Disconnect, window close, exception, crash: today the card kept
  cutting 527 mm with PWM on after a mid-job disconnect.
- **Supervised streamer process.** Add a heartbeat and a parent check; it kept streaming after the
  UI was killed.
- **Fast, latched link-loss detection.** About 150 ms instead of 4–5 s, a FAULT state that blocks
  Start, and the prefill capped at 1–1.5 s.
- **No silent FIFO auto-restart.** It currently re-fires the beam mid-contour without a pierce.
- **SafetySupervisor.** Arming is enforced on every outgoing command, connect starts disarmed,
  an ARMED/DRY-RUN/SIMULATOR banner is shown, and `--real` fails loudly instead of falling back to
  the demo controller.
- **Pre-flight check before Start/Frame/Resume.** Covers travel bounds (a job at x = 50 m compiles
  today), homed state, defined layers (an undefined layer means 100 % power today), open contours
  and card limits.
- **Persistent log and audit trail; alarms as banners with remedies.** Today they are log lines,
  and the status bar says Ready while an alarm is latched.
- **UX P0s that are safety issues:** opening a file mid-job replaces the document, Start/Pause/Stop
  scroll off-screen at 150 % scaling, and there are no keyboard shortcuts for Hold/Stop.
- **Hardware:** a "PC alive" watchdog relay in the laser-enable chain. Software cannot guarantee a
  safe state when the PC dies.

*Done in this session:* the autosave crash (micro-joint pairs in the `.chf` writer) and the
unformatted estimate message.
*Done in this session:* the 14.7× compile speed-up. `ContourSmoother._locate` no longer rebuilds a
list per sample. On autosave.chf compile went from 36.0 s to 2.45 s, the wire output is
byte-identical, and a regression test guards it.

### Stage 2: structure (M each), architecture §6–§8
- **Break the two import cycles.** Add `protocol/` (frame codec), `geom/` (one flatten/spline
  library) and `machine/api.py`, enforced with import-linter.
- **Fence the bit-exact ports** in `parity_ports/` behind a `ParityProfile`, so modern algorithms
  arrive as new profiles and never edit a verified port.
- **Services layer:** a typed `MachineAPI` plus an event bus (replacing log-regex job detection
  and 26 `hasattr` probes); an immutable `PlannedJob` compiled in a worker, so the preview is
  exactly what gets cut; a `ConfigService` generated from the 1,129-entry registry (GUI hardware
  options currently never reach the motion code); command-based undo with stable IDs.
- **A `PluginContext`** to replace extensions rebinding 170 commands by monkeypatching.

### Stage 3: modern workflow (the big user-facing win), ux §3–§4
- **Task-based workspaces:** Design → Process → Nest → Run → Report (+ Machine), with a
  persistent run bar and a Ctrl+K command palette, so all 246 commands stay reachable. An optional
  "classic ribbon" keeps the original layout.
- **Material/technology database** (material × thickness × gas × nozzle → cut/pierce parameters,
  leads, kerf, joints; versioned, diff against factory values). This replaces a 160-row register
  grid.
- **Properties panel** that follows the selection, with units and live previews, instead of modal
  dialogs.
- **Preview with a scrubber and a time/cost card.**
- **Touch-first Run screen:** progress, ETA, current contour, live values, alarms with remedies.
- **Graphical nest workspace**, then a job queue and production reports.
- **Guided procedures** for edge seek and calibration; Operator, Engineer and Service modes.
- **i18n clean-up:** hard-coded English in three extensions and the vendor's mistranslations.
  Later, move to readable keys (Qt Linguist/gettext) with an importer for the original's files.
- **Later:** a LAN phone/tablet monitor (no vendor cloud), dark and high-contrast themes with SVG
  icons, remnant stock, a practice mode on the simulator, a camera overlay.

### Stage 4: performance and libraries, libraries §1–§3, architecture §4
- **Ordering:** O(n²) pure-Python ordering makes large documents freeze (3.6 s per edit at 2,000
  parts; a 30,000-part scan takes 351 s to open). Vectorise with numpy using the same formulas;
  golden order tests guard it.
- **Raster engraving** as a first-class object instead of one Part per scan segment.
- **Optional native kernel** (Cython or Rust) for sample → quantise → encode, with the Python
  version kept as the reference.
- **Library swaps** (no parity cost, or as optional modes):

  | Area | Change | Licence |
  |---|---|---|
  | SVG | svgelements | MIT |
  | DXF | "modern import" mode on ezdxf (hatches, dimensions, MINSERT, SHX) | MIT |
  | Kerf fallback | pyclipper + arc re-fit | BSL-1.0 |
  | Nesting | sparrow/spyrrow behind `Nester`; check spyrrow's licence (jagua-rs is MPL-2.0) | — |
  | Ordering | optional OR-Tools backend | Apache-2.0 |
  | Text | fontTools + uharfbuzz (CJK shaping) | MIT |
  | Packaging | PyInstaller one-folder + signing | — |
  | Motion (last, hardware-validated) | optional Ruckig-based planner | — |

- **Licences to avoid in a permissive product:** GPL (scapy, potrace bindings, LinuxCNC, grblHAL,
  Klipper, deepnest core), AGPL (PyMuPDF; Nuitka without its exception) and non-commercial (LKH,
  elkai).

### Platform decision
**Stay standalone (PySide6).** MeerK40t (MIT, wx, low-maintenance phase) has no motion planner,
leads, nesting, pierce, gas or follower; a plugin would have to bring everything already verified
here. Rayforge (MIT, GTK4) is the best source to borrow CAM code from, but not a host.
LinuxCNC/QtPlasmaC is the best design reference, but it is GPL and needs real-time Linux. Keep the
Qt-free core (cam → motion → nc → controller) usable as a library, so drivers for other front-ends
stay possible.

## Keep (strengths to build on)
- **Verified ports:** every stage that runs without the licence check matches the original, and
  is checked by `tools/compare_capture.py` and the Wine harness.
- **The end-to-end simulator** (it becomes the practice and safety-test bed).
- **Data-driven command ids** and the coverage tool.
- **Accurate time estimates.**
- **Open, scriptable core:** what lets this beat closed CypCut-class software.
