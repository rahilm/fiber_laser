# Library and platform review: what to replace, wrap or keep

Scope: `<workspace>/rebuild/` (about 44.5k LOC of Python in `mlaser/`, 528 tests). This was a read-only review; no project file was changed.
Library status was checked on 2026-10-18 against the PyPI JSON API, GitHub and the project docs. The GitHub and web-search rate limits hit partway through, so a few activity figures come from PyPI release cadence instead.

## 0. What I checked first

**Installed vs declared dependencies.**
- The venv has PySide6 6.12.0, ezdxf 1.4.5, numpy 2.5.4, shapely 2.2.0, segno 1.6.6, fontTools 4.67.0, hidapi 0.15.0, Pillow 13.0.0, matplotlib 3.11.2 and pefile.
- `pyproject.toml` still declares only the first four. segno, fontTools, hidapi, Pillow and matplotlib are imported (`cad/qr.py`, `io/dxf_import.py`, `devices/pendant.py`, `engrave/image_ops.py`, `parity/report.py`) but not declared.
- `devices/transport.py` imports `serial` (pyserial), which is neither installed nor declared.
- Fix: put them in extras (`[qr]`, `[pendant]`, `[serial]`, `[parity]`), or make them required.

**Parity scope is wider than the brief's list.** `docs/re/parity.md` §3 and §9 compare these against the original running under Wine:
- motion planner, segInterp/arcInterp, CContoutSmooth;
- NC frames and process items, `.chf` bytes;
- **DXF import** (DxfParse, 12 DXFs, byte-identical `.chf` for 11 of them);
- **arc fitting** (CircleFitDLL `mainFit` → `cam/cleanup.fit_arcs`);
- the **import sort**, including MSVC `rand()` (`cam/ordering.py`);
- **craft primitives**: leads, kerf, start point, overcut/gap, micro-joints (§9, "crafts → primitives" 51/24);
- `crtmath.py`, which reproduces msvcrt `acos`/`atan2`/`hypot` bit for bit.

All of these count as "faithful" code below. A library can replace them only behind an option.

