"""One precedence model for the cutting values (SETTINGS_MAP.md §2, WORK_ITEMS X-2).

::

    registry default ─► technology record ─► layer override ─► part override ─► contour override
                                                         effective value = the last one that is set

* **Layer values** (the GP.* cut / pierce / motion parameters): *recipe → layer override*.  A layer
  without a recipe has every value "set on this layer" (the .chf / hand-made case).
* **Craft values** (lead-in, lead-out, kerf side and width, micro-joint spec): *registry default →
  recipe craft default → layer override → part override → contour override*.  Positions that only
  make sense on geometry (start point, joint and cooling point positions) are part / contour data
  and are not in the chain.

**Effective values stay materialised** where every reader already looks: ``LayerParams`` fields
and ``extra['process']``, ``Part.craft`` and ``Contour.craft``.  The job compiler, planner, .chf,
wire and parity code read the same concrete numbers as before, so nothing downstream changes.
What is new is *which* values are overridden (the marks):

* ``LayerParams.extra['overrides']``: ids set on the layer ("GP.CutSpeed", "craft.kerf_width" ...),
  ``LayerParams.extra['craft']``: the layer-level craft values (flat: {"craft.kerf_width": 0.1}).
* ``Craft.extra['overrides']`` of a part craft or a contour's own craft: the craft ids set on that
  part / contour.  The marks live on the craft so they follow the object through copy, paste,
  undo and the .mlz file; the .chf writer reads named ``Craft.extra`` keys only, so .chf bytes are
  unchanged (and the .chf has no layers at all).

A file without marks gets them by :func:`derive_marks` (a diff against the inherited values; it
changes no value).  :func:`materialise` writes the inherited values of every value that is *not*
overridden (after a recipe is assigned or edited, an override is reset ...); overridden values are
never touched.  Qt-free.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any, Iterable, Optional

from mlaser.model import Craft, Document, LayerParams, LeadParams, Part

from . import technology as T

#: craft value ids of the chain (part / contour level fields of :class:`mlaser.model.Craft`)
LEAD_FIELDS = ("kind", "angle_deg", "length", "radius", "at_corner")
CRAFT_IDS: tuple[str, ...] = (
    *(f"craft.lead_in.{f}" for f in LEAD_FIELDS),
    *(f"craft.lead_out.{f}" for f in LEAD_FIELDS),
    "craft.kerf_comp", "craft.kerf_width",
)
#: generator settings that only exist on recipe / layer level (no Craft field)
SPEC_IDS: tuple[str, ...] = tuple(f"craft.micro_joint.{f}" for f in ("kind", "count", "distance", "length",
                                                                       "min_size"))
LEVELS = ("registry", "recipe", "layer", "part", "contour")
OVERRIDES = "overrides"
LAYER_CRAFT = "craft"


# --------------------------------------------------------------------------- craft access
def craft_get(craft: Craft, vid: str) -> Any:
    """Value of craft id ``vid`` in a :class:`Craft`."""
    parts = vid.split(".")[1:]
    if parts[0] in ("lead_in", "lead_out"):
        return getattr(getattr(craft, parts[0]), parts[1])
    return getattr(craft, parts[0])


def craft_set(craft: Craft, vid: str, value: Any) -> None:
    parts = vid.split(".")[1:]
    if parts[0] in ("lead_in", "lead_out"):
        setattr(getattr(craft, parts[0]), parts[1], value)
    else:
        setattr(craft, parts[0], value)


def _registry_craft(vid: str) -> Any:
    if vid in SPEC_IDS:
        return T.DEFAULT_CRAFT["micro_joint"][vid.rsplit(".", 1)[1]]
    return craft_get(Craft(), vid)


def _recipe_craft(tech: Optional[T.Technology], vid: str) -> tuple[bool, Any]:
    """(set, value) of craft id ``vid`` in a recipe's craft defaults."""
    if tech is None:
        return False, None
    cr = {**T.DEFAULT_CRAFT, **(tech.craft or {})}
    parts = vid.split(".")[1:]
    if parts[0] in ("lead_in", "lead_out"):
        d = cr.get(parts[0])
        if not isinstance(d, dict):
            return False, None
        base = asdict(LeadParams(kind=1 if parts[0] == "lead_in" else 0))
        base.update({k: v for k, v in d.items() if k in base})
        return True, base[parts[1]]
    if parts[0] == "micro_joint":
        mj = cr.get("micro_joint") or {}
        return parts[1] in mj, mj.get(parts[1])
    return parts[0] in cr, cr.get(parts[0])


