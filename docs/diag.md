# Monitoring, diagnostics, reports and auxiliary functions

Phase 3, agent I. Code: `mlaser/diag/`, `mlaser/report/`, UI `mlaser/ui/ext_diag.py`
(`register(window)`). Tests: `tests/test_diag_*.py`, `tests/test_report_*.py`.

Tags: **O** observed (address given; MainApp.exe unless another module is named, NC =
NCModule.dll, CAD = CADModule.dll, report.exe = `Report/report.exe`), **I** inferred,
**U** unknown. Command ids are the MFC command ids of the ribbon buttons.

## 0. Method

* Ribbon button ids from the builders FUN_004a7d20 (System Analysis) and FUN_004aa8c0
  (Advanced); button order matched with the lang-key order in .rdata 0x8592dc..0x85972c.
* Handlers from the dynamically initialised message maps (`_DAT_009ce…` blocks around
  MainApp.c line 99130..99640: `{0x111, code, id, id_last, sig, pfn}`; extracted with
  `work/diag/scripts/dynmap.py`).
* Parameters from the descriptor table (`mlaser/formats/data/mlaser_params.json`, struct
  offsets), used through `mlaser.diag.paramset.ParamSet` so every dialog shows the original
  parameter names, defaults, ranges and enum options.
* report.exe was decompiled with the REA CLI (Ghidra 12.1.4); its UTF-8/GBK strings were
  read from .rdata.
* `objdump` searches for `[reg+offset]` displacements decided whether a flag has readers.

## 1. Command map

