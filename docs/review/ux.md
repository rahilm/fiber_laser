# UX review: mlaser rebuild (PySide6) vs. modern fiber-cutting software

Date: 2026-10-18 · Scope: `rebuild/mlaser/ui/*` with all 8 extensions, in-process MCC100 simulator · Read-only review (no repo file changed).
Screenshots: `work/review/shots/` · Driver scripts: `work/review/scripts/` (`shot1.py`, `flow.py`, `preflight.py`).

## 0. Summary

The rebuild is a faithful and technically strong port. The CAM, planner, streamer and simulator all work end to end: I imported, ran leads, kerf, micro-joints and sort, simulated, framed, dry-ran, cut, paused and resumed, raised an alarm, nested, opened the bitmap and edge-seek tools, all on the simulator. The **UX, though, is the original's 2010s BCG ribbon copied 1:1, plus extensions bolted on**. It inherits the original's weaknesses and adds some of its own:

* **Safety and run state are under-communicated.** I found no pre-flight check. A job with open contours and a job 50× larger than the table both started with one click and no warning. You can open a new file while a job runs. Alarms appear only as a log line; the status bar still said `MCC <Ready>` while a servo alarm was latched. The run panel shows no progress or ETA. Start/Pause/Stop have no keyboard shortcut.
* **Layout breaks at common sizes.** At 1400 px the key Process commands show as `L…e`, `C…e`, `Mi…nt`, `Ma…rt`. At 150 % scaling on a 1080p touchscreen (a typical machine PC), **Start/Pause/Stop are scrolled out of the Run panel entirely**. The ribbon is fixed at 118 px and uses raster icons.
* **Parameters are a raw register dump.** Layer parameters are a ~160-row property grid with group "-", a column titled "Default", no units, `Gas Type = 0`, and no material or thickness concept. Hardware options are tabs named `AxisParam` and `MachineAxisConfig_0`.
* **Feedback leaks internals.** Examples: raw lang keys (`mf207`), unformatted printf (`Estimate processing time: %dmin %0.1fsec …`), Python exceptions (`autosave.chf not written: unsupported operand type(s) for /: 'list' and 'float'`), protocol errors (`edge-seek failed: RecvDataErr (code 502)`), and a yes/no question printed as a log line.
* **Every modern feature an operator values is missing.** None of the following exists: a technology/material library, cost estimate, job queue and production report, remnant stock, cut-path scrubber, live progress, remote monitoring, operator/engineer modes, dark theme or command search. CypCut, BodorThinker, Bystronic and TRUMPF all have most of these (§5).

**Top recommendation:** keep the ribbon commands as the "expert" surface, reachable through a command palette. Put a **task-based shell on top: Design → Process → Nest → Run → Report**, built on three new cores:
1. a **Technology database** (material × thickness × gas × nozzle → layer + pierce parameters);
2. a **Pre-flight check** that gates Start;
3. a **full-screen, touch-first Run screen** with clear machine state, progress/ETA, hold/stop, and alarms with remedies.

That combination is what CypCut, Bystronic ByVision and TRUMPF HomeZone/TecZone users expect.

---

## 1. What I did (workflows driven, click counts)

Setup: `MainWindow(UiController(), extensions=EXTENSIONS)`, `connect_simulator(time_scale=10)`, samples `gear.dxf` and `mix.dxf`. "Clicks" are the minimum mouse actions for a first-time user at 1600×1000, not counting typing. In the "≈ target" column, *auto* means a pre-flight step the proposed design would perform by itself.

