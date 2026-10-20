# mlaser: cross-platform rebuild of Gweike Mlaser

A Python (PySide6) rebuild of **Gweike Mlaser v0.0.1.51_SP1**, the Windows CAM/CNC program for
fiber-laser sheet cutting with the MCC100 Ethernet motion card, with an optional C kernel for the
step emitter. The original's behaviour was reverse-engineered from its binaries and shipped
files. The rebuild covers file formats, DXF/SVG/G-code import, the CAM toolpath (leads, kerf,
micro-joints, ordering), the velocity planner, the card protocol, an NC compiler that turns a
drawing into the card's FIFO step stream, a card simulator, and the main window.

> ## ⚠ Safety warning
> **Status: pre-release (0.1.0 release candidates). Not validated on any real machine.** No part of
> mlaser (card protocol, motion planner, laser, gas, follower or interlock handling) has been run
> against a real MCC100 card or a real laser; everything is tested against mlaser's own card
> simulator and, for file formats and computations, against the original program under Wine.
>
> The motion-card protocol is **reverse-engineered and has never been tested on real hardware**.
> Much is observed in the original code, but a lot is still inferred: the meaning of some status
> words, the bench output/DA wiring used when no machine parameter files are loaded, follower
> state meanings, and what the card does on a FIFO underrun (see the unknowns sections of
> [docs/re/protocol.md](docs/re/protocol.md), [docs/re/nc.md](docs/re/nc.md) and [docs/re/devices.md](docs/re/devices.md)). A wrong guess can move the head at the wrong speed or fire the laser
> unexpectedly. Before connecting to a machine:
> 1. Run everything against the simulator first (`mlaser --demo`).
> 2. On the machine, **physically disable the laser** (interlock or key off) and start with
>    `mlaser --real --laser-off`. That clears the laser byte of every step. Use **Dry Cut** and
>    **Frame** with the e-stop in reach.
> 3. Load your machine's real parameters (`Machine.from_dat_dir(<Mlaser>/dat)`), which set the
>    step scale (ppr ÷ lead, default 10000 ÷ 100 = 100 pulses/mm) and the gas/follower wiring.
>    Check the scale and speed with a short jog before running a job.
>
> You use this at your own risk. It is not a certified machine controller.

## Notice

- **Independent reimplementation.** mlaser is an independent reimplementation written for
  interoperability: it reads and writes the original's file formats and speaks the MCC100 card
  protocol so that owners can drive their own machines from any OS. It is not affiliated with,
  endorsed by or supported by Gweike or the motion-card vendor; their names are used only to
  identify what this software interoperates with.
- **No vendor code.** No vendor source code, binaries or DLLs are included in this repository or
  in its packages. The original binaries are used only on the owner's own machine, under Wine,
  as a reference to compare behaviour against (`../parity/`, not part of this project).