| command | key | id | handler (O) | rebuild |
|---|---|---|---|---|
| mon.controller | mf119 | 0x36b1 | FUN_004b8500 → view FUN_004b7420 (class FUN_0061e230, refresh FUN_00624a00) | `ControllerMonitor` |
| mon.ftc | mf120 | 0x36b2 | FUN_004b8550, only if `ZF.ZFType` (cfg+0x4994) ≠ 0 → FUN_004b75a0 | `DeviceMonitor("zf")` |
| mon.af | mf249 | 0x36b6 | FUN_004b85a0, only if `AF.AFType` (cfg+0x4ac8) ≠ 0 → FUN_004b7a50 | `DeviceMonitor("af")` |
| mon.ext | mf620 | 0x36b7 | FUN_004b85f0, only if `EC.ECType` (cfg+0x4ae8) ≠ 0; enabled when connected and NC +0x1fc ≠ 3 (FUN_004b0510) | `DeviceMonitor("ec")` |
| diag.log | mf124 | 0x36b4 | FUN_004bade0 shows the log pane | (lead, log dock) |
| diag.log_dir | mf431 | 0x36b5 | FUN_004bae30: `explorer.exe %LOCALAPPDATA%\NexCut\Log\` | `LogViewer` |
| (mf431-1 "Open Dog Log") | mf431-1 | 0x36b8 | FUN_004baf70: `explorer.exe <exe>\Log\Code.txt` | listed by `LogViewer` if present |
| diag.device_report | mf801 | 0x3aa5 | FUN_004b9260 | `DeviceReportDialog` |
| diag.measure | mf229 | 0x3a9b | FUN_004b8ef0 toggles cfg+0x4327; UI FUN_004b8f20 | toggle + `MeasureWindow` |
| diag.work_report / _all | mf252 / mf253 | 0x3aa6 / 0x3aa7 | FUN_004b96c0 | `ReportWindow` |
| aux.interferometer | mf180 | 17000 | FUN_004bbc60 toggles panel (cfg+0x40b8) | interferometer dock |
| aux.ball_arm | mf702 | 0x426b | FUN_004bbd20 | pattern into the drawing |
| aux.burn_in | newLang1 | 0x4271 | FUN_004bbee0 → dialog FUN_005db330 | `BurnInDialog` |
| aux.light_test | newLang200 | 0x4272 | FUN_004bbf80 → dialog FUN_00567010 | `LightTestDialog` |
| aux.batch_cut | mf705 | 0x4270 | FUN_004c4550 → dialog FUN_004fe130 | `BatchDialog` + runner |
| aux.maintain | A251113_0 | 0x4273 | FUN_004c47f0 → dialog FUN_00419d10 | `MaintainDialog` |
| aux.custom_io / _nl | newLang301 / 300 | 56000..56020 | FUN_004bc460 | `CustomIODialog` |
| aux.dedust | mf140 | 0x4269 | FUN_004bc020, caption FUN_004bc090 | DO.ClearAsh toggle |
| app.help | mf23 | – | dialog FUN_00560e90 | `HelpWindow` |

## 2. Monitors

### 2.1 Controller (mf119)

The original replaces the main view with a monitor view (FUN_004b7420 creates a 0x85a0-byte
view, O). The register pages are labelled by global string arrays built by static initialisers;
the array index is the register index (O):

| block | registers | labels (initialiser) |
|---|---|---|
| status RO | 1000 + i, i < 36 | RORegName_1..32, A241105_0 (Wifi), A241105_1 (Run Time), A250516_5/6 (PWM 5V freq/duty) — FUN_006f0870 |
| axis RO | 2000 + 10a + k, k < 10 | AxisRORegName_1,2,3,4,6,12,7,8,9,10 (status, speed, pulse pos, encoder pos, stop pulse, stop encoder, **total mileage**, calc. pulses, calc. encoder, accumulated pulses) — FUN_006f0b60 |
| system RW | 50000 + 2i, i < 26 | SystemRWRegName_1..10, 28, RWRegName_4, 5, SystemRWRegName_29..32, 34, 35, A241009_2, SystemRWRegName_36..41 — FUN_006f1020 |
| axis RW | 50200 + 40a + 2k, k < 14 | AxisRWRegName_1, 3, 2, 4..14 (config, **negative** soft limit, positive soft limit, acc, jerk, coarse/fine home speed, origin offset, spare, lead, command pulses, encoder pulses, input / output config) — FUN_006f0d80 |
| pulse axis | – | PulseAxisRORegName_1,2,3,4,6,7 / PulseAxisRWRegName_* (bus-card variant, not shown) |

The RO label order agrees with the protocol facts (RO[4] inputs, RO[5] outputs, RO[6]/[7]
alarm words, RO[15]/[16] FIFO id/space, RO[19] processing status, RO[21]/[22] extended IO,
RO[23]/[24] profile status / contour index), which confirms index = position (O). The system
RW labels agree the same way with the decode of FUN_10039f20 (protocol.md §11): word 5 "Bus
cycle" = interpolation period, 6 "Adjust the axis to enable" = ZF type, 7/8 DA1/DA2
calibration, 14/15/16 dual-drive configuration / tolerance / alarm time, **17 "Coefficient of
Precision" = counts per mm**, 18 brake delay (O agreement). The legacy `RegName1..143` table names the old MCC3721 card
registers and is not used by these pages (I).

IO page (FUN_00624a00, O): 58 DO labels from the DO parameter names, the 20 custom DO names,
`ss22` "Oil" at `MP.OilDOPort` (cfg+0x4a54), `ss23` "Dedust(%d, %d)" for the section-DO matrix
(`MP.SecitonDORow` × `MP.SecitonDOColumn`, ports at cfg+0xc9c + row·0x280 + col·0x28); DI 1..28;
per-axis `ss19` NEG / `ss20` ORG / `ss21` POS. Other controls: ReadOnly `ss9` / Read&Write
`ss10` (password `ss11`, Advanced Mode `ss12`), axis pulse send (`ss13`/`ss14`/`ss8`/`ss15`,
`newLang400..403`), DA1/DA2/PWM test (`ss16..18` errors), System Reset `ss0`.

Rebuild: `mlaser.diag.registers.read_snapshot` (read-only `MotionController.read_status`,
`read_regs(1050, 3)`, `read_system_params`, `read_axis_params`), `table_rows`, and
`ControllerMonitor` (refresh 500 ms; parameter pages on demand). Writes only in Read&Write
mode: `set_output`, `jog` (`[3, axis, speed, acc, acc·10, distance]`, FUN_10032b80),
`set_da`, `set_pwm`, `stop_axes`, alarm reset. NEG/POS come from axis status bits 1/0 (O,
FUN_00598280 alarm bits); the ORG source is **U**. The access password is intentionally not
reproduced (vendor password); Read&Write is a checkbox.

### 2.2 FTC (mf120), auto focus (mf249), extended card (mf620)

Status words and scales are the tables of `mlaser.devices` (docs/devices.md): follower
`getReg(7, i)` = card registers 10000..10017 labelled `ZFReadOnly01..18`; AF `getReg(9, i)`
(card registers 13000.. for AF types 3/5/6, network RTU otherwise; labels `af0..af8`, `af17`,
`af18`, `af30_*`); EC `getReg(11, i)` (labels `ec0..ec10`). Buttons (O keys): FTC refresh /
write params `zf0`/`zf1` (`writeReg(11000 + 2·idx, v)` then `150 := 9999`, FUN_00633000),
head calibration `zf24` (refused unless homed, `zf49`), cancel `zf27`, export / import
`zf103`/`zf102` (`.zpf`: `N\r\n` + N × `%d\r\n` + `eof`, FUN_006343c0 / FUN_00634780);
AF go origin `af13`, step up/down `af14`/`af15` with step `af9`, stop `af16`; EC go origin
`ec14`, stop `ec13`, jog with speed 1..200 mm/s (`ec25`) and length 0..10000 mm (`ec23`).
Network AF / EC endpoints come from `DeviceSettings` (File/ipAdd.ini); the connection is only
opened with the Connect button so an absent device never blocks the UI. Restart / reconnect /
factory reset (`zf33`/`zf34`/`zf56`, password `zf57`) are not offered: the restart sequence is
**U** and the factory reset is behind a vendor password.

## 3. Diagnostics

### 3.1 Device report (mf801, 0x3aa5)

FUN_004b9260 (O): a first dialog (template 0x189, FUN_0061d230, result stored at cfg+0x3fec;
purpose **U**, not reproduced), then — if connected — NC slot +0x2cc → VM +0x1a4
(NC FUN_1003d3a0): `read(0x41a = 1050, 3)` and `getReg(3, axis·10 + 6)` for X (cfg+0xbbd4),
Y (cfg+0xbbd8) and, when a follower is configured, Z (cfg+0x4ab8); then
`SOP.DeviceTotalLaserOnTime += word[2] / 3600` (0x83a7c0 = 3600.0) and
`SOP.Device{X,Y}AxisTotalMoveLength += 0.001 · mileage / ppm` (0x84cc00 = 0.001, ppm =
cfg+0xbc28); not connected → message box. Then the property grid FUN_005ee240 with the nine
counters `SOP.Device*` (pd880..888, struct 0x4428..0x4468) and Set Zero / All Set Zero
(`ap20`/`ap21`, confirmation `ap22`).

So register 1050..1052 is the card's **device run information** (power-on, communication,
laser-on time: RORegName_26..28 name the same quantities; word meanings I), not a latch, and
axis RO word **k6 is the total mileage** (label AxisRORegName_7), not a latched position
(corrections to protocol.md §3, requested from H). Whether the card resets these counters
after the read is **U**. The Z sum target is **I** (DeviceZAxisTotalMoveLength).

Software-side counters (I): software use time (hours the program runs), controller use time
(hours connected), total / last processing time and processing count (per completed job).
Units are not stated in lang.txt; the rebuild uses hours and metres.

### 3.2 Logs (mf124 / mf431)

O: daily files `Log\YYYY-MM-DD.log` (GBK) with lines `2026-09-16 13:34:41.379 [Info] -> …`
(levels Info / Warning / Error, strings 0x86262c..), session markers `--- Start Sys ---`
(0x862680) and `---NC Start---` (NC module). mf431 opens `%LOCALAPPDATA%\NexCut\Log\`
(CSIDL 0x1c via FUN_004cc320); mf431-1 opens `Log\Code.txt`, the licensing ("Check code",
dongle) log, which belongs to omitted functionality.
Rebuild: `LogFileWriter` writes the UI log in the same format (UTF-8) to `<data dir>/Log`;
`LogViewer` lists `*.log`/`*.txt` of the data-dir log directory, `settings["log_dirs"]` and
`%LOCALAPPDATA%\NexCut\Log` (when that variable exists), with text and level filters, and
an "Open Log Directory" button (`QDesktopServices`).

### 3.3 Data measurement (mf229, 0x3a9b)

O: FUN_004b8ef0 toggles the byte cfg+0x4327 (cleared at start-up 0x47dba4; check state
FUN_004b8f20). objdump finds no other access to `[reg+0x4327]` in MainApp, NCModule,
CADModule, ParaModule or ControlModule: the switch has no consumer in this build.
Rebuild (I): the switch also runs `DataRecorder` (time, X, Y, Z, speed, laser, mode at the
200 ms UI tick) with a sample window and CSV export.

## 4. Reports

### 4.1 Recording (MainApp)

O: the statistics records live in a vector at cfg+0x43e4 (record: +0x20/+0x28 width/height
doubles, +0x30/+0x34 cut/move length ints ×0.001, +0x3c duration in 0.1 s, +0x40 end time,
+0x48 run count, +0x80/+0x84 thumbnail size). At job completion the status timer FUN_00598280
stamps the end time, increments the count and calls FUN_005e6d40 and FUN_004c5380 (append to
`Report\TotalReport.txt`); it then compares `SOP.PlanManuCount` (cfg+0x43ac) with
`SOP.TotalManuCount` (+0x43b0) for the production-counter prompt (§5.6).
Formats (O, shipped files + format strings 0x85a100..0x85a1ac):

```
report.txt / TotalReport.txt
  name,W x H mm,start,cut m,move m,pierces,<gp146 duration>,cut s Sec,move s Sec,pierce s Sec
