# Mlaser UI: reverse-engineered layout and feature map

Target: Gweike **Mlaser v0.0.1.51_SP1** (`$ORIG/MainApp.exe`, internal name *NexCut*, MFC +
BCGControlBar Pro 22.1 ribbon). This document maps the original UI to the PySide6 rebuild in
`mlaser/ui/`. **Observed** = seen in a shipped file (path/offset/function cited).
**Inferred** = deduced from naming, order or layout and not confirmed in code.

## 0. Sources and method

| Source | What it gave |
|---|---|
| `MainApp.exe` PE resources (pefile, `work/ui/scripts/resdump.py`) | 2 menus, 7 *empty* dialog templates, 12 string blocks, 1 accelerator table, 409 RT_BITMAP, 71 RT_PNG. **No ribbon XML**: the ribbon, panels and dialogs are all built in code. |
| `MainApp.exe` .rdata UTF-16 literals (`work/ui/mainapp.str.txt`, 47 839 strings) | Every UI string is fetched as `("语言包", "<key>")`. The literal order mirrors construction order (ribbon, status bar, parameter registry). |
| `$ORIG/Lang/lang.txt` (UTF-16, 3 668 lines, `key#中文#English`) | Complete string catalogue. Other languages are `key#text` files. `lang.ini` `Lang=` holds the index 0 zh, 1 en, 2 ru, 3 de, 4 es, 5 pt, 6 fr, 7 it, 8 pl, 9 vi, 10 tr. |
| `decomp/MainApp.c` (Ghidra) | Ribbon categories FUN_004a7d20 / FUN_004aa8c0 / FUN_004ad0f0, status bar panes (~line 104400), view key handler FUN_004d0480, layer colours FUN_004473c0, layer naming FUN_0057dc70. The big "Start/Common/Process/Draw" builder was not in the dump, so their layout comes from the string order (observed) plus the tab tiles (observed bitmaps). |
| Module DLLs (CAD/NC/Para/Control/Lang/Log) | No UI resources (only manifests). CADModule = OpenGL view + G-code/DXF/CHF parsing; NCModule = parameter registry & process; ParaModule = XML params. |
| `$ORIG/Report/report.exe` (Qt5) | Processing report tool (see 9.1). |
| `$ORIG/Bitmap/next_cut.dll` (.NET 8, REA `inspect-managed-members`) | "PicConvert" raster-engraving tool (see 9.2). |
| `$ORIG/File/IPSet.exe` | Sets the PC NIC to a static 10.1.1.x address via `netsh` (mask 255.255.255.0, gw 10.1.1.1). |

Key-family cheat sheet (lang.txt prefixes): `mf` main frame / ribbon (409), `mp` machine panel &
status (200), `pd` parameter labels `Group.Name` (1 291), `gp` alarms & misc (162), `hp`
hardware-config dialog (74), `mv` view context menu & canvas messages (37), `lp` layer
parameters (20), `slp` process-flow editor, `ss` IO debug, `zf` FTC (height follower) monitor
(92), `af` auto-focus monitor, `ec` extended card monitor, `cp` pitch compensation, `de`
tooltips, `db`/`da` technology (process) database, `np`/`nset`/`pd_Nest` nesting, `SBT`/`SLED`
the dark-theme big-button panel and dashboard, `A2xxxxx_n` features added 2024-2026 (date-coded),
`RegName*`/`*RegName*`/`RTC_*`/`ZFRead*` register names in diagnostics pages, `EtherCAT*` bus
axis config/errors, `ab`/`ls`/`dog*` activation/licence (omitted, see 11).

## 1. Main window layout

```
+--------------------------------------------------------------------------------------+
| [File v] Start | Common | Process | Draw | System Analysis | Advanced | Nest   [lang] |  ribbon (BCGP)
| [group][group][group] ... (32px large buttons + 16px small buttons in 3-row columns)   |
+---------------+----------------------------------------------------+-----------------+
| Layer | Part  |                                                    | Run Control     |
| colour row    |               drawing canvas (OpenGL)              |  jog / IO /     |
| layer table   |   machine table, origin, contours by layer colour, |  start pause    |
| [Layer Params]|   start points, order numbers, rapids, head marker |  stop frame ... |
+---------------+----------------------------------------------------+-----------------+
| Log | Alarm                                                                          |
+--------------------------------------------------------------------------------------+
| MC: MCC <Ready> | ZF: FTC <Ready> | LF: Laser <Ready> | XY: X Y Z | CV: speed | PD | DA: mouse X Y |
```

* Ribbon: `CBCGPRibbonBar` (observed `AddCategory`/`AddPanel`/`CBCGPRibbonButton`, ribbon sliders
  for zoom). Application button = "File" (`mf7`).
* Docking panes (inferred placement, observed names): run control `mf141`, run params `mf142`, nest
  views `np_Part`/`np_Sheet`/`np_Result`, technology library `A250418_0`, log `mf124`.
