"""Technology database (mlaser.services.technology): record -> layer -> compiled job, versions,
diff against factory values, JSON and LayerPara.xml import/export."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from mlaser.cam.toolpath import CamParams
from mlaser.model import Document, LayerParams
from mlaser.nc.compiler import compile_job
from mlaser.ops import demo_document
from mlaser.services import technology as T
from mlaser.services.technology_examples import examples


@pytest.fixture
def store(tmp_path):
    s = T.TechnologyStore(tmp_path / "tech.sqlite3")
    yield s
    s.close()


def _doc_with(layer_for) -> Document:
    doc = demo_document()
    for li in sorted({p.craft.layer for p in doc.parts}):
        doc.layers[li] = layer_for(li)
    return doc


def _frames(doc: Document):
    job = compile_job(doc, CamParams())
    return job, job.frames()


def test_examples_are_labelled_and_valid():
    ex = examples()
    assert len(ex) == 9
    assert {t.key.material for t in ex} == {"Mild steel S235", "Stainless steel 1.4301", "Aluminium AlMg3"}
    for t in ex:
        assert t.origin == "example" and t.is_example and not t.verified
        assert "verify on your machine" in t.notes
        t.validated()                         # every value is inside its registry range
        assert set(t.params) <= set(T.layer_registry())


def test_store_seeds_examples_per_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path))
    a = T.TechnologyStore(profile="machine A")
    b = T.TechnologyStore(profile="machine B")
    assert a.path != b.path and a.path.parent == tmp_path / "technology"
    assert len(a.all()) == len(b.all()) == 9
    a.delete(a.all()[0].id)
    assert len(T.TechnologyStore(profile="machine A").all()) == 8      # persisted, not re-seeded
    assert len(T.TechnologyStore(profile="machine B").all()) == 9


def test_technology_layer_compiles_like_hand_typed():
    """Applying a technology fills the layer exactly as typing the same values would: the model
    fields plus the GP.* overrides in extra['process'] (what the layer dialog stores)."""
    tech = next(t for t in examples() if t.key.material.startswith("Mild") and t.key.thickness_mm == 6.0)

    def by_tech(li):
        return T.apply_to_layer(LayerParams(index=li, color="#ff0000"), tech)

    def by_hand(li):
        lp = LayerParams(index=li, color="#ff0000")
        p = dict(tech.params)
        lp.speed = p.pop("GP.CutSpeed")
        lp.duty_pct = p.pop("GP.CutPower")
        lp.power_pct = p.pop("GP.CutPeakCurrent")
        lp.frequency_hz = p.pop("GP.CutFreq")
        lp.gas = p.pop("GP.CutGasType")
        lp.gas_pressure = p.pop("GP.CutAirPressure")
        lp.cut_height = p.pop("GP.CutHeight")
        lp.extra["process"] = p
        return lp

    job_t, fr_t = _frames(_doc_with(by_tech))
    job_h, fr_h = _frames(_doc_with(by_hand))
    assert fr_t == fr_h
    assert job_t.wire == job_h.wire and job_t.process_time_s == job_h.process_time_s
    _, fr_default = _frames(_doc_with(lambda li: LayerParams(index=li)))
    assert fr_default != fr_t                 # the technology changes the job
    assert job_t.pierces > 0 and job_t.process_items > 0


def test_apply_replaces_previous_layer_values():
    """Nothing of an earlier setting (LayerPara.xml values, pierce dicts, placeholder) shadows
    the technology; the layer's effective values are the record's full parameter set."""
    tech = examples()[0]
    lp = LayerParams(index=2, speed=5.0, pierce={"type": 3, "stages": [{"DrillHeight": 9.0}]},
                     extra={"values": {"GP.CutFocusPos": 7.0, "GP.SlowStart": True}, "placeholder": True,
                            "gp": {"GP.Note": "old"}})
    T.apply_to_layer(lp, tech)
    assert "placeholder" not in lp.extra and "values" not in lp.extra and "gp" not in lp.extra
    assert T.layer_values(lp) == tech.full_params()
    assert T.layer_changes(lp, tech) == []
    lp.speed = 12.0                            # an edit after applying: the diff shows it
    ch = T.layer_changes(lp, tech)
    assert [(c.key, c.old, c.new) for c in ch] == [("GP.CutSpeed", tech.params["GP.CutSpeed"], 12.0)]
    assert T.layer_technology_ref(lp)["key"]["material"] == tech.key.material


def test_versions_diff_and_revert(store):
    t = store.find(examples()[3].key)
    assert t is not None and t.version == 1
    assert store.diff_factory(t.id) == []
    t2 = store.save(replace(t, params={**t.params, "GP.CutSpeed": 33.0, "GP.CutFocusPos": -0.5},
                            craft={**t.craft, "kerf_width": 0.25}), "tuned")
    assert t2.version == 2
    assert store.save(t2, "no change").version == 2          # unchanged content: no new version
    ch = {c.key: (c.old, c.new) for c in store.diff_factory(t.id)}
    assert ch["GP.CutSpeed"] == (t.params["GP.CutSpeed"], 33.0)
    assert ch["GP.CutFocusPos"] == (t.params["GP.CutFocusPos"], -0.5)
    assert ch["craft.kerf_width"] == (t.craft["kerf_width"], 0.25)
    assert len(ch) == 3
    assert [h.comment for h in store.history(t.id)] == ["example set", "tuned"]
    r = store.revert(t.id)
    assert r.version == 3 and store.diff_factory(t.id) == []
    assert store.diff_versions(t.id, 2, 3)
    assert store.get(t.id, 2).params["GP.CutSpeed"] == 33.0  # history kept


def test_user_record_diffs_against_registry_defaults(store):
    key = T.TechKey("Brass", 1.0, "N2", "2.0 single")
    t = store.save(T.Technology(key, {"GP.CutSpeed": 40.0}))
    assert store.factory(t.id) is None
    assert [(c.key, c.new) for c in store.diff_factory(t.id)] == [("GP.CutSpeed", 40.0)]


def test_validation_refuses_out_of_range(store):
    key = T.TechKey("X", 1.0, "N2", "1.0")
    with pytest.raises(ValueError):
        store.save(T.Technology(key, {"GP.CutSpeed": 5000.0}))          # registry max 1200 mm/s
    with pytest.raises(KeyError):
        store.save(T.Technology(key, {"GP.NoSuchKey": 1}))


def test_json_round_trip(store, tmp_path):
    p = tmp_path / "tech.json"
    assert store.export_json(p) == 9
    data = json.loads(p.read_text("utf-8"))
    assert data["format"] == T.FORMAT
    other = T.TechnologyStore(tmp_path / "other.sqlite3", seed_examples=False)
    got = other.import_json(p)
    assert len(got) == 9
    for a, b in zip(sorted(store.all(), key=lambda t: t.key.as_tuple()),
                    sorted(other.all(), key=lambda t: t.key.as_tuple())):
        assert a.key == b.key and a.content() == b.content()
    with pytest.raises(ValueError):
        (tmp_path / "bad.json").write_text("{}", "utf-8")
        other.import_json(tmp_path / "bad.json")


def test_layerpara_xml_import_export(store, tmp_path):
    """The original's LayerPara.xml / craft files: every layer the file defines becomes a record
    whose values are the file's (nothing lost), and exporting writes them back."""
    from mlaser.formats.params import ParamStore, layer_category

    st = ParamStore()
    st.set_raw(layer_category(0), "GP.CutSpeed", "42.5")
    st.set_raw(layer_category(0), "GP.ManuType", "3")
    st.set_raw(layer_category(0), "GP.DrillHeight1", "8")
    st.set_raw(layer_category(0), "GP.CutGasType", "2")
    st.set_raw(layer_category(2), "GP.CutSpeed", "10")
    src = tmp_path / "SS 2mm.xml"
    st.save(src)
    recs = store.import_xml(src)
    assert [r.key.nozzle for r in recs] == ["layer 1", "layer 3"]
    assert recs[0].key.material == "SS 2mm" and recs[0].key.thickness_mm == 2.0 and recs[0].key.gas == "N2"
    assert recs[0].params == {"GP.CutSpeed": 42.5, "GP.ManuType": 3, "GP.DrillHeight1": 8.0, "GP.CutGasType": 2}
    assert store.factory(recs[0].id) is not None              # imported = the factory reference
    out = tmp_path / "LayerPara.xml"
    store.export_xml(out, recs)
    back = T.read_layer_xml(out)
    assert back[0].params == recs[0].params and back[1].params == recs[1].params
    # a layer loaded the old way (formats.params) and the same record applied give equal values
    from mlaser.formats.params import load_layer_params

    lp_file = load_layer_params(src)[0]
    lp_tech = T.apply_to_layer(LayerParams(index=0), recs[0])
    assert T.layer_values(lp_file) == T.layer_values(lp_tech)


