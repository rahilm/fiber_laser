# Raster engraving tool (Bitmap/Bitmap.exe = next_cut.dll)

Rebuild package: `mlaser/engrave/` (`image_ops.py`, `raster.py`); the dialog is `mlaser/ui/engrave_dialog.py`.
Original: `$ORIG/Bitmap/Bitmap.exe` (.NET apphost) + `next_cut.dll` (.NET 8 WinForms, OpenCvSharp
4.11, Costura/Fody), form title "PicConvert(1.06)", G-code header "(BitmapRaster)".

Evidence tags: **O** = observed in the CIL of `next_cut.dll` (method token, IL offset); **I** =
inferred (mostly the behaviour of the OpenCV/.NET routine that the original calls); **U** = unknown.
The CIL was disassembled from the file bytes with a small decoder written for this task
(`<workspace>/work/engrave/tools/ildis.py` -> `nextcut.il.txt`), driven by the REA
member dump `work/ui/nextcut.members.json`. Nothing from the target was executed.

## 1. Program structure (O)

| Type | Role |
|---|---|
| `next_cut.MyFrom` (0x02000002) | main form, 1062x701, `TopMost`, `MaximizeBox=false` |
| `next_cut.ImageHandle` | image state: `g_currentImage`, `_originalImage`, file name, aspect flags |
| `next_cut.Gcode` | `GetSegments`, `Power`, `generateGCode0`, `FloydSteinberg`, `DistributeError`, `HalftoneFilter` |
| `RasterSegment` (struct) | `int Pixel; int Power;` + `GCode(int rho, ref PointF pos, bool reverse)` |
| `next_cut.KJNova_Clipper` | crop window (8 resize handles, OK/Cancel) |
| `next_cut.Luaguage` | `string[28,2]` UI table (col 0 中文, col 1 English) |
| `Properties.Settings` | `comboBoxSave` (language, default "中文"), `设置` (int, unused) in `next_cut.dll.config` |

## 2. Form controls and defaults

| Control (field) | Text zh / en | Default / range | Handler | Tag |
|---|---|---|---|---|
| `OpenImageButton` | 打开图片 / Open Image | filter `png\|*.png\|jpg\|*.jpg\|all\|*.*` | `button2_Click` -> `ImageHandle.OpenImage` | O |
| `pictureBox1` + `ImagePixelShow`/`label2` | 图片尺寸（像素）: | shows `"<w>*<h>"` | | O |
| `brightess` (TrackBar) | 亮度: | 0..200, value 100, tick 5 (set in `WidgetInit` after an image is opened) | `trackBarContrast_Scroll` | O |
| `Contrast` (TrackBar) | 对比度: / contrast ratio: | 0..200, value 100, tick 5 | same | O |
| `BinarizationButton` | 二值化 / Binarization | threshold 170 | `button3_Click` | O |
| `GaussianBlurbutton` | 高斯模糊 / Gaussian Blur | 7x7, sigma 0 | `button8_Click` | O |
| `button5` | 反色 / Inverse color | | `button5_Click` | O |
| `button10` / `button11` | 水平翻转 / 垂直翻转 | `Flip(1)` / `Flip(0)` | | O |
| `button1` | 图片裁剪 / Crop images | opens `KJNova_Clipper` | `button1_Click_1` | O |
| `button6` | 取消编辑 / Cancel Editing | reload file, sliders 100 | `button6_Click_1` | O |
| `comboBox2` | 位图处理: | items 不启用 / 1bit抖动 / 半调网屏; text after start-up "启用半调网屏" (not an item) -> no processing | read in `button9_Click` | O |
| `setWidth` / `SetHight` | W： / H： (设置当前绘图尺寸（MM）) | "50" / "50"; digits only (`textBox1_KeyPress`); opening an image sets W = 60 and H from the aspect | `WidthTextChanged` / `HightTextChanged` (300 ms debounce) | O |
| `SetRho` | 设置线密度大小（>0）： | "6" (lines = pixels per mm, `int.Parse`) | | O |
| `SetLaserPower` | 设置激光功率大小（>0）： | "20" (`int.Parse`) | | O |
| `comboBox1` | 语言设置： | 中文 / English, saved to user settings | `<.ctor>b__4_0` | O |
| `button9` | 生成GCode / Generate Gcode | disabled while the size is being recomputed | `button9_Click` | O |

