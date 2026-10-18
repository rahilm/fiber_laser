# Drawing and CAD editing (phase 3, agent F)

Code: `mlaser/cad/` (geometry, Qt-free apart from `text.py`), `mlaser/ui/ext_draw.py`
(commands, dialogs, canvas driver) and the interaction-tool hook in `mlaser/ui/canvas.py`.
Tests: `tests/test_draw_geometry.py`, `tests/test_draw_tools.py`.

Tags: **O** = observed in the decomp or the binary (address given), **I** = inferred,
**U** = unknown. MainApp addresses are `0x004xxxxx`, CADModule addresses are `0x10xxxxxx`.
Scratch evidence (asm dumps, scripts): `/home/dev/scratch/mlaser/work/draw/`.

## 1. Command routing (O)

The ribbon builder for the Draw / Process tabs (`FUN_004a2290`, not decompiled; read from the
disassembly with `work/draw/ribmap.py`) creates the buttons with these command ids. The
message map is built at run time by `FUN_00479570` (`work/draw/msgmap_rt.txt`).

| Command | id | MainApp handler | CADModule entry (ICADModule vtable 0x10122c4c) |
|---|---|---|---|
| draw.point .. draw.text | 0x334a..0x3357 | `FUN_004b2030` -> view `FUN_004d1230(id-0x334a)` | +0x84 `0x100ef350` (creates the tool) |
| draw.trim / draw.extend | 0x348f / 0x3490 | `FUN_004b6130` -> `FUN_004d1780` view mode 16 / 17 | click handlers `0x100ea620` -> `0x100bebb0`, `0x100ea6b0` -> `0x100beff0` |
| draw.advert_text | 0x3440 | `FUN_004b4ac0` (worker mode 1 in `FUN_00480520`) | +0x100 `0x100f2df0` -> CFillCircle `0x100ce2d0` |
| draw.adv_text | 0x34ed | `FUN_004b66f0`: CGraphParaDlg section 0x21 | +0x17c `0x100f31f0` -> `0x100c3170` |
| draw.qr | 0x3502 | `FUN_004b67d0`: CQRCodeCreateDlg | via scan G-code import (worker mode 2) |
| draw.fine_tune | panel mf445 | edit 30000 (mf447), check (mf446); key handler `FUN_004d0930` | +0x128 translate |
| geom.merge | 0x3412 | `FUN_004b2dc0`: CGraphParaDlg, GRP.ManualConnectGate | +0xe4 `0x100f2d10` -> `0x100bf7e0` |
| geom.explode | 0x3413 | `FUN_004b2d70` | +0xe8 `0x100f2d30` -> `0x100b7ce0` |
| geom.split | 0x341c | `FUN_004b4480` -> view mode 12 | click `0x100ea210` -> `0x100546e0`, `0x100b6bb0` |
| xform.size | 0x3390 (presets 0x3391..0x339a) | `FUN_004b20a0`, dialog `FUN_005f6c10` | +0x12c `0x100f2f00` -> `0x100b9e50` |
| xform.align | 0x33c2 (0x33c3..0x33c9) | `FUN_004b28c0` | +0x138 `0x100f2f70` -> `0x100c2a80` |
| select.similar / inner / outer | 0x331d / 0x331e / 0x331f | `FUN_004b1e80` (mode 7, (9,1), (9,2)) | +0xbc `0x100f2c20` -> `0x100b59d0` |
| tool.group / select_all / explode / explode_all | 0x33fe / 0x33ff / 0x3400 / 0x3401 | `FUN_004b2bf0` | +0x164 `0x100c4020`; +0xbc(5); +0x13c(1) / +0x13c(0) `0x100bb940` |
| tool.measure | 0x3552 | `FUN_004b70c0` -> view mode 8 | clicks `0x100e9da0` / `0x100e9f00`, result mv17 in `FUN_004cf000` |

View interaction modes live at CCADModule+0x1f0 (setter `0x100e81a0`); per-mode
left-down / move / left-up handlers are function-pointer tables at +0x118 / +0x160 / +0x1a8,
filled in the constructor `0x100ebc40`. (O)

## 2. Drawing tools (O unless tagged)