* Status bar panes (observed MainApp.c ~104400 `CBCGPRibbonStatusBar::AddElement`): `MC` controller
  state (`mf143` "MCC <Ready>"), `ZF` follower (`mf144`), `LF` laser (`mf145`), `XY` machine
  position "X: 99999.000  Y: 99999.000  Z: 99999.000" (id 0x80ed), `CV` current speed (`mf146` /
  `ZFReadOnly08`), `PD` ping/focus (`mf430` "Ping", `mf433` "Focus Pos(mm)"), extended `DA` canvas
  cursor "X: 99999.000 Y: 99999.000" (id 0x80ec). Remote monitor state `mf242`/`mf243`.
* Themes: `Theme01` DARK / light (`pd379`, `pd672_1`). The dark theme uses big tile tabs
  (RT_BITMAP 458-466/489-501: 开始 Start, 绘图 Draw, 工艺 Process, 高级 Advanced, 排样 Nest,
  控制器 Controller, 调高器 FTC, 电动头 AF head, 扩展板 Extended card), a big-button control
  panel (`SBT01..SBT42`, bitmaps 661-858) and a dashboard (`SLED01..11`, bitmaps 471-484:
  speed, X/Y/Z, laser power, focus, gas pressure, temperature, length sum, process time,
  pierce count). `pd862` "UI Direction" mirrors the layout (inferred).

Rebuild: `mlaser/ui/mainwindow.py` (`MainWindow`), `ribbon.py` + `ribbon_spec.py`, `canvas.py`,
`panels.py`, `dialogs.py`, status bar in `MainWindow._status_bar`.

## 2. Ribbon (tabs, groups, buttons)

Order and keys are **observed** (literal order in .rdata 0x45681c..0x459118); group-to-tab split
is **inferred** except where a decompiled builder exists. Priority: P0 must-have, P1 next,
P2 later. Button icons: strips 348/350 (Start), 351/352 (CAD tabs), 353/354 (System analysis,
observed `AddCategory(..,0x161,0x162)` in FUN_004a7d20), 355/356 (Advanced), 441/442, 451/452
(Nest).

### 2.1 File (application button, `mf7`)
| Key | Text | Pri | Rebuild |
|---|---|---|---|
| mf8 / mf9 / mf13 | New / Open / Import (merge) | P0 | `file.new/open/import` -> `mlaser.formats.chf.load_chf`, `mlaser.io.dxf_import.import_dxf` (lazy) |
| mf10 / mf11 | Save / Save as (`.chf`) | P0 | `mlaser.formats.chf.save_chf` (lazy) |
| A250622_0 | Bitmap (launches `Bitmap\Bitmap.exe`, imports `newGcode.nc`/`newSVG.svg`) | P2 | not implemented |
| A251104_0 | Import scan G-code | P2 | – |
| A250320_0..2 | Task / Import task / Save task (`*.aut`, bundles graph+layers+breakpoint) | P2 | – |
| mf149 | open filter: `*.dxf;*.chf;*.nc;*.txt;*.cnc;*.g;*.plt;*.svg` (100 MB limit `mf154`) | P0 | `MainWindow.FILTER` |
| mf148 | "Graphics have been changed, save?" | P1 | – |

### 2.2 Start (`mf6`)
| Group | Buttons (key) | Pri |
|---|---|---|
| File `mf7` | New, Open, Import, Save, Save as, Bitmap | P0 |
| Initialize `mf14` | **Go Origin `mf15`** v [ZF then XY `mf482`, XYZ `mf222`, Only X `mf16`, Only Y `mf17`, AF focus origin `A240827_1`, dual-servo `mf17_1`, 4th axis `mf17_2`]; Edge Seek `mf19` v [disc `newLang27`, capacitive `A250211_0`, two-point `A250211_1`, memorise/clear/recover angle `A250211_2..5`]; Rotation axis `A251202_1`; Lifting platform `A250410_2`; Laser type `gp120` v [Fiber `A241024_0`, CO2 `A241024_1`] | P0 (home), P1 (edge seek), P2 |
| Coordinate `mf307` | Float `mf300`, Tool 1..6 `mf301..306`, Manual `mf513`; SetToolOrigin `mp129`, ReturnToolOrigin `mp130` | P1 |
| Backup `mf22` | Parameters backup `mf210` v [to hardware `mf480`, to file `mf234`]; import `mf211` v [from hardware `mf481`, from file `mf235`] | P1 |
| Help `mf23` | Options `mf137`, Help `mf23`, About `mf24` | P1 |

### 2.3 Common (`mf26_1`)
| Group | Buttons | Pri |
|---|---|---|
| View `mf26_2` | Contour Select `mf27`, Vertex Edit `mf28`, View Move `mf29`, Zoom In/Out/Reset `mf198/199/200`, Undo `mf30`, Redo `mf31` | P0 (select/pan/zoom), P1 undo |
| Select `mf32` | All `mf33`, Invert `mf34`, Unclosed `mf35`, Smaller-than `mf36`, Similar `mf37`, Inner `mf651`, Outer `mf652` | P0 all/invert, P1 rest |
| Show `mf38_0` | Box `mf38`, Unclosed red `mf39`, Index `mf40`, Path start `mf41`, Direction arrows `mf42`, Move path `mf43` | P0 |
| Geometric transformations `mf590` | Size `mf591` (presets 10/30/50/75/200/500 %, 10/50/100/200 mm), Align `mf70` (`mf71..77`), Mirror H/V `mf68/69`, Rotate 90 CW/CCW/180/any `mf62/63/63_1/66` | P1 |

