"""The workspace with the stub context (offscreen Qt), and the packaging contract."""
import tomllib
from pathlib import Path

import pytest

from mlaser_onshape import pipeline as PL

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_matches_the_distribution():
    man = tomllib.loads((ROOT / "mlaser_onshape" / "mlaser-plugin.toml").read_text(encoding="utf-8"))
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    p = man["plugin"]
    assert p["id"] == "onshape" and p["entry"] == "mlaser_onshape:plugin"
    assert py["project"]["entry-points"]["mlaser.plugins"] == {"onshape": "mlaser_onshape:plugin"}
    assert p["version"] == py["project"]["version"]
    import mlaser_onshape
    assert mlaser_onshape.__version__ == p["version"] and mlaser_onshape.plugin.PLUGIN_API == 2
    perms = man["permissions"]
    assert perms["network"] == ["cad.onshape.com", "*.onshape.com"]
    assert perms["machine"] is False and perms["queue"] is False and perms["secrets"] is True
    assert "mlaser-plugin.toml" in py["tool"]["setuptools"]["package-data"]["mlaser_onshape"]


def test_register_needs_api_2(tmp_path):
    from mlaser_onshape import OnshapePlugin
    from stubctx import StubCtx
    ctx = StubCtx(tmp_path)
    ctx.http = None
    with pytest.raises(RuntimeError, match="plugin API 2"):
        OnshapePlugin().register(ctx)


@pytest.fixture
def ws(ctx, fake):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from mlaser_onshape import OnshapePlugin
    plugin = OnshapePlugin().register(ctx)
    w = plugin.workspace
    w.server.setText(fake.url)
    w.access.setText(fake.world.auth[0])
    w.secret.setText(fake.world.auth[1])
    w.save_connection()
    yield w
    w.timer.stop()
    w.deleteLater()
    app.processEvents()


def test_workspace_flow(ws, ctx, fake):
    assert ctx.workspaces[0][0] == "onshape" and ctx.workspaces[0][2] == "Onshape"
    assert {"onshape.refresh", "onshape.prepare"} <= set(ctx.registered)
    assert ctx.secrets.get("access_key") == fake.world.auth[0] and ws.access.text() == ""
    ws.test_connection()
    assert "Test Team" in ws.conn.text()
    ws.reload_documents()
    assert ws.docs.count() == 2
    ws._doc_activated(ws.docs.item(0))                      # the folder
    assert ws.b_up.isEnabled() and ws.docs.count() == 1
    ws.go_up()
    ws._doc_activated(ws.docs.item(1))                      # the document
    assert ws.wvm_box.count() == 2 and ws.asms.count() == 2
    ws._asm_activated(ws.asms.item(0))
    assert ws.table.rowCount() == 11 and "Columns" in ws.bom_info.text()
    # machine type: Laser and Fiber laser, Process and Process 2, 2 robots
    ws.values.setText("Laser, Fiber laser")
    ws.robots.setValue(2)
    ws.apply_machine()
    ws.select_for_machine()
    picked = ws.checked_keys()
    names = {ws.service.plans[k].part.name for k in picked}
    assert "Pocket plate" not in names and "Side plate" in names and "Gusset" not in names
    ws.prepare_selected()
    assert "order line(s) added" in ws.status.text()
    assert ws.mat_table.rowCount() >= 4
    # map the aluminium through the table's combo box
    for r in range(ws.mat_table.rowCount()):
        lab = ws.mat_table.item(r, 0).text()
        box = ws.mat_table.cellWidget(r, 2)
        box.setCurrentText("Aluminium AlMg3" if "alu" in lab.lower() else "Mild steel S235")
    assert all(p.status != PL.NO_MATERIAL for p in ws.service.plans.values())
    ws.create_orders()
    assert len(ctx.services.orders.lines) == 8
    cards = [ws.cards.itemAt(i).widget() for i in range(ws.cards.count() - 1)]
    assert len(cards) == len(ws.service.groups()) >= 3
    from PySide6.QtWidgets import QPushButton
    cards[0].findChild(QPushButton).click()
    assert ctx.commands.ran and ctx.commands.ran[-1][0] == "orders.to_nest"
    assert int(ws.calls.text().split(":")[1]) > 0
    # made counts edit the quantity
    row = next(r for r in range(ws.table.rowCount()) if ws.table.item(r, 0).text() == "Side plate")
    ws.table.item(row, 8).setText("5")
    side = next(p for p in ws.service.plans.values() if p.part.name == "Side plate")
    assert side.status == PL.NOTHING
    # the face picker saves the face of the current part
    row = next(r for r in range(ws.table.rowCount()) if ws.table.item(r, 0).text() == "Bracket")
    ws.table.setCurrentCell(row, 0)
    plan = ws.selected_plan()
    assert ws.choose_face(plan.faces[1][0]) == plan.faces[1][0]
    assert ws.service.get("face_overrides")[plan.part.key] == plan.faces[1][0]
    # only the fake server was contacted
    from urllib.parse import urlsplit
    assert {urlsplit(u).netloc for u in ctx.http.log} == {urlsplit(fake.url).netloc}


def test_workspace_reports_errors(ws, fake):
    ws.service.save_keys("x", "y")
    ws.test_connection()
    assert "refused" in ws.status.text()
