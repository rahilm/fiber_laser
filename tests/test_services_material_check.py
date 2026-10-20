"""Sheet material check (mlaser.services.material_check): the mismatch gate, its audited
override, and the "load this sheet" card contents."""
from __future__ import annotations

import logging
from dataclasses import asdict

from mlaser.model import Contour, Craft, Document, LayerParams, Line, Part
from mlaser.services import material_check as M
from mlaser.services.queue import JobQueue
from mlaser.services.technology import Technology, TechKey, apply_to_layer


def tech(material="Mild steel S235", thickness=3.0, gas="O2", nozzle="1.0 double", example=False):
    t = Technology(TechKey(material, thickness, gas, nozzle), origin="example" if example else "user")
    t.id, t.version = 7, 2
    return t


def doc_with(*techs, sheet=None):
    d = Document()
    for li, t in enumerate(techs, 1):
        d.parts.append(Part([Contour([Line((0, 0), (10, 0)), Line((10, 0), (0, 0))], True)], Craft(layer=li)))
        lp = LayerParams(index=li)
        if t is not None:
            apply_to_layer(lp, t)
        d.layers[li] = lp
    if sheet is not None:
        M.set_sheet(d, sheet)
    return d


S235_3 = M.SheetSpec("Mild steel S235", 3.0, "queue", "e1", 1)


def test_matching_sheet_has_no_issue():
    assert M.check(doc_with(tech(), sheet=S235_3)) == []
    # case and spacing do not matter, thickness to 0.01 mm
    assert M.check(doc_with(tech("mild  steel s235", 3.004), sheet=S235_3)) == []


def test_mismatch_is_a_blocking_error():
    for t in (tech("Aluminium", 1.0), tech("Mild steel S235", 2.0)):
        (i,) = M.check(doc_with(t, sheet=S235_3))
        assert i.code == M.MISMATCH and i.blocking and "the sheet is Mild steel S235 3 mm" in i.message


def test_layer_without_technology_and_example_values_warn():
    d = doc_with(tech(example=True), None, sheet=S235_3)
    codes = sorted((i.code, i.severity) for i in M.check(d))
    assert codes == [("material_unverified", "warning"), ("technology_example", "warning")]


def test_undeclared_sheet_material_warns_and_no_sheet_is_not_checked():
    d = doc_with(tech("Aluminium", 1.0), sheet=M.SheetSpec("", 0.0, "queue"))
    assert [i.code for i in M.check(d)] == ["material_unverified"]
    assert M.check(doc_with(tech("Aluminium", 1.0))) == []          # a plain design


def test_gate_refuses_overrides_and_audits(caplog):
    d = doc_with(tech("Aluminium", 1.0), sheet=S235_3)
    now = [1000.0]
    gate = M.MaterialGate(lambda: d, clock=lambda: now[0], ttl_s=60, user=lambda: "operator")
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        ok, msg = gate.start_guard("process")
        assert not ok and "material check" in msg and "Aluminium 1 mm" in msg
        assert gate.start_guard("dry_run") == (True, "")            # laser off: not checked
        assert gate.start_guard("frame") == (True, "")
        assert gate.override("operator checked the sheet")
        assert gate.start_guard("process") == (True, "")
    events = [r.audit["event"] for r in caplog.records if hasattr(r, "audit")]
    assert events == ["material_check", "material_override", "material_check"]
    ov = next(r.audit for r in caplog.records if getattr(r, "audit", {}).get("event") == "material_override")
    assert ov["reason"] == "operator checked the sheet" and ov["user"] == "operator" and ov["entry"] == "e1"
    # the override expires, and it is for this sheet only
    now[0] += 61
    assert not gate.start_guard("process")[0]
    gate.override()
    M.set_sheet(d, M.SheetSpec("Mild steel S235", 3.0, "queue", "e1", 2))      # the next sheet
    assert not gate.start_guard("process")[0]


def test_override_without_mismatch_does_nothing(caplog):
    d = doc_with(tech(), sheet=S235_3)
    gate = M.MaterialGate(lambda: d)
    with caplog.at_level(logging.INFO, logger="mlaser.audit"):
        assert not gate.override()
        assert gate.start_guard("process") == (True, "")
    assert not [r for r in caplog.records if hasattr(r, "audit")]


def test_load_card_contents(tmp_path):
    q = JobQueue(None)
    d = doc_with(tech())
    layers = {str(k): asdict(v) for k, v in d.layers.items()}
    e = q.add(str(tmp_path / "s.chf"), 3, name="nest #1", material="Mild steel S235", thickness_mm=3.0,
              sheet="R-0007", remnant_id="R-0007",
              extra={"sheet_bounds": [0, 0, 664, 500], "sheet_rings": [[[0, 0], [664, 0], [664, 500], [0, 500]]],
                     "parts": 24, "utilisation": 0.174, "layers": layers,
                     "orders": [{"customer": "ACME", "order": "4711", "part": "gear", "qty": 6},
                                {"customer": "", "order": "", "part": "tab", "qty": 2}]})
    e.done = 1
    card = M.load_card(e, d)
    assert card["material"] == "Mild steel S235" and card["thickness"] == "3 mm"
    assert card["size"] == "664 × 500 mm" and card["remnant_id"] == "R-0007" and card["sheet"] == "2/3"
    assert card["parts"] == 24 and card["nozzle"] == "1.0 double"
    assert card["technology"] == ["Mild steel S235 3 mm · O2"]
    assert card["orders"] == ["ACME · 4711: 6 × gear", "2 × tab"]
    assert card["mismatch"] == [] and card["rings"]
    wrong = M.load_card(e, doc_with(tech("Aluminium", 1.0)))
    assert wrong["mismatch"] and "Aluminium" in wrong["mismatch"][0]
    bare = M.load_card(q.add(str(tmp_path / "x.chf"), 1))
    assert bare["material"] == "(material not set)" and bare["size"] == "" and bare["sheet"] == "1/1"
