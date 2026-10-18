"""Image operations of the "PicConvert(1.06) (BitmapRaster)" raster-engraving tool.

Port of ``$ORIG/Bitmap/next_cut.dll`` (.NET 8 WinForms + OpenCvSharp 4.11).  Every function works
on a 2-D ``numpy.uint8`` grayscale array (rows x cols, row 0 = top of the picture) and returns a
new array; nothing is modified in place.  Evidence tags follow docs/engrave.md:
O = observed in the IL of next_cut.dll (method token + IL offset), I = inferred (usually: the
behaviour of the OpenCV routine the original calls), U = unknown.

The original runs every pixel operation on 8-bit data through OpenCV.  OpenCV is *not* needed
here: the arithmetic of each routine is reproduced in numpy, bit-exact where the OpenCV
implementation is integer/fixed-point (gray conversion, threshold, 7x7 Gaussian, nearest resize)
and IEEE float32-exact where the original does float32 maths itself (brightness/contrast,
Floyd-Steinberg) or float64 maths (halftone).
"""
from __future__ import annotations

import math
from typing import Optional, Union

import numpy as np

__all__ = [
    "BINARIZE_THRESHOLD", "BLUR_KERNEL", "BLUR_KSIZE", "BLUR_SIGMA", "HALFTONE_CELL", "HALFTONE_ANGLE",
    "HALFTONE_MIN_DOT", "HALFTONE_GAMMA", "DITHER_THRESHOLD",
    "to_gray", "load_image", "brightness_contrast", "binarize", "invert", "gaussian_blur",
    "flip_h", "flip_v", "crop", "target_size", "resize_nearest", "resize_to_mm",
    "floyd_steinberg", "halftone",
]

# --- constants (all O, see docs/engrave.md) ------------------------------------------------------
#: ImageHandle.ImageBinarization (0x06000050) IL_0006: ``ldc.r8 170``; maxval 255, type 0 (BINARY).
BINARIZE_THRESHOLD = 170
#: ImageHandle.GaussianBlur (0x06000051) IL_0008: ``new Size(7, 7)``, sigmaX = sigmaY = 0, border 4.
BLUR_KSIZE = 7
#: sigma that OpenCV derives for ksize 7 when sigma <= 0: 0.3*((7-1)*0.5-1)+0.8 (I, OpenCV rule).
BLUR_SIGMA = 1.4
#: OpenCV's fixed 7-tap table used for ksize 7 / sigma <= 0 (getGaussianKernelBitExact), in 1/256
#: units as used by the bit-exact 8U path: {0.03125, 0.109375, 0.21875, 0.28125, ...} * 256 (I).
BLUR_KERNEL = (8, 28, 56, 72, 56, 28, 8)
#: Gcode.FloydSteinberg (0x06000040) IL_004c: ``ldc.i4 128`` (old < 128 -> 0 else 255).
DITHER_THRESHOLD = 128
#: Gcode.HalftoneFilter (0x0600003e) IL_0013..IL_003e: cell 6.0 px, min dot 0.23, gamma 0.7,
#: angle 0.5235987755982988 rad (30 degrees).
HALFTONE_CELL = 6.0
HALFTONE_MIN_DOT = 0.23
HALFTONE_GAMMA = 0.7
HALFTONE_ANGLE = 0.5235987755982988

_F32 = np.float32


# ------------------------------------------------------------------------------------- loading
def _bgr_to_gray(r: np.ndarray, g: np.ndarray, b: np.ndarray) -> np.ndarray:
    """OpenCV ``cvtColor(COLOR_BGR2GRAY)`` for 8-bit data (I: OpenCV 4.x ``RGB2Gray<uchar>``,
    15-bit fixed point: Y = (B*3735 + G*19235 + R*9798 + 2^14) >> 15).  The original calls it with
    code 6 in ImageHandle.Set_GrayImage (0x06000052) IL_0052/IL_009a (O)."""
    y = (b.astype(np.uint32) * 3735 + g.astype(np.uint32) * 19235 + r.astype(np.uint32) * 9798
         + (1 << 14)) >> 15
    return np.minimum(y, 255).astype(np.uint8)


