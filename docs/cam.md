# CAM: import, toolpath features, and the MotionItem stream

**Sources**
- `Module/CADModule.dll`: dump `decomp/CADModule.c`. Addresses below are CADModule addresses unless prefixed.
- `NCModule.dll`, `MainApp.exe`, `DxfParseDllvc100.dll`, `Dxf2Grp.dll`, `SVG/SVG2GCode.exe`.
- `Lang/lang.txt` keys, written `pdNNN`.
- Scratch extracts: `/home/dev/scratch/mlaser/work/cam/` (vtable dumper `vft.py`; NC/MainApp extracts in `nc/`) and `/home/dev/scratch/mlaser/work/io/`.

**Tags:** **O** observed, **I** inferred, **U** unknown.

**Code**
- `mlaser/io/`: `dxf_import.py`, `svg_import.py`, `gcode.py`, `geom.py`.
- `mlaser/cam/`: `items.py`, `geometry.py`, `leads.py`, `kerf.py`, `microjoint.py`, `ordering.py`, `toolpath.py`.
- Phase 2 (agent C): `flycut.py` (§11), `commonedge.py` (§12), `corners.py` (§13), `array.py` (§14), `cleanup.py` (§15); UI in `mlaser/ui/ext_cam.py`; contour smoother in `mlaser/motion/smooth.py` (motion.md §9).

**Intentionally omitted:** licensing, dongle and HID checks, activation, and the cloud phone-home (none of it is in the CAM path).

## 0. Data model of the original (O)

**Segment types** (IEditableGlyph +8):

| Code | Segment |
|---|---|
| 1 | point |
| 2 | line |
| 3 | arc |
| 4 | circle |
| 5 | ellipse arc (I) |
| 6 | lwpoly |
| 7 | spline |

**Graph types:** 8 contour (CGlyContour), 9 group, 10 text, 11 group-like.

**Arc struct** (0x48 bytes, 0x1002fec0 / 0x10009b30): `cx, cy, r, a0, a1, start(x,y), end(x,y)`. CCW when a1 > a0.

**Contour fields**

| Offset | Meaning |
|---|---|
| 0x0c | layer |
| 0x28 | bbox |
| 0x48 | length |
| 0x68 / 0x78 | start / end point |
| 0x88 | nesting depth (outermost = 1, set by 0x10022270) |
| 0xb8 | segments, as {seg*, dir ±1} |
| 0xe8 | closed |
| 0xec | CCW flag |
| 0xf0 | cached primitive list |
| 0x100 / 0x108 | kerf mode / kerf width |
| 0x110 | kerf child contour |
| 0x114 / 0x118 / 0x128 / 0x13c | micro-joint mode / centres / lengths / intervals |
| 0x15c | cooling points |
| 0x180 | start parameter |
| 0x188 | start point |
| 0x198 | overcut (+) or gap (−) |
| 0x1b0 | CGuideCurve (parameters at 0x1b8) |
| 0x1f8 | lead side flag |

**Orientation test** (0x1006aff0): cast a horizontal ray at mid-bbox height and take the crossing with the largest x. If that edge goes up (dy > 0), the contour is CCW.

**Reverse** (0x10073980): reverses the segments and sets start = 1 − start. It swaps the start and end points and toggles the CCW flag and the lead side flag.

**Direction normalisation:** none. Contours keep the direction they were drawn in. Inner and outer only matter through the depth-parity rules below.

## 1. Import

### DXF (DxfParseDllvc100.dll v1.4.93, only export `newDxfFileParse` 0x10004390, then CADModule) (O)

Two stages: the DLL parses, then CADModule turns the parser entities into glyphs, chains them into
contours and places them (the app-level parity run checks the whole chain: `parity/out/app_files`).

**Stage 1: DxfParse.** A hand-written parser (not ODA). The entity factory is FUN_1000a4b0. It reads
the ENTITIES section in file order and expands INSERTs.

| Entity | Notes |
|---|---|
| LINE (4) | |
| CIRCLE (5) | |
| ARC (6) | degrees converted to radians; 2π added if end < start |
| ELLIPSE (7) | |
| SPLINE / HELIX (8) | knots, control points, weights. **A weight (41) is read only directly after the 10/20/30 of its control point** (FUN_1000e5xx peeks the next tag); weights written before the control points (ezdxf's order) are dropped. Fit points are read and discarded. |
| LWPOLYLINE / POLYLINE (9) | bulges; widths ignored; only the "closed" bit of flag 70 is kept for POLYLINE |
| TEXT (10) / MTEXT (11) | kept as text objects |
| POINT (1), XLINE (2), RAY (3) | |
| 3DFACE (12), SOLID (13), TRACE (14) | parsed, but CADModule has no handler for them (ignored) |
| INSERT | expanded recursively: base point, scale, rotate, translate, OCS. MINSERT rows/columns are ignored (one copy; inserts2.dxf) |
| REGION | counted only; MainApp warns "explode REGION first" |

Anything else (HATCH, DIMENSION, …) is skipped.

- Entities with group 60 (invisible) set are skipped. **Paper-space entities (group 67 = 1) are
  kept**: the active layout's entities that sit in the ENTITIES section are imported like
  model-space ones (parity D-09: visibility.dxf gives 2 lines).
- INSERT scaling (entity vtable slot 3): points are scaled per axis. ARC/CIRCLE (FUN_10008c60 /
  FUN_10008ac0) scale the radius by |sx| **only when ||sx| − |sy|| < 0.001**, otherwise the radius is
  kept, so arcs stay circular under non-uniform scale (D-07). Exactly one negative scale (mirror)
  maps the arc angles to [π−a1, π−a0] (x) or [−a1, −a0] (y), negates LWPOLYLINE bulges
  (FUN_100090f0) and maps ELLIPSE parameters to [−t1, −t0] (FUN_10008e60). Rotation adds to the angles.
- Units: the DLL ignores $INSUNITS. CADModule reads `$MEASUREMENT` (FUN_10004620) and can scale
  every entity by 25.4 (this+0x68 == 1); the samples (`$MEASUREMENT` 1) are not scaled (U: when it
  applies; not implemented).
- OCS / extrusion: arbitrary axis algorithm (0x1000ad90). Mirrored extrusions flip arc angles and
  negate bulges.

**Stage 2: CADModule.** Handler table FUN_10008f80 (one function per parser type):

| Parser type | Handler | Glyph(s) |
|---|---|---|
| 4 line | FUN_10003100 | segment (2) |
| 6 arc | FUN_100032e0 | arc (3), a0 normalised into (−π, π]; an arc under 30° whose sagitta r(1−cos(sweep/2)) is below 0.01 mm becomes a segment |
| 5 circle | FUN_100031f0 | circle (4): starts at angle 0 and runs CCW, also when the extrusion is mirrored |
| 9 lwpoly | FUN_100061f0 | **one** lwpoly glyph (6) with bulges and the closed flag |
| 8 spline | FUN_10008740 | 2 control points: segment. Degree 2 with an odd number n of control points and n+3 knots (FUN_100992d0): every quadratic Bezier (P0,P1,P2) becomes a cubic (P0, P0+k(P1−P0), P2+k(P1−P2), P2) with **k = 1.882·w/(2w+1)** (DAT_10120f48; w = weight of P1, or 1), one spline glyph per piece, knots 0,0,0,0,1,1,1,1. This is not exact degree elevation (that would be k = 2/3): with w = 1 the cubic is 0.42 mm from the quadratic (D-08b). Only the counts are tested, so a uniform-knot quadratic is also cut into (P0,P1,P2) triples (mix.dxf: 2.0 mm off its true shape). Otherwise the spline is copied as one glyph (FUN_10008680; the weights are dropped, FUN_10004be0 copies only knots and control points) with its knots normalised (FUN_10095640: shifted to start at 0, scaled to end at 2 if they end below 0.5); a triple interior knot does not split it (mix.dxf). A spline whose control points are collinear within 1e-5 becomes a segment (FUN_10092fb0). |
| 7 ellipse | FUN_10003500 → FUN_10097600 | t0 normalised into [−π, π]; cubic Bezier pieces split at multiples of π/2 (found with truncation toward zero, so a negative range such as the mirrored [−2.0, −0.2] of mix.dxf is one piece), control points P(u) + 4/3·tan(Δ/4)·P′(u); one spline glyph per piece (u1 = 4) with knots 0,0,0,0,K,K,K,K, K = 1/pieces scaled to 2 when below 0.5 |
| 1 point | FUN_100062f0 | point (1): a point graph |
| 10/11 text | FUN_10007bf0 | TrueType outline of each text line (font DAT_1011d850 = 宋体, FUN_100aca20 / GetGlyphOutline GGO_BEZIER: segment and cubic spline glyphs, see below), scaled so that the outline height equals the text height, rotated, the font origin on the insertion point; a group graph (type 9) on **layer 10**; lines are 1.2·height apart |
| 2/3, 12–14 | – | ignored |

Chaining (FUN_100b57c0 → FUN_10061c90, sequential mode): a glyph is appended to the current
contour when one of its ends is within 0.1 mm (per axis) of the contour's end (forward or
reversed) or start (prepended), else it starts a new contour (FUN_1006da30). Then, from the last
contour backwards, an open contour is merged into the nearest earlier open contour it touches
(FUN_10073e50). Contours get closing tolerance 0.1 (+0xd8), layer 0, and the nesting depth in
+0x88 (FUN_10022270). The graphs are finally moved so that their bbox lower-left is 20 mm
(DAT_1011d848) right of the existing drawing, on its bottom edge: **(20, 0)** for an empty
drawing (FUN_100ecef0). Text groups are appended after the contours. (The ODA-based
`Dxf2Grp.dll`, `CDrawing::CaluChain` / `SetCalcuTol`, is used only on the AutoNest import path.)

**Import sort (O, FUN_100b57c0).** Right after chaining, when the import settings' sort type
(CAD+0x8b8+0x20 = IGP.AutoSortType pd372, MainApp default "5" at 0x0090d06c, copied by MainApp
FUN_004d35c0) is not 0, the graphs go into a containment tree (FUN_10025080 / FUN_10024480:
children keep their insertion order, a new container is appended after the remaining
siblings) and through the sort dispatcher 0x1003f010 with **mode 5 and small_first = 1**
(+0x24 is always set to 1 by FUN_004d35c0): per tree level 0x1003e690 → 0x1003d300 (§8.4,
including the reversal of open contours picked by their end, written back through vtable +0xb0
= 0x10073980, which also toggles the lead side flag +0x1f8), then 0x1003c480 (containers last)
and the post-order flattening 0x1003c7b0. The repeated grid merge FUN_100606d0 (cells only above
100 open contours) and the open-contour grouping FUN_1005dd70 do not change any sample. With
fewer than 4 graphs on a level, or when the new route is not 5 % shorter than the document
order (start-to-start lengths), the order stays document order with holes first, which is why 8
of the 12 samples looked unsorted. The closed contours are shuffled with `srand(time(NULL))`:
on inserts.dxf the original's own result depends on the second it ran in (the observed order
comes out for about one seed in three; parity harness `sort_seed`).

