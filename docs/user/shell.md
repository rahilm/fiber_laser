# The mlaser window: workspaces, command palette, user modes, themes

mlaser opens in a **task-based shell**. The original's ribbon is still there for anyone who
prefers it (see [Classic ribbon](#4-classic-ribbon)).

```
┌ top bar ─────────────────────────────────────────────────────────────────────────────┐
│ [Mlaser ▾]  part.chf ●     [Search commands…  Ctrl+K]   ◆ SIMULATOR ● DISARMED  Engineer ▾  English ▾ │
├ safety state banner (and the alarm banner while an alarm is active) ─────────────────┤
│ rail │ workspace toolbar                                                              │
│ ✎ Design  ├──────────────────────────────────────────────┬────────────────────────────┤
│ ⚙ Process │ canvas, or the workspace's own screen        │ panels (docks) of the      │
│ ▦ Nest    │                                              │ workspace                  │
│ ▶ Run     │                                              │                            │
│ ▤ Report  │                                              │                            │
│ 🔧 Machine│                                              │                            │
├ run bar: state · Start F5 · Hold Space · STOP Esc/F12 · Frame F6 · Dry run · Arm · progress ┤
└ status bar ──────────────────────────────────────────────────────────────────────────┘
```

## 1. Top bar

* **Mlaser ▾** — the application menu: File, Workspaces, Settings (theme, classic ribbon,
  user mode, PINs, options, imports, reset layout) and Help (command palette, keyboard
  shortcuts, welcome sheet, these docs, about).
* **File name** — the open file, `Untitled` for a new or imported drawing. A **●** after the
  name means unsaved changes (also in the window title).
* **Search commands** — opens the [command palette](#3-command-palette).
* **State chips** — the safety state at a glance: `◆ SIMULATOR`, `◌ DRY-RUN`,
  `▲ ARMED` / `● DISARMED`, `✖ FAULT`, `! ALARM`, `– OFFLINE`. Each chip says its state in
  words and with a symbol, never by colour alone; hover for an explanation. The full-width
  safety banner below the top bar and the run bar at the bottom stay as described in
  [docs/SAFETY.md](../SAFETY.md): the chips only repeat the state.
* **User mode** — Operator, Engineer or Service ([§5](#5-user-modes)).
* **Language** — switches immediately. Languages other than English come from your own Mlaser
  install (Settings > Import languages).

### Files

File > **Open** (`Ctrl+O`) reads mlaser projects (`.mlz`) and the original's `.chf`, DXF, SVG
and G-code. File > **Save** (`Ctrl+S`) saves a new drawing as an `.mlz` project (the drawing
plus the technology, the job queue and the nest); a drawing opened from a `.chf` is saved back
to that `.chf`. File > **Export CHF** writes the interchange `.chf` for the original program.

## 2. Workspaces

The rail on the left switches between the tasks. `Ctrl+1` … `Ctrl+6` do the same from anywhere.

| Workspace | Key | What it is for | Who sees it |
|---|---|---|---|
| **Design** (default) | Ctrl+1 | the canvas with the drawing tools: select, node edit, pan, import, line, rectangle, circle, text, undo/redo, align, mirror, rotate, array, zoom | Engineer, Service |
| **Process** | Ctrl+2 | technology, leads, kerf, micro-joints, cutting order, preview | Engineer, Service |
| **Nest** | Ctrl+3 | parts, sheets and remnants, nest results | Engineer, Service |
| **Run** | Ctrl+4 | the operator screen: load a job, home, frame, cut, alarms, breakpoint resume | everyone |
| **Report** | Ctrl+5 | production, job statistics, costs, alarm history | everyone |
| **Machine** | Ctrl+6 | connect, home, edge seek, devices, options, monitors, firmware | everyone (Operator: home and connect only) |

Each workspace shows the panels it needs (Design: layers/parts, run control, log; Nest: the
nest panel and the log; Report: the log ...). A panel you open yourself stays open.

Process, Nest, Run, Report and Machine are being rebuilt in Wave 3. Until a workspace is
finished it shows its classic commands: as a toolbar above the canvas (Process, Nest, Run) or as
large buttons on an empty page (Report, Machine). Plugins can add workspaces of their own;
they appear in the rail after Machine (`Ctrl+7` …).

## 3. Command palette

`Ctrl+K` (or the search box) opens the palette over the window. Type any part of a
command's name: letters may be spread out (`mj` finds *Micro Joint*, `hxyz` *Home XYZ*), the
workspace name works too (`nest`). Each row shows the command, its workspace and its key.

* `Enter` runs the selected command, `↑` `↓` choose, `Esc` closes.
* Commands you used recently come first (★).
* Commands your user mode does not allow are not listed; commands that are not available right
  now (for example *Frame* without a machine) are greyed.
* The palette runs the command exactly like its button: the pre-flight check, the job lock and
  the user mode apply.

## 4. Classic ribbon

Settings > **Classic ribbon** switches to the original's layout: the tabbed ribbon on top
(Start, Common, Process, Draw, System Analysis, Advanced, Nest, CAM+, Devices, Options+, plus a
*Workspaces* tab), the canvas in the middle and the panels around it; no rail and no top bar.
The choice is remembered. `Ctrl+K`, all shortcuts and the safety banners work the same.

Ribbon labels are never cut off any more: long labels wrap onto two lines like the original's
(*Micro / Joint*), a tab that does not fit the window scrolls sideways, and every button has a
tooltip with its full name and key.

## 5. User modes

| Mode | Can do | Cannot do |
|---|---|---|
| **Operator** | Run, Report and a limited Machine workspace: load a job (open, task import), connect, home, frame, dry run, start / hold / stop, alarms, breakpoint resume, view options | edit the drawing or any parameter (layers, options, technology); service functions |
| **Engineer** | everything except the service functions | firmware, card/hardware parameters, monitors and diagnostics |
| **Service** | everything, including firmware upgrades, card and hardware parameters, IP settings, monitors and diagnostics, the PINs | – |

In Operator mode buttons, lists and the rail get **large touch targets** (at least 48 px; the
run bar's are 56 px).

**PINs.** In Service mode, Settings > *User mode PINs…* sets an optional PIN (4–12 digits) for
Engineer and for Service. Switching *up* to a protected mode then asks for its PIN; switching
down never does. With an Engineer PIN mlaser always starts in Operator mode, with only a
Service PIN at most in Engineer mode. Only a salted hash (PBKDF2-SHA256) of each PIN is stored;
there are no vendor passwords. A forgotten PIN is removed by deleting the `shell.pin.*` lines
from `config.json` in the mlaser configuration folder.

## 6. Themes

Settings > Theme: **Light** (default), **Dark** and **High contrast** ("safety glasses": black,
white and yellow). The choice is remembered. Icons follow the theme. The canvas follows it as
well: light background in the light theme, the original's black in the dark and high-contrast
themes. Layer colours keep their hue but are darkened just enough to stay visible on a light
canvas, and they stay distinguishable from each other:

| Theme | text / window | smallest layer contrast on the canvas | smallest difference between two layers (ΔE) |
|---|---|---|---|
| Light | 14.7 : 1 | 3.0 : 1 | 14.9 |
| Dark | 13.0 : 1 | 4.8 : 1 | 20.8 |
| High contrast | 21 : 1 | 4.8 : 1 | 20.8 |

(WCAG asks 4.5 : 1 for text and 3 : 1 for graphics.) The colour stored in your files does not
change with the theme. The safety banners keep their fixed colours in every theme and always
say their state in words.

## 7. Keyboard and accessibility

The full list is [keyboard.md](keyboard.md) (Help > Keyboard shortcuts, `Ctrl+F1`, or `?` on the
canvas; generated from the same table). The most used:

| Key | Does |
|---|---|
| `Ctrl+K` | command palette |
| `F1` | help (the user guide, offline) |
| `Ctrl+1` … `Ctrl+6` | workspaces |
| `F5` | Start (pre-flight check first) |
| `F6` / `Shift+F6` | Frame / Dry run |
| `Space` | Hold / continue while a job runs |
| `Esc`, `F12` | Stop while a job runs (`F12` always) |
| `V` `H` `N` | select, pan, node edit (canvas) |
| `L` `K` `J` `S` `P` | lead, kerf, micro-joint, sort, simulate (canvas) |
| `Ctrl+Z` / `Ctrl+Y` | undo / redo |

One-letter keys work while the canvas has the keyboard focus, so typing in a field never
triggers them. `Tab` moves through the window in a fixed order: top bar, rail, workspace
toolbar, canvas, run bar. Every rail, toolbar and top-bar control has an accessible name for
screen readers.

## 8. Screen sizes and the layout

The window is laid out for 1280 × 800 and for a 1920 × 1080 touchscreen at 150 % scaling
(1280 × 720 logical): the run bar is never clipped, and below 1400 px the layers/parts panel
shares one tabbed column with the run control so the canvas keeps its width. Workspace
toolbars show their labels when there is room and icons only (with tooltips) when not. Your
panel arrangement is remembered; Settings > **Reset layout** (`Ctrl+Shift+F12`) restores the
default.

## 9. First start

The first start shows a **welcome sheet**:

* **Practise on the simulator** — *Connect the simulator* runs the built-in motion-card
  simulator over the real protocol: import, simulate, frame and cut as on the machine.
* **Your Mlaser parameters and languages** — *Import parameters…* reads `HardPara.xml` and
  `ManuPara.xml` from the `dat` folder of your original Mlaser install; *Import languages…*
  reads its `Lang` folder.
* **The safety model** — every connection starts disarmed; arming is a confirmed operator
  action; Start, Frame and Dry run run a pre-flight check; while a job runs Space holds and
  Esc/F12 stop; a fault latches until it is reset ([docs/SAFETY.md](../SAFETY.md)).
* **Help** — this page and the keyboard shortcuts.

Untick *Show this sheet at the next start* to keep it closed; Help > Welcome opens it again.
