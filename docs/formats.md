# Mlaser file formats

Target: Gweike Mlaser v0.0.1.51_SP1. Evidence is cited as `DLL FUN_<addr>` (Ghidra dumps in
`decomp/`) or `MainApp 0x<addr>` (instruction address in MainApp.exe). Labels:
**observed** means read directly from code or sample bytes. **INFERRED** means a likely meaning
that the code does not prove. **UNKNOWN** means not determined.

Implementations:

| module | contents |
|---|---|
| `mlaser/formats/scio.py` | CSerialEng line reader/writer shared by every "scFlie" file |
| `mlaser/formats/chf.py` | `.chf` drawings: `load_chf`, `save_chf`, `loads_chf`, `dumps_chf` |
| `mlaser/formats/scfile.py` | AutosaveParam, ManuContour, tempIsBreak, PithCompensate, softPara.ini, JumpAddTime.txt, calib.csv |
| `mlaser/formats/params.py` + `data/mlaser_params.json` | machine and process parameter XML files and descriptor table; `LayerParams` |
| `mlaser/i18n/langfile.py` | `Translator` for `Lang/*.txt` and `lang.ini` |

Tests: `tests/test_formats.py`. Both `.chf` samples and all the small files round-trip byte for byte.

---------------------------------------------------------------------------------------------

## 1. "scFlie" text container (class `CSerialEng`)

CADModule `FUN_100e70e0` (ctor) to `FUN_100e7f30`. MainApp has its own copy (`FUN_00403db0`
ctor, `FUN_00403ed0` open, `FUN_004043a0` ReadInt, `FUN_00404400` ReadDouble,
`FUN_00404580` ReadBool, `FUN_00404700` open-write, `FUN_004048c0` WriteStr,
`FUN_004049b0` WriteInt, `FUN_00404820` WriteEof).

* The file is ANSI text (CP_ACP, which is GBK on the Chinese builds) with CRLF line ends. The
  first line is `scFlie` (sic) and the last line is `eof`. On open, `100e7b00` checks both:
  error 5 if the header is wrong, error 4 if the file does not end in `eof`. Bytes <= 0x0d after
  `eof` are ignored.
* **ReadLine** `100e7350` reads up to CR or LF and swallows one following CR or LF. It
  **removes every space and TAB inside the line**, so `####Gly: 1` is read as `####Gly:1`. The
  tags are never compared: the reader just skips their lines.
* ReadInt `100e7770`: the line must be `[-0-9]*`, then `atoi`. ReadDouble `100e77b0`: the line
  must be `[-.0-9]*` with fewer than 2 dots, then `atof`. ReadBool `100e77f0`: true only if the
  line equals `1`. ReadPoint `100e7840`: `x,y`. ReadDoubleArray `100e7a90`. ReadPointArray
  `100e7f30`. ReadWString `100e7d30`: MultiByteToWideChar(CP_ACP).
* WriteDouble `100e7530`: if `|x| < 1e-10` (DAT_10122268) it writes `0.0`; else if `|x| < 1e-6`
  (DAT_1011fe30) it writes `%12.10f`; otherwise `%f`. WritePoint `100e7680` always writes
  `%f,%f`. WriteInt writes `%d`, WriteTagInt writes `%s%d` (`100e7460`), WriteBool writes `1`/`0`.
* Because of these rules, re-formatting the parsed values reproduces the original bytes. That
  is how `save_chf` round-trips.

---------------------------------------------------------------------------------------------

## 2. `.chf` drawing / job file

Writers: `FUN_100ebdd0` (document), `FUN_100f34d0` (part library, `\Graph\Work%d\%d.chf`). Both
write the version from `this+0x9a0`, which is set to **9** in the CAD ctor
(`param_1[0x268] = 9`). Readers: `FUN_100ee650` and `FUN_100f04a0`, then `FUN_100bd0c0`
(document) and `FUN_100bcec0` (graph list).

Notation below: `D` = double line, `I` = int line, `B` = bool line, `P` = `x,y` line,
`T` = tag line (skipped on read), `S` = text line, `[vN]` = present only when the version
condition holds. "old" means versions 2..4 (`param - 2U < 3`), which carry extra opaque lines.

### 2.1 Document (`FUN_100b1a20` write / `FUN_100bd0c0` read)

```
S  scFlie
I  version                               (if unreadable the loader uses 1)
T  <Begin Graphs>
   [old] 5 opaque lines
I  graph count N
   N x { T "####graph NO:<i>"  I graph type  <graph body> }
T  <End Graphs>
[v>7] T <Begin Vision>  I n  n x I graph index (values <0 dropped on load)  T <End Vision>
B  rotated                (INFERRED: the whole drawing is rotated; see below)
D  rotate angle
P  rotate centre          (CAD this+0x9d00)
P  rotate offset          (CAD this+0x9d10)
S  eof
```

