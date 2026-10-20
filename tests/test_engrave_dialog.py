"""Qt tests for mlaser.ui.engrave_dialog.BitmapDialog (headless)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog  # noqa: E402

from mlaser.engrave import RasterParams, generate_gcode, read_status_ini  # noqa: E402
from mlaser.ui.engrave_dialog import ENGLISH, TEXTS, BitmapDialog  # noqa: E402
from mlaser.ui.style import Dialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _img():
    rng = np.random.default_rng(11)
    return rng.integers(0, 256, (48, 64), dtype=np.uint8)


def test_construct_resets_status(app, tmp_path):
    (tmp_path / "status.ini").write_bytes(b"1")
    d = BitmapDialog(out_dir=tmp_path)
    assert read_status_ini(tmp_path) == 0
    assert isinstance(d, Dialog) and d.windowTitle() == "Bitmap engraving"
    p = d.params
    assert (p.rho, p.power, p.width_mm, p.height_mm, p.mode) == (6, 20, 50, 50, "none")
    assert d.open_button.text() == ENGLISH[0]          # no translator: the application's English
    d.deleteLater()


def test_language_from_translator(app):
    class Tr:
        language = "en"
    d = BitmapDialog(tr=Tr())
    assert d.open_button.text() == "Open image…"
    assert d.mode_combo.itemText(1) == "1-bit dithering"
    assert not hasattr(d, "lang_combo")                # the application's language, no own combo

    class Zh:
        language = "zh"
    z = BitmapDialog(tr=Zh())
    assert z.generate_button.text() == TEXTS[16][0] == "生成GCode"
    z.set_language(1)
    assert z.generate_button.text() == "Generate G-code"
    z.deleteLater()
    d.deleteLater()


def test_set_image_and_aspect_lock(app):
    d = BitmapDialog()
    d.set_image(_img())
    assert d.size_value.text() == "64*48"
    assert d.width_mm.value() == 60 and d.height_mm.value() == 44
    d.width_mm.setValue(80)
    assert d.height_mm.value() == 59  # 80 / (float)1.3333334 truncated, like the original
    d.width_mm.setValue(100)
    assert d.height_mm.value() == 74
    d.height_mm.setValue(30)
    assert d.width_mm.value() == 40
    d.lock_aspect.setChecked(False)
    d.height_mm.setValue(10)
    assert d.width_mm.value() == 40
    d.deleteLater()


def test_edits_sliders_and_cancel(app):
    d = BitmapDialog()
    img = _img()
    d.set_image(img)
    d.brightness.setValue(120)
    d.invert_button.click()
    d.flip_h_button.click()
    d.flip_v_button.click()
    d.binarize_button.click()
    d.blur_button.click()
    p = d.params
    assert p.edits == ("invert", "flip_h", "flip_v", "binarize", "blur") and p.brightness == 120
    d.crop_x.setValue(4)
    d.crop_y.setValue(4)
    d.crop_w.setValue(32)
    d.crop_h.setValue(20)
    d.crop_button.click()
    assert d.session.current.shape == (20, 32)
    assert d.height_mm.value() == 37  # Update_pixels after crop: 60 / 1.6
    d.cancel_button.click()
    assert d.session.current.shape == (48, 64) and d.brightness.value() == 100
    assert (d.session.current == img).all()
    d.deleteLater()


def test_generate_writes_and_accepts(app, tmp_path):
    d = BitmapDialog(out_dir=tmp_path)
    img = _img()
    d.set_image(img)
    d.set_mode("dither")
    d.rho.setValue(2)
    d.power.setValue(35)
    path = d.generate()
    assert path == d.gcode_path == tmp_path / "newGcode.nc"
    assert d.result() == QDialog.Accepted
    assert read_status_ini(tmp_path) == 1
    text = path.read_text()
    assert text == generate_gcode(img, d.params)
    assert text == generate_gcode(img, RasterParams(mode="dither", rho=2, power=35, width_mm=60, height_mm=44))
    assert " S35" in text
    d.deleteLater()


def test_generate_without_image_does_nothing(app, tmp_path):
    d = BitmapDialog(out_dir=tmp_path)
    assert d.generate() is None
    assert d.gcode_path is None and read_status_ini(tmp_path) == 0
    assert d.status.text() == ENGLISH[17]
    d.invert_button.click()  # no crash without image
    d.deleteLater()


def test_open_image_uses_file_dialog(app, tmp_path, monkeypatch):
    from PIL import Image
    p = tmp_path / "pic.png"
    Image.fromarray(np.full((10, 20), 90, np.uint8), "L").save(p)
    seen = {}

    def fake(parent, caption, directory, flt):
        seen["filter"] = flt
        return str(p), flt
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake))
    d = BitmapDialog()
    d.open_button.click()
    assert "*.png" in seen["filter"] and "*.jpg" in seen["filter"] and "*.bmp" in seen["filter"]
    assert d.session.current.shape == (10, 20) and d.image_path == str(p)
    assert d.height_mm.value() == 30
    d.deleteLater()


def test_exec_stub_does_not_block(app, monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)
    d = BitmapDialog()
    assert d.exec() == 0
    assert d.gcode_path is None
    d.deleteLater()
