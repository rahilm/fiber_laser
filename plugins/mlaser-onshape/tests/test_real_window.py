"""Against the real application window (built-in plugins, offscreen): the plugin's order lines go
into the real Orders book and Load into Nest fills the real Nest workspace with that group.
The core's plugin API 2 is stood in for by the stub context (http, secrets, data dir)."""
import sys
from pathlib import Path

import pytest

import fake_onshape as F
from mlaser_onshape.bom import MachineConfig
from mlaser_onshape.service import OnshapeService

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    if not (REPO / "tests" / "wiring.py").exists():
        pytest.skip("needs the mlaser source checkout (tests/wiring.py)")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "mlaser-data"))
    sys.path.insert(0, str(REPO / "tests"))
    try:
        from wiring import real_window
        w = real_window()
    finally:
        sys.path.remove(str(REPO / "tests"))
    yield w
    w.close()
    w.deleteLater()
    app.processEvents()


def test_orders_and_load_into_nest_in_the_real_window(window, tmp_path, fake):
    from stubctx import StubCtx
    orders = window.extension("orders")
    orders = orders[0] if isinstance(orders, tuple) else orders
    ctx = StubCtx(tmp_path)
    ctx.window = window
    ctx.services.orders = orders.book
    n0 = len(orders.book.lines)
    svc = OnshapeService(ctx)
    svc.set("server", fake.url)
    svc.save_keys(*fake.world.auth)
    svc.machine = MachineConfig(values=["Laser"], slots=["Process"], robots=2, spares=1)
    svc.map_material("Aluminum - 6061", "Aluminium AlMg3")
    bom = svc.load_bom(F.DID, "w", F.WID, F.AID, "Robot", "Robot 2026")
    plans = svc.prepare(bom.parts)
    added, _ = svc.create_orders(plans)
    assert added == 3 and len(orders.book.lines) == n0 + 3       # side plate and the two spacers
    g = next(g for g in svc.groups(plans) if g.title == "Aluminium AlMg3 3 mm")
    parts = svc.load_into_nest(g.key)
    assert sum(p.quantity for p in parts) == 5 + 7 + 3
    nest = window.extension("nest")
    nest = nest[0] if isinstance(nest, tuple) else nest
    assert len(nest.job.parts) == 3
    assert nest.material.text() == "Aluminium AlMg3" and nest.thickness.value() == pytest.approx(3.0)
