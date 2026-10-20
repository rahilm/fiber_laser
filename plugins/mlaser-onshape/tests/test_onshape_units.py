"""The Onshape workspace in imperial (mlaser.services.units): thicknesses, sheet sizes, areas and
the pipeline's notes show in inches, a change relabels the open workspace, and an edit made in
inches is stored in mm (the plugin's settings, the pipeline and the DXFs stay mm)."""
import re

import pytest

import fake_onshape as F
from mlaser_onshape.bom import MachineConfig
from mlaser_onshape.service import OnshapeService

pytest.importorskip("PySide6")

# metric unit text that must not show in imperial (as tests/units_sweep.py of the core)
METRIC = re.compile(r"(?<![\w/])(?:mm|m|cm)(?:²|³)?(?![\w²³/])")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def units():
    from mlaser.services import units as U
    U.reset()
    yield U
    U.reset()


@pytest.fixture
def ws(app, ctx, fake, units):
    from mlaser_onshape.ui import OnshapeWorkspace
    s = OnshapeService(ctx)
    s.set("server", fake.url)
    s.save_keys(*fake.world.auth)
    s.machine = MachineConfig(values=["Laser", "Fiber laser"], slots=["Process", "Process 2"], robots=2, spares=1)
    s.map_material("Aluminum - 6061", "Aluminium AlMg3")
    w = OnshapeWorkspace(s)
    w.resize(1280, 800)
    bom = s.load_bom(F.DID, "w", F.WID, F.AID, "Robot", "Robot 2026")
    w._reclassify()
    s.prepare(bom.parts)
    w.refresh_table()
    yield w
    w.timer.stop()
    w.deleteLater()


def _texts(w):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QAbstractSpinBox, QLabel, QTableWidget
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)     # the replaced group cards
    out = [lab.text() for lab in w.findChildren(QLabel)]
    out += [sb.text() for sb in w.findChildren(QAbstractSpinBox)]
    for t in w.findChildren(QTableWidget):
        for c in range(t.columnCount()):
            h = t.horizontalHeaderItem(c)
            out.append(h.text() if h else "")
            for r in range(t.rowCount()):
                it = t.item(r, c)
                out.append(it.text() if it else "")
    return [x for x in out if x]


def test_imperial_shows_no_metric_text_and_follows_live(ws, units):
    assert any("mm" in t for t in _texts(ws))                       # metric to begin with
    units.set_system("imperial")
    bad = [t for t in _texts(ws) if METRIC.search(t)]
    assert not bad, bad
    heads = [ws.table.horizontalHeaderItem(c).text() for c in range(ws.table.columnCount())]
    assert "t faces (in)" in heads
    cards = " ".join(t for t in _texts(ws) if "sheet(s) of" in t or "area" in t)
    assert " in" in cards and "ft²" in cards
    units.set_system("metric")
    assert "t faces (mm)" in [ws.table.horizontalHeaderItem(c).text() for c in range(ws.table.columnCount())]


def test_edit_in_inches_stores_mm(ws, units):
    units.set_system("imperial")
    w0 = ws.service.get("sheet_w")
    assert ws.sheet_w.value() == w0                                  # mm, untouched by the switch
    ws.sheet_w.setValue(ws.sheet_w.units().parse("100 in", "length"))
    assert ws.service.get("sheet_w") == pytest.approx(2540.0)
    ws.tol.setValue(units.current().parse("0.004", "small_length"))
    ws.save_connection()
    assert ws.service.get("thickness_tol") == pytest.approx(0.1016)


def test_mm_text_converts_the_pipelines_notes(units):
    from mlaser_onshape.ui import mm_text
    assert mm_text("Top (+Z) 120 x 80 mm") == "Top (+Z) 120 x 80 mm"   # metric: unchanged
    units.set_system("imperial")
    assert mm_text("Top (+Z) 127 x 254 mm (1290 mm²)") == "Top (+Z) 5.000 x 10.000 in (2.00 in²)"
    assert mm_text("the material name says 3.2 mm, the part measures 3.18 mm") == \
        "the material name says 0.126 in, the part measures 0.1252 in"
    assert mm_text("Aluminium AlMg3 3 mm") == "Aluminium AlMg3 0.118 in"
    assert mm_text("10 mm/s") == "10 mm/s"                            # not a length: left alone
