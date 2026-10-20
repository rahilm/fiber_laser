# Nest workspace and remnant stock

Open it with `workspace.nest`. It edits the same nest project as the Nest dock.

* **Parts**: name, quantity, maximum (fill up to) and priority (higher is placed first), the
  **rotation** of each part and **mirror** (mirrored copies allowed). Rotation: *sheet setting*
  (the Rotation parameter below), *fixed 0°*, *0° / 180°*, *90° steps*, or *with the grain* (the
  part keeps its X axis along the sheet grain: 0° / 180° for grain along X, 90° / 270° for grain
  along Y). Add the selected parts of the drawing, or import a DXF / .chf / SVG file.
* **Sheets / remnants**: standard sheets (width × height × count) and the remnants in stock that
  match the material and thickness typed above the list. Tick a remnant to nest on it (remnants
  are filled first).
* **Parameters**: spacing and edge distance (mm), rotation, the engine (*Raster*, built in; or
  *Sparrow*, when the optional package `spyrrow` is installed), the time limit.
* **Options** opens two tabs (every option is off by default):
  * **Nesting**: *Common line* (neighbouring parts share one cut; the shortest shared edge,
    default 20 mm, and the kerf used as the part distance), *Part in part* (small parts in the
    holes of large ones), *Mirror (all parts)*, *Gravity* (the corner parts are pushed to: left,
    bottom, right, top or automatic) and *Sheet grain* (none, along X, along Y; for the parts set
    to *with the grain*). The Sparrow engine supports neither common line, part in part nor
    mirror (the run says so).
  * **Cutting strategy**: *Skeleton cut-up* (off, grid, in the gaps) with the largest scrap
    piece, *Remnant separation cut* with the narrowest remnant worth separating, *Fix lead
    collisions* and *Tip-avoiding order* (micro-joint, cut last, rapids avoid). These apply to the
    sheets sent to the queue or copied to the drawing, and need no new nesting run; the sheet view
    shows the cut-up lines (orange) and the separation cut (green). See
    [Cutting strategy](cutting-strategy.md).
* **Nest ▶** runs in the background with progress; **Cancel** keeps what was placed.
* **Results**: one thumbnail per sheet with utilisation, part count and the leftover strip; click
  one to see it large. **Compare runs** lists every run of this session with its engine, sheets,
  utilisation, **cut length** (m, the contours as cut: common line, cut-up and separation lines
  included, leads not) and **pierces** (one per contour), and the unplaced parts (★ = fewest
  unplaced parts, then fewest sheets, then the best utilisation); click one to go back to it.
* **Send to queue** writes every sheet as a `.chf` (`<data dir>/production/sheets/`) and adds it
  to the job queue (identical sheets become one entry with their count), with the material,
  thickness, the sheet outline and the layer parameters. With common line on, the shared edges
  are cut once. A sheet with a cutting strategy (cut-up or separation lines, tip-avoiding order)
  is written as a `.mlz` project instead, which keeps the cut order. **Copy sheet to drawing**
  replaces the drawing with the selected sheet as it is cut (undoable).

## Remnants

After a nested sheet is cut, the unused area is stored as a remnant: a polygon with material,
thickness, its source job and an id (`R-0001`). The boundary is a *staircase*: each part is
extended to the sheet edge it was nested against, so the remnant can be separated with straight
cuts; slivers narrower than the minimum width and pieces smaller than the minimum area are
dropped. With the *Remnant separation cut* the remnant is the piece that cut frees, stored
exactly as cut. Remnants are offered in the Nest workspace, reserved while their sheet is queued
and marked used when it has been cut (`<data dir>/production/remnants.json`).