Trailer: in `FUN_100bd2bc` (the tail of the load), if `rotated && |angle| > 0.0015` the loader
keeps copies of the graphs and re-applies a rotation about 0x9d00 plus the offset 0x9d10. The
rotation helper `FUN_100bf4a0` uses cos/sin of the angle around 0x9d00, then adds
0x9d10. The angle is probably in radians (INFERRED). When the drawing is empty, the writer sets
both points to (0,0). The two values B and D come from the caller (MainApp) through the
interface; their UI meaning is INFERRED.

The "Vision" list holds the indices of graphs marked as camera/vision features (INFERRED from
the name and from the per-contour `<Vision Feature>` flag).

### 2.2 Graph types (factory `FUN_100ad550`)

| type | class | body (save / load) |
|---|---|---|
| 8 | CGlyContour | `FUN_100660d0` / `FUN_10072ad0` (§2.3) |
| 9 | CGlyGroup | `FUN_1007bf70` / `FUN_1007df90` (§2.4) |
| 10 | CGlyText | `FUN_100aaea0` / `FUN_100acf50` (§2.6) |
| 11 | CGlyScan | `FUN_1009a080` / `FUN_1009c9c0` (§2.5) |
| 12 | CGlyContourEx | `FUN_10077d70` / `FUN_10079a80`+`FUN_10077bc0` (§2.7) |

The load loop (`FUN_100bcec0`) discards graphs whose length is below 0.01. The exception is a
contour or group whose only glyph is a single point (type 1). `load_chf` keeps everything.

**Groups vs graphs.** A *graph* is a top-level object. A *group* (type 9, and the group base of
types 10, 11 and 12) contains `Group elem` children, and each child is a full CGlyContour (the
loader always news a 0x358-byte CGlyContour). In the model, each top-level graph becomes one
`Part`, and each contour (or group element) becomes one `Contour`. CGlyScan path contours are
not exposed as model contours; they are kept in `Part.extra['chf'].paths`.

### 2.3 CGlyContour body

The offsets are fields of the C++ object (ctor `FUN_1006c2d0`, IGraph base ctor `FUN_10066de0`).

```
D  +0xd8  closing tolerance                       ctor arg DAT_1011d800 = 0.01; tempGraph has 0.1
T  <Glyphs>
   [old] 10 opaque lines
D  +0x48  length (sum of glyph lengths)            observed (20.0 for a 20 mm line; 2*pi*r for the circle)
P  +0x28  bbox min
P  +0x38  bbox max
P  +0x68  start point                              observed (= first glyph start)
P  +0x78  end point                                observed (= last glyph end)
I  glyph count n
   n x { T "####Gly: <i>"
         I  dir      int stored with the glyph pointer (pair vector +0xb8); 1 = forward,
                     -1 = traversed reversed (FUN_10071xxx: `piVar1[i*2+1] == -1 -> 1 - t`)
         I  glyph type (factory FUN_10062860, §2.8)
         <glyph body>
         [old] 3 opaque lines }
[v>6] I +0x354  UNKNOWN int. The ctor never initialises it, so it usually holds stack/heap
                garbage. -1066022767 = 0xC075C891 is the high dword of a double near -348.5;
                scan paths store 4. Set by `FUN_100a3f10` (`piVar5[0xd5] = param_3`)
      I count m; m x { D length, B flag, B flag }   +0x344 sub-segment table: per-piece lengths
                with two flags, built when contours are merged or micro-jointed (`FUN_100a3f10`).
                Flag meanings UNKNOWN.
[v>7] T <Vision Feature>  B +0xb1 vision-feature flag  T <End Vision Feature>
T  <End Glyphs>
I  +0x0c  layer index (INFERRED: the vtable+0x48 setter FUN_10063440 / FUN_1007be30 pushes it to
          every glyph and to the derived contour +0x110; class COpGraphLayerCmd exists).
          autosave = 6, tempGraph = 0.
I  +0x88  IGraph flag, default 1 (INFERRED: visible/enabled)
T  <Crafts>
   [old] 20 opaque lines
I  +0x100 kerf compensation side, default -1 = none (INFERRED: 0/1 = inner/outer)
D  +0x108 kerf width
T  <PWM Control>
I  +0x114 mode, default 1. In mode 1 the list below is generated from the pairs (`FUN_1006c930`); scan paths use 3 with an explicit list.
I  n; n x { D +0x118[i] position (fraction 0..1 of length), D +0x128[i] width (mm) }
          micro-joints / bridges (INFERRED)
I  m; m x D +0x13c[i]  laser on/off switch positions (fractions 0..1); in mode 1 each pair gives
          pos +/- width*0.5/length
T  <End PWM Control>
D  +0x180 start position as a fraction of length (setter `FUN_10071f30`; forced to 0 if the contour is open)
D  +0x198 overcut / overlap length in mm (setter FUN_10070e80; used as length/+0x48 offset on
          the end parameter; INFERRED sign: >0 overlap, <0 gap)
T  <GuideCurve Para>                              CGuideCurve at +0x1b0 (ctor FUN_10068890)
I  +0x1b8 lead-in type (default 0)
D  +0x1c0 lead-in angle  (default 90, DAT_1011d6c8)
D  +0x1c8 lead-in length (default 2, DAT_1011d6c0)
[v>=2] D +0x1d0 lead-in arc radius (default 1)
B  +0x1f8 lead side flag (O: toggled by the reverse 0x10073980, so an open contour that the
          import sort reverses is written with 1: spline_ellipse, mix, inserts2; model name
          LeadParams.at_corner kept, INFERRED "lead at corner" earlier)
[v>=6] I +0x1d8 lead-out type, D +0x1e0 angle, D +0x1e8 length, D +0x1f0 radius
T  <End GuideCurve Para>
[v>2] T <coolPos Para>  I n  n x D +0x15c cooling positions (fractions, INFERRED)  T <End coolPos Para>
T  <End Crafts>
```

