# The Machine workspace and the guided procedures

The **Machine** workspace (command `machine.workspace`) gathers everything about the machine
itself: connecting, homing and jogging, origins, the guided procedures, the device monitors,
the hardware settings and the alarm history.  Until the workspace switcher of the new shell is
in place it opens as its own window.

Screens: `work/w3/machine/ws_*.png` and `wiz_*.png` (offscreen captures).

## Sections

| Section | What it does | Lowest user mode |
|---|---|---|
| Connection | Simulator or real motion card (IP address, port), Connect / Disconnect, the live status (connection, safety state, address, link, homed) and the network self-test (`mlaser --selftest`), optionally with one status request to the card | Operator |
| Homing and jog | Position in mm, Home X/Y, X, Y, X/Y/Z, focus homing; **hold-to-jog** X± Y± Z± (release stops; a step size of 0.1 / 1 / 10 mm makes one click one step; the jog stops when the window loses focus or is hidden, docs/SAFETY.md §8); coordinate system, set origin, go to origin, clear the sheet rotation | Operator |
| Procedures | The guided procedures below, each with the mode it needs | per procedure |
| Monitors | Controller, follower (FTC), autofocus and EtherCAT monitors as panels (they poll only while visible); the ribbon's monitor commands open them here | Engineer |
| Hardware settings | The hardware parameter sections with readable names, a search box, typed editors with units and ranges; Apply validates every value; read-only below Service mode.  Changes reach the card at the next connect.  **Safety I/O...** opens the interlock and heartbeat settings ([below](#safety-io-interlocks-and-the-pc-alive-heartbeat)) | Engineer (view), Service (edit); Safety I/O: Engineer |
| Alarms | Every card alarm and latched fault with raise and clear time, duration, code, text and **what to do**; Reset alarms; Clear history.  Kept in `alarm_history.jsonl` in the data folder | Operator |

User modes (Operator / Engineer / Service) come from the shell.  Without them every section is
open.  A command above the current mode says which mode it needs instead of running.

## Safety I/O: interlocks and the PC-alive heartbeat

**Machine > Hardware settings > Safety I/O...** (command `machine.safety_io`, Engineer and
Service mode; also offered by the commissioning checklist) sets which card inputs the
interlocks read and which card output drives the watchdog relay (docs/SAFETY.md §8 and §11):

| Setting | Meaning |
|---|---|
| Gas-low interlock | input of the low-pressure switch; trips (FAULT, halt) during a cut |
| Door / guard interlock | input of the door or guard switch; trips during a cut |
| Laser-source alarm interlock | input wired to the source's alarm output; trips while armed or running, and refuses arming |
| Alarm when | input ON (normally open contact) or input OFF (normally closed: a broken wire trips too, the safer choice) |
| This machine has no such sensor | the service engineer's declaration: the pre-flight stops warning, the Run screen still lists it |
| Follower alarm interlock | the on-board follower's alarm word (on by default) |
| PC-alive heartbeat | the output toggled while mlaser runs normally (0 = off) and its frequency (5 Hz) |

Inputs are 1..28, outputs 1..26, 0 = not wired.  Every input starts at 0 because the wiring of
a machine is unknown until it is commissioned, but "not configured" is never silent: the **Run**
screen shows an amber line naming every interlock and the heartbeat that is not configured, the
**pre-flight** of a laser job on a real machine lists them as warnings (to acknowledge), and the
**commissioning checklist** flags them.  The dialog refuses a heartbeat on a process output
(laser, gas, shutter...).  Values are stored at once (`safety.*` in `config.json`) and reach the
controller immediately when the machine is disarmed and idle; while a job runs or the machine is
armed the change waits ("change pending" on the Run screen) and applies as soon as it is
disarmed.  Check every input with the commissioning checklist's wiring test after a change.

## Guided procedures

Every procedure has the same frame: the step list on the left, the instructions, an amber
**Safety** box, the step's own page, and the buttons Back, Run (the step's action), Yes / No
(questions), Next / Finish and **Abort**.

* **Abort** (the red button, Esc or F12) is always enabled.  It stops the machine at once
  (`safe_halt`: decelerated stop, laser off, outputs off, follower stop) in every step, also
  when nothing moves, and cleans up (disarm, cancel the follower calibration).
* Steps that move the axes or fire say so in their safety box, and start only when you press
  their button.  After an abort the next step needs a new button press.
* **Dry run on the simulator** (first step): the whole procedure runs against a private
  simulator, with a rotated sheet, a disc or a follower as needed.  Results are shown but never
  applied or saved.  The real machine is not touched.

### Edge seek (sheet position and angle)

Four variants: **capacitive 3 points** (two on one edge, one on the other), **capacitive 4
points**, **disc centre** (jog over the disc first) and **two points** (jog the red light to
two points of one edge and press Record each time).

1. Before you start: the sheet lies flat, the follower is calibrated, the area is clear.
2. Probe parameters: start point (or the current position), sheet size, spacing ratios, the
   origin offset from the corner, the move-out speed and follow height, in mm and mm/s.
3. Check: connected, no fault, follower configured, corner dock point.
4. Measure: the head probes each point (or the two recorded points are used).
5. **Check the measured sheet**: the outline, the measured points, the corner (red) and the
   origin (green) and the angle are drawn in the wizard and on the canvas.  Abort if it looks
   wrong.
6. Apply rotation: the drawing is rotated by the measured angle about the job origin.  This is
   one undo step (Ctrl+Z).  For the disc, "Also set the work origin here" sets the origin at the
   centre.

### Follower calibration

Put a clean, flat, grounded sheet under the nozzle.  The follower must be homed (as in the
original).  Calibrate lowers the head and measures; the curve (raw capacitance over height) is
plotted with its grade and checks (points, span, monotonic) and saved as `calib.csv` in the
mlaser data folder (the previous file is kept as `.bak`).

### Focus homing

Homes the motorised focus for the configured autofocus type (network head, EtherCAT bridge, IO
pulse or a card axis) and, for a card axis, waits until it reports homed.

### Laser power test

Short pulses in place at chosen duty levels (for example onto a power meter).  The machine is
armed **only inside this procedure**: the Arm step asks for a confirmation, the Fire step asks
again, and the machine is disarmed at the end, on Abort and on any error.  It is refused in
dry-run mode (`--laser-off`), with active alarms or a latched fault.  Enter the measured power
per pulse on the Results page.

### Commissioning checklist (first connection)

Run it once when a machine is connected for the first time, and after wiring changes.  Switch
the laser source off (key switch) first.

| Check | Pass when | Fix offered |
|---|---|---|
| Network and connection | the self-test has no failure and the card is connected | – |
| Card scaling | the card's pulses/mm (X, Y) and interpolation period equal the machine parameters | – |
| Soft limits | the card's soft limits equal the axis strokes (`MAC.SoftLimitMaxLen`) | write the soft limits |
| Interlocks | every interlock is wired or declared "not fitted"; then the **wiring test**: trigger each wired sensor when the checklist asks (open the door, close the gas supply, set the source alarm) and release it; the interlock monitor must see it go active and clear within 30 s each (a dry run toggles the simulator's inputs) | Configure safety I/O... |
| Homing | Home X/Y finishes and the axes report homed | – |
| Watchdog heartbeat | the "PC alive" output toggles at its frequency, and the watchdog relay drops when you press **Hold heartbeat** (docs/SAFETY.md §11) | set the output in Safety I/O |
| Jog direction X / Y | a +10 mm jog increases the position, and you confirm the head moved right (X+) / away from you (Y+) | reverse the axis (`MAC.AxisReverse`, written at the next connect) |

The report (`commissioning/commissioning-<date>.json` in the data folder) lists every check
with the measured values.

## Import from my Mlaser installation

Procedures › Import (Service mode) takes the settings of an existing Mlaser installation:

1. choose the install folder, and the parameter folder (`%LOCALAPPDATA%\NexCut` or a copy of
   it; it is found automatically when it sits in the install folder as `dat` or `NexCut`);
2. **Preview** lists every change, grouped by area, with the current and the imported value;
3. untick what you do not want, then **Apply**.

| Area | From | To |
|---|---|---|
| Machine parameters | `HardPara.xml`, `ManuPara.xml` (or `SystemPara.xml`) | the configuration (machine profile "params") |
| Layer table | `LayerPara.xml` | a copy in the mlaser data folder, used as the machine layer table |
| IP addresses | `File/ipAdd.ini` | the IP settings, and a copy for the network self-test |
| Rapid settle time | `JumpAddTime.txt` | the machine profile |
| Follower calibration | `Help/calib.csv` | `calib.csv` in the data folder |
| Languages | `Lang/*.txt` | the imported translations |

The original installation is only read: mlaser refuses to write anything inside the install
folder or the parameter folder.  The previous `config.json` is kept as a dated `.bak`.
