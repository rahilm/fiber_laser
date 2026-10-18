# mlaser: cross-platform rebuild of Gweike Mlaser

A pure-Python (PySide6) rebuild of **Gweike Mlaser v0.0.1.51_SP1**, the Windows CAM/CNC program
for fiber-laser sheet cutting with the MCC100 Ethernet motion card. The original's behaviour was
reverse-engineered from its binaries and shipped files. The rebuild covers file formats, DXF/SVG/
G-code import, the CAM toolpath (leads, kerf, micro-joints, ordering), the velocity planner, the
card protocol, an NC compiler that turns a drawing into the card's FIFO step stream, a card
simulator, and the main window.

> ## ⚠ Safety warning
> The motion-card protocol is **reverse-engineered and has never been tested on real hardware**.
> Much is observed in the original code, but a lot is still inferred: the meaning of some status
> words, the bench output/DA wiring used when no machine parameter files are loaded, follower
> state meanings, and what the card does on a FIFO underrun (see the unknowns sections of
> [docs/protocol.md](docs/protocol.md), [docs/nc.md](docs/nc.md) and [docs/devices.md](docs/devices.md)). A wrong guess can move the head at the wrong speed or fire the laser
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

```bash
cd rebuild
uv venv .venv --python 3.12
uv pip install -p .venv -e .            # PySide6, ezdxf, numpy, shapely
uv pip install -p .venv pytest          # for the tests
```

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

