# Nesting (排样, Nest tab `mf460`); `mlaser.nest`, `mlaser/ui/ext_nest.py`

Tags: **O** = observed (address in the decomp dump or file offset), **I** = inferred, **U** = unknown,
**R** = rebuild design (not taken from the original).

## 1. What the original uses

### 1.1 Two third-party DLLs (O)

**SmartNest.dll** is a licensed build of **Geometric Ltd's "NestLib" SDK**, a commercial true-shape nester.
- Evidence: `NESTLIB_MODE` compared with `"Geometric"` (SmartNest.c line ~26579, string 0x100c7f00);
  `Nestlib Development Version` (0x100d9a4c); `/mna/usr/home/nestlib/tbom`; `nestlib.err`; `c:\tmp\napidump.log`.
- It logs every API call with a printf signature (strings from 0x100c4a0c). The signatures below come from
  those strings.
- It has no VERSIONINFO and no PDB path.
- Option keywords: `STEP_ANGLE FIT_TYPE IF_MIRROR ANGLE_LIST ROTATION_ANGLE_METHOD(BY_ANGLE_LIST) PRIORITY
  QUANTITY CLEARANCE RESOLUTION COLLAR MIN_COMMON_EDGE_LEN COMMON_EDGE_PROXIMITY
  NEST_FILLERS_UPTO_NESTED_HT PREF_HOLE_FILL`.
- Fit types: "CG fit" and "Grid Fit" (0x100c4ddc).
- Modules: common edge, common punch, multi-torch, guillotine, clusters, part-in-part.
- **Its algorithm is proprietary and was not studied or copied.** The rebuild only mirrors its inputs and
  outputs.

**AutoNest.dll** ("HePin DLL", HePin.DLL ©2014) is a wrapper around it.
- PDB: `D:\Code\奥森迪克DLL\HePin\Release\AutoNest.pdb`.
- Dialog credit: 武汉智格科技有限公司 / SmartNest智格套料软件 (www.smartnest.com.cn).
- It loads `smartnest.dll` dynamically (FUN_100534ab, function table 0x100897a8).
- It contains its own DXF writer (`nest-%d.dxf`), NC writer and temporary files
  (`nest_output_sheet.%d`, `sheet_used.txt`, …).
- **Intentionally omitted:** its licence and dongle path, i.e. `Nest_CheckLock/GetId/SetCalNew/EncodeNew/EncodeByKey`,
  the HID dongle `Vid_3689&Pid_8762`, the "user authorization code" dialog and the tier quantity caps at
  0x1004ca74 (50 part types × 200 copies, or 100 × 5000).

### 1.2 NestLib API (O; names from the exports, signatures from the log strings)

| Group | Calls |
|---|---|
| Session | `NOpenNestLib(dir, int, int)`, `NCloseNestLib`, `NInqGetBuildInfo`, `NInqMessage`, `NInqLogFileName` |
| Callbacks | `NStartSettingCallBacks` … ErrorHandler, ProgressFunction, NewPartGeomHandle, NewSheetGeomHandle, NestedSheetResult, NestedOrderResult … `NEndSettingCallBacks` |
| Parameters | `NStartSetParams`; `NSetResolution(d)`, `NSetClearance(d)`, `NSetCutterDiameter`, `NSetPartDistType`, `NSetPartSeqMethod`, `NSetFitMaxOnLast`, `NSetFitPartInPart(int,int)`, `NSetConnectingDistance(d)`, `NSetEnableTurboMode`, `NSetApplyBestNestingDirection`, `NSetGuillotineCut*`, `NSetNestFillersUptoNestedHeight`, `NSetSheetApplyMultipleCorners`; `NEndSetParam`. Logged but not exported: `NSetMinComnEdgeLength`, `NSetComnEdgeProximity`, `NSetProxAngle` |
| Parts | `NStartNewPartList`; `NAddPartInfo(name, minQty, maxQty, int, d, d, stepAngle, char mirror, int, int, int, int)`; `NDefineAngleList(n, double*)`; `NSetOffDistLeadins`; groups `NStartGroup/NAddGroupMember/NEndGroup`; `NChgPartMinQty/MaxQty/Priority`, `NChgPartTypeOfFit`; `NEndNewPartList` |
| Geometry | `NDwgGeomStart` → `NDwgProfsStart` / `NDwgClosedProfStart` / `NDwgLeadinsStart` → `NDwgAddLine(x1,y1,x2,y2)`, `NDwgAddArc(5 d)`, `NDwgAddCircle(cx,cy,r)` → `…End`; read back with `NDwgStartGetGeom`/`NDwgGetGeomProfile*` |
| Sheets / run | `NStartNestOrder`; `NAddSheetInfo(name, qty, int, isRect, w, h, …)` (23 words are passed against 18 logged fields, so the exact mapping is U); `NNestOrder`; `NSetStopNesting`; `NPlaceMyPart`; `NCommitNestedOrientation` |
| Results | `NInqSheetTotalNested`, `NInqNestedSheet` (12 outputs), `NInqNestedPartType`, `NInqNestedPartOrientation`, `NInqNestedInstance`, `NInqPartNested(Quantity)`, `NInqPartNotNested`, `NInqNestedParameters` |
| Utilities | profile (`NCreateProfile`, `NValidateProfile`, `NFindBoundaryProfile`, …), offset (`NOffset*`), part (`NPartCheckPartInPart`, `NPartCheckMinimumDistance`, `NPartGetPartAreas`) |

