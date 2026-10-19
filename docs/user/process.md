# Process

Setting how every contour is cut. *This guide is a starter; the Process workspace of the Wave 3
interface extends it.*

## Layers

Every part belongs to a **layer** (a colour). A layer holds the cutting parameters: speed, laser
power, frequency and duty, gas and pressure, cutting height and the pierce stages. Edit them in
the layer panel (**Layer Params**). A layer without parameters blocks a real cut in the
[pre-flight check](safety.md#pre-flight-check).

## Contour features

| Feature | Where | What it does |
|---|---|---|
| Lead-in / lead-out | **Process → Lead** | line, arc or line+arc approach away from the part edge |
| Start point | **Process → Start point** | where the contour is pierced |
| Kerf compensation | **Process → Kerf** | offsets the path by half the kerf, inside or outside |
| Micro-joints | **Process → Micro-joint** | short uncut bridges that hold the part in the sheet |
| Cooling points | **Process → Cool point** | stops that let the material cool on sharp corners |
| Gap / seal / overcut | **Process** | leaves a gap, closes or runs past the start |
| Corners | **Process** | fillet, relief loop and corner handling |
| Fly cut | **Process** | cuts rows of holes or lines in one pass |

Manual tools (click on the drawing) exist for leads, start points, micro-joints, cooling points
and splitting. **One-key process** applies a complete setup in one go.

## Order

**Process → Sort** sets the cut order: by bands, nearest neighbour, rings, layer priority, the
shortest path, a grid, or a manual order (click the parts in the order you want). Inner contours
are always cut before the outer one. "Avoid cut holes" keeps rapids away from parts that are
already cut.

## Raster engraving

**File → Bitmap** turns an image into engraving lines (dither, threshold, grey scale).

## Reference

[CAM: toolpath features](../re/cam.md), [Manual craft tools](../re/craft.md),
[Raster engraving](../re/engrave.md).
