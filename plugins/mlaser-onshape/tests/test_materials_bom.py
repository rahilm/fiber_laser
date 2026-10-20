"""Material names, the technology mapping, BOM parsing, the machine filter and quantities."""
import pytest

from fake_onshape import World
from mlaser_onshape.bom import MachineConfig, machine_match, parse_bom, quantity_to_cut, is_sheet
from mlaser_onshape.materials import MaterialMap, family, label_thickness, non_metal, strip_thickness, suggest


@pytest.mark.parametrize("text, mm", [
    ("Aluminum 3mm", 3.0), ("Steel 6 mm", 6.0), ('6061 1/8"', 3.175), ("Alu 1/4in", 6.35),
    ("Plate 0.125 in", 3.175), ('Al .25"', 6.35), ("Polycarbonate 3/16 inch", 4.7625), ('Bar 1-1/2"', 38.1),
    ("Stahl 0,8 mm", 0.8), ("Al 2.5mm sheet", 2.5), ("Delrin 1/2″", 12.7),
    ("6061-T6", None), ("Aluminum - 6061", None), ("Steel", None), ("", None), ("10 mmx", None),
])
def test_label_thickness(text, mm):
    got = label_thickness(text)
    assert (got is None) if mm is None else got == pytest.approx(mm)


def test_strip_family_non_metal():
    assert strip_thickness('Aluminum 5052 1/8"') == "Aluminum 5052"
    assert family("Aluminum - 6061") == "aluminium" and family("Stainless steel 304") == "stainless"
    assert family("A36 plate") == "steel"
    assert non_metal("Polycarbonate 1/8\"") and non_metal("Plywood 6mm") and not non_metal("Aluminum 3mm")


def test_suggestions_and_map():
    names = ["Mild steel S235", "Stainless steel 1.4301", "Aluminium AlMg3"]
    assert suggest("Aluminum - 6061", names)[0] == "Aluminium AlMg3"
    assert suggest("Steel 4mm", names)[0] == "Mild steel S235"
    assert suggest("304 Stainless 2mm", names)[0] == "Stainless steel 1.4301"
    m = MaterialMap()
    assert m.resolve("Aluminum - 6061", names) == ("", "unmapped")
    m.set("  aluminum -  6061 ", "Aluminium AlMg3")
    assert m.resolve("Aluminum - 6061", names) == ("Aluminium AlMg3", "mapped")
    assert m.resolve("Mild steel S235 3mm", names) == ("Mild steel S235", "same name")
    assert MaterialMap.from_json(m.to_json()).get("ALUMINUM - 6061") == "Aluminium AlMg3"
    assert MaterialMap.from_json(["junk"]).table == {}


def test_parse_bom_columns_configuration_and_skips():
    w = World()
    res = parse_bom(w.bom(), assembly_did="d0c0000000000000000000a1", assembly="Robot", document="Robot 2026")
    names = [p.name for p in res.parts]
    assert "Bearing" not in names and any("standard content" in s for s in res.skipped)
    side = next(p for p in res.parts if p.name == "Side plate")
    assert (side.quantity, side.part_number, side.material) == (2, "2026-01-001", "Aluminum - 6061")
    assert side.processes == {"Pre Process": "", "Process": "Laser", "Process 2": ""}
    spacers = [p for p in res.parts if p.source["pid"] == "JCD"]
    assert {p.source["configuration"] for p in spacers} == {"Length=100+mm", "Length=150+mm"}
    assert len({p.key for p in spacers}) == 2
    linked = next(p for p in res.parts if p.name == "Linked plate")
    assert linked.source["link"] == "d0c0000000000000000000a1" and linked.source["wvm"] == "v"
    assert set(res.process_values["Process"]) >= {"Laser", "laser", "CNC mill", "Fiber laser", "Lathe"}
    assert res.missing == []


def test_parse_bom_with_other_property_names_and_process_1():
    bom = World().bom()
    for h in bom["headers"]:
        if h["name"] == "Process":
            h["name"], h["propertyName"] = "Process 1", "Process 1"
    res = parse_bom(bom, ["Process", "Fertigung"])
    assert res.missing == ["Fertigung"]
    assert next(p for p in res.parts if p.name == "Side plate").processes["Process"] == "Laser"


def test_machine_filter():
    parts = {p.name: p for p in parse_bom(World().bom()).parts}
    cfg = MachineConfig(values=["laser", "Fiber Laser"], slots=["Process", "Process 2"])
    assert machine_match(parts["Side plate"], cfg)[0]
    assert machine_match(parts["Slot plate"], cfg)[0]                 # case-insensitive
    assert machine_match(parts["Bracket"], cfg)[0]                    # a second value
    ok, why = machine_match(parts["Pocket plate"], cfg)
    assert not ok and "CNC mill" in why
    ok, why = machine_match(parts["Gusset"], cfg)                     # Lathe first, then Laser
    assert not ok and "Lathe" in why and "does not know" in why
    cfg.include_after_other = True
    assert machine_match(parts["Gusset"], cfg)[0]
    cfg2 = MachineConfig(values=["Laser"], slots=["Process"])         # Process 2 does not count
    cfg2.include_after_other = True
    assert not machine_match(parts["Gusset"], cfg2)[0]
    assert machine_match(parts["Pocket plate"], MachineConfig(mode="sheet"))[0]


def test_quantities_and_sheet_rule():
    cfg = MachineConfig(robots=2, spares=1)
    assert quantity_to_cut(3, cfg) == 7
    assert quantity_to_cut(3, cfg, made=5) == 2
    assert quantity_to_cut(3, cfg, made=50) == 0
    assert is_sheet(100, 60, 3, cfg) and not is_sheet(10, 60, 6, cfg) and not is_sheet(500, 500, 40, cfg)
    assert MachineConfig.from_json({"robots": 0, "spares": -3, "mode": "x"}).to_json()["robots"] == 1