`FUN_100ef350` instantiates an `ICreateGlyph` subclass per tool index. Slots (all CCreate*
vtables): [5] click with snapped point, [6] move, [7] finished?, [8] typed point, [3]/[4]
preview (`0x10084000`: yellow `glColor3d(0.9,0.9,0)`, `glLineStipple(2,0xAAAA)`), [2] create.
A click within tool+0x38 of the previous point in **both** x and y is ignored. Rebuild: the
same rule with 2 screen pixels (`MIN_CLICK_PX`; the original's +0x38 value is **U**).

| Tool | Class / addr | Clicks and geometry |
|---|---|---|
| point | CCreatePoint 0x100832b0 | 1 click; rebuild stores a zero-length open contour `extra['point']` (I) |
| line | CCreateSegment 0x10083470 | 2 clicks |
| circle | CCreateCircleOR 0x1007f850 | centre, point on circle |
| circle3 | CCreateCircle3Pt 0x1007fb50 / circumcircle 0x1007f610 | 3 points (collinear 3rd point rejected) |
| arc3 | CCreateArc3Pt 0x1007ef10 / 0x100098a0 | start, through-point, end |
| scan arc | CCreateArcORA 0x1007e910 | centre, start (radius + start angle), end angle; CCW (direction I) |
| ellipse | CCreateEllipse 0x10080520 | centre, major-axis end, ratio = dist(centre, p3)/major; flattened to lines (model has no ellipse, I) |
| rect | CCreateRect 0x100817b0 / 0x100810e0 | 2 corners, needs dx and dy >= tol; vertices (x0,y0)(x1,y0)(x1,y1)(x0,y1) |
| round rect | CCreateRoundRect 0x10082e90 / 0x10082c20 | 2 corners + radius point; r = min(dist(corner2, p), 0.5·min(w,h)) (0.5 = 0x1011d738), r >= 0.02 (0x101203e0); 8 elements |
| polygon | CCreatePolygon ctor 0x10081b60(6), 0x100811e0 | centre, vertex; n = 6 (clamped 3..360), vertex k rotated k·2π/n. Rebuild: `+`/`-` change n (addition) |
| star | CCreateStar ctor 0x10081df0(6), 0x10081310, 0x10082070 | centre, outer vertex (default inner r = R·sin(π/2−2π/n)/sin(π/n+π/2)), third click sets inner radius; 2n vertices |
| pline | CCreateLwploy 0x10082900 | vertices until Finish (mv2) / Enclose (mv4); arc mode (mv6) adds bulge arcs (FUN_10080d30; rebuild uses the tangent-continuous bulge, I); Backspace removes the last vertex (rebuild) |
| curve | CCreateSpline 0x10083b90 | fit points until Finish; the original keeps a NURBS (splineAnalyer), the rebuild flattens a centripetal Catmull-Rom spline (I) |
| text | CGlyText 0x100ab590 / 0x100aca20 | see §3 |

Snapping (`mlaser/cad/snap.py`, O): CDynCaptureEng `0x10054420` asks each graph
(`IGraph+0x90`, CGlyContour `0x10062f10`: only inside the bbox ± tol) and each element
(`+0x88`): segment start/end/mid (`0x10084550`), arc start/end/mid/centre (`0x10086f30`),
circle centre + quadrants (`0x10091810`), square window |dx|,|dy| <= tol. The tolerance is
CCADModule+0x270, derived from the pixel size by `0x100e9210` (factor **U**); rebuild:
8 px. The original returns the first hit, the rebuild the nearest (I). **No grid snap** exists
(no grid parameter or drawing code found). Shift while clicking disables snapping (rebuild).

After one shape the rebuild returns to selection (I; the original's repeat behaviour is U);
right-click with no points / Esc = mv3 End Draw.

## 3. Text, advert text, adv text, QR

* **draw.text** (O): CTextInputDlg (`FUN_005df8b0`): text (prompt mv20), CBCGPFontComboBox
  `Setup(TRUETYPE_FONTTYPE)`, height ti0 (int, app+0xb490); font name app+0xb474, default face
  gp79 (宋体). CADModule renders glyphs with `CreateFontA(h,0,0,0,500,...)` (`0x100e1e30`) and
  `GetGlyphOutlineW(..., GGO_BEZIER)` (format 3, `0x100f51c0`), advance = gmCellIncX. Rebuild:
  `QPainterPath.addText` (same TrueType outlines), curves flattened (0.02 mm), height = font
  cell height (I), placed with the bbox bottom-left at the click (I). Default height 10 mm (I,
  not registered).
* **draw.advert_text** 广告字 (O): sets worker mode 1; the worker calls ICADModule+0x100 with
  `GRP.FillGlyDir` (pd725, 0 X / 1 Y / 2 along edge), `GRP.FillCircleRadius` (pd726, 4),
  `GRP.FillCircleStock` (pd727, 10), `GRP.FillCircleSpace` (pd728, 10) -> CFillCircle
  `0x100ce2d0` (LED-hole circles inside luminous letters). Rebuild: `mlaser.cam.array.fill_circles`
  per outer contour of the selection; an open contour aborts with A251103_1.
* **draw.adv_text** 精品字 (O): CGraphParaDlg section 0x21, `GRP.AdvTextType` (pd1810: 0 all
  shrink pd503, 1 all expand pd504) and `GRP.AdvTextOffset` (pd1811, mm, default 0); for every
  selected contour `0x100c3170` calls the kerf offset `FUN_100738b0` and adds the copy as a new
  graph (COpCopyCmd). Rebuild: `mlaser.cam.kerf.compensate(c, 2 + type, offset)`; |offset| <
  0.01 does nothing (0x1011d800). The rebuild's dialog default offset is 1 mm (the registered 0
  is a no-op).
* **draw.qr** (O): CQRCodeCreateDlg (`FUN_00423040`, title A260420_1): `QR.QRCodeNote` text,
  `QR.QRCodeSize` (A260420_2, 25 mm, 10..100), `QR.QRCodeLineDensity` (A260420_4, 12, 3..30).
  The code is written as a scan-line G-code `QR_Code_Transform.nc` with line spacing
  `1/density` mm (`FUN_004b67d0`) and imported by the scan G-code importer. Rebuild:
  `segno` + `mlaser.cad.qr.qr_scan_lines` (serpentine horizontal lines, same spacing),
  option "outline squares" (rebuild); placed by click. Encoder details of the original
  (error level, version) are **U**; the rebuild uses level M.

## 4. Editing

* **trim / extend** (O entry points, I behaviour): trim removes the piece of the clicked
  contour between the two intersections (with every other contour and itself) that bracket
  the click; a closed contour needs >= 2 intersections and becomes open. Extend prolongs the
  open end nearer to the click (line along its direction, arc along its circle) to the nearest
  intersection. Both stay active for further picks until right-click / Esc. A miss creates no
  undo step.
* **split** (O): nearest point on the contour under the click; open -> two graphs, closed ->
  one open contour starting at the point.
* **explode** (O): contours with > 1 element (or a polyline with several vertices) become one
  open graph per element (`0x100b7ce0`); a lone circle stays.
* **merge** (O call, I rule): dialog pd368 `GRP.ManualConnectGate` (0.1 mm, 0.01..100), joins
  open contours end-to-end in either direction; a chain meeting itself is closed.
* **align** (O): >= 2 graphs, reference = selection bbox; main button = left (mode 0).
* **size** (O): dialog width (gp64) / height (gp63) / ratio lock (pd659 `GRP.ZoomRatioIsLock`) /
  anchor (+0xb400, 1..9, default 5). Lock: the changed side's factor is used for both
  (`FUN_004b20a0`). Anchor numbering (row-major from top-left) is **I**. Non-uniform scaling
  flattens arcs (I). Presets observed: 10/30/50/75/200/500 % and 10/50/100/200 mm (larger side).
* **groups** (O call chain): a CGlyGroup is one graph; the rebuild represents it as one Part
  with all member contours and `extra['group'] = True`. Explode selected / all splits it into
  one Part per contour (craft copied).
* **select similar** (O): exactly one selected graph; others selected when length within 0.02
  (0x1011dae0), same element count, elements pairwise similar in the same or reversed order
  (`0x10063540`).
* **select inner / outer** (O): mode 9 selects graphs whose contour-tree depth (+0x88, set by
  `0x10022270`, 1 = outermost) equals the sub mode: `select.inner` = 0x331e (mf651) -> depth 1,
  `select.outer` = 0x331f (mf652) -> depth 2. The Chinese captions (外模 outer / 内模 inner)
  match this; the English captions in lang.txt (and the rebuild's command names) are swapped.
  Rebuild: shapely nesting over all closed contours; a multi-contour Part is selected if any
  contour has the depth.
* **measure** (O): two snapped clicks, result mv17 `X: |dx| Y: |dy| Length: d` (inch variant
  A250917_6) in the log and status bar.
* **fine-tune** (O): step `IGP.KeyboardMoveStepLength` (pd377, 1.0) and auto step
  `IGP.AutoCalcKeyboardMoveStepLength` (pd376, on). Auto step (`FUN_004d0930`):
  v = 0.02·extent (width for ←/→, height for ↑/↓), extent >= 50: divide v by 10 while v >= 10,
  else multiply by 10 while v < 1; step = max(1, round(v))·decade. The rebuild replaces
  `window.nudge` with this rule; the dialog sets step/auto.

## 5. Canvas interaction-tool API (rebuild)

`CanvasView.set_tool(tool)` routes left/right press/release, move and keys (model
coordinates) to `tool.press(x, y, button, mods)`, `move(x, y, mods)`, `release(...)`,
`key(name, mods) -> bool`, `cancel()`. Esc/Enter/Backspace/Delete are taken before the window
shortcuts (ShortcutOverride) and the context menu is suppressed while a tool is active.
`set_preview(polylines, markers)`, `clear_preview()`, `snap_point(x, y, tol_px)`,
`pixel_size()`. Middle-button pan and wheel zoom keep working. With no tool the canvas
behaves exactly as before.

## 6. Unknowns

* Default text height and the original repeat/one-shot behaviour of the drawing tools.
* Exact pick / snap / min-click tolerances (CCADModule+0x270 factor, tool+0x38).
* The anchor numbering of the size dialog and the scan-arc sweep direction.
* The exact trim / extend algorithm inside CTrim (`0x1004ff60` vtable) — the rebuild follows
  standard CAD semantics.
* QR error-correction level / version chosen by the original encoder.
* Whether open contours take part in the inner/outer contour tree.