| # | Workflow | Path in the rebuild | Clicks | Friction observed | ≈ target |
|---|---|---|---|---|---|
| 1 | Connect + home | Advanced → Connect simulator, then **two consecutive modal prompts** (`mp119` "connect OK, go origin?" then `mf156` "System will go origin, continue?") | 3–4 | The double confirmation fires at startup. My first scripted run hung on it. Log says `X (go origin done)` in EN and `X、Y轴回原开始 (go origin done)` ("homing *started* (done)") in ZH: wrong key mapping. | 1 (one banner: "Machine not homed – Home now") |
| 2 | Import DXF | File ▾ → Open → file dialog → Open | 4 | Fine. No import report: open contours, duplicates and out-of-table parts aren't flagged. The mix.dxf parts were only drawn red. | 3 + import summary card |
| 3 | Set layer parameters | Select parts → colour swatch → double-click layer row (or row + "Layer Parameters") → scroll a 160-row grid → OK | 5–7 | No material/thickness. No units. Gas is the integer `0`. Group header "-". Column "Default" holds the *current* value. Duplicate "Smooth Type". Editing in the table needs `float()` text, and invalid input is silently ignored. [02](shots/02_layer_params_dialog.png) | 2 (pick "Mild steel 3 mm N₂" in a library) |
| 4 | Leads | Ctrl+A → Process → `L…e` (elided) → dialog → OK | 4 | No diagram or preview, no units, "Start Point Mode" checkbox with no explanation. Log prints the raw key `mf207`. [03](shots/03_lead_dialog.png) | 1 (part of the technology) |
| 5 | Kerf | Process → `C…e` → type/width → OK | 3 | Defaults to *Outside*. No preview of the offset. [04](shots/04_kerf_dialog.png) | auto |
| 6 | Micro-joints | Process → `Mi…nt` ▾ → params → OK | 4 | Typo "Min-size of **mico** joint graph". No units. Gaps invisible on selected parts because the dashed white selection style hides them. [05](shots/05_microjoint_dialog.png), [07](shots/07_simulating.png) | 2 |
| 7 | Sort | Process → Sort ▾ → mode | 3 | Feedback is only `sort: mode 5` in the log. The path is not redrawn in a way that shows a change, and the order count/rapid length isn't shown. | 1 |
| 8 | Simulate | Run panel → Sim (horizontally clipped at ≤1400 px) | 1–2 | No scrubber, speed or progress. The estimate goes only to the log, with unformatted placeholders: `Estimate processing time: %dmin %0.1fsec, cut length: %0.2fm … (estimated time 21.7 s …)`. [07](shots/07_simulating.png) | 1, with a timeline + cost panel |
| 9 | Frame / Dry run | Run panel → Frame / Dry Cut | 1 each | Works. Dry run logs `cut 0.0 mm … [DRY RUN: laser disabled]`, which is good, but the UI shows no "DRY RUN" state. Every job logs `autosave.chf not written: unsupported operand type(s) …` (a bug surfaced as UI text). | 1 |
| 10 | Start job | Run panel → Start | 1 | **No pre-flight and no confirmation.** It started with open contours (mix.dxf, plus a `RuntimeWarning: divide by zero` in `motion/profile.py:51`). It also started a job scaled ×50 (cut 18.3 m, head at Y −258 mm, outside the drawn table) with no warning. [pf_out_of_bounds](shots/pf_out_of_bounds.png) | 1 + pre-flight sheet |
| 11 | Pause / resume | Pause; resume = click Start **or** Pause again | 1 / 1 | On pause, the log says both "paused … breakpoint at wire 15868" **and** "process stopped". The state isn't shown on the canvas or panel; only the status-bar text `MCC <Pause>` changes. | 1 / 1 with a big state banner |
| 12 | Handle alarm | Notice the log line → Alarm tab → scroll the Run panel to "Clear Warning" (in the *Coordinate* group) → Start | 3–4 | No banner, sound, modal or colour change. The status bar keeps `MCC <Ready>` while the alarm is latched. The alarm table has no severity, remedy or "cleared" state. "Cannot resume while alarms are active" appears only in the log. [12](shots/12_alarm_during_job.png), [13](shots/13_alarm_tab.png), [pf_alarm_midjob](shots/pf_alarm_midjob.png) | 1–2 (alarm banner with remedy + Reset) |
| 13 | Nest | Nest tab → New → Import Part (file dlg) → Import Sheet (dlg) → Nest Params (dlg) → Advanced Nest → "Copy to maun" → Start tab | 9–11 | **No graphical preview of the nest result.** You see only a table row "Result_1 (Sheet1) 2 0.1 %" until you copy it over the main drawing. Default 3000×1500 sheet. Part names "0#1". Old cut track stays on top of the new layout. [17](shots/17_nest_result.png), [18](shots/18_nest_in_main.png) | 4–5 in a dedicated Nest workspace |
| 14 | Engrave bitmap | File ▾ → Bitmap → Open Image (dlg) → sliders → Generate Gcode → back | 6+ | A separate mini-app (PicConvert) with its own *Language* combo. English is poor ("contrast ratio", "Set laser power size (>0)"). Empty white preview. [19](shots/19_bitmap_dialog.png) | 3, as an image object with properties |
| 15 | Edge seek (capacitive) | Start → Edge Seek ▾ → Capacitive → 12-field form → OK | 4 | Hard-coded English labels (not translated), no diagram, no units. Failure appears only as `edge-seek failed: RecvDataErr (code 502)` in the log. [20](shots/20_edge_capacitive_dialog.png) | 2, as a wizard with a diagram + live probe |
| 16 | Options | Start → Options / Advanced → Options | 2 | Both dialogs are titled "Options". Tabs show internal names (`AxisParam`, `MachineAxisConfig_0`). Empty values, no search, no units. [22](shots/22_options_software.png), [23](shots/23_options_hardware.png) | Settings with search + sections |

Other observations:

* At startup the log printed `The system abnormal termination, recovered to the last processing state? (…use mf138 System Recovery…)`: **a question with no way to answer it**.
* You can open a new file while a job is running, and the canvas document is replaced mid-cut. Frame during a job is silently ignored.
* There are no keyboard shortcuts for Start/Pause/Stop/Frame/Sim. Only 11 accelerators exist in total; Space/Esc aren't bound to Hold.

## 2. Findings by area (with evidence)

