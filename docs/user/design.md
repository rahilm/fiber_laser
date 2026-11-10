# Design

Preparing the drawing: import, draw, edit, text. *This guide is a starter; the Design workspace
of the Wave 3 interface extends it.*

## Import

**File → Open** or **Import** reads:

| Format | Notes |
|---|---|
| `.dxf` | lines, arcs, circles, polylines with bulges, splines, ellipses, TEXT/MTEXT; the original's import rules by default, a modern import (`modern-dxf` profile) for newer DXF features |
| `.svg` | paths, shapes, `<use>`, CSS and transforms |
| G-code | the original's G-code dialect, also scan (engraving) G-code |
| `.chf` | Mlaser drawings, saved back byte for byte |
| bitmap | raster engraving (**File → Bitmap**), see [Process](process.md) |

The import can clean up the drawing: remove duplicates, join open ends, drop tiny objects and fit
arcs (**Process → Optimize**). After a drawing is opened, the **Import review** lists open
contours (with the gap), duplicates, tiny and zero-length geometry, self-intersections and text
that was not converted, and fixes them as one undo step: see [Import](import.md).

Files are read in the background: a large file shows a progress window with the number of
entities read so far and a **Cancel** button, and the rest of the window (the run controls
included) keeps working. A file that would expand far beyond any real drawing is refused with
a message instead of being imported: a DXF with more than 2,000,000 entities once its blocks
are expanded (or a MINSERT array of more than 100,000 copies), an SVG with more than 500,000
elements once its `<use>` copies are made, or a project (`.mlz`) over its size limits
([project format](../dev/project-format.md#limits)). Such files are either damaged or built to
exhaust the computer; explode or simplify the blocks in the CAD program and export again.

## Draw

The **Draw** tab has point, line, circle (centre or 3 points), arc, ellipse, rectangle, rounded
rectangle, polygon, star, polyline (with arcs), curve, text and QR code. Clicks snap to end,
middle and centre points and circle quadrants; hold **Shift** to place a point without snapping.
**Esc** or a right click ends the tool.

## Edit

Select with a click or a window drag; move, rotate, mirror, scale, align and set the size from
the **Common** tab. Trim and extend, explode, group and ungroup, arrays (rectangular, circular,
fill) and common-edge joining are on the **Common** and **Process** tabs. Every edit can be
undone (**Ctrl+Z**, ⌘Z on a Mac); **Edit → Undo** names the step it undoes ("Undo Move"). The
arrow keys nudge the selection; a run of nudges is one undo step. The history is limited by
memory (256 MB of copies), not by a number of steps. Opening another drawing (or **File → New**)
can itself be undone, but the history of the drawing that was closed is let go.

**Cut**, **Copy** and **Paste** (**Ctrl+X / C / V**, ⌘X / C / V) use the system clipboard: parts
copied in one mlaser window paste into another, or into another mlaser, with all their process
settings. The copy also carries SVG and DXF, so a drawing program or CAD program can paste it;
mlaser pastes DXF or SVG copied from other programs too (the same import limits as for files).
Pasted parts land 10 mm from the originals.

Copying, pasting or arraying a few hundred parts takes a moment: the status bar says so
("Copying 256 parts…") and the pointer shows the busy cursor until it is done. The DXF for other
programs is added to the clipboard a moment after the copy, in the background.

Drag and drop: drop DXF, SVG or G-code files on the window or the canvas to import them into the
drawing, a project (`.mlz`) or `.chf` to open it. Drag selected rows out of the **Parts** list
onto the canvas (a copy where you drop it), into another mlaser window, or into a folder (a
`.dxf` file).

## Parts list

The **Parts** dock lists every part of the drawing, one row each: number, name, layer,
technology, size, contours, cut order and warnings (an open contour, or a layer with no cut
settings: the warning sign beside the name, and the text in the *Warnings* column and its
tooltip). It is a tab next to *Layer parameters* and *Properties* in Design, and next to
*Properties* in Process; **Edit → Part list** brings it to the front. In a narrow window the
least important columns hide first (contours, cut order, size, warnings text); widen the dock to
see them again. Click a column header to sort by it.

**Find a part:** **Ctrl+F** (⌘F on a Mac, also **Edit → Find part…**) puts the cursor in the
filter field. Type words that must all match the part number, name, layer, technology or warning
text; `#12` is part 12 exactly; `>50` and `<50` compare the larger side of the part with 50 (in
the display units). **All layers** picks one layer, **Warnings only** shows the parts with a
warning. **Esc** clears the field, **Down** or **Enter** moves into the list. Filtering only
hides rows: it never changes the drawing, and parts selected but hidden by the filter stay
selected.

**Selection** works both ways: selecting rows selects the parts on the canvas, and selecting on
the canvas selects the rows and scrolls the first one into view. **Enter**, a double-click or
**Show on canvas** zooms the canvas to the selected parts. In the list the arrow keys move
through the rows (they do not nudge the parts there); **Esc** goes back to the filter field.

**Assign technology…** gives the selected parts another technology; see
[Process › Technology for some parts](process.md#technology-for-some-parts).

## Text

**Draw → Text** writes text as cuttable outlines. The font is bundled with mlaser (Noto Sans SC,
which covers Latin and Chinese), so the same text gives the same geometry on every computer.
DXF TEXT and MTEXT use the same bundled font unless the drawing names a font file that is
installed. To use an installed font, pick it explicitly in the Text dialog. The original's
SimSun lookup is available with the environment variable `MLASER_TEXT_FONT=system`.

## Saving your work

| File | Use it for |
|---|---|
| `.mlz` project | your own work: everything mlaser knows about the job is kept (layers, leads, micro-joints with their widths, cooling points, raster engravings, nest results, the job queue, technology assignments) |
| `.chf` drawing | exchanging drawings with the original Mlaser (or a machine that runs it) |

* **Save project…** (`file.save_project`, Ctrl+K) writes an `.mlz`. **Open project…** reads it.
* **Export .chf…** writes the interchange file. A `.chf` you opened and did not change is
  exported byte for byte as it was. Data that `.chf` cannot hold (cooling points, raster
  engravings, nest results) is kept only in the project.
* Saving is safe against crashes and power loss: the old file stays intact until the new one is
  complete.
* mlaser autosaves the project every two minutes when something changed, and when a job is
  prepared (`File/autosave.mlz` next to the original's `File/autosave.chf`). After a crash it
  offers to recover the last state (and the resume point of a held job, see the
  [FAQ](faq.md#mlaser-crashed-is-my-work-lost)). You can also recover any time with **System Recovery**.

A project file that is damaged, or that was made by a newer mlaser, is not opened. The message
says what is wrong, for example "document.json is damaged (checksum mismatch)". The open drawing
is not changed.

## Reference

How each tool works in detail: [Drawing and CAD editing](../re/draw.md),
[CAM import](../re/cam.md).
