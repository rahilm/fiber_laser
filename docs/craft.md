# Manual craft tools, scan engraving and bitmap import (agent G, phase 3)

**Code:** `mlaser/cam/manual.py` (geometry), `mlaser/cam/flycut.py` (scan engraving types 4/5,
added), `mlaser/ui/ext_craft.py` (`register(window)`: ribbon re-binding, dialogs and canvas click
tools), `mlaser/engrave/` (Bitmap.exe port, see [engrave.md](engrave.md)).
**Tests:** `tests/test_craft_manual.py`, `tests/test_craft_ui.py`, `tests/test_engrave_*.py`.
**Scratch:** `/home/dev/scratch/mlaser/work/craft/` (`ribbon_ids.py` key→command-id pairs from the
ribbon builder, `msgmap.py` MFC message map, `xref.py`, `peread.py`; `forkA/`, `forkB/` notes:
CADModule vtables `vft.txt`, GraphParam offset→key maps).

**Tags:** **O** observed (address; MA = MainApp.exe, otherwise CADModule.dll), **I** inferred,
**U** unknown, **R** rebuild choice. Licensing / dongle / cloud code is not involved here.

## 1. Command map (O)

Ribbon builder (MA ~0x4994b9..0x4a20a9) pushes the lang key then the command id; MFC message map
(static init around MA line 98900 of the dump) maps ids to handlers. `FUN_004d1780(&tool)` switches
the CAD view into a click tool.

