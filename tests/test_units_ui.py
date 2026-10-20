"""mlaser.ui.units_qt: QuantitySpinBox / QuantityLabel / helpers, and the stored values never
changing when the system is switched (docs/user/units.md)."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from mlaser.services import units as U
from mlaser.services.config import ConfigService
from mlaser.ui import units_qt as UQ
from mlaser.ui.units_qt import QuantityLabel, QuantitySpinBox


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def cfg(tmp_path):
    c = ConfigService(tmp_path / "c.json", load=False)
    U.bind(c)
    yield c
    U.reset()


def _type(sb, text):
    sb.lineEdit().selectAll()
    sb.lineEdit().clear()
    QTest.keyClicks(sb.lineEdit(), text)
    sb.lineEdit().editingFinished.emit()
    sb.editingFinished.emit()


def test_constructor_signature_is_stable(app):
    sb = QuantitySpinBox("length")
    sb2 = QuantitySpinBox("speed", None)
    assert sb.kind() == "length" and sb2.kind() == "speed"
    with pytest.raises(ValueError):
        QuantitySpinBox("lenght")


def test_value_is_internal_and_relabels_live(app, cfg):
    sb = QuantitySpinBox("length", minimum=0, maximum=5000)
    sb.setValue(3.175)
    got = []
    sb.valueChanged.connect(got.append)
    assert sb.text() == "3.17 mm" or sb.text() == "3.18 mm"
    cfg.set("units.system", "imperial")
    assert sb.text() == "0.125 in" and sb.suffix() == " in"
    assert sb.value() == 3.175 and got == []            # relabel only: no value change
    assert sb.decimals() == 3
    cfg.set("units.system", "metric")
    assert sb.suffix() == " mm" and sb.value() == 3.175


def test_typed_units_in_any_system(app, cfg):
    cfg.set("units.system", "imperial")
    sb = QuantitySpinBox("length", minimum=-1e6, maximum=1e6)
    for text, mm in (('1/8"', 3.175), ("3.2 mm", 3.2), ("0.125 in", 3.175), ("2'6\"", 762.0), ("2", 50.8)):
        _type(sb, text)
        assert sb.value() == pytest.approx(mm, abs=1e-9), text
    _type(sb, "3.2 mm")
    assert sb.value() == pytest.approx(3.2, abs=1e-12)  # exact, not the 0.126 in display rounding
    _type(sb, "nonsense")
    assert sb.value() == pytest.approx(3.2)             # rejected: value kept, text restored
    assert sb.text() == "0.126 in"


def test_untouched_value_never_drifts(app, cfg):
    """Open a dialog in imperial and save without edits: the stored mm value is unchanged."""
    cfg.set("units.system", "imperial")
    for kind, v in (("length", 3.1), ("small_length", 0.15), ("speed", 123.456), ("accel", 7777.7),
                    ("pressure", 12.34)):
        sb = QuantitySpinBox(kind, minimum=0, maximum=1e6)
        sb.setValue(v)
        sb.setFocus()
        sb.lineEdit().editingFinished.emit()             # focus out without typing
        sb.editingFinished.emit()
        assert sb.value() == v, kind
        _type(sb, sb.text())                             # retyping the shown text is no edit either
        assert sb.value() == v, kind


def test_range_steps_and_clamp(app, cfg):
    cfg.set("units.system", "imperial")
    sb = QuantitySpinBox("length", minimum=0.0, maximum=100.0)
    sb.setValue(500.0)
    assert sb.value() == 100.0
    sb.setValue(25.4)
    sb.stepBy(1)                                         # 1.000 in + 0.05 in
    assert sb.value() == pytest.approx(1.05 * 25.4)
    k = QuantitySpinBox("small_length", minimum=0, maximum=10)
    k.setValue(0.2)
    k.stepBy(1)
    assert k.value() == pytest.approx(0.2 + 0.0254, abs=1e-3)
    assert k.decimals() == 4
    cfg.set("units.system", "metric")
    m = QuantitySpinBox("length", minimum=0, maximum=100, decimals=1, step=0.5)
    m.setValue(10.0)
    m.stepBy(2)
    assert m.value() == 11.0 and m.decimals() == 1
    cfg.set("units.system", "imperial")
    assert m.decimals() == 3 and m.singleStep() == 0.05   # metric overrides only apply in mm


def test_edit_in_imperial_stores_mm(app, cfg):
    """The system flipped to imperial, a value edited through a QuantitySpinBox: the stored value is mm."""
    cfg.set("units.system", "imperial")
    sb = QuantitySpinBox("length", minimum=0, maximum=1000)
    sb.setValue(cfg.get("bed.slat_pitch"))
    assert sb.text() == "3.937 in"
    sb.valueChanged.connect(lambda v: cfg.set("bed.slat_pitch", v))
    _type(sb, "4")
    assert cfg.get("bed.slat_pitch") == pytest.approx(101.6)
    _type(sb, '1/2"')
    assert cfg.get("bed.slat_pitch") == pytest.approx(12.7)


def test_quantity_label_and_cells(app, cfg):
    lab = QuantityLabel("speed", value=25.4 / 60 * 100, template="Speed {}")
    assert lab.text() == "Speed 25.4 mm/s".replace("25.4", f"{25.4 / 60 * 100:.1f}")
    cfg.set("units.system", "imperial")
    assert lab.text() == "Speed 100.0 in/min"
    it = UQ.cell(3048.0, "length", decimals=1)
    assert it.text() == "120.0" and UQ.cell_value(it) == 3048.0
    assert UQ.header("Width", "length") == "Width (in)"
    assert UQ.status_xy(25.4, 50.8) == "X 1.000  Y 2.000 in"
    assert UQ.cell_value(QTableWidgetItem("1")) is None


def test_on_units_changed_dies_with_widget(app, cfg):
    lab = QuantityLabel("length")
    seen = []
    UQ.on_units_changed(lab, lambda u: seen.append(u.system))
    assert seen == ["metric"]
    cfg.set("units.system", "imperial")
    assert seen == ["metric", "imperial"]
    lab.deleteLater()
    QApplication.processEvents()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    cfg.set("units.system", "metric")                    # no call into a deleted widget
    assert seen == ["metric", "imperial"]


def test_special_value_and_keys(app, cfg):
    sb = QuantitySpinBox("length", minimum=0, maximum=10)
    sb.setSpecialValueText("auto")
    sb.setValue(0)
    assert sb.text() == "auto"
    sb.setValue(1)
    sb.setFocus()
    QTest.keyClick(sb, Qt.Key_Up)
    assert sb.value() == 2.0