def to_gray(arr: np.ndarray) -> np.ndarray:
    """Convert an image array to the tool's 8-bit gray image (ImageHandle.Set_GrayImage, O).

    * 2-D array: returned as is (one channel).
    * H x W x 3 (RGB order, as Pillow/numpy deliver it): BGR2GRAY.
    * H x W x 4 (RGBA): gray of the colour channels, then every pixel whose alpha is exactly 0 is
      set to white (255) -- ``gray.CopyTo(white, alpha)``: the alpha plane is a *mask*, any
      non-zero alpha keeps the colour's gray value (O, IL_0010..IL_0084; no alpha blending).
    * H x W x 2 (gray + alpha): treated like RGBA with R=G=B (I: OpenCV's PNG reader expands
      gray+alpha to BGRA for IMREAD_UNCHANGED).
    16-bit input is reduced to 8 bits by ``>> 8`` (U: the original would hand a CV_16U Mat on).
    """
    a = np.asarray(arr)
    if a.dtype != np.uint8:
        if a.dtype == np.uint16:
            a = (a >> 8).astype(np.uint8)
        elif a.dtype == bool:
            a = a.astype(np.uint8) * 255
        else:
            a = np.clip(a, 0, 255).astype(np.uint8)
    if a.ndim == 2:
        return a.copy()
    if a.ndim != 3:
        raise ValueError("expected a 2-D gray or 3-D colour array")
    ch = a.shape[2]
    if ch == 1:
        return a[:, :, 0].copy()
    if ch == 2:
        gray, alpha = a[:, :, 0].copy(), a[:, :, 1]
        gray[alpha == 0] = 255
        return gray
    gray = _bgr_to_gray(a[:, :, 0], a[:, :, 1], a[:, :, 2])
    if ch >= 4:
        gray[a[:, :, 3] == 0] = 255
    return gray


def load_image(src: Union[str, "object"]) -> np.ndarray:
    """Load any Pillow-readable file (or an open ``PIL.Image``) as the tool's gray image.

    The original opens files with ``Cv2.ImRead(name, IMREAD_UNCHANGED)`` and then
    ``Set_GrayImage`` (ImageHandle.OpenImage 0x06000054 IL_004b..IL_0054, O).  Pillow modes are
    mapped so that the colour/alpha planes reach :func:`to_gray` like OpenCV would deliver them:
    palette images with transparency and LA/RGBA keep their alpha, everything else becomes RGB or
    L.  (I: OpenCV expands palette PNGs to BGR(A).)
    """
    from PIL import Image

    im = src if isinstance(src, Image.Image) else Image.open(src)
    try:
        im.load()
        mode = im.mode
        if mode == "P":
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        elif mode in ("1", "L"):
            im = im.convert("L")
        elif mode in ("I;16", "I;16B", "I;16L", "I"):
            arr16 = np.asarray(im).astype(np.int64)
            if arr16.max(initial=0) > 255:
                arr16 = arr16 >> 8
            return np.clip(arr16, 0, 255).astype(np.uint8)
        elif mode == "LA":
            pass
        elif mode == "PA":
            im = im.convert("RGBA")
        elif mode == "RGBA":
            pass
        elif mode in ("RGBa", "La"):
            im = im.convert("RGBA")
        elif mode == "CMYK" or mode == "YCbCr" or mode == "LAB" or mode == "HSV" or mode == "F":
            im = im.convert("RGB") if mode != "F" else im.convert("L")
        else:
            im = im.convert("RGBA" if "A" in mode else "RGB")
        return to_gray(np.asarray(im))
    finally:
        if not (isinstance(src, Image.Image)):
            im.close()


# --------------------------------------------------------------------------- pixel operations
def brightness_contrast(img: np.ndarray, brightness: int = 100, contrast: int = 100) -> np.ndarray:
    """ImageHandle.AdjustContrastBrightness / ...Direct (0x0600004d / 0x0600004e), O.

    Both track bars run 0..200 with neutral value 100 (MyFrom.WidgetInit 0x06000012).  With
    ``c = contrast/100f`` and ``b = brightness/100f`` every byte becomes, in float32::

        v = ((p - 128f) * c + 128f) + ((b - 1f) * 255f)
        out = (byte) Math.Max(0f, Math.Min(255f, v))      # conv.u1 truncates

    (IL_00b5..IL_00fe).  100/100 is the identity.
    """
    c = _F32(_F32(contrast) / _F32(100.0))
    b = _F32(_F32(brightness) / _F32(100.0))
    p = np.asarray(img, dtype=np.uint8).astype(np.float32)
    v = (p - _F32(128.0)) * c + _F32(128.0)
    v = v + _F32(_F32(b - _F32(1.0)) * _F32(255.0))
    v = np.maximum(_F32(0.0), np.minimum(_F32(255.0), v))
    return v.astype(np.uint8)  # truncation toward zero, values are >= 0