### 2.4 Process (`mf186`; panel "Basic Process" `mf860`)
| Group | Buttons | Pri |
|---|---|---|
| Lead `mf87` | Lead line (dialog `mf205`), manual `mf88`, check `mf89`, clear error mark `mf91`, clear `mf90`; Path start `mf93` (start point); Home ref/dock `mf92` | **P0** lead; P1 rest |
| Compensate `mf113` | Set `mf486` / cancel `mf217`; Inside `mf98` (阴切) / Outside `mf99` (阳切); Circle cut `mf487`; α-corner add/clear `mf485/mf403` | **P0** kerf |
| Micro joint `mf100` | Manual `mf101`, Auto `mf103`, params `mf102`/`mf191`, explode `mf103_1`, clear `mf103_2`; Reverse `mf94`; Seal `mf96`, Gap `mf95`, Over-cut `mf97`; Cool point `mf406` (auto `mf407`, clear `mf408`) | P1 |
| Corners/geometry | Unload `mf404`, Chamfer `mf400` (manual `mf484`, auto `mf401`), Merge `mf84`, Explode `mf85`, Split `mf216`, Smooth `mf213` | P2 |
| Sort `mf105` | small first `mf204`, L->R `mf106`, R->L `mf107`, B->T `mf108`, T->B `mf109`, nearest `mf110`, in->out `mf202`, out->in `mf203`, selected layer first/last `mf610/611`, Manual sort `mf111` | P1 |
| Tools `mf790` | Array `mf79`, Group `mf80` (`mf81..83`), Scan (flying) cutting `mf112` (circle `mf112_1`, line `mf112_2`, engraving `mf112_5`), Share edge `mf250`/`mf250_1`, Bridge `mf114`, Measurement `mf116`, Optimize `mf470` (smooth `mf474`, segment `mf475`, dedup `mf471`, join `mf476`, replace circle `mf472`, adjust start `mf604`, point->circle `mf605`), OneKey planning `mf196` (`pd476..481`: mirror H, mirror V, optimize, sort, lead, micro-joint) | P1 array/optimize, P2 rest |

### 2.5 Draw (`mf4401`, group Graph `mf4402`)
Line `mf45`, Pline `mf56`, Rect `mf51`, Rounded rect `mf52`, Polygon `mf53`, Star `mf54`,
Circle `mf46`, 3-point circle `mf47`, 3-point arc `mf48`, Scan arc `mf49`, Ellipse `mf50`, Text
`mf55` (font `gp77`, height `ti0`), Curve `mf57`, Point `mf58`; Trim/Extend `A250707_12/13`;
Advert text `mf254`, Adv text `mf251`, QR code `A260420_0`, Linear fill `A250707_10`, Fill circle
`mf100_1`; keyboard Fine-tuning `mf445` (step `mf447`, auto step `mf446`). **P2** (P1: text).

### 2.6 System Analysis (`mf117`; observed builder FUN_004a7d20, strips 353/354)
Monitor `mf118`: Controller `mf119`, FTC `mf120`, Auto focus `mf249`, Extended card `mf620`;
Simulation speed `mf121` (slider); Diagnostics `mf125`: Log `mf124` (open dir `mf431`), Device
report `mf801` (`pd880..888` usage counters), Data measurement `mf229`, Work report new/all
`mf252/253` (launches Report\report.exe). Alternative category `pd312` "Process" (same builder,
other branch): Offline process `A241115_1`, Simu `SBT05`, Stop `ec13`, Device connection
`A241218_0`, File upload `A250529_0`. **P1** (monitor), P2 rest.

### 2.7 Advanced (`mf127`; builder FUN_004aa8c0, 5 panels)
Hardware `mf128`: Options `mf129` (hardware config, section 5.3), Reconnect `mf130`, Restart
`mf131`, Upgrade `mf132` (`*.mcf`; FTC `mf440` `*.zfb`; AF `*.afb`; ext card `mf625` `*.efb`),
Set (PC) IP `mf133` (IPSet.exe), Network test `mf436`, Access config `mf134`, Remoter match
`mf135` (`jm0..3`). Software `mf136`: Options `mf137`, System recovery `mf138`. Auxiliary
`mf139`: Laser interferometer `mf180`, Ball arm `mf702`, Burn-in test `newLang1..24`, Light test
`newLang200..206`, Batch cut `mf705`, Maintain `A251113_0`, Custom IO `newLang300/301`, Dedust
`mf140/170/171`. P1 reconnect/IP/options; P2 rest.