def test_technology_from_layer_round_trip():
    tech = examples()[5]
    lp = T.apply_to_layer(LayerParams(index=1), tech)
    back = T.technology_from_layer(lp, tech.key)
    assert back.full_params() == tech.full_params()


def test_apply_craft_defaults_and_rules():
    tech = next(t for t in examples() if t.key.thickness_mm == 6.0)
    rules = copy.deepcopy(tech.rules)
    rules["small_feature"]["max_size_mm"] = 25.0          # the demo plate's holes are 16-20 mm
    rules["corner"]["max_angle_deg"] = 90.0               # its corners are right angles
    tech = replace(tech, rules=rules)
    doc = demo_document()
    parts = list(doc.parts)
    before = copy.deepcopy(doc)
    n = T.apply_craft(doc, tech, parts)
    assert n == len(parts)
    assert all(p.craft.kerf_comp == 1 and p.craft.kerf_width == tech.craft["kerf_width"] for p in doc.parts)
    closed = [p for p in doc.parts if all(c.closed for c in p.contours)]
    assert closed and all(p.craft.lead_in.kind == 1 for p in closed)
    assert all(p.craft.lead_in.kind == 0 for p in doc.parts if p not in closed)   # open: no lead
    # small-feature rule: holes below max_size_mm get the short lead, outer contours keep theirs
    from mlaser.cam.manual import contour_craft

    sf = tech.rules["small_feature"]
    small = [contour_craft(p, ci) for p in doc.parts if len(p.contours) > 1 for ci in range(len(p.contours))
             if contour_craft(p, ci).lead_in.length == sf["lead_length_mm"]]
    assert small
    assert all(p.craft.lead_in.length == tech.craft["lead_in"]["length"] for p in closed)
    assert [p.selected for p in doc.parts] == [p.selected for p in before.parts]   # selection kept
    # corner rule: cooling points at corners of at most max_angle_deg (a square: 4 corners)
    from mlaser.model import Contour, Line, Part

    pts = [(0, 0), (50, 0), (50, 50), (0, 50), (0, 0)]
    sq = Document(parts=[Part([Contour([Line(a, b) for a, b in zip(pts, pts[1:])])])])
    T.apply_craft(sq, tech, sq.parts)
    assert len(sq.parts[0].craft.extra["cool_points"]) == 4
