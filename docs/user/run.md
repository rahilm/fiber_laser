# Run workspace: operator screen and job queue

Open it with the command `workspace.run` (until the workspace switcher exists it opens in its own
window; **F11** toggles full screen). Everything on this screen goes through the same safety path
as the run bar: arming question, pre-flight check, the planned job, the safety supervisor
(docs/SAFETY.md). Nothing here bypasses it.

## The state display

| State | Colour | Meaning |
|---|---|---|
| READY | grey (amber while armed) | connected and idle; the line next to it says whether the laser is armed, disarmed or disabled (dry-run mode) |
| RUNNING | dark slate | cutting, dry run or framing (the line says which) |
| HOMING / MOVING | dark slate | the axes are homing or moving to a position; Start, Frame and Dry run wait ("Homing in progress"), STOP stops the move |
| HOLD | grey | held, or paused at a breakpoint (with its X/Y position) |
| ALARM / E-STOP | red | a card alarm or the emergency stop is active (code and text) |
| FAULT | dark red | a fault is latched (the reason); nothing moves until **Reset** |
| OFFLINE | grey | no machine connected |

Colour is never the only cue: every state has its text and an icon.

Below it: the sheet number of a queued job, **part n/N** and **contour n/N** (from the job's
contour markers), a progress bar, the elapsed time and the **ETA** (the planner's estimate at
the start, the measured rate as the job advances).