The writer `FUN_100660d0` always emits the lead-out block, and `<Vision>` / `+0x354` only for
v>7 / v>6. Our writer applies the reader's version gates, so `save_chf(doc, p, version=5)`
yields files that read back correctly. tempGraph (v5) has no lead-out block.

Model mapping: `Craft.layer` = graph +0x0c, `kerf_comp` = +0x100, `kerf_width` = +0x108,
`lead_in` / `lead_out` = GuideCurve (at_corner = +0x1f8). `micro_joints` = pair positions *
length (mode 1). `Craft.extra` holds `micro_joint_widths`, `pwm_mode`, `cool_positions`,
`start_param` and `overcut`.

### 2.4 CGlyGroup body (`FUN_1007bf70` / `FUN_1007df90`)

```
[old] 3 opaque lines
D length (loader of CGlyScan recomputes it from the paths)
P bbox min   P bbox max   P start   P end
I n;  n x { T "####Group elem NO:<i>"  <CGlyContour body> }
I +0x0c layer    I +0x88 flag
```

### 2.5 CGlyScan (type 11, raster/scan engraving) `FUN_1009a080` / `FUN_1009c9c0`

```
<group body>
T <Scan path>
[old] 5 opaque lines
I n;  n x { T "####Path:<i>"  <CGlyContour body> }  computed scan path (lines + spline U-turns)
[v>8] I +0xe0  UNKNOWN (4 in the sample)
T <End Scan path>
```

The sample `autosave.chf` is one CGlyScan with 839 elements (20 mm lines) and one 2077-glyph
path. The path's PWM block has mode 3 and lists 1678 laser switch fractions.

### 2.6 CGlyText (type 10)

```
[old] 5 opaque lines
P +0x130 insertion point;  D +0x140, D +0x148, D +0x150 (INFERRED: height, angle, spacing)
S +0xc8  text (ANSI; the original reader strips spaces, we keep the raw line)
T <Text para>
S +0xe8  font name
D +0x108 (default 1.0)  D +0x110 (default 20.0, DAT_1011d848)  D +0x118 (default 0)   UNKNOWN
T <End Text para>
v>=4: <group body> (the outline glyphs)    v<4: I +0x0c, I +0x88 (outline rebuilt from text, FUN_100aca20)
```

### 2.7 CGlyContourEx (type 12)

```
<group body>
I n;  n x { T "####ContourEx link info:<i>"  I a  I b  D D D D  P p1  P p2 }
```

On read, p1 is parsed only if a==1 and p2 only if b==1. Otherwise the line is skipped (we keep
it raw). The meaning of these link records is UNKNOWN.

### 2.8 Glyphs (factory `FUN_10062860`; load/save addresses)

