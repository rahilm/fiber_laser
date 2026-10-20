"""Every migrated dialog is on the one dialog frame (mlaser.ui.style.Dialog, DESIGN.md §4):
a window title, a QDialogButtonBox (the platform's button order), SP3 margins, units only in the
fields' suffixes (never also in their labels), and no sideways scrolling at its own size."""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import (QAbstractScrollArea, QAbstractSpinBox, QApplication, QDialog,  # noqa: E402
                               QDialogButtonBox, QDoubleSpinBox, QLabel, QMessageBox, QSpinBox)

from mlaser.ui import style as S  # noqa: E402
from mlaser.ui.units_qt import QuantitySpinBox  # noqa: E402

UNIT_IN_LABEL = re.compile(r"\((mm|mm/s|mm/min|in|in/s|in/min|ipm|%|°|deg|bar|psi|kpa|mpa|s|ms|hz)\)", re.I)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(app, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("MLASER_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    mp.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "question"):
        mp.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    from wiring import dispose, real_window
    w = real_window()
    yield w
    dispose(w)
    mp.undo()


def _ext(w, name):
    e = w.extension(name)
    return e[0] if isinstance(e, tuple) else e


def _factories(w):
    from mlaser.ops.cam import FilletParams
    from mlaser.ui import dialogs as D
    from mlaser.ui import ext_card, ext_craft, ext_devices, ext_diag, ext_draw, ext_machine, ext_nest, ext_options
    from mlaser.ui.engrave_dialog import BitmapDialog
    from mlaser.ui.ext_automation import ApiSettingsDialog
    from mlaser.ui.ext_cam import FieldDialog
    from mlaser.ui.ext_notify import NotifyDialog
    tr = w.tr_
    diag = w.extension("ext_diag")

    def draw():
        d = ext_draw._Dialog(tr, "mf591", w)
        d.add("gp64", ext_draw._lspin(0.001, 1e6, 10.0))
        d.add("pd659", ext_draw.QCheckBox())
        d.finish()
        return d

    def field():
        return FieldDialog(tr, "mf403", FilletParams(), [("radius", "pd600", "len", (0.1, 10)),
                                                          ("min_angle", "pd601", "float", (0, 180)),
                                                          ("max_angle", "pd602", "float", (0, 180))], w)

    from mlaser.diag import ballarm
    return {
        "ValueDialog": lambda: D.ValueDialog(tr, "mf185", "pd339", 30.0, -360, 360, w, suffix=" °"),
        "IPDialog": lambda: D.IPDialog(tr, None, w),
        "PasswordDialog": lambda: D.PasswordDialog(tr, w),
        "AlarmDialog": lambda: D.AlarmDialog(tr, [("mf123", "12:00")], w),
        "AboutDialog": lambda: D.AboutDialog(tr, w),
        "FieldDialog": field,
        "DockDialog": lambda: ext_craft.DockDialog(tr, 0, w),
        "DrawDialog": draw,
        "BitmapDialog": lambda: BitmapDialog(tr, w),
        "SheetDialog": lambda: ext_nest.SheetDialog(w, None, w),
        "FollowerDialog": lambda: ext_devices.FollowerDialog(w),
        "LaserDialog": lambda: ext_devices.LaserDialog(w),
        "PendantDialog": lambda: ext_devices.PendantDialog(w),
        "ProfileDialog": lambda: ext_options.ProfileDialog("original", w),
        "NetCheckDialog": lambda: ext_options.NetCheckDialog(w),
        "PluginsDialog": lambda: ext_options.PluginsDialog(w, [], {}),
        "AlarmListDialog": lambda: ext_card.AlarmListDialog(w, []),
        "UpgradeConfirmDialog": lambda: ext_machine.UpgradeConfirmDialog(
            w, "motion card", SimpleNamespace(path=Path("card.bin"), data=b"\0" * 64)),
        "NotifyDialog": lambda: NotifyDialog(_ext(w, "ext_notify")),
        "ApiSettingsDialog": lambda: ApiSettingsDialog(_ext(w, "ext_automation")),
        "ParamDialog": lambda: ext_diag.ParamDialog(w, "Ball arm", ballarm.params(diag.settings)),
        "DeviceReportDialog": lambda: ext_diag.DeviceReportDialog(diag),
        "LogViewer": lambda: ext_diag.LogViewer(diag),
        "MeasureWindow": lambda: ext_diag.MeasureWindow(diag),
        "CustomIODialog": lambda: ext_diag.CustomIODialog(diag, True),
        "LightTestDialog": lambda: ext_diag.LightTestDialog(diag),
        "BurnInDialog": lambda: ext_diag.BurnInDialog(diag),
        "BatchDialog": lambda: ext_diag.BatchDialog(diag),
        "MaintainDialog": lambda: ext_diag.MaintainDialog(diag),
    }


NAMES = ["ValueDialog", "IPDialog", "PasswordDialog", "AlarmDialog", "AboutDialog", "FieldDialog", "DockDialog",
         "DrawDialog", "BitmapDialog", "SheetDialog", "FollowerDialog", "LaserDialog", "PendantDialog",
         "ProfileDialog", "NetCheckDialog", "PluginsDialog", "AlarmListDialog", "UpgradeConfirmDialog",
         "NotifyDialog", "ApiSettingsDialog", "ParamDialog", "DeviceReportDialog", "LogViewer", "MeasureWindow",
         "CustomIODialog", "LightTestDialog", "BurnInDialog", "BatchDialog", "MaintainDialog"]


def _units_of(field) -> list[str]:
    if isinstance(field, QuantitySpinBox):
        return [field.suffix().strip()] if hasattr(field, "suffix") and field.suffix().strip() else []
    if isinstance(field, (QSpinBox, QDoubleSpinBox)) and field.suffix().strip():
        return [field.suffix().strip()]
    return []


@pytest.mark.parametrize("name", NAMES)
def test_dialog_is_on_the_frame(win, app, name):
    d = _factories(win)[name]()
    try:
        assert isinstance(d, S.Dialog), type(d)
        assert d.windowTitle().strip()
        box = d.findChild(QDialogButtonBox, "dialog_buttons")
        assert box is not None and box is d.buttons and box.buttons()
        m = d.layout().contentsMargins()
        assert (m.left(), m.top(), m.right(), m.bottom()) == (S.SP3,) * 4
        # a unit in the field, never in its label as well
        for lab in d.findChildren(QLabel):
            buddy = lab.buddy()
            if not isinstance(buddy, QAbstractSpinBox):
                continue
            text = lab.text()
            assert not UNIT_IN_LABEL.search(text), (name, text)
            for unit in _units_of(buddy):
                assert unit not in text.split(), (name, text, unit)
        # at its own size, nothing scrolls sideways
        d.resize(d.sizeHint())
        d.show()
        for _ in range(3):
            app.processEvents()
        assert d.minimumSizeHint().width() <= d.width()
        for area in d.findChildren(QAbstractScrollArea):
            if area.isVisibleTo(d):
                assert not area.horizontalScrollBar().isVisible(), (name, area.objectName() or type(area).__name__)
    finally:
        d.hide()
        d.deleteLater()
        app.processEvents()


def test_field_dialog_units_move_to_the_suffix(win):
    d = _factories(win)["FieldDialog"]()
    try:
        assert isinstance(d.editors["radius"], QuantitySpinBox)
        ang = d.editors["min_angle"]
        lab = d.form().labelForField(ang)
        assert lab is not None and "deg" not in lab.text() and "(" not in lab.text()
        d.editors["radius"].setValue(2.5)
        d.store()
        assert d.obj.radius == pytest.approx(2.5)               # still mm inside
    finally:
        d.deleteLater()


def test_shadowing_attributes_are_renamed(win):
    from mlaser.ui import ext_devices, ext_nest
    s = ext_nest.SheetDialog(win)
    f = ext_devices.FollowerDialog(win)
    try:
        assert callable(s.width) and callable(s.height) and callable(f.height)
        assert s.sheet().width == s.width_spin.value()
    finally:
        s.deleteLater()
        f.deleteLater()
