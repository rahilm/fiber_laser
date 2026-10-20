"""Layer inspector (SETTINGS_MAP H3, WORK_ITEMS P-2): the one editor of a layer's cutting values.

* **Recipe**: which technology record the layer follows (material × thickness chips, gas,
  nozzle, *Assign recipe*) and a link to it in the Library;
* the layer's GP values in the grouped :class:`~mlaser.ui.techform.TechForm` (Cutting, Piercing,
  Gas and nozzle, Height follow, Advanced; search): a value set on this layer is bold with the
  chip "set on layer 2" and a restore button, the others are inherited from the recipe;
* **Leads and kerf (layer defaults)**: the craft values every part on the layer inherits unless
  the part sets its own (:class:`~mlaser.ui.inherited_field.InheritedField`);
* actions: *Save as new recipe…*, *Push changes into the recipe…*, *Restore all*.

Every edit is one undo step through ``PluginContext.edit`` and :mod:`mlaser.services.inheritance`
(the effective values are written into the layer, the parts follow).  The inspector is read-only
while a job runs and in Operator mode.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QInputDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout,
                               QWidget)

from mlaser.model import LayerParams
from mlaser.services import inheritance as I
from mlaser.services import technology as T
from mlaser.ui.inherited_field import InheritedField
from mlaser.ui.techform import quantity
from mlaser.ui.responsive import FlowLayout
from mlaser.ui.techform import TechForm

from .recipe_picker import RecipePicker

#: the layer-level craft values (ids of services.inheritance) shown in the inspector
LAYER_CRAFT_ROWS = (
    ("craft.lead_in.kind", "tech.lead_kind", "tech.h.lead_kind", False),
    ("craft.lead_in.length", "tech.lead_length", "tech.h.lead_length", False),
    ("craft.lead_in.angle_deg", "tech.lead_angle", "tech.h.lead_angle", False),
    ("craft.lead_in.radius", "tech.lead_radius", "tech.h.lead_radius", True),
    ("craft.lead_out.kind", "tech.lead_out_kind", "tech.h.lead_out_kind", True),
    ("craft.lead_out.length", "tech.lead_out_length", "tech.h.lead_out_length", True),
    ("craft.kerf_comp", "tech.kerf_side", "tech.h.kerf_side", False),
    ("craft.kerf_width", "tech.kerf_width", "tech.h.kerf_width", False),
)


def craft_editor(vid: str, tr: Any) -> QWidget:
    """The typed editor of a craft value id."""
    t = tr.tr
    if vid.endswith(".kind"):
        w = QComboBox()
        w.addItems([t(k) for k in ("props.lead.none", "props.lead.line", "props.lead.arc", "props.lead.line_arc")])
        return w
    if vid == "craft.kerf_comp":
        w = QComboBox()
        for v, k in ((-1, "props.kerf.none"), (0, "props.kerf.inner"), (1, "props.kerf.outer")):
            w.addItem(t(k), v)
        return w
    if vid.endswith("angle_deg"):
        return quantity("angle", 0.0, 180.0, 1)
    if vid == "craft.kerf_width":
        return quantity("small_length", 0.0, 10.0, 3)
    return quantity("length", 0.0, 1000.0, 3)


class LayerInspector(QWidget):
    """See the module docstring.  ``changed`` after every committed edit."""

    changed = Signal()

    def __init__(self, ctx: Any, store: T.TechnologyStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.ctx, self.store, self.tr_ = ctx, store, ctx.tr
        self.setObjectName("layer_inspector")
        self.layer: Optional[int] = None
        self._loading = False
        t = self.tr_.tr
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)
        # one line: the layer and the recipe it follows (a link to it in the Library)
        self.title = QLabel()
        self.title.setObjectName("layer_title")
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.RichText)
        self.title.linkActivated.connect(self._open_recipe)
        lay.addWidget(self.title)
        self.recipe_line = self.title          # (the old name)
        # ---- values
        unit = int(ctx.config.get("UN.GasPressureUnit", 0) or 0) if getattr(ctx, "config", None) is not None else 0
        self.form = TechForm(self.tr_, unit, scope="layer")
        self.form.fieldEdited.connect(self._gp_edited)
        self.form.fieldReset.connect(self._gp_reset)
        self.craft_fields: dict[str, InheritedField] = {}
        rows = []
        for vid, label, help_, secondary in LAYER_CRAFT_ROWS:
            fld = InheritedField(craft_editor(vid, self.tr_), f"layer.{vid}", self.tr_, ctx.window)
            fld.edited.connect(lambda v, vv=vid: self._craft_edited(vv, v))
            fld.reset.connect(lambda vv=vid: self._craft_reset(vv))
            self.craft_fields[vid] = fld
            rows.append((t(label), fld, t(help_), secondary))
        self.form.add_section("layer_craft", t("layer.craft_section"), rows)
        # ---- recipe: the picker and the recipe actions, a section of the form (it folds with
        # the others when the column is short; open first while the layer has no recipe)
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(0, 0, 0, 0)
        self.no_recipe = QLabel(t("layer.no_recipe"))
        self.no_recipe.setWordWrap(True)
        bv.addWidget(self.no_recipe)
        self.picker = RecipePicker(self.tr_, store)
        bv.addWidget(self.picker)
        self.assign_btn = QPushButton(t("layer.assign"))
        self.assign_btn.setToolTip(t("layer.assign_tip"))
        self.assign_btn.clicked.connect(self.assign_picked)
        acts = QWidget()
        fl = FlowLayout(acts, justify=False, hspacing=6, vspacing=6)
        fl.addWidget(self.assign_btn)
        self.action_buttons: dict[str, QPushButton] = {}
        for key, fn in (("layer.save_new", self.save_as_recipe), ("layer.push", self.push_into_recipe),
                        ("layer.reset_all", self.reset_all)):
            b = QPushButton(t(key))
            b.clicked.connect(lambda _c=False, f=fn: f())
            fl.addWidget(b)
            self.action_buttons[key] = b
        bv.addWidget(acts)
        self.recipe_section = self.form.add_widget_section("layer_recipe", t("layer.recipe"), body, at=0,
                                                           priority=15)
        from mlaser.ui.homes import bind_editor
        for w in (self.picker.material, self.picker.thickness, self.picker.gas, self.picker.nozzle):
            bind_editor(w, f"layer.recipe.{w.accessibleName()}")
        lay.addWidget(self.form, 1)
        self.setEnabled(False)

    # ---------------------------------------------------------------- state
    @property
    def document(self):
        return self.ctx.document

    def _read_only(self) -> bool:
        win = self.ctx.window
        shell = getattr(win, "shell", None)
        if shell is not None and shell.modes.mode == "operator":
            return True
        from mlaser.ui import safety_ui
        return bool(getattr(win, "safety_ui", None)) and safety_ui.job_running(win)

    def set_layer(self, layer: Optional[int]) -> None:
        self.layer = layer
        self.refresh()

    def refresh(self) -> None:
        t = self.tr_
        li = self.layer
        doc = self.document
        # no layer (no drawing): no values to show, the form takes no room
        self.form.setVisible(li is not None)
        if li is None:
            self.title.setText(t.tr("layer.none"))
            self.setEnabled(False)
            return
        lp = doc.layers.get(li)
        title = f"<b>{t.format('layer.title', n=li)}</b>"
        tech = I.recipe_of(doc, li, self.store)
        if tech is not None:
            self.title.setText(title + " · " + t.format("layer.follows",
                                                        recipe=f"<a href='recipe'>{I.recipe_label(tech)}</a>"))
            self.picker.pick(tech.key)
            self.recipe_section.set_summary(I.recipe_label(tech))
        else:
            self.title.setText(title)
            self.recipe_section.set_summary(t.tr("layer.no_recipe_short"))
        # no recipe: the picker comes first (it is what to do next)
        self.no_recipe.setVisible(tech is None)
        self.recipe_section.priority = 15 if tech is not None else 90
        if tech is None and self.recipe_section.user_choice is False:
            self.recipe_section.user_choice = None
        self.form.panel.schedule()
        marks = I.layer_overrides(lp)
        values = T.layer_values(lp) if lp is not None else T.registry_defaults()
        here = t.format("inherit.set_on", where=t.format("inherit.src.layer", n=li))
        if tech is not None:
            self.form.set_values(values, tech.full_params(), I.recipe_label(tech), marks=marks, here_label=here)
        else:
            self.form.set_values(values, None, "", marks=None)
        for vid, fld in self.craft_fields.items():
            val, src = I.effective(doc, self.store, vid, layer=li, upto=2)
            over = src == "layer"
            _pv, psrc = I.effective(doc, self.store, vid, layer=li, upto=1)
            source = I.source_label(doc, self.store, psrc, li, t)
            fld.set_state(val, over, t.format("inherit.src.layer", n=li), source,
                          parent_vid=("recipe." + vid) if psrc == "recipe" else None)
        self.setEnabled(not self._read_only())

    # ---------------------------------------------------------------- edits
    def _edit(self, fn, label_key: str) -> None:
        if self.layer is None or self._read_only():
            self.refresh()
            return
        self.ctx.edit(fn, self.tr_.tr(label_key))
        self.refresh()
        self.changed.emit()

    def _gp_edited(self, key: str, value: Any) -> None:
        li = self.layer

        def fn(d):
            if li not in d.layers:
                d.layers[li] = LayerParams(index=li)
            (d.layers[li].extra or {}).pop("placeholder", None)
            I.set_override(d, self.store, "layer", key, value, layer=li)
        self._edit(fn, "layer.edit.value")

    def _gp_reset(self, key: str) -> None:
        li = self.layer
        self._edit(lambda d: I.clear_override(d, self.store, "layer", key, layer=li), "layer.edit.reset")

    def _craft_edited(self, vid: str, value: Any) -> None:
        li = self.layer

        def fn(d):
            if li not in d.layers:
                d.layers[li] = LayerParams(index=li)
            I.set_override(d, self.store, "layer", vid, value, layer=li)
        self._edit(fn, "layer.edit.value")

    def _craft_reset(self, vid: str) -> None:
        li = self.layer
        self._edit(lambda d: I.clear_override(d, self.store, "layer", vid, layer=li), "layer.edit.reset")

    def reset_all(self) -> int:
        li = self.layer
        box: list = []
        self._edit(lambda d: box.append(I.reset_all(d, self.store, "layer", layer=li)), "layer.edit.reset")
        return box[0] if box else 0

    # ---------------------------------------------------------------- recipe
    def assign_picked(self) -> Optional[dict]:
        tech = self.picker.picked()
        if tech is None or self.layer is None:
            return None
        return self.assign(tech, [self.layer])

    def assign(self, tech: T.Technology, layers: list[int], joints: bool = False, rules: bool = False) -> dict:
        """Assign ``tech`` to ``layers`` (one undo step); values set on a layer, a part or a
        contour are kept.  Logs what was kept."""
        from mlaser.ui import safety_ui

        win = self.ctx.window
        if bool(getattr(win, "safety_ui", None)) and safety_ui.job_locked(win, self.tr_.tr("layer.assign")):
            return {}
        box: list = []
        self.ctx.edit(lambda d: box.append(I.assign_recipe(d, self.store, tech, layers, joints=joints, rules=rules)),
                      self.tr_.format("process.edit.apply", name=tech.key.label()))
        res = box[0] if box else {"layers": layers, "kept_layer": 0, "kept_parts": 0}
        self.ctx.log(self.tr_.format("layer.assigned", name=tech.key.label(), layers=", ".join(map(str, layers)),
                                     kept=res["kept_layer"], parts=res["kept_parts"]))
        self.refresh()
        self.changed.emit()
        return res

    def _open_recipe(self, _href: str) -> None:
        from mlaser.ui.homes import edit_in
        tech = I.recipe_of(self.document, self.layer, self.store) if self.layer is not None else None
        edit_in(self.ctx.window, "recipe.GP.CutSpeed", tech)

    def save_as_recipe(self) -> Optional[T.Technology]:
        """A new record holding the layer's effective values."""
        li = self.layer
        if li is None:
            return None
        lp = self.document.layers.get(li)
        tech = I.recipe_of(self.document, li, self.store)
        base = tech.key if tech is not None else T.TechKey("Material", 1.0, "N2", "nozzle")
        if getattr(self.ctx.window, "_test_no_dialogs", False):
            text, ok = f"{base.material} (layer {li})", True
        else:
            text, ok = QInputDialog.getText(self, self.tr_.tr("layer.save_new"), self.tr_.tr("tech.new_material"),
                                            text=f"{base.material} (layer {li})")
        if not ok or not text:
            return None
        new = T.technology_from_layer(lp, replace(base, material=text))
        if tech is not None:
            new = replace(new, craft=dict(tech.craft or {}), rules=dict(tech.rules or {}))
        saved = self.store.save(new, self.tr_.format("process.from_layer", n=li))
        self.changed.emit()
        return saved

    def push_into_recipe(self) -> Optional[T.Technology]:
        """The layer's changes become a new version of its recipe (after a confirmation listing
        them); the layer then follows that version with no overrides."""
        li = self.layer
        tech = I.recipe_of(self.document, li, self.store) if li is not None else None
        if tech is None:
            return None
        lp = self.document.layers[li]
        marks = I.layer_overrides(lp)
        vals = T.layer_values(lp)
        params = dict(tech.params)
        params.update({k: vals[k] for k in marks if k.startswith("GP.") and k in vals})
        craft = dict(tech.craft or {})
        lc = (lp.extra or {}).get(I.LAYER_CRAFT) or {}
        for vid, v in lc.items():
            parts = vid.split(".")[1:]
            if parts[0] in ("lead_in", "lead_out"):
                d = dict(craft.get(parts[0]) or T.DEFAULT_CRAFT[parts[0]])
                d[parts[1]] = v
                craft[parts[0]] = d
            else:
                craft[parts[0]] = v
        if not marks:
            return None
        if not getattr(self.ctx.window, "_test_no_dialogs", False):
            names = ", ".join(sorted(marks))
            if QMessageBox.question(self, self.tr_.tr("layer.push"),
                                    self.tr_.format("layer.push_confirm", n=len(marks), names=names,
                                                    recipe=I.recipe_label(tech))) != QMessageBox.Yes:
                return None
        saved = self.store.save(replace(tech, params=params, craft=craft), self.tr_.format("process.from_layer", n=li))

        def fn(d):
            lpd = d.layers[li]
            lpd.extra[I.OVERRIDES] = []
            lpd.extra[I.LAYER_CRAFT] = {}
            I.assign_recipe(d, self.store, saved, [li])
        self.ctx.edit(fn, self.tr_.tr("layer.push"))
        self.refresh()
        self.changed.emit()
        return saved


__all__ = ["LayerInspector", "LAYER_CRAFT_ROWS", "craft_editor"]