### 2.1 Layout, HiDPI, touch
* **Elided ribbon labels.** At 1400 px wide the Process tab shows `L…e`, `Pat…art`, `Ho…ef`, `C…e`, `Mi…nt`, `Ma…rt`, and Advanced shows `Rec…ect Connect`, `…imulator`, `U…e`, `Netw…Test`, `Lig…est`, `…f-locking]`. At 150 % even Sort and Tools are elided. [ribbon_all_tabs_1400](shots/ribbon_all_tabs_1400.png), [tab2_en_1280x720@1.5](shots/tab2_en_1280x720.png). The cause is `Ribbon.setFixedHeight(118)` plus `Qt.ElideRight`, with no collapse-to-small or overflow behaviour.
* **Run Control dock is too narrow.** It is 300 px wide but its content needs about 380 px. Stop, Sim, Z-jog, the 3rd DO column and the BF length spin box are clipped behind a horizontal scrollbar at every size I tried. At 150 % scale on a 1920×1080 screen, the Start/Pause/Stop buttons are **below the fold**. [tab0_en_1400x900](shots/tab0_en_1400x900.png), [tab2_en_1280x720](shots/tab2_en_1280x720.png)
* **Touch targets are too small.** Jog arrows are 40 px icons, DO buttons ~22 px tall, checkboxes 13 px. Recommended minimums are about 48 dp, and 15 mm for gloved industrial use.
* **Icons are raster.** They are 32/16 px PNG strips sliced at runtime and look blurry at 150 %. There are no SVG/@2x assets.
* **Visual inconsistency.** The CAM+ and Devices tabs are plain text buttons with no icons or group captions. [ribbon_all_tabs_1400](shots/ribbon_all_tabs_1400.png) A large red **Stop** sits in *System Analysis → Process*, far from the run panel. The **Laser** button on the Start tab sets the laser *type*, while the **Laser** button on the run panel *fires* the laser.
* **Weak canvas visuals.** No machine bed or sheet is drawn by default. Selection paints everything dashed white, which hides layer colour, leads and micro-joint gaps. The cut track from the previous job stays over a new nest layout. [18](shots/18_nest_in_main.png)

