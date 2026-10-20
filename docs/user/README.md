# User guide

This guide is for people who run a fiber-laser cutter with mlaser: preparing drawings, setting
the cutting process, nesting sheets and running jobs. It ships with the program (**Start → Help**) and works offline.

> **Read [Safety](safety.md) before you connect mlaser to a machine.** The motion-card protocol
> was reverse-engineered and has not yet been tested on real hardware.

| Guide | What it covers |
|---|---|
| [Getting started](getting-started.md) | install, first start, the demo and the simulator |
| [The window](shell.md) | workspaces and the rail, the command palette (Ctrl+K), user modes and PINs, themes, the classic ribbon |
| [Keyboard shortcuts](keyboard.md) | every key, generated from the command registry |
| [Safety](safety.md) | arming, pre-flight checks, faults, the laser-off mode, what software cannot do |
| [Importing your Mlaser setup](importing-mlaser-setup.md) | machine parameters, layer parameters, languages and drawings from an existing Mlaser install |
| [Design](design.md) | importing DXF/SVG/G-code, drawing and editing, text |
| [Process](process.md) | layers, leads, kerf, micro-joints, cooling points, ordering |
| [Nest](nest.md) | placing parts on sheets, remnants, the nest project |
| [Cutting strategy](cutting-strategy.md) | lead-in collision check and placement, tipping on the slats, skeleton cut-up, remnant separation cut |
| [Run](run.md) | connecting, framing, dry runs, cutting, pause and resume |
| [Report](report.md) | production reports, logs and statistics |
| [Machine](machine.md) | homing, jogging, outputs, follower, calibration and diagnostics |
| [FAQ](faq.md) | common questions and problems |

The task guides (Design to Machine) follow the workspaces of the Wave 3 interface. Where a
workspace is still being built, the guide describes the ribbon commands that do the same job.

Other documentation: the [developer docs](../dev/README.md) (architecture, plugins, testing,
release) and the [reverse-engineering notes](../re/README.md) (how the original program works).