def _same(a: Any, b: Any) -> bool:
    return T._same(a, b)


# --------------------------------------------------------------------------- marks
def _marks(holder: dict) -> list:
    v = holder.get(OVERRIDES)
    return list(v) if isinstance(v, (list, tuple)) else []


def layer_overrides(lp: Optional[LayerParams]) -> set[str]:
    return set(_marks(lp.extra or {})) if lp is not None else set()


def craft_overrides(craft: Optional[Craft]) -> set[str]:
    return set(_marks(craft.extra or {})) if craft is not None else set()


def has_marks(doc: Document) -> bool:
    """Whether the document carries override marks (a file saved with this model)."""
    if any(OVERRIDES in (lp.extra or {}) for lp in doc.layers.values()):
        return True
    for p in doc.parts:
        if OVERRIDES in (p.craft.extra or {}):
            return True
    return False


def _set_marks(holder_obj: Any, marks: Iterable[str]) -> None:
    ex = holder_obj.extra if holder_obj.extra is not None else {}
    ex[OVERRIDES] = sorted(set(marks))
    holder_obj.extra = ex


# --------------------------------------------------------------------------- recipes
def recipe_of(doc: Document, layer: int, store: Optional[T.TechnologyStore]) -> Optional[T.Technology]:
    """The technology record a layer is assigned to (its referenced version), or None."""
    lp = doc.layers.get(layer)
    ref = T.layer_technology_ref(lp)
    if not ref or store is None:
        return None
    try:
        if ref.get("id") is not None:
            return store.get(int(ref["id"]), int(ref["version"]) if ref.get("version") else None)
    except KeyError:
        pass
    try:
        return store.find(T.TechKey(**(ref.get("key") or {})))
    except TypeError:
        return None


def recipe_label(tech: T.Technology) -> str:
    return tech.key.label() + (f" · v{tech.version}" if tech.version else "")


# --------------------------------------------------------------------------- effective values
def _contour_craft(part: Part, ci: Optional[int]) -> Optional[Craft]:
    if ci is None or ci >= len(part.contours):
        return None
    c = part.contours[ci].craft
    return c if c is not None and c is not part.craft else None


def inherited(doc: Document, store: Optional[T.TechnologyStore], vid: str, layer: Optional[int] = None,
              part: Optional[Part] = None, contour: Optional[int] = None,
              level: Optional[str] = None) -> tuple[Any, str]:
    """(value, source) a value would have at ``level`` without an override there: the parent's
    effective value.  ``level`` defaults to the most specific target given."""
    if level is None:
        level = "contour" if contour is not None and part is not None else "part" if part is not None else "layer"
    return effective(doc, store, vid, layer=layer, part=part, contour=None, upto=LEVELS.index(level) - 1)


def effective(doc: Document, store: Optional[T.TechnologyStore], vid: str, layer: Optional[int] = None,
              part: Optional[Part] = None, contour: Optional[int] = None, upto: int = 4) -> tuple[Any, str]:
    """(value, source) of value id ``vid`` for a layer / part / contour; ``source`` in
    :data:`LEVELS`.  ``upto`` limits the chain (index in LEVELS: 2 = up to the layer)."""
    if part is not None and layer is None:
        layer = part.craft.layer
    lp = doc.layers.get(layer) if layer is not None else None
    tech = recipe_of(doc, layer, store) if layer is not None else None
    if vid.startswith("GP."):
        cur = T.layer_values(lp).get(vid) if lp is not None else None
        if lp is not None and (tech is None or vid in layer_overrides(lp)) and upto >= 2:
            return cur, "layer"
        if tech is not None and upto >= 1:
            return tech.full_params().get(vid), "recipe"
        return T.registry_defaults().get(vid), "registry"
    # craft chain
    if upto >= 4 and part is not None and contour is not None:
        cc = _contour_craft(part, contour)
        if cc is not None and vid in craft_overrides(cc) and vid in CRAFT_IDS:
            return craft_get(cc, vid), "contour"
    if upto >= 3 and part is not None and vid in craft_overrides(part.craft) and vid in CRAFT_IDS:
        return craft_get(part.craft, vid), "part"
    if upto >= 2 and lp is not None and vid in layer_overrides(lp):
        return (lp.extra or {}).get(LAYER_CRAFT, {}).get(vid, _registry_craft(vid)), "layer"
    if upto >= 1:
        ok, v = _recipe_craft(tech, vid)
        if ok:
            return v, "recipe"
    return _registry_craft(vid), "registry"


