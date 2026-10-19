# Nest

Placing parts on sheets to save material. *This guide is a starter; the graphical Nest
workspace of the Wave 3 interface extends it.*

## A nest in five steps

1. **Nest → New** starts a nest project.
2. **Import part** or **Add part** (the selection) adds parts; set the quantity, the priority and
   whether a part may be mirrored in the part table.
3. **Import sheet** or **Add sheet** adds the sheets; a selected drawing becomes a remnant sheet.
4. **Nest → Params** sets the spacing, the edge margin, the allowed rotations and part-in-part.
5. **Run** places the parts. The result table shows the part count and the material use per
   sheet. **To main view** brings a sheet into the drawing to cut it (undoable).

## Engines

The built-in nester works without extra software. With the optional `nest` extra installed
(`uv sync --extra nest`) a strip-packing engine (sparrow) is available for denser results.

## Files

Nest projects are saved as `.nestproj`, the same format as the original, byte for byte.

## Reference

[Nesting notes](../re/nest.md).
