"""The precedence model (services/inheritance.py, WORK_ITEMS X-2): registry -> recipe -> layer
override -> part override -> contour override, with the effective values materialised where the
compiler, .chf and parity read them.

(a) migration changes no value: for the test corpus, every layer's effective GP values and every
    craft are identical after derive_marks + materialise, and the .chf bytes are identical;
(b) a recipe change reaches the values that follow it, never an overridden one; reset restores
    the inherited value;
(c) applying a lead keeps a start point set by hand.
"""
from __future__ import annotations

import copy
import pathlib

import pytest

from mlaser import ops
from mlaser.model import Contour, Craft, Document, LayerParams, LeadParams, Line, Part
from mlaser.services import inheritance as I
from mlaser.services import technology as T

DATA = pathlib.Path(__file__).parent / "data"


@pytest.fixture
def store(tmp_path):
    s = T.TechnologyStore(tmp_path / "tech.sqlite3")
    yield s
    s.close()


def _corpus():
    from mlaser.io.dxf_import import import_dxf
    from mlaser.parity_ports.cadmodule.chf import load_chf
    from mlaser.project.format import load_project

    out = [("demo", ops.demo_document())]
    for p in sorted((DATA / "chf").glob("*.chf")):
        out.append((p.name, load_chf(p)))
    for p in sorted((DATA / "dxf").glob("*.dxf")):
        out.append((p.name, import_dxf(str(p))))
    for p in sorted((DATA / "project").glob("*.mlz")):
        out.append((p.name, load_project(p).document))
    return out


def _snapshot(doc):
    layers = {li: T.layer_values(lp) for li, lp in doc.layers.items()}
    crafts = [(copy.deepcopy(p.craft), [copy.deepcopy(c.craft) for c in p.contours]) for p in doc.parts]
    return layers, crafts


def _strip_marks(crafts):
    for pc, cs in crafts:
        for c in [pc] + [c for c in cs if c is not None]:
            c.extra.pop(I.OVERRIDES, None)
    return crafts


@pytest.mark.parametrize("name,doc", _corpus(), ids=lambda v: v if isinstance(v, str) else "")
def test_migration_changes_no_value(name, doc, store, tmp_path):
    from mlaser.parity_ports.cadmodule.chf import save_chf

    before = _snapshot(doc)
    chf_before = tmp_path / "a.chf"
    save_chf(doc, chf_before)
    I.derive_marks(doc, store)
    assert I.materialise(doc, store) == 0
    after = _snapshot(doc)
    assert after[0] == before[0]
    assert _strip_marks(after[1]) == _strip_marks(copy.deepcopy(before[1]))
    chf_after = tmp_path / "b.chf"
    save_chf(doc, chf_after)
    assert chf_after.read_bytes() == chf_before.read_bytes()


def _square_doc():
    sq = [(0, 0), (60, 0), (60, 40), (0, 40)]
    h = [(20, 10), (30, 10), (30, 20), (20, 20)]
    parts = [Part([Contour([Line(h[i], h[(i + 1) % 4]) for i in range(4)], True),
                   Contour([Line(sq[i], sq[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=1)),
             Part([Contour([Line(sq[i], sq[(i + 1) % 4]) for i in range(4)], True)], Craft(layer=1))]
    return Document(parts=parts, layers={1: LayerParams(index=1)})


def test_recipe_follows_except_overrides(store):
    doc = _square_doc()
    tech = store.all()[0]
    I.derive_marks(doc, store)
    I.assign_recipe(doc, store, tech, [1])
    lp = doc.layers[1]
    assert lp.speed == tech.full_params()["GP.CutSpeed"]
    want_lead = (tech.craft or T.DEFAULT_CRAFT)["lead_in"]["length"]
    assert all(p.craft.lead_in.length == want_lead for p in doc.parts)
    assert I.effective(doc, store, "GP.CutSpeed", layer=1)[1] == "recipe"
    # overrides: a layer speed, a part's lead length, a contour's kerf
    assert I.set_override(doc, store, "layer", "GP.CutSpeed", 12.5, layer=1)
    assert I.set_override(doc, store, "part", "craft.lead_in.length", 7.0, part=doc.parts[1])
    assert I.set_override(doc, store, "contour", "craft.kerf_width", 0.3, part=doc.parts[0], contour=0)
    assert I.effective(doc, store, "GP.CutSpeed", layer=1) == (12.5, "layer")
    assert I.effective(doc, store, "craft.lead_in.length", part=doc.parts[1]) == (7.0, "part")
    assert I.effective(doc, store, "craft.kerf_width", part=doc.parts[0], contour=0) == (0.3, "contour")
    # the recipe changes: what follows it moves, the overrides stay
    new = store.save(T.Technology(tech.key, {**tech.params, "GP.CutSpeed": 99.0, "GP.CutFreq": 1234},
                                  {**(tech.craft or {}), "lead_in": {**T.DEFAULT_CRAFT["lead_in"], "length": 4.5},
                                   "kerf_width": 0.2, "kerf_comp": 1},
                                  tech.rules, origin=tech.origin, id=tech.id))
    I.assign_recipe(doc, store, new, [1])
    assert lp.speed == 12.5 and lp.frequency_hz == 1234
    assert doc.parts[1].craft.lead_in.length == 7.0 and doc.parts[0].craft.lead_in.length == 4.5
    assert doc.parts[0].contours[0].craft.kerf_width == 0.3          # contour override kept
    assert doc.parts[0].contours[1].craft is None or doc.parts[0].contours[1].craft.kerf_width == 0.2
    assert doc.parts[0].craft.kerf_width == 0.2
    # reset: back to the inherited value
    assert I.clear_override(doc, store, "layer", "GP.CutSpeed", layer=1) == 99.0 and lp.speed == 99.0
    assert I.clear_override(doc, store, "part", "craft.lead_in.length", part=doc.parts[1]) == 4.5
    # typing the inherited value clears the mark
    assert not I.set_override(doc, store, "layer", "GP.CutFreq", 1234, layer=1)
    assert "GP.CutFreq" not in I.layer_overrides(lp)
    # the compiler reads the materialised values: the effective layer values are the recipe's
    # except what is overridden
    assert T.layer_values(lp)["GP.CutSpeed"] == 99.0


def test_layer_level_craft_override_reaches_parts(store):
    doc = _square_doc()
    I.derive_marks(doc, store)
    I.assign_recipe(doc, store, store.all()[0], [1])
    I.set_override(doc, store, "layer", "craft.kerf_width", 0.25, layer=1)
    I.set_override(doc, store, "layer", "craft.kerf_comp", 1, layer=1)
    assert all(p.craft.kerf_width == 0.25 and p.craft.kerf_comp == 1 for p in doc.parts)
    assert I.effective(doc, store, "craft.kerf_width", part=doc.parts[0])[1] == "layer"


def test_lead_apply_keeps_a_start_set_by_hand():
    from mlaser.cam.manual import contour_craft, set_start_at

    doc = _square_doc()
    for p in doc.parts:
        p.selected = True
    part = doc.parts[1]
    set_start_at(part, 0, 0.4 * part.contours[0].length())
    I.mark_start(part, 0)
    ops.apply_lead(doc, LeadParams(kind=1, length=3.0), LeadParams(kind=0), keep_start=I.start_is_set)
    assert contour_craft(part, 0).start_param == pytest.approx(0.4)
    # without the mark (and without keep_start: the parity default) the start is chosen again
    ops.apply_lead(doc, LeadParams(kind=1, length=4.0), LeadParams(kind=0))
    assert contour_craft(part, 0).start_param != pytest.approx(0.4)