def source_label(doc: Document, store: Optional[T.TechnologyStore], source: str, layer: Optional[int] = None,
                 tr: Any = None) -> str:
    """Plain words for the chip: "Mild steel S235 3 mm · O2 · v4", "layer 2", "this part"."""
    def t(key: str, default: str, **kw) -> str:
        if tr is None:
            return default.format(**kw)
        return tr.format(key, **kw) if kw else tr.tr(key, default)
    if source == "recipe" and layer is not None:
        tech = recipe_of(doc, layer, store)
        if tech is not None:
            return recipe_label(tech)
    return {"registry": t("inherit.src.registry", "default"),
            "layer": t("inherit.src.layer", "layer {n}", n=layer if layer is not None else "?"),
            "part": t("inherit.src.part", "this part"),
            "contour": t("inherit.src.contour", "this contour")}.get(source, source)


# --------------------------------------------------------------------------- overrides
def set_override(doc: Document, store: Optional[T.TechnologyStore], level: str, vid: str, value: Any,
                 layer: Optional[int] = None, part: Optional[Part] = None,
                 contour: Optional[int] = None) -> bool:
    """Set ``vid`` on a level (``"layer"`` with ``layer``, ``"part"`` with ``part``, ``"contour"``
    with ``part`` and ``contour``) and mark it overridden; a value equal to the inherited one
    clears the mark instead.  The levels below follow (materialised).  Returns True when the
    value is an override afterwards."""
    if level == "layer":
        if layer is None:
            raise ValueError("layer level needs a layer")
        lp = doc.layers[layer]
        parent, _src = effective(doc, store, vid, layer=layer, upto=1)
        marks = layer_overrides(lp)
        same = _same(value, parent) and T.layer_technology_ref(lp) is not None
        if vid.startswith("GP."):
            _write_gp(lp, vid, value)
        else:
            ex = lp.extra if lp.extra is not None else {}
            lc = dict(ex.get(LAYER_CRAFT) or {})
            if same:
                lc.pop(vid, None)
            else:
                lc[vid] = value
            ex[LAYER_CRAFT] = lc
            lp.extra = ex
        if same:
            marks.discard(vid)
        else:
            marks.add(vid)
        _set_marks(lp, marks)
        materialise(doc, store, layers=[layer])
        return vid in marks
    if part is None:
        raise ValueError("part / contour level needs a part")
    if vid not in CRAFT_IDS:
        raise KeyError(f"{vid} is not a part / contour value")
    if level == "contour":
        from mlaser.cam.manual import contour_craft
        cr = contour_craft(part, int(contour or 0), create=True)
        parent, _src = effective(doc, store, vid, part=part, upto=3)
    else:
        cr = part.craft
        parent, _src = effective(doc, store, vid, layer=part.craft.layer, upto=2)
    craft_set(cr, vid, copy.deepcopy(value))
    marks = craft_overrides(cr)
    if _same(value, parent):
        marks.discard(vid)
    else:
        marks.add(vid)
    _set_marks(cr, marks)
    if level == "part":
        _materialise_part(doc, store, part)
    return vid in marks


def clear_override(doc: Document, store: Optional[T.TechnologyStore], level: str, vid: str,
                   layer: Optional[int] = None, part: Optional[Part] = None,
                   contour: Optional[int] = None) -> Any:
    """Remove the override: the value returns to the inherited one (written).  Returns it."""
    if level == "layer":
        if layer is None:
            raise ValueError("layer level needs a layer")
        lp = doc.layers[layer]
        marks = layer_overrides(lp)
        marks.discard(vid)
        _set_marks(lp, marks)
        lc = dict((lp.extra or {}).get(LAYER_CRAFT) or {})
        lc.pop(vid, None)
        lp.extra[LAYER_CRAFT] = lc
        if vid.startswith("GP."):
            v, _ = effective(doc, store, vid, layer=layer, upto=1)
            if T.layer_technology_ref(lp) is not None:
                _write_gp(lp, vid, v)
        materialise(doc, store, layers=[layer])
        return effective(doc, store, vid, layer=layer)[0]
    if part is None:
        raise ValueError("part / contour level needs a part")
    cr = _contour_craft(part, contour) if level == "contour" else part.craft
    if cr is None:
        return effective(doc, store, vid, part=part, contour=contour)[0]
    marks = craft_overrides(cr)
    marks.discard(vid)
    _set_marks(cr, marks)
    _materialise_part(doc, store, part)
    return effective(doc, store, vid, part=part, contour=contour)[0]


