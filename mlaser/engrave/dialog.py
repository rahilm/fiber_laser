"""PySide6 version of the PicConvert form (``next_cut.MyFrom``, 1062 x 701, title
"PicConvert(1.06)"), see docs/engrave.md.

Controls follow MyFrom.InitializeComponent (0x06000039) and the tool's own two-language table
(next_cut.Luaguage..ctor 0x06000073, rows 0..27, column 0 = 中文, 1 = English).  Differences
from the original, all deliberate:

* messages go to a status line instead of modal ``MessageBox`` calls (the dialog must never
  block, e.g. under the headless coverage tool that stubs ``QDialog.exec``);
* the original shows table row 18 ("确定"/"sure") where row 17 ("请先选择一个目标图片") is
  meant, and row 22 ("GCode生成成功") for a zero height; the rebuild shows 17 / 20;
* the processing mode is taken from the combo *index*, so it also works in English (the
  original compares the combo text with the Chinese item strings, so in English no processing
  mode is ever applied);
* the crop rectangle is entered with spin boxes (image pixels) instead of the KJNova_Clipper
  window with its 8 resize handles.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from . import image_ops as ops
from .raster import (
    GCODE_NAME, MODES, EditSession, RasterParams, aspect_height, aspect_width,
    gcode_from_bitmap, write_gcode, write_status_ini,
)

#: next_cut.Luaguage.LanguaLibrary[28, 2] (O, 0x06000073).
TEXTS = [
    ("打开图片", "Open Image"),
    ("图片尺寸（像素）:", "Image Size (Pixels):"),
    ("亮度:", "Brightness:"),
    ("对比度:", "contrast ratio:"),
    ("二值化", "Binarization"),
    ("高斯模糊", "Gaussian Blur"),
    ("反色", "Inverse color"),
    ("水平翻转", "Flip Horizontal"),
    ("图片裁剪", "Crop images‌"),
    ("取消编辑", "Cancel Editing"),
    ("垂直翻转", "Flip Vertical"),
    ("启用半调网屏", "Enable half tone screen"),
    ("语言设置:", "Language:"),
    ("设置绘图尺寸(MM):", "Set drawing size (MM):"),
    ("设置线密度大小（> 0）:", "Set line density size (>0):"),
    ("设置激光功率大小（> 0）:", "Set laser power size (>0):"),
    ("生成GCode", "Generate Gcode"),
    ("请先选择一个目标图片", "Please select a target image first"),
    ("确定", "sure"),
    ("取消", " Cancel"),
    ("请输入正确的高度", "Please enter the correct Hight"),
    ("请输入正确的宽度", "Please enter the correct Windth"),
    ("GCode生成成功", "success"),
    ("GCode生成中，请稍后......", "GCodeGenerating, please wait......"),
    ("不启用", "Not enabled"),
    ("1bit抖动", "1bit dithering"),
    ("半调网屏", "half-tone screen"),
    ("位   图   处   理 :", "bitmap processing:"),
]
#: OpenFileDialog filter "png|*.png|jpg|*.jpg|all|*.*" (O, OpenImage IL_0007) + bmp.
FILE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp);;png (*.png);;jpg (*.jpg *.jpeg);;bmp (*.bmp);;all (*.*)"


def _language_index(tr) -> int:
    """0 = 中文, 1 = English, from a rebuild translator (``.language`` code / ``.current`` index)
    or a plain string/int."""
    if tr is None:
        return 0
    if isinstance(tr, int):
        return 0 if tr == 0 else 1
    if isinstance(tr, str):
        code = tr
    else:
        code = getattr(tr, "language", None)
        if code is None:
            cur = getattr(tr, "current", None)
            if isinstance(cur, int):
                return 0 if cur == 0 else 1
            return 0
    code = str(code).lower()
    return 0 if code in ("zh", "cn", "zh_cn", "chinese", "中文", "简体中文") else 1


def _to_pixmap(img: np.ndarray) -> QPixmap:
    a = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = a.shape
    qimg = QImage(a.data, w, h, w, QImage.Format_Grayscale8).copy()
    return QPixmap.fromImage(qimg)


class BitmapDialog(QDialog):
    """Raster-engraving dialog.  After "Generate GCode", ``gcode_path`` holds the written
    ``newGcode.nc`` and the dialog is accepted.

    ``out_dir``: where ``newGcode.nc`` / ``status.ini`` go (the original uses its own exe
    directory, ``<install>\\Bitmap``).  When given, ``status.ini`` is reset to ``0`` at
    construction like the original start-up does; without it a temp directory is used at
    generation time.
    """

    def __init__(self, tr=None, parent: Optional[QWidget] = None, out_dir=None):
        super().__init__(parent)
        self.tr_ = tr
        self.lang = _language_index(tr)
        self.out_dir: Optional[Path] = Path(out_dir) if out_dir else None
        self.gcode_path: Optional[Path] = None
        self.gcode_text: Optional[str] = None
        self.session = EditSession()
        self.image_path: Optional[str] = None
        self._sync = False
        self.setWindowTitle("PicConvert(1.06)")
        self.resize(1062, 701)
        self._build()
        self._retranslate()
        if self.out_dir is not None:
            try:
                write_status_ini(self.out_dir, 0)
            except OSError:
                pass

    # ------------------------------------------------------------------ UI
    def _t(self, i: int) -> str:
        return TEXTS[i][self.lang]

    def _build(self) -> None:
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        self.open_button = QPushButton()
        self.open_button.clicked.connect(self.open_image)
        left.addWidget(self.open_button, 0, Qt.AlignLeft)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(560, 560)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview.setStyleSheet("background: #ffffff; border: 1px solid #a0a0a0;")
        left.addWidget(self.preview, 1)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        root.addLayout(right, 2)
        lang_row = QHBoxLayout()
        self.lang_label = QLabel()
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["中文", "English"])
        self.lang_combo.setCurrentIndex(self.lang)
        self.lang_combo.currentIndexChanged.connect(self._on_language)
        lang_row.addWidget(self.lang_label)
        lang_row.addWidget(self.lang_combo)
        lang_row.addStretch(1)
        right.addLayout(lang_row)

        size_row = QHBoxLayout()
        self.size_title = QLabel()
        self.size_value = QLabel("")
        size_row.addWidget(self.size_title)
        size_row.addWidget(self.size_value)
        size_row.addStretch(1)
        right.addLayout(size_row)

        sliders = QFormLayout()
        self.brightness_label = QLabel()
        self.brightness = QSlider(Qt.Horizontal)
        self.contrast_label = QLabel()
        self.contrast = QSlider(Qt.Horizontal)
        for s in (self.brightness, self.contrast):
            s.setRange(0, 200)
            s.setValue(100)
            s.setTickInterval(5)
            s.setTickPosition(QSlider.TicksBelow)
            s.valueChanged.connect(self._on_bc)
        sliders.addRow(self.brightness_label, self.brightness)
        sliders.addRow(self.contrast_label, self.contrast)
        right.addLayout(sliders)

        edit_box = QGroupBox()
        grid = QGridLayout(edit_box)
        self.crop_button = QPushButton()
        self.binarize_button = QPushButton()
        self.blur_button = QPushButton()
        self.cancel_button = QPushButton()
        self.invert_button = QPushButton()
        self.flip_h_button = QPushButton()
        self.flip_v_button = QPushButton()
        self.binarize_button.clicked.connect(lambda: self.apply_edit("binarize"))
        self.blur_button.clicked.connect(lambda: self.apply_edit("blur"))
        self.invert_button.clicked.connect(lambda: self.apply_edit("invert"))
        self.flip_h_button.clicked.connect(lambda: self.apply_edit("flip_h"))
        self.flip_v_button.clicked.connect(lambda: self.apply_edit("flip_v"))
        self.cancel_button.clicked.connect(self.cancel_editing)
        self.crop_button.clicked.connect(self.apply_crop)
        for i, b in enumerate((self.binarize_button, self.blur_button, self.invert_button,
                               self.flip_h_button, self.flip_v_button, self.cancel_button)):
            grid.addWidget(b, i // 3, i % 3)
        crop_row = QHBoxLayout()
        self.crop_x, self.crop_y, self.crop_w, self.crop_h = (QSpinBox() for _ in range(4))
        for sb, lab in zip((self.crop_x, self.crop_y, self.crop_w, self.crop_h), "XYWH"):
            sb.setRange(0, 100000)
            crop_row.addWidget(QLabel(lab))
            crop_row.addWidget(sb)
        crop_row.addWidget(self.crop_button)
        grid.addLayout(crop_row, 2, 0, 1, 3)
        right.addWidget(edit_box)

        mode_row = QHBoxLayout()
        self.mode_label = QLabel()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["", "", ""])
        mode_row.addWidget(self.mode_label)
        mode_row.addWidget(self.mode_combo, 1)
        right.addLayout(mode_row)

        size_box = QGroupBox()
        self.size_box = size_box
        sform = QFormLayout(size_box)
        self.width_mm = QSpinBox()
        self.height_mm = QSpinBox()
        for sb in (self.width_mm, self.height_mm):
            sb.setRange(0, 100000)
            sb.setSuffix(" mm")
        self.width_mm.setValue(50)   # InitializeComponent: setWidth.Text = "50"
        self.height_mm.setValue(50)  # SetHight.Text = "50"
        self.width_mm.valueChanged.connect(self._on_width)
        self.height_mm.valueChanged.connect(self._on_height)
        self.lock_aspect = QCheckBox("W:H")
        self.lock_aspect.setChecked(True)
        sform.addRow("W:", self.width_mm)
        sform.addRow("H:", self.height_mm)
        sform.addRow("", self.lock_aspect)
        right.addWidget(size_box)

        laser_form = QFormLayout()
        self.rho_label = QLabel()
        self.power_label = QLabel()
        self.rho = QSpinBox()
        self.rho.setRange(1, 1000)
        self.rho.setValue(6)
        self.power = QSpinBox()
        self.power.setRange(0, 100000)
        self.power.setValue(20)
        laser_form.addRow(self.rho_label, self.rho)
        laser_form.addRow(self.power_label, self.power)
        right.addLayout(laser_form)

        self.generate_button = QPushButton()
        self.generate_button.clicked.connect(self.generate)
        right.addWidget(self.generate_button)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        right.addStretch(1)

    def _retranslate(self) -> None:
        t = self._t
        self.open_button.setText(t(0))
        self.size_title.setText(t(1))
        self.brightness_label.setText(t(2))
        self.contrast_label.setText(t(3))
        self.binarize_button.setText(t(4))
        self.blur_button.setText(t(5))
        self.invert_button.setText(t(6))
        self.flip_h_button.setText(t(7))
        self.crop_button.setText(t(8))
        self.cancel_button.setText(t(9))
        self.flip_v_button.setText(t(10))
        self.lang_label.setText(t(12))
        self.size_box.setTitle(t(13))
        self.rho_label.setText(t(14))
        self.power_label.setText(t(15))
        self.generate_button.setText(t(16))
        for i in range(3):
            self.mode_combo.setItemText(i, t(24 + i))
        self.mode_label.setText(t(27))

    def _on_language(self, idx: int) -> None:
        self.lang = 0 if idx == 0 else 1
        self._retranslate()

    def _message(self, i_or_text) -> None:
        self.status.setText(self._t(i_or_text) if isinstance(i_or_text, int) else str(i_or_text))

    # ------------------------------------------------------------------ image handling
    def set_image(self, array: np.ndarray, path: Optional[str] = None) -> None:
        """Load an image array (gray/RGB/RGBA) as if opened from a file (OpenImage +
        button2_Click): sliders back to 100, W = 60 mm, H from the aspect ratio."""
        self.session.open(array)
        self.image_path = path
        self._sync = True
        self.brightness.setValue(100)
        self.contrast.setValue(100)
        self._sync = False
        h, w = self.session.current.shape
        self.crop_x.setValue(0)
        self.crop_y.setValue(0)
        self.crop_w.setValue(w)
        self.crop_h.setValue(h)
        self._set_size(60, None)
        self._refresh()

    def open_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self._t(0), "", FILE_FILTER)
        if not path:
            return
        try:
            arr = ops.load_image(path)
        except Exception as exc:  # the original shows Exception.Message
            self._message(str(exc))
            return
        self.set_image(arr, path)

    def _refresh(self) -> None:
        cur = self.session.current
        if cur is None:
            self.preview.clear()
            self.size_value.setText("")
            return
        h, w = cur.shape
        self.size_value.setText(f"{w}*{h}")
        pm = _to_pixmap(cur)
        size = self.preview.size()
        if size.width() > 1 and size.height() > 1:
            pm = pm.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(pm)

    def _require_image(self) -> bool:
        if not self.session.loaded:
            self._message(17)
            return False
        return True

    def apply_edit(self, name) -> None:
        if not self._require_image():
            return
        self.session.edit(name)
        self._refresh()

    def apply_crop(self) -> None:
        if not self._require_image():
            return
        self.session.edit(("crop", self.crop_x.value(), self.crop_y.value(),
                           self.crop_w.value(), self.crop_h.value()))
        h, w = self.session.current.shape
        self.crop_x.setValue(0)
        self.crop_y.setValue(0)
        self.crop_w.setValue(w)
        self.crop_h.setValue(h)
        self._set_size(self.width_mm.value(), None)  # Update_pixels after the clipper closes
        self._refresh()

    def cancel_editing(self) -> None:
        if not self._require_image():
            return
        self.session.cancel()
        self._sync = True
        self.brightness.setValue(100)
        self.contrast.setValue(100)
        self._sync = False
        self._set_size(self.width_mm.value(), None)
        self._refresh()

    def _on_bc(self, _v=None) -> None:
        if self._sync or not self.session.loaded:
            return
        self.session.set_brightness_contrast(self.brightness.value(), self.contrast.value())
        self._refresh()

    # ------------------------------------------------------------------ size / aspect
    def _set_size(self, w_mm: int, h_mm: Optional[int]) -> None:
        self._sync = True
        try:
            self.width_mm.setValue(int(w_mm))
            if h_mm is None and self.session.loaded:
                ih, iw = self.session.current.shape
                h_mm = aspect_height(iw, ih, int(w_mm)) if w_mm else self.height_mm.value()
            if h_mm is not None:
                self.height_mm.setValue(int(h_mm))
        finally:
            self._sync = False

    def _on_width(self, v: int) -> None:
        if self._sync or not self.lock_aspect.isChecked() or not self.session.loaded:
            return
        if v == 0:
            self._message(21)
            return
        ih, iw = self.session.current.shape
        self._sync = True
        self.height_mm.setValue(aspect_height(iw, ih, v))
        self._sync = False

    def _on_height(self, v: int) -> None:
        if self._sync or not self.lock_aspect.isChecked() or not self.session.loaded:
            return
        if v == 0:
            self._message(20)
            return
        ih, iw = self.session.current.shape
        self._sync = True
        self.width_mm.setValue(aspect_width(iw, ih, v))
        self._sync = False

    # ------------------------------------------------------------------ parameters / output
    @property
    def params(self) -> RasterParams:
        """Current form state as :class:`RasterParams` (height always explicit)."""
        return RasterParams(
            brightness=self.brightness.value(), contrast=self.contrast.value(),
            edits=tuple(self.session.history), mode=MODES[max(0, self.mode_combo.currentIndex())],
            width_mm=self.width_mm.value(), height_mm=self.height_mm.value(),
            rho=self.rho.value(), power=self.power.value())

    def set_mode(self, mode: str) -> None:
        self.mode_combo.setCurrentIndex(MODES.index(mode))

    def engraving_bitmap(self) -> np.ndarray:
        """The bitmap button9_Click hands to generateGCode0: resize + processing mode."""
        p = self.params
        bmp = ops.resize_to_mm(self.session.current, p.width_mm, p.height_mm, p.rho)
        if p.mode == "dither":
            bmp = ops.floyd_steinberg(bmp)
        elif p.mode == "halftone":
            bmp = ops.halftone(bmp)
        return bmp

    def generate(self) -> Optional[Path]:
        """"Generate GCode": write ``newGcode.nc`` + ``status.ini`` = 1 and accept."""
        if not self._require_image():
            return None
        p = self.params
        if p.width_mm <= 0:
            self._message(21)
            return None
        if p.height_mm <= 0:
            self._message(20)
            return None
        self._message(23)
        text = gcode_from_bitmap(self.engraving_bitmap(), p.rho, p.power)
        out = self.out_dir or Path(tempfile.gettempdir()) / "mlaser" / "Bitmap"
        out.mkdir(parents=True, exist_ok=True)
        path = write_gcode(out / GCODE_NAME, text)
        write_status_ini(out, 1)
        self.gcode_text = text
        self.gcode_path = path
        self._message(22)
        self.accept()
        return path
