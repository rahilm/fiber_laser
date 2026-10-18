"""Raster G-code generation of "PicConvert(1.06) (BitmapRaster)" (``$ORIG/Bitmap/next_cut.dll``).

Pipeline of the original (MyFrom.button9_Click, token 0x06000020; tags as in docs/engrave.md):

1. the edited gray image (``g_currentImage``) is resampled with nearest neighbour to
   ``(rho*W) x (rho*H)`` pixels, W/H integer millimetres, rho integer lines per mm (O);
2. the processing mode is applied: "1bit抖动" -> Floyd-Steinberg, "半调网屏" -> halftone,
   anything else -> unchanged (O, IL_00c6..IL_0118);
3. ``Gcode.generateGCode0(img, rho, W, H, 0.0, 0.0, power)`` (0x0600003d) turns every pixel row
   into run-length segments of equal power (``GetSegments`` 0x0600003c, ``Power`` 0x0600003b:
   pixel <= 125 -> ``power``, else 0) and emits a bidirectional (serpentine) raster with a fixed
   5 mm overscan;
4. the text goes to ``<exe dir>\\newGcode.nc``, then ``status.ini`` is overwritten with ``1`` and
   the program exits (``Application.Exit``).  At start-up ``status.ini`` is set to ``0``.

Coordinates: X to the right, Y up, origin at the lower-left corner of the picture (the last
image row is engraved first at Y=0, each further row is 1/rho mm higher).  Positions are kept in
``System.Drawing.PointF`` (float32) exactly like the original, which is why this module does
its arithmetic with ``numpy.float32``.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

from . import image_ops as ops

__all__ = [
    "MODE_NONE", "MODE_DITHER", "MODE_HALFTONE", "MODES", "MODE_LABELS",
    "OVERSCAN_MM", "POWER_THRESHOLD", "HEADER", "FOOTER", "GCODE_NAME", "STATUS_NAME", "SVG_NAME",
    "RasterParams", "RasterSegment", "EditSession",
    "aspect_height", "aspect_width", "apply_edits", "process_image", "pixel_power",
    "raster_segments", "segment_gcode", "gcode_from_bitmap", "raster_from_bitmap", "generate_gcode",
    "write_gcode",
    "write_status_ini", "read_status_ini", "generate_to_dir", "format_f3",
]

MODE_NONE = "none"
MODE_DITHER = "dither"
MODE_HALFTONE = "halftone"
MODES = (MODE_NONE, MODE_DITHER, MODE_HALFTONE)
#: comboBox2 items (InitializeComponent) and their English versions (Luaguage rows 24..26), O.
MODE_LABELS = {
    MODE_NONE: ("不启用", "Not enabled"),
    MODE_DITHER: ("1bit抖动", "1bit dithering"),
    MODE_HALFTONE: ("半调网屏", "half-tone screen"),
}

#: generateGCode0 IL_0016 ``ldc.i4.5`` -> local used as X overscan in mm (O).
OVERSCAN_MM = 5
#: Gcode.Power IL_0001 ``ldc.i4.s 125; ble.s`` (O).
POWER_THRESHOLD = 125
#: generateGCode0 IL_0044..IL_0074 (O).
HEADER = "(BitmapRaster)\n(V1.06)\n(2025-11-26)\n\nG90\nM4\n"
#: generateGCode0 IL_02a2..IL_02ae (O).
FOOTER = "G0 X0 Y0 S0\nM5\n"
GCODE_NAME = "newGcode.nc"
STATUS_NAME = "status.ini"
#: MainApp also knows ``\\Bitmap\\newSVG.svg`` but next_cut.dll never writes it (O: no such literal).
SVG_NAME = "newSVG.svg"

_F32 = np.float32


# ------------------------------------------------------------------------------- parameters
@dataclass
class RasterParams:
    """All parameters of the PicConvert form with the original defaults.

    brightness / contrast: track bars 0..200, 100 = neutral (MyFrom.WidgetInit, O).
    edits: image edits in click order: ``"binarize"``, ``"invert"``, ``"blur"``, ``"flip_h"``,
        ``"flip_v"`` or ``("crop", x, y, w, h)`` (image pixels).
    mode: ``"none"`` (form default, combo text not an item -> no processing), ``"dither"``,
        ``"halftone"``.
    width_mm: text box ``setWidth``; the form shows 50 but opening a picture sets 60
        (ImageHandle.OpenImage IL_007a ``ldc.i4.s 60``), O.
    height_mm: ``None`` = follow the picture's aspect ratio like Update_pixels does (O); an int
        overrides it.
    rho: ``SetRho`` text, default "6" (lines and pixels per mm), O.
    power: ``SetLaserPower`` text, default "20" (the S word of every burn segment), O.
    """

    brightness: int = 100
    contrast: int = 100
    edits: Tuple = ()
    mode: str = MODE_NONE
    width_mm: int = 60
    height_mm: Optional[int] = None
    rho: int = 6
    power: int = 20

    def size_for(self, img_w: int, img_h: int) -> Tuple[int, int]:
        """(W, H) in mm for an image of ``img_w`` x ``img_h`` pixels."""
        w = int(self.width_mm)
        h = aspect_height(img_w, img_h, w) if self.height_mm is None else int(self.height_mm)
        return w, h

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r}")
        if int(self.rho) <= 0:
            raise ValueError("line density must be > 0")
        if int(self.width_mm) <= 0 or (self.height_mm is not None and int(self.height_mm) <= 0):
            raise ValueError("drawing size must be > 0")


def aspect_height(img_w: int, img_h: int, width_mm: int) -> int:
    """Update_pixels / WidthTextChanged (0x06000058 / 0x0600005a), O:
    ``ratio = (double)(float)((float)w / (float)h)``; ``H = (int)(W / ratio)`` (truncation)."""
    ratio = float(_F32(_F32(img_w) / _F32(img_h)))
    return int(float(width_mm) / ratio)


def aspect_width(img_w: int, img_h: int, height_mm: int) -> int:
    """HightTextChanged (0x06000056), O: ``ratio = (float)h/(float)w``; ``W = (int)(H / ratio)``."""
    ratio = float(_F32(_F32(img_h) / _F32(img_w)))
    return int(float(height_mm) / ratio)


# ------------------------------------------------------------------------- image pipeline
def _apply_edit(img: np.ndarray, e) -> np.ndarray:
    if isinstance(e, str):
        name, args = e, ()
    else:
        name, args = e[0], tuple(e[1:])
    if name == "binarize":
        return ops.binarize(img)
    if name == "invert":
        return ops.invert(img)
    if name == "blur":
        return ops.gaussian_blur(img)
    if name == "flip_h":
        return ops.flip_h(img)
    if name == "flip_v":
        return ops.flip_v(img)
    if name == "crop":
        x, y, w, h = (int(v) for v in args)
        return ops.crop(img, x, y, w, h)
    raise ValueError(f"unknown edit {e!r}")


def apply_edits(img: np.ndarray, edits: Iterable) -> np.ndarray:
    """Apply the edit list in order to a gray image."""
    out = np.asarray(img, dtype=np.uint8)
    for e in edits:
        out = _apply_edit(out, e)
    return out


def process_image(img: np.ndarray, params: Optional[RasterParams] = None) -> np.ndarray:
    """Return the engraving bitmap (rows = rho*H, cols = rho*W) that generateGCode0 receives.

    ``img`` may be gray or RGB(A); it is converted like Set_GrayImage.  Edits run first, then
    brightness/contrast (the original always recomputes ``current = BC(original)`` after an
    edit, so B/C act on the edited picture; see :class:`EditSession` for the exact crop quirk),
    then the nearest-neighbour resize and the processing mode.
    """
    p = params or RasterParams()
    p.validate()
    gray = ops.to_gray(img)
    gray = apply_edits(gray, p.edits)
    gray = ops.brightness_contrast(gray, p.brightness, p.contrast)
    w_mm, h_mm = p.size_for(gray.shape[1], gray.shape[0])
    if w_mm <= 0 or h_mm <= 0:
        raise ValueError("drawing size must be > 0")
    bmp = ops.resize_to_mm(gray, w_mm, h_mm, p.rho)
    if p.mode == MODE_DITHER:
        bmp = ops.floyd_steinberg(bmp)
    elif p.mode == MODE_HALFTONE:
        bmp = ops.halftone(bmp)
    return bmp


class EditSession:
    """State machine of ImageHandle (``g_currentImage`` / ``_originalImage``), O.

    * open / cancel editing: ``original = current = gray(file)``, sliders back to 100
      (OpenImage 0x06000054, CancelEditing 0x0600004f, button6_Click_1 0x0600001a);
    * binarize / invert / blur / flips: applied to ``current`` *and* ``original``, then
      ``current = BC(original)`` (button3/5/8/10/11_Click);
    * slider moves: ``current = BC(original)`` (trackBarContrast_Scroll 0x0600004c);
    * crop: ``current = crop(current)`` and ``original = current`` -- the B/C in effect at that
      moment is baked into ``original`` and is applied a second time by the next edit or slider
      move (KJNova_Clipper.button1_Click + MyFrom.kJNova_Clipper_Closed, a quirk of the
      original, reproduced here).
    """

    def __init__(self, img: Optional[np.ndarray] = None):
        self.source: Optional[np.ndarray] = None
        self.original: Optional[np.ndarray] = None
        self.current: Optional[np.ndarray] = None
        self.brightness = 100
        self.contrast = 100
        self.history: List = []
        if img is not None:
            self.open(img)

    @property
    def loaded(self) -> bool:
        return self.current is not None

    def open(self, img: np.ndarray) -> None:
        self.source = ops.to_gray(img)
        self.cancel()

    def cancel(self) -> None:
        if self.source is None:
            return
        self.original = self.source.copy()
        self.current = self.source.copy()
        self.brightness = self.contrast = 100
        self.history = []

    def _recompute(self) -> None:
        self.current = ops.brightness_contrast(self.original, self.brightness, self.contrast)

    def set_brightness_contrast(self, brightness: int, contrast: int) -> None:
        self.brightness, self.contrast = int(brightness), int(contrast)
        if self.original is not None:
            self._recompute()

    def edit(self, name) -> None:
        if self.current is None:
            raise RuntimeError("no image")
        if (name if isinstance(name, str) else name[0]) == "crop":
            self.current = _apply_edit(self.current, name)
            self.original = self.current.copy()
        else:
            self.current = _apply_edit(self.current, name)
            self.original = _apply_edit(self.original, name)
            self._recompute()
        self.history.append(name)


# --------------------------------------------------------------------------- segmentation
@dataclass
class RasterSegment:
    """``RasterSegment {int Pixel; int Power}`` (value type, token 0x02000004), O."""

    pixel: int = 0
    power: int = 0


def pixel_power(pixel: int, power: int) -> int:
    """Gcode.Power (0x0600003b), O: ``pixel > 125 ? 0 : power``."""
    return 0 if int(pixel) > POWER_THRESHOLD else int(power)


def raster_segments(img: np.ndarray, power: int) -> List[List[RasterSegment]]:
    """Gcode.GetSegments (0x0600003c), O: per image row (top row first) the run-length list of
    equal ``Power(pixel)`` values, each ``RasterSegment(pixel=run length, power=S value)``."""
    a = np.asarray(img)
    if a.ndim == 3:
        a = ops.to_gray(a)
    rows, cols = a.shape
    out: List[List[RasterSegment]] = []
    on = a <= POWER_THRESHOLD
    pw = int(power)
    for y in range(rows):
        vals = np.where(on[y], pw, 0)
        segs: List[RasterSegment] = []
        if cols:
            change = np.flatnonzero(vals[1:] != vals[:-1]) + 1
            starts = np.concatenate(([0], change))
            ends = np.concatenate((change, [cols]))
            for s, e in zip(starts.tolist(), ends.tolist()):
                segs.append(RasterSegment(e - s, int(vals[s])))
        out.append(segs)
    return out


# ------------------------------------------------------------------------------ formatting
def format_f3(v) -> str:
    """``float.ToString("F3")`` of .NET 8 for a float32 value (I: exact binary value rounded
    half away from zero; negative values that round to zero keep the sign, ``-0.000``;
    invariant-style ``.`` separator as on zh/en Windows)."""
    f = float(_F32(v))
    neg = math.copysign(1.0, f) < 0
    q = Decimal(abs(f)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return ("-" if neg else "") + f"{q:.3f}"


def _format_r(v) -> str:
    """Default ``float.ToString()`` (shortest round-trip) for the first rapid line (-5 / 0)."""
    f = float(_F32(v))
    if f == int(f) and abs(f) < 1e7:
        return ("-" if math.copysign(1.0, f) < 0 and f == 0 else "") + str(int(f))
    return np.format_float_positional(_F32(v), unique=True, trim="-")


def segment_gcode(seg: RasterSegment, rho: int, pos: List, reverse: bool) -> str:
    """RasterSegment.GCode (0x06000048), O: advance X by ``(float)(Pixel/(double)rho)`` (minus
    when ``reverse``) and emit ``G1 X.. Y.. S<Power>`` if Power > 0, else ``G0 X.. Y.. S0``.
    ``pos`` is a 2-item list [X, Y] of float32 that is updated in place (``ref PointF``)."""
    step = _F32(seg.pixel / float(rho))
    pos[0] = _F32(pos[0] - step) if reverse else _F32(pos[0] + step)
    if seg.power > 0:
        return f"G1 X{format_f3(pos[0])} Y{format_f3(pos[1])} S{seg.power}\n"
    return f"G0 X{format_f3(pos[0])} Y{format_f3(pos[1])} S0\n"


def gcode_from_bitmap(bmp: np.ndarray, rho: int, power: int) -> str:
    """Gcode.generateGCode0 (0x0600003d), O.  ``bmp`` is the already resized/processed bitmap.

    Rows are emitted from the last image row (bottom) upwards; the first row runs +X, then the
    direction alternates.  Per row: rapid to the row start (X moved back by the 5 mm overscan),
    the segments, then a rapid 5 mm past the row end that also steps Y by 1/rho.
    """
    rho = int(rho)
    over = _F32(OVERSCAN_MM)
    rows = raster_segments(bmp, power)
    out = [HEADER]
    pos = [_F32(0.0), _F32(0.0)]
    pos[0] = _F32(pos[0] - over)
    out.append(f"G0 X{_format_r(pos[0])} Y{_format_r(pos[1])}\n")
    reverse = False
    dy = 1.0 / float(rho)
    for segs in reversed(rows):
        if not reverse:
            pos[0] = _F32(pos[0] + over)
            out.append(f"G0 X{format_f3(pos[0])} Y{format_f3(pos[1])} S0\n")
            for s in segs:
                out.append(segment_gcode(s, rho, pos, False))
            pos[0] = _F32(pos[0] + over)
        else:
            pos[0] = _F32(pos[0] - over)
            out.append(f"G0 X{format_f3(pos[0])} Y{format_f3(pos[1])} S0\n")
            for s in reversed(segs):
                out.append(segment_gcode(s, rho, pos, True))
            pos[0] = _F32(pos[0] - over)
        pos[1] = _F32(float(pos[1]) + dy)
        out.append(f"G0 X{format_f3(pos[0])} Y{format_f3(pos[1])} S0\n")
        reverse = not reverse
    out.append(FOOTER)
    return "".join(out)


def raster_from_bitmap(bmp: np.ndarray, rho: int, power: int, params: Optional[RasterParams] = None):
    """The engraving of :func:`gcode_from_bitmap` as one
    :class:`mlaser.cam.raster_engraving.RasterEngraving` (numpy arrays of the burn runs).

    Same float32 walk as generateGCode0 (rows bottom-up, serpentine, 5 mm overscan, run steps
    ``(float)(Pixel/(double)rho)``), vectorised per row; the coordinates are the values of the
    G-code text (``format_f3``), so ``parse_gcode(gcode_from_bitmap(...))`` and this object give
    the same burn lines bit for bit.  Runs with zero length in the text (``parse_gcode`` drops
    them) are dropped too.  ``power`` <= 0 gives no burn runs."""
    from ..cam.raster_engraving import RasterEngraving, f3_values
    rho = int(rho)
    pw = int(power)
    a = np.asarray(bmp)
    if a.ndim == 3:
        a = ops.to_gray(a)
    rows, cols = a.shape
    on = a <= POWER_THRESHOLD
    over = _F32(OVERSCAN_MM)
    dy = 1.0 / float(rho)
    x = _F32(_F32(0.0) - over)
    y = _F32(0.0)
    xs0, xs1, rr, ys, pws = [], [], [], [], []
    reverse = False
    nrow = 0
    for r in range(rows - 1, -1, -1):
        if pw != 0 and cols:
            v = on[r]
            change = np.flatnonzero(v[1:] != v[:-1]) + 1
            starts = np.concatenate(([0], change))
            ends = np.concatenate((change, [cols]))
            burn = v[starts] if pw > 0 else np.zeros(len(starts), bool)
        else:
            starts = np.zeros(1 if cols else 0, np.int64)
            ends = np.full(len(starts), cols, np.int64)
            burn = np.zeros(len(starts), bool)
        steps = ((ends - starts) / float(rho)).astype(np.float32)
        if reverse:
            x = _F32(x - over)
            seq = np.concatenate(([x], -steps[::-1])).astype(np.float32)
            burn = burn[::-1]
        else:
            x = _F32(x + over)
            seq = np.concatenate(([x], steps)).astype(np.float32)
        walk = np.add.accumulate(seq, dtype=np.float32)
        if burn.any():
            k = np.flatnonzero(burn)
            b0, b1 = f3_values(walk[k]), f3_values(walk[k + 1])
            keep = b0 != b1
            if keep.any():
                xs0.append(b0[keep])
                xs1.append(b1[keep])
                rr.append(np.full(int(keep.sum()), nrow, np.int32))
                ys.append(float(f3_values(y)))
                nrow += 1
        x = _F32(walk[-1] - over) if reverse else _F32(walk[-1] + over)
        y = _F32(float(y) + dy)
        reverse = not reverse
    cat = (lambda L, dt: np.concatenate(L) if L else np.zeros(0, dt))
    x0 = cat(xs0, np.float64)
    prm = {} if params is None else {k: (list(v) if isinstance(v, tuple) else v)
                                    for k, v in params.__dict__.items()}
    return RasterEngraving(x0, cat(xs1, np.float64), cat(rr, np.int32), np.array(ys, np.float64),
                           np.full(len(x0), pw, np.int32), rho=rho, overscan=float(OVERSCAN_MM),
                           params=prm)


def generate_gcode(img: np.ndarray, params: Optional[RasterParams] = None, *,
                   preprocessed: bool = False) -> str:
    """Full pipeline: :func:`process_image` then :func:`gcode_from_bitmap`.

    With ``preprocessed=True`` ``img`` is taken as the finished engraving bitmap.
    """
    p = params or RasterParams()
    bmp = np.asarray(img) if preprocessed else process_image(img, p)
    return gcode_from_bitmap(bmp, p.rho, p.power)


def write_gcode(path: Union[str, Path], img_or_text, params: Optional[RasterParams] = None, *,
                preprocessed: bool = False) -> Path:
    """Write the G-code like ``File.WriteAllText``: UTF-8 without BOM, ``\\n`` line ends (O/I)."""
    text = img_or_text if isinstance(img_or_text, str) else generate_gcode(
        img_or_text, params, preprocessed=preprocessed)
    path = Path(path)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


# ------------------------------------------------------------------------------ status.ini
def write_status_ini(directory: Union[str, Path], status: Union[int, bool, str]) -> Path:
    """``File.WriteAllText(BaseDirectory + "status.ini", "0" | "1")`` (O: MyFrom..ctor IL_0088,
    button9_Click IL_015c).  The file holds the bare digit, no section, key or newline:
    ``0`` = tool started / nothing generated, ``1`` = ``newGcode.nc`` written."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    s = str(int(status)) if isinstance(status, (bool, int)) else str(status)
    p = d / STATUS_NAME
    p.write_bytes(s.encode("utf-8"))
    return p


def read_status_ini(directory: Union[str, Path]) -> Optional[int]:
    """Read ``status.ini``: 0 / 1, or ``None`` if missing or not a number."""
    p = Path(directory) / STATUS_NAME
    try:
        raw = p.read_bytes().decode("utf-8-sig", "replace").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def generate_to_dir(directory: Union[str, Path], img: np.ndarray,
                    params: Optional[RasterParams] = None, *, preprocessed: bool = False) -> Path:
    """What "Generate GCode" does: write ``newGcode.nc`` then ``status.ini`` = ``1``."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = write_gcode(d / GCODE_NAME, img, params, preprocessed=preprocessed)
    write_status_ini(d, 1)
    return path