def reset_all(doc: Document, store: Optional[T.TechnologyStore], level: str, layer: Optional[int] = None,
              part: Optional[Part] = None, contour: Optional[int] = None) -> int:
    if level == "layer":
        ids = layer_overrides(doc.layers.get(layer) if layer is not None else None)
    elif part is None:
        return 0
    else:
        cr = _contour_craft(part, contour) if level == "contour" else part.craft
        ids = craft_overrides(cr)
    for vid in sorted(ids):
        clear_override(doc, store, level, vid, layer=layer, part=part, contour=contour)
    return len(ids)


# --------------------------------------------------------------------------- materialise
def _write_gp(lp: LayerParams, key: str, value: Any) -> None:
    """Write one GP value the way the layer dialog / apply_to_layer store it."""
    attr = T.CORE_FIELDS.get(key)
    if attr is not None:
        cur = getattr(lp, attr)
        setattr(lp, attr, type(cur)(value))
        return
    ex = lp.extra if lp.extra is not None else {}
    proc = dict(ex.get("process") or {})
    proc[key] = value
    ex["process"] = proc
    lp.extra = ex


def materialise(doc: Document, store: Optional[T.TechnologyStore], layers: Optional[Iterable[int]] = None) -> int:
    """Write the inherited value of every value that is not overridden (layers with a recipe:
    the GP values; every part / contour on those layers: the craft values).  A value already
    equal is not written, so a document whose marks were derived is left as it is.  Returns
    the number of values written."""
    if not has_marks(doc):
        derive_marks(doc, store)          # never overwrite values of a file whose marks are unknown
    want = set(layers) if layers is not None else set(doc.layers) | {p.craft.layer for p in doc.parts}
    n = 0
    for li in sorted(want):
        lp = doc.layers.get(li)
        tech = recipe_of(doc, li, store)
        if lp is not None and tech is not None:
            marks = layer_overrides(lp)
            cur = T.layer_values(lp)
            for k, v in tech.full_params().items():
                if k in marks or k not in cur or _same(cur.get(k), v):
                    continue
                _write_gp(lp, k, v)
                n += 1
    for p in doc.parts:
        li = p.craft.layer
        if li not in want:
            continue
        if T.layer_technology_ref(doc.layers.get(li)) is not None and recipe_of(doc, li, store) is None:
            continue                      # the recipe is not known here (no store): leave the parts
        n += _materialise_part(doc, store, p)
    return n


def _materialise_part(doc: Document, store: Optional[T.TechnologyStore], part: Part) -> int:
    n = 0
    marks = craft_overrides(part.craft)
    for vid in CRAFT_IDS:
        if vid in marks:
            continue
        v, _src = effective(doc, store, vid, part=part, upto=2)
        if not _same(craft_get(part.craft, vid), v):
            craft_set(part.craft, vid, v)
            n += 1
    for ci, c in enumerate(part.contours):
        cc = _contour_craft(part, ci)
        if cc is None:
            continue
        cm = craft_overrides(cc)
        for vid in CRAFT_IDS:
            if vid in cm:
                continue
            v = craft_get(part.craft, vid)
            if not _same(craft_get(cc, vid), v):
                craft_set(cc, vid, copy.deepcopy(v))
                n += 1
    return n


