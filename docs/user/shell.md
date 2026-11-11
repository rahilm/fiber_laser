# The mlaser window: workspaces, command palette, user modes, themes

mlaser opens with a menu bar and a rail of task workspaces. The original's ribbon is still there
for anyone who prefers it (see [Classic ribbon](#4-classic-ribbon)).

```
┌ menu bar: File Edit View Draw Process Workspace Machine Tools Window Help (macOS: the global one) ┐
├ top bar ─────────────────────────────────────────────────────────────────────────────┐
│ part.chf ●     [Search commands…  Ctrl+K]   ◆ SIMULATOR ● DISARMED  Engineer ▾  English ▾ │
├ safety state banner (the one alarm block while an alarm is active) ──────────────────┤
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

After the run bar's buttons: what Start will cut (*Start: gear.dxf*), and while a job runs a thin
progress line with the percentage, the elapsed time and the ETA. On the Run workspace, whose
own screen shows all of that in large type, the run bar is one row: the machine state and the
progress line.

## 1. Menu bar and top bar

The **menu bar** holds every command once: **File**, **Edit**, **View**, **Draw**, **Process**,
**Workspace**, **Machine**, **Tools**, **Window** and **Help**; a plugin's commands appear in it
too. It follows the platform: on macOS it is the global menu bar, and *About mlaser*,
*Settings…* (⌘,), *Plugins…* and *Quit* are in the **mlaser** menu; on Windows *Options…* is at
the end of Tools and *Exit* at the end of File; on Linux *Preferences…* is at the end of Edit and
*Quit* at the end of File. These guides write menu paths as on Windows and Linux, for example
**Machine › Procedures › Commissioning checklist**.

The **top bar** below it:

* **File name** — the open file, `Untitled` for a new or imported drawing. A **●** after the
  name means unsaved changes (also in the window title).
* **Search commands** — opens the [command palette](#3-command-palette).
* **State chips** — the safety state at a glance: `◆ SIMULATOR`, `◌ DRY-RUN`,
  `▲ ARMED` / `● DISARMED`, `✖ FAULT`, `! ALARM`, `– OFFLINE`. Each chip says its state in
  words and with a symbol, never by colour alone; hover for an explanation. A chip appears only
  for a state the safety banner below the top bar does not show itself; today the banner shows
  them all (the laser state, the motion, the alarm block and the violet SIMULATOR chip), so the
  top bar does not change when an alarm comes. The banner and the run bar at the bottom are
  described in [docs/SAFETY.md](../SAFETY.md).
* **User mode** — Operator, Engineer or Service ([§5](#5-user-modes)).
* **Language** — switches immediately. Languages other than English come from your own Mlaser
  install (**File › Parameters › Import language files…**).

### Files

**File › Open** (`Ctrl+O`) reads mlaser projects (`.mlz`) and the original's `.chf`, DXF, SVG
and G-code. **File › Save** (`Ctrl+S`) saves a new drawing as an `.mlz` project (the drawing
plus the technology, the job queue and the nest); a drawing opened from a `.chf` is saved back
to that `.chf`. **File › Export .chf…** writes the interchange `.chf` for the original program.
Closing, quitting, **File › New** or opening another drawing with unsaved changes asks *Save* /
*Don't Save* / *Cancel*.

## 2. Workspaces

The rail on the left switches between the tasks; so do the **Workspace** menu and `Ctrl+1` …
`Ctrl+6`, from anywhere.

| Workspace | Key | What it is for | Who sees it |
|---|---|---|---|
| **Design** (default) | Ctrl+1 | the canvas with the drawing tools: select, node edit, pan, import, line, rectangle, circle, text, undo/redo, align, mirror, rotate, array, zoom | Engineer, Service |
| **Process** | Ctrl+2 | technology, leads, kerf, micro-joints, cutting order, preview | Engineer, Service |
| **Nest** | Ctrl+3 | parts, sheets and remnants, nest results; the *Orders* tab | Engineer, Service |
| **Run** | Ctrl+4 | the operator screen: load a job, home, frame, cut, alarms, breakpoint resume | everyone |
| **Report** | Ctrl+5 | production, job statistics, costs, alarm history | everyone |
| **Machine** | Ctrl+6 | the pages *Connection*, *Homing and jog*, *Procedures*, *Monitors*, *Setup* and *Alarms* ([Machine](machine.md)) | everyone (Operator: connection, homing and jog, the procedures it may run, alarms) |

Each workspace shows the panels it needs (Design: layers/parts, run control, log; Nest: the
nest panel and the log; Report: the log ...). A panel you open yourself stays open.

Design and Process show the canvas with a workspace toolbar above it (Design: the drawing tools;
Process: leads, kerf, micro-joints, start points, cutting order, toolpath display); Nest, Run,
Report and Machine have their own screens. The Run workspace hides the other panels so STOP,
Hold and Start always stay in view. Plugins can add workspaces of their own; they appear in the
rail after Machine (`Ctrl+7` …).

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

**View › Classic ribbon** switches to the original's layout: the tabbed ribbon on top
(Start, Common, Process, Draw, System analysis, Advanced, Nest, CAM+, Devices, Options+,
Process+, plus a *Workspaces* tab), the canvas in the middle and the panels around it; no rail
and no top bar.
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

**Switching.** The user-mode box in the top bar, or **View › User mode** (`Ctrl+Shift+M` opens
*User mode...*).

**PINs.** In Service mode, **View › User mode › User mode PINs...** sets an optional PIN (4–12 digits) for
Engineer and for Service. Switching *up* to a protected mode then asks for its PIN; switching
down never does. With an Engineer PIN mlaser always starts in Operator mode, with only a
Service PIN at most in Engineer mode. Only a salted hash (PBKDF2-SHA256) of each PIN is stored;
there are no vendor passwords. A forgotten PIN is removed by deleting the `shell.pin.*` lines
from `config.json` in the mlaser configuration folder.

## 6. Themes

**View › Appearance** (or Settings › Appearance): **System appearance** (the default), **Light**,
**Dark** and **High contrast** ("safety glasses": black, white and yellow). The choice is
remembered. Icons follow the theme. The canvas follows it as
well: light background in the light theme, the original's black in the dark and high-contrast
themes. Layer colours keep their hue but are darkened just enough to stay visible on a light
canvas, and they stay distinguishable from each other, also for red-green colour-blind users
(deuteranopia and protanopia, about 1 in 12 men): two layers that differ by hue only, such as
the green, yellow and red of layers 1, 4 and 9, are drawn at different lightness. Hovering a
part on the canvas names its layer, so a layer is never told by its colour alone.

| Theme | text / window | smallest layer contrast on the canvas | smallest difference between two layers (ΔE) | the same for red-green colour-blind vision |
|---|---|---|---|---|
| Light | 14.7 : 1 | 3.0 : 1 | 16.2 | 10.5 |
| Dark | 13.0 : 1 | 4.8 : 1 | 19.3 | 10.5 |
| High contrast | 21 : 1 | 4.8 : 1 | 19.3 | 10.5 |

(WCAG asks 4.5 : 1 for text and 3 : 1 for graphics.) The colour stored in your files does not
change with the theme.

**Settings › Appearance › Colour-blind-safe layer colours** (off by default) draws the layers in
a palette made for red-green colour blindness instead (based on Okabe and Ito's set: bluish
green, sky blue, reddish purple, yellow, blue, orange, vermillion and greys, adjusted per theme):
any two layers then differ by at least 15 ΔE for deuteranope and protanope vision as well as
normal vision, against about 10 with the drawing's own colours. It applies at once, to the
canvas, the Run live view and the layer tables' swatches. It changes only what you see: the
drawing's layer colours are kept, saved and exported as they are.

The planned toolpath's parts differ by line style as well as colour: rapids dashed, micro-joint
gaps (laser off) dotted, lead-outs dash-dotted, the kerf-compensated path on a translucent band
(shown where the kerf is at least 2 pixels wide), a ring at each pierce point; while a job runs
the cut track is drawn wider than any planned line and the head is a ringed cross. Every one of
these, the markers and the Run view's done parts has a colour of its own in every theme, at
least 12 ΔE from the others also for red-green colour-blind vision; none is red or amber, which
stay for alarms, STOP, hazards and the laser being on. The legend in the canvas bar (the arrow
after *Toolpath*) draws each one as it appears on the canvas; it opens by itself only in a tall
canvas, so it does not cover the drawing. The rulers' numbers are in the theme's ruler colour
and the caption font. The safety banners keep their fixed colours in every theme and always
say their state in words.

With the theme left at **System** (the default when nothing was chosen), mlaser follows the
operating system's contrast setting: Windows "Contrast themes", macOS "Increase contrast", or a
GNOME / desktop-portal high-contrast setting switch it to the high-contrast theme, live where
the platform reports the change. A theme you choose yourself always wins. Links follow the
theme too (also after a live light/dark switch).

## 7. Keyboard and accessibility

The full list is [keyboard.md](keyboard.md) (**Help › Keyboard shortcuts**, `Ctrl+F1`, or `?` on the
canvas; generated from the same table). The most used:

| Key | Does |
|---|---|
| `Ctrl+K` | command palette |
| `F1` | help (the user guide, offline) |
| `Ctrl+1` … `Ctrl+6` | workspaces |
| `F5` | Start, or Continue a held job (pre-flight check first); Run workspace only |
| `F6` / `Shift+F6` | Frame / Dry run; Run workspace only |
| `Space` | Hold while a job runs (never continues) |
| `Esc`, `F12` | Stop while a job runs (`F12` always) |
| `V` `H` `N` | select, pan, node edit (canvas) |
| `L` `K` `J` `S` `P` | lead, kerf, micro-joint, sort, simulate (canvas) |
| `Ctrl+Z` / `Ctrl+Y` | undo / redo |

One-letter keys work while the canvas has the keyboard focus, so typing in a field never
triggers them. `Tab` moves through the window in a fixed order: top bar, rail, workspace
toolbar, canvas, run bar. Every control has an accessible name for screen readers (a test
walks the accessibility tree of every workspace).

Screen readers (VoiceOver, Narrator, NVDA, Orca) are told about what changes on its own: a new
alarm or fault is announced at once with its remedy, then "Alarms cleared"; a safety state
change (armed, disarmed...) and a job's start and end are announced politely. The canvas
describes the drawing ("4 parts, 1 selected. Selected: Part 2 of 4: ...") and, while it has the
keyboard focus, `Page Down` / `Page Up` select the next / previous part (`Home` / `End` the first
/ last) and announce it; the arrows still nudge the selection. The painted views (the live
sheet on Run, the nest result, the report chart, the lead preview, the follower calibration
curve) describe what they show.

## 8. Screen sizes and the layout

The window is laid out for 1280 × 800 and for a 1920 × 1080 touchscreen at 150 % scaling
(1280 × 720 logical): the run bar is never clipped, and below 1400 px the layers/parts panel
shares one tabbed column with the run control so the canvas keeps its width. Workspace
toolbars show their labels when there is room and icons only (with tooltips) when not. Your
panel arrangement is remembered; **Window › Panels** shows or hides a panel, and **Window ›
Reset layout** (`Ctrl+Shift+F12`) restores the default.

## 9. First start

The first start shows a **welcome sheet**:

* **Practice on the simulator** — *Connect the simulator* runs the built-in motion-card
  simulator over the real protocol: import, simulate, frame and cut as on the machine.
* **Your Mlaser parameters and languages** — *Import parameters…* reads `HardPara.xml` and
  `ManuPara.xml` from the `dat` folder of your original Mlaser install and shows what would
  change before anything is applied ([Importing your Mlaser setup](importing-mlaser-setup.md));
  *Import languages…* reads its `Lang` folder.
* **Connect your machine** — set the card's address on the Machine workspace's *Connection*
  page, then *Open the commissioning checklist* ([Machine](machine.md#commissioning-checklist-first-connection)).
  The checklist needs Service mode; in another mode the button reads *Switch to Service mode
  and open the checklist* (it asks for the PIN when one is set).
* **The safety model** — every connection starts disarmed; arming is a confirmed operator
  action; Start, Frame and Dry run run a pre-flight check; while a job runs Space holds (it never continues) and
  Esc/F12 stop; F5, F6 and Shift+F6 act only on the Run workspace; a fault latches until it is reset ([docs/SAFETY.md](../SAFETY.md)).
* **Help** — this page and the keyboard shortcuts.

The sections sit in two columns when one would be taller than the screen, so the sheet fits a
1280 × 800 screen without scrolling. Untick *Show this sheet at the next start* to keep it closed;
**Help › Welcome** opens it again.
