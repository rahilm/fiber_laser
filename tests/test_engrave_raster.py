"""Tests for mlaser.engrave (port of Bitmap/next_cut.dll "PicConvert(1.06) (BitmapRaster)")."""
import numpy as np
import pytest

from mlaser.engrave import (
    FOOTER, HEADER, EditSession, RasterParams, RasterSegment, aspect_height, aspect_width,
    binarize, brightness_contrast, crop, flip_h, flip_v, floyd_steinberg, format_f3,
    gaussian_blur, gcode_from_bitmap, generate_gcode, generate_to_dir, halftone, invert,
    load_image, pixel_power, process_image, raster_segments, read_status_ini, resize_nearest,
    target_size, to_gray, write_status_ini,
)
from mlaser.engrave import image_ops as ops


# ----------------------------------------------------------------------------- gray / loading
def test_to_gray_rgb_fixed_point():
    rgb = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255], [10, 10, 10]]], dtype=np.uint8)
    g = to_gray(rgb)
    assert g.tolist() == [[(255 * 9798 + 16384) >> 15, (255 * 19235 + 16384) >> 15,
                           (255 * 3735 + 16384) >> 15, 10]]


def test_to_gray_alpha_is_a_mask():
    rgba = np.array([[[0, 0, 0, 0], [0, 0, 0, 1], [200, 200, 200, 255]]], dtype=np.uint8)
    assert to_gray(rgba).tolist() == [[255, 0, 200]]
    la = np.array([[[0, 0], [50, 9]]], dtype=np.uint8)
    assert to_gray(la).tolist() == [[255, 50]]


def test_load_image_png_with_alpha(tmp_path):
    from PIL import Image
    a = np.zeros((4, 5, 4), dtype=np.uint8)
    a[..., 3] = 255
    a[0, 0, 3] = 0
    a[1, 1, :3] = 128
    p = tmp_path / "t.png"
    Image.fromarray(a, "RGBA").save(p)
    g = load_image(str(p))
    assert g.shape == (4, 5) and g.dtype == np.uint8
    assert g[0, 0] == 255 and g[1, 1] == 128 and g[2, 2] == 0
    Image.fromarray(np.full((3, 3), 77, np.uint8), "L").save(tmp_path / "g.bmp")
    assert (load_image(str(tmp_path / "g.bmp")) == 77).all()


# ------------------------------------------------------------------------------- pixel ops
def test_brightness_contrast_formula():
    img = np.array([[0, 100, 128, 200, 255]], dtype=np.uint8)
    assert (brightness_contrast(img, 100, 100) == img).all()
    # c = 1.5: (p-128)*1.5+128, truncated and clamped
    assert brightness_contrast(img, 100, 150).tolist() == [[0, 86, 128, 236, 255]]
    # b = 1.2 adds 0.2*255 = 51
    assert brightness_contrast(img, 120, 100).tolist() == [[51, 151, 179, 251, 255]]
    assert brightness_contrast(img, 0, 100).tolist() == [[0, 0, 0, 0, 0]]
    assert brightness_contrast(img, 100, 0).tolist() == [[128] * 5]


def test_binarize_invert_flip():
    img = np.array([[170, 171], [0, 255]], dtype=np.uint8)
    assert binarize(img).tolist() == [[0, 255], [0, 255]]
    assert invert(img).tolist() == [[85, 84], [255, 0]]
    assert flip_h(img).tolist() == [[171, 170], [255, 0]]
    assert flip_v(img).tolist() == [[0, 255], [170, 171]]


def _ref_blur(img):
    k = np.array(ops.BLUR_KERNEL, dtype=np.float64) / 256.0
    p = np.pad(img.astype(np.float64), 3, mode="reflect")  # numpy reflect == REFLECT_101
    tmp = sum(k[i] * p[:, i:i + img.shape[1]] for i in range(7))
    out = sum(k[i] * tmp[i:i + img.shape[0], :] for i in range(7))
    return np.floor(out + 0.5).astype(np.uint8)


def test_gaussian_blur_matches_reference_and_impulse():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, (13, 17), dtype=np.uint8)
    assert (gaussian_blur(img) == _ref_blur(img)).all()
    flat = np.full((9, 9), 93, np.uint8)
    assert (gaussian_blur(flat) == 93).all()
    imp = np.zeros((15, 15), np.uint8)
    imp[7, 7] = 255
    out = gaussian_blur(imp)
    assert out[7, 7] == (72 * 72 * 255 + 32768) >> 16
    assert out[7, 7] == out.max() and (out == out.T).all()
    assert ops.BLUR_SIGMA == pytest.approx(0.3 * ((7 - 1) * 0.5 - 1) + 0.8)