LogReport.txt
  <YYYY-MM-DD HH:MM:SS>  <record line>          (7-field layout in 2024 lines)
File/ProcessesStatistic.txt (GBK)
  name,WXHmm,cut mm,move mm,pierces,<s>秒,start
```

`gp146` = `%d分%02d秒` / `%02dMin%02dSec` (language dependent). report.txt lines end with
`\r\r\n`; the size separator is `x` (report.txt) or `×` (TotalReport). Untitled jobs are named
`未命名-1`.

Rebuild: `mlaser.report.records` (`JobRecord`, `parse_line`, `ReportStore`),
`mlaser.report.recorder.StatsRecorder` fed by `mlaser.diag.jobwatch.JobWatcher` (start =
controller log `"<mode>: …"`, completion = `"done: <mode>…"`, stop/abort otherwise; state
polling fallback). The estimates come from `mlaser.motion.interp.estimate_time` of the
window's CAM toolpath (cut, rapid, dwell → pierce time), the duration from the real run time.

### 4.2 Work Report [New] / [All] (mf252 / mf253)

FUN_004b96c0 (O): 0x3aa6 takes only the newest record (`local_bf4 = n - 1`), 0x3aa7 all
records of the vector; it rewrites `Report\report.txt`, appends the launch to `LogReport.txt`,
renders the job contours to `Report\<name>.jpg` (GDI+, white background, `CImage::Save`),
writes `Report\lang.txt` (`lang==0` Chinese, `lang==1` other) and starts `Report\report.exe`.
Rebuild: the same files in `<data dir>/Report`, then the PySide6 viewer.

### 4.3 Viewer (report.exe → `mlaser.report.window.ReportWindow`)

report.exe (Qt 5, class `Qlist_test`, decompiled with the REA CLI, O):

* window 800 × 320 fixed, title `加工统计` / `Report`, icon `./icon.ico`; reads
  `<app dir>/report.txt`, accepts only 10-field lines, fills rows bottom-up (newest first),
  column 0 a checkable index (FUN_00402980, FUN_00404c90, FUN_00407220);
* headers `选择,加工文件名,幅面尺寸,日期,切割总长,空移总长,穿孔个数,预估加工时长,预估切割时间,
  预估空移时间,预估穿孔时间` (0x40da18) / `Pick,Name,Size,Date,Cutting,EmptyMove,Punches,…`;
* buttons Select All, Reverse, Add (hidden), Delete (confirmation, rewrites report.txt,
  FUN_00403b80), Count/Bill (hidden), Export = 生成加工报告单 (print preview of the checked rows,
  "please choose the print contents" when none; Save Pdf via QPdfWriter, A4, default name
  `yyyy-MM-dd-HH-mm-ss.pdf`); double click on a row;
* report page (FUN_004054d0…): title `---加工报告单---` / `---Processing Report---` in 宋体
  18 pt in `(0, 10, width, 100)`, rule, thumbnail `<dir>/<name>.jpg` ("Failed to read the
  image file"), 12 pt grid of 45-unit rows: Name/Date, Size/Est. processing time, Cutting
  Length/Est. cutting time, EmptyMove Length/Est. empty time, Punches/Est. piercing time;
* `countFee` 统计&计费 dialog: cumulative empty-move length (m), pierce count, cut length (m),
  processing time (s), a unit price (单价(元)) per quantity, 费用计算 / Total Cost.

Rebuild differences: Count/Bill is visible (it is the only way to reach the billing dialog);
the cost formula `Σ quantity × unit price` is **I** (five price labels exist, the fifth
quantity is **U**). Run standalone with `python -m mlaser.report [data dir]`.

## 5. Auxiliary functions

### 5.1 Custom IO (newLang301 self-locking / newLang300 non-self-locking)

O: `DO.CustomDOStr` (pd521, cfg+0x944) holds up to 20 records `name#port#flag` separated by
`,` (literals 0x862ca4 `,`, 0x862ca8 `#`); the flag is compared with the literal at 0x862cac
(bytes `2d 00`, "-") and selects type 1 (self-locking) or 0; port → cfg+0x960+0x28·i, type →
+0x964. FUN_004aa8c0 builds one ribbon panel per type with buttons 56000+i. The command
handler FUN_004bc460 acts **only for type 1**: `set(port, !get(port))` (NC +0x148 / +0x13c).
**The difference**: self-locking buttons toggle and stay; non-self-locking buttons have no
command action in the ribbon handler — their momentary action is **U** (I: on while pressed).
Rebuild: `CustomIODialog` with toggle buttons (type 1) or press/release buttons (type 0),
plus an editor that writes `DO.CustomDOStr`; outputs through `MotionController.set_output`
when a card is connected (`customio.OutputDriver`).