### 1.3 AutoNest wrapper (O, AutoNest.c)

- **`Nest_SetNestPara(idx, double)`** (0x1004c840):

  | idx | Meaning |
  |---|---|
  | 0 | margin |
  | 1 | spacing |
  | 2 | rotation step, quantised: ≤ 90 → "90", ≤ 180 → "180", ≤ 270 → "270", else "360" |
  | 3 | direction: 1 horizontal (L→R), 2 vertical, 3 "自动(根据排样效果)" auto |
  | 4 | accuracy (resolution; also the minimum sheet edge length, error 0xf) |
  | 5 | part-in-part |
  | 6 | share edge |
  | 7 | minimum common-edge length |
  | 8 | polyline rebuild flag (I) |

- **`Nest_SetPartPara(name, value, which)`** (0x1004ca13): three per-name maps. Map 0 = own 360° rotation,
  map 1 → `NAddPartInfo` arg 12 (priority, I), map 2 → arg 15 (fit type / part-in-part, I).
  CADModule passes (1,0), (0,1), (1,2) (CADModule.c ~183430).
- **`Nest_WritePartData(name, qty, nContours, {nPts, xs*, ys*}…)`** (0x1004ca74): closed polylines only.
  Contour 0 is the outer and must be closed within 0.1.
  Errors: 0x10 no contours, 0xd open outer, 0xe duplicate vertex < 1e-6, 0xf segment < 0.1, 0x11 too many types.
- **`Nest_WriteSheetData(…, kind, nContours, …)`** (0x1004d0b2): kind ≠ 0 is a rectangle (W, H).
  kind = 0 is a shaped sheet whose contour 0 is the boundary and whose other contours are defects
  (flag +0x58), named "钢板虚拟零件".
- **`Nest_AutoNest`** (FUN_10007526):
  - If margin < spacing, the sheet is grown by 2·(spacing − margin), because NestLib applies the clearance
    at the sheet edges too.
  - Share edge forces spacing to 0.
  - Call sequence: `NSetClearance(spacing)`, `NSetConnectingDistance(0.1)`, `NSetResolution(accuracy)`,
    `NSetFitPartInPart(0,0 | 1,1)`, `NSetApplyBestNestingDirection(1)` for auto direction.
  - Each part (≤ 1000): `NAddPartInfo(name, qty, qty, 0, 0, 0, step, 'N', map1, 1, 0, map2)`.
    **min = max = qty and mirror is always 'N'.**
  - `NAddSheetInfo("矩形板材"|name, 10000, 1, isRect, W, H, …, dir 'X'|'Y', 'N', 1, max(0, margin − spacing), 0, 0)`.
    The sheet count of **10000** means unlimited copies.
  - Then `NNestOrder`. Progress goes through `Nest_RegProgressCB`.
- **Results:**
  - `Nest_ReadNestNum` gives the number of distinct layouts (CADModule rejects 0 or > 20000).
  - `Nest_ReadPartNumbyNest(i)` gives the placements in layout i.
  - `Nest_ReadNestResult(out, i)` (0x1004d690) fills out[2] = number of identical sheets, out[3] = nParts,
    out[4..5] = a double (I: utilisation) and out[6] = an array of 0x30-byte placements:
    +0 part id, +0x18 x, +0x20 y, +0x28 angle (+8/+0x10 U).
  - `Nest_ReadOddData` / `Nest_GetOddCountByNest` return remnant polylines, which CADModule does not use.
