# Nest workspace and remnant stock

Open it with `workspace.nest`. It edits the same nest project as the Nest dock.

* **Parts**: name, quantity, maximum (fill up to) and priority (higher is placed first).
  **+ From drawing** adds the drawing's *recognised parts* (an outline with its holes and
  features is one part, identical parts are counted; the selection, else everything) after a
  review where parts can be merged or split. **Import file...** takes several DXF / .chf / SVG
  files at once (each file's parts recognised) or a parts list (.csv, into the Orders tab);
  files can also be dropped on the workspace. See [Import](import.md#part-recognition).
* **Orders** (the tab next to Nest): parts lists with quantity, material, thickness, customer,
  order, due date and priority; **Load into Nest** fills the part list with one material ×
  thickness group ([Import](import.md#orders-several-files-and-parts-lists)).
* **Sheets / remnants**: standard sheets (width × height × count) and the remnants in stock that
  match the material and thickness above the list. The material and thickness follow the
  technology on the drawing's layers (set in the Process workspace, or by an order) until you
  type in them; the material field completes from the technology database, and **Technology**
  applies the matching technology to the layers the parts use. Tick a remnant to nest on it
  (remnants are filled first).
* **Parameters**: spacing and edge distance (mm), rotation, the engine (*Raster*, built in; or
  *Sparrow*, when the optional package `spyrrow` is installed), the time limit.
* **Nest ▶** runs in the background with progress; **Cancel** keeps what was placed.
* **Results**: one thumbnail per sheet with utilisation, part count and the leftover strip; click
  one to see it large. **Compare runs** lists every run of this session (★ = fewest unplaced
  parts, then fewest sheets, then the best utilisation); click one to go back to it.
* **Send to queue** writes every sheet as a `.chf` (`<data dir>/production/sheets/`) and adds it
  to the job queue (identical sheets become one entry with their count), with the material,
  thickness, the sheet outline, the layer parameters and the customer / order references of the
  parts on each sheet. **Copy sheet to drawing** replaces the drawing with the selected sheet
  (undoable) and remembers its material and thickness for the material check at Start.

## Remnants

After a nested sheet is cut, the unused area is stored as a remnant: a polygon with material,
thickness, its source job and an id (`R-0001`). The boundary is a *staircase*: each part is
extended to the sheet edge it was nested against, so the remnant can be separated with straight
cuts; slivers narrower than the minimum width and pieces smaller than the minimum area are
dropped. Remnants are offered in the Nest workspace, reserved while their sheet is queued and
marked used when it has been cut (`<data dir>/production/remnants.json`).
