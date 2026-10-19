# Reverse-engineering notes

How the original Gweike Mlaser v0.0.1.51_SP1 works, as far as it was observed in its binaries
and files, and how the rebuild maps it. Tags used throughout: **O** observed (address or file
offset cited), **I** inferred, **U** unknown, **R** rebuild choice.

| Note | Subject |
|---|---|
| [protocol.md](protocol.md) | MCC100 motion-card protocol: frames, commands, FIFO streaming, homing, alarms |
| [formats.md](formats.md) | `.chf`, parameter XML, ini and auxiliary files |
| [cam.md](cam.md) | import, toolpath features, ordering, the MotionItem stream |
| [motion.md](motion.md) | velocity planning, interpolation, smoothing |
| [nc.md](nc.md) | NC compiler and controller integration |
| [devices.md](devices.md) | follower, laser source, auto-focus, EtherCAT, pendant |
| [diag.md](diag.md) | monitors, diagnostics, reports, auxiliary functions |
| [ui.md](ui.md) | the original's UI layout and feature map |
| [draw.md](draw.md) | drawing and CAD editing |
| [craft.md](craft.md) | manual craft tools, scan engraving |
| [engrave.md](engrave.md) | raster engraving (Bitmap.exe) |
| [nest.md](nest.md) | nesting |
| [parity.md](parity.md) | the Wine parity harness and its reports |
| [history/BRIEF.md](history/BRIEF.md) | the original project brief (archived) |