- **Common line (I):** with share edge on, part edges at 0°/180° that are longer than the minimum common-edge
  length are flagged (AutoNest.c ~8229). The common-edge cutting itself is CAM (CShareEdge, docs/re/cam.md §12).

### 1.4 CADModule / MainApp side (O)

- **MainApp (~MainApp.c 122790)** passes the dialog values to CCADModule vtable +0x308: EdgeStock, PartSpace,
  StepAngle, NestAccuracy, NestDirection + 1, IsNestPartInner, EnableShareEdge, ShareEdgeMinLen and
  OnlyNestSelectedPart.
- **Nest object (doc+0x9a8):**

  | Offset | Field |
  |---|---|
  | +0x48 | margin |
  | +0x50 | spacing |
  | +0x58 | step |
  | +0x60 | direction (1 L→R, 2 B→T) |
  | +0x68 | accuracy |
  | +0x70 | part-in-part (only if every part allows it, part +0x40) |
  | +0x71 | share edge |
  | +0x78 | minimum share length |
  | +0x80 | selected only |

  Collections: parts vector +8, sheets +0x28, results +0x88.
- **Run (FUN_100e0c90):**
  1. Pre-check that bbox + 2.01·margin fits the sheet; when step < 91° the swapped orientation is also allowed.
  2. Set the parameters.
  3. Per part, FUN_100e0320 flattens it (FUN_100de8f0), writes the contours in reverse order with
     name = part id and qty = required − already nested.
  4. Write sheet[0] (FUN_100dc7a0).
  5. Call `AutoNest(0)`, then read the layouts.
- **Results:** each layout becomes a CNestResult (0x58 bytes). FUN_100dc420 rotates every graph by the
  angle if |angle| > 0.001 and then translates it by (x, y); the rotation centre is U.
  A layout with multiplicity m is duplicated m − 1 times and the parts' nested counts are increased.
- **Only sheet[0]** (the first sheet) is passed to the engine in the observed run (I: selection works by
  reordering, `pd_Nest_SheetSlect`).
- **NestParam (ManuPara.xml) defaults:**

  | Key | Default | Note |
  |---|---|---|
  | NP.SheetWidth | 1500 | |
  | NP.SheetHeight | 3000 | |
  | NP.EdgeStock | 5 | |
  | NP.PartSpace | 3 | |
  | NP.NestDirection | 0 | L2R / D2U |
  | NP.PartRoateStepAngle_deg | 90 | |
  | NP.IsNestPartInner | 0 | |
  | NP.EnableShareEdge | 0 | |
  | NP.ShareEdgeMinLen | 20 | |
  | NP.NestAccuracy | 0.5 | 0.1–1.0 |
  | NP.OnlyNestSelectedPart | 0 | |

  GRP.AddPartNum (pd766) is the import quantity. The UI exposes no per-part priority, mirror or min/max
  quantity.

### 1.5 UI strings (O, Lang/lang.txt)

- **Ribbon:** mf460 Nest, mf461 Import Part, mf462/mf473 Import Sheet / Std Sheet, mf463 Simple Nest,
  mf464 Advanced Nest, mf465 Add Part, mf466 Add Sheet, mf412 Nest Parameters, mf420 Export to Sheets,
  `mf_S_File_NewProject/OpenProject/SaveProject/SaveAs`.
- **Nest views:** np_Part, np_PartName, np_PartNum, np_NestPartCount, np_Sheet, np_SheetName (残板名称,
  remnant), np_SheetNum, np_StdSheet, np_Result, np_Reslut_, np_FileSaveDone.
- **Copy to the main view:** nset_UI_Part2MainView, nset_UI_Sheet2MainView, nset_UI_Result2MainView.
- **Parameter labels:** pd766..769, pd_Nest_EdgeStock, PartSpace, NestDir(_L2R/_D2U),
  PartRotateStepAngle, NestPartInner, ShareEdge, ShareEdgeMinLen, NestAccuracy, OnlyNestSelectedPart,
  PartSlect, SheetSlect, StdSheet_W/H.
