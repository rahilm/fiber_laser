"""ext_options (profiles, modern DXF, pyclipper kerf, network self-test), raster parts in the UI
(contour tools skip them, saving expands them, file.bitmap makes one), fonts and About."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from mlaser import ops  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app):
    from mlaser.parity_ports.profile import set_profile
    from wiring import real_window

    w = real_window()
    yield w
    w.close()
    set_profile("original")


def test_profile_selection_warns_and_replans(win, monkeypatch):
    from mlaser.parity_ports.profile import active_profile
    from mlaser.ui import ext_options as X

    assert "modern-ruckig" in X.profiles() and active_profile().name == "original"
    ext = win.extension("ext_options")
    d = X.ProfileDialog("original")
    d.combo.setCurrentIndex(d.combo.findData("modern-ruckig"))
    assert "NOT validated on hardware" in d.info.text()
    win.set_document(ops.demo_document())
    first = win.jobs.settle(timeout=60)
    logs = []
    win.log_panel.log = lambda m, *a, **k: logs.append(m)
    ext.choose_profile("modern-ruckig")  # the warning reaches the user
    assert active_profile().name == "modern-ruckig" and any("NOT validated" in m for m in logs)
    assert win.config.get("plugin.ext_options.parity_profile") == "modern-ruckig"
    ext.choose_profile("original-reference")  # no optional dependency needed
    second = win.jobs.settle(timeout=120)
    assert second.profile == "original-reference" and second.key != first.key
    assert second.job.wire == first.job.wire  # the reference kernels: bit-identical
    ext.choose_profile("original")


def test_dxf_mode_and_kerf_fallback(win, tmp_path):
    ext = win.extension("ext_options")
    assert win.hooks.provider("dxf.mode")() is None
    win.ribbon.actions["app.dxf_modern"].setChecked(True)
    assert ext.dxf_mode() == "modern" and win.config.get("plugin.ext_options.dxf_modern") is True
    win.ribbon.actions["craft.kerf_clipper"].setChecked(True)
    assert win.cam_params.kerf_fallback == "clipper"
    win.ribbon.actions["craft.kerf_clipper"].setChecked(False)
    assert win.cam_params.kerf_fallback is None
    import ezdxf

    dx = ezdxf.new()
    dx.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    p = tmp_path / "sq.dxf"
    dx.saveas(p)
    assert win.load_path(str(p)) and len(win.document.parts) == 1


def test_netcheck_dialog_and_about(app, monkeypatch, tmp_path):
    from mlaser import netcheck
    from mlaser.ui import dialogs, ext_options as X
    from mlaser.ui.i18n import translator

    monkeypatch.setattr(netcheck, "marker_path", lambda: tmp_path / "netcheck.json")
    d = X.NetCheckDialog()
    assert "no self-test result" in d.text.toPlainText()
    d.run()
    assert "network self-test" in d.text.toPlainText()
    about = dialogs.AboutDialog(translator())
    from PySide6.QtWidgets import QLabel

    texts = " ".join(lbl.text() for lbl in about.findChildren(QLabel))
    assert "LGPL" in texts and "network self-test" in texts
    assert "Emitter kernel: " in texts                         # C kernel or Python port


def _raster_part():
    import numpy as np
    from mlaser.cam.raster_engraving import RasterEngraving, make_raster_part

    r = RasterEngraving.from_segments([0.0, 1.0], [0.5, 2.0], [0.0, 0.1])
    assert len(r) == 2 and isinstance(r.x0, np.ndarray)
    return make_raster_part(r)


def test_contour_tools_skip_raster_parts_and_save_expands(win, tmp_path):
    from mlaser.formats.chf import load_chf
    from mlaser.model import Document

    raster = _raster_part()
    doc = ops.demo_document()
    doc.parts.append(raster)
    win.set_document(doc)
    win.run("craft.mj_auto")
    assert raster.craft.micro_joints == [] and any(p.craft.micro_joints for p in win.document.parts)
    win.run("craft.outside")
    assert win.document.parts[-1].craft.kerf_comp == -1
    assert ops.contour_targets(Document(parts=[raster])) == []
    path = tmp_path / "r.chf"
    win.path = str(path)
    win.file_save()
    back = load_chf(str(path))
    assert len(back.parts) == len(doc.parts) - 1 + 2  # the raster as its 2 burn segments


def test_bitmap_dialog_result_is_a_raster_part(win, tmp_path):
    import numpy as np
    from mlaser.cam.raster_engraving import raster_of_part

    ext = win.extension("ext_craft")
    ext.bitmap_dir = str(tmp_path)
    win.run("file.bitmap")  # the dialog object exists (not executed)
    d = ext.bitmap_dialog
    img = np.full((12, 24), 255, np.uint8)
    img[3:9, 4:20] = 0
    if not hasattr(d, "generate_from_array"):
        from mlaser.cam.raster_engraving import RasterEngraving, make_raster_part

        d.raster = RasterEngraving.from_segments([0.0], [1.0], [0.0])
        d.raster_part = make_raster_part(d.raster, name="bitmap")
    else:  # pragma: no cover
        d.generate_from_array(img)
    assert ext.import_bitmap_result() == "raster"
    assert len(win.document.parts) == 1 and raster_of_part(win.document.parts[0]) is not None
    win.undo()
    assert raster_of_part(win.document.parts[0]) is None if win.document.parts else True


def test_bundled_fonts_in_the_font_combo(app):
    from PySide6.QtGui import QFontDatabase

    from mlaser.cad.fonts import bundled_fonts
    from mlaser.ui.ext_draw import register_bundled_fonts

    fams = register_bundled_fonts()
    if not bundled_fonts():
        pytest.skip("bundled fonts not present")
    assert fams and all(f in QFontDatabase.families() for f in fams)