`RasterParams` defaults: `brightness=100, contrast=100, edits=(), mode="none", width_mm=60,
height_mm=None (aspect), rho=6, power=20`.

## 3. Image state and edits (O)

* Open (`OpenImage` 0x06000054): `Cv2.ImRead(name, IMREAD_UNCHANGED)` -> `Set_GrayImage` ->
  `original = current`; then `SetWidth = 60` and `Update_pixels`.
* `Set_GrayImage` (0x06000052): 4 channels: gray = BGR2GRAY of the colour planes, then
  `gray.CopyTo(white, mask=alpha)` (alpha is a mask: alpha 0 -> 255, any other alpha keeps the
  gray); 3 channels: BGR2GRAY; 1 channel unchanged. BGR2GRAY = OpenCV fixed point
  `(B*3735 + G*19235 + R*9798 + 2^14) >> 15` (I). Rebuild: `image_ops.to_gray/load_image`
  (Pillow; palette+transparency -> RGBA, 16-bit -> `>>8` (U)).
* Binarize/invert/blur/flip are applied to both `current` and `original`, then
  `current = BC(original)` (button3/5/8/10/11_Click). Slider moves: `current = BC(original)`.
  So without crop the result is `BC(edits(gray))` -> `process_image`.
* Crop (`KJNova_Clipper.CropImage` 0x06000068, `MyFrom.kJNova_Clipper_Closed` 0x0600002e):
  the selection (initial rectangle (300,100, pb.w/2, pb.h/2) on the zoomed preview) is mapped
  to image pixels with float32 scale and `conv.i4` truncation (`GetAbsoluteRect` 0x06000069);
  selections < 10 px wide or high do nothing; the cut is a 1:1 copy into a 32bpp ARGB bitmap.
  Then `original = current` (B/C at that moment are baked in and applied once more by the next
  edit or slider move: quirk, reproduced by `EditSession`). The rebuild crops with an explicit
  pixel rectangle (`crop(img, x, y, w, h)`, dialog spin boxes).
* Cancel editing (`CancelEditing` 0x0600004f): reload file, gray, sliders 100.

## 4. Algorithms

### Brightness / contrast (O, `AdjustContrastBrightnessDirect` 0x0600004e IL_00b5..IL_00fe)
float32: `c = C/100f`, `b = B/100f`,
`v = ((p - 128f) * c + 128f) + ((b - 1f) * 255f)`, `out = (byte)Max(0, Min(255, v))` (truncation).

### Binarize (O 0x06000050): `Cv2.Threshold(src, dst, 170, 255, THRESH_BINARY)`: `p > 170 ? 255 : 0`.
### Invert (O 0x06000053): `Cv2.BitwiseNot`.
### Gaussian blur (O 0x06000051): `Cv2.GaussianBlur(src, dst, Size(7,7), 0, 0, Reflect101)`.
For ksize 7 and sigma 0 OpenCV uses its fixed table {1/32, 3.5/32, 7/32, 9/32, ...} (sigma
equivalent 0.3*((7-1)/2-1)+0.8 = 1.4) and, for 8-bit data, a bit-exact fixed-point separable
filter: taps [8,28,56,72,56,28,8]/256, row pass exact, column pass, `(sum + 2^15) >> 16` (I,
OpenCV 4.x `GaussianBlurFixedPoint`). Rebuild: `gaussian_blur` (numpy integer separable
convolution, REFLECT_101 borders).
### Flip (O): `Cv2.Flip(.., 1)` horizontal, `Cv2.Flip(.., 0)` vertical.

