# Run

Connecting to the machine and cutting. *This guide is a starter; the Run screen of the Wave 3
interface extends it.* Read [Safety](safety.md) first.

## Connect

* Simulator: `mlaser --sim` or **Advanced → Hardware → Connect to simulator**.
* Machine: `mlaser --real --ini ipAdd.ini`, with `--laser-off` for the first sessions.

A connection starts disarmed. Home the machine when asked (or **Start → Home**).

## Before cutting

1. **Sim** previews the toolpath with the real motion planner and gives the time estimate.
2. **Frame** traces the outline of the job with the laser off.
3. **Dry Cut** runs the whole job with the laser off.
4. **Arm** (state banner or run bar), then **Start**.

## During the job

The run bar at the bottom shows the progress and has Hold, Stop and the arming state.
**Space** holds and continues, **Esc** or **F12** stops. Pause keeps a breakpoint: **Resume**
returns to it with the laser off, pierces again and continues. **Backward / Forward** move along
the path with the laser off.

## After a fault

A fault stops the machine and latches. Read the alarm banner (it names the cause and the
remedy), fix the cause, press **Reset**, arm again and **Resume**.

## Reference

[NC compiler](../re/nc.md), [card protocol](../re/protocol.md),
[safety architecture](../SAFETY.md).