### 2.8 Nest (`mf460`; builder FUN_004ad0f0, 4 panels)
Implemented in `mlaser/ui/ext_nest.py`; engine, parameters and file format: [nest.md](nest.md).
New/Save/Save-as project (`*.nestproj`), Import part `mf461`, Import sheet `mf473`, Simple nest
`mf463`, Advanced nest `mf464` (params `mf412`, `pd_Nest_*`, `pd766..769`), Add part/sheet
`mf465/466`, copy part/sheet/result to main view `nset_UI_*`, export to sheets `mf420`. Uses
SmartNest.dll/AutoNest.dll (`Nest_*` exports in CADModule). **P2**.

## 3. Side panels

### 3.1 Layer panel (`lp0` "Layer Parameters", `gp83` "Layer")
* **11 layer records** per laser type (observed: `_eh_vector_constructor_iterator_(cfg+0x4ed8, 0x480, 0xb)`
  for fiber and `cfg+0x8058` for CO2, MainApp.c ~40100; selected by `cfg+0x4728` laser type,
  FUN_00458c60). Records are copied into CADModule's layer vector (MainApp.c ~133690) whose
  colour drives `glColor3d` (CADModule.c ~84115).
* **Layer colours** (observed FUN_004473c0, doubles in .rdata 0x833e50.. 0x843700), index 0..10:
  `#00FC00 #00FCFC #FC80FC #FCFC00 #00A0E8 #6E91BE #96DCE6 #B47855 #ED1C24 #999999 #E6E6E6`
  (saved as `mlaser/ui/resources/layer_colors.json`). Names: index 0 "BackLayer"
  (`gp81` Bk layer), others "Layer %d" / "Engrave %d" when the layer is an engraving layer
  (`A250728_1`) (observed FUN_0057dc70; exact index offset inferred).
* Grid columns (inferred from keys next to the layer grid code: `gp79..83`, `lp2` Power, `lp3`
  Freq, `lp4` Notes, `lp6` Film layer): layer, speed, power, frequency, gas, pressure, height,
  plus per-layer flags `pd112..124`: Uncut, Short unlift, Unfollow, PreDrill, With film, Keep gas
  on, Direct cut, Section/Gradual/3-stage drill, Fix height, Dynamic power.
* "Layer parameters" dialog = property grid of ~160 `GP.*` keys (section `LayerParam`, see
  `resources/params.json`): Cut Basic (speed `pd125`, height `pd129`, focus `pd589`), Cut Laser
  (power `pd127`, freq `pd128`, peak current `pd132`), Cut Gas (type `pd130`, pressure `pd131`),
  Delay (laser on `pd126`, before/after off `pd137/138`), Adv (slow start `pd133..135`, up height
  `pd136`), first..fifth drill stages (height/focus/power/freq/current/gas/time/gradual), smooth
  pierce, bolt drill, clean residue, start/end segment, ZF vibration abatement, graph shift,
  power/frequency curves vs speed. Import/export layer params (`lp7..lp11`, `hp9` *.xml),
  validation messages `lp16..19`. Technology library (`A250418_*`, `db0..9`, `da0..6`).
* Rebuild: `panels.LayerPanel` (colour buttons assign selection to a layer; editable table of the
  core fields), `dialogs.LayerParamDialog` (full GP grid, core fields mapped to
  `model.LayerParams` via `params.LAYER_FIELD_MAP`, rest kept in `LayerParams.extra['gp']`). **P0**.

### 3.2 Part list
Nest views list parts/sheets/results (`np_PartName`, `np_PartNum`, `np_SheetName`, `sf0..2`).
The main view has no separate part tree in the original (inferred); the rebuild adds
`panels.PartListPanel` (index `gp75`, name, layer, contour count `gp76`, size `sf1`) with
two-way selection sync. P1.

### 3.3 Run control panel (`mf141`; keys `mp*`, bitmaps 467-659 light / 661-858 dark)
Observed button bitmaps (57x57, normal / hover / pressed-orange / disabled sets) labelled:
设置 settings, 润滑 lube, 除尘 dedust, 锁定 lock (`pi0..2` unlock password), 光闸 shutter, 吹气 blow,
快速 fast, 步进 step, 红光 red light, 跟随 follow, 标定 calibrate, 回原 origin, 交换 exchange,
W+/W-, ▲▼◀▶ (Y/X jog), Z+/Z-, 回零 return zero, 标记点 mark point, 添加标记 add mark, 返回标记 return
mark, 坐标系 coordinate, 设置原点 set origin, 工件回原 tool origin, 循环加工 loop, 自动交换 auto
exchange, 选中加工 selected only, 开始 start, 暂停 pause, 停止 stop, 空走 dry run, 边框 frame,
模拟 simulate, 断点 breakpoint, 前进/后退 forward/backward, 继续 continue, Fn1/Fn2 custom DO,
Z回原 Z origin, 交换回原 exchange origin, 激光 laser.