- **No vendor assets.** Icons are [Tabler Icons](https://tabler.io/icons) (MIT, SVG). The UI
  text is mlaser's own English catalogue (`mlaser/i18n/en.json`). Other languages are not
  shipped: you import them from your own Mlaser installation (see [Languages](#languages)).
  Earlier development snapshots bundled icons and language files taken from the original; they
  were removed. [docs/ASSETS.md](docs/ASSETS.md) lists the provenance and licence of every
  bundled resource.
- **Licence checks are deliberately not implemented.** The original's licensing, dongle/HID
  checks, activation codes, laser rental lock, user locks, vendor passwords and cloud phone-home
  are left out on purpose (see the end of this file). Nothing in this project circumvents or
  emulates them; the parity harness runs the original unpatched with its checks intact.
- **Licence.** mlaser is released under the [MIT licence](LICENSE).

## Install

### From a release

Each release on the GitHub release page carries:

| asset | for |
|---|---|
| `mlaser-<version>-<os>-<arch>.zip` / `.tar.gz` | the one-folder program (PyInstaller, Python and Qt included): Windows x64, Linux x86_64, macOS Apple silicon |
| `mlaser-<version>-cp312/cp313-...whl` | `pip install` into your own Python 3.12 / 3.13: manylinux x86_64 and aarch64, Windows AMD64, macOS arm64 |
| `mlaser-<version>.tar.gz` | the source distribution (builds the C kernel when a compiler is present) |

```bash
pip install mlaser-0.1.0rc1-cp312-cp312-win_amd64.whl    # the wheel for your Python and OS; PySide6 etc. from PyPI
python -m mlaser._native                                 # the C emitter kernel is active
mlaser --demo
```

Platform notes:

* **macOS: Apple silicon (arm64) only.** There is no Intel macOS build. The bundle and the wheels
  are not signed unless the release says so: the first start of an unsigned `mlaser.app` needs
  right-click > Open (or `xattr -dr com.apple.quarantine mlaser.app`).
* **Linux:** the wheels are tagged manylinux_2_5 (x86_64) and manylinux_2_17 (aarch64), as the C kernel
  needs nothing newer, but PySide6
  6.12 itself needs glibc 2.34 (x86_64) / 2.39 (aarch64), e.g. Ubuntu 22.04 / 24.04 or newer. The
  one-folder bundle is built on Ubuntu 22.04 and runs on glibc 2.35 or newer; it needs the X11 /
  Wayland and EGL libraries of a desktop install (`libegl1 libxkbcommon-x11-0 libxcb-cursor0`).
* **Windows:** 64-bit only. An unsigned bundle shows a SmartScreen warning on first start.
* The one-folder bundles include the optional OR-Tools, spyrrow and Ruckig; a wheel installs them
  with the extras (`pip install "mlaser-....whl[modern]"`).

### From the source

```bash
git clone https://github.com/rahilm/fiber_laser.git && cd fiber_laser
uv sync --locked                        # .venv from uv.lock: the package (with its C emitter kernel
                                        #   when a C compiler is present), pytest, ruff, pre-commit
uv sync --locked --extra devices        # + hidapi / pyserial (pendant, serial links)
uv sync --locked --extra modern         # + optional modern options: OR-Tools (sort mode 20),
                                        #   spyrrow (nesting), Ruckig (planner, NOT hardware-validated)
pre-commit install                      # optional: ruff on every commit
python -m mlaser._native                # is the C emitter kernel built and in use? (else the Python port runs)
uv sync --group packaging --all-extras && uv run python packaging/build.py --smoke   # frozen one-folder build
```

The emitter loop (sample -> quantise -> encode) runs on a C kernel, `mlaser._native`, compiled
during the install whenever a C compiler is available (Linux: gcc, `build-essential`; macOS:
the Xcode command line tools; Windows: Visual Studio Build Tools, "Desktop development with
C++"). Its output is bit-identical to the Python port, which stays the reference: at first use
a built-in self-check compares the two, and without a compiler, or if the check fails, the
Python port runs (about 2x slower to compile a job) with a warning in the log.
`MLASER_NATIVE=0` forces the Python port, `MLASER_NATIVE=1` requires the kernel (error if it
cannot be used); the About dialog and `python -m mlaser --selftest` show which one is active.
The wheels and the one-folder bundles from CI include it. Details:
[docs/dev/NATIVE_KERNEL.md](docs/dev/NATIVE_KERNEL.md).

Optional behaviour is selected through parity profiles; the default `"original"` is the
bit-exact one: `use_profile("modern-ortools")`, `"modern-ruckig"`, `"modern-dxf"` (or
`import_dxf(path, mode="modern")`), `"original-python"` (emitter loop always on the Python
port); nesting with
`NestParams.engine="spyrrow"`; kerf fallback with `CamParams.kerf_fallback="clipper"`. Text uses
the bundled Noto Sans SC subset (OFL) when no installed font matches
(`python tools/build_bundled_fonts.py --full-cjk` rebuilds it with all CJK ideographs). A first
start runs a network self-test (`python -m mlaser --selftest [--probe]` runs it on demand;
`MLASER_NO_FIRSTRUN=1` skips it). Measurements: docs/dev/PERFORMANCE.md; licences:
docs/ASSETS.md.

Without uv: `python -m venv .venv && .venv/bin/pip install -e ".[test]"` (also builds the C
kernel when a compiler is present). Python 3.12 or 3.13
(both tested in CI). CPython 3.12 is the reference interpreter: the goldens are pinned to its float
`sum()` semantics (compensated summation since 3.12) and are checked bit-exactly on it. Python 3.11
is not supported: PySide6 6.12 aborts on it with "Fatal Python error: none_dealloc" (it drops one
reference to `None` too many; 3.12 made `None` immortal, which hides the bug).

## Run

```bash
.venv/bin/mlaser --demo                 # sample document + in-process card simulator (UDP on localhost)
.venv/bin/mlaser part.dxf --sim         # open a file and connect to the simulator
.venv/bin/mlaser --demo --sim-speed 5   # simulator runs 5x faster than real time
.venv/bin/mlaser --real --ini ipAdd.ini --laser-off   # real card (UNTESTED), laser disabled
.venv/bin/mlaser --fake                 # protocol-free demo controller
.venv/bin/mlaser-cardsim --port 5020    # stand-alone card simulator; then --real --host 127.0.0.1 --port 5020
.venv/bin/mlaser --import-lang "/path/to/Mlaser" --lang de   # see Languages below
```

You can also connect from inside the UI: **Advanced → Hardware → Connect to simulator**. Start,
Pause, Stop, Frame and Dry Cut on the Run Control panel go through the full pipeline (CAM →
planner → 1 ms samples → pulse steps → FIFO frames → UDP), and the head marker and the red track
follow the position the simulator reports. **Sim** previews the toolpath with the motion planner
and writes the time estimate to the log.

Scripting:

```python
from mlaser.io.dxf_import import import_dxf
from mlaser.nc import compile_job, Machine
job = compile_job(import_dxf("part.dxf"), {"sort_mode": 5}, Machine())
print(job.summary())          # steps, frames, replay time, cut length, pierces
frames = job.frames(first_seq=1)   # values for write(102, frame)
```

Tests (Qt runs offscreen):

```bash
uv run pytest -q -n auto --dist loadgroup   # default suite in parallel, as CI (~2.7 min; safety
                                        #   and sim tests stay on one worker, docs/dev/TESTING.md)
uv run pytest -q                        # default suite serially: unit, ui, sim, safety (~5.5 min)
uv run pytest -q -m unit                # or ui / sim: one suite
uv run pytest -q -m golden              # golden artefacts (~75 s), see tests/golden/
uv run pytest -q -m parity              # Wine parity suite (needs ../parity, ~2 min)
uv run python tools/make_goldens.py     # regenerate the goldens, only after a reviewed change (CONTRIBUTING.md)
```

The times were measured on a shared 64-core host under heavy load (load average about 250,
2026-10-19); see [docs/dev/TESTING.md](docs/dev/TESTING.md#parallel-runs-pytest-xdist).

## Languages

mlaser ships English only. Its English is its own wording; it fixes the original's
mistranslations ("Copy to maun", "Roasting machine test", …) and uses named placeholders.
If you own a copy of the original Mlaser, you can use its translations (Simplified Chinese,
Russian, German, Spanish, Portuguese, French, Italian, Polish, Vietnamese, Turkish) with
mlaser. Point mlaser at the install folder, its `Lang` folder or a single `Lang/*.txt` file:

* in the app: **File → Import Language Files…**, then pick the language at the top right;
* on the command line: `mlaser --import-lang "C:/Program Files/Mlaser" --lang zh`, or
  `python -m mlaser.i18n import <path>` (`list`, `remove <code>` manage them).

The texts are copied once into your data folder (`~/.mlaser/lang/`, or `$MLASER_DATA_DIR/lang`,
or `$MLASER_LANG_DIR`). After that the install is no longer needed. To read them in place
without copying, use `mlaser --lang-dir <path> --lang zh`. Strings mlaser added that the
original doesn't have stay in English.

Translators: `mlaser/i18n/en.json` maps readable ids (`cmd.machine.home`) to text, and its
`legacy` table maps the original's keys (`mf15`) to those ids. A new language is a
`mlaser/i18n/<code>.json` with the same ids. `tools/i18n_keys.py` checks that every key the
code uses has an English text.

## Architecture

The packages are layered; dependencies point downward only, and the domain packages import no Qt.
[docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md) has the full map, the rules and how they are
checked (import-linter and mypy in CI), the moved modules, and the parity fence.

| Layer | Package | Content | Doc |
|---|---|---|---|
| apps | `mlaser/ui` | main window, ribbon, canvas, panels, dialogs, SVG icons | [ui.md](docs/re/ui.md) |
| | `mlaser/diag`, `report`, `engrave` | diagnostics, production report, bitmap engraving | [diag.md](docs/re/diag.md) |
| machine | `mlaser/controller` | UDP/TCP card client, alarms, job streamer (thread or separate process), card simulator, UI adapter | [protocol.md](docs/re/protocol.md) |
| | `mlaser/machine`, `devices` | `machine.api` (ControllerFacade, MachineState); height follower, Raycus/IPG laser, auto-focus, EtherCAT heads, XHC pendant | [devices.md](docs/re/devices.md) |
| NC | `mlaser/nc` | job compiler (sampling, quantisation, step items, frames), process sequences, resume | [nc.md](docs/re/nc.md) |
| CAM | `mlaser/io`, `nest` | DXF (ezdxf), SVG, G-code import/export; true-shape nester, `.nestproj` | [cam.md](docs/re/cam.md) §1, [nest.md](docs/re/nest.md) |
| | `mlaser/cam` | toolpath: start point, kerf, leads, overcut/gap, micro-joints, cool points, ordering | [cam.md](docs/re/cam.md) |
| | `mlaser/motion` | path planning over the toolpath, simulation, time estimate | [motion.md](docs/re/motion.md) |
| | `mlaser/formats`, `cad` | `.chf` (byte-identical round trip), parameter XML and registry, ini/aux files; drawing primitives, text | [formats.md](docs/re/formats.md) |
| parity fence | `mlaser/parity_ports` | the bit-exact ports of the original (planner, smoother, quantisation, NC encoder, sort modes, start point, CircleFit, DXF import rules, `.chf` codec) behind a `ParityProfile` | [parity.md](docs/re/parity.md) |
| foundation | `mlaser/geom`, `protocol`, `toolpath`, `i18n` | one geometry library; MCC100 frame codec, ManuItem, enums; MotionItem types; string catalogue | [protocol.md](docs/re/protocol.md), [ASSETS.md](docs/ASSETS.md) |
| | `mlaser/model.py` | shared document model (parts, contours of lines/arcs, craft, layers) | |

The bit-exact ports change only with a Wine parity re-run; new algorithms are added as new
`ParityProfile`s next to the default `"original"` one. Old import paths from before this layout
(`mlaser.controller.frames`, `mlaser.cam.items`, ...) still work but warn
(`DeprecationWarning`).

## Feature status

✅ implemented and tested · 🟡 partial or inferred · ❌ unknown or not implemented

| Area | Feature | Status | Notes / doc |
|---|---|---|---|
| Files | `.chf` open/save | ✅ | byte-identical round trip ([formats.md](docs/re/formats.md)) |
| | Machine/layer parameter XML | ✅ | `formats.params.MachineParams`, `Machine.from_dat_dir` |
| | DXF / SVG / G-code import, G-code export | ✅ | [cam.md](docs/re/cam.md) §1 |
| | Nest project (`.nestproj`) | ✅ | byte-identical round trip ([nest.md](docs/re/nest.md)) |
| CAM | Leads, start point, overcut/gap, micro-joints, cool points | ✅ | cam.md §2–7 |
| | Kerf compensation incl. Bézier corner join | ✅ | cam.md §5, §13 |
| | Fly cut (circle/line scan paths) | ✅/🟡 | types 3/5 U (cam.md §11) |
| | Common edge, array (rect; circular/fill as extensions), fill circle | ✅/🟡 | cam.md §12, §14 |
| | Corner fillet, relief loop, α corner, auto cool points | ✅/🟡 | arc/arc fillets not ported (cam.md §13) |
| | Import cleanup (dedupe, join, small objects, arc fitting) | ✅ | cam.md §15 |
| | Ordering modes 1–7 (bands, local optimal, ring), layer priority, manual order | ✅ | cam.md §8 |
| | Shortest-path / grid sort, rapid avoidance of cut holes | ✅ | rebuild extensions, not in the original |
| | Nesting (rotations, mirror, part-in-part, remnants, common line) | ✅ | own algorithm; the original uses a licensed SDK ([nest.md](docs/re/nest.md)) |
| Motion | S-curve/trapezoid profiles, look-ahead planner | ✅ | merge/bump passes not ported ([motion.md](docs/re/motion.md) §4) |
| | Contour smoothing (CContoutSmooth) | ✅/🟡 | `Machine(smooth=True)`; some end conditions U (motion.md §9) |
| | Simulation + time estimate | ✅ | rapid parameters INFERRED |
| NC | 1 ms sampling, pulse carry, step items (freq/duty/power), frames | ✅ | Σdeltas = final position (tested) |
| | Step scale ppr ÷ lead, speed cap 127 pulses/period (1270 mm/s default) | ✅ | protocol.md §11 |
| | Pierce stages, gas valves/pressure, follower moves, slow start/end, power curve | ✅/🟡 | op 106/3002 U ([nc.md](docs/re/nc.md), protocol.md §10) |
| Card | Frame codec, CRC, retries, HAL filter | ✅ | [protocol.md](docs/re/protocol.md) §1–2 |
| | Status, outputs, DA, jog, move, card scaling read-back | ✅ | protocol.md §11 |
| | Homing (`[2, mask, 0]`), alarms + reset | ✅ | protocol.md §12–13; Edge Seek ❌ |
| | Pause/stop (program stop + breakpoint), resume, backward/forward | ✅/🟡 | backward/forward wire sequence INFERRED (§14) |
| | FIFO streaming: prefill, underrun watchdog, separate-process streamer | ✅ | §15, `tools/bench_stream.py` |
| | Firmware upgrade | ❌ | `.mcf` is encrypted; described only |
| Devices | On-card height follower, calibration curve | ✅/🟡 | alarm bit layout U ([devices.md](docs/re/devices.md)) |
| | Raycus / IPG laser (emission, aiming beam) | ✅ | the original sends no status queries |
| | Auto-focus, EtherCAT heads | ✅/🟡 | alarm words U |
| | XHC wireless pendant (HID) | ✅ | needs `hidapi`; keys hold-to-jog |
| UI | Main window, ribbon (SVG icons), canvas, layers, parts, log, status bar | ✅ | [ui.md](docs/re/ui.md) |
| | English catalogue; 10 more languages imported from your own Mlaser install | ✅ | [Languages](#languages), [ASSETS.md](docs/ASSETS.md) |
| | CAM+, Nest, Devices and card extensions | ✅ | `mlaser/ui/ext_*.py`, loaded by the app |
| | Drawing/editing tools (14 shapes, text, QR, trim/extend, groups, align, size) | ✅/🟡 | [draw.md](docs/re/draw.md); ellipse/curve flattened, trim internals inferred |
| | Manual craft tools, one-click process, bitmap engraving (Bitmap.exe port) | ✅ | [craft.md](docs/re/craft.md), [engrave.md](docs/re/engrave.md) |
| | Edge seek, coordinate systems, firmware upload, parameter backup, task/recovery | ✅/🟡 | protocol.md §16–§21; offline job partial |
| | Monitors, device/work reports (report.exe port), aux tests, batch, maintenance, help | ✅/🟡 | [diag.md](docs/re/diag.md) |
| | Command coverage | ✅ | all 247 ribbon commands bound (`tools/command_coverage.py`) |

## Parity with the original

A separate parity harness, not part of this repository (`../parity/` next to the checkout on the
development machine), runs the original binaries under 32-bit Wine (loopback-only
network namespace, unpatched, licence checks untouched) and compares them with the rebuild:
`./run_parity.sh` for the DLL level, `--app` and `run_app.sh machine` to drive MainApp itself.
Latest: **263 match, 14 differ, 0 errors, 4 skipped** ([docs/re/parity.md](docs/re/parity.md)).

* **Match:**
  * card frame encoding/decoding and the start-up sequence (byte-identical);
  * the motion planner and interpolators, against both MotionCtrl.dll and CADModule's copy;
  * arc fitting;
  * DXF import → `.chf` (byte-identical on 11 of 12 samples);
  * `.chf` round trips;
  * lead defaults and start points;
  * every manual machine operation: jog, homing, origins, outputs, mark/return, laser type.
* **Accepted differences (14):**
  * a trapezoid-profile bug in both original copies that cutting never reaches;
  * the original's own spline approximation measured against its DXF parser;
  * hinted font outlines for TEXT.
* **Not comparable (4):** the original disables Frame, Dry Cut, Start and breakpoint behind its
  hardware licence check, so the cutting step stream is verified only by the rebuild's own
  tests, not against the original.
* **Check it on your own machine:** capture the original's card traffic during a Dry Cut and run
  `tools/compare_capture.py`; see [Verifying against your machine](docs/re/parity.md#8-verifying-against-your-machine).

## Timing: why Python is enough

The card replays the FIFO on its own servo clock (1 ms steps), so Python never does hard real-time
work. It only has to compile the job, which runs roughly 100× faster than real time with the
default profile (smoothing on: 90-180× measured on a loaded host, 1 ms per step; with smoothing
off about 400×; [PERFORMANCE.md §5](docs/dev/PERFORMANCE.md#5-native-kernel-p2-2-the-default))
and is done before the start, and keep the card's FIFO from running dry, which takes about 12 kB/s. Measured
against the simulator replaying at real speed:

| Setup | Card starved | Lowest buffer |
|---|---|---|
| streamer thread, idle UI | 0 s | – |
| streamer thread, 2 busy Python threads (GIL contention) | 2.4 s over a 47 s job (FIFO ran dry) | – |
| streamer **process**, same load | ≈0 s | – |
| `tools/bench_stream.py`, 16 KiB FIFO at 20×, 4 busy threads | thread: 48 underruns | process: 0 underruns, ≥755 ms buffered |

So the UI uses a separate streamer process (`ProcessStreamer`) for a real card. A watchdog stops
the FIFO and turns the PWM off before it drains.

**Intentionally omitted:** licensing, the dongle/HID checks, activation codes, the laser rental
lock ("laser unlock"), user locks, vendor passwords and the cloud monitor phone-home.