An **amber line** under the header names every interlock (gas-low, door, laser-source alarm)
and the PC-alive heartbeat that is not configured, and says when a Safety I/O change is waiting
for the machine to be disarmed ([machine.md](machine.md#safety-io-interlocks-and-the-pc-alive-heartbeat)).
It disappears once everything is wired; a sensor declared "not fitted" stays listed as such.

## Live view and process values

The live view draws the job as it is placed on the machine: parts already cut grey and dashed,
the part being cut thick in the accent colour, the others in their layer colour, the head as a
red dot. The row underneath shows each value under its name: speed, the layer's power (%), gas
and pressure, cut height and the X / Y / Z position, in the display units; the figures keep their
width as they change, and in a narrow window the row wraps between two values, never inside one.
When the link is lost the last values stay, greyed, marked *Last known values*.

## After a job: where it stopped, and cutting the rest

When a job ends the screen keeps it until the next start: a line under the state says
*Last job stopped at 14:02 in part 5 of 6 (4 finished), after 2:15 · gear.dxf*, or
*Last job finished at 14:03: all 6 parts, in 2:40*. The live view numbers every part in the
order it was cut and draws the finished ones grey and dashed. A part the job was cutting when
it stopped counts as not finished, even if most of it was cut.

The section **Cut the rest: new job** then offers:

* **Cut the n remaining parts**: a new job of the parts the last job did not finish;
* **Start from part n**: a new job from that part of the cut order to the end. Type the number,
  or click the part in the live view; the live view shows which parts that job would leave out
  (dotted).

Both select those parts (you see the selection in Design too) and start them exactly like
*Start selection only*: the pre-flight check and the arming question run as for Start. Each part
is cut from its beginning, with its own pierce. This is not **Resume from the breakpoint**, which
continues inside the contour where a held or faulted job stopped. When the drawing changed since
that job (an edit, another file, other technology) the offer is withdrawn and the line says why;
while a job is held at a breakpoint, resume it or stop it first. *Cut remaining parts* is also in
the Start button's menu of the run bar and in the command search.

## Buttons

All targets are at least 56 px high (44 px for the secondary ones), for gloves and touch screens.

* **STOP** (Esc, F12): stops at once; the halt goes out first.
* **HOLD** (Space): holds the job. Space only holds: pressing it again does not continue.
* **START / CONTINUE** (F5 on this workspace): starts, or continues a held job; START runs the
  pre-flight check. F5, F6 (Frame) and Shift+F6 (Dry run) act only on the Run workspace and not
  while you type in a field; on another workspace they bring you here and focus the button.
* **Frame**, **Dry run**: laser off.
* **Arm laser / Disarm laser**: asks for confirmation before arming. Arming lasts for one cut:
  the machine is disarmed again when a job that fires the laser ends, is stopped or fails, and
  the next Start asks "Arm and start" again. A hold keeps the arming for Continue, and the queue
  keeps it between its sheets ([safety](safety.md#arming)).
* **Resume from the breakpoint: Here / Contour start**: after a pause or a fault reset, continue
  exactly where the job stopped or from the start of that contour (a laser-off rapid back and a
  re-pierce). Arming is asked first when the job fires the laser; the controller checks the
  resumed job again (pre-flight).

## The machine PC during a job

While a job runs, mlaser keeps the computer from going to sleep on its own (macOS and
Windows; the screen may still turn off).  On macOS it also opts out of App Nap for as long as
it runs, so its safety display and timers keep their pace when the window is hidden or covered.
On Linux, switch off automatic suspend in the desktop's power settings on the machine PC.
None of this replaces the machine's own safety chain: the card halts by itself when the PC
stops talking to it ([safety](safety.md)).

## Alarms

When an alarm or a fault is active, the alarm list shows each one with its time and a **remedy**
(what to check, then what to press), with **Reset alarm** and **Details**.

## Job queue

The **Queue** tab lists jobs (document files) with quantity (sheets), done count, status,
material, sheet, estimated and actual time. Add files, reorder, change the quantity, skip,
run again or clear finished jobs. The queue is saved after every change
(`<data dir>/production/queue.json`) and survives a restart; a sheet that was running when the
program ended is shown as *interrupted*.

**Run queue** puts the screen in queue mode. Before every sheet the operator loads the sheet and
presses **Sheet loaded: start**: the next job is opened and started through the normal Start
(pre-flight, arming). The queue never starts a sheet by itself. Arming is asked before the first
sheet only: the machine stays armed between the queue's sheets, is disarmed when a sheet is
stopped or fails, and when the last sheet is done. While a large sheet is being checked the
queue says "Checking the sheet's job…" and the window stays responsive: STOP or **Leave
queue** in that time cancels the start (nothing moves, the sheet waits for a new confirmation).

While the queue waits, the **Load this sheet** card shows, in large type, the material and
thickness, the sheet size, the remnant id and the sheet number (n/N), with a picture of the
sheet and its nest; below it the technology and nozzle on its layers, the part count and the
orders on the sheet (customer · order: quantity × part). The live view shows that sheet, not the
previous drawing. A red line on the card warns when the sheet does not match the technology.

**Material check.** Before a sheet starts (from the queue or with Start), its material and
thickness are compared with the technology on every layer it uses. A mismatch (a 1 mm aluminium
technology on a sheet queued as 3 mm mild steel) is a **blocking** pre-flight error. Load the
right sheet or apply the right technology; if you are sure, **Override and cut** in the
confirmation lets this one sheet start, and the override is recorded in the audit log
(`material_override`). A layer set by hand (no technology) or a sheet without a material only
gives a warning. A drawing that is not a queued or nested sheet has no declared material and is
not checked. A stopped sheet stays in the
queue (confirm to run it again, or skip it). The planned-quantity interlock of the production
counters applies.

Sheets sent from the Nest workspace carry their sheet outline: after a sheet is cut, the unused
part is saved as a remnant (see nest.md), and a remnant sheet is marked *used*.
Settings: `plugin.run.remnant_auto`, `remnant_min_width` (mm), `remnant_min_area` (mm²).

The command `queue.add_current` adds the open (saved) document to the queue.

### Jobs submitted from outside

Jobs from the command line (`mlaser queue add`) or the [automation API](automation.md) never go
into the queue by themselves. They appear above the queue table under *submitted from outside*,
with where they came from, the file, quantity, material and the note; the queue card says how
many wait. **Approve** adds the job to the queue (it still runs only after **Run queue** and
**Sheet loaded: start**); **Reject** removes it. While the automation API runs, the header shows
**Automation API ON** with its address.