| Function | Keys | Pri | Facade method |
|---|---|---|---|
| Connect / reconnect | `mf130`, `mf0/mf5`, `mf165/166` | P0 | `connect()` |
| Home (XY, X, Y, Z, XYZ) | `mp29`, `mf15..18`, `mf222`, `mp30` | P0 | `home(axes)` |
| Jog ±X ±Y ±Z (±W), step/fast, step list 0.05..200 mm | `mp28`, `mp31/32`, `mp90..99`, `pd81..83` | P0 | `jog_start/jog_stop/jog_step` |
| Follow, gas (Air/O2/N2/high), shutter, red light, laser + power % | `mp41..51`, `mp53` | P0/P1 | `set_output`, `set_gas_type`, `set_laser_power` |
| Start / pause / continue / stop | `mp54/55/27/56` | P0 | `start/pause/resume/stop` |
| Frame (`mp58`, speed `pd528`) / Dry cut (`mp57`) | | P0 | `frame/dry_run` |
| Simulate (`mp59`, speed `mf121`) | | P0 | canvas only |
| Breakpoint / backward / forward / BF length & speed | `mp60..65`, `pd84/85` | P1 | `step_backward/forward` |
| Only selected (`mp52`, `pd79`), loop (`mp171`, `pd80`, `pd340..342`) | | P1 | `start(selected_only)` |
| Return after done: dock/start/end/origin (`mp67..71`, `pd86`), return to dock on stop `mp72`, soft limit `mp73` | | P1 | – |
| Mark points P1..P6 (`mp33..39`, `mp88`, `mp390/391`), position-to (`mp111..113`) | | P1 | `move_to` |
| Coordinate systems float/tool1-6/manual (`mp200..207`), set/return origin (`mp163/164`) | | P1 | `set_origin/go_origin_point` |
| Clear alarm (`mf21`, `gp90`) | | P0 | `clear_alarms` |
| Exchange platform / roll sheet / rotate platform (`mp166..187`, `pe0..8`, `rsd1..3`) | | P2 | – |

Rebuild: `panels.ControlPanel` using text-free jog/process bitmaps (720-731) and translated text
buttons; wired to `facade.ControllerFacade`.

