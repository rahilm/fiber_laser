"""The service against the fake Onshape server: auth, hosts, quota, cache, the whole run, the
order lines, the groups, and the DXFs read back by mlaser's own importer."""
import base64
import math
from urllib.parse import urlsplit

import pytest

import fake_onshape as F
from mlaser_onshape import pipeline as PL
from mlaser_onshape.bom import MachineConfig
from mlaser_onshape.client import AuthError, OnshapeClient, QuotaError, check_server
from mlaser_onshape.service import OnshapeService


@pytest.fixture
def svc(ctx, fake):
    s = OnshapeService(ctx)
    s.set("server", fake.url)
    s.save_keys(*fake.world.auth)
    s.machine = MachineConfig(values=["Laser", "Fiber laser"], slots=["Process", "Process 2"], robots=2, spares=1)
    return s


def load(s):
    return s.load_bom(F.DID, "w", F.WID, F.AID, "Robot", "Robot 2026")


def by_name(plans):
    return {p.part.name: p for p in plans}


def test_server_check_refuses_other_hosts():
    assert check_server("https://cad.onshape.com/") == "https://cad.onshape.com"
    assert check_server("https://myschool.onshape.com") == "https://myschool.onshape.com"
    assert check_server("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    for bad in ("http://cad.onshape.com", "https://example.com", "https://onshape.com.evil.org", "ftp://x", ""):
        with pytest.raises(ValueError):
            check_server(bad)


def test_basic_auth_header_and_connection_test(svc, fake):
    assert svc.test_connection() == "Test Team"
    auth = fake.world.requests[-1][3]["Authorization"]
    assert auth == "Basic " + base64.b64encode(b"on_testaccesskey:secret-key-value").decode()
    svc.save_keys("on_testaccesskey", "wrong")
    with pytest.raises(AuthError):
        svc.test_connection()


def test_keys_live_in_secrets_not_settings(svc, ctx):
    assert ctx.secrets.get("secret_key") == "secret-key-value"
    assert "secret-key-value" not in repr(ctx.config.values)
    svc.forget_keys()
    assert svc.keys() is None


def test_browse_documents_folders_assemblies(svc, fake):
    page = svc.documents("", 5)
    kinds = [i["jsonType"] for i in page["items"]]
    assert kinds == ["folder", "document-summary"]
    inner = svc.documents(parent="f0lder")
    assert inner["items"][0]["name"].endswith("(in folder)")
    q = [r for r in fake.world.requests if r[1].endswith("/documents")]
    assert q[0][2]["filter"] == ["5"] and q[0][2]["limit"] == ["20"] and q[1][2]["parentId"] == ["f0lder"]
    ch = svc.workspace_choices(F.DID, F.WID)
    assert ch[0] == ("Workspace: Main", "w", F.WID) and ch[-1][1] == "v"
    assert [a["name"] for a in svc.assemblies(F.DID, "w", F.WID)] == ["Robot", "Intake"]
    bom_q = None
    load(svc)
    for r in fake.world.requests:
        if r[1].endswith("/bom"):
            bom_q = r[2]
    assert bom_q["indented"] == ["false"] and bom_q["onlyVisibleColumns"] == ["false"]


def test_full_run_statuses_dxfs_and_orders(svc, fake, ctx):
    res = load(svc)
    plans = by_name(svc.prepare(res.parts))
    st = {k: p.status for k, p in plans.items()}
    assert st["Pocket plate"] == PL.NOT_MACHINE                     # CNC mill
    assert st["Gusset"] == PL.NOT_MACHINE                           # Lathe first
    assert st["Shaft"] == PL.NO_FLAT
    # materials: "Aluminum - 6061" has no database name yet, steel names do
    assert st["Side plate"] == PL.NO_MATERIAL
    assert st["Bracket"] == PL.NO_MATERIAL and plans["Bracket"].sheet_material == ""
    side = plans["Side plate"]
    assert side.to_cut == 2 * 2 + 1 and side.t_plane == pytest.approx(3.0) and side.t_volume == pytest.approx(3.0)
    assert side.dxf.endswith("2026-01-001.dxf") and "Top (+Z) 100 x 60 mm" == side.face_label
    slot = plans["Slot plate"]                                      # 'Aluminum 5052 1/8"': 3.175 in the name
    assert slot.t_label == pytest.approx(3.175) and any("material name says" in n for n in slot.notes)
    # configured parts: two DXFs, the geometry of each configuration
    s100, s150 = plans["Spacer 100"], plans["Spacer 150"]
    assert s100.dxf != s150.dxf and (s100.width, s150.width) == pytest.approx((100.0, 150.0))
    body_q = [r[2] for r in fake.world.requests if r[1].endswith("/bodydetails") and "/JCD/" in r[1]]
    assert sorted(q["configuration"][0] for q in body_q) == ["Length=100+mm", "Length=150+mm"]
    # the linked part is read through its version with the assembly document as link
    linked = [r for r in fake.world.requests if F.LINKED_VID in r[1]]
    assert linked and linked[0][2]["linkDocumentId"] == [F.DID]
    # the cam plate needed one tessellation call
    assert len(fake.paths("/tessellatededges")) == 1
    assert plans["Cam plate"].status in (PL.READY, PL.NO_MATERIAL) and plans["Cam plate"].dxf

    # map the materials, then create the orders
    svc.map_material("Aluminum - 6061", "Aluminium AlMg3")
    svc.map_material('Aluminum 5052 1/8"', "Aluminium AlMg3")
    svc.map_material("Aluminum 2mm", "Aluminium AlMg3")
    for lab in ("Steel 4mm", "Mild steel", "Steel 2mm"):
        svc.map_material(lab, "Mild steel S235")
    ready = [p for p in svc.plans.values() if p.status == PL.READY]
    assert {p.part.name for p in ready} == {"Side plate", "Slot plate", "Bracket", "Quarter", "Cam plate",
                                            "Spacer 100", "Spacer 150", "Linked plate"}
    added, replaced = svc.create_orders(list(svc.plans.values()))
    assert (added, replaced) == (8, 0)
    book = ctx.services.orders
    ln = next(x for x in book.lines if x.file == side.dxf)
    assert (ln.quantity, ln.material, ln.thickness_mm) == (5, "Aluminium AlMg3", 3.0)
    assert ln.customer == "Robot 2026" and ln.order == "Robot" and "P/N 2026-01-001" in ln.notes
    assert ln.source == "onshape:" + side.part.key
    # a second run replaces, it does not duplicate
    assert svc.create_orders(list(svc.plans.values())) == (8, 8) and len(book.lines) == 8

    groups = {g.title: g for g in svc.groups()}
    assert set(groups) == {"Aluminium AlMg3 3 mm", "Aluminium AlMg3 2 mm",
                           "Mild steel S235 4 mm", "Mild steel S235 3 mm", "Mild steel S235 2 mm"}
    g3 = groups["Aluminium AlMg3 3 mm"]
    # side plate 2x2+1, slot plate (measured 3 mm, named 1/8") 1x2+1, spacers 3x2+1 and 1x2+1
    assert g3.parts == 4 and g3.quantity == 5 + 3 + 7 + 3 and g3.sheets == 1
    # the book groups by the same key, so Load into Nest finds the lines
    assert g3.key in dict(book.groups())
    svc.load_into_nest(g3.key)                           # no window: the command
    assert ctx.commands.ran == [("orders.to_nest", (g3.key,))]
    got = []

    class Orders:
        def to_nest(self, key=None, replace=None):
            got.append(key)
            return ["parts"]

    class Win:
        def extension(self, name):
            return Orders() if name == "orders" else None
    ctx.window = Win()
    assert svc.load_into_nest(g3.key) == ["parts"] and got == [g3.key]


def test_dxfs_import_as_closed_parts_in_mlaser(svc):
    from mlaser.services.orders import nest_parts_for
    res = load(svc)
    svc.map_material("Aluminum - 6061", "Aluminium AlMg3")
    plans = [p for p in svc.prepare(res.parts) if p.dxf]
    lines = PL.order_lines(plans)
    assert lines
    parts, problems = nest_parts_for(lines)
    assert not problems, problems
    side = next(p for p in parts if p.name.startswith("2026-01-001"))
    assert side.quantity == 5
    from mlaser.services.orders import load_drawing
    dxf = next(p.dxf for p in plans if p.part.name == "Side plate")
    x0, y0, x1, y1 = load_drawing(dxf).bbox()
    # millimetres (the importer places a drawing at its own import position, so only the size counts)
    assert (x1 - x0, y1 - y0) == pytest.approx((100, 60), abs=1e-6)


def test_cache_by_microversion(svc, fake):
    res = load(svc)
    svc.prepare(res.parts)
    n_body = len(fake.paths("/bodydetails"))
    calls = svc.calls
    res = load(svc)                                     # same microversion: BOM and parts from the cache
    svc.prepare(res.parts)
    assert len(fake.paths("/bodydetails")) == n_body
    assert svc.calls == calls + 1                       # only the currentmicroversion check
    fake.world.microversion = "mv0002"                  # the document changed
    res = load(svc)
    svc.prepare(res.parts)
    assert len(fake.paths("/bodydetails")) > n_body
    # the linked part is read through an immutable version: still cached
    assert len([p for p in fake.paths("/bodydetails") if F.LINKED_VID in p]) == 1


def test_429_is_retried_and_a_persistent_429_is_a_quota_error(svc, fake, ctx):
    fake.world.fail_429, fake.world.retry_after = 2, "0"
    assert svc.test_connection() == "Test Team"
    assert ctx.http.sleeps == [0.0, 0.0]
    fake.world.fail_429 = 10
    with pytest.raises(QuotaError):
        svc.test_connection()
    fake.world.fail_429 = 0


def test_only_the_configured_onshape_host_is_contacted(svc, fake, ctx):
    res = load(svc)
    svc.prepare(res.parts)
    svc.client().shaded_view(res.parts[0].source)
    hosts = {urlsplit(u).netloc for u in ctx.http.log}
    assert hosts == {urlsplit(fake.url).netloc}
    assert all(urlsplit(u).path.startswith("/api/v17/") for u in ctx.http.log)
    # a server outside the manifest permission is refused by ctx.http itself
    c = OnshapeClient(ctx.http, "https://cad.onshape.com", ("a", "b"))
    with pytest.raises(PermissionError):
        c.session()


def test_concurrency_is_at_most_two(svc, fake):
    import threading
    import time
    active, peak = [0], [0]
    lock = threading.Lock()
    real = svc.client().http.get

    def slow(url, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        try:
            return real(url, **kw)
        finally:
            with lock:
                active[0] -= 1
    svc.client().http.get = slow
    try:
        res = load(svc)
        svc.prepare(res.parts)
    finally:
        svc.client().http.get = real
    assert peak[0] == 2


def test_cancel_and_made_counts(svc, fake):
    import threading
    res = load(svc)
    ev = threading.Event()
    ev.set()
    plans = svc.prepare(res.parts, cancel=ev)
    assert all(p.status in (PL.CANCELLED, PL.NOT_MACHINE, PL.NOTHING) for p in plans)
    side = next(p for p in res.parts if p.name == "Side plate")
    svc.set_made(side.key, 5)
    assert svc.classify([side])[0].status == PL.NOTHING
    svc.set_made(side.key, 3)
    assert svc.classify([side])[0].to_cut == 2


def test_face_override_is_used(svc, fake):
    res = load(svc)
    side = next(p for p in res.parts if p.name == "Side plate")
    plan = svc.prepare([side])[0]
    bottom = next(fid for fid, lab in plan.faces if lab.startswith("Bottom"))
    svc.set_face(side.key, bottom)
    assert svc.prepare([side])[0].face_label.startswith("Bottom (-Z)")
    svc.set_face(side.key, "")
    assert svc.prepare([side])[0].face_label.startswith("Top (+Z)")


def test_sheet_mode_without_process_property(svc, fake):
    svc.machine = MachineConfig(mode="sheet", max_sheet_mm=5.0)
    res = load(svc)
    st = {p.part.name: p.status for p in svc.prepare(res.parts)}
    assert st["Pocket plate"] == PL.NOT_SHEET                         # 6 mm > 5 mm
    assert st["Shaft"] == PL.NO_FLAT
    assert st["Side plate"] in (PL.READY, PL.NO_MATERIAL)


def test_area_and_sheet_estimate():
    from mlaser_onshape.bom import BomPart
    p = PL.PartPlan(BomPart("k", "a"), status=PL.READY, to_cut=10, thickness=3.0, sheet_material="S235",
                    dxf="x.dxf", width=500, height=400, net_area=500 * 400)
    big = PL.PartPlan(BomPart("k2", "big"), status=PL.READY, to_cut=1, thickness=3.0, sheet_material="S235",
                      dxf="y.dxf", width=3000, height=100, net_area=3e5)
    g = PL.group_summaries([p, big], 2500, 1250, 0.7)[0]
    assert g.quantity == 11 and g.too_big == ["big"]
    assert g.sheets == math.ceil((505 * 405 * 10 + 3005 * 105) / (2500 * 1250 * 0.7))
