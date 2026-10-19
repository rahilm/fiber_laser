# Run workspace: operator screen and job queue

Open it with the command `workspace.run` (until the workspace switcher exists it opens in its own
window; **F11** toggles full screen). Everything on this screen goes through the same safety path
as the run bar: arming question, pre-flight check, the planned job, the safety supervisor
(docs/SAFETY.md). Nothing here bypasses it.

## The state display

| State | Colour | Meaning |
|---|---|---|
| ● READY | blue | connected and idle; the line next to it says whether the laser is armed, disarmed or disabled (dry-run mode) |
| ▶ RUNNING | green | cutting, dry run or framing (the line says which) |
| ⏸ HOLD | amber | held, or paused at a breakpoint (with its X/Y position) |
| ⚠ ALARM | red | a card alarm is active (code and text) |
| ⚠ FAULT | dark red | a fault is latched (the reason); nothing moves until **Reset** |
| ○ OFFLINE | grey | no machine connected |

Colour is never the only cue: every state has its text and an icon.

Below it: the sheet number of a queued job, **part n/N** and **contour n/N** (from the job's
contour markers), a progress bar, the elapsed time and the **ETA** (the planner's estimate at
the start, the measured rate as the job advances).

An **amber line** under the header names every interlock (gas-low, door, laser-source alarm)
and the PC-alive heartbeat that is not configured, and says when a Safety I/O change is waiting
for the machine to be disarmed ([machine.md](machine.md#safety-io-interlocks-and-the-pc-alive-heartbeat)).
It disappears once everything is wired; a sensor declared "not fitted" stays listed as such.

## Live view and process values

The live view draws the job as it is placed on the machine: parts already cut in grey, the part
being cut thick and orange, the others in their layer colour, the head as a red dot. The strip
underneath shows speed (mm/s), the layer's power (%), gas and pressure (bar), cut height (mm) and
the X/Y/Z position (mm).

## Buttons

All targets are at least 56 px high (44 px for the secondary ones), for gloves and touch screens.

* **STOP** (Esc, F12): stops at once; the halt goes out first.
* **HOLD / START (CONTINUE)** (Space / F5): hold and continue; START runs the pre-flight check.
* **Frame**, **Dry run**: laser off.
* **Arm laser / Disarm laser**: asks for confirmation before arming.
* **Resume from the breakpoint: Here / Contour start**: after a pause or a fault reset, continue
  exactly where the job stopped or from the start of that contour (a laser-off rapid back and a
  re-pierce). Arming is asked first when the job fires the laser; the controller checks the
  resumed job again (pre-flight).

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
(pre-flight, arming). The queue never starts a sheet by itself. A stopped sheet stays in the
queue (confirm to run it again, or skip it). The planned-quantity interlock of the production
counters applies.

Sheets sent from the Nest workspace carry their sheet outline: after a sheet is cut, the unused
part is saved as a remnant (see nest.md), and a remnant sheet is marked *used*.
Settings: `plugin.run.remnant_auto`, `remnant_min_width` (mm), `remnant_min_area` (mm²).

The command `queue.add_current` adds the open (saved) document to the queue.