# --------------------------------------------------------------------------- migration
def derive_marks(doc: Document, store: Optional[T.TechnologyStore], force: bool = False) -> int:
    """Marks for a document saved without them (.chf, DXF, an older .mlz): every value that
    differs from what it would inherit is marked as set at its level.  No value changes.
    Returns the number of marks."""
    if has_marks(doc) and not force:
        return 0
    n = 0
    for li, lp in doc.layers.items():
        tech = recipe_of(doc, li, store)
        marks: set[str] = set()
        if tech is not None:
            marks = {c.key for c in T.layer_changes(lp, tech)}
        _set_marks(lp, marks)
        n += len(marks)
    for p in doc.parts:
        pm = set()
        for vid in CRAFT_IDS:
            v, _ = effective(doc, store, vid, layer=p.craft.layer, upto=2)
            if not _same(craft_get(p.craft, vid), v):
                pm.add(vid)
        _set_marks(p.craft, pm)
        n += len(pm)
        for ci, c in enumerate(p.contours):
            cc = _contour_craft(p, ci)
            if cc is None:
                continue
            cm = {vid for vid in CRAFT_IDS if not _same(craft_get(cc, vid), craft_get(p.craft, vid))}
            _set_marks(cc, cm)
            n += len(cm)
    return n


def pin_crafts(parts: Iterable[Part]) -> None:
    """Mark every craft value of ``parts`` (and their contours' own crafts) as their own: a
    recipe assigned afterwards does not change their leads or kerf."""
    for p in parts:
        for cr in [p.craft] + [c.craft for c in p.contours if c.craft is not None and c.craft is not p.craft]:
            _set_marks(cr, craft_overrides(cr) | set(CRAFT_IDS))


# --------------------------------------------------------------------------- positions set by hand
START = "craft.start_param"


def mark_start(part: Part, ci: int) -> None:
    """A start point set by hand (start tool, Properties): later lead edits keep it."""
    from mlaser.cam.manual import contour_craft
    cr = contour_craft(part, ci)
    _set_marks(cr, craft_overrides(cr) | {START})


def start_is_set(part: Part, ci: int) -> bool:
    from mlaser.cam.manual import contour_craft
    return START in craft_overrides(contour_craft(part, ci))


# --------------------------------------------------------------------------- recipes on layers
def assign_recipe(doc: Document, store: Optional[T.TechnologyStore], tech: T.Technology,
                  layers: Iterable[int], joints: bool = False, rules: bool = False) -> dict:
    """Assign a recipe to layers (SETTINGS_MAP *Assign recipe*, replacing the copy-once *Apply
    technology*): the layers follow the record, and so do the craft values of their parts;
    every value overridden on a layer, a part or a contour is kept.  The values of a layer that
    had no recipe were never overrides: they are replaced (values the user explicitly set,
    marked, are kept).  ``joints`` / ``rules``: also generate the record's micro-joints and its
    rules (small-feature leads, cooling points, start placement) on the parts, marked as their
    parts' / contours' own values.  Returns {"layers", "kept_layer", "kept_parts"}."""
    from mlaser.model import LayerParams

    layers = list(layers)
    kept_layer = 0
    for li in layers:
        lp = doc.layers.get(li) or LayerParams(index=li)
        kept = layer_overrides(lp)
        cur = T.layer_values(lp)
        keep_vals = {k: cur.get(k) for k in kept if k.startswith("GP.")}
        T.apply_to_layer(lp, tech)
        for k, v in keep_vals.items():
            _write_gp(lp, k, v)
        _set_marks(lp, kept)
        doc.layers[li] = lp
        kept_layer += len(kept)
    materialise(doc, store, layers=layers)
    targets = [p for p in doc.parts if p.craft.layer in layers]
    if (joints or rules) and targets:
        T.apply_craft(doc, tech, targets, leads=False, kerf=False, joints=joints, rules=rules)
        for p in targets:                      # what the rules generated is the part's own
            for ci in range(len(p.contours)):
                cc = _contour_craft(p, ci)
                if cc is not None:
                    _set_marks(cc, craft_overrides(cc) | {vid for vid in CRAFT_IDS
                                                          if not _same(craft_get(cc, vid), craft_get(p.craft, vid))})
    kept_parts = sum(1 for p in targets if craft_overrides(p.craft) or any(
        craft_overrides(_contour_craft(p, ci)) for ci in range(len(p.contours)) if _contour_craft(p, ci)))
    return {"layers": layers, "kept_layer": kept_layer, "kept_parts": kept_parts}


__all__ = ["CRAFT_IDS", "SPEC_IDS", "LEVELS", "effective", "inherited", "set_override", "clear_override",
           "reset_all", "materialise", "derive_marks", "assign_recipe", "recipe_of", "source_label", "layer_overrides",
           "craft_overrides", "has_marks", "craft_get", "craft_set", "mark_start", "start_is_set"]