### Resample (O, `button9_Click` 0x06000020 IL_0090..IL_00c1)
`Cv2.Resize(current, dst, Size(rho*W, rho*H), 0, 0, INTER_NEAREST)`; W, H, rho are integers.
OpenCV `resizeNN`: `sx = min(floor(x / (dst_w/src_w)), src_w-1)` (I). Pixel pitch = 1/rho mm in X
and Y.

### Aspect lock (O, `Update_pixels` 0x06000058, `WidthTextChanged` 0x0600005a, `HightTextChanged` 0x06000056)
`H = (int)(W / (double)(float)(w/(float)h))`, `W = (int)(H / (double)(float)(h/(float)w))`.
The float32 ratio makes e.g. 64x48 at W=60 give H=44 (60/1.3333334 = 44.99999). A zero entry
shows a message (table row 21 for width; row 22 "GCode生成成功" for height: wrong row in the
original, rebuild uses row 20).

### Processing mode (O, IL_00c6..IL_0118)
`comboBox2.Text == "1bit抖动"` -> `FloydSteinberg`; `== "半调网屏"` -> `HalftoneFilter`; else
unchanged. Because the comparison uses the Chinese item text, **in English no mode is ever
applied** (English items are "1bit dithering", "half-tone screen"); the rebuild dialog uses the
combo index instead.

### Floyd-Steinberg (O, `FloydSteinberg` 0x06000040, `DistributeError` 0x0600003f)
float32 copy; rows top->bottom, every row left->right (no serpentine);
`new = old < 128 ? 0 : 255`; `err = old - new`; error to (x+1,y)·0.4375, (x-1,y+1)·0.1875,
(x,y+1)·0.3125, (x+1,y+1)·0.0625 (targets outside the image dropped). Output 0/255.
Rebuild: `floyd_steinberg`, float32-exact (checked against a naive per-pixel reference).

### Halftone screen (O, `HalftoneFilter` 0x0600003e)
Cell 6.0 px, angle 30° (0.5235987755982988 rad), min dot 0.23, gamma 0.7.
`u = dx·cos − dy·sin`, `v = dx·sin + dy·cos` with `dx = x − cols/2`, `dy = y − rows/2`; offsets
`-min(u)`, `-min(v)` over the 4 corners; `gridCols = max(1, ceil(rangeU/6))`, same for rows;
cell index `floor((u+offU)/6)` clamped. Per cell: float32 sum / count = mean;
radius = 0 (empty), 3.0 (mean <= 0), `0.69·0.5` (mean >= 255), else
`max(3·(1−mean/255)^0.7, 0.69)`. Output white; pixel black if its squared distance to the
cell centre `(gi·6+3, gj·6+3)` is `<= radius²`. Note the screen is in *pixels*, independent of
rho. Rebuild: `halftone`.

### Pixel -> power (O, `Gcode.Power` 0x0600003b)
`pixel > 125 ? 0 : power`. No grey-scale power modulation: the S value is always the form's
laser power (or 0), so "not enabled" mode is a hard threshold at 125.

### Run-length segments (O, `GetSegments` 0x0600003c)
Per image row, top row first: runs of equal `Power(pixel)` -> `RasterSegment{Pixel = run
length, Power}`. Rebuild: `raster_segments(img, power)`.

## 5. Output format (O, `generateGCode0` 0x0600003d, `RasterSegment.GCode` 0x06000048)

Called as `generateGCode0(img, rho, W, H, 0.0, 0.0, power)` (W, H and the two doubles unused).
Position is a `System.Drawing.PointF` (float32); `\n` line ends; written with
`File.WriteAllText` (UTF-8, no BOM).

```
(BitmapRaster)
(V1.06)
(2025-11-26)

G90
M4
G0 X-5 Y0                      <- "G0 X{0} Y{1}": X = 0 - 5 (overscan), default float format
G0 X0.000 Y0.000 S0            <- row 1 (bottom image row), +X: X += 5
G0 X1.000 Y0.000 S0            <- segment with Power 0:  X += (float)(Pixel/(double)rho)
G1 X2.000 Y0.000 S20           <- segment with Power > 0
G0 X7.000 Y1.000 S0            <- X += 5, Y = (float)(Y + 1.0/rho)
G0 X2.000 Y1.000 S0            <- row 2, -X: X -= 5, segments in reverse order, X -= len
...
G0 X-5.000 Y2.000 S0
G0 X0 Y0 S0
M5
```

