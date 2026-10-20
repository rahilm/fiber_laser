# Units: metric or imperial

mlaser shows and takes every length, speed, acceleration, area, pressure, gas flow and mass in
the units you choose: **metric** (mm, mm/s, bar) or **imperial** (in, in/min, psi). The choice
applies to the whole application at once: the canvas rulers, grid and coordinates, the
Properties panel, the layer and technology parameters, Process, Nest (sheet sizes, spacing,
remnants), Run (live position, speed, height, gas), Machine (jog step and speed, settings),
the wizards, Report (cut length, areas, rates), Orders, labels and plugin windows.

## Switching

**Settings → General → Units → Unit system**: *Metric* or *Imperial*. The change applies at
once to every open window; nothing needs a restart.

Stored values never change. Drawings, projects, settings and the technology database are always
kept in millimetres (and mm/s, bar ...), and the machine receives exactly the same job whichever
system you choose. Switching back and forth, or opening a dialog and pressing OK without changing
anything, never changes a part or a parameter by a rounding step.

## What imperial shows

| Quantity | Metric | Imperial | Imperial resolution |
|---|---|---|---|
| Lengths (positions, sizes, lead lengths, sheet sizes) | mm | in | 0.001 in |
| Small lengths (kerf, offsets, tolerances, gaps) | mm | in | 0.0001 in |
| Totals (cut length) | m | ft | 0.1 ft |
| Speed | mm/s | in/min (ipm) | 0.1 in/min |
| Acceleration | mm/s² | in/s² | 0.1 in/s² |
| Area (parts) / sheets | mm² / m² | in² / ft² | |
| Pressure | bar | psi | 0.1 psi |
| Gas flow | l/min | ft³/h (SCFH) | |
| Mass / density | kg / g/cm³ | lb / lb/in³ | |
| Rates (per metre, per m²) | per m, per m² | per ft, per ft² | |

Times (s, ms), angles (°), power (%, W), frequency (Hz) and percentages are the same in both
systems.

The conversion factors are exact: 1 in = 25.4 mm, 1 ft = 304.8 mm, 1 lb = 0.45359237 kg,
1 psi = 0.0689475729 bar.

### Per-quantity overrides

Under **Settings → Units** you can override single quantities and keep the rest of the system,
for example imperial with speeds in in/s, or metric with speeds in m/min:

| Setting | Choices (empty = follow the system) |
|---|---|
| Length unit (`units.length`) | mm, cm, m, in, ft |
| Speed unit (`units.speed`) | mm/s, mm/min, m/min, in/s, in/min (ipm) |
| Acceleration unit (`units.accel`) | mm/s², m/s², in/s², g |
| Area unit (`units.area`) | mm², cm², m², in², ft² |
| Mass unit (`units.mass`) | kg, lb |
| Pressure unit (`units.pressure`) | bar, MPa, kPa, psi |
| Gas flow unit (`units.flow`) | l/min, m³/h, ft³/h, ft³/min |
| Decimals (`units.decimals_length`, `_speed`, `_accel`, `_area`, `_pressure`) | 0 … 6 |

## Typing values

Every value field accepts a plain number in the units shown, or a number **with a unit**,
whatever the system:

| You type | Means |
|---|---|
| `0.125` (imperial) | 0.125 in |
| `3.2 mm` | 3.2 mm, kept exactly (shown as 0.126 in) |
| `1/8"` or `1/8 in` | 0.125 in |
| `1 1/2 in` or `1-1/2"` | 1.5 in |
| `2'6"`, `2' 6`, `2 ft 6 in` | 30 in |
| `300 mm/min`, `100 ipm` | in a speed field |
| `40 psi`, `2.5 bar` | in a pressure field |

A decimal comma is accepted (`3,2 mm`). A value with a unit of the wrong kind (`3 psi` in a
length field) is refused and the field keeps its value. The arrow keys and the mouse wheel step
by a sensible amount in the units shown (0.05 in for lengths, 0.001 in for kerf).

## What stays metric

A few texts are data, not measurements, and stay as they are:

* **Technology record names** such as "Mild steel 3 mm O2": the name the record is stored and
  matched under. The thickness *field* shows in your units.
* **Pulse equivalents** (pulses per mm) in the machine parameters and the commissioning wizard:
  the motion card's own calibration constants, defined per mm.
* **Machine parameters without a unit** in the parameter files (the original registry declares
  none): they are shown as stored. Parameters that declare mm, mm/s or mm/s² convert.
* **Files**: DXF/SVG import and export keep their own units; CSV and JSON exports state their
  units in the header (`mm`).

## Command line

`mlaser --units imperial <command>` prints human-readable output (sizes, cut length, extents,
estimates) in imperial; without the option the CLI follows `units.system` of the configuration.
JSON output (`--json`) is always in millimetres and says so with `"units": "mm"`.