TEXT outlines (FUN_100aca20 → FUN_100ac060 → FUN_100ab710, O): CTextGlyphAdapter
(FUN_100f51c0) calls `GetGlyphOutlineW(..., 3 = GGO_BEZIER, ...)`, advance gmCellIncX. Each
TT_POLYGON is one closed contour (closing tolerance 0.01, depth field 1): every record's point
list starts with the current point and a closing line record to pfxStart is added; line records
become one segment glyph per piece (pieces under 1e-4 in both axes dropped), a TT_PRIM_CSPLINE
record (a TrueType run of off-curve points, each implied quadratic as an exact cubic) with
n = 3P+1 points becomes ONE cubic spline glyph, u0 = 0, u1 = 3, knots 0, (i/P ×3, i = 0..P), 1.
The letter contours are then sorted with the same dispatcher (mode 5, small first), so holes
come before their letter. In the parity file some spline records come out cut into Bezier
sub-pieces (the O of "HELLO": 12 and 6 glyphs instead of 4 records each, knots of the pieces
with step 0.25, uninitialised u0/u1); the code doing that cut was not found (U).

**Rebuild:** `io.dxf_import.import_dxf(path, join_tol=0.1, flatten_tol=0.01, group='contour'|'nest',
placement=(20, 0), text_outlines=True)` follows both stages with ezdxf for the parsing:
- INSERTs are expanded with the DxfParse rules above (`_insert_levels`, `_xform`), not with
  ezdxf's exact transform; MINSERT grids are ignored as there.
- SPLINE weights are read from the raw tags in DxfParse's way (`_acad_weights`).
- Every contour keeps its glyphs (`model.Glyph`, `Contour.set_source_glyphs`), so
  `formats.chf.save_chf` writes the original's glyph kinds; `Contour.segments` is the flattened
  geometry: lines, arcs, lwpoly pieces, splines flattened exactly like the original (`io.geom.flatten_bspline`,
  formats.md §2.8).
- Order: the import sort above, ported statement by statement (`cam.ordering.import_sort` /
  `local_optimal_elems`, `sort_mode=5`; `sort_mode=0` keeps document order with holes first).
  Its shuffle uses msvcrt's `rand()` seeded with `sort_seed` (default 0) instead of the time.
  The sort bbox of a graph is computed from its glyphs (arc/circle extremes from the stored
  centre and radius), so equal extremes stay bit-equal for the strict container test.
- ELLIPSEs inside INSERTs are transformed as parser entities (centre and major axis through the
  INSERT, a mirror maps the parameters to [−t1, −t0], t0 into (−π, π], FUN_10008e60) before the
  Bezier cut, so a mirrored ellipse keeps running CCW (inserts2.dxf).
- TEXT/MTEXT outlines: the glyph outlines of the font's glyf table (fontTools; font file found in
  the usual Windows/macOS/Linux font folders), turned into GGO_BEZIER records and glyphs exactly
  as above (`_tt_records`, `_record_glyphs`), so letters keep line and spline glyphs; unhinted,
  and the pen advances by the unhinted advance width (GDI: hinted gmCellIncX), so coordinates
  differ slightly (parity D-16). The family is 宋体 if installed, else WenQuanYi Micro Hei (what
  the parity Wine prefix substitutes). Without a TrueType file the Qt outline flattened to lines
  is used (`mlaser.cad.text`). The texts are also listed in `doc.extra['texts']`, POINTs in
  `doc.extra['points']`.
- `placement=None` keeps the DXF coordinates; `group='nest'` groups holes with their outer contour
  (rebuild extension).
- Byte comparison with MainApp's own Save As (`tests/test_io_dxf_sort.py`, parity area
  "rebuild DXF->.chf bytes"): identical on all 11 non-TEXT samples except the uninitialised ints
  (contour +0x354, spline u0, and u1 of Bezier pieces: FUN_10097600 copies an uninitialised local
  for ellipse pieces, usually 4; FUN_100992d0 never sets the piece's +0x50, observed equal to the
  control-point count, which the rebuild writes).

### SVG (O)

- `SVG2GCode.exe` is a renamed build of open-source **juicy-gcode 1.0.0.0**: Haskell, GHC 8.10.7, svg-tree 0.6.2.4.
- MainApp (around 0x4d45a0) runs `SVG2GCode.exe "<svg>" -o "<app>\SVG\Output.nc"` hidden, waits for it, requires exit code 0, then imports `Output.nc` as NC.
- It passes no flavor, dpi or tolerance options, so juicy-gcode's defaults apply: 96 dpi and biarc fitting.

**Rebuild:** `io.svg_import.import_svg` parses SVG directly.
- Elements: path (all commands), rect, circle, ellipse, line, polyline, polygon, `<g>`.
- Transforms are applied.
- Units are 96 dpi px, converted to mm; the viewBox and mm/cm/in sizes are honoured.
- Y is flipped.
- Curves are flattened; circles stay arcs.

### G-code dialect of Output.nc (O); `io.gcode`

```
G17 / G90 / G0 Z1 / G0 X0 Y0          header
G00 Z1 ; G00 X.. Y.. ; G01 Z0 F10.00  per contour: beam off, rapid, beam on
G01 X.. Y..  |  G02/G03 X.. Y.. I.. J..   I/J relative to the arc start; no F
G0 Z1                                  footer (no M2/M30/M3/M5)
```

- `%.4f` everywhere. CRLF line endings. Z0 means laser on, Z1 means laser off.
- `document_to_gcode` and `contours_to_gcode` reproduce the format exactly. Full circles are split into two halves (I, for readers that cannot take start == end).
- `toolpath_to_gcode` exports a MotionItem stream and adds F in mm/min.
- `parse_gcode` / `read_gcode` import G0–G3, G90/G91, I/J or R. Z ≤ 0 or M3/M7 means laser on. Each laser-on run becomes one contour. The original `Output.nc` parses to 7 closed contours (tested).

## 2. Lead-in / lead-out (CGuideCurve) (O); `cam.leads`

**Parameter block:** 72 bytes at contour+0x1b8.
- In: type, angle (deg), length, radius.
- Out: the same four fields at +0x1d8.
- Defaults (0x10068890): type 0, 90°, 2 mm, 1 mm.

**Types** (pd297–pd300): 0 none, 1 line, 2 arc, 3 line+arc.

**Setter** (0x10064e10):
- P = start point; d_in = tangent leaving P; d_out = tangent arriving at the end.
- The lead is disabled if the lead length or the contour length is < 0.01 (0x10064860).

**Geometry** (0x100300c0). With a = angle and flag = side:

```
v_in  = Rot(flag ? -a : +a) d_in ;  v_out = Rot(flag ? +a : -a) d_out
type 1 in : line P - v_in·len -> P
type 2 in : arc S=P-(d_in+v_in)·len -> P, radius len/tan(a/2)   (90°: quarter circle radius len)
type 3 in : t=R·tan(a/2); a<5° or t<0.1 -> type 1; t>len-0.01 -> type 2;
            line S-v_in·(len-t) -> S, arc S -> P radius R, S=P-(d_in+v_in)·t
lead-out: mirror from the contour end with v_out
```

- Arcs are CW when flag = 0. They are built by 0x10009b30 (centre on the minor-arc side).
- **Side rule:** `flag = (depth even) XOR (contour CW) XOR invert`, where invert is pd322 "outermost layer is inside cut".
- flag = 0 puts the lead on the right of travel. As a result, outer contours (depth 1, CCW) and holes (depth 2, CW) both get their leads on the scrap side whichever way they were drawn (tested).
- The lead is computed on the kerf child contour.
- A lead interference check exists (0x10020e50, I).

**Lead directions** (O, FUN_10064860 → FUN_10063be0(contour, t, flag); `leads.start_tangents`):
both tangents are taken at the start fraction t of the *unrotated* contour (target =
+0x48 · t, the glyphs walked with their own lengths). FUN_10030b50 gets the flag-1 tangent
for the lead-in and the flag-0 tangent for the lead-out.
- Start within 0.05 mm (DAT_1011d6d0) of the end of glyph i ("at a vertex"): flag 1 → the
  travel start of glyph i+1 (past the last glyph: the end of the last glyph, open contours
  glyph 0); flag 0 → the travel start of glyph i itself.
- Otherwise the glyph containing the target: an lwpoly asks FUN_1008bb90(t, ±0.05·dir) for
  the exact tangent of the piece 0.05 mm after (flag 1) / before (flag 0) the point (wrap
  below 0, extension of the last piece past the end); a spline glyph the true curve's tangent
  (FUN_10093dc0); any other glyph its exact tangent.
- Consequence: at the usual stored start t = 0 both leads use the *departing* direction of
  glyph 0, so on a rectangle the lead-out leaves along the first edge (parity JC-05, fixed);
  on bulge arcs the two leads are turned ±0.05/r against each other (JC-06, fixed).
- The start point is t · stored length (`geometry.stored_length`: a .chf contour keeps its
  stored +0x48, the loader does not recompute it); inside a spline glyph it lies on the true
  curve (FUN_10093390 + FUN_10093b40), not on the chord of the flattened polyline (JC-07).

## 3. Start point (O, 0x10071f80); `cam.ordering.choose_start`

Closed contours only (open contours start at 0).

| Mode | Name | Rule |
|---|---|---|
| 0 | long side first | Middle of the first line longer than 2·lead length; otherwise the longest segment. A circle starts at its start point. Requires lead length ≥ 0.1. |
| 1 | vertex first | First vertex with \|cross(t_in, t_out)\| > 0.01745. |
| 2 | percentage | User percentage of the length. |
| 3 | keep | Start unchanged. |

Mode 0's "longest" is the **first** maximum (strict `<`). The rebuild treats lengths within
1e-9 mm as equal (`ordering.LENGTH_TIE_EPS`, I): geometrically equal pieces (the 24 teeth of
gear.dxf) differ in the last bits in both programs, and the original keeps the first one.
Remaining (U, ACCEPTED): spline_ellipse.dxf's four quarter splines have lengths that differ by
one ulp in the original's x87 arithmetic (…396, …400, …400, …396), so it picks glyph 1; the
rebuild's are bit-identical and it picks glyph 0 (parity JC-11).
The stored start is a fraction of the stored length (+0x48): `choose_start` returns
fraction · computed length, so `start_param = d / length` reproduces the original's value.