### 2.2 Safety and run state
* **No pre-flight check.** Open contours, out-of-bounds parts, an unhomed machine, missing layer parameters (gas pressure 0, focus empty) and overlapping duplicates are not checked before Start. [pf_out_of_bounds](shots/pf_out_of_bounds.png)
* **Status is inaccurate.** `MCC <Ready>` showed while an X servo alarm was latched. [12](shots/12_alarm_during_job.png) "FTC <Offline>" shows in simulator mode with no explanation.
* **No laser-armed or dry-run indicator.** `--laser-off` and dry run only show up in a log line. The DO-panel "Laser" is a press-and-hold fire button next to "RedLight", with the same style and no guard.
* **The document is not locked during a job.** Opening, editing and nesting all stay enabled while cutting.
* **Confirmations are in the wrong places.** There are two consecutive homing prompts at connect, but none for the dangerous actions: Start with warnings, laser fire, clearing an alarm and resuming, or firmware upgrade (there is a dialog, but it isn't explained).

### 2.3 Feedback, errors and i18n
* **Raw keys and untranslated strings in the log.** `mf207`, `mp76` (the `LogPanel.log` key translation only works when the message contains a colon). The estimate line prints raw printf placeholders. Two languages get mixed in one line, e.g. ZH `硬件重连成功 (connected to MCC …)`.
* **Hard-coded English bypasses the lang files.** The edge-seek dialog (12 labels), "Avoid cut holes (rapids)", the Devices tab buttons and some diag labels. Grep finds hard-coded `addRow/QLabel/addItem("English…")` calls: 12 in `ext_devices.py`, 3 in `ext_diag.py`, 2 in `ext_machine.py`, and 1 each in `ext_nest.py` and `dialogs.py`. Tabs "CAM+" and "Devices" stay English in ZH. [tab2_zh_1280x800](shots/tab2_zh_1280x800.png)
* **The original's English catalogue is poor and now ships as-is.** "Copy to maun", "Roasting machine test" (burn-in), "Select Inside Mould", "Pline", "Contour Select", "Cut/Extension" (for Trim/Extend), "Fine-tuning / Fine-tuning", "SelectedManu", "Clear Warning", "Hardware connect successfully". A curated English (and German) layer is needed on top of `lang.txt`.
* **Errors carry no remedy.** `RecvDataErr (code 502)`, Python exception text, and `card alarm: X servo input alarm (code 8104)`. None says what to check.

### 2.4 Parameters and process knowledge
* There is no material, thickness, gas or nozzle concept. The tech library (`A250418_*`, `db*`) is listed in docs/ui.md as P1 but isn't implemented.
* Layer table columns: Speed, Power, Freq, Gas, Pressure, Height, all without units. Pierce stages, focus, slow start, etc. are hidden in the 160-row grid.
* Nest parameters, sheet dialog, micro-joint, lead and kerf are each a separate modal dialog. Four styles are in use: tabbed form, form, `ParamGridDialog` tree, and hand-built form.

### 2.5 What's good (keep)
* The data-driven ribbon (every command is a `QAction` with an id) makes a command palette and task-based UI cheap to add.
* Correct, fast CAM/estimates: planner estimate 21.72 s vs replay 21.75 s. Breakpoint resume works.
* Undo/redo is applied to all edits. Part list ↔ canvas selection stays in sync.
* There is an 11-language catalogue and live language switching.
* The simulator is end-to-end through the real protocol. It's ideal for an onboarding "practice mode".

## 3. Prioritised recommendations

Effort: **S** ≤ 3 days, **M** 1–3 weeks, **L** > 3 weeks (one developer).

### P0: safety, correctness, can't-operate-it problems

| # | Problem (evidence) | Proposal | User value | Effort |
|---|---|---|---|---|
| P0-1 | No pre-flight: open contours, ×50 out-of-bounds job, no homing check. Start = 1 click ([pf_out_of_bounds](shots/pf_out_of_bounds.png)) | **Pre-flight check** run on Start, Frame and Dry run. Errors block: out of soft limits/table, not homed, active alarm, layer with no parameters, gas pressure 0 on a cut layer, laser disabled vs. real cut. Warnings need acknowledging: open contours to be cut, duplicates/overlaps, tiny features < kerf, parts without leads, unsaved doc. Show one sheet listing the issues; click an issue to zoom to it. Offer "Fix" buttons (close gaps, dedupe, apply technology). | Prevents crashes into the clamps, ruined sheets and wasted gas, which are the most expensive mistakes on a multi-kW machine | M |
| P0-2 | Run panel clipped. At 150 % Start/Stop are off-screen ([tab2_en_1280x720](shots/tab2_en_1280x720.png)) | Fixed-position **Run bar**, never scrolled: Start, Hold, Stop, Frame, Dry-run, a state pill, progress and ETA, docked at the bottom or right in every workspace. Minimum 56 px targets. Space = Hold, Esc×2 / F12 = Stop, F5 = Start (with pre-flight), F6 = Frame | Operators can always find Stop | S |
| P0-3 | Alarm is only a log line. Status says Ready during an alarm ([12](shots/12_alarm_during_job.png)) | **Alarm banner**: red bar across the canvas with alarm text, time and axis, a **remedy** ("Check X servo drive; reset drive; then Reset alarm"), and *Reset* / *Details* buttons. The state pill turns red ("ALARM"). Optional sound and tower light. Write remedies for the ~200 `gp*` alarms (start with the 30 most common). | Faster recovery, less operator guessing | M (banner S, remedy texts M) |
| P0-4 | No laser-armed / dry-run / simulator state indicator | Persistent **mode chip** in the title bar: `SIMULATOR`, `LASER DISABLED`, `DRY RUN`, `LASER ARMED` (amber when emission is enabled). The DO-panel laser fire button gets a guard (hold 0.5 s or a slide-to-arm) and its own style. | Prevents surprises such as firing during "testing" | S |
| P0-5 | Document editable and replaceable during a job | Lock the job document while running: open/import/edit/nest operate on a *copy* or are disabled, with a "Job running" chip. | Avoids desync between what's on screen and what's cutting | S |
| P0-6 | Raw keys, printf placeholders, Python exceptions and protocol codes shown to users (`mf207`, `%dmin`, `unsupported operand…`, `RecvDataErr (code 502)`) | Message layer: (a) fix `LogPanel.log` key handling and the estimate formatting; (b) fix the autosave bug; (c) map every exception or protocol error to a sentence plus next step; (d) keep raw details behind "Details". Add a CI test that flags any log text matching `^[a-z]{1,4}\d+` or `%[0-9.]*[dfs]`. | Trust; supportable error reports | S |
| P0-7 | Start-up double homing modal; a recovery *question* printed to the log | One non-modal start-up card: "Connected ✓ · Not homed → [Home] · Previous job interrupted at 43 % → [Resume] [Discard]" | Clean start of shift | S |

### P1: the modern workflow (biggest gains over the original)

| # | Problem | Proposal | User value | Effort |
|---|---|---|---|---|
| P1-1 | 246-button ribbon with elided labels; CAM+/Devices tabs inconsistent; Stop and Laser buttons duplicated with different meanings | **Task-based shell**: left rail with *Design · Process · Nest · Run · Report* (+ *Machine* for setup/diagnostics). Each workspace has a slim contextual toolbar (≤15 commands). The full 246-command set stays in a **command palette** (Ctrl+K / F1) and an optional "Classic ribbon" for migrating users. The data-driven `ribbon_spec` already supplies ids, labels and icons. | Fewer clicks, learnable, fits on 1366×768 touch PCs | L |
| P1-2 | No material/technology concept; 160-row grid ([02](shots/02_layer_params_dialog.png)) | **Technology database**: material × thickness × gas × nozzle × laser power → cut parameters, pierce strategy (1–5 stages, as in the original), leads, kerf, micro-joint defaults, cool points, and corner and slow-start settings. Import from the original's XML/`.dat`. Version entries, diff them against factory values, mark "tested OK" with a sample photo. Choosing the sheet material in Process applies it to all layers. The layer grid becomes "overrides". Later: a rule-based tuning wizard ("dross on bottom → −5 % speed, +0.5 bar"), as with Bystronic's Parameter Wizard and Bodor AutoMatch. | One-click correct parameters; know-how kept when operators change | L |
| P1-3 | Modal dialogs for lead/kerf/micro-joint/nest, without units or previews ([03](shots/03_lead_dialog.png)–[05](shots/05_microjoint_dialog.png), [16](shots/16_nest_params_dialog.png)) | **Contextual properties panel** (right side) that follows the selection: Part → geometry, layer, lead in/out with a **live mini-diagram**, kerf side, joints, start point; Layer → technology overrides. All fields carry units (mm, mm/s, %, Hz, bar, ms) and ranges. Changes preview live on the canvas; Apply/undo replaces OK/Cancel. | Faster editing; mistakes are visible before they cut | M |
| P1-4 | Simulation without timeline; estimate only in the log, broken | **Preview/Simulate**: scrubber, play/pause, ×1–×50 speed. Rapids, pierces and lead-ins in distinct colours. Optional collision-risk overlay (rapids crossing cut parts, tipping parts). A side card shows total time, cut/rapid length, pierces, gas use and **cost** (machine €/h + gas + material, configurable). Export the estimate. | Quoting, catching path mistakes | M |
| P1-5 | Run state invisible; no progress/ETA ([10](shots/10_job_running.png)) | **Run/Operator screen** (wireframe §4.4): big state, progress %, part n/N, elapsed and remaining time, live speed, power, gas and height, follower state, current/next contour highlighted, cut vs. not-yet-cut colours. Breakpoint resume as "Resume from here / from contour start / pick contour". | Operator confidence; less walking to the PC | M |
| P1-6 | Nest has no graphical result; result replaces the main drawing ([17](shots/17_nest_result.png)) | **Nest workspace**: part list with quantities and priorities, sheet stock (incl. remnants), and graphical result thumbnails per sheet with utilisation. Drag parts manually, common-cut toggle, "Send sheet to Process". | Material savings visible and controllable | M |
| P1-7 | No job queue or production report (the report.exe port exists but is a separate window) | **Job queue** (ordered sheets/jobs with status, start/finish times, actual vs. estimated time) and a **Report** workspace (per-shift/day: jobs, cut length, pierces, uptime/alarm time, cost; CSV/PDF export). The `report` package already has records. | Production tracking without spreadsheets | M |
| P1-8 | Edge seek, FTC calibration and other procedures are forms of unlabelled numbers ([20](shots/20_edge_capacitive_dialog.png)) | **Guided procedures (wizards)**: edge seek, follower calibration, focus/nozzle check, homing, pitch compensation. Step-by-step with the original's diagrams (bitmaps 407/445/447 are already extracted), a live probe readout, and a result card with "apply". | Rarely used procedures done right first time | M |
| P1-9 | i18n: hard-coded English, mixed-language log lines, poor original English | Route every string through the translator. Add a curated `en`/`de` overlay above `lang.txt` (fixes "Copy to maun", "Roasting machine test", …). Add a pseudo-locale test (long strings, RTL check) and a CI check for untranslated literals. | Professional feel; non-Chinese markets | S–M |
| P1-10 | No user roles; anyone can change hardware parameters | **Operator / Engineer / Service modes**, PIN or login (the original has two levels, `A250416/7`). Operator mode: Run, Queue, jog, load job, select technology. Engineer: Process/Tech editing. Service: hardware options, firmware, monitors. Every parameter change goes to an audit log. | Protects tuned machines; simpler UI for operators | M |
| P1-11 | Keyboard: 11 accelerators, none for machine control | Keyboard-first: command palette, one-key tools (L lead, K kerf, J joints, S sort, P preview), Tab between panels, numeric entry on the canvas (type "50" after a move), shortcut cheat sheet (?) | Speed for programmers | S |

### P2: differentiation and polish

| # | Problem | Proposal | User value | Effort |
|---|---|---|---|---|
| P2-1 | No remote visibility (cloud monitor phone-home intentionally omitted) | **Local web/phone monitor**: read-only by default, served on the LAN (FastAPI/WebSocket). Shows state, job, progress/ETA, alarms, queue, and today's report. Push notification on job done or alarm. Optional controlled "Hold" with a PIN. No vendor cloud. | Run unattended / multiple machines | M |
| P2-2 | Light only, raster icons, fixed ribbon height | **Theming**: light, dark, and a high-contrast "safety-glasses" theme (as LaserGRBL does), with design tokens. SVG icon set. Respect the OS scaling and font. | Shop-floor readability, HiDPI | M |
| P2-3 | Remnants not tracked | **Remnant stock**: save leftover sheet outline + material + thickness + location, auto-propose remnants in Nest, optionally mark remnant IDs on the sheet. | Material cost | M |
| P2-4 | No onboarding | **Practice mode** on the built-in simulator (the default when no card is found), first-run tour, contextual "?" links to docs, and an embedded searchable help (the original's `Help` only opens a file). | Shorter training | S–M |
| P2-5 | Bitmap engraving is a separate mini-app ([19](shots/19_bitmap_dialog.png)) | Make an image a canvas object with properties (dither, DPI, power, size) and live preview. Drop the in-dialog language switch. | Consistency | M |
| P2-6 | No camera | Optional **camera overlay** (sheet position/skew, remnant capture), as in LightBurn and Rayforge | Faster sheet alignment, remnant reuse | L |
| P2-7 | Accessibility | Accessible names on icon buttons (jog arrows have none). Never use colour alone (red = open contour → add an icon or label). Keyboard focus order. Respect the font scale. | Compliance, fewer errors | S |
| P2-8 | Status bar is dense text | Replace with structured chips (Controller, Follower, Laser, Gas, position) that turn red on fault and open the relevant monitor on click | At-a-glance health | S |

### Suggested sequence
1. **Sprint 1 (S items):** P0-2, P0-4, P0-5, P0-6, P0-7, P1-11, P2-8.
2. **Sprint 2:** P0-1, P0-3, P1-9.
3. **Then:** P1-2, P1-3, P1-4, P1-5, building towards the P1-1 shell.
4. **Then:** P1-6 to P1-8, P1-10, and the P2 items.

## 4. Proposed information architecture and wireframes

```
Left rail (always visible)          Top bar (always visible)
 ┌───┐                              [≡ Mlaser] job.chf ●  ⌘K Search commands…   [SIMULATOR][LASER DISABLED]  user: operator ▾
 │ ✎ │ Design     draw/import/edit
 │ ⚙ │ Process    technology, leads, kerf, joints, order, preview
 │ ▦ │ Nest       parts, sheets/remnants, nest results
 │ ▶ │ Run        operator screen, queue
 │ ▤ │ Report     production, costs, alarms history
 │ 🔧│ Machine    connect, home, calibrate, monitors, settings (engineer/service)
 └───┘
Persistent run bar at the bottom of every workspace:
 [● READY ] job 3/7 ▓▓▓▓▓░░░ 62 %  ETA 04:12 │ [▶ Start F5] [⏸ Hold Space] [■ Stop] [Frame] [Dry]
```

### 4.1 Design
```
┌────┬──────────────────────────────────────────────────────────────┬──────────────────────┐
│rail│ Tools: Select  Node  Line  Rect  Circle  Text  Import  │ Align ▾  Array  Mirror │
│    ├──────────────────────────────────────────────────────────────┤ PROPERTIES (selection)│
│    │                                                              │ Part 3 "flange"       │
│    │   canvas: machine bed outline, sheet outline, rulers         │  Size  92.0 × 92.0 mm │
│    │   parts coloured by layer; open contours ⚠ badge              │  Pos   X 120  Y 40 mm │
│    │                                                              │  Layer [■ 1 Cut  ▾]   │
│    │                                                              │  Closed ✓  Holes 1    │
│    │                                                              │ Import report (last)  │
│    │                                                              │  ⚠ 2 open contours [Fix]│
│    │                                                              │  ⚠ 4 duplicates  [Fix]│
│    ├──────────────────────────────────────────────────────────────┤                      │
│    │ Parts (tree)  | Layers                                       │                      │
├────┴──────────────────────────────────────────────────────────────┴──────────────────────┤
│ run bar                                                                                   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Process / Technology
```
┌────┬───────────────────────────────────────────┬───────────────────────────────────────┐
│rail│ Material [Mild steel ▾] Thk [3.0 mm ▾]    │ TECHNOLOGY  S235 3 mm · N₂ · 1.5 S    │
│    │ Gas [N₂ ▾] Nozzle [1.5 single ▾] [Apply]  │ ┌ Cut ───────────────────────────────┐│
│    ├───────────────────────────────────────────┤ │ Speed 4200 mm/min  Power 85 %        ││
│    │ Auto: [Leads✓][Kerf✓][Joints○][Order✓]    │ │ Freq 5000 Hz  Gas 12.0 bar  H 0.8 mm ││
│    │                                           │ │ Focus −1.0 mm                        ││
│    │   canvas with lead-ins, joints,           │ ├ Pierce (2 stages) ────────────────── ┤│
│    │   order numbers, rapids                   │ │ 1: H 3.0  P 60 %  t 300 ms  ▁▃       ││
│    │                                           │ │ 2: H 1.5  P 85 %  t 150 ms  ▃▆       ││
│    ├───────────────────────────────────────────┤ ├ Lead in  [Arc ▾] r 2 mm  [diagram]   ┤│
│    │ ◀ ▶ ═══════●═══════════  ×10   00:14/00:22│ │ Kerf  Outside 0.10 mm               ││
│    │ (preview scrubber)                        │ └─ modified from factory: 2 fields [↺] ┘│
│    │                                           │ ESTIMATE  22 s · cut 630 mm · 2 pierces │
│    │                                           │ gas 0.4 m³ · cost €0.63  [Pre-flight ✓] │
├────┴───────────────────────────────────────────┴───────────────────────────────────────┤
│ run bar                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Nest
```
┌────┬───────────────────────┬──────────────────────────────────────────┬─────────────────┐
│rail│ PARTS        qty  pri │  Sheet 1 / 3   3000×1500  S235 3 mm      │ RESULTS         │
│    │ ▣ flange      40   1  │ ┌──────────────────────────────────────┐ │ ▣ Sheet 1 82 %  │
│    │ ▣ bracket     12   2  │ │  ◯◯◯◯ ▭▭ ◯◯◯◯ ▭▭ ...                 │ │ ▣ Sheet 2 79 %  │
│    │ [+ Add from drawing]  │ │  (drag to adjust, common-cut shown)  │ │ ▣ Sheet 3 41 %  │
│    │ SHEETS / REMNANTS     │ └──────────────────────────────────────┘ │  → remnant R-12 │
│    │ ▭ 3000×1500 ×5        │ Spacing 3 mm  Edge 5 mm  Rot 90° ▾       │ Total 3 sheets  │
│    │ ◩ R-07 1200×800 (S235)│ [Nest ▶]  Mode: speed ◄──●──► yield      │ [Send to queue] │
├────┴───────────────────────┴──────────────────────────────────────────┴─────────────────┤
│ run bar                                                                                   │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 Run / Operator (touch-first, full-screen capable)
```
┌────┬──────────────────────────────────────────────────────────┬─────────────────────────┐
│rail│  ● CUTTING   Sheet 2/3 · part 17/40 · contour 112/380      │  ┌───────────────────┐  │
│    │  ▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░  62 %   elapsed 06:51  ETA 04:12    │  │   ■  STOP          │  │
│    ├──────────────────────────────────────────────────────────┤  └───────────────────┘  │
│    │                                                          │  ┌─────────┐┌────────┐  │
│    │   live canvas: done = grey, current = bright,            │  │ ⏸ HOLD  ││ ▶ START │  │
│    │   next = outline; head marker; follower height bar       │  └─────────┘└────────┘  │
│    │                                                          │  [Frame] [Dry run]      │
│    │                                                          │  Resume: [here][contour]│
│    ├──────────────────────────────────────────────────────────┤  JOG  (hold-to-move)    │
│    │ Speed 4180 mm/min  Power 85 %  Gas N₂ 12.1 bar  H 0.80 mm │       [ ▲ ]             │
│    │ Follower ● following   Laser ● armed   Shutter ● open     │  [ ◀ ][HOME][ ▶ ]  Z▲  │
│    ├──────────────────────────────────────────────────────────┤       [ ▼ ]       Z▼  │
│    │ ⚠ ALARM  X servo input alarm (8104)          12:03:11     │  step [0.1][1][10][∞]  │
│    │ Remedy: check X drive display / cable; reset drive.       │  ┌───────────────────┐  │
│    │ [Reset alarm]  [Details]  [Call log]                      │  │ QUEUE  ▶ job 3/7  │  │
│    │                                                          │  └───────────────────┘  │
└────┴──────────────────────────────────────────────────────────┴─────────────────────────┘
min target 56 px · Space = Hold · Esc Esc = Stop · colour + text + icon for every state
```

## 5. Competitive feature matrix (short)

Y = yes, P = partial, N = no, ? = unverified. The mlaser column is from this review. The other columns come from a web benchmark of vendor docs and press (sources below). Several vendor cells are marketing claims, not hands-on tests.

| Feature | **mlaser rebuild** | Original Mlaser | CypCut/CypNest | FSCUT/HypCut | BodorThinker | TRUMPF TruTops | Bystronic | LightBurn | Lantek/SigmaNEST | MeerK40t/LaserGRBL/Rayforge |
|---|---|---|---|---|---|---|---|---|---|---|
| Material/thickness technology library | N | P (A250418) | Y | Y? | Y | Y? | Y | Y | Y? | P / N / Y |
| Parameter wizard / auto-tune | N | N | N? | ? | Y (AutoMatch) | P | Y | N | N | N |
| Multi-stage pierce | Y (grid) | Y | Y | Y (9-stage) | ? | Y? | Y? | N | Y | N |
| Pre-flight check before run | N | P (bounds msg) | P? | ? | ? | Y? | Y (guided) | P | n/a | N |
| Scrubbable path animation + time | P (no scrub) | P | Y | Y | ? | Y? | Y? | Y | Y? | P/Y/? |
| Cost estimate / quote | N | P (report.exe) | Y | ? | ? | Y? | Y | N | Y | N |
| Nesting + graphical result | P (no preview) | Y (SDK) | Y | Y | Y | Y | Y | P | Y | N |
| Remnant management | N | P | Y | ? | Y | Y | ? | N | Y | N |
| Job queue / order pool | N | N | P | Y | P | Y | Y? | N | Y | N |
| Production reports | P | P | Y | Y? | Y | Y | ? | N | Y | N |
| Breakpoint resume | Y | Y | Y | Y? | ? | ? | ? | N? | n/a | N? |
| Alarm decode with remedy | N (decode only) | N | P | ? | ? | ? | Y? | – | n/a | P (LaserGRBL) |
| Remote / phone monitoring | N (omitted) | P (vendor cloud) | P (RayBox) | Y (Laser Cloud) | Y (Bodor+) | Y (Monitor/Fab App) | Y (live stream) | N | P | N |
| Camera alignment | N | N | P (edge find) | P | P | ? | Y | Y | n/a | Y/N/Y |
| Touch-first HMI | N | P (dark big buttons) | N | P? | P? | Y? | Y (22") | N | n/a | N |
| Operator vs engineer modes | N | P (2 levels) | ? | ? | ? | Y (HomeZone/TecZone) | P | N | ? | N |
| IA style | ribbon | ribbon | ribbon | ribbon? | ? | task zones | guided touch | toolbar | ? | docks/toolbar/GNOME |
| Command search | N | N | N? | ? | ? | ? | ? | N | ? | P (MeerK40t console) |
| Dark / high-contrast theme | N | Y (dark) | N? | ? | ? | ? | ? | ? | ? | ?/Y (goggle)/Y |
| Open source / scriptable | Y (Python) | N | N | N | N | N | N | N | N | Y |

Benchmark highlights relevant to this review:
* **TRUMPF TruTops Boost** splits HomeZone (automated order flow) from TecZone (expert, "shows only the functions needed for each work step"). That is the model for the task shell plus classic or expert access.
* **Bystronic ByVision** uses a 22" touchscreen, guides the operator through programming and production, and draws parameters from a database keyed on material, thickness and geometry. Its *Parameter Wizard* targets less experienced operators.
* **BodorThinker AutoMatch** is a picture-based tuning wizard. Bodor+ adds IoT monitoring, OTA updates and maintenance orders.
* **CypCut/CypNest** is the de-facto baseline: the same ribbon IA as Mlaser, but with per-material process saving, multiple pierce modes, breakpoint resume before/after, remnant import, and Excel production and pricing reports. BOCHU's *Laser Cloud* app adds mobile monitoring.
* **LightBurn** has a material library, a preview scrubber with red rapids and time, and camera alignment. Its known weakness is time estimates that ignore acceleration, which mlaser's planner already beats (21.72 s planned vs. 21.75 s replayed).
* **Lantek and SigmaNEST** market collision avoidance (avoid-cut rapids, tip-up micro-joints) and remnant stock with exact shape and material certificate. mlaser already has avoid-cut rapids (`sort.avoid`), but buries it in a sort drop-down with a hard-coded English label.
* **LaserGRBL** decodes alarms and has a safety-glasses colour scheme. **MeerK40t** has dockable panes and a console. **Rayforge** has material recipes, camera and libadwaita theming.

**Where mlaser can win:** it is the only open, scriptable option with a real motion planner and a simulator in the box. Built-in practice mode, honest estimates, a local web monitor with no vendor cloud, and a command palette are cheap differentiators that no Chinese controller suite offers.

Sources: LightBurn docs (docs.lightburnsoftware.com: camera alignment, optimisation settings), Thunder Laser KB on estimate accuracy, OMTech and MechStream CypCut overviews, Piranha (CypCut pierce modes), bochu.com (CypNest, FSCUT8000/HypCut, RayBox), App Store "激光云", bodor.com (BodorThinker, AutoMatch), DirectIndustry (Bodor+), trumpf.com (TruTops Boost, Smart View), The Fabricator (HomeZone/TecZone), machinery.co.uk (TruTops Monitor), bystronic.com (convenience package / Parameter Wizard, ByCut Eco 22"), lantek.com (2020 collision avoidance, remnant handling), sigmanest.com, github.com/meerk40t, lasergrbl.com, rayforge.org. Not verified: the CypCut PDF manual contents, forum evidence of operator priorities, and Bystronic BySoft mobile.

## 6. Screenshot index

| File | Shows |
|---|---|
| `ribbon_all_tabs_1400.png` | All 9 tabs stacked at 1400 px: elisions, inconsistent CAM+/Devices tabs, original translation errors |
| `tab*_en_1400x900.png` | Each tab with mix.dxf loaded; run panel clipped |
| `tab*_zh_1280x800.png` | Chinese UI: CAM+/Devices untranslated, mixed-language log, Start half visible |
| `tab*_en_1280x720.png` | `QT_SCALE_FACTOR=1.5` (= 1080p @150 %): Start/Stop out of view, blurry icons |
| `01_import_gear.png`, `06_after_craft_sort.png` | Import and craft results |
| `02`–`05` | Layer-param grid, lead, kerf, micro-joint dialogs |
| `07_simulating.png` | Sim: no scrubber; estimate with raw `%d` placeholders in the log |
| `08`–`11` | Frame, dry run, job running (no progress/ETA), paused |
| `12`–`14`, `pf_alarm_midjob.png` | Alarm: log-only; status still "Ready"; alarm table without remedy |
| `15`–`18` | Nest: sheet/params dialogs, table-only result, result copied into main with stale track |
| `19_bitmap_dialog.png` | PicConvert engraving mini-app |
| `20`, `21` | Edge-seek dialogs (hard-coded English, no diagram) |
| `22`, `23` | Software and hardware options (both titled "Options", internal tab names) |
| `24_onekey_dialog.png`, `27_controller_monitor.png` | One-key planning; controller monitor |
| `pf_out_of_bounds.png` | Job ×50 larger than the table started without warning |