### 5.2 Dedust (mf140, 0x4269)

O: FUN_004bc020 toggles `DO.ClearAsh` (pd1400, cfg+0x2ec); the button exists only when that
port is non-zero (FUN_004aa8c0) and its caption is mf171 "Enable Dedust" / mf170 "Disable
Dedust" (FUN_004bc090). Rebuild: same toggle; without `DO.ClearAsh` the controller's
`output_map["dedust"]` is used.

### 5.3 Light test (newLang200, 0x4272)

O: `LT.ltPower` newLang201, `LT.ltDutyfactor` 202, `LT.ltFreq` 203, `LT.ltEnableTime` 204,
`LT.ltLaserTime` 205 (struct 0x46e4..0x46f8). Start (FUN_005686e0) switches the laser on via
the run panel (FUN_005b0680); timed mode counts `ftol(duration)` seconds with a 1 s timer
(FUN_00568500, newLang206 remaining time) and then switches off (FUN_005688a0 →
FUN_005b06f0); closing the dialog stops first (FUN_005686c0). cfg+0x43a4 is set while open.
Rebuild: `lighttest.LightTest` (card PWM `[9999, 3, freq, duty]` I, laser via the facade).

### 5.4 Burn-in (newLang1, 0x4271)

O: StressTest parameters `ST.Type` (XY / Z / XYZ empty travel), `ST.xyCycle`, `ST.xySleep`,
`ST.STIsReturnAfterManu`, `ST.zStartPos`, `ST.zEndPos`, `ST.EnableTestAlarm` (0x46b8..0x46e0);
counter newLang7, exit warning newLang24. Run FUN_005aaa60 (`CManuPanel::OnEmptyMoveBtn`):
`[118, 5, 1]` masks the follower capacitance alarm when EnableTestAlarm (undone with
`[118, 5, 0]` on stop, FUN_005ab070); XY / XYZ require a graphic and dry-run it; Z / XYZ add
flow items `0x16 zStart, 2/7, 0x16 zEnd, 2/7`; then delay `xySleep·1000` ms and loop
`xyCycle`. Rebuild: `burnin.BurnIn` cycles through `program.ProgramRunner` (dry run, Z moves
as relative jog steps, optional return move, interval); `xyCycle = 0` runs until stopped (I).