def binarize(img: np.ndarray, threshold: int = BINARIZE_THRESHOLD) -> np.ndarray:
    """``Cv2.Threshold(src, dst, 170, 255, THRESH_BINARY)``: p > 170 -> 255 else 0 (O 0x06000050)."""
    a = np.asarray(img, dtype=np.uint8)
    return np.where(a > threshold, 255, 0).astype(np.uint8)


def invert(img: np.ndarray) -> np.ndarray:
    """``Cv2.BitwiseNot`` (ImageHandle.InvertImageColors 0x06000053, O)."""
    return (255 - np.asarray(img, dtype=np.uint8)).astype(np.uint8)


def _reflect101_index(n: int, pad: int) -> np.ndarray:
    """Source indices for an axis of length n padded by ``pad`` with BORDER_REFLECT_101."""
    idx = np.arange(-pad, n + pad)
    if n == 1:
        return np.zeros_like(idx)
    period = 2 * (n - 1)
    idx = np.abs(idx) % period
    return np.where(idx >= n, period - idx, idx)


def gaussian_blur(img: np.ndarray, kernel=BLUR_KERNEL) -> np.ndarray:
    """``Cv2.GaussianBlur(src, dst, new Size(7,7), 0, 0, BorderTypes.Reflect101)`` (O 0x06000051).

    Separable convolution in integer arithmetic, reproducing OpenCV's bit-exact 8U path (I):
    the horizontal pass keeps 8 fractional bits exactly, the vertical pass gives 16 fractional
    bits, and the result is rounded half up: ``(sum + 2^15) >> 16``.  Kernel taps are in 1/256.
    """
    a = np.asarray(img, dtype=np.uint8)
    k = np.asarray(kernel, dtype=np.int64)
    if int(k.sum()) != 256:
        raise ValueError("kernel taps must sum to 256 (8 fractional bits)")
    r = len(k) // 2
    rows, cols = a.shape
    xi = _reflect101_index(cols, r)
    yi = _reflect101_index(rows, r)
    src = a.astype(np.int64)
    padded = src[:, xi]
    h = np.zeros((rows, cols), dtype=np.int64)
    for t in range(len(k)):
        h += k[t] * padded[:, t:t + cols]
    hp = h[yi, :]
    v = np.zeros((rows, cols), dtype=np.int64)
    for t in range(len(k)):
        v += k[t] * hp[t:t + rows, :]
    return np.minimum((v + (1 << 15)) >> 16, 255).astype(np.uint8)


def flip_h(img: np.ndarray) -> np.ndarray:
    """``Cv2.Flip(src, dst, 1)``: mirror left/right (MyFrom.button10_Click 0x0600001d, O)."""
    return np.ascontiguousarray(np.asarray(img)[:, ::-1])


def flip_v(img: np.ndarray) -> np.ndarray:
    """``Cv2.Flip(src, dst, 0)``: mirror top/bottom (MyFrom.button11_Click 0x0600001e, O)."""
    return np.ascontiguousarray(np.asarray(img)[::-1, :])


#: KJNova_Clipper.CropImage (0x06000068) IL_0018/IL_0027: selections narrower or lower than 10
#: pixels (on screen) leave the image unchanged.
CROP_MIN_SIZE = 10


