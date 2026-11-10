# Safety

mlaser drives a multi-kilowatt fiber laser and heavy, fast axes. Read this page before you
connect it to a machine. The technical design behind it is the
[safety architecture](../SAFETY.md).

> **The motion-card protocol was reverse-engineered and has never been tested on real
> hardware.** A wrong guess can move the head at the wrong speed or fire the laser unexpectedly.
> You use mlaser at your own risk; it is not a certified machine controller.

## Before the first connection

1. Run everything on the simulator first (`mlaser --demo`).
2. **Physically disable the laser** (interlock or key switch off) and start with
   `mlaser --real --laser-off`. In this dry-run mode every step is sent with the laser off.
3. Keep the e-stop within reach. Use **Frame** and **Dry run** before any real cut.
4. Load your machine's real parameters ([Importing your Mlaser setup](importing-mlaser-setup.md)).
   They set the step scale (pulses per mm) and the gas and follower wiring. Check the scale and
   the speed with a short jog.
5. Fit the hardware watchdog relay described in the
   [safety architecture](../SAFETY.md#11-hardware-watchdog-pc-alive): when the PC freezes or the
   program dies, the relay opens the laser-enable chain.

## Arming

A connection always starts **disarmed**: the laser cannot fire. Arming is a deliberate step
(**Arm** on the state banner or the run bar, with a confirmation). It is refused in dry-run
mode, while a fault is latched, while an interlock holds and while a job runs. Frame, dry runs
and Backward/Forward always run disarmed.

Arming lasts for **one cut**. When a job that fires the laser ends, is stopped or fails, the
machine is disarmed again, so the next **Start** asks "Arm and start" again (Cancel is the
default). Two exceptions keep the arming:

* **Hold**: a job held at a breakpoint stays armed, so **Continue** / **Resume** goes on
  without a new question. (After a fault reset the machine is disarmed and Resume asks once.)
* **Job queue**: between the sheets of a queue the machine stays armed, so each **Sheet loaded:
  start** goes straight to the pre-flight check. A sheet that is stopped or fails disarms, and
  the queue disarms when its last sheet is done.

Frame and dry run leave the arming as it was. The laser-test procedure stays armed between its
pulses and is disarmed after the last one; a single laser test from the panel disarms after it.

The state banner at the top of the window always shows the state:

| Banner | Meaning |
|---|---|
| grey **DISARMED** | connected, laser locked out |
| red **ARMED** | the next job may fire the laser |
| amber **DRY-RUN** | started with `--laser-off` |
| blue **SIMULATOR** | connected to the simulator, not a machine |
| dark red **FAULT** | something went wrong; motion and the laser are refused until you reset |

## Pre-flight check

Before Start, Frame, Dry run, Resume and moves, mlaser checks the job: alarms, homing, arming,
undefined layers, travel limits and broken geometry block the start; open contours and unknown
limits are warnings you acknowledge.

## Faults

A fault (lost link, card alarm, interlock, streaming problem, program error) halts the machine
and **latches**: Start, Resume, jog and moves stay refused until you press **Reset**. Reset
clears the card alarms, checks the interlocks and leaves the machine disarmed. A breakpoint
taken at the fault is kept, so after arming you can **Resume**.

## Keys while a job runs

* **Space**: hold (not while typing in a text field). Space never continues: a second press
  keeps the machine held. Continue with **Start / Continue** on the Run workspace (the button,
  or F5 there).
* **Esc** or **F12**: stop. Stop never waits; the halt goes out first. Esc stops the job in any
  window; in a dialog it also cancels the dialog, as usual.
* **F5 / F6 / Shift+F6** (Start, Frame, Dry run) act only on the Run workspace and never while
  you type in a field. Pressed on another workspace they show the Run workspace and put the
  focus on the button, without starting anything. Space and Enter never press Start, Frame,
  Dry run or Resume: click the button or use its key.
* Jogging is hold-to-jog: releasing the key or button, or losing focus, stops the axis.

## What software cannot do

A PC can freeze, lose power or be killed. Only the machine's own e-stop chain and a hardware
watchdog relay make it safe. On a lost link the card still runs out what is buffered (at most
1.2 seconds of motion).