### 5.5 Ball arm (mf702, 0x426b) and interferometer (mf180, 17000)

Ball arm (O): GraphParam `GRP.BallArm*` (pd1100..1106) in FUN_005ee240(0x1f); CAD slot +0xcc
(CAD FUN_100f2c60 → FUN_100c5bf0) replaces the drawing with a guide line from the origin and
one arc of n turns; geometry table in `mlaser/diag/ballarm.py` (constants π, 2π, π/2, 3 at
CAD 0x101212f8/0x1013dc50/0x1013dc58/0x1011da80).

Interferometer (O): panel toggle FUN_004bbc60; SoftParam `SP.Interfere*` (pd455..462,
0x40bc..0x40f0); Simulate FUN_00564610/FUN_005c2c40, Start FUN_005647c0 (VM +0x68); the
program FUN_0045fdf0: points `i·step` (i = 0..int(range/step)), optional compensation
(MP.CompensateType cfg+0x4cd4: 2 table, 1 linear factor 0x40f8/0x4100), rounding to counts,
direction away from home (`MAC.GoOriginalDirection`), per loop: gap take-up `p0 − gap`
(first loop only with pd461) + stay, p0..pN + stay, overshoot `pN + gap` + stay, pN..p0 +
stay. Rebuild: `interferometer.build_program` → `ProgramRunner`; Simulate draws the path on
the canvas. Pitch compensation data (cp0..cp33, `*.rtl;*.ren;*.pos;*.csv`): the rebuild
imports position / forward / backward tables and shows deviations and the mean backlash
(cp15); Renishaw binary formats and `File/PithCompensate.pcf` records are **U**.