def crop(img: np.ndarray, x: int, y: int, w: int, h: int, min_size: int = CROP_MIN_SIZE) -> np.ndarray:
    """Cut the rectangle (x, y, w, h) in image pixels (top-left origin).

    The original crop window (KJNova_Clipper) works on a zoomed preview; it maps the selection
    back with GetAbsoluteRect (0x06000069, float32 scale, ``conv.i4`` truncation) and copies it
    1:1 with ``Graphics.DrawImage(.., GraphicsUnit.Pixel)``.  A selection smaller than 10 px in
    either direction returns the image unchanged (O, IL_0000..IL_0031).  Here the rectangle is
    given directly in image pixels and clipped to the image (parts outside the source stay
    transparent black in the original; I).
    """
    a = np.asarray(img)
    if w < min_size or h < min_size:
        return a.copy()
    rows, cols = a.shape[:2]
    out = np.zeros((h, w) + a.shape[2:], dtype=a.dtype)
    sx0, sy0 = max(0, x), max(0, y)
    sx1, sy1 = min(cols, x + w), min(rows, y + h)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y:sy1 - y, sx0 - x:sx1 - x] = a[sy0:sy1, sx0:sx1]
    return out


# ---------------------------------------------------------------------------------- resizing
def target_size(width_mm: int, height_mm: int, rho: int) -> tuple[int, int]:
    """Pixel grid of the engraving: ``new Size(rho*W, rho*H)`` -> (cols, rows) (O
    MyFrom.button9_Click 0x06000020 IL_0090..IL_009b; all three are ``int.Parse`` integers)."""
    return int(rho) * int(width_mm), int(rho) * int(height_mm)


