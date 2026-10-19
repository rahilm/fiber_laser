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
arcs (**Process → Optimize**).

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

## Reference

How each tool works in detail: [Drawing and CAD editing](../re/draw.md),
[CAM import](../re/cam.md).