**Rebuild:** `Craft.extra['start_param']` (0..1) forces mode 2. Mode 0 works on the original's
glyph list (`cam.ordering.contour_glyphs`): a contour's own source glyphs
(`Contour.source_glyphs()`, set by the rebuild's DXF reader and by `load_chf`) split with
`io.geom.glyph_segments`, counted like the importer builds the contour (zero-length pieces
dropped, end-point snapping keeps the count); then a .chf record; else one glyph per segment.
So a closed LWPOLYLINE imported by the rebuild is one glyph and starts in the middle of its
longest piece, as in the original (basic_r2000: 0.374474, parity "import_dxf(basic_r2000.dxf) ->
lead"; before this it started on the first 30 mm line, 0.118537).

## 4. Overcut / gap (O, 0x10070eb0, 0x1006f2d0)

- The signed value is stored at contour+0x198 (pd323/pd324 "Gap Seal").
- Open contours keep only gaps.
- The value is clamped to ±L/2.
- |v| ≤ 0.05 is ignored.
- An overcut is forced to 0 when a lead-out exists.
- The path ends at start + v: an overcut wraps past the start, a gap stops short of it.

- O (FUN_1006f2d0, local_12d): the value used for the primitive list is 0 whenever a
  lead-out type is set (+0x1d8 ≠ 0), so **an overcut and a gap are both dropped** with a
  lead-out: the whole contour is cut and the lead-out leaves from the start point.

**Rebuild:** `Craft.extra['overcut']`; `toolpath._overcut` returns 0 when the lead-out type is
set (parity JC-02, fixed; before, the gap was kept and the lead-out moved to the gap end).

## 5. Kerf compensation (O, 0x100738b0 → 0x10072540 → 0x1003ad00); `cam.kerf`

**Gates:** closed contour, max(bbox w, h) ≥ 3·width, and 0.01 ≤ |d| ≤ 200.

**Sign by mode** (depth 1 = outermost):

| Mode | Behaviour | Sign |
|---|---|---|
| 0 | outermost shrinks, others alternate | −(odd ? 1 : −1) |
| 1 | outermost expands, others alternate | odd ? 1 : −1 |
| 2 | all shrink | −1 |
| 3 | all expand | +1 |

- CW contours invert the sign.
- d = s·width, and d > 0 offsets to the right of travel.
- The model's `Craft.kerf_comp` follows the same numbering: −1 none, 0 inner, 1 outer.

**Mode 1 in practice:** outer contour paths grow and hole paths shrink, so the beam runs on the scrap side for every contour. Tested: a circle hole of r = 5 with width 0.1 gives r = 4.9 whichever way it is drawn.

**Primitives:**
- Lines are shifted.
- Arcs: CCW gives r + d; CW gives r − d; the arc is dropped if r < 0.01.

**Joins** (by turn at the vertex):
- Tangent (dot > cos 3°): no join.
- Concave: trim at the intersection (line/line, line/arc, arc/arc).
- Convex, turn < 95°: miter.
- Sharper convex turn: round join of radius |d| around the vertex. The original (0x10038130) uses a cubic Bézier with handles |AB| when the two sides differ by ≥ 0.01, else a tangent arc; recovered in §13.5 (`corners.kerf_corner_join`), not yet used by `kerf.py`.

The result is stored as child contour 0x110 and inherits the start, overcut, joints and leads.
The start is inherited as a **fraction**: FUN_100627e0 → FUN_10071f30(child, parent +0x180), so
the compensated path starts at t · child length on the child built from the *unrotated*
parent, e.g. at the miter corner (−0.2, −0.2) of a 60×40 rectangle grown by 0.2 (parity JC-03,
fixed; the rebuild used to offset the rotated contour and restart it nearest to the old start).

Tangent joins (dot > cos 3°) are left alone: each piece keeps its own offset end points, an
arc keeps its glyph angles with r ± d. On .chf input whose arc ends come from 6-decimal angles
the pieces stay a few µm apart, exactly as in the original (polyline kerf 0.3: 39.7 and
30.473442 = 9.7 · 3.141592; parity JC-04, fixed; the rebuild used to average the two ends).

**Rebuild:** the analytic offset keeps arcs. If it fails or the result self-intersects, the code falls back to a shapely buffer (round joins, arcs lost).

## 6. Micro-joints (O); `cam.microjoint`

**Storage:** mode at 0x114, centres (normalised) at 0x118, lengths in mm at 0x128. In the .chf file the data sits in the block named "<PWM Control>".

**Intervals** (0x1006c930): c ± L/(2·length), clamped and merged.

**Auto placement** (0x10071180):
- Skip the contour if max(bbox) < max(2, 5L, minSize).
- n = count, or floor(R/spacing) − 1.
- If n·L > R/2, then L = R/(2n).
- Closed contours: c_k = k/(n+1). Open contours with "joint at head": c_k = k/n.
- An X/Y-only scan-line mode exists; it accepts crossings within 30° of the axis (axis flag mapping U).

**Manual** (0x10071940): clicking inside a joint removes it; otherwise one is added if it does not overlap.

**Output** (0x100673f0):
- Intervals are rebased by the start parameter and mapped onto lead-in + contour + overcut.
- Inside a joint the head keeps moving at cut speed with per-sample duty = 0.
- NC options for micro-joint deceleration, re-pierce and low power exist (pd594/pd595, 0x10103780, I).

**Rebuild:**
- `Craft.micro_joints` holds positions in mm from the contour's first segment. They are rebased onto the rotated/kerf contour and scaled by the length ratio.
- Width per joint: a (pos, len) entry's own, else the stored per-joint widths of a .chf load
  (`Craft.extra['micro_joint_widths']`, the +0x128 list; parity JC-10, fixed), else
  `Craft.extra['micro_joint_len']` (default 0.5).
- Intervals wrap across the start of closed contours.
- Joint spans become `CutLine`/`CutArc` with `laser=False`.

## 7. Cooling points, corner features (O)

**Cooling points** (positions at 0x15c):
- Auto (0x1006a220): corners whose interior angle ≤ the max angle (pd606), plus optionally the lead point (pd604).
- Manual: 0x10069fd0.
- At output the planner forces v = 0 there (0x10104640; points within 0.2 mm of the ends are ignored), then dwells for "Cool Position Delay" (pd596).
- **Rebuild:** `Craft.cool_point` or `Craft.extra['cool_points']` (points or arc lengths). The emitted sequence is `LaserOff, Delay(cool_delay_s), Follow(False)`, then the rest of the contour is resumed with `Follow, Pierce(0 s), LaserOn` (I: the exact item sequence is U).

**Corner features:** fillet CArcRound, relief loop CUnloadAngle, α corner CAlphaRound and the automatic cooling points are in §13 (`cam.corners`).

## 8. Cutting order; `cam.ordering`

Tags: O = observed in CADModule.c / MainApp.c (address given), I = inferred, U = unknown,
R = rebuild extension (not in the original).

### 8.1 Entry points (O)

- Ribbon handler MainApp FUN_004b6190 → CCADModule vtable +0xa0 (slot 40) → 0x100c1850(sort type, small_first)
  → rebuilds the containment tree → dispatcher **0x1003f010(mode, small_first)** → undo step.
- Ribbon key → command id → sort type (ids from the ribbon builder; mf108/mf109 ids I):

| Key | Id | Type | Rebuild |
|---|---|---|---|
| mf105 Sort / mf110 Nearest | 0x34bc / 0x34c1 | 5 | `MODE_LOCAL` |
| mf106 / mf107 L→R / R→L | 0x34bd / 0x34be | 1 / 2 | band sort |
| mf108 / mf109 B→T / T→B | 0x34bf / 0x34c0 | 3 / 4 | band sort |
| mf202 Inside→Outside | 0x34c3 | 6 | `MODE_RING_IN` |
| mf203 Outside→Inside | 0x34c2 | 7 | `MODE_RING_OUT` |
| mf204 Small first | 0x34c4 | toggles small_first (cfg+0x441e) | `CamParams.small_first` |
| mf111 Manual sort | 0x34c5 | tool 9 | `manual_pick` + `MODE_KEEP` |
| mf610 / mf611 Selected layer first / last | 0x34c6 / 0x34c7 | 10 / 11 | `set_layer_priority` + `MODE_KEEP` |

- Parameters (O): GRP.SortType pd465 is an enum over mf106..mf110, mf202, mf203, mf204, so mode = index + 1
  (default index 4 = mode 5; index 7 gives 8, which the dispatcher ignores); `SORT_TYPE_ENUM`.
  IGP.AutoSortType pd372 (sort on import): 0 none, 1..7 = modes. pd466/pd1607: small first.
  pd707: fly-cut circle sort (none, L→R, R→L, B→T, T→B, local optimal). mf190: sort dialog title.
- There is no grid sort, no "by layer" sort beyond types 10/11, no separate shortest-path mode and
  **no rapid avoidance** (避让) in the original: no strings, no class (CPathLinkerPlan is the base of
  CSSort and CRingSort; COpAutoSortCmd is the undo record). Related motion features are
  FC.ShortNoUpMaxLength pd102 (short move without head lift, default 10 mm), GP.ShortDistNoUp pd113 and
  FC.EnableLeapFrogUp pd103 (NCModule CFrogJumpSvr) (O, I that they are the only "avoid" behaviour).

### 8.2 Dispatcher 0x1003f010 (O)

| Mode | Chain | Order |
|---|---|---|
| 1 / 2 | 0x1003e8f0 (CSSort, 0x100429e0) | L→R / R→L |
| 3 / 4 | 0x1003ef90 (same band sort) | B→T / T→B |
| 5 | 0x1003e690 per level (recursive) → 0x1003d300; then 0x1003c480 | local optimal |
| 6 | 0x1003efd0(…, 1) → 0x1003ec40 → CRingSort 0x1003f690(list, 1) | inside → outside |
| 7 | same with 0 | outside → inside |
| 0, 8, other | returns 0 | unchanged |

Every per-level sort recurses into the children first; the tree (CContourTopTree, doc+0x9d20) is then
flattened post-order (0x1003c7b0): holes and parts inside holes are cut before their parent.
0x1003e8f0 / 0x1003ec40 return 1 when an index was not written back and the caller shows
"重复排序序号！！！！！" (duplicate sort index).

**Element record** (0x58 bytes, 0x1003d0b0 / 0x1003e430): +0 index, +8..+0x27 bbox, +0x28 closed
(1 unless a type-8 CGlyContour with +0xe8 set), +0x30 start, +0x40 end (open contours only; closed
contours use their own start, not the lead start), +0x50 reversed (write-back calls vtable +0xb0, which
reverses open contours). Distances use an approximate hypot 0x1003b470 (0 below 1e-8; M + t·M·(1+0.5·t·(t−1)),
t = 0.5·(min/M)²) and point-to-segment distance 0x1003b800 (|p−b| if the segment is < 1e-5).

### 8.3 Modes 1–4: band sort (O)

- Direction by normalising coordinates (0x10040030): modes > 2 swap x/y in points and bbox, even modes
  negate x.
- CSSort 0x100429e0: normalise; with small_first split off small items (0x1003cd10: width < max(0.15·group
  width, 0.35·average width) and height likewise); band-sort the small items (0x10042420(…, 0)) and the
  rest (…, 1); concatenate; 0x1003fd80 with a NULL start.
- 0x10042420: extent floored at 0.001; band = average item size (0x10040170), if < 0.01 then 0.5·extent/n
  floored at 0.01; in-band tolerance 0.1·band. Each band is selection-sorted (0x10040210, moves only when
  smaller by more than the tolerance; axis/flag meaning I) and run through 0x1003fd80 from the previous exit;
  if the previous exit is no farther from the band's last start than from its first, the band is re-sorted
  and appended reversed (serpentine). With sep set, 0x10040d70 moves some items to the end (which, U).
- Occupancy bands 0x10041c10: < 3 boundaries → one band; otherwise each item's [y0, y1] goes to the
  overlapped band with the best score overlap·(0.9·(mid − y0)/h + 0.1). Histogram 0x10041690 and boundary
  rule 0x10040fb0: U.
- **0x1003fd80 is not a nearest-neighbour pass** (correction of the phase-1 doc): it only chooses the
  direction of each open contour, keep = d(P,s) + 0.5·(d(e,N.s) + d(e,N.e)) vs flip = d(P,e) + 0.5·(d(s,N.s) + d(s,N.e)).
- Rebuild: bands assigned by centre coordinate (I simplification of 0x10041c10); open contours are never
  reversed. Unchanged from phase 1.

### 8.4 Mode 5: local optimal (O) → `_local_optimal`, `_containers_last`

0x1003d300(elements, small_first, fix_first = 0):
1. Fewer than 4 items: unchanged. The machine origin is not used.
2. With small_first the small items (0x1003cd10) start the route (first small item, the others by cheapest
   insertion); a floor L = fixed + small items is set and nothing is inserted before L.
3. Open contours are appended by greedy nearest neighbour from the route end (distance = nearer of start/end,
   initial best 999999999; reversed if the end is nearer).
4. Closed contours are shuffled (srand(time), random insert positions) and added by **cheapest insertion**:
   best = distance from p to the closing edge (last exit → first start), then for pos = len−1 … 1 the gap
   a = route[pos−1].exit → b = route[pos].start is tried when |p.x−m.x| − |b.x−m.x| < best and likewise for y
   (m = midpoint); cost = point-to-segment distance; insert at max(best pos, L).
5. Cut the cycle: C = closing edge + consecutive distances; Lmax = longest edge (closing edge first); if an
   internal edge k is longest (and fix_first = 0 and no small items) the route is rotated to start at k.
6. Accept only if C − Lmax < **0.95** · (length of the incoming order); otherwise keep the incoming order.

0x1003c480 (after 0x1003e690, recursive): for c = n−1 … 1 the lowest j < c whose bbox strictly contains
slot c's bbox is moved to position c (a sibling whose box surrounds others is cut after them).

**Statement-level port** (used by the DXF import, §1): `ordering.local_optimal_elems`
(0x1003d300 on 0x58-byte `SortElem` records: closed contours use their start as exit),
`containers_last_faithful` (0x1003c480: for slot c the chain of strict bbox containers is
followed downwards and the last one found moves to c) and `import_sort` (0x1003e690 per tree
level, 0x1003c840 reversal of flipped open contours, 0x1003c7b0 post-order). Details that the
summary above leaves out (O): the small items' insertion measures the closing gap as the segment
route[0].exit → route[-1].start; the open-contour pass uses the approximate hypot 0x1003b470 and
compares start and exit; the insertion floor is the number of small items; the cycle and the
incoming length are measured **start to start** (hypot), the route is rotated only when an
internal edge is the longest, there are no small items and fix_first = 0; the shuffle is
`srand(time(NULL))` then `rand() % (len + 1)` insert positions (`MsvcRand`, seeded).
Point-to-segment distance 0x1003b800 measures the projection from the segment's second point.

Rebuild of the ribbon / toolpath sort: `build_toolpath` with `sort_mode` 5 runs the same
statement-level port as the import (`ordering.toolpath_sort` = `import_sort` with other element
points): the element start is what the graph's vtable +0x88 (FUN_100627a0) returns once the
crafts are applied, i.e. the kerf child's start, the lead-in start (FUN_1002f630) or the start
point +0x188 = the prepared contour's `entry`; open contours also get their `exit` as +0x40.
The open contours the sort flips are reversed (0x1003c840 → vtable +0xb0 = 0x10073980) and
prepared again, so their leads, kerf and joints follow the new direction. `order_nodes(mode=5)`
uses the same port on the nodes' entry/exit (it returns only the order). Remaining (I): the
shuffle uses a fixed seed (`LOCAL_SEED`) instead of `time(NULL)`. The former simplified
optimiser (`_local_optimal`) is no longer used by mode 5.

### 8.5 Modes 6 / 7: CRingSort 0x1003f690 (O) → `ring_sort`

1. Element points = bbox centres; B = group bbox; tolerance 0.1·(W·H)/n (an area used as a length, as in
   the binary); reference R = (B.x0 − 1.0, B.y0).
2. Pick: v = P[last] − R (picked at once if both components < tol); scan i = n−2 … 0 with w = P[i] − R:
   within tol → pick; s = cross(v, w)/(|v||w|); if s ≤ −0.1 then v = w; else if |s| < 0.1 and
   |w| < (1 − 4|s|)·|v| then v = w; after an update pick v if it is within tol of R.
3. Append the pick, remove it, R = its centre. This is a gift-wrap walk: the hull counter-clockwise from the
   bottom edge, spiralling inwards.
4. Mode 7 (outside → inside) keeps the spiral and then picks open-contour directions (0x1003f140, compares
   keep/flip of d(P,s)+d(e,N.s) etc.); mode 6 (inside → outside) emits the spiral reversed (0x1003f4c0) and
   does not run 0x1003f140.

Rebuild: implemented as above; the default tolerance is 0.1·sqrt(W·H/n) (I: with the literal 0.1·W·H/n
almost every point is "within tolerance" for real sheets and the walk degenerates to reverse input order);
`RING_TOL_FAITHFUL = True` restores the binary's formula. Open contours are not reversed.

### 8.6 Types 9–11: list reorders (O) → `manual_pick`, `set_manual_order`, `move_first_last`, `set_layer_priority`

- **Manual sort** (tool 9): step k at doc+0xa208. Start (view vtable +0xa4, 0x100ece20) needs ≥ 2 graphs,
  sets k = 1 and snapshots the order. Click (+0xa8 → 0x100b9a50): only graphs at index ≥ k−1 can be hit; the
  hit graph moves to slot k−1, the rest shift right; k += 1; past n the session ends (k = 0). Previous step
  (+0xac, mv13): k −= 1 if k ≥ 2. End (+0xb0, 0x100edf60; mv23/mv14): k = 0, one undo step if changed.
  First/Last process (+0x9c, 0x100b8bb0; mv16/mv15): exactly one selected graph moves to the front/back.
  Status texts mv19 "Manual Sort: Step %d", mv22, mv18, mv21; tool handling FUN_004cf000, menu FUN_004cd6d0.
- **Types 10/11**: all selected graphs must be on one layer; that layer's graphs move first/last in a
  stable partition; no geometric sort; undoable.
- Rebuild: these reorder `Document.parts` and the UI then switches to **mode 0 (`MODE_KEEP`)**, which cuts
  in document order (still inner before outer).

### 8.7 Rebuild extensions (R)

- **Mode 20 `MODE_SHORTEST` / `shortest_path`**: one global order over all contours with the constraint
  "after all its children": greedy nearest available contour, then 2-opt (segment reversal with exact
  asymmetric entry/exit costs, prefix sums; a reversal is allowed only if it contains no parent/child pair)
  and or-opt (single-contour moves inside the allowed window), bounded by `time_limit` (2 s).
- **Mode 21 `MODE_GRID`**: cells of 2× the average item size, rows bottom→top, columns serpentine, nearest
  neighbour inside a cell.
- **Rapid avoidance** `plan_rapids(order, start, clearance, avoid_parts)` / `route_rapid`: rapids that would
  cross an already-cut opening (even-depth closed contour; with `avoid_parts` also finished parts) are
  routed around it through a visibility graph over the obstacle outlines grown by `clearance`. An opening
  that contains an end point (the hole just cut) is still avoided, without clearance. Not wired into
  `build_toolpath` yet (see the requested change in the phase-2 report: `CamParams.avoid_cut`).
- `rapid_length(order, start)` measures travel; the tests check that modes 1, 3, 5, 20, 21 cut the rapid
  length of a scattered layout to < 60 % of document order and that mode 20 ≤ nearest neighbour.

### 8.8 Unknowns (U)

Which items 0x10040d70 separates; the histogram/boundary maths of 0x10041690 / 0x10040fb0; the axis/flag
meaning of 0x10040210; the feature behind pd351..358.

## 9. Formerly not recovered

Fly cut, common edge, array, import cleanup, corner features and contour smoothing were recovered in
phase 2: see §11–§15 below and motion.md §9.

## 10. Document → controller stream

### Original pipeline (O)

**1. Primitive list** (0x1006f2d0, cached at contour+0xf0; uses the kerf child if one exists). Elements are 0xd0 bytes:
- +0: type (1 line, 2 arc, 3 spline, 4 marker after a line-only lead-in, I)
- +8: length
- +0x20: arc struct
- +0x68: spline
- Order: lead-in → contour from the start (partial first and last pieces, with overcut or gap) → lead-out.

**2. CInterpMrg** (0x10105260): primitives → CContoutSmooth → CVelocityPlanning → one XY sample per period (motion.md §1). MainApp drives it through CCADModule vtable slots 125/136/131/134/137/138: select contour, load the power/frequency-vs-speed curves, run, fetch samples, fetch per-sample duty and frequency.

**3. MainApp job loop** (0x00459100): elements are 1 cut, 4 cut + start-cut sequence, 2 rapid jump, 3 scan.
- Pierce (0x00464e10): gradual or 1–5 stages. Each stage: gas type and pressure, wait, laser on with the stage's frequency and duty, hold, ramp, laser off, gas off. Stage parameters are at layer+0x158 + s·0x58.
- Start cut (0x00465460): gas, follow to cutting height, laser on (frequency at layer+0x80, duty at layer+0x7c), delay.
- End cut (0x004657e0): delay, laser off, gas off (kept open for short hops), lift or frog jump. The jump time adds JumpAddTime.
- Samples → 28-byte ManuItems (0x00462630): pulses = round(Δx_mm·10000·pulseFactor), with the rounding remainder carried.

**ManuItem layout**

| Offset | Content |
|---|---|
| +0 / +4 | dX / dY pulses |
| +0x08 | power word |
| +0x0c | U |
| +0x10 | U |
| +0x12 | duty (u8) |
| +0x14 | frequency (u16) |
| +0x16 | class |
| +0x18 | layer / pierce-stage flags |

**ManuItem classes**

| Class | Meaning |
|---|---|
| 0 | motion sample |
| 1 | system command. Sub-op: 1 laser on/off, 3 DA (gas), 4 digital out, 5 delay |
| 4 / 5 | pierce stage start / end |
| 7 / 0xa / 0xb / 0x11 | start-cut follow |
| 8 | end-cut lift |
| 9 | final lift |
| 0x12 | layer change / Z before rapid |
| 0x13 | after gradual pierce |
| 0xe | marker |

**4. NCModule encoder** (NC 0x10043c20, sendManuItems NC 0x100352b0):
- Frames start with `40 66 00 00`.
- Each command word is `(payloadBytes<<16)|opcode`.

| Opcode | Meaning |
|---|---|
| 0xbb8 | motion sample: `w1=(freq&0xff)<<24\|duty<<16\|int8(dY)<<8\|int8(dX)`, `w2=(duty?power:0)<<16\|freq>>8` |
| 0xbb9 | end of motion block |
| 0xbba | dwell |
| 0x270f | IO / laser / DA |
| 0x67..0x6d, 0x76 | height follower (payload U) |
| 0x7d1 | wait |

The protocol agent owns this layer.

### Rebuild MotionItem stream (`cam.items`, `cam.toolpath.build_toolpath`)

**Exact splines (parity JC-07).** The original keeps spline glyphs as spline primitives (type
3) into CContoutSmooth. The rebuild's contour segments are the chords of the splineAnalyer
polyline (`io.geom.flatten_bspline_params` gives their curve parameters); every cut piece that
flattens a spline carries its exact curve: `PreparedContour.curves[i]` /
`CutLine.curve` = `cam.geometry.CurveRef(curve, u0, u1)` with `SplineCurve` (point, unit tangent,
curvature on the true B-spline; arc length → parameter as FUN_10093390: linear between the
samples). The start point and the lead tangent inside a spline glyph come from the true curve.
Consumers that do not know `curve` see ordinary CutLines; the motion layer does not use it yet
(open: planner curvature from `SplineCurve.curvature`, sample positions `CurveRef.point`).
`PreparedContour.lead_in_count` / `lead_in_line` give the lead-in pieces (the type-4 marker
FUN_1006f2d0 appends after a line lead-in, parity JC-08).

`build_toolpath(doc, machine_params)`:
- `machine_params` is a `CamParams`, a dict of its fields, or None.
- It returns a flat `list[MotionItem]`.
- Coordinates are in mm, feeds in mm/s, times in seconds.
- The feed comes from `doc.layers[craft.layer].speed`.
- Pierce time comes from `layer.pierce['time_s']`, with stages from `layer.pierce['stages']`.

| Item | Fields | Meaning / mapping hint |
|---|---|---|
| `Marker(kind, part, contour, info)` | | Bookkeeping (UI progress). Original class 0xe. |
| `Rapid(start, end, lift=True)` | | Laser-off jump with the head lifted (original element 2; follower off before). |
| `LayerChange(layer)` | | Select the process parameter set (original class 0x12). |
| `Follow(enable)` | | Height follower on (cut height) / off (lift). Original classes 7/0xa/0xb/0x11 and 8/9. |
| `Pierce(point, layer, time_s, stages)` | | Pierce sequence at the current point. Original classes 4/5/0x13; stage dicts are opaque. |
| `LaserOn(layer)` / `LaserOff()` | | Beam on with the layer's frequency and duty / beam off (system command sub-op 1). |
| `CutLine(start, end, feed, layer, laser)` | | Straight cut. `laser=False` is a micro-joint span (duty 0, same speed). |
| `CutArc(start, end, center, ccw, feed, layer, laser)` | | Arc cut; a full circle has start == end. |
| `Delay(seconds)` | | Dwell (sub-op 5 / 0xbba). |
| `IO(port, value)` | | Digital output (sub-op 4). Not emitted by build_toolpath yet; available for gas/valves. |

**Per-contour sequence**

```
Marker, Rapid(prev -> lead-in start), [LayerChange], Follow(True), Pierce, LaserOn,
[Delay(start_delay)], CutLine/CutArc..., [Delay(end_delay)], LaserOff, Follow(False)
```

At a cooling point the sequence is: `LaserOff, Delay(cool_delay), Follow(False), Follow(True), Pierce(0), LaserOn`, then cutting continues. Contour order is inner before outer, using `CamParams.sort_mode`.

The protocol layer turns this stream into ManuItems:
1. Sample cut runs with `mlaser.motion.interp` (planner + 1 ms sampling).
2. Convert to pulses, carrying the rounding remainder.
3. Set duty 0 on spans with `laser=False`.
4. Expand Pierce, Follow and Laser items into the system-command classes listed above.

## 11. Fly cut (飞切, scan cutting `mf112`); `cam.flycut`

Tags: **O** observed (address), **I** inferred, **U** unknown. CADModule addresses unless prefixed `MainApp`.

### 11.1 Entry and dispatch (O)

- Ribbon handler (MainApp, around 0x004b6d00; command ids 0x34ef..0x34f4) sets the scan type in MainApp+0xf368, then the worker thread MainApp 0x00480520 calls CCADModule vtable slot 88 (offset 0x160, 0x100f30d0 → `FUN_100c80d0`).

| Command | Type | Dialog | Builder |
|---|---|---|---|
| 0x34ef (with dialog 0x15) / 0x34f1 (no dialog) | 1 circle (`mf112_1`) | CGraphParaDlg mode 0x15 | 0x100a7d30 |
| 0x34f0 (dialog 0x16) | 2 line / segment (`mf112_2`) | CGraphParaDlg mode 0x16 | 0x100a9d80 |
| 0x34f2 | 3 arc (`mf112_4`, I) | none | 0x1009f220 |
| 0x34f3 (dialog FUN_004310c0(0)) | 4 scan engraving (`mf112_5`) | engraving dialog | 0x100a9d80 |
| 0x34f4 (dialog FUN_004310c0(2)) | 5 (G-code scan engraving, I) | | 0x100a6580 |

- Arguments (MainApp 0x00480520, GraphParam struct offsets; names from `mlaser_params.json`):

| Arg | Offset | Param | Key | Default |
|---|---|---|---|---|
| first done single-contour circles | 0xb2e0 | GRP.FirstDoneSingleContourCircleScan | pd851 | 1 |
| circle sort type | 0xb2d4 | GRP.CircleFlySortType | pd707 | 0 (none, L→R, R→L, B→T, T→B, local optimal) |
| circle max fly length | 0xb2d8 | GRP.CircleFlyMaxLen | pd708 | 80 mm |
| line start corner | 0xb2e4 | GRP.LineFlyStartPos | pd715 | 0 (LD, RD, LU, RU); type 4 uses 0/2 from pd719_4 (0xb3b0) |
| collinear tolerance | 0xb2e8 | GRP.LineFlyCollineTol | pd716 | 1 mm |
| max fly length | 0xb2f8 | GRP.LineFlyMaxLen | pd718 | 40 mm |
| max smooth-link length | 0xb2f0 | GRP.LineFlyMaxLinkLen | pd717 | 80 mm |
| min horizontal gap / min scan line | 0xb398 / 0xb3a0 | pd719_1 / pd719_2 | | 0.01 |
| side line length | 0xb3a8 (type 4) or 0xb340 | pd719_3 | | 5 mm |

- `FUN_100c80d0`: needs ≥ 2 selected type-8 contours, all on the first one's layer (and not the last layer index). Creates a CGlyScan (0xe8 bytes, ctor 0x1009b570) with +0xe0 = type (the "scan +0xe0 int" of formats.md §2.5). After a successful build the used graphs become the scan's group elements (0x1007d880), are removed from the drawing, and the scan takes the first one's slot; an undo command COpGroupGraphCmd is pushed.
- Hard parameters (FCParam): pd289 encoder check tolerance ratio (0x49f8), pd290 circle velocity ratio (0x4a00, default 1), pd513 circle PWM delay time (0x4a08, 0), pd514/pd515 line PWM open/close "forward cycles" (0x4a10/0x4a18, −10). MainApp 0x004572f0 copies them with other options into a 0x40-byte block passed through slot 127 (0x100e8cc0 → 0x101020b0) to both CInterpMrg instances (+8..+0x48) (O). Where the interpolator applies them is **U**; the rebuild offers `shift_switches(path, on_mm, off_mm)` (I: time × feed).
- Not ported (U): type 3 (concentric arcs: 0x1009f220 accepts arcs with span·r ≥ 0.1 on a common centre; equal radii are joined along the circle, different radii by a 4-point Bézier with k = min(d/2, 2)), type 5, the engraving generator itself, the link-collision split 0x100a7580.

### 11.2 Circle fly (type 1) (O unless tagged)

1. **Circle test** (0x1009f0f0): one CEditableCircle glyph, or a fitted circle (0x1009eb40) whose span is 2π within 0.1 rad (0x1011f0b0). The kerf child's radius is used when present. Rebuild: `detect_circle` (arcs on one centre/radius summing to 2π ± 0.1).
2. **Overlap filter** (0x100a7d30): a circle with |c−c_k| < r + r_k for an accepted circle is rejected (stays a normal contour).
3. **Sort** (0x100a18e0): pd707 1..4 use CSSort band sort on the circle bboxes (0x100429e0, same as cam §8), 5 uses 0x100a04a0, 0 keeps the order. Rebuild reuses `ordering.sort_level`.
4. **Groups**: a new group starts where consecutive centres are farther apart than pd708. Single-circle groups become a plain circle contour; others one scan path contour with +0x114 = 2 (0x100a1f80).
5. **pd851** (FUN_100c80d0 type-1 pre-pass, I in detail): circles inside one outer contour are flown as their own groups first.
6. **Path** (0x100a1f80): per circle {centre, r, start angle θ (+0x30), tangent (+0x38), dir ±1 (+0x48), link type (+0x4c)}.
   - First circle: θ = angle(u₁) − 90°, dir = +1, u₁ = unit vector to the next centre; the CCW tangent at the start points along u₁.
   - Collinear runs (centres on a line within the scan's +0xd8 tolerance) of equal radius copy θ and dir.
   - Other circles: θ = angle(u_i) − dir·90°; the original also flips dir when the turn lies in (30°, 135°) (constants 0x1011f3d8 = 30, 0x101210f0 = 135) and uses link types 1/2 (0x1009db40) or 3 (0x100a0d00) (**U**, not decoded).
   - Each circle is one full-turn arc glyph (0x148 CEditableArc) from θ to θ + dir·2π.
   - Link type 0: cubic B-spline glyph (0x188) with 5 control points, knots 0,0,0,0,0.5,1,1,1,1: P0 = previous start point, P1 = P0 + k·t, P2 = midpoint − min(r/4, 2)·radial unit of the next circle, P3 = P4 − k·t, P4 = next start point. k = r/√(…) (argument **U**); rebuild uses d/3 and the previous circle's exit tangent at P0 (I).
   - Rebuild addition (I): when the next entry is behind the exit tangent, the head overtravels (beam off) on the circle just cut until its tangent is within 30° of the next entry, then links. This replaces the undecoded arc links and keeps linkers off the cut circle.
7. **Laser switches** (+0x13c, 0x100a1f80 tail): a = min(6°·r, 1.2 mm) (0x1011fc00, 0x1011d8f0), b = min(10°·r, 3 mm) (0x1011f0f0, 0x1011da80). For circle i with path range [s_i, e_i]: "on" at (s_i − a + b)/L (skipped for the first), "off" at (e_i + a + b)/L (skipped for the last). Beam on at the path start. The list is sorted. For the last circle the decompiled on-position reads `… + b/3` (likely an x87 artefact, **U**; rebuild uses the same formula as the others).
   - Consequence (I): with the defaults the beam comes on (b − a) after the circle start and stays on (a + b) into the linker. The meaning (laser latency compensation?) is **U**. `CircleFlyParams(lag_*=0, overlap_*=0)` gives exact one-turn coverage.
8. pd290 circle velocity ratio: rebuild multiplies the feed on arcs by it (I).

### 11.3 Line fly (type 2) (O unless tagged)

1. **Edges** (0x1009cbe0): only contours whose glyphs are all lines (segment or bulge-free lwpoly; type 4/5 also accept splines) qualify. Edges shorter than 0.5 mm (DAT_1011d738) are dropped.
2. **Collinear lines** (0x1009c6b0): the first edge sets direction (+0x10) and origin (+0x30); a later edge joins when |dir × v| ≤ min(tol, 0.08·len) (0x1011fa60) and its start's distance from the line ≤ tol (+0x40 = pd716). 0x1009b030 sorts the edges along the line; a line with direction (−√½, √½) is flipped (0x1009c090, constants 0x10121140/38). Rebuild: canonical direction angle in (−45°, 135°]; overlapping edges along a line are merged (I), so a shared edge of two touching rectangles is cut once.
3. **Gap split** (0x100a9d80): a line is split where the gap between edges exceeds 1.1·pd717 (0x10121130; 500 mm for type 4, 0x10121108).
4. **Ordering** (0x100a8970, 0x100a5c70): direction families (|Δdir| ≤ 0.01). Start at the pd715 corner of the bbox (even → x min, odd → x max; < 2 → y min, else y max). Pick the line end nearest the current point (continuing the current family while within 5·pd717 + 0.5, 0x1011f428), then serpentine: chain the next line of the family whose nearer end is within pd717. Chains are merged when the link is ≤ pd717 and the turn is within the 120°/150° window (0x10121118/20) (rebuild: distance test only, I).
5. **Path** (0x100a3f10): edge glyphs alternate with linker glyphs. Between edges (P0 end of previous, P3 start of next, d = |P3 − P0|, d < 0.5 → 2):
   - straight Line when the directions differ by ≤ 0.005 (0x1011da88) per component and the lateral offset is < 0.035·d (0x10121100);
   - cubic Bézier when dot(t0, t1) ≥ 0 or |t0 × t1| ≥ 0.14 (0x101210f8): P0, P0 + k·t0, P3 − k·t1, P3 with k = min(d/2, 2), k = 4 (0x1011f5c8) when d > 30;
   - otherwise (U-turn) a 6-point cubic B-spline, knots 0,0,0,0,0.05,0.95,1,1,1,1 (0x1011d6d0, 0x1011f5a0): h = min(d, 2), b = min(0.4·h, 2) (0x1011f178), a = b/2; e = |Δ·t1| − |Δ×t1|; if e > 0 and Δ·t0 ≤ 0 then a, b += e; points P0, P0 + a·t0, P0 + (a + h)·t0, P3 − (b + h)·t1, P3 − b·t1, P3.
6. **Switches**: +0x114 = 3, +0x354 = scan type. For types other than 4, every odd glyph (a linker) contributes its start and end fraction, so the beam is on at the start and off on each linker. For type 4 (engraving) the odd glyphs are the cuts (beam off at the start; spline glyphs never switch).
   - Verified on the sample `autosave.chf` (scan +0xe0 = 4, one 2077-glyph path, 1678 switches): with "start off" parity the laser-on length is 3080.945 mm, equal to the 839 group elements (3080.963 mm), and the on-span length histogram matches the element lengths (tested). The stored fractions (6 decimals) drift against our spline lengths, so `scan_paths_from_chf` snaps line/engraving switches to glyph boundaries while tracking the offset.
7. pd718 "max fly length" is passed to 0x100a9d80 but no use was found (**U**).

### 11.4 Rebuild API

- `circle_fly(contours, CircleFlyParams, parents=None) -> (paths, used_indices)`
- `line_fly(contours, LineFlyParams) -> (paths, used_indices)`
- `ScanPath(contour, switches, initial_on, kind, pwm_mode, sources, arc_feed_ratio)`, with `.spans()` → [(segment, laser_on)] and `.on_intervals()`.
- `split_by_switches`, `switches_from_spans`, `shift_switches`.
- `scan_motion_items(path, feed, layer, pos, pierce_time_s, stages, part, index)` gives `Marker, Rapid, LayerChange, Follow(True), [Pierce], LaserOn, CutLine/CutArc(laser on/off)…, LaserOff, Follow(False)`. Off spans are moves at cut speed with `laser=False`, like micro-joint gaps (one continuous motion, per-sample duty 0).
- `make_scan_part(paths, sources, craft, kind)`: a Part whose contours are the source contours, with `extra['scan_paths']` and a ChfScan record (`extra['chf']`, chf_type 11; path +0x114 = mode, +0x13c = switches, +0x354 = kind), so `save_chf` writes a type-11 graph. `scan_paths_of_part(part)` returns the paths of built or loaded scans.
- `apply_fly_cut(doc, kind, params, selected_only=True)` replaces the consumed contours by one scan part, in the first consumed part's slot.

### 11.5 Unknowns

- How CInterpMrg/MainApp apply pd513–pd515 and pd289 (encoder position check; RegName37–39/87 "flycut cache" registers suggest controller-side position-triggered switching).
- Link types 1/2/3 and direction flips between non-collinear circles; the spline k factor; collision split 0x100a7580.
- Types 3 and 5; pd718.

## 12. Common edge (共边) (O unless tagged); `cam.commonedge`

Lang: mf250 "Share Edge", mf250_1 "C-Type Share Edge", A250707_11 "Rectangles have common edges"
(A250707_0 type, A250707_1 over-cut on/off, A250707_2 over-cut length); nesting has its own
pd_Nest_ShareEdge / pd_Nest_ShareEdgeMinLen (SmartNest/AutoNest, not covered here).
Classes: CShareEdge (vtable 0x101213a4, only a destructor; a value object holding tol at +8),
undo record COpShareEdgeCmd (vtable 0x10121690).

### 12.1 General share edge: driver 0x100cabb0(this, c_type, rebuild_tree)
- CShareEdge+8 = 0.05 (0x1011d6d0) is the coincidence tolerance.
- If `rebuild_tree`, the containment tree at doc+0x9d20 is rebuilt first (0x100239b0/0x100250a0/0x10022270).
- Only type-8 contours take part; the **kerf child (+0x110) is used when present** (0x10047310), so
  parts nested one kerf apart share their compensated paths.
- **Pair test** 0x10047310 → 0x10044770: bboxes overlap within tol, then glyph pairs. A contour holding a
  circle (glyph 4) or a point (glyph 1) never shares. Line/line 0x10043d50: |d2×d1|/|d1| ≤ 0.1
  (0x1011f0b0) plus a sin(ε)·|d2| check (ε on the x87 stack, U); end points projected with 0x10043390
  (coincident within tol, else on-segment with perpendicular distance ≤ tol); overlap 0x100434d0 must be
  ≥ 0.1 mm on both edges. Arc/arc 0x10043ef0, lwpoly 0x10043850/0x10043b60, arc/lwpoly 0x10042e10, spline
  0x10042ca0 also exist (rebuild: line/line and concentric equal-radius arc/arc, I).
- **Intervals** 0x100494f0: for each pair i < j the interval on i is tagged 1, the interval on j tagged 2
  (0x10081510 inserts; 0x10043630/0x10045b20 sort and merge).
- **Normal** (c_type = 0) 0x10049c40: per contour, the gaps between consecutive intervals (wrapping on
  closed contours) become open pieces (0x10046650 extracts a sub-contour), and every **tag-2** interval
  becomes a shared piece; pieces ≤ 0.1 mm are dropped. So each shared stretch is cut once, from the later
  contour's copy. The pieces replace the originals and are re-sorted with 0x1003f010(.., 3, 1), i.e.
  bottom→top (mode 3, small-first flag 1), at the minimum depth of the inputs.
- **C-type** (c_type ≠ 0): parts are grouped by connectivity (0x10050ed0), groups ordered by
  0x1003e430(.., 3); 0x10049730 deletes the tag-2 intervals from each contour and keeps the rest as open
  pieces per contour. Every part is cut as an open "C" that omits the edges an earlier part already cut;
  the earlier part keeps (cuts) the shared edge.
- Output pieces are new CGlyContours (`FUN_1006c2d0`, closing tolerance copied from +0xd8): no kerf,
  no leads (I: default craft).

### 12.2 Rectangle common edge: 0x100c36e0(type, overcut) → 0x10047530 → 0x10047d30
- 0x10047530: every selected contour must pass 0x10043220 (closed, 5 points/4 sides each equal to the
  bbox width/height within 0.01), have the first rectangle's size within 0.02 (0x1011dae0), be at least
  0.09 (0x1011f6a8) in both directions, and the centres must fill a complete rows × cols grid
  (cell = round((c − c_min)/size)). Otherwise nothing happens.
- 0x10047d30 builds the grid point matrix P[r][c] = (x0 + c·w, y0 + r·h) and, by type:
  - **0**: rows+1 horizontal and cols+1 vertical full-length lines (0x100430b0 makes a 2-point contour).
  - **1**: one path per cell, rows bottom→top, columns alternating direction per row (serpentine):
    [left edge if first cell of the row] → top → right → [bottom if row 0]. Every cell is separated
    by its own path, and every grid edge is cut once.
  - **2**: one staircase polyline per anti-diagonal (k = 0..rows+cols−2): per cell right and top edges,
    plus bottom on row 0 and left on column 0; odd diagonals reversed.
  - Type labels (I): A250707_7/8/9 "横平竖直 / S型 / 梯形" sit next to these keys and fit 0/1/2.
- **Overcut** (types 1/2 only): when ≥ 0.1 (0x1011f0b0) the first point moves 0.7·overcut (0x1011f298)
  along the first segment (not for the very first path), and the last point is pushed out by the
  overcut along the last segment; type 1's last cell of a row instead appends (x, y + overcut). The
  original also splits each path into a short head (2–3 points) and the rest (0x1001d670 / 0x10046d10)
  and emits both: purpose U, not ported.

### 12.3 Rebuild API
- `find_shared_edges(contours, tol=0.05, min_len=0.1) -> list[SharedEdge]` (i < j, mm intervals on each).
- `share_edge(parts, c_type=False, tol, min_len, sort=True) -> list[Part]`; untouched parts are returned
  first and unchanged, new parts carry `extra['common_edge'] = {shared|c_type, source}`; normal mode
  makes one Part per piece (open contours), C-type one Part per source part.
- `share_edge_document(doc, c_type, selected_only)` → new Document for `build_toolpath`.
- `detect_rect_grid(parts) -> RectGrid | None`, `rect_share_edge(grid, kind, overcut) -> list[Contour]`.
- Tests: `tests/test_camx_commonedge.py` (two/three squares: shared edge cut once, cut length reduced by
  the shared length, every unit grid edge covered exactly once for all three rectangle types).

**Unknowns:** ε of the parallel test; the head/tail split under overcut; the exact chaining of output
pieces (0x1004a5d0 joins pieces with 1.5·w / 3.5·w windows, 0x1011f0e8/0x1011f718, not ported); arc/
spline share variants; whether the original puts shared pieces before or after the plain pieces
(the final sort makes it moot).

## 13. Corner treatments (O unless tagged); `cam.corners`

All four corner commands share the CCornerBase-like helper object (ctor 0x10056620, vt[1] 0x10056830
loads the contour glyph list with its directions, vt[2] 0x10056f40 writes the result back; the
derived vtables CArcRound 0x1011fb7c, CCoolPoint 0x1011fb8c, CUnloadAngle 0x1011fb9c, CAlphaRound
0x1011fbac differ only in the destructor). For the vertex between glyph i and i+1: t_in = tangent at
the end of i (sign flipped for dir −1), t_out = tangent at the start of i+1, and
**c = −(t_in·t_out) = cos(interior angle)** (180° straight, 90° square, ~0° spike).
`corners(contour)` enumerates vertices with these fields.

### 13.1 Fillet CArcRound (倒圆角, pd600 radius, pd601 min angle, pd602 max angle; mf400/401/484)
- Manual (click a corner): contour 0x10075e00 → 0x10057010 (find corner near the point) → 0x10059f70.
  Needs both glyphs ≥ r + 0.05, arcs with radius ≥ 2r, c ≤ 0.999 (0x1011f3a8).
- Auto: 0x10075e00(NULL) → 0x1005a650. Lwpolys are exploded first; the wrap vertex counts when the
  contour ends within 0.1 of its start. Filter: glyphs ≥ 0.05, arcs radius ≥ 2r,
  −0.999 ≤ c ≤ 0.999 and cos(max) − 0.01 ≤ c ≤ cos(min).
- Geometry: line/line 0x100579d0 (bisector of −t_in and t_out, centre at r/sin(θ/2), arc angles from
  asin), arc/arc 0x100595d0, line/arc 0x10059b10. Both glyphs are split at the tangent points
  (vt+0x28, tolerance 0.1) and the arc is inserted (vt+0x94 trims, 0x10057e90/0x10057ee0 insert glyph
  and dir). The result reports (position fraction, length change, arc length).
- Rebuild `fillet(c, radius, min_angle, max_angle, at=None, manual=False)`: line/line only (I: arc
  variants not ported); a corner is skipped when the tangent points do not fit on what is left of the
  lines. After a corner edit the contour's kerf child is dropped and kerf mode reset to −1, joints
  regenerated (0x10074e00, 0x1006c930) (O, caller side).

### 13.2 Relief loop CUnloadAngle (释放角, pd603 radius; mf404)
- 0x10076040 (click) → 0x100574a0 → 0x10058370. Line/line only, both lines ≥ 2r, |c| ≤ 0.97
  (0x1011f950/0x1011f958).
- S = P − r·t_in, E = P + r·t_out; arc 0x10057800 centred on the vertex P, radius r, CCW when
  cross(t_in, t_out) ≥ 0 else CW, angle wrapped by ±2π (0x1013c240) so it goes the long way round,
  outside the corner wedge (270° at a square corner). Lines trimmed to S and E.
- Effect (I): a dog-bone/ear at the corner. On a hole's corner it gives the classic corner relief.
- Rebuild `unload_loop(c, radius, at=None)`.

### 13.3 α corner CAlphaRound (倒α角, pd607 max angle, pd608 min edge, pd609 outside lead length, pd614 type: pd610 auto / pd611 outside / pd612 inner / pd613 both; mf402/403/485)
- 0x100761d0(type, ...) → 0x100587a0; type −1 = clear (0x100593a0 removes inserted loops).
  0x10056ff0 stores the contour's CCW flag (+0xec) and "depth even" (+0x88).
- Accept a vertex when both glyphs ≥ min edge, cos(max) ≤ c ≤ 0.9998 (0x1011f960), and the side test
  passes: s = CCW ? +1 : −1; auto negates s on even depth; inner negates s; both skips the test;
  reject when cross(t_in, t_out)·s < 0 — i.e. outside = convex corners, inner = reflex, auto = the
  corners that are material tips (convex on outer contours, reflex on holes). Vertices within
  min(0.35·lead, 0.5) (0x1011f598, 0x1011d738) of the start point are skipped.
- Inserted glyph: cubic B-spline (CEditableSpline 0x188, 0x10097140), knots [0,0,0,0,2,4,4,4,4],
  control points P, P + k·t_in, P + k·(t_in − t_out), P − k·t_out, P, with k = 0.5·lead/√X. The loop
  leaves along the incoming direction and comes back along the outgoing direction (an "α"). The edges
  are not trimmed.
- X is computed on the x87 stack (U); the rebuild uses √X = |t_in − t_out| (I).
- Rebuild `alpha_corners(c, max_angle, min_edge, lead, mode, depth, start_dist)`; the spline is flattened
  to 24 lines (`alpha_loop`, `bspline_points`).

### 13.4 Auto cooling points (pd604 lead point, pd605 sharp corner, pd606 max angle; mf406/407/408)
- 0x1006a220(lead, sharp) → 0x10057c10: a vertex is cooled when c ≥ cos(max angle); position = arc
  length at the start of the outgoing glyph / total length, stored at +0x15c. With "lead", the start
  parameter +0x180 is inserted in order unless a point lies within 0.01 (fraction). Neither flag
  clears the list. At output (0x10104640) points within 0.2 mm of the ends are ignored (see §7), so the
  lead point only matters for MainApp's own handling (U).
- Rebuild `auto_cool_points(c, max_angle, sharp=True, lead_point=False, start_dist=0) -> [mm]`, ready for
  `Craft.extra['cool_points']`.

### 13.5 Kerf corner join (the Bézier, closes §5's unknown)
- Offset builder 0x1003ad00 → 0x1003a2f0 → 0x10038130. Vertex records (0x10031ec0, 0x50 bytes): cross
  and dot of the adjacent tangents, both tangents. At a gap (convex) vertex with dot below the miter
  limit, between offset end A (tangent ta) and offset start B (tangent tb):
  - if | |ta×AB| − |tb×AB| | ≥ 0.01 (0x1011d800): cubic Bézier, knots [0,0,0,0,1,1,1,1], control
    points A, A + |AB|·ta, B − |AB|·tb, B (spline glyph; dropped later if shorter than 0.02, 0x10031be0);
  - else a tangent arc A→B (0x100313c0, kept if longer than 0.001).
  - shallow corners: the pieces are extended to their intersection (miter), and arc neighbours use the
    mid-point of the two extensions.
- Rebuild `kerf_corner_join(a, ta, b, tb)` / `bezier_corner_join(...)` (flattened Bézier). `cam.kerf`
  still uses its own round join; hooking this in is a change to kerf.py (not owned here).

**Unknowns:** X in the α spline factor; the arc/arc and line/arc fillet constructions (0x100595d0,
0x10059b10) beyond their inputs; the ε of the share-edge parallel test; MainApp's use of the lead cooling
point.

## 14. Array (阵列) and fill (圆填充 / 直线填充); `cam.array`

### Rectangular array (O)

MainApp `0x004b29b0` (ribbon mf79) sets `GRP.OffsetType = 1` (spacing), opens the property-grid dialog
and on OK calls CCADModule vtable +0x124 (slot 73, thunk `0x100f2ea0` → `0x100b92b0`). The call-site
assembly (`0x4b2a3a..0x4b2ab0`, base = ParaModule struct +0xb3b8) passes:

| Arg | Source | Parameter |
|---|---|---|
| rows | +0xb3bc | pd332 `GRP.ArrayRowNum` (int, default 3, 1..10000) |
| cols | +0xb3c0 | pd333 `GRP.ArrayColNum` (int, default 3) |
| x positive | +0xb3dc == 1 | pd338 `GRP.ColDir` (0 left, 1 right) |
| y positive | +0xb3d8 == 1 | pd337 `GRP.RowDir` (0 down, 1 up) |
| spacing | +0xb3c4 != 0 | pd334 `GRP.OffsetType` (0 offset, 1 spacing) |
| x value | +0xb3d0 | pd336 `GRP.ColGapVct` (default 10) |
| y value | +0xb3c8 | pd335 `GRP.RowGapVct` (default 10) |

`0x100b92b0`:
- step_x = ±ColGap, step_y = ±RowGap; in spacing mode the selection bbox width/height (signed the same
  way) is added, so the value is the gap between copies; in offset mode it is the pitch.
- The element count rows·cols·n is checked by `0x100da250` (memory guard; mf505 "out of memory").
- Copies are created row by row. Even rows place column c at c·step_x, odd rows at (cols−1−c)·step_x,
  so the creation (and default cutting) order is serpentine. Cell (0,0) is the original.
- Each copy is a clone (vt+0x70) translated by vt+0xc; the whole operation is one undoable COpCopyCmd.

**Rebuild:** `ArrayParams`, `rect_array(parts, prm)` (returns the copies, each with
`extra['array_cell'] = (row, col)`), `apply_rect_array(doc, prm)` (selection or all).

### Circular and fill-sheet array (rebuild additions)

No lang key, command or code for a circular/polar array or a "fill the sheet" array exists in this
version (searched lang.txt, MainApp and CADModule strings). They are provided as conveniences:
`circular_array(parts, count, center, total_angle_deg=360, rotate_items=True)` and
`fill_sheet_array(parts, sheet_rect, gap, margin)` (spacing-mode rectangular array sized to the sheet).
Real nesting is in `mlaser.nest`.

### Fill circle / linear fill (O, details I)

Class CFillCircle (vtable 0x101219a0), driver `0x100ce2d0(type, radius, stock, space, angle_deg,
keep_original)`; the per-region worker is `0x1002f390`. Parameters (GraphParam):

| Key | Lang | Default | Object field |
|---|---|---|---|
| `GRP.FillGlyDir` | pd725 (pd720 X, pd721 Y, pd722 along edge) | 0 | +0xc type |
| `GRP.FillCircleRadius` | pd726 | 4 | +0x20 |
| `GRP.FillCircleStock` | pd727 | 10 | +0x10 (margin to the boundary) |
| `GRP.FillCircleSpace` | pd728 | 10 | +0x18 (gap between circles) |
| linear fill angle / spacing / margin / keep | A250707_3..6 | | +0x28 = angle·π/180 |

- The circle count is pre-estimated as bbox area / (r+space)² per region and checked by `0x100da250`.
- Type 0 / 1 (`0x1002aec0(this, 1|0)`): scan lines over x (type 0) or y (type 1) from bbox.min + stock + r
  to bbox.max − stock − r (+0.1). A line without inside intervals advances by 0.1 mm, otherwise by
  2r + space. Intervals come from the outer contour and the holes (`0x100286a0`, sorted `0x10026580`).
- Per interval (`0x100292d0`; skipped if shorter than 2·(stock + r)): the first valid centre is searched
  from a + stock + r forward and the last from b − stock − r backward, in steps of 0.2·space. Valid means
  the circle of radius r + 0.5·stock clears the outer contour and all holes (`0x10026880`). If the span is
  ≥ −0.1·space it is divided evenly into n = round(span / pitch) gaps (a single centre at the midpoint when
  n < 1). Each centre must also be ≥ 2r + 0.7·space from all circles already placed (`0x10026a10`).
- Type 2 "along edge" (`0x1002dd70`) and type 3 linear fill (`0x1002e210`; the region is rotated by
  −angle, filled with horizontal lines, rotated back) were not decoded in detail (I).
- A selection containing an open contour fails with A251103_1.

**Rebuild:** `FillParams`, `fill_circle_centres(outer, holes, prm)`, `fill_circles(...) -> [Part]` (one full
circle per Part, ready for circle fly cut), `linear_fill(...) -> [Part]` (S-ordered hatch lines). The
along-edge mode places circles at the pitch on inward offset rings (I).

## 15. Import cleanup; `cam.cleanup`

### Parameters (O)

MainApp `0x004d35c0` copies the "IGP." import parameters into the CAD import option block
(CCADModule+0x8b8, returned by vtable +0x260). With "raw import" (param true) it forces no
joining, overlap removal on, micro removal on, gates 0.

| Block | Key | Lang | Default | Range |
|---|---|---|---|---|
| +0x00 | `IGP.MegerConnectGraphType` | pd367 | 1 | 0 none, 1 direction first, 2 length first, 3 distance first |
| +0x08 | `IGP.ConnectGate` | pd368 | 0.1 | 0.01..0.5 |
| +0x10 | `IGP.IsFilteOverlapGraph` | pd365 | 1 | |
| +0x18 | `IGP.OverlapGate` | pd366 | 0.1 | 0.01..0.5 |
| +0x25 | `IGP.IsAutoFilterMicoGraph` | pd363 | 1 | |
| +0x28 | `IGP.MicoGraphGate` | pd364 | 0.1 | 0.01..10 |
| +0x30 | `IGP.IsAutoSmoothGraph` | pd369 | 0 | |
| +0x38 | `IGP.SmoothAccuracy` | pd370 | 0.05 | 0.01..10 |
| (+0xb462) | `IGP.IsAutoSmoothSpline` | pd369_1 | 0 | (consumer U) |
| (GRP) | `GRP.ManualConnectGate` | pd368 | 0.1 | manual "connect" command (vt+0xe4 → `0x100bf7e0`) |

The DXF reader also receives MicoGraphGate (`0x100f0e10` → `0x10005f90`).

### Pipeline (O, `CGly2ContourEng` `0x10061c90`, called from the import `0x100f0e10` via `0x100b1220`)

1. If +0x10: overlap removal on the loose glyphs, `0x10061190(glyphs, bbox, min(ConnectGate, OverlapGate),
   0.11·ConnectGate)`, using a 20×20 spatial grid (`0x1005bef0`). Glyphs covered by another (vt+0x70) are
   deleted; partial overlaps are trimmed (`0x1005e490`). Exact overlap test: I.
2. Chaining by +0x00: type 0 makes one contour per glyph; type 1 uses `0x1005f7d0`/`0x100606d0`; others
   build contours one by one with `0x10076a20`.
3. Small objects `0x1005c150`: gate = MicoGraphGate if +0x25, else 0.01. A contour is deleted when
   max(bbox w, h) < gate; a single-point contour is kept.
4. `0x1005c2f0` splits some open contours (vt `0x10075a10`; U).
5. If +0x30: per contour `0x10075820(accuracy)` → `0x1004b8e0`, which replaces runs of short glyphs with
   splines (CEditableSpline); the fit is U.
6. Orientation (`0x10073620`), bbox/length refresh.

**Joining rules** (`0x10076a20`, O): ends match when |dx| ≤ tol and |dy| ≤ tol (square test); the new
contour's closing tolerance is the gate (ctor `0x1006c2d0(tol)`). With several candidates: type 1 picks the
maximum dot product of the end tangent with the candidate's tangent (initial −2), type 2 the candidate by
length (initial 1e8, so the shortest, I: the compare is inverted in the x87 pseudocode), type 3 the nearest
candidate end.

**Clear duplicate lines** (mf471; vt+0xd8 → `0x100c0a30(OverlapGate)`, O): from the last contour backwards,
contour i is removed when another contour j (bbox overlapping i's bbox ± gate; for closed i also
|len_i − len_j| ≤ 10·gate) contains all of i's sample points (i's polyline decimated so that consecutive kept
points differ by ≥ 5·gate in x or y; with < 3 samples the end points are also tested).

**Merge connected lines** (mf476; vt+0xec → `0x100c0300(ConnectGate, type)`, O): explodes every contour and
group into glyphs and re-chains them all with `0x10076a20`.

### Arc fitting: CircleFitDLL (O, ported statement by statement)

Exports: `CCircleFit::mainFit(MyPoint*, int first, int last, CdlFitOut*)` (`0x10001010` → `0x100011e0`), data
`Err_CircleFit` 0.02, `Ang_CircleFit` π/10, `Len_CircleFit` 100, `Rmax_CircleFit` 5000, and the output counter
`fitOutSequence` (index of the last record written). MyPoint is 0x58 bytes: x @0, y @8, tangent sums @0x18/@0x20,
tangent angle @0x28, scratch errors @0x40/@0x48, chord length to the next point @0x50. CdlFitOut is 0x30 bytes:
type `'L'` (start, end), `'A'` (centre, radius, a0, a1; no direction flag) or `'I'` (centre, radius).

Users: Dxf2Grp `0x1000eec0` (runs of line glyphs; it sets Ang = π/10, Err = caller tolerance, Len = 10, resets
`fitOutSequence` to 0 and copies 'A'→arc, 'I'→circle, 'L'→line) and AutoNest (sets Rmax). CADModule/MainApp do not
import the DLL.

`0x100011e0` (O):
- drops every point whose |dx| and |dy| to its predecessor are both < 1e-6; two points → 'L' records only;
- `0x10002420`: interior tangent t_k = atan2 of the sum of the two normalised neighbouring chords; end tangents
  t = 2·(end chord angle) − (neighbour tangent), wrapped to [−π, π];
- `0x10002af0`: if no interior tangent corner (`0x100029d0`: tangent jump > Ang, with the ±π/2 wrap rule) and the
  Kåsa least-squares circle of all points (end point included) has max radial error of the points and the chord
  midpoints < Err and end gap² < 0.1·Err → one 'I';
- otherwise, from p = first while p < last:
  - **A** (tangents decreasing from p, i.e. turning CW): extend k while t keeps decreasing (wrap-aware) and the chord
    turn at k (angle(k−1,k) vs angle(k,k+1), wrap fix at ±π/2) is ≤ Ang. If k < last, t_k is reset to the mirror of
    t_{k+1} about chord k→k+1. If the run has more than 4 points it is copied reversed (`0x10002620`, tangents
    recomputed on the copy), fitted by `0x100019e0`, and the records are put back in path order with 'L' end points
    swapped (`0x100028a0`);
  - **B** (tangents increasing, CCW): same growth; if k < last, t_k := mirror of t_{k−1} about chord k−1→k (end
    tangent); a run of more than 4 points goes to `0x100019e0` in place; then t_k := mirror of t_{k+1} (start of the
    next run);
  - **C** (neither A nor B produced output): a line from p, extended while the chord deviation (`0x10002680`:
    max |p_i p_m|·|sin Δangle|) to k+1 is ≤ Err and the chord turn at k is ≤ Ang; t_k := mirror of t_{k+1}.
- `0x100019e0(i, j)`: one 'L' if the whole run's chord deviation < Err; else grow k from i+1 while the biarc i→k
  (`0x100020b0`: α = chord − t_i, β = t_k − chord, r1 = |sin(β/2)·d / (2 sin(α/2) sin((α+β)/2))|,
  r2 = |sin(α/2)·d / (2 sin(β/2) sin((α+β)/2))|, sines clamped to ±1e-6, centres on the left of t_i and of the
  chord) keeps every point within Err (`0x10003100`); if more than 4 points, shrink k until the chord midpoints are
  within Err (`0x10002eb0`), r1, r2 ≤ Rmax, r1/r2 within 50 and |c1c2| − |r2 − r1| ≤ Err, then output 'A','A'
  (a0/a1 = angles of the end points about each centre); otherwise output the longest line (deviation < Err, no
  tangent corner, turn ≤ Ang). Recurse on k..j.

**Rebuild** (`cam.cleanup`): `CleanupParams` (IGP defaults), `remove_small`, `remove_duplicate_contours`,
`remove_overlaps` (collinear lines / co-circular arcs merged, I), `merge_connected(items, tol, mode)`,
`close_gaps` (rebuild addition), `fit_arcs(points, FitParams, closed)` (the port above; an 'A' record becomes
an Arc whose sweep direction is the one whose middle lies on the fitted run, since CdlFitOut has no direction; the
parity harness gives the same L/A/I counts as the DLL on all 13 test polylines, geometry within 1e-13 mm), `smooth_contour(contour, accuracy)` (fits runs of ≥ 3 lines, keeps arcs; used instead of the
U spline smoother), `cleanup_contours` / `cleanup_document` (the `0x10061c90` order).

## Unknowns (summary)

- Remaining details of fly cut (§11.5), common edge, corners, fill, cleanup and the smoother: see the unknowns in §11–§15 and motion.md §9.
- Cutting order: see §8.8 (CRingSort and the mode-5 optimiser are now recovered).
- The exact cooling-point item sequence.
- The DXF join tolerance of Dxf2Grp.
- ManuItem fields +0x0c / +0x10, and the follower opcode payloads.
- Whether the UI kerf value is a full or half width.

## 16. UI (`mlaser/ui/ext_cam.py`)

`register(window)` rewires these existing ribbon commands: `tool.array`, `tool.scan_cut`/`scan_circle`/`scan_line`,
`tool.share_edge`/`_c`, `tool.optimize`, `opt.dedup`/`join`/`smooth`, `geom.smooth`, `craft.chamfer*`, `craft.unload`,
`craft.cool_auto`/`cool_clear` and `draw.linear_fill`. It also adds a "CAM+" tab for fill circle (mf100_1), α corner add/clear
(mf485/mf403), rectangle common edge (A250707_11), circular array, fill-sheet array and the full cleanup.
The dialogs edit parameter dataclasses kept on `window.cam_ext`, using the lang keys cited in §11–§15.
