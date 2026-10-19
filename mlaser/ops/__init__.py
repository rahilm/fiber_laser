"""Domain operations on the document model (Qt-free).

``mlaser.ops`` is where business logic that used to live in the UI goes (Wave 2, ROADMAP
"business logic out of the UI"): geometry transforms, craft edits, the CAM commands of the
ribbon.  The UI and the services call these; ``mlaser.diag`` (batch cutting) does too, so the
package sits below ``diag`` in the layer contract.  Nothing here knows about undo: run an op
inside :meth:`mlaser.services.document.DocumentService.edit` to make it undoable.
"""

from __future__ import annotations

from .craft import (
    MicroJointParams,
    apply_kerf,
    apply_lead,
    auto_microjoints,
    clear_joints,
    default_lead_params,
    one_key,
)
from .document import (
    align,
    contour_targets,
    is_raster,
    delete_selected,
    demo_document,
    mirror,
    paste_parts,
    reverse,
    rotate,
    scale,
    scale_to,
    select_where,
    selection_bbox,
    set_bbox,
    set_layer,
    targets,
    transform_parts,
    translate,
)

__all__ = [
    "MicroJointParams", "contour_targets", "is_raster", "apply_kerf", "apply_lead", "auto_microjoints", "clear_joints",
    "default_lead_params", "one_key", "align", "delete_selected", "demo_document", "mirror",
    "paste_parts", "reverse", "rotate", "scale", "scale_to", "select_where", "selection_bbox", "set_bbox",
    "set_layer", "targets",
    "transform_parts", "translate",
]