* Rows: image rows from the last (bottom) to the first; Y starts at 0 and grows by 1/rho per
  row; X starts at 0 at the left edge: origin = lower-left corner, Y up (O).
* Bidirectional (serpentine): first row +X, then alternating (O, local 5 toggled with
  `ceq` at IL_028c).
* Overscan: 5 mm before and after every row, travelled with `G0 ... S0` (O, `ldc.i4.5`
  IL_0016); the diagonal step to the next row happens in the overscan zone.
* Numbers: `{0:F3}` / `$"{x:F3}"` of float32 with the current culture (O); rounding of exact
  ties (half away from zero) and `-0.000` for tiny negative values follow .NET Core 3.0+ (I);
  a non-"." decimal separator culture would give commas (I, U whether ever seen in the field).
  Accumulated float32 X drifts slightly (e.g. `X59.999`, `X-0.001`); the rebuild reproduces it.
* Power: integer from `SetLaserPower`, e.g. `S20` (O); `Power <= 0` gives `G0 .. S0` (O).
* No F word, no Z, no M3 (O).

## 6. status.ini protocol (O)

File `<Bitmap.exe dir>\status.ini` (shipped with content `0`), bare ASCII digit, no newline,
no section/key:

| When | Content | Evidence |
|---|---|---|
| form constructor (`MyFrom..ctor`) | `0` | IL_0088..IL_00a1 `File.WriteAllText(BaseDirectory+"status.ini", "0")` |
| after "Generate GCode" wrote `newGcode.nc` | `1`, then `Application.Exit()` | `button9_Click` IL_0140..IL_0170 |

Closing the window without generating leaves `0`. `next_cut.dll` never writes `newSVG.svg`
(no such literal in any method body, O); MainApp references `\Bitmap\newSVG.svg` next to
`\Bitmap\newGcode.nc` and `\Bitmap\status.ini` (MainApp.strings.txt 0x00859d08, O); its use is
U here. Rebuild: `write_status_ini(dir, 0|1)`, `read_status_ini(dir)`, `generate_to_dir(dir, ...)`.

## 7. Rebuild API

```python
from mlaser.engrave import RasterParams, process_image, generate_gcode, generate_to_dir
p = RasterParams(mode="dither", width_mm=60, rho=6, power=20)   # height from aspect
text = generate_gcode(gray_or_rgb_array, p)
generate_to_dir("/path/Bitmap", img, p)   # newGcode.nc + status.ini = 1
```

`BitmapDialog(tr=None, parent=None, out_dir=None)`: `set_image(array)`, `params`,
`set_mode()`, `generate()` -> writes `out_dir/newGcode.nc` (temp dir if no `out_dir`), sets
`gcode_path`, `accept()`. Messages go to a status label (no modal boxes). `mlaser.io.gcode.parse_gcode`
reads the output: `M4` turns the laser on, `S` words are ignored, each `G1` run becomes one
2-point open contour/part.

## 8. Differences and unknowns

* Rebuild differences (deliberate): mode by combo index (works in English); correct message
  rows; crop by numeric rectangle; no `TopMost`; no `Application.Exit` (dialog accepts).
* U: exact OpenCV build paths (IPP/HAL) for resize/blur on the shipped OpenCvSharpExtern
  (assumed the portable bit-exact paths); .NET tie rounding in `F3`; behaviour for 16-bit PNGs.
* U: `Properties.Settings.设置` is never read.

## 9. How MainApp consumes it

see docs/re/craft.md §6 (status.ini check, `newGcode.nc` imported as a type-5 scan G-code,
`ext_craft.CraftExtension.bitmap` / `import_bitmap_result`).