**Licence context.** The project licence is not chosen yet.
- PySide6 is LGPL-3. That already means: ship it as replaceable shared libraries (PyInstaller onedir rather than a static onefile), and include the LGPL notice and a relinking path.
- Everything recommended below is MIT/BSD/Apache/BSL/MPL, unless marked otherwise.
- Avoid in a closed or permissive product: GPL (scapy, potrace and all its bindings, pygcode, LinuxCNC/grblHAL/Klipper code, deepnest's `deepnest.js` core), AGPL (PyMuPDF, Nuitka unless the runtime exception is confirmed), non-commercial (LKH-3/elkai).

Legend:
- **Parity**: F = faithful port, verified or targeted against the original. A library is allowed only as (a) output-identical or (b) an optional "modern mode". "–" = no parity constraint.
- **Effort**: S < 2 days, M ≈ 1–2 weeks, L > 2 weeks.

## 1. Subsystem table

### Geometry and CAD

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| Kerf offset | `cam/kerf.py` 246: analytic line/arc offset, falls back to shapely `buffer` (arcs lost) | **pyclipper** 1.4.0 (MIT, Clipper1, wheels cp310-314 everywhere); **clipper2-py** 0.1.1 (MIT+BSL, full wheels but only weeks old); pyclipr 0.1.8 (BSL, no Linux wheels); shapely `offset_curve` (polyline only); OCCT `BRepOffsetAPI_MakeOffset` via build123d/OCP (exact arcs, LGPL OCCT, wheels of several hundred MB) | **Keep** the analytic offset. **Wrap**: replace the shapely fallback with pyclipper, then re-fit arcs (`cleanup.fit_arcs`) for self-intersecting or degenerate inputs | S | Low | F (JC-03/04/13 are fixed numerics). Clipper is option (b), fallback path only | **Done, opt-in** (Wave 2): pyclipper + `fit_arcs` re-fit behind `CamParams.kerf_fallback="clipper"`; the default stays shapely because the goldens' random jobs reach the fallback 87 times (the parity tests never do) |
| Booleans / union / containment | shapely used in `nest/`, `cam/ordering.py`, `cam/array.py`, `cad/qr.py` | shapely 2.2.0 (BSD, GEOS 3.14, active); skia-pathops 0.9.2 (BSD, Bezier booleans) | **Keep** shapely. Add skia-pathops only if Bezier-exact unions of glyphs or splines are needed | – | Low | – | kept; STRtree now filters the containment tree and container chain (bit-identical) |
| Arc fitting | `cam/cleanup.py` 914 (CircleFitDLL port, 13/13 parity) | No maintained general biarc fitter on PyPI. `clothoids` has biarcs (niche); fibomat biarc works on curves only | **Keep** | – | – | F | – |
| NURBS / B-splines | `io/geom.py` 431 (own de Boor eval/flatten), `motion/curvetable.py` 736 (openNURBS sample table), `cad/primitives.spline` (Catmull) | ezdxf.math `BSpline` (MIT, already a dependency); geomdl 5.4.0 (MIT, dormant, pure Python, slow); rhino3dm 8.35 (MIT, active: evaluation only, no offsets/booleans); OCP/build123d (LGPL OCCT, heavy) | **Keep** curvetable (F). **Replace** the generic eval in `io/geom.py` with `ezdxf.math.BSpline`/`ezdxf.path` where it is not on the parity path, and cross-check in tests. Don't add OCCT for this | S | Low | `io/geom` feeds the DXF parity path: only an output-identical swap allowed | – |
| Intersect / trim / extend / split | `cad/intersect.py` 207, `cad/edit.py` 314 (analytic line/arc) | shapely (polylines only, so arcs lost); OCCT 2D (exact, heavy); ezdxf.math `intersection_line_line_2d` / `ConstructionArc` helpers | **Keep**. It is small and analytic, and it keeps arcs, which no light library does | – | Low | trim internals inferred; no parity test | – |
| Snap, arrange, primitives, drawing tools | `cad/snap.py` 73, `arrange.py` 322, `primitives.py` 284, `tools.py` 495 | none worth it | **Keep** | – | – | behaviour port | – |
| Common-edge cutting | `cam/commonedge.py` 463 (CShareEdge port) | none in OSS (Deepnest/SVGnest merge lines only crudely) | **Keep** | – | – | behaviour port | – |
| Lead-in/out, micro-joints, corners, fly cut, array | `leads.py` 257, `microjoint.py` 110, `corners.py` 387, `flycut.py` 994, `array.py` 371, `manual.py` 701 | Rayforge (MIT) has lead-in/tab post-processors; MeerK40t (MIT) has tabs (`mktab*`) | **Keep**. Niche, already parity-tested; Rayforge/MeerK40t are reading material only | – | – | F (§9) | – |

### File I/O

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| DXF import | `io/dxf_import.py` 1128 on ezdxf 1.4.5 (reads and recovers; own entity→glyph mapping, INSERT rules and SPLINE weights as DxfParse does them; TEXT via fontTools glyf; skips HATCH, DIMENSION, LEADER, MLEADER, ATTRIB/ATTDEF, MLINE, IMAGE) | ezdxf 1.4.5 (MIT, very active): `ezdxf.path`, `disassemble.recursive_decompose`, `addons.text2path` (glyph outlines; SHX via `ezdxf.fonts`), `MTextExplode`, HATCH boundary paths, `addons.drawing` | **Keep** the faithful path. **Wrap**: add a "modern import" option that uses `recursive_decompose` + `ezdxf.path` + `text2path` for what DxfParse drops (ATTRIB text, DIMENSION/LEADER blocks, HATCH boundaries as cut contours, MINSERT grids, SHX fonts) | M | Low | F for the default. Modern import = option (b) | **Done, optional** (Wave 2): `import_dxf(path, mode="modern")` / profile `modern-dxf` (`io/dxf_modern.py`: recursive_decompose, ezdxf.path, HATCH, DIMENSION/LEADER, ATTRIB, MINSERT, SHX via ezdxf.fonts); faithful default unchanged |
| SVG import | `io/svg_import.py` 299 (own parser: no `<use>`, no CSS/`style` classes, no rounded `rect`, `defs`/`clipPath` skipped, no `display:none`) | **svgelements** 1.9.6 (MIT; last PyPI release 2023 but maintained inside MeerK40t, vendored there; most complete: CSS, `use`, units, shapes → Path); svgpathtools 1.8.0 (MIT, active, weaker document parser); vpype 1.15 (MIT, no 3.14 support) | **Replace** the parser with svgelements. Keep our arc/flatten conversion to model contours. The original goes through juicy-gcode, so there is no parity constraint | S | Low (svgelements stale: vendor or pin it) | – (the original's pipeline is SVG2GCode.exe; not compared) | **Done** (Wave 2): svgelements parser plus our pre-pass (use/symbol and nested-svg viewports, exact units, symbol/mask/marker/pattern dropped) and our arc/flatten conversion |
| PDF / AI import (missing) | none | pdfplumber/pdfminer.six (MIT, vector paths with CTM); pikepdf 10.17 (MPL-2.0, raw operators); pypdf (BSD, low level); **PyMuPDF (AGPL, avoid)**. AI ≥ 9 files are PDF-compatible | **Add** via pdfminer.six (paths → contours) if the feature is wanted | M | Med (PDF curves/clip quirks) | – | – |
| G-code import/export | `io/gcode.py` 204 (juicy-gcode dialect) | gcodeparser 0.3 (MIT, tokenizer only); pygcode (GPL, dead) | **Keep**. The dialect is tiny and fixed | – | – | Output.nc dialect | – |
| Text → outlines | `cad/text.py` 121 (Qt font engine), DXF TEXT via fontTools glyf (`dxf_import`), `ezdxf.fonts` unused | fontTools 4.67 (MIT, already used); **uharfbuzz** 0.56 (Apache-2, HarfBuzz MIT: shaping, kerning, Arabic/Indic, vertical CJK); skia-pathops (overlap removal); ezdxf `text2path` | **Wrap**: route Draw→Text through fontTools + uharfbuzz (one code path for DXF and drawn text, platform-independent, no QApplication needed). Keep the GGO_BEZIER emulation for DXF parity | S–M | Low | DXF TEXT is F (D-16 known diff); Draw→Text has no parity | **Done** (Wave 2): Draw→Text and the DXF TEXT fallback on fontTools + uharfbuzz with bundled Noto Sans SC / Arabic subsets; GGO_BEZIER path kept; cad and io are Qt-free (import-linter ignores removed) |
| QR codes | `cad/qr.py` 96 on segno | segno 1.6.6 (BSD); qrcode 8.2 | **Keep** segno (add it to pyproject) | – | – | – | – |
| Image engraving / dithering | `engrave/image_ops.py` 390 + `raster.py` 395 (numpy re-implementation of the OpenCV calls next_cut.dll makes, bit-exact) | opencv-python-headless 5.0 (Apache-2); Pillow FS dithering; scikit-image | **Keep**. Replacing it with OpenCV gives the same output but adds about 60 MB for no gain | – | – | engrave port (bit-exact claims) | kept; engraving is now one raster object (`cam/raster_engraving.py`), not one Part per burn run |
| Image tracing (missing) | none (the original has raster engraving only) | potrace and all its bindings (**GPL**: avoid unless the project goes GPL); OpenCV `findContours`+`approxPolyDP` (Apache); skimage `find_contours` (BSD) + our `fit_arcs`/Bezier fit | **Add** as an extension using OpenCV or skimage contours → `cleanup.fit_arcs` | M | Med (tracing quality is below potrace) | – | – |

### CAM ordering and nesting

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| Nesting | `nest/engine.py` 474 + `shapes.py` 132 (own FFT raster bottom-left fill + shapely compaction); `model.py`, `apply.py`, `project.py` (file format) | **sparrow** 0.3 (MIT, Rust, state of the art 2024–25) on **jagua-rs** 1.1 (MPL-2.0, file-level copyleft: fine unmodified), Python via **spyrrow** 0.9 (wheels Win/mac/Linux; PyPI licence field empty, so **verify the licence in the repo**); deepnest-next 1.5.6 (core `deepnest.js` GPLv3, Electron: external process only); libnest2d/pynest2d (LGPL-3, not on PyPI, painful to build); SVGnest/Nest4J (dead) | **Wrap**: add a spyrrow engine behind the `Nester` interface (`NestJob` → `NestResult`). Keep the raster engine as default/fallback. sparrow is strip packing, so multi-sheet, part-in-part and remnants need a driver loop on our side | M | Med (spyrrow is young; part-in-part and remnant support need work) | – (the original's SmartNest/AutoNest is a licensed SDK, not ported) | **Done, optional** (Wave 2): `NestParams.engine="spyrrow"` (licence verified MIT; jagua-rs 0.7.0 MPL-2.0 unmodified inside); raster engine stays default |
| Faithful sort modes 1–7, import sort, containment tree | `cam/ordering.py` (bulk of its 1185 LOC) | – | **Keep** | – | – | F | kept; mode 5 kernels run as bit-identical numpy/grid drop-ins (`ordering_fast`), references in profile `original-reference` |
| Shortest-path / grid sort extension | `ordering.shortest_path` (precedence-constrained NN + 2-opt + or-opt, pure Python, 2 s time limit), `_grid_sort` | **OR-Tools** 9.15 (Apache-2; routing with pickup/precedence, or CP-SAT); **PyVRP** 0.14 (MIT, HGS); **fast-tsp** 0.2 (MIT, C++ 2-opt/or-opt, no precedence); LKH-3/elkai (**non-commercial**, avoid) | **Wrap**: optional OR-Tools backend for mode 20 (precedence "children first", asymmetric entry/exit cost, heat constraints later). Keep the pure-Python one as the zero-dependency default | M | Low–Med (OR-Tools wheel ~25 MB) | – (a rebuild extension) | **Done, optional** (Wave 2): profile `modern-ortools` (children-first precedence, small first, seeded with the built-in order) |

### Motion and NC

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| S-curve profiles, segInterp/arcInterp | `motion/profile.py` 498, `interp.py` 551, `crtmath.py` 192 | **Ruckig** 0.19.4 (Community MIT; Pro commercial. In Community, intermediate waypoints are computed through a remote cloud API, so per-segment calls only); TOPP-RA 0.6.10 (MIT, Linux wheels only, no native jerk limit) | **Keep** faithful. Ruckig only inside the modern planner below | – | – | F (bit-exact) | kept; Ruckig used only inside `modern-ruckig`, per segment |
| Look-ahead velocity planner | `motion/planner.py` 842 (CVelocityPlanning, 18/18 parity) | LinuxCNC `tp.c` (GPL-2, 2.10 adds a jerk-limited planner), grblHAL planner (GPL-3), Klipper lookahead (GPL-3): **reference only**. Ruckig per block (MIT) | **Keep** + optional **modern planner (b)**: our own block look-ahead (junction-deviation / curvature limits, ideas from the GPL planners re-derived, not copied) with Ruckig per segment for jerk-limited profiles. It must emit the same `Plan.sample` interface, so 1 ms sampling, quantisation and frames are unchanged | L | High (safety: untested hardware; validate against the simulator and real traces first) | F default; modern = (b) | **Done, optional** (Wave 2): profile `modern-ruckig` (`parity_ports/profiles/modern_motion.py`), off by default, **not validated on hardware** |
| Contour smoothing (corner blend) | `motion/smooth.py` 1098, `curvetable.py` 736 | none suitable (openNURBS via rhino3dm evaluates splines but has no CContoutSmooth equivalent) | **Keep** | – | – | F | – |
| Sampling, pulse quantisation, ManuItem/FIFO, frames, process items, `.chf` | `nc/*` 1750, `controller/frames.py` 771, `process.py` 1040, `formats/chf.py` 852 | – | **Keep** | – | – | F (byte-identical) | kept; packed numpy wire (`nc/packed.py`, identical); optional C kernel `mlaser-native` / profile `native-sampling` (bit-identical on all golden jobs) |
| Card client, streamer, simulator | `controller/client.py` 994, `simulator.py` 907, `nc/stream.py` 382 (stdlib sockets, threads/multiprocess) | asyncio / Qt sockets; pymodbus is not usable (the card's CExtModbus frame is custom: BE header, LE payload, own CRC) | **Keep** | – | – | F (traffic parity §7) | – |

### Devices and networking

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| AF / EC Modbus-RTU over UDP/TCP | `devices/modbus_rtu.py` 149, `codecs.py` 331, `transport.py` 368 | **pymodbus** 3.16.1 (BSD, very active; RTU framer over UDP/TCP, custom function codes; but the 3.x API changes between minors, and custom framers subclass the non-public `FramerBase`) | **Keep**. Optionally use pymodbus's CRC/RTU framer *in tests* as an independent oracle. Plain RTU (fc 3/6/16) would fit pymodbus, but the CSerialModbus/CExtCardModbus quirks, retry semantics and the small code size don't justify the dependency churn | – | – | wire behaviour from NCModule | – |
| Serial (optional transport) | `transport.py` imports `serial` lazily | pyserial 3.5 (BSD; **no release since 2020**, works); **QtSerialPort** (in PySide6, LGPL, maintained) | **Replace** pyserial with `PySide6.QtSerialPort`, or at least declare pyserial | S | Low | – | – |
| XHC pendant (HID) | `devices/pendant.py` 412 on hidapi | hidapi 0.15 (BSD or GPL-3: choose BSD); `hid` 1.0.9 (ctypes, needs system lib) | **Keep** (declare it as an extra) | – | – | – | – |
| pcap/pcapng reading | `parity/pcap.py` 562 (own: pcap + pcapng, many link types, IPv4 reassembly, writer) | dpkt 1.9.8 (BSD, release 2022); python-pcapng 2.1.1 (Apache, dormant); **scapy (GPL-2)**; pyshark (MIT, needs tshark) | **Keep**. It is complete and dependency-free, and the alternatives are dormant or GPL. Optionally cross-validate it with dpkt in tests | – | – | parity tooling | – |

### UI

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| Drawing canvas | `ui/canvas.py` 739: QGraphicsView, one `QGraphicsPathItem` per contour, `FullViewportUpdate` | QGraphicsView tuned (caching, `MinimalViewportUpdate`, LOD); custom `QOpenGLWidget`/QRhi renderer with VBOs; VisPy 0.17 (BSD); pyqtgraph 0.14 (MIT, plotting-oriented); Qt Quick Shapes (needs a QML UI) | **Keep**, then tune: item caching, LOD for tiny contours, batching each layer into one path, a separate overlay layer for the live track. Move to a `QOpenGLWidget` renderer only if profiling on 50k+ contour sheets demands it. VisPy/pyqtgraph don't fit CAD editing with snapping | S (tune) / L (GL) | Low | – | tuned (Wave 2): raster engraving item, level of detail above 3,000 contours, cached contour paths |
| Docking | plain `QDockWidget` (`mainwindow.py`, `ext_nest.py`, `ext_diag.py`) | **PySide6-QtAds** 5.1.1 (LGPL-2.1, active, abi3 wheels): perspectives, tabs, auto-hide | **Optional replace**: worth it if users want saved layouts. LGPL, same obligations as PySide6 | S–M | Low | – | – |
| Ribbon | `ui/ribbon.py` 152 + `ribbon_spec.py` 292 (own tabbed toolbar) | pyqtribbon 0.7.8 (MIT, via qtpy, last commit 2025-06, slowing) | **Keep**. It is small and data-driven, and the library is stagnating | – | – | layout parity with BCG ribbon | – |
| Property grids / parameter dialogs | `ui/dialogs.py` 323 (QTreeWidget from `params.json`, ~860 params), `ui/params.py` | QtPropertyBrowser has no PySide6 binding; pyqtgraph ParameterTree (MIT) | **Keep / evolve** into a `QTreeView` + model + delegates driven by a typed schema (see config below). ParameterTree adds a big dependency for little gain | M | Low | – | – |
| i18n | `formats/lang.py` 163 + `ui/i18n.py` 177 (reads the original `lang.txt`/`<Lang>.txt`, 11 languages) | Qt Linguist `.ts`/`.qm` + `QTranslator`; gettext/Babel | **Keep** the `lang.txt` reader (compatibility with the original's language packs is a feature). Optionally add a converter `lang.txt` ↔ `.ts` so translators can use Linguist; new strings go under rebuild-specific keys | S | Low | file-format compatibility | – |
| Reports / PDF | `report/printing.py` 133 (QPdfWriter/QPrinter, report.exe port), `window.py` 263 | reportlab 5.0.1 (BSD; major version jump); WeasyPrint 70 (BSD, needs Pango, awkward on Windows) | **Keep** QPdfWriter: zero dependencies, fine for job sheets. Use reportlab only for rich multi-page nest reports later | – | – | report.exe layout | – |
| Plotting | `ui/ext_devices.CurvePlot` (QPainter); matplotlib only in `parity/report.py` | pyqtgraph 0.14 (MIT) for live plots (follower curve, interferometer, burn-in); QtCharts/QtGraphs (in PySide6 Addons) | **Wrap**: pyqtgraph for any live/zoomable plot in the app; keep matplotlib in parity tooling only (move it to a `[parity]` extra) | S | Low | – | – |

### Infrastructure

| Subsystem | Current (files, LOC) | Candidates (status) | Rec. | Effort | Risk | Parity impact | Status (Wave 2) |
|---|---|---|---|---|---|---|---|
| Config / parameters | `formats/params.py` 358 (Hard/Manu/LayerPara XML, descriptor tables), `ui/resources/params.json`, ini via `formats/scfile.py`/`scio.py`, dataclasses everywhere | pydantic 2.14 (MIT) or attrs 26.1 + cattrs (MIT); platformdirs 4.13 (MIT, py ≥ 3.11) | **Wrap**: keep the XML/ini readers and writers (F), and build one typed schema (pydantic or attrs) from `params.json` with min/max/unit/enum for validation and UI generation. Use platformdirs for user data/log dirs instead of the hard-coded `%LOCALAPPDATA%\NexCut` analogue | M | Low | file formats F | – |
| Persistence (job history, material libraries) | text files as the original (`report/records.py` 296: TotalReport.txt/report.txt), JSON in `diag/` | stdlib `sqlite3` | **Add** SQLite for job history and material/process libraries as a rebuild feature, keeping the original's text exports | M | Low | keep text outputs | – |
| Logging | stdlib `logging` in 3 modules; `diag/logs.py` opens dirs | structlog 26.1 (MIT); loguru (slow releases) | **Keep** stdlib logging. Add a central config (rotating file handler in the platformdirs log dir, plus the in-app log dock as a handler). No library needed | S | Low | – | – |
| Packaging | none (editable install only) | PyInstaller 6.22 (GPL + bootloader exception: OK for closed apps); Briefcase 0.4.6 (BSD); cx_Freeze 8.7 (PSF); **Nuitka 4.x is now AGPL-3 with a runtime exception (legal check)**; pyside6-deploy wraps Nuitka | **Add** PyInstaller onedir (LGPL-friendly for Qt) + Inno Setup/MSIX on Windows, `.app`/dmg on macOS, AppImage on Linux. Briefcase if you want one tool for installers | M | Med (Qt plugins, hidapi native lib) | – | **Done** (Wave 2): PyInstaller onedir (`packaging/`, CI workflow `package.yml`), LGPL notices; installers still to do |
| Signing | none | Windows: Azure Trusted/Artifact Signing + signtool; macOS: Developer ID + `notarytool` + hardened runtime | **Add** with packaging | S | Low | – | hooks present (Azure Trusted Signing / codesign + notarytool + stapler), inactive until secrets are set |
| Auto-update | none | tufup 0.10 (MIT, TUF, works with PyInstaller); WinSparkle 0.9.4 / Sparkle 2.10 (MIT); PyUpdater (dead) | **Add later**: tufup, or WinSparkle + Sparkle. Never update during a job | M | Med | – | – |
| Crash reporting | none | sentry-sdk 2.72 (MIT, self-hostable) + stdlib `faulthandler` | **Add** faulthandler now (Qt segfaults are already seen in tests); Sentry opt-in only (privacy, offline shops) | S | Low | – | – |
| Tests | pytest + manual Qt teardown (`conftest.py`), 528 tests | pytest-qt 4.5 (MIT, PySide6); hypothesis 6.170 (MPL-2.0, test-only) | **Add** pytest-qt (qtbot replaces hand-rolled teardown and waits); hypothesis property tests for kerf/offset/intersect/trim/ordering (closed in → closed out, offset distance, precedence kept) | S | Low | parity tests unchanged | hypothesis added (dev group): ordering, kerf, packed wire, native kernel, SVG |
| CI | none (no git repo visible in `rebuild/`) | GitHub Actions matrix Win/mac/Linux × py3.11–3.13, offscreen Qt; parity job on a self-hosted Linux runner with Wine | **Add** | S–M | Low | runs the parity suite | – |

## 2. Build on an existing platform?

**Recommendation: continue standalone in PySide6.** Borrow MIT code and ideas selectively, and treat LinuxCNC/QtPlasmaC as a design reference. Do not port to MeerK40t or any other host.

**MeerK40t** (MIT, wxPython, latest release 0.9.8100 on 2025-08-15; README says "low-maintenance phase"; 388 commits in 2026 vs 1,894 in 2025; about 267k LOC).

Strengths:
- Its architecture is genuinely good. A kernel runs lifecycle-phased `plugin(kernel, lifecycle)` functions.
- Path-registered services and contexts, signals and channels, and a console command language behind the GUI.
- External plugins load through the `meerk40t.extension` entry point.
- A clean device model: device service → driver (`move_abs`, `plot(cutcode)`, `laser_on`, `home`, ...) → controller/transport, with a spooler of LaserJobs carrying CutCode.
- An MCC100 driver plugin would be easy to *package*.

What it does not have:
- Every MeerK40t driver relies on its controller to plan motion. There is no look-ahead, S-curve or corner blending (jerk/lookahead appear only in the GRBL controller code).
- No lead-ins, nesting, pierce stages, gas, height follower or common-edge.
- It does have kerf and tabs, and cut-plan travel optimisation (inner-first, NN, 2-opt).

So a plugin would bring along everything this project has already built and verified: the planner, CContoutSmooth, 1 ms quantisation, FIFO streaming, process items and the cutting state machine. In return it would get a wx GUI and an element tree that don't fit a fiber-sheet workflow. The plugin would also give up:
- PySide6 and the Mlaser-shaped UI (the ribbon, layer/craft model, `.chf` parity and lang packs);
- control over threading for the real-time-sensitive streamer;
- dependence on a project that is slowing down.

Fiber in MeerK40t means galvo marking (Balor/JCZ). I found no gantry sheet-cutting users.

**Verdict on MeerK40t:** a plugin would not beat standalone. It is reasonable, later and cheaply, to ship an *optional* MeerK40t device plugin that feeds MeerK40t CutCode into `mlaser.nc` + `controller`, if there is demand from MeerK40t users. The kernel/plugin pattern is also worth copying into the rebuild's own `ext_*` loader (entry points instead of the hard-coded `EXTENSIONS` list).

**Other platforms:**

- **Rayforge** (MIT, GTK4, very active, 1.12.0 on 2026-10-02)
  - Has the cleanest permissive CAM pipeline: kerf, tabs, lead-in/out post-processors, travel optimisation, material/recipe library, addon manager.
  - Its controllers do the motion planning, and it is GTK.
  - Use: a source of MIT code and patterns, not a host.
- **LinuxCNC / QtPlasmaC** (GPL-2, PyQt5, PREEMPT_RT Linux)
  - The architecture matches best: 1 kHz servo thread, a jerk-limited TP in 2.10, `hm2_eth`-style per-cycle UDP to an Ethernet card.
  - QtPlasmaC already has the sheet-cutting process model: material files (kerf, pierce height/delay, cut height), hole-cutting rules, cut recovery forward/back, height control.
  - Blocked by Linux-RT only (the users are on Windows) and GPL.
  - Use as the *specification* for the process state machine, material schema, hole rules and recovery. Re-derive, don't copy.
- **Not suitable:** LaserGRBL (GPL, C#, Windows), LightBurn (proprietary, no driver API), VisiCut/LibLaserCut (LGPL Java, CO2 fablab), bCNC (GPL Tk), CNCjs and OpenBuilds (G-code senders), LaserWeb4 (AGPL, dormant since 2018), FreeCAD CAM (milling-oriented), Kiri:Moto (browser, basic laser).
- **Deepnest/deepnest-next:** the core is GPLv3, so it can only run as an external process. sparrow/spyrrow is the better permissive route.
- **CypCut and SheetCAM** are the commercial references for features, not code sources.
- No open-source gantry fiber sheet-cutting CAM/controller exists. The rebuild would be filling that gap, which argues for keeping it standalone and well-modularised (see the next paragraph) rather than subordinate to a host.

**Make it embeddable.** `mlaser.cam` → `mlaser.motion` → `mlaser.nc` → `mlaser.controller` is already Qt-free. Publishing it as a headless library with a stable `Document → Job → stream` API is the best way to "integrate with" other platforms: a MeerK40t or Rayforge driver, or a CLI. It also costs little.

## 3. Top recommendations (in priority order)

Status after Wave 2 (performance and libraries track, docs/dev/PERFORMANCE.md): 1 done (all
imports declared, extras `devices`, `plots`, `ortools`, `nest`, `ruckig`, `modern`); 2 done; 3
done (optional mode); 4 done (opt-in, see the kerf row); 5 done (optional engine); 6 done
(optional profile); 7 done; 8 done for PyInstaller onedir + signing hooks (installers and
auto-update open); 9 done in software (optional profile), hardware validation open; 10 kept.

1. **Fix the dependency declaration.** Declare segno, fontTools, hidapi and Pillow, and add extras for pyserial/QtSerialPort and matplotlib. Add faulthandler, pytest-qt and a CI matrix. (S)
2. **SVG: replace the parser with svgelements** (MIT, vendor or pin it). It fixes `<use>`, CSS, rounded rects and visibility at no parity cost. (S)
3. **DXF: add a "modern import" option** with ezdxf `recursive_decompose`/`ezdxf.path`/`text2path`, covering HATCH boundaries, DIMENSION/ATTRIB, MINSERT and SHX. Keep the DxfParse-faithful default. (M)
4. **Kerf fallback: pyclipper + arc re-fit** instead of shapely buffer. Keep the analytic offset (parity). (S)
5. **Nesting: add a sparrow/spyrrow engine** behind `Nester` (verify the spyrrow licence first; jagua-rs is MPL-2.0). Keep the raster nester as the fallback. (M)
6. **Ordering mode 20: optional OR-Tools backend** with precedence constraints. Avoid LKH/elkai (non-commercial). (M)
7. **Text: fontTools + uharfbuzz** for Draw→Text, giving CJK and complex-script shaping without needing Qt fonts. (S–M)
8. **Packaging:** PyInstaller onedir + signing (Azure/Apple notarisation). Updates via tufup or WinSparkle/Sparkle later. Do not use Nuitka until the AGPL runtime exception is cleared. (M)
9. **Modern motion mode (optional, last):** own look-ahead + Ruckig Community per segment, behind a switch, with the faithful planner as the default. Hardware validation is required first. (L, high risk)
10. **Keep** all faithful ports: motion, smoothing, nc, frames, process, `.chf`, DXF default path, CircleFit, crafts, import sort, crtmath. Also keep the small self-contained modules where libraries add nothing: pcap reader, Modbus-RTU codecs, ribbon, lang.txt i18n, QPdfWriter reports, engrave numpy ops.
