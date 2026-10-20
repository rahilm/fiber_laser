# Practice mode: learn the machine on the simulator

Practice mode is a set of short guided lessons for new operators. They run on the **simulator**
only: nothing moves and no laser fires. You use the normal Run screen buttons (Frame, Arm laser,
START, HOLD, Reset alarm, Resume); a small coach window tells you what to do next and checks that
you did it.

Open it with the command **Practice (simulator)...** (`practice.open`, available in Operator
mode; search for "practice" in the command palette). The coach window stays open next to the
main window, so you can work on the Run screen while it shows the steps.

## Before you start

The lessons refuse to start on a real machine. If the program is connected to a real card, the
coach window says so and offers **Connect the simulator** (the same as Machine > Connect
simulator; the real machine is disconnected). Jobs you run during a lesson are recorded as
**practice** runs: the Report leaves them out of the production figures (select *Runs: All* to
see them, [report.md](report.md)), and they add nothing to the processing hours, the maintenance
counters or the planned quantity.

## The lessons

| Lesson | What you do | What the coach checks |
|---|---|---|
| Load a sheet | press **Load the practice sheet** and look at the parts in the live view | the practice sheet (three parts, 300 × 200 mm) is open with cutting parameters |
| Frame the job | press **Frame** | a frame ran to the end, laser off |
| Arm and start a cut | **Arm laser** (confirm), then **START**; let the job finish | the machine was armed, a cut started and finished |
| Hold and continue | start a cut, press **HOLD** (or Space), then **START / CONTINUE** | a breakpoint was taken and the job resumed and finished |
| Handle an alarm | start a cut; the lesson presses the simulator's emergency stop; read the alarm and its remedy; press **Reset alarm** | the FAULT was latched and you reset it |
| Resume from the breakpoint | as above, then arm again and press **Resume: Here** or **Contour start** | the stopped job resumed from its breakpoint and finished |

The later lessons open the practice sheet themselves when it is not open. During the alarm
lessons the lesson also releases the simulated emergency stop after the fault, as a colleague
would release it on a real machine once the cause is fixed.

Press **Abort the lesson** at any time: the simulator is stopped at once (the same halt as Stop)
and a pressed simulated emergency stop is released. A lesson that waits more than 15 minutes for
a step fails; start it again.

## Progress

The coach window lists every lesson with a tick when you passed it, the number of attempts and
your best time. The progress is kept in the settings (`plugin.ext_practice.progress`) and
survives a restart.

## Safety

* A lesson starts only when the controller is the program's own simulator; it checks this before
  it does anything. An external simulator reached over the network looks like a real card and is
  refused.
* The lesson never presses a machine button for you: arming, Start, Hold, Reset and Resume are
  your actions, with the usual confirmation questions and pre-flight checks
  ([run.md](run.md), [../SAFETY.md](../SAFETY.md)).
* The only thing a lesson does to the simulator itself is the simulated emergency stop of the
  alarm lessons, and the halt when you abort.