### 5.6 Batch cut (mf705, 0x4270) and production counter

O: GraphParam `GRP.EnableBatchCut` pd2415, dock point `GRP.BatchCutX_LeftEdge/RightEdge`
(pd2416/2417 = X/Y), auto micro-joint pd2418, auto rotate pd2419/2420, start DI pd2420_1, file
table `MC.BatchCutFileTableStr` (ap101..ap104). FUN_004c4550: after OK with batch enabled,
the first entry with done (+0x20) < planned (+0x1c) is loaded and started. Counter:
`SOP.PlanManuCount` pd858, `SOP.TotalManuCount` pd850, `SOP.AfterPlanFinishedActionType`
pd859 {none, prompt, prohibit}; FUN_00598280 prompts once (FUN_004a7c60) when plan ≤ total.
Rebuild: `batch.BatchRunner` (load → micro-joints / rotate → dock move → start; done +1 per
completed job; next entry), `ProductionCounter` (prompt once; "prohibit" stops a job started
after the plan is reached, I). Table encoding (JSON) and the start-DI behaviour are I / U.

### 5.7 Maintenance (A251113_0, 0x4273)

O: dialog FUN_00419d10: groups Clean (A251113_20..25), Check (40..46), Operation (60..63)
and customised items (A251113_13); columns prompt cycle in days (A251113_7), time mode
(world / processing time A251113_11/12), early warning days (A251113_9), prompt type (pop-up,
top error bar, log bar A251113_4..6); stored in `MC.MaintainParamStr` (cfg+0xbf88, encoding U).
Rebuild (I): JSON plan, default cycles 7 / 30 / 90 days, an item is "warn" from
`cycle − warning` and "due" at `cycle`; checked every minute, announced once per state by the
chosen prompt type; "Operation OK" resets an item.