def test_crop_min_size_and_clip():
    img = np.arange(400, dtype=np.uint16).reshape(20, 20).astype(np.uint8)
    assert (crop(img, 2, 3, 12, 11) == img[3:14, 2:14]).all()
    assert (crop(img, 0, 0, 9, 15) == img).all()        # narrower than 10 px: unchanged
    c = crop(img, 15, 15, 10, 10)                       # partly outside: zero padding
    assert c.shape == (10, 10) and (c[:5, :5] == img[15:, 15:]).all() and (c[5:, :] == 0).all()


def test_resize_nearest_and_target_size():
    img = np.array([[0, 1, 2, 3]], dtype=np.uint8)
    assert resize_nearest(img, 6, 1).tolist() == [[0, 0, 1, 2, 2, 3]]
    assert resize_nearest(img, 2, 3).tolist() == [[0, 2]] * 3
    assert target_size(60, 45, 6) == (360, 270)


def test_aspect_lock_float32_truncation():
    assert aspect_height(64, 48, 60) == 44           # 60 / (float)1.3333334 = 44.99999
    assert aspect_height(100, 50, 60) == 30
    assert aspect_width(64, 48, 45) == 60
    assert aspect_width(100, 50, 30) == 60


# ------------------------------------------------------------------------------- dithering
def _naive_fs(img):
    f = img.astype(np.float32)
    h, w = f.shape
    out = np.zeros((h, w), np.uint8)
    for y in range(h):
        for x in range(w):
            old = f[y, x]
            new = np.float32(0) if old < np.float32(128) else np.float32(255)
            e = np.float32(old - new)
            out[y, x] = int(new)
            for dx, dy, k in ((1, 0, 0.4375), (-1, 1, 0.1875), (0, 1, 0.3125), (1, 1, 0.0625)):
                xx, yy = x + dx, y + dy
                if 0 <= xx < w and 0 <= yy < h:
                    f[yy, xx] = np.float32(f[yy, xx] + np.float32(e * np.float32(k)))
    return out


def test_floyd_steinberg_matches_sequential_reference():
    rng = np.random.default_rng(7)
    img = rng.integers(0, 256, (11, 13), dtype=np.uint8)
    assert (floyd_steinberg(img) == _naive_fs(img)).all()


def test_floyd_steinberg_levels():
    assert (floyd_steinberg(np.zeros((8, 8), np.uint8)) == 0).all()
    assert (floyd_steinberg(np.full((8, 8), 255, np.uint8)) == 255).all()
    out = floyd_steinberg(np.full((60, 60), 127, np.uint8))
    assert set(np.unique(out).tolist()) <= {0, 255}
    assert 0.45 < (out == 255).mean() < 0.55


def test_halftone_screen():
    white = halftone(np.full((60, 60), 255, np.uint8))
    black = halftone(np.zeros((60, 60), np.uint8))
    mid = halftone(np.full((60, 60), 128, np.uint8))
    assert set(np.unique(mid).tolist()) <= {0, 255}
    assert (white == 0).mean() < 0.05
    assert (black == 0).mean() > 0.7
    # dot radius for mean 128: 3 * (1-128/255)^0.7 -> area fraction ~ pi r^2 / 36
    r = 3 * (1 - 128 / 255) ** 0.7
    assert (mid == 0).mean() == pytest.approx(np.pi * r * r / 36, abs=0.04)
    darker = [(halftone(np.full((48, 48), v, np.uint8)) == 0).mean() for v in (200, 128, 50)]
    assert darker[0] < darker[1] < darker[2]


# ------------------------------------------------------------------------- segments / power
def test_power_mapping_threshold():
    assert pixel_power(125, 20) == 20
    assert pixel_power(126, 20) == 0
    assert pixel_power(0, 33) == 33


def test_raster_segments_run_length():
    img = np.array([[0, 0, 255, 255, 255, 0], [200] * 6, [125, 126, 125, 126, 0, 0]], np.uint8)
    segs = raster_segments(img, 20)
    as_t = [[(s.pixel, s.power) for s in row] for row in segs]
    assert as_t == [[(2, 20), (3, 0), (1, 20)], [(6, 0)], [(1, 20), (1, 0), (1, 20), (1, 0), (2, 20)]]
    assert all(isinstance(s, RasterSegment) for s in segs[0])


# -------------------------------------------------------------------------------- G-code
def test_gcode_exact_small_serpentine():
    bmp = np.array([[0, 255], [255, 0]], dtype=np.uint8)
    g = gcode_from_bitmap(bmp, 1, 20)
    expected = HEADER + (
        "G0 X-5 Y0\n"
        "G0 X0.000 Y0.000 S0\n"      # bottom image row first, +X
        "G0 X1.000 Y0.000 S0\n"
        "G1 X2.000 Y0.000 S20\n"
        "G0 X7.000 Y1.000 S0\n"      # 5 mm overscan + line step
        "G0 X2.000 Y1.000 S0\n"      # second line runs -X
        "G0 X1.000 Y1.000 S0\n"
        "G1 X0.000 Y1.000 S20\n"
        "G0 X-5.000 Y2.000 S0\n"
    ) + FOOTER
    assert g == expected
    assert HEADER == "(BitmapRaster)\n(V1.06)\n(2025-11-26)\n\nG90\nM4\n"
    assert FOOTER == "G0 X0 Y0 S0\nM5\n"