| Rebuild cmd | Key | Id | MA handler | Action |
|---|---|---|---|---|
| craft.lead_manual | mf88 | 0x3459 | FUN_004b5520 | tool 5 |
| craft.lead_check | mf89 | 0x345a | FUN_004b5520 | CCAD vt+0x14c → 0x100d1f70 |
| craft.lead_clear_mark | mf91 | 0x345c | FUN_004b5520 | vt+0x150 → 0x100adfe0 |
| (craft.lead_clear mf90) | mf90 | 0x345b | FUN_004b5520 | vt+0x158 → 0x100c1420 |
| craft.start_point | mf93 | 0x346d | FUN_004b5a40 | tool 6 |
| craft.home_ref | mf92 | 0x346c | FUN_004b57c0 | dialog mf192, tool 7 / vt+0x198 → 0x100bc890 |
| craft.circle_cut | mf487 | 0x343a | FUN_004b4810 | split button "环切" of the α corner: dialog, vt+0xf4 → 0x100c2680 (0x343b mf403 clears) |
| craft.mj_manual | mf101 | 0x3462 | FUN_004b5d70 | tool 10 |
| craft.mj_explode | mf103_1 | 0x3466 | FUN_004b5d70 | vt+0x174 → 0x100bad60 |
| craft.gap / seal / overcut | mf95 / mf96 / mf97 | 0x3476 / 0x3477 / 0x3478 | FUN_004b5ad0 | vt+0x168 → 0x100c46a0(mode, value) |
| craft.cool_point | mf406 | 0x348a | FUN_004b5f50 | tool 15 (0x348b auto mf407, 0x348c clear mf408 are agent C's) |
| tool.bridge | mf114 | 0x34da | FUN_004b6350 | CGraphParaDlg mode 9 (FUN_005ee240(9)), tool 11 |
| tool.engrave | mf112_5 | 0x34f3 | FUN_004b6cf0 | dialog FUN_004310c0(0), scan type 4 |
| tool.onekey | mf196 | 0x4a42 | FUN_004be780 | see §8 |
| opt.segment | mf475 (= mf216) | 0x341c | FUN_004b4480 | tool 12 |
| opt.replace_circle | mf472 | 0x3447 | FUN_004b5240 | vt+0xf8 → 0x100b8e00 |
| opt.adjust_start | mf604 | 0x3448 | FUN_004b5240 | vt+0xe0 → 0x100c9b00(1) |
| file.bitmap | A250622_0 | 0x35b6 | FUN_004b2f90 | Bitmap.exe, then scan G-code import |
| file.import_scan_gcode | A251104_0 | 0x35b7 | FUN_004b3870 | scan G-code import |

Other tool numbers (O): 8 measure (0x3552), 9 manual sort, 13 manual fillet, 14 unload.

## 2. Click tools (O unless tagged)

- The CAD view at MA view+0x138 is the CCADModule object (vft 0x10122c4c): +0x54 set tool
  (FUN_100e81a0, stored at this+0x1f0), +0x58 get tool, +0x60 click FUN_100ecc30 → handler table
  this+0x118 + 4·tool; hover table this+0x1a8 + 4·tool.
- Pick tolerance this+0x270 = 8.0 (DAT_1011ff18) × scale ⇒ 8 px. Rebuild: `ToolHost.tolerance()`
  = 8 px / zoom.
- Contour hit test CGlyContour vt+0x28 (FUN_10062d20): kerf child first, bbox ± tol, then the
  **first** segment that passes (not the nearest), returning u = arc length / length.
  Rebuild (R): `manual.hit_test` picks the nearest contour within tolerance.
- Rebuild host: `ext_craft.ToolHost` installs a `PickTool` through agent F's
  `CanvasView.set_tool` (press/move/release/key/cancel in model coordinates, `set_preview` for the
  hover marker) and falls back to a viewport event filter when the slot is missing. Right click or
  Esc leaves the tool (MA FUN_004d0480: Esc cancels; whether tool state is reset is U).

### Tool 5, manual lead (FUN_100e9a40 → FUN_100be7d0, FUN_10064760)
Two snapped clicks. Click 1 stores Q (doc+0xa210, state doc+0xa20c = 1; hover draws a rubber line).
Click 2 must hit a closed type-8 contour, otherwise it becomes the new Q. Then (undo
COpGuideLineCmd): start = u (FUN_10071f30 → +0x180), and if |Q − S| ≥ 0.01 the lead-in becomes a
line from Q: type 1, length |S − Q|, angle |a|·57.2958 with a = signed angle from the start
tangent to S − Q (may exceed 90°), side flag +0x1f8 = (a ≤ 0); otherwise lead-in type 0. Lead-out
and radius untouched.
Rebuild: `manual.manual_lead(part, ci, q, dist, depth, invert)`. The rebuild toolpath takes the side
from depth/orientation (`leads.side_flag`), so the angle is stored signed for that side; the
generated lead line runs exactly from Q to S (tested).

### Tool 6, path start (FUN_100e9bd0 → FUN_100bea40)
One snapped click on a closed contour: start = u, start point / overcut / leads recomputed.
Rebuild: `manual.set_start_at` → `Craft.extra['start_param']` (fraction).

### Tool 7 and dialog mf192, Home Ref (dock point) (MA FUN_004b57c0, FUN_005e0cd0, FUN_004d2750)
Dialog: radio 20000 = manual (label U; rebuild uses mp207 "Manual"), radios 20001..20009 = 3×3 grid
ap2..ap10 (top-left, top, top-right, left, centre, right, bottom-left, bottom, bottom-right);
SoftParam 0x497c. Anchor: x = x0 / centre / x1 for mode%3 = 1/2/0, y = top for modes < 4, centre
for 4..6, bottom for ≥ 7 (rotated sheet corners when an edge-seek angle is active). MainApp then
reads the head position P, calls vt+0x198 (0x100bc890, undo COpDockPtCmd) with d = anchor − P,
which translates every graph by −d when |d| > 0.01, and stores P at cfg 0x4980 with a marker.
So Home Ref moves the drawing so the chosen anchor lies under the head (O). Manual mode (tool 7)
only stores and marks the snapped point (I).
Rebuild: grid modes translate the document so the anchor lands on the controller head position
(`window.ctl.state().x/y`; DemoController → 0,0), undoable; the dock point is kept in
`window.settings['SP.DockPoint']` and `doc.extra['dock_point']`; manual mode stores the clicked
point. U: the job-start re-dock (MA FUN_0045cc20, platform positions cfg 0x42c0) and the NC park
options pd407..409 / pd78 / mp72 (agent H's area).

### Tool 10, manual micro-joint (FUN_100ea0a0 → FUN_100d04b0 → FUN_10071940)
Length L = GRP.MicroLinkLength (GraphParam 0xb238, pd325; pd346 is registered on the same offset,
so manual and auto share it). L < 0.01 → nothing. A click inside an existing interval removes that
joint; otherwise the joint is rejected when (u − c_{k−1})·Len ≤ (L_{k−1} + L)·0.5 or
(c_k − u)·Len ≤ (L_k + L)·0.5, else inserted sorted; intervals rebuilt (FUN_1006c930). Undo
COpMicoLinkContourCmd. Rebuild: `manual.toggle_joint` (R: also checks the wrap between the last and
first joint, which the original does not). The rebuild keeps its own `ManualJointParams.length`
(dialog mf102 / pd325) separate from `window.mj_params['length']`.

### Tool 11, bridge (FUN_100ea190 → FUN_100b7370 → CBridge FUN_1001bc60)
Width W = GRP.BridgeWidth (0xb260, pd347). Two clicks on non-scan graphs (A, B), ordered by graph
index. Cut lines (FUN_1001a7f0), u = unit(B − A), n = (−u_y, u_x), h = W/2:
L1 = A + h(n − 2u) → B + h(n + 2u), L2 = A + h(−n − 2u) → B + h(−n + 2u).
- Two contours: each contour is cut by L1 and L2 (parameters p1, p2); d = (p2 − p1) mod 1 must be
  ≥ 0.6 or ≤ 0.4, the longer portion is kept, and piece 1 + line + piece 2 + line form one new
  closed contour (FUN_10074e00). Undo COpBridgeCmd; tree rebuilt.
- Same contour (FUN_1001af80): four intersections; the strip between the lines is removed, giving
  two closed contours each closed by one bridge edge (I).
- Groups resolve to the member containing the point (O); U: FUN_100b2bc0, the exact intersection
  choice in FUN_1001a1e0 (rebuild: the crossing nearest to the click).
Rebuild: `manual.bridge_lines`, `bridge_two`, `bridge_same`; the merged contour replaces the first
contour, the second is removed (its part too when empty).

### Tool 12, curve segmentation / split (FUN_100ea210 → FUN_100b6bb0)
Snapped click on a type-8 contour; margin m = max(3·tol, 0.2)/Len capped at 0.3; for m < u < 1 − m
the contour is replaced by [0, u] and [u, 1] (FUN_100752c0, layer copied; undo
COpSplitContourCmd). A closed contour is therefore cut at its first-segment start and at the click
(I). Rebuild: `manual.split_contour_at(c, dist, tol)`, `split_margin`. The same id 0x341c serves
mf216 (geom.split, agent F's command).

### Tool 15, manual cooling point (FUN_100ea550 → FUN_100c94d0 → FUN_10069fd0, CCoolPoint)
Per segment in order: start inside the 2·tol box → snap to start; else end → snap to end; else the
point on the segment. Every existing point with |p − u|·Len < max(tol, 0.5) is removed and u is
inserted (so clicking a point moves it); undo COpCoolPointCmd only if the count changed (O/I).
Rebuild: `manual.snap_to_vertex`, `manual.place_cool_point` → `Craft.extra['cool_points']` (mm).

## 3. Lead check / clear mark (O)

- **Check (mf89, 0x100d1f70 → 0x10020e50):** all graphs, ≥ 2 needed; 30×30 grid index over the
  union bbox + 0.5. Uses the kerf child; the lead-in polyline (+0x278) with its first point pushed
  back by 0.5·lead length; crossings with the own contour, then the other contours (bbox prefilter,
  0x100205b0; distance × 0.7 when the lead type is not a line). d = min distance (initial 10000).
  On a hit: lead-in length +0x1c8 = d, error flag +0x1f9 = 1, lead rebuilt, undo record. No message.
  Drawn red (0.99, 0, 0) instead of grey when +0x1f9 is set (0x100659e0).
- **Clear mark (mf91, 0x100adfe0):** +0x1f9 = 0 on every contour, no undo, lengths stay.
- Rebuild: `manual.check_leads(doc)` / `clear_lead_marks(doc)`; mark = `Craft.extra['lead_error']`.
  The distance is measured along the lead from the start point (I); a crossing on the 0.5·L
  extension shortens to d/1.5 (I). No kerf child is used (R). Marked parts are selected so they
  show highlighted; drawing marked leads red needs a canvas change (§9).

## 4. Gap / seal / overcut (O, 0x100c46a0)

v = mode·value, mode = GraphParam 0xb230 (−1 gap, 0 seal, +1 overcut), value =
GRP.LoopGapOverCutLength 0xb228, registered twice (pd323, pd324) on the same offset, so both
dialogs edit one value (default hint 2 mm). Seal has no dialog and clears. Selected graphs only
(groups expanded); a contour changes only when |v − current| > 0.02 and |v| < 0.33·length (setter
0x10070e80; the open-contour / lead-out rules are applied at output, cam.md §4). Undo
COpOverCutContourCmd. Rebuild: `ext_craft.CraftExtension._gap` + `manual.set_gap_seal` →
`Craft.extra['overcut']`.

## 5. Micro-joint explode (O, 0x100bad60 → 0x10075640)

Selected graphs, scans skipped; needs ≥ 2 switch fractions (+0x13c); s = [0, switches…, 1], every
beam-on piece ≥ 0.05 mm becomes a new open, selected contour (sub-path 0x100752c0, wraps on closed
contours) in place of the original; new contours have default craft (geometry and layer only, I).
Undo COpExplodeMicoLinkCmd. The first and last pieces of a closed contour are not merged (I); the
rebuild's pieces run between consecutive joints, so the piece across the start is one contour (R).
Rebuild: `manual.explode_joints`.

## 6. Scan engraving and scan G-code (O unless tagged)

- **Dialog FUN_004310c0 / OnInit FUN_00435ff0**, mode 0 (mf112_5, type 4): scanSideLineLength
  pd719_3 (0xb3a8, 5 mm), scanDirection pd719_4 (0xb3b0, 0); minHorizontalLineGap (0xb398) and
  minScanLineLength (0xb3a0) forced to 0.01. Mode 1 is the linear fill (A250707_10, agent C). Mode
  2 (G-code): GCodeScanSideLineLength (0xb340, 5), GCodeScanDirection (0xb348, 0),
  GCodeAutoScanEngrave (0xb33a) forced to 1. Rebuild: `flycut.EngraveScanParams`.
- **Type 4 (0x34f3):** worker FUN_100c80d0 with start corner 0 (LD) for direction 0, else 2 (LU);
  ≥ 2 selected contours on one layer; builder 0x100a9d80 with engraving parity (path starts beam
  off). Rebuild: `flycut.apply_engrave_scan` / `engrave_scan`: collinear rows (tol 0.01), rows by
  offset from the start side, serpentine, each row extended by the side-line length (beam off),
  gaps and linkers (0x100a3f10 `_linker`) beam off (I: the type-4 row extension is taken from the
  type-5 loader).
- **Scan G-code parse** (CGCodeFileParse 0x1000bfd0 with the scan flag): G0 or G1 with S ≤ 0 is a
  move, G1 with S > 0 burns (S only tested for > 0); burns on the same Y join a row; rows ordered by
  0x1000a730 (direction 0: bottom-up, left→right; else code 3: top-down, right→left); each row
  becomes a line contour x_first − L .. x_last + L carrying its burn spans; the type-5 builder
  0x100a6580 forces a serpentine. Rebuild: `flycut.parse_scan_gcode`, `gcode_scan_part` (a type-5
  scan part whose sources are the extended row lines).
- **Import Scan G-Code (FUN_004b3870):** multi-select dialog (filter A251104_1), new document,
  per file ≤ 100 MB with extension g/nc/cnc: mode-2 dialog, load (FUN_004d4f60 → vt+0x288
  0x100efd00), then the type-5 scan. A value from FUN_005d5b90 clamped to 1..10000 is unused (U).
  The busy flag cfg+0x43d0 makes canvas clicks ignored during the import.
- **Bitmap (FUN_004b2f90):** CreateProcess `Bitmap\Bitmap.exe`, make it topmost, disable the main
  window until it exits, check `Bitmap\status.ini` (FUN_004ae380; the companion writes `0` at
  start and `1` after Generate), then the same flow as the G-code import with `Bitmap\newGcode.nc`.
  `newSVG.svg` is referenced only by MainApp (U). Rebuild (R): the `mlaser.engrave.BitmapDialog`
  runs in-process with `out_dir` = `<tmp>/mlaser_bitmap`, then `import_bitmap_result()` checks
  status.ini = 1 and imports `newGcode.nc` as a type-5 scan (one row contour per raster row, so a
  picture does not become thousands of parts as it would through `io.gcode.parse_gcode`).

## 7. Optimize sub-commands (O)

- **Replace circle (mf472, 0x100b8e00):** all graphs. Circle test 0x1009a4f0: closed, bbox
  |w − h| ≤ 0.05, centre = bbox centre, r = (w + h)/4, points with |d² − r²| ≤ 0.2·r, arcs with radius
  and centre within 0.1 (splines and regular-polygon edge checks, I). Replaced by a CEditableCircle
  with default craft (I), selected; undo COpSmoothContourCmd. Rebuild: `manual.circle_of`,
  `replace_circles` (tests start/end/mid point of every segment; a single full-circle arc is left
  alone; the replacement keeps the contour orientation).
- **Adjust start (mf604, 0x100c9b00(1) → CUniformStartPos 0x10051070, corner 1):** selected closed
  contours with ≥ 2 glyphs; the glyph start nearest (Manhattan) to the bbox lower-left + offset is
  rotated to the front; the offset is (0, 0) on the first contour, then (chosen vertex − corner) of
  the first contour. The start parameter is not touched. Rebuild: `manual.adjust_starts`.
- Point → circle (mf605, 0x100c6090) is not one of these commands; not decoded.

## 8. OneKey planning (mf196, id 0x4a42, MA FUN_004be780) (O)

In v0.0.1.51 the handler tests GRP.OneKeySort (0xb34f, pd479), GRP.OneKeyGuideLine (0xb350, pd480)
and GRP.OneKeyMicroLink (0xb351, pd481), but each step calls FUN_00428430, an empty function, and
then shows mf209 "OneKey planning done". pd476..478 (mirror H/V, optimize) exist only as lang
labels. Rebuild (R): dialog mf197 with all six steps (pd476..481) that runs mirror H, mirror V,
cleanup (agent C's `cleanup_document`), lead (window lead params), micro-joint (window auto
micro-joint params) and sort (nearest), in the pd476..481 order except that the sort runs last,
then logs mf209.

## 9. Requests for lead-owned files

1. `mlaser/cam/toolpath.py`, per-contour craft (the original keeps craft per contour; the rebuild
   stores per-contour overrides from the manual tools in `Contour.extra['craft']` for parts with
   several contours). In `build_toolpath`, replace

   ```python
           pc = prepare_contour(n.contour, doc.parts[pi].craft, n.depth, prm)
   ```
   with
   ```python
           ov = n.contour.extra.get("craft") if isinstance(n.contour.extra, dict) else None
           craft = ov if isinstance(ov, Craft) else doc.parts[pi].craft
           pc = prepare_contour(n.contour, craft, n.depth, prm)
   ```
   and `n.layer = doc.parts[pi].craft.layer` with `n.layer = craft.layer`. Until then manual
   edits on multi-contour parts (e.g. a plate with holes) are stored but not cut.
2. `mlaser/ui/canvas.py` (agent F / lead): draw the lead of a contour whose craft has
   `extra['lead_error']` in red (original 0x100659e0), and mark `doc.extra['dock_point']`.

## 10. Unknowns

- Esc / right-click handling inside the CAD tools; FUN_100b2bc0 in the bridge; FUN_1001a1e0
  intersection rules; the dock translation's interaction with platforms; the radio-20000 label.
- Lead check: which point the distance is measured from; whether the 0.5·L extension can lengthen.
- Point → circle (mf605); type 3 concentric-arc scans; how `newSVG.svg` is produced.
