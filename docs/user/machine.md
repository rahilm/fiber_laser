# Machine

Manual machine control, calibration and diagnostics. *This guide is a starter; the Machine
workspace of the Wave 3 interface extends it.*

## Manual control

The run control panel on the right jogs the axes (hold to jog), moves to points, sets and
returns to the origin, homes the axes and switches outputs: gas, red light, dust extraction,
the follower. The XHC wireless pendant works when the `devices` extra is installed.

## Height follower, auto-focus, laser source

The follower (on-card or serial), auto-focus heads, EtherCAT heads and the laser source (Raycus,
IPG) are set up and monitored from the **Advanced** tab. The follower calibration curve and
edge seek are there too.

## Diagnostics

**System Analysis** has the controller, follower, auto-focus and extended-card monitors, the IO
test, the light test, burn-in, the ball-arm pattern, batch cutting and the maintenance plan.

## Machine parameters

The machine's parameters come from the original's `dat` folder
([Importing your Mlaser setup](importing-mlaser-setup.md)). Parameter backups can be made and
restored from the **Advanced** tab.

## Reference

[Peripheral devices](../re/devices.md), [card protocol](../re/protocol.md),
[diagnostics](../re/diag.md).
