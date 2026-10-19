# Nest workspace and remnant stock

Open it with `workspace.nest`. It edits the same nest project as the Nest dock.

* **Parts**: name, quantity, maximum (fill up to) and priority (higher is placed first). Add the
  selected parts of the drawing, or import a DXF / .chf / SVG file.
* **Sheets / remnants**: standard sheets (width × height × count) and the remnants in stock that
  match the material and thickness typed above the list. Tick a remnant to nest on it (remnants
  are filled first).
* **Parameters**: spacing and edge distance (mm), rotation, the engine (*Raster*, built in; or
  *Sparrow*, when the optional package `spyrrow` is installed), the time limit.
* **Nest ▶** runs in the background with progress; **Cancel** keeps what was placed.
* **Results**: one thumbnail per sheet with utilisation, part count and the leftover strip; click
  one to see it large. **Compare runs** lists every run of this session (★ = fewest unplaced
  parts, then fewest sheets, then the best utilisation); click one to go back to it.
* **Send to queue** writes every sheet as a `.chf` (`<data dir>/production/sheets/`) and adds it
  to the job queue (identical sheets become one entry with their count), with the material,
  thickness, the sheet outline and the layer parameters. **Copy sheet to drawing** replaces the
  drawing with the selected sheet (undoable).

## Remnants

After a nested sheet is cut, the unused area is stored as a remnant: a polygon with material,
thickness, its source job and an id (`R-0001`). The boundary is a *staircase*: each part is
extended to the sheet edge it was nested against, so the remnant can be separated with straight
cuts; slivers narrower than the minimum width and pieces smaller than the minimum area are
dropped. Remnants are offered in the Nest workspace, reserved while their sheet is queued and
marked used when it has been cut (`<data dir>/production/remnants.json`).
