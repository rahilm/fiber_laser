"""lp0 "Layer parameters": every GP.* parameter of one layer, in the grouped and searchable
:class:`mlaser.ui.techform.TechForm` (it used to be one 164-row tree).

What the dialog stores is unchanged: the core fields go to :class:`LayerParams` (speed, duty,
frequency, power, gas, pressure, height), every other value to ``extra['process']``, the GP.*
overrides the job compiler reads (``parity_ports.mainapp.layer.LayerView``).  ``values`` holds
the layer's values with the edits applied on OK (the old ``ParamGridDialog`` contract).

When the layer was set from a technology (Process workspace), the form compares every value
with that technology: a value changed on this layer is marked and can be reset to the
technology's.  Otherwise it compares with the registry defaults.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from mlaser.services import technology as T

from .i18n import Translator
from .params import LAYER_FIELD_MAP
from .techform import TechForm


def _store_of(parent: Any) -> Optional[T.TechnologyStore]:
    ext = getattr(parent, "extension", None)
    try:
        proc = ext("ext_process") if callable(ext) else None
    except Exception:  # noqa: BLE001 - no plugin
        proc = None
    return getattr(proc, "store", None)


class LayerParamDialog(QDialog):
    """See the module docstring.  ``store``: the technology store (default: the Process
    plugin's of ``parent``) used to show the layer's technology values as the base."""

    def __init__(self, tr: Translator, layer, parent=None, store: Optional[T.TechnologyStore] = None) -> None:
        super().__init__(parent)
        self.tr_ = tr
        self.layer = layer
        self.errors: list[str] = []
        t = tr.tr
        ex = layer.extra or {}
        vals = dict(ex.get("values", {}) or {})
        vals.update(ex.get("gp", {}) or {})
        vals.update(ex.get("process", {}) or {})
        for gp, attr in LAYER_FIELD_MAP.items():
            vals[gp] = getattr(layer, attr)
        self.values: dict = vals
        self.setWindowTitle(tr.format("layerdlg.title", n=layer.index))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)
        intro = QLabel(t("layerdlg.intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        unit = 0
        cfg = getattr(parent, "config", None)
        if cfg is not None:
            try:
                unit = int(cfg.get("UN.GasPressureUnit", 0) or 0)
            except Exception:  # noqa: BLE001
                unit = 0
        self.form = TechForm(tr, unit, scope="layer")
        lay.addWidget(self.form, 1)
        full = T.registry_defaults()
        full.update({k: v for k, v in vals.items() if k in full})
        base, label = self._base(store if store is not None else _store_of(parent))
        self.form.set_values(full, base, label)
        self._initial = self.form.values()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.accepted.connect(self._store)
        self.resize(self.sizeHint())
        # what the dialog stores is read by the compiler's LayerView: make sure its defaults are
        # available (the registry's; an open ConfigService's provider is kept)
        from mlaser.formats.params import layer_default_values
        from mlaser.parity_ports.mainapp.layer import set_layer_defaults
        set_layer_defaults(layer_default_values, replace_existing=False)

    def _base(self, store: Optional[T.TechnologyStore]) -> tuple[Optional[dict], str]:
        ref = T.layer_technology_ref(self.layer)
        if ref and store is not None:
            tech = None
            try:
                if ref.get("id") is not None:
                    tech = store.get(int(ref["id"]), int(ref["version"]) if ref.get("version") else None)
            except KeyError:
                tech = None
            if tech is None:
                try:
                    tech = store.find(T.TechKey(**(ref.get("key") or {})))
                except TypeError:
                    tech = None
            if tech is not None:
                return tech.full_params(), self.tr_.format("layerdlg.base_tech", name=tech.key.label())
        return T.registry_defaults(), self.tr_.tr("tech.base.defaults")

    def _ok(self) -> None:
        self.errors = []
        cur = self.form.values()
        for k, v in cur.items():
            if T._same(v, self._initial.get(k)) and k not in self.values:
                continue
            if k in self.values and T._same(v, self.values.get(k)):
                continue
            try:
                self.values[k] = T.validate_param(k, v)
            except (ValueError, KeyError) as e:
                self.errors.append(str(e))
        self.accept()

    def _store(self) -> None:
        for gp, attr in LAYER_FIELD_MAP.items():
            if gp in self.values:
                try:
                    setattr(self.layer, attr, type(getattr(self.layer, attr))(self.values[gp]))
                except (TypeError, ValueError):
                    pass
        self.layer.extra.pop("gp", None)
        proc = dict(self.layer.extra.get("process", {}) or {})
        proc.update({k: v for k, v in self.values.items() if k not in LAYER_FIELD_MAP})
        self.layer.extra["process"] = proc