| type | class | fields | notes |
|---|---|---|---|
| 1 | CEditablePoint | `P` | `FUN_10091520`/`FUN_10090fb0` |
| 2 | CEditableSegment | `P start, P end` + [v>6] `I n, n x (P,P)` | `FUN_10092db0`/`FUN_10091a10`. The pair list at +0x54 is INFERRED to be micro-joint gap segments |
| 3 | CEditableArc | `P centre, D r, D a0, D a1` | `FUN_100867e0`/`FUN_10084270`. Angles are radians (`FUN_10001500`: start = c + r(cos a0, sin a0)). Normalised by +/-2*pi (`FUN_10001470`). INFERRED CCW from a0 to a1 (we use `ccw = a1 >= a0`) |
| 4 | CEditableCircle | `P centre, D r` | `FUN_10088180`/`FUN_10086e10`. Starts at angle 0 (sample: start = c+(r,0)). Direction comes from `dir` |
| 5 | CEditableEllipsArc | `P centre, P axis, D ratio, D t0, D t1` | `FUN_1008a640`/`FUN_10088430`. INFERRED DXF convention (major-axis vector, minor/major ratio, parameter angles) |
| 6 | CEditableLwpoly | `I flag, I n, n x (P vertex, D bulge)` | `FUN_10090c50`/`FUN_1008bcf0`. flag = closed, bulge as in DXF (observed: MainApp's DXF import writes a closed LWPOLYLINE as one glyph with flag 1 and its DXF bulges) |
| 7 | CEditableSpline | `I u0, I u1, I n, n x P ctrl, I m, m x D knot` | `FUN_10097450`/`FUN_10093260`. The loader reads u0/u1 and **discards** them (+0xc4/+0xc0). u0 is often garbage (1752629811, 132898544, 1064 in the samples). u1 = 3 for copied DXF splines (FUN_10008740 sets the piece record's +0x50 = 3) and for text records (FUN_100ab710); for Bezier pieces it is uninitialised: ellipse pieces (FUN_10097600 copies the never-set local at -0x1c: 4 in most files, 131573736 once in mix.dxf) and quadratic-SPLINE pieces (FUN_100992d0 never sets +0x50: 3 / 5 observed = the control-point count, which the rebuild writes). Requires n>3 and m == n+4, so the spline is cubic (order 4) |

Geometry conversion is in `gly_segments()`. **Spline flattening** (D-15) is a port of the original's
(CEditableSpline FUN_100953d0 → splineAnalyer vtable+0x2c FUN_100bf350 / FUN_100befa0), in
`geom.curves.flatten_bspline`: the analyser gets d = sqrt(2·R·e) = 0.632 mm and α = acos(1 − e/R) =
18.19° from the glyph's tolerance e = 0.1 (+0x10) and reference radius R = 2 (+0x180). Each knot
span [u0, u1] is examined at u0, the middle and u1: with A = tangent turning start→middle→end and
D = |Δx|+|Δy| start→middle + middle→end, the span is cut into n = even(max(round(A/α), round(D/d), 1))
equal parameter steps if A < 15° or D < d, otherwise into even(trunc(1.5·A/15°) + 2) parts that are
examined again. Interior knots are included, points within 0.01 mm (per axis) of the previous point
are dropped and the end point is appended. The polyline length equals the contour length the
original writes (+0x48) to 1e-4 mm on all samples (650.5932 for the dxfTemplate spline, 68.6944,
32.0927, 18.1649 per ellipse quarter), and stays within 0.016 mm of the curve (the old 8 points per
span were up to 1.01 mm off). The contour bbox (+0x28/+0x38) is the bbox of this polyline.

Writing new geometry: a contour that still carries its **source glyphs**
(`model.Glyph` in `Contour.extra['glyphs']`, valid while `Contour.segments` is unchanged, see
`Contour.source_glyphs()`; set by the DXF import and by `load_chf`) is written with those glyphs,
so an LWPOLYLINE stays one type-6 glyph, a SPLINE a type-7 glyph and a circle a type-4 glyph; its
length is the sum of the glyph lengths (spline: the flattened polyline, as the original), its
closing tolerance `extra['close_tol']` (0.1 for imported contours) and +0x88 `extra['depth']`.
Otherwise a `Line` becomes type 2, an `Arc` type 3 (CW arcs are stored as a CCW arc end->start with
dir=-1) and a full circle that starts at angle 0 type 4. Angles are written with `%f` (6 decimals,
as the original does), so re-read arc end points can move by about 1e-6*r. The bbox is exact
(arc extremes at the quadrant angles).

### 2.9 Version differences (all from the reader gates above)

| version | difference |
|---|---|
| 1 | lead-in has no radius |
| 2..4 | extra opaque lines (graph list 5, contour 10+3/glyph+20, group 3, scan 5, text 5) |
| >2 | coolPos block |
| <4 | text without group body |
| >=6 | lead-out block |
| >6 | segment tab list, contour +0x354 and sub-segment table |
| >7 | `<Vision Feature>` per contour, document `<Begin Vision>` list |
| >8 | scan +0xe0 int |

Sample versions: autosave = 9, tempGraph = 5.

### 2.10 Round-trip strategy in `chf.py`

Every record is kept, including garbage ints and opaque old-version lines, as dataclasses in
`Part.extra['chf']` and `Contour.extra['chf']`. A snapshot of `Part.craft` and of the contour
segments is stored in `Part.extra['_chf_snapshot']`. On save, if the model differs from the
snapshot, the glyphs, length, bbox and start/end are rebuilt from `Contour.segments`, and/or the
craft fields are written into every contour of the part. Otherwise the original records are
written unchanged. Parts without chf data (made by other subsystems or by the DXF import) are
written as a type-8 contour (one contour) or a type-9 group (several contours, e.g. a text line
of the DXF import, layer 10); their glyphs come from the source glyphs when present (§2.8).
The DXF import's file is byte-identical to MainApp's import + Save As on all non-TEXT parity
samples (graph order and reversed open contours from the import sort, cam.md §1), except the
uninitialised ints: contour +0x354, spline u0, and the u1 of Bezier pieces (§2.8). Text groups
carry line and cubic spline glyphs as the original (GGO_BEZIER records), with unhinted
coordinates.

**Per-contour crafts.** Every group element is a full CGlyContour with its own `<Crafts>` block
(§2.4), so a contour of a multi-contour part may carry its own `Craft` in
`Contour.extra['craft']` (cam.manual.contour_craft, ui.docops.apply_lead). The writer applies it
to that element (layer, kerf, PWM/micro-joints, start, overcut, leads, cooling); the other
elements get `Part.craft`. On load, an element whose craft differs from the first element's
becomes such an override. Unchanged overrides are not re-applied, so the round trip stays
byte-identical; craft snapshots are deep copies, so in-place edits (`craft.lead_in.length = 5`)
are detected.

---------------------------------------------------------------------------------------------

## 3. Small "scFlie" state files (`scfile.py`)

| file | layout | evidence / meaning |
|---|---|---|
| `File/AutosaveParam1.ini`, `...2.ini` (also `File/Temp/`) | `I counter, I x*1000, I y*1000, I a, I b` | MainApp `FUN_004693c0`: written only when app+0x484c == 2 (machining). Global counter DAT_009f2354: even writes file 1, odd writes file 2, then the counter increments. The newest file has the larger counter. x/y come from `FUN_00457b80` (machine position) * 1000 (DAT_0084cc10), rounded, i.e. micrometres. a, b come from NC interface vtable+0x278 (INFERRED: graph index, path element index at the break point). Read by `FUN_0045e180` for "resume after break" |
| `File/ManuContour.dat` | `I n, n x I` | written by `FUN_00457de0`, read by `FUN_0045e180` (the last value is compared with graph count-1). INFERRED: list of graph indices already processed / processing order |
| `File/Temp/tempIsBreak.ini` | `I isBreak, I laserType` | read in `FUN_0048c550`. The 2nd value is compared with app+0x4728, and a mismatch shows lang `A250421_0` ("The processing task does not match the current type of laser?") |
| `File/PithCompensate.pcf` | `I count` + UNKNOWN records | leadscrew pitch-error compensation. Saved by the dialog `FUN_0052e250` through interface vtable+0x2c4 (module not identified). Dialog rows hold 6 doubles (`FUN_0052e370`: nominal, measured-, measured+, err+, err-, backlash; INFERRED). The sample only has count 0 |
| `File/Temp/tempGraph.chf` | `.chf` v5 | drawing saved with the break state |
| `File/Temp/tempLayer.xml`, `\_tempLayer.xml`, `\_tempSource.chf` | ParameterRoot XML / chf | layer parameters and source drawing saved with the break state (MainApp 0x48cd12, 0x48861c) |

### Plain INI-style files

* `File/softPara.ini` `[SC2000]`. `NormalExit` is set to 0 at start-up (MainApp `FUN_00596320`)
  and to 1 on a clean exit (`FUN_0047e150`). `XAxis/YAxis/ZAxis/WAxis` (written by `FUN_00481c70`) are the machine
  positions at exit as ints (micrometres, INFERRED) and are restored to app+0xbccc+axis*0xa0 on start
  (`FUN_00596320`). If the last exit was not clean, a prompt is shown.
* `JumpAddTime.txt` (INI despite the name):
  * `[Jump] AddTime` (default 100) is added in ms (x0.001) to the computed jump time
    (MainApp `FUN_004657e0`).
  * `[Axis4Freq] Is4Freq` (default 0, MainApp `FUN_004d90e0`).
  * `[Arc2SegVelK] K_X, K_Y` (default 100, scaled x0.01) are used by CADModule `FUN_101041d0` for arc-to-segment velocity.
  * `[LimitSamllCircleVel] IsLimit/SlowRatio` are in the shipped file but **no binary
    references them**.
* `LanFormatEStr.txt` `[SC] Var<N>=<langKey>` is a debug log. `FUN_005436a0` increments a
  counter and records lang keys whose translated text does not split into the expected
  `Group.Name` parts. It is not configuration.
* `File/CustomerInformation.ini` `[Information] Url, QrCode` (also `Customer`, read with
  GetPrivateProfileStringA, MainApp `FUN_004e5770`) is branding.
* `File/ipAdd.ini` (`[IP] CardIP` default 10.1.1.168, `MonitorIP`, `MonitorPort` 0x1778,
  `MonitorReconnInterval`, ...) belongs to the controller/cloud agent. Not implemented here.
* `Help/calib.csv`: `fprintf("%d, %d\n")` (text mode, so CRLF) of the pairs returned by the
  height-controller interface vtable+0x1b8 (MainApp `FUN_00633da0`, dialog `CCalibCurveDlg`),
  after which the folder opens in Explorer. A second export (`FUN_00634070`) lets the user pick
  a csv path. The columns are (height, raw capacitance count). INFERRED units: micrometres
  (0..9001) and sensor counts (~406k..413k). The first row `413023, 0` is INFERRED to be the
  far-field/reference reading. Rows from 2301 onward repeat at the end of the sample, which
  looks like a stale buffer tail.

---------------------------------------------------------------------------------------------

## 4. Machine / process parameters (`params.py`)

### 4.1 Storage

`<exe>\dat\` (MainApp `FUN_004d90e0`, created with SHCreateDirectoryExW):

> **Correction (phase 3, agent H; see protocol.md §21):** in this build the live parameter XMLs
> are read from `%LOCALAPPDATA%\NexCut\`, and the `Bk*.xml` backups go to `<exe>\File\`
> (FUN_00451c30). The shipped `dat\` folder is empty. The rebuild uses
> `formats.backup.default_param_dir()`, overridable with `MLASER_APP_DIR`.

| file | ParaModule slot | message if missing |
|---|---|---|
| `HardPara.xml` | 0 | "The hardware parameter file was not found and was re-created." |
| `ManuPara.xml` | 1 | "The software parameter file ..." |
| `LayerPara.xml` | 2 | "The layer parameter file ..." |
| `SystemPara.xml` | legacy | if it exists and HardPara.xml does not, all three slots are loaded from it (migration) |
| `BkHardPara.xml`, `BkLayerPara.xml`, `BkManuPara.xml`, `SecondBkManuPara.xml`, `File/ErrorManuPara.xml`, `File/ErrorBkManuPara.xml` | backups / error copies | strings only |

The shipped install has an empty `dat/`, so the defaults below are the only data. ParaModule
can also store parameters in the registry under HKCU (`FUN_1000ba20` RegCreateKeyW,
RegSetValueExW, RegQueryValueExW); this is not used by MainApp for these files and is not
reproduced.

### 4.2 XML layout (ParaModule.dll `CXMLParaEngine`, own XML parser "2013.8.14")

* Load of a descriptor array: `FUN_10011010(slot, descs, n)`. Save: `FUN_10011510`. Set:
  `FUN_10010d30`.
* Full path = `"P"` (DAT_1001af94) + category + `"."` + key. It is split on `.`; every component
  except the last is an element, and the last is an attribute:
  `<ParameterRoot><PLayerParam1><GP CutSpeed="100" .../></PLayerParam1></ParameterRoot>`.
* Values are text. Missing values take the descriptor default. Doubles are written with
  boost::lexical_cast (`%.*g` with precision 17), bools as 0/1. Save goes through
  `<file>.tmp` + MoveFileExW (`FUN_1000e360`).
* On-disk encoding: the engine detects UTF-8/UTF-16. We write UTF-8 with an XML declaration
  (INFERRED).

### 4.3 Descriptor tables (MainApp)

Static initialisers (e.g. MainApp 0x7ddfac) fill global arrays of 0xd0-byte records:

| off | field |
|---|---|
| +0x00 | category (wstring) |
| +0x1c | key, e.g. `GP.CutSpeed` |
| +0x38 | lang key, e.g. `pd125` (label from lang.txt: "Cut Basic.Cut Speed") |
| +0x54 | value address (app object + offset) |
| +0x58 | type |
| +0x5c | default (text) |
| +0x78 | unit |
| +0x98 | min (double) |
| +0xa0 | max (double) |
| +0xa8 | option-list pointer (array of lang-key wstrings, stride 0x1c) |
| +0xac | option count |
| +0xb0 | condition / group string (e.g. `OnlyMC`, `LaserDA`) |
| +0xcc | flag, UNKNOWN |

Types (from the `FUN_10011010` switch and the values): 1 int, 2 double, 3 string, 4 enum,
5 bool, 6 DO port, 7 DI port, 8 IPv4 as uint32, 9 pulse equivalent (double), 10 DA port,
11 gas ratio, 12 extended DO port, 13 serial port, 14 colour (0xBBGGRR). Types 6..14 are
INFERRED from key names and ranges.

Table getters `FUN_006352f0`..`FUN_00635f90`. Slots are taken from the vtable+0x18/+0x1c call
sites (MainApp 0x4d96b8, 0x4d9c4d, 0x4da42b, 0x4dab6e..0x4db24f):

| table @ | n | slot / file |
|---|---|---|
| 0xafb4d8 | 457 | HardPara.xml (Axis, MachineAxisConfig_0..4, DI, DO, Home, Laser, Gas, ZF, FC, DA, part of ManuParam/SoftParam) |
| 0xaec428 | 95 | ManuPara.xml (ManuParam MC.*/LC.*/GC.*, SoftParam) |
| 0xb12ee0 | 218 | ManuPara.xml (GraphParam GRP.* = drawing/CAM defaults: leads, micro-joints, sorting...; NestParam) |
| 0xb1e650, 0xb24ea8, 0xb27060, 0xb290b8, 0xb29fb8 | 34, 11, 9, 8, 7 | ManuPara.xml (ImportGraphParam, ZF/EC, AF, SoftParam, StressTest) |
| 0xaf1210 | 164 | LayerPara.xml: **per-layer cutting parameters** |
| 0xaf9770 | 22 | LayerPara.xml: CO2-laser layer parameters |
| 0xb22e28, 0xb258d0, 0xb26430, 0xb27820, 0xb28860, 0xb2a568 | 40, 14, 15, 20, 10, 5 | not loaded through these slots (ZF/AF/EC/serial/laser-test; keys built at run time, UNKNOWN) |

All 1129 records (key, category, type, default, unit, range, label key, enum option label keys,
struct offset, file) are in `mlaser/formats/data/mlaser_params.json`. They are loaded by
`param_defs()`, which takes the label text from mlaser's catalogue (`mlaser/i18n/en.json`) and,
for Chinese, from translations imported from the user's install. The data file holds no vendor
label text (docs/ASSETS.md).

### 4.4 Layers

`FUN_006354c0`: there are **11 layers**. Layer *i* (0-based) has its own copy of the 164-entry
table with category `LayerParam{i+1}` and values at app+0x4ed8+i*0x480. CO2 layers use
`CO2LayerParam{i+1}`. The UI calls tab 0 "BackLayer" and the others "Layer n" / "Engrave n"
(MainApp `FUN_0057dc70`). The mapping from the `.chf` layer index (+0x0c) to these tabs is INFERRED.

Main per-layer keys (defaults / unit / range):

* Basic:
  * `GP.CutSpeed` 100 mm/s (0.01..1200)
  * `GP.CutHeight` 1 mm (0.2..5)
  * `GP.CutFocusPos` 0 mm (-50..50)
  * `GP.UpHeight` 20 mm
* Laser:
  * `GP.CutPower` 100 % (duty)
  * `GP.CutPeakCurrent` 100 %
  * `GP.CutFreq` 5000 Hz (1..50000)
  * CO2: `GP.CutDuty` 100 %
* Gas:
  * `GP.CutGasType` 0 (options: Air(CO2), O2(Fiber), N2/Air(Fiber))
  * `GP.CutAirPressure` 5 "V" (0..9999)
* Delays:
  * `GP.LaserOnDelay`, `GP.LaserOffBeforeDelay`, `GP.LaserOffAfterDelay` (0 ms)
* Process type `GP.ManuType`: Direct Cut / Fix Height Cut / One Step Drill / Two Steps Drill.
* Pierce stages s = 0..4:
  * `GP.DrillHeight{s}` (5 / 10 / 10 / 5 / 5 mm), `GP.DrillFocusPos{s}`, `GP.DrillPower{s}` 50 %
  * `GP.DrillFreq{s}` 500 Hz, `GP.DrillPeakCurrent{s}` 100 %, `GP.DrillGasType{s}`
  * `GP.DrillGasPressure{s}` 5, `GP.DrillDelay{s}` 1000 ms
  * `GP.EnableGradualDrill{s}` / `GP.GradualTime{s}`
  * `GP.EnableFocusGradual{s}` / `GP.FocusGradualEndPos{s}` / `GP.FocusGradualTime{s}`
  * `GP.Before/AfterLaserOffDelay{s}`
  * `GP.BoltDrill_Enable/Power/Freq_{s}` for s = 1..5
* Other:
  * slow start: `GP.SlowStart`, `GP.SlowStartLength` 1 mm, `GP.SlowStartSpeed` 10 mm/s
  * residue cleaning: `GP.CleanResidue_*`
  * start / end work segment: `GP.UD_*`
  * smooth pierce: `GP.SmoothPierce*`
  * ZF vibration abatement: `GP.ZFVibAbat*`
  * power/frequency curves: `GP.PWMCurveNodes`, `GP.FreqCurveNodes` (strings, format UNKNOWN), `GP.PowerAdjustWithSpeed`, `GP.FreqAdjustWithSpeed`
  * film and pre-pierce flags: `GP.NoManu`, `GP.NoFollow`, `GP.ShortDistNoUp`, `GP.PreDrill`, `GP.WithFilm`, ...
  * `GP.LayerFileName` (the craft file this layer came from), `GP.Note`, `GP.RecentFile`.

`LayerParams` mapping (INFERRED semantics):

| LayerParams field | fiber | CO2 |
|---|---|---|
| speed | `GP.CutSpeed` | `GP.CutSpeed` |
| power_pct | `GP.CutPeakCurrent` | `GP.CutDuty` |
| duty_pct | `GP.CutPower` | `GP.CutDuty` |
| frequency_hz | `GP.CutFreq` | `GP.CutFreq` |
| gas | `GP.CutGasType` | `GP.CutGasType` |
| gas_pressure | `GP.CutAirPressure` | `GP.CutAirPressure` |
| cut_height | `GP.CutHeight` | `GP.CutHeight` |

`pierce` = `{type: GP.ManuType, stages: [5 dicts]}`. `extra['values']` holds all 164 raw values.

### 4.5 Craft / process library files ("FileFilter_CraftFile" = 工艺文件 / "Craft file")

These are `*.xml` files (MainApp strings `\*.xml`, `.xml`). The per-layer key `GP.LayerFileName`
and `File/Temp/tempLayer.xml` show that a craft file is a ParameterRoot XML holding layer
parameter sections. Whether a craft file uses `LayerParam{n}` categories or an unnumbered
category is UNKNOWN (no sample). `load_layer_params` / `save_layer_params` use the LayerPara.xml
convention.

---------------------------------------------------------------------------------------------

## 5. Language files (`lang.py`, LangModule.dll)

* `Lang/lang.txt` is UTF-16LE with BOM and CRLF. Each line is `key#中文#English`. Loader
  `FUN_10009590`: lines with >= 3 `#` fields fill map 0 (zh) and map 1 (en). Shorter lines are
  ignored, and lines are read up to 1000 wchar. Special keys: `0_SongFont` (宋体/Arial) and
  `0_FontSize`.
* `Lang/<Language>.txt` uses the same encoding with `key#translation`. Lines with >= 2 fields
  fill that language's map from field[1]. Note that `lp17##Layer ...` (a typo in French.txt)
  therefore maps to an empty string, as in the original (INFERRED: boost::split without
  token compression).
* Language list (enumerator `FUN_10006850`): `简体中文`, `English`, then the stem of every
  `Lang\*.txt` except `lang`, in FindFile order (alphabetical on NTFS).
* `Lang/lang.ini` is ANSI. `[Global] Lang=<language name>` (the sample has 简体中文 in GBK) is
  read with GetPrivateProfileString. If the name is unknown, GetPrivateProfileInt reads it as an
  index. It is written back with WritePrivateProfileStringW (`FUN_100021a0`). `hintStr` is a
  human-readable index list only (its order differs from the real enumeration order).
* Lookup `FUN_10005710`: current language, then English, then the key itself.

---------------------------------------------------------------------------------------------

## 6. Intentionally omitted

* Licensing, dongle/HID checks (`xhcusb.txt` is an HID error log), "Check code" / admin
  activation (`SP.CheckUserID`, `[Soft] AlarmDay`), cloud phone-home (`File/sources.txt`
  `https://app.gweikecloud.com/mlaser/PC`, `MonitorIP`/`MonitorPort`), and
  `ProcessesStatistic.txt` (GBK cloud statistics line).
* The nest project format (`<Begin Parts>/<Begin Sheets>/<Begin NestResults>`, CADModule
  `FUN_100da580`/`FUN_100df500`, saved by `FUN_100ebef0`). It uses the same container; its
  layout and the `.nestproj` reader/writer are in [nest.md](nest.md) §2.4.

## 7. Open unknowns

* chf: contour +0x354 and the sub-segment flags, the scan +0xe0 int, the meanings of the
  ContourEx link fields and the text doubles. The exact semantics of `+0x88`, of the kerf side
  values and of lead types 1..3 (see docs/cam.md, owned by the CAM agent).
* Trailer `rotated/angle` units.
* PithCompensate.pcf record layout and the AutosaveParam a/b ints.
* XML on-disk encoding/indentation (no sample file). Craft-file category naming. The tables not
  loaded through slots 0..2.