- **File dialogs and messages:** FileTitle_OpenNestProjFile / SaveNestProjFile / ExportNestFile,
  MsgBox_NestDataChange_IsSave, MsgBox_Nest_InvalieFileSuffix.

## 2. Rebuild: `mlaser.nest`

### 2.1 Data model (`nest/model.py`)

- **`NestPart`** has:
  - `parts`: a list of `model.Part`;
  - `name` and `quantity`, which is the required count, i.e. NestLib minQty;
  - `max_quantity`: fill up to this count if space remains (maxQty);
  - `priority`: higher is placed first;
  - `angles`: an explicit angle list, as in `NDefineAngleList`; otherwise the parameter step is used;
  - `mirror`: whether mirrored copies are allowed;
  - `fit`: `FIT_SHAPE` (true shape) or `FIT_RECT` (bounding rectangle).
- **`NestSheet`** has:
  - `width`, `height`, `origin` and `quantity`;
  - `outline`: parts that form a shaped or remnant sheet; inner contours are defects;
  - `is_remnant`, and an optional per-sheet `margin`.
- **`NestParams`** defaults to the NP.* defaults: `spacing` 3, `margin` 5, `rotation_step` 90,
  `part_in_part` False, `common_line` False, `share_edge_min_len` 20 and `accuracy` 0.5.
  - `accuracy` is used as the curve flattening tolerance ×0.1 (R).
  - `gravity` is `left` (L2R), `bottom` (D2U) or `auto` (AutoNest direction 3: both are tried and the better
    is kept).
  - Rebuild additions (R): `kerf` (spacing used with common line), `resolution` (raster cell),
    `time_limit_s`, `sort_by`, `compact` and `fill_sheets`.
- **Results:**
  - `Placement(part_id, x, y, angle, mirror)` means: mirror x → −x, rotate CCW by `angle` degrees about the
    part's local origin, then translate.
  - `SheetLayout` holds the sheet id, placements, utilisation, used extent and remnant rectangle.
  - `NestResult` holds the layouts and the `placed` / `unplaced` counts.
  - `NestJob` holds parts, sheets, parameters and the result.

### 2.2 Engine (`nest/engine.py`, R)

The engine is a true-shape **raster bottom-left fill** with FFT collision search. It is not NestLib's algorithm.

1. **Shapes.** Each part becomes a shapely (Multi)Polygon with holes. Contours are composed even-odd, as in
   the CContourTopTree depths, and open contours become a 0.05 mm strip. Every allowed orientation
   (angle list × mirror) is rasterised *conservatively*: "body" cells are those whose centre lies within
   half a cell diagonal of the part, and "fit" cells are those within spacing + half a diagonal. Holes stay
   free, which is how part-in-part works. When part-in-part is off, the occupancy uses the filled outline.
2. **Sheets.** Cells not fully inside the usable region are blocked. The usable region is the sheet or
   remnant outline shrunk by the margin, so defects are grown by the margin. The static feasibility of each
   orientation against the region is one FFT cross-correlation per sheet.
3. **Placement.** Each step does one FFT of the occupancy grid plus one inverse FFT per orientation. The
   feasible offset nearest the gravity corner wins; across orientations the smallest far edge wins. Disjoint
   conservative masks guarantee the real outlines keep `spacing` and stay inside the region.
4. **Compaction.** The placed outline then slides toward the corner with exact shapely distance checks
   (step halving down to res/32, or 1e-3 with common line). The exact outline is rasterised back into the
   occupancy grid.
5. **Order and stopping.**
   - Parts go by priority, then area. Required copies are placed over the sheets in list order.
   - A sheet kind on which nothing fits is skipped, so `quantity = 10000` behaves as "unlimited", as in
     AutoNest.
   - When every required copy is placed, the last sheet is filled up to `max_quantity`.
   - The time limit and progress callback (`progress(fraction, msg) -> False` to cancel) are honoured.
6. **Resolution** defaults to sqrt(largest sheet area / 160000), at least 0.25 mm. Measured (numpy FFT,
   single-threaded): 100 mixed parts (L shapes and rings, 4 rotations) on 2500×1250 take about 1.1 s, and
   300 parts on 3000×1500 take about 2.2 s. The test budget is 10 s for 100 parts.