### 3.4 Log / alarm
Log (`mf124`, log files in `Log\`, `pd526/527` general/debug log), alarm list (`mf123`; columns
time `gp84`, description `gp85`, message `gp86`, status `gp93` cleared `gp94`; text `gp88` "Alarm:
%s %s"). Alarm catalogue: servo/encoder/limit/double-drive per axis `gp1..gp39`, FTC `gp40..45/57/59`,
laser `gp44..58`, auto focus `gp100..115`, extended card `gp200..218`, EtherCAT
`EtherCATErrorInfo*`/`EtherCATAxisErrorInfo*`, custom alarms (`hp45..50`). Alarm panel colours
`pd671/672`, LED/buzzer twinkle `pd673..678`. Rebuild: `panels.LogPanel` (Log + Alarm tabs). P0.

## 4. Dialogs

Only 7 dialog templates exist and all are empty shells (RT_DIALOG 319, 331, 393, 443, 444, 448, 459,
"MS Shell Dlg"); controls are created at run time, mostly BCGP property grids whose rows are the
`pd*` labels (`Group.Name`) and whose storage keys are the `XX.Name` names recovered into
`resources/params.json` (~860 entries; sections GraphParam 187, ManuParam 178, LayerParam 161,
SoftParam 71, DIParam 64, DOParam 41, LaserParam, MachineAxisConfig, ImportGraphParam, GasParam,
ZFParam, AFParam, NestParam, FCParam, AxisParam, HomeParam, DAParam, ...).

| Dialog | Keys | Content | Pri | Rebuild |
|---|---|---|---|---|
| Layer parameters | `lp0..19`, `pd125..155`, `GP.*` | see 3.1 | P0 | `LayerParamDialog` |
| Lead line params | `mf205`, `pd315..322`, `pd599` | type (none/line/arc/line+arc), angle, length, radius, start-point mode/position, only closed, only selected, outer-as-inner | P0 | `LeadDialog` (lead in/out tabs -> `Craft.lead_in/out`) |
| Compensate (kerf) | `mf486`, `pd506/507`, `ap12` | type inside/outside, width | P0 | `KerfDialog` -> `Craft.kerf_comp/kerf_width` |
| Micro joint | `mf191`/`mf102`, `pd313_0`, `pd314_0/1`, `pd325`, `pd343..346`, `pd594/595*` | type, count, distance, length, direction, min size; manual length | P1 | `MicroJointDialog` (values not yet applied) |
| Gap/seal | `pd323/324` | gap length, overcut length | P1 | – |
| Array | `pd332..338` | rows, cols, offset type, spacing, directions | P1 | `ArrayDialog` |
| Rotate / scale | `pd339`, `pd348..350`, `pd659` | angle; percent, W, H, ratio lock | P1 | `ValueDialog` |
| Sort params | `mf190`, `pd465/466`, `pd372`, `pd707` | sort type, small first | P1 | sort menu |
| Optimize / import params | `mf189`, `pd363..374`, `pd369_1`, `pd532*` | remove tiny, dedup, join, smooth, explode DXF groups, text->curves, split long | P1 | via Options |
| View params | `pd326..331` | show box/unclosed/index/start/direction/move | P0 | Show toggles |
| Software options (`mf137`) | `gv0/gv1`, `pd375..379`, `pd468`, `pd516/517`, `pd529/530`, `pd630`, `pd671/672`, `pd862`, `A250419_3` | language, theme, auto-load last file, require homing, keyboard step, colours | P1 | `ParamGridDialog` (SoftParam, GraphParam, ImportGraphParam, ManuParam) |
| Run/manu params (`mf142`) | `pd74..95`, `pd491..493`, `pd528`, `pd661` | jog speeds/steps, move/cut acceleration, spline/corner precision, frame speed | P0 | Options |
| Hardware config (`mf129`) | `hp0..104` tabs: Axis `hp2/16` (`pd197..247` per axis: enable, index, double drive, encoder reverse, limit type, length, pulse equivalent, max speed/acc, home dir/offset), IO `hp6/17` (DI `pd251..290`, DO `pd255..282`, custom DO/DI `hp39/64`), Laser `hp3/30` (type `pd258`, PWM/DA `pd260/261`), FTC `hp4/31`, Gas `hp5/32` (valves `pd266..278`, proportional `pd272..275`, pressure map `pd1600..1606`), Go-origin `hp29` (`pd239..243`), Correction `hp18..24` (verticality AB/AC), Pitch compensate `hp25..28` (`cp0..33`, files rtl/ren/pos/csv), Auto focus `hp55/56`, Extended card `hp70..73`, Advanced `hp33`, Debug `hp35` | P0 axis basics, P1 rest | `ParamGridDialog` (hardware sections) |
| IP settings | `pd445..453`, `mf133`, `mf437` | controller/FTC/laser IP, mask, gateway; PC NIC via IPSet.exe | P0 | `IPDialog` |
| Access config | `mf134`, `is0/is1` | controller access params (restarts hardware) | P2 | – |
| Alarm list | `mf123`, `gp84..94` | | P0 | `LogPanel` alarm tab, `AlarmDialog` |
| FTC monitor & follower calibration | `mf120`, `zf0..107`, `ZFReadOnly*`, `ZFReadWrite*`, `pd426..443`, `pd542..565`, `pd729..742` | info (model, versions, DI/DO, alarm, run state), refresh/write params, head calibration `zf24/26/28` (quality Excellent/Good/Bad `zf20..23`), servo calibration `zf29..31` (needs >20 mm), restart/reconnect `zf33/34`, reset with password `zf56..59`, import/export `zf102..107` | P1 | – |
| Controller monitor | `mf119`, `RegName*`, `RORegName_*`, `RWRegName_*`, `SystemRWRegName_*`, `ss0..24` | register views, DI/DO toggles, axis pulse send, DA/PWM test; ReadOnly `ss9` vs Read&Write `ss10` needs password `ss11` (Advanced mode `ss12`) | P1 | – |
| Auto-focus monitor | `mf249`, `af0..30_5`, `RTC_RO_*`, `RTC_RW_*`, `pdAFDA_*` | focus pos, gas pressure, temps, LED colour, step up/down, set origin | P2 | – |
| Extended card / EtherCAT | `mf620`, `ec0..30`, `EtherAxisInfos_*`, `AllAxisInfo*`, `GoHomeAdv*` | | P2 | – |
| Dual servo calib | `mf21_1/21_2`, `mf900..903`, `pd2112/2113` | | P2 | – |
| Edge seek | `es0..4`, `A250212_*`, `pd2108..2115` | mark P1/P2 with red light -> rotation angle; capacitive 3/4-point auto edge | P1 | – |
| Process flow editor | `mf603`, `slp0..19`, `pd910..917` | insert before/after: start manu, empty move, wait DI, open/close DO, delay, loop, go origin, jog, platform change | P2 | – |
| Gas pressure calibration | `hp80`, `gpa0..13` | DA voltage vs bar table | P2 | – |
| Pitch compensation | `cp0..33`, `cpd0/1` | | P2 | – |
| Burn-in / light test / ball arm / interferometer | `newLang1..24`, `newLang200..206`, `pd1100..1106`, `pd459..462` | | P2 | – |
| Device report | `mf801`, `pd880..888`, `ap20..22` | usage counters, reset | P2 | – |
| Password / lock | `mf162`, `pi0..2`, `cd0`, `A250416_0` Login, `A250417_0/1` company password, start with operator privileges | operator vs. full access (two levels inferred) | P1 | `PasswordDialog` stub |
| Technology library | `A250418_*`, `A250419_*`, `db0..9`, `da0..6` | named process sets per material | P1 | – |
| Nest dialogs | `pd_Nest_*`, `mf412`, `pd766..769`, `np_*` | | P2 | – |

## 5. Keyboard and mouse

| Input | Action | Evidence |
|---|---|---|
| Ctrl+N / Ctrl+O / Ctrl+S | New / Open / Save | RT_ACCELERATOR 128 (observed) |
| Ctrl+Z, Alt+Backspace / Ctrl+Y | Undo / Redo | accel 128; FUN_004d0480 (0x5A, 0x59 with LCtrl) |
| Ctrl+C / Ctrl+V (Ctrl+Ins / Shift+Ins), Ctrl+X / Shift+Del | Copy / Paste / Cut | accel 128; FUN_004d0480 (0x43, 0x56). Paste places via menu 134 (确认 OK, 取消 Cancel, 闭合 close, 直线 line, 圆弧 arc) |
| Ctrl+A | Select all | FUN_004d0480 (0x41) |
| Delete | Delete selection | FUN_004d0480 (0x2E) |
| Esc | Cancel current command, deselect | FUN_004d0480 (0x1B) |
| Arrow keys | Nudge selection by step (`mf445`, `pd376/377`) | FUN_004d0480 (0x25..0x28 -> FUN_004d0930) |
| Shift (held) / Ctrl | Multi-select | FUN_004d0480 (0x10 flag), `np_CtrlShiftSelTip` |
| F6 / Shift+F6 | Next / previous pane | accel 128 (MFC default) |
| Wheel | Zoom about cursor | inferred |
| Middle drag, or `mf29` mode + left drag | Pan | inferred |
| Left drag | Rubber-band selection (L->R window, R->L crossing) | inferred |
| Right click | Context menu: from this point continue `mv0`, reset view `mv1`, copy/paste/delete `mv7..9`, clear track `mv11`, previous step/first/last process `mv13..16`, text->spline `mv33`, select same layer `mv34`, apply craft to same contour `mv35` | lang `mv*` (order inferred) |
| Remote pendant | `xhcusb.txt`, `pd292..295`, `jm0..3` | P2 |

Rebuild: `MainWindow._shortcuts` and `_context_menu`; `F` = zoom fit and Ctrl+I = import are additions.

## 6. Canvas behaviour

* Engine: OpenGL in CADModule (`glBegin/glVertex2d/glColor3d/glLineStipple/glCallLists`,
  `wglUseFontBitmapsW` for numbers). Background colour `SP.DrawblkColor` (`A250419_3`).
* Machine range drawn; loading graphics larger than the range fails (`mv31`), processing out of
  range warns (`gp1000`, `mp120`).
* Colours by layer (3.1); unclosed contours red (`mf39`); selection highlighted (dashed in the
  rebuild; original style not confirmed).
* Overlays: bounding box `mf38`, order numbers `mf40`, start point `mf41`, direction arrows
  `mf42`, rapid moves `mf43` (dashed via glLineStipple), lead lines, micro-joint gaps, cool
  points, kerf-offset path.
* Measurement result `mv17` "X: Y: Length:"; manual sort `mv18..23`; draw tools `mv2..6`.
* Simulation (`mp59`): head moves along the ordered path at `mf121` speed, track drawn (cleared by
  `mv11` / `pd105`); estimate dialog `mp83`/`mp86`/`gp2000/2001` (time, cut length, move length,
  pierce count). Real processing draws the live head position and the cut track.
* Breakpoint: `mv0` "from this point continue", `mp122`, `mf231`.

Rebuild: `canvas.CanvasView` (QGraphicsView, Y up, cosmetic pens, `pick()` nearest-segment hit
test with 5 px tolerance, window/crossing rubber band, overlays, head marker, `start_simulation`).

## 7. Assets extracted (`mlaser/ui/resources/`)

| Path | Source |
|---|---|
| `icons/strips/strip_<id>.png` | RT_PNG 346..452: ribbon image strips (32 px: 348, 351, 353, 355, 441, 451; 16 px: 350, 352, 354, 356, 442, 452; 394 misc) |
| `icons/buttons/btn_<id>.png` | RT_BITMAP 467..1457 57x57/62x57 control-panel buttons (light: normal/hover/pressed/disabled; dark: 661..858) |
| `icons/misc/` | other PNG/BMP: dashboard tiles 458..506, edge-seek/verticality diagrams 407/445/447/42886/42887, gauges 449/455, small icons |
| `layer_colors.json` | layer colour table (3.1) |
| `params.json` | parameter registry (4) |
| `lang/*.txt` | all 11 language files (UTF-16) |

Not copied (vendor branding): splash/logo pictures RT_BITMAP 472 and 42885, `res/splash.bmp`,
`File/help.bmp`, `File/logo_1.bmp`, app icon.

## 8. Rebuild skeleton (`python -m mlaser.ui [--demo] [--lang zh] [file]`)

* `i18n.py` Translator (lang.txt + per-language files, live `bind()` re-labelling, English fallback).
* `ribbon_spec.py` / `ribbon.py` data-driven ribbon; every button is a `QAction` keyed by a
  command id (`file.open`, `machine.home`, `craft.lead`, ...).
* `canvas.py`, `panels.py`, `dialogs.py`, `params.py`, `docops.py` (simple transforms, align,
  array, sort fallback, layer/lead/kerf assignment, demo document).
* `facade.py` `ControllerFacade` (abstract) + `DemoController` (time-integrated simulation) +
  `make_controller()` hook expecting `mlaser.controller.ui_adapter.UiController`.
* Lazy imports: `mlaser.formats.chf.load_chf/save_chf`, `mlaser.io.dxf_import.import_dxf`,
  optional `mlaser.io.svg_import.import_svg`, `mlaser.io.gcode_import.import_gcode`; if missing,
  the UI logs that the importer is not available.
* Tests: `tests/test_ui.py` (offscreen; screenshot -> `work/ui/screenshot.png`).

## 9. Companion programs

### 9.1 Report\report.exe (Qt 5, 100 KB)
Reads `Report\report.txt` (current job: `name,WxH mm,start time,cut length m,move length m,
pierce count,duration,cut s,move s,pierce s`), `LogReport.txt` (one line per finished job:
timestamp, name, size, cut length, move length, pierces, duration, start time) and
`TotalReport.txt` (history), plus `lang.txt` (`lang==0`) and a job thumbnail JPG
(`_tempthumbnail.jpg`). Shows "Processing Report"/"生成加工报告单" with a statistics & billing section
("统计&计费": unit price, cost calculation, cumulative move length, pierce count, cut length,
processing time; "Total Cost"), print preview (QPrintPreviewDialog) and Save PDF (QPdfWriter).
MainApp writes these files after a job (strings `\Report\LogReport.txt`, `,%.2fx%.2f mm`, `%Y-%m-%d
%H:%M:%S`) and launches it from `mf252/mf253`. Rebuild: P2 (a Qt print/PDF report is easy).

### 9.2 Bitmap\Bitmap.exe = next_cut.dll "PicConvert(1.06) (BitmapRaster)" (.NET 8 WinForms + OpenCvSharp)
Not a vector tracer: a **raster-engraving G-code generator**. Form `MyFrom`: Open image
(png/jpg), image size in pixels, brightness and contrast trackbars, Binarization, Invert, Gaussian
blur, Flip H/V, Crop (`KJNova_Clipper` with 8 resize handles), Cancel editing, bitmap processing
mode {not enabled, 1-bit dithering (Floyd-Steinberg), half-tone screen}, drawing size W/H in mm
(aspect locked), line density (`SetRho`, default 6 lines/mm), laser power (`SetLaserPower`,
default 20), language zh/en. "Generate GCode" writes `Bitmap\newGcode.nc`: header comments,
`G90`, `M4`, then per raster line `G0 X Y S0` / `G1 X.. Y.. S<power>` run-length segments
(`RasterSegment{Pixel, Power}`), `G0 X0 Y0 S0`, `M5`; status via `Bitmap\status.ini`. MainApp
then imports `newGcode.nc` (or `newSVG.svg`). Rebuild: P2.

## 10. Priorities summary

* **P0**: open/import DXF & CHF, save CHF; canvas view/zoom/pan/select; show options; layer
  assignment and layer params (speed/power/freq/gas/height); lead in/out; kerf; simulate;
  connect/reconnect; home; jog (step/continuous, fast); frame; dry run; start/pause/resume/stop;
  outputs (gas, shutter, red light, follow, laser); status bar; log/alarms; IP settings;
  language switch.
* **P1**: undo/redo, selection filters, transforms/align/array, sort, micro-joints, gaps,
  cool points, optimize, run/manu params, hardware config, FTC monitor & calibration, controller
  monitor, coordinates/marks, breakpoint resume, edge seek, technology library, password levels,
  text drawing.
* **P2**: drawing tools, flying/scan cut, share edge, bridges, nesting, process-flow editor,
  platforms/roll sheet, EtherCAT/extended card, pitch compensation, test tools, reports,
  bitmap engraving, offline/cloud.

## 11. Intentionally omitted

Licensing/activation (`ab*`, `ls*`, `dog*`, `dogActiveReslut*`, `mf228`, `mf496`), laser unlock
codes (`mf490..495`, `gp121/122`), hardware-ID checks, vendor fixed passwords, cloud/remote
monitor phone-home (`pd518..534`, `pd870..873`, `mf225/226`, `mf242/243`, HTTP
`/NexCut/File/LoadFile`, `47.104.17.21:9001`), online/offline account login (`A250416_*`).

## 12. Open unknowns

* Exact tab membership/order of the Start/Common/Process/Draw categories (their builder was not
  decompiled; current split follows string order + tiles). Icon indices are visual matches.
* Whether layer index 0 is the background layer or "Layer 1" (two code paths, FUN_0057dc70).
* Real default values/ranges of most parameters (`params.json` `hint` is unverified; ranges
  exist in `pp0/pp1` messages and must come from NCModule/ParaModule code).
* Default canvas background and selection style; original rubber-band semantics.
* Control panel placement (left/right) and which panes are docked by default.
* Exact enum values for lead type, kerf type, micro-joint type, sort type (shared with
  `docs/cam.md` / `docs/formats.md`).