def test_gcode_structure_default_params():
    img = np.tile(np.array([0, 255], np.uint8), (30, 20))  # 30 x 40 stripes
    p = RasterParams(width_mm=10)
    g = generate_gcode(img, p)
    lines = g.split("\n")
    assert lines[:7] == ["(BitmapRaster)", "(V1.06)", "(2025-11-26)", "", "G90", "M4", "G0 X-5 Y0"]
    assert lines[-3:] == ["G0 X0 Y0 S0", "M5", ""]
    assert "\r" not in g
    burns = [ln for ln in lines if ln.startswith("G1 ")]
    assert burns and all(ln.endswith(" S20") for ln in burns)
    h_mm = aspect_height(40, 30, 10)
    rows = h_mm * 6
    # each raster line: 1 entry rapid + segments + 1 exit rapid
    assert sum(1 for ln in lines if ln.startswith("G0 ") and ln.endswith("S0")) >= 2 * rows
    ys = sorted({float(ln.split(" Y")[1].split()[0]) for ln in burns})
    assert ys[0] == 0.0 and ys[-1] == pytest.approx((rows - 1) / 6, abs=1e-3)
    assert len(ys) == rows


def test_power_zero_or_negative_never_burns():
    bmp = np.zeros((3, 4), np.uint8)
    for pw in (0, -5):
        g = gcode_from_bitmap(bmp, 2, pw)
        assert "G1" not in g


def test_format_f3_rounding_and_sign():
    assert format_f3(np.float32(1.0)) == "1.000"
    assert format_f3(np.float32(0.0625)) == "0.063"      # tie: away from zero
    assert format_f3(np.float32(-0.0004)) == "-0.000"
    assert format_f3(np.float32(1 / 6)) == "0.167"


def test_process_image_shape_and_modes():
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, (48, 64), dtype=np.uint8)
    bmp = process_image(img, RasterParams())
    assert bmp.shape == (44 * 6, 60 * 6)
    for mode in ("dither", "halftone"):
        out = process_image(img, RasterParams(mode=mode, width_mm=10, height_mm=8, rho=4))
        assert out.shape == (32, 40) and set(np.unique(out).tolist()) <= {0, 255}
    with pytest.raises(ValueError):
        process_image(img, RasterParams(mode="bogus"))


def test_edit_session_matches_pipeline_and_crop_quirk():
    rng = np.random.default_rng(5)
    img = rng.integers(0, 256, (30, 40), dtype=np.uint8)
    s = EditSession(img)
    s.set_brightness_contrast(110, 130)
    s.edit("invert")
    s.edit("flip_h")
    ref = brightness_contrast(flip_h(invert(img)), 110, 130)
    assert (s.current == ref).all()
    p = RasterParams(brightness=110, contrast=130, edits=("invert", "flip_h"), width_mm=20, height_mm=15)
    assert (process_image(img, p) == resize_nearest(ref, 120, 90)).all()
    s.edit(("crop", 2, 2, 20, 20))
    assert (s.current == ref[2:22, 2:22]).all()
    s.edit("flip_v")  # B/C applied a second time on top of the baked crop
    assert (s.current == brightness_contrast(flip_v(ref[2:22, 2:22]), 110, 130)).all()
    s.cancel()
    assert (s.current == img).all() and s.brightness == 100


# ------------------------------------------------------------------------------ status.ini
def test_status_ini_round_trip(tmp_path):
    assert read_status_ini(tmp_path) is None
    write_status_ini(tmp_path, 0)
    assert (tmp_path / "status.ini").read_bytes() == b"0"
    assert read_status_ini(tmp_path) == 0
    write_status_ini(tmp_path, True)
    assert read_status_ini(tmp_path) == 1


def test_generate_to_dir_and_parse_gcode(tmp_path):
    from mlaser.io.gcode import parse_gcode
    bmp = np.full((4, 6), 255, np.uint8)
    bmp[1, 1:4] = 0
    bmp[3, 0] = 0
    path = generate_to_dir(tmp_path, bmp, RasterParams(rho=2, power=30), preprocessed=True)
    assert path.name == "newGcode.nc" and read_status_ini(tmp_path) == 1
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("ascii")
    doc = parse_gcode(text)
    burns = [ln for ln in text.splitlines() if ln.startswith("G1")]
    assert len(doc.parts) == len(burns) == 2
    seg = doc.parts[0].contours[0].segments[0]
    # bottom row (image row 3) first: one pixel at x 0..0.5 mm, y 0
    assert seg.start == pytest.approx((0.0, 0.0)) and seg.end == pytest.approx((0.5, 0.0))
