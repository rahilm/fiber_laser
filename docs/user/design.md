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
undone (**Ctrl+Z**).

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
  offers to recover the last state. You can also recover any time with **System Recovery**.

A project file that is damaged, or that was made by a newer mlaser, is not opened. The message
says what is wrong, for example "document.json is damaged (checksum mismatch)". The open drawing
is not changed.

## Reference

How each tool works in detail: [Drawing and CAD editing](../re/draw.md),
[CAM import](../re/cam.md).