Tests: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`

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

## Layout

| Package | Content | Doc |
|---|---|---|
| `mlaser/model.py` | shared document model (parts, contours of lines/arcs, craft, layers) | |
| `mlaser/formats` | `.chf` load/save (byte-identical round trip), parameter XML, lang files, ini/aux files | [formats.md](docs/formats.md) |
| `mlaser/io` | DXF (ezdxf), SVG, G-code import/export | [cam.md](docs/cam.md) §1 |
| `mlaser/cam` | toolpath: start point, kerf, leads, overcut/gap, micro-joints, cool points, ordering | [cam.md](docs/cam.md) |
| `mlaser/motion` | S-curve profiles, look-ahead velocity planner, interpolators, simulation, time estimate | [motion.md](docs/motion.md) |
| `mlaser/nc` | job compiler (sampling, pulse quantisation, step items, frames) and job streamer | [nc.md](docs/nc.md) |
| `mlaser/controller` | MCC100 frame codec, process items, UDP/TCP client, alarms, FIFO streaming (thread or separate process), card simulator, UI adapter | [protocol.md](docs/protocol.md) |
| `mlaser/devices` | height follower, Raycus/IPG laser, auto-focus, EtherCAT heads, XHC wireless pendant | [devices.md](docs/devices.md) |
| `mlaser/nest` | true-shape nester, `.nestproj` project files | [nest.md](docs/nest.md) |
| `mlaser/ui` | main window, ribbon, canvas, panels, dialogs, SVG icons | [ui.md](docs/ui.md) |
| `mlaser/i18n` | English string catalogue, importer for an original install's translations | [ASSETS.md](docs/ASSETS.md) |

## Feature status

✅ implemented and tested · 🟡 partial or inferred · ❌ unknown or not implemented

| Area | Feature | Status | Notes / doc |
|---|---|---|---|
| Files | `.chf` open/save | ✅ | byte-identical round trip ([formats.md](docs/formats.md)) |
| | Machine/layer parameter XML | ✅ | `formats.params.MachineParams`, `Machine.from_dat_dir` |
| | DXF / SVG / G-code import, G-code export | ✅ | [cam.md](docs/cam.md) §1 |
| | Nest project (`.nestproj`) | ✅ | byte-identical round trip ([nest.md](docs/nest.md)) |
| CAM | Leads, start point, overcut/gap, micro-joints, cool points | ✅ | cam.md §2–7 |
| | Kerf compensation incl. Bézier corner join | ✅ | cam.md §5, §13 |
| | Fly cut (circle/line scan paths) | ✅/🟡 | types 3/5 U (cam.md §11) |
| | Common edge, array (rect; circular/fill as extensions), fill circle | ✅/🟡 | cam.md §12, §14 |
| | Corner fillet, relief loop, α corner, auto cool points | ✅/🟡 | arc/arc fillets not ported (cam.md §13) |
| | Import cleanup (dedupe, join, small objects, arc fitting) | ✅ | cam.md §15 |
| | Ordering modes 1–7 (bands, local optimal, ring), layer priority, manual order | ✅ | cam.md §8 |
| | Shortest-path / grid sort, rapid avoidance of cut holes | ✅ | rebuild extensions, not in the original |
| | Nesting (rotations, mirror, part-in-part, remnants, common line) | ✅ | own algorithm; the original uses a licensed SDK ([nest.md](docs/nest.md)) |
| Motion | S-curve/trapezoid profiles, look-ahead planner | ✅ | merge/bump passes not ported ([motion.md](docs/motion.md) §4) |
| | Contour smoothing (CContoutSmooth) | ✅/🟡 | `Machine(smooth=True)`; some end conditions U (motion.md §9) |
| | Simulation + time estimate | ✅ | rapid parameters INFERRED |
| NC | 1 ms sampling, pulse carry, step items (freq/duty/power), frames | ✅ | Σdeltas = final position (tested) |
| | Step scale ppr ÷ lead, speed cap 127 pulses/period (1270 mm/s default) | ✅ | protocol.md §11 |
| | Pierce stages, gas valves/pressure, follower moves, slow start/end, power curve | ✅/🟡 | op 106/3002 U ([nc.md](docs/nc.md), protocol.md §10) |
| Card | Frame codec, CRC, retries, HAL filter | ✅ | [protocol.md](docs/protocol.md) §1–2 |
| | Status, outputs, DA, jog, move, card scaling read-back | ✅ | protocol.md §11 |
| | Homing (`[2, mask, 0]`), alarms + reset | ✅ | protocol.md §12–13; Edge Seek ❌ |
| | Pause/stop (program stop + breakpoint), resume, backward/forward | ✅/🟡 | backward/forward wire sequence INFERRED (§14) |
| | FIFO streaming: prefill, underrun watchdog, separate-process streamer | ✅ | §15, `tools/bench_stream.py` |
| | Firmware upgrade | ❌ | `.mcf` is encrypted; described only |
| Devices | On-card height follower, calibration curve | ✅/🟡 | alarm bit layout U ([devices.md](docs/devices.md)) |
| | Raycus / IPG laser (emission, aiming beam) | ✅ | the original sends no status queries |
| | Auto-focus, EtherCAT heads | ✅/🟡 | alarm words U |
| | XHC wireless pendant (HID) | ✅ | needs `hidapi`; keys hold-to-jog |
| UI | Main window, ribbon (SVG icons), canvas, layers, parts, log, status bar | ✅ | [ui.md](docs/ui.md) |
| | English catalogue; 10 more languages imported from your own Mlaser install | ✅ | [Languages](#languages), [ASSETS.md](docs/ASSETS.md) |
| | CAM+, Nest, Devices and card extensions | ✅ | `mlaser/ui/ext_*.py`, loaded by the app |
| | Drawing/editing tools (14 shapes, text, QR, trim/extend, groups, align, size) | ✅/🟡 | [draw.md](docs/draw.md); ellipse/curve flattened, trim internals inferred |
| | Manual craft tools, one-click process, bitmap engraving (Bitmap.exe port) | ✅ | [craft.md](docs/craft.md), [engrave.md](docs/engrave.md) |
| | Edge seek, coordinate systems, firmware upload, parameter backup, task/recovery | ✅/🟡 | protocol.md §16–§21; offline job partial |
| | Monitors, device/work reports (report.exe port), aux tests, batch, maintenance, help | ✅/🟡 | [diag.md](docs/diag.md) |
| | Command coverage | ✅ | all 247 ribbon commands bound (`tools/command_coverage.py`) |

## Parity with the original

`/home/dev/scratch/mlaser/parity/` runs the original binaries under 32-bit Wine (loopback-only
network namespace, unpatched, licence checks untouched) and compares them with the rebuild:
`./run_parity.sh` for the DLL level, `--app` and `run_app.sh machine` to drive MainApp itself.
Latest: **263 match, 14 differ, 0 errors, 4 skipped** ([docs/parity.md](docs/parity.md)).

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
  `tools/compare_capture.py`; see [Verifying against your machine](docs/parity.md#8-verifying-against-your-machine).

## Timing: why Python is enough

The card replays the FIFO on its own servo clock (1 ms steps), so Python never does hard real-time
work. It only has to compile the job, which runs about 200× faster than real time and is done
before the start, and keep the card's FIFO from running dry, which takes about 12 kB/s. Measured
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