def resize_nearest(img: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """``Cv2.Resize(src, dst, new Size(cols, rows), 0, 0, InterpolationFlags.Nearest)`` (O, IL_00c1).

    OpenCV ``resizeNN`` (I): ``inv = dst/src`` as double, ``ifx = 1/inv``,
    ``sx = min(floor(x*ifx), src_w-1)`` and the same for rows.
    """
    a = np.asarray(img)
    sh, sw = a.shape[:2]
    if cols <= 0 or rows <= 0:
        raise ValueError("target size must be positive")
    ifx = 1.0 / (float(cols) / float(sw))
    ify = 1.0 / (float(rows) / float(sh))
    sx = np.minimum(np.floor(np.arange(cols, dtype=np.float64) * ifx).astype(np.int64), sw - 1)
    sy = np.minimum(np.floor(np.arange(rows, dtype=np.float64) * ify).astype(np.int64), sh - 1)
    return np.ascontiguousarray(a[sy][:, sx])


def resize_to_mm(img: np.ndarray, width_mm: int, height_mm: int, rho: int) -> np.ndarray:
    """Resample to the engraving grid of ``rho`` lines (pixels) per mm in both directions."""
    cols, rows = target_size(width_mm, height_mm, rho)
    return resize_nearest(img, cols, rows)


# -------------------------------------------------------------------------- processing modes
def floyd_steinberg(img: np.ndarray, threshold: int = DITHER_THRESHOLD) -> np.ndarray:
    """1-bit Floyd-Steinberg dithering, Gcode.FloydSteinberg (0x06000040) + DistributeError
    (0x0600003f), O.

    The image is converted to float32 (``ConvertTo(CV_32F)``) and scanned row by row, left to
    right in every row (no serpentine).  ``new = old < 128 ? 0 : 255``, ``err = old - new``
    (float32) and the error goes to (x+1, y) * 0.4375, (x-1, y+1) * 0.1875, (x, y+1) * 0.3125,
    (x+1, y+1) * 0.0625 (float32 products and sums, out-of-range targets dropped).  Output is
    0/255 uint8.  The per-row work is ordered so that every float32 addition happens in the same
    sequence as in the original loop.
    """
    a = np.asarray(img, dtype=np.uint8)
    rows, cols = a.shape
    f = a.astype(np.float32)
    out = np.zeros((rows, cols), dtype=np.uint8)
    thr = _F32(threshold)
    k7, k3, k5, k1 = _F32(0.4375), _F32(0.1875), _F32(0.3125), _F32(0.0625)
    zero, full = _F32(0.0), _F32(255.0)
    for y in range(rows):
        row = f[y]
        err = np.empty(cols, dtype=np.float32)
        carry = None
        vals = row.tolist()
        for x in range(cols):
            old = _F32(vals[x]) if carry is None else _F32(_F32(vals[x]) + carry)
            new = zero if old < thr else full
            e = _F32(old - new)
            err[x] = e
            if new:
                out[y, x] = 255
            carry = _F32(e * k7)
        if y + 1 < rows and cols:
            nxt = f[y + 1]
            # contributions arrive in the order x-1 (1/16), x (5/16), x+1 (3/16)
            nxt[1:] = nxt[1:] + err[:-1] * k1
            nxt[:] = nxt + err * k5
            nxt[:-1] = nxt[:-1] + err[1:] * k3
    return out


def halftone(img: np.ndarray, cell: float = HALFTONE_CELL, min_dot: float = HALFTONE_MIN_DOT,
             gamma: float = HALFTONE_GAMMA, angle: float = HALFTONE_ANGLE) -> np.ndarray:
    """Amplitude-modulated halftone screen, Gcode.HalftoneFilter (0x0600003e), O.

    * A square grid of ``cell`` (6.0) px is rotated by 30 degrees about the image centre
      ``(cols/2, rows/2)``: ``u = dx*cos - dy*sin``, ``v = dx*sin + dy*cos`` (float64), offset by
      the minimum (u, v) of the four image corners so cell indices start at 0.
      ``gridCols = max(1, ceil(rangeU/cell))``, ``gridRows`` likewise; indices are clamped.
    * Per cell the float32 mean of its pixels gives the dot radius (radius in px):
      empty cell 0; mean <= 0: ``cell/2``; mean >= 255: ``minR*0.5``; otherwise
      ``max(cell/2 * (1-mean/255)^0.7, minR)`` with ``minR = cell/2 * clamp(0.23, 0, 1)``.
    * Output starts white (255); a pixel becomes black (0) when its squared distance from its
      cell centre ``(gi*cell + cell/2, gj*cell + cell/2)`` is ``<= radius^2``.
    """
    a = np.asarray(img, dtype=np.uint8)
    rows, cols = a.shape
    half = cell / 2.0
    cs, sn = math.cos(angle), math.sin(angle)
    cx, cy = cols / 2.0, rows / 2.0
    us, vs = [], []
    for (px, py) in ((0.0, 0.0), (float(cols), 0.0), (0.0, float(rows)), (float(cols), float(rows))):
        dx, dy = px - cx, py - cy
        us.append(dx * cs - dy * sn)
        vs.append(dx * sn + dy * cs)
    min_u, max_u = min(us), max(us)
    min_v, max_v = min(vs), max(vs)
    off_u, off_v = -min_u, -min_v
    grid_cols = max(1, int(math.ceil((max_u - min_u) / cell)))
    grid_rows = max(1, int(math.ceil((max_v - min_v) / cell)))

    dy = (np.arange(rows, dtype=np.float64) - cy)[:, None]
    dx = (np.arange(cols, dtype=np.float64) - cx)[None, :]
    u = dx * cs - dy * sn
    v = dx * sn + dy * cs
    uu = u + off_u
    vv = v + off_v
    gi = np.clip(np.floor(uu / cell).astype(np.int64), 0, grid_cols - 1)
    gj = np.clip(np.floor(vv / cell).astype(np.int64), 0, grid_rows - 1)
    idx = gj * grid_cols + gi

    n = grid_cols * grid_rows
    sums = np.zeros(n, dtype=np.float32)
    np.add.at(sums, idx.ravel(), a.ravel().astype(np.float32))
    counts = np.bincount(idx.ravel(), minlength=n)

    min_r = half * max(0.0, min(1.0, min_dot))
    radius = np.zeros(n, dtype=np.float32)
    gamma_on = abs(gamma - 1.0) > 1e-9
    for i in range(n):
        c = int(counts[i])
        if c == 0:
            radius[i] = 0.0
            continue
        mean = float(_F32(sums[i] / _F32(c)))
        if not mean > 0.0:
            radius[i] = _F32(half)
        elif not mean < 255.0:
            radius[i] = _F32(min_r * 0.5)
        else:
            t = 1.0 - mean / 255.0
            if gamma_on:
                t = math.pow(t, gamma)
            radius[i] = _F32(max(half * t, min_r))

    cu = gi.astype(np.float64) * cell + half
    cv = gj.astype(np.float64) * cell + half
    du = uu - cu
    dv = vv - cv
    d2 = du * du + dv * dv
    r = radius[idx].astype(np.float64)
    out = np.full((rows, cols), 255, dtype=np.uint8)
    out[d2 <= r * r] = 0
    return out