### 5.8 Help (mf23)

O: FUN_00560e90 shows `res\help.bmp` and `File\help.bmp` (vendor picture). `File\sources.txt`
(`https://app.gweikecloud.com/mlaser/PC`) is used by the online update check FUN_004e93a0
(A250708_*) — omitted. Rebuild: offline `HelpWindow` over `docs/*.md` (search, local links
only) and About.

## 6. Data directory

`settings["data_dir"]` → `$MLASER_DATA_DIR` → `~/.mlaser` (a temp dir while pytest runs):
`Log/`, `Report/` (report.txt, LogReport.txt, TotalReport.txt, lang.txt, `<name>.jpg`),
`File/ProcessesStatistic.txt`, `diag_state.json` (counters, ST/LT/SP/GRP/MC/DO parameters of
these dialogs).

## 7. Intentionally omitted

Licensing / dongle log (`Code.txt` is only displayed when present), vendor passwords
(controller Read&Write `ss11`, FTC reset `zf57`), online update (`sources.txt`), cloud
monitoring, vendor help pictures.

## 8. Unknowns

* Purpose of the first dialog of the device report (FUN_0061d230, cfg+0x3fec).
* Meaning of words 0/1 at 1050 and whether the card clears its counters after reading.
* ORG indicator source on the controller IO page.
* Momentary behaviour of non-self-locking custom IO; comparison literal "-" of the flag.
* Burn-in flow items 0x17 / 0x16 / 2 exact semantics; meaning of `xyCycle = 0`.
* Encodings of `MC.MaintainParamStr`, `MC.BatchCutFileTableStr`; default maintenance cycles.
* report.exe fifth price field; the 2024 → 2026 change of the LogReport layout.
* Pitch-compensation file formats (`.pcf`, Renishaw).

## 9. Requests to agent H (controller)

1. A job-finished hook on `UiController` (and `DemoController`): `signals.jobFinished(mode,
   ok, duration_s)` / `add_job_listener(fn)`; ext_diag currently parses the log text
   (`"<mode>: …"`, `"done: <mode>…"`).
2. `move_to(x, y, speed=None)` and an absolute `move_z(z, speed=None)` on the facade
   (interferometer speed, burn-in Z start/end).
3. A public `run_items(items, mode, what, after=None)` (compile + `_run_job`) so test programs
   can run as one FIFO job with exact dwells instead of step-by-step moves.
4. protocol.md / frames.py: `REG_LATCH` 1050..1052 is the device run information (power-on /
   communication / laser-on time) read by VM +0x1a4 FUN_1003d3a0, and axis RO k6 is "total
   mileage" (AxisRORegName_7), not a latch; the simulator could serve both (e.g. accumulate
   travelled counts in k6, seconds in 1050..1052) for tests.