7. **Common line.** Spacing becomes `kerf` (default 0) and parts are compacted until they touch.
   `apply_result` then runs `mlaser.cam.commonedge.share_edge` (agent C's CShareEdge port) with
   `share_edge_min_len`, so shared edges are cut once.

### 2.3 Result application and statistics (`nest/apply.py`)

- `apply_result(job, layout_index=None, include_sheet=False, gap=50, share_edges=None)` returns a Document
  of transformed part copies, i.e. `nset_UI_Result2MainView`.
  - Each copy carries `Part.extra['nest'] = {part_id, sheet_id, index, x, y, angle, mirror}`.
  - Mirroring flips arc direction.
  - With no index, all layouts are laid side by side.
  - `include_sheet` adds the sheet outline on layer 0 (back layer, not cut).
- `stats(job)` reports per sheet: part area (net, holes excluded), sheet area, utilisation, utilisation of
  the used part (sheet minus remnant) and the remnant rectangle (the free strip beyond the used extent in
  the gravity direction).
- `job_from_document(doc)` makes one nest part per document Part.

### 2.4 Project file `*.nestproj` (`nest/project.py`, O layout)

CADModule writer FUN_100da580 / reader FUN_100df500, saved by FUN_100ebef0 in the CSerialEng "scFlie" text
container. Graph bodies reuse the .chf serialiser (type + body).

```
scFlie / 9 / " "
<Begin Parts> nParts { Part_i, id, qty, nested, name, "Part Graphs", nGraphs, {type, body} } <End Parts>
<Begin Sheets> stdW, stdH, nSheets(incl. sheet 0)
  { Sheet_i (i ≥ 2), id, qty, used, name, isRect, W, H, "Sheet Graphs", nGraphs, graphs } <End Sheets>
<Begin NestResults> nResults
  { Result_i, sheetId, name, nPlacements, {partId, angle, "x,y"}, "Result Graphs", nGraphs, graphs }
<End NestResults>
eof
```

- **Rebuild mapping:**
  - The first rectangular non-remnant sheet is sheet 0. Its quantity is not stored and loads as 10000.
  - Remnant outlines go into "Sheet Graphs".
  - Result graphs are the transformed part copies.
  - Since the original's rotation centre is U, the loader recomputes each placement's (x, y) from the stored
    graphs when their count matches.
  - Rebuild-only fields (priority, max quantity, angle list, mirror and the parameters) are not in the format
    and are not saved. The parameters live in ManuPara.xml NestParam in the original.
  - Save → load → save is byte-identical (tested).
- **U:** the exact semantics of the sheet qty/used fields, result +0x48 (I: utilisation, not saved), and the
  formats of "Export nest result" and "Export to sheets". The rebuild exports one .chf per layout,
  `Result_<n>.chf`.

### 2.5 UI (`mlaser/ui/ext_nest.py`, `register(window)`)

- **Ribbon commands:** wires the existing `nest.*` commands (new/save/save as, import part mf461, import
  sheet mf473, run mf464, add part mf465, to main `nset_UI_Result2MainView`). It adds `nest.open`,
  `nest.add_sheet` (mf466; selection becomes a remnant sheet), `nest.params` (mf412), `nest.simple` (mf463),
  `nest.export` (mf420) and `nest.show`.
- **Nest dock:** editable part table (name, quantity, max, priority, mirror), sheet table and result table
  (part count, utilisation %).
- **Dialogs:** `NestParamsDialog` (pd_Nest_* keys) and `SheetDialog` (pd767..769).
- **Run:** cancellable `QProgressDialog`. To-main is undoable through the window's undo stack.
- **Sort commands:**
  - mf202 → 6, mf203 → 7, mf105/mf110 → 5;
  - "Shortest path" → 20, "Grid" → 21, "Keep current order" → 0;
  - mf610/611 → stable layer partition + mode 0;
  - manual sort mf111 (click order through the canvas selection, mv13 previous step, mv23 end,
    mv16/mv15 first/last);
  - a checkable "Avoid cut holes (rapids)" that sets `CamParams.avoid_cut` (needs the toolpath change below).

## 3. Unknowns

- NestLib internals (proprietary, not studied).
- The exact `NAddSheetInfo` argument mapping.
- The meaning of `NAddPartInfo` args 4–6 and 10–12.
- The rotation centre of the original's result transform.
- Placement +8/+0x10.
- Result out[0].
- The "Export nest result" file format.
- How a non-first sheet is selected for nesting.
