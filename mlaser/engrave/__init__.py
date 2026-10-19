"""Raster engraving: port of the companion tool ``Bitmap/Bitmap.exe`` (next_cut.dll,
"PicConvert(1.06) (BitmapRaster)").  See docs/engrave.md.

``image_ops`` holds the pixel operations, ``raster`` the parameters, the pipeline, the
G-code writer and the ``status.ini`` protocol; ``dialog`` (imported lazily, needs PySide6) the
form.
"""
from .image_ops import (
    binarize, brightness_contrast, crop, flip_h, flip_v, floyd_steinberg, gaussian_blur, halftone,
    invert, load_image, resize_nearest, resize_to_mm, target_size, to_gray,
)
from .raster import (
    FOOTER, GCODE_NAME, HEADER, MODE_DITHER, MODE_HALFTONE, MODE_LABELS, MODE_NONE, MODES,
    OVERSCAN_MM, POWER_THRESHOLD, STATUS_NAME, SVG_NAME, EditSession, RasterParams, RasterSegment,
    apply_edits, aspect_height, aspect_width, format_f3, gcode_from_bitmap, generate_gcode,
    generate_to_dir, pixel_power, process_image, raster_from_bitmap, raster_segments, read_status_ini, segment_gcode,
    write_gcode, write_status_ini,
)

__all__ = [
    "binarize", "brightness_contrast", "crop", "flip_h", "flip_v", "floyd_steinberg",
    "gaussian_blur", "halftone", "invert", "load_image", "resize_nearest", "resize_to_mm",
    "target_size", "to_gray",
    "FOOTER", "GCODE_NAME", "HEADER", "MODE_DITHER", "MODE_HALFTONE", "MODE_LABELS", "MODE_NONE",
    "MODES", "OVERSCAN_MM", "POWER_THRESHOLD", "STATUS_NAME", "SVG_NAME", "EditSession",
    "RasterParams", "RasterSegment", "apply_edits", "aspect_height", "aspect_width", "format_f3",
    "gcode_from_bitmap", "generate_gcode", "generate_to_dir", "pixel_power", "process_image",
    "raster_from_bitmap",
    "raster_segments", "read_status_ini", "segment_gcode", "write_gcode", "write_status_ini",
]


def __getattr__(name):
    if name == "BitmapDialog":
        from .dialog import BitmapDialog
        return BitmapDialog
    raise AttributeError(name)
