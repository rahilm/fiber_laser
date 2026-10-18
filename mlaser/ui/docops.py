"""Compatibility alias: the document operations moved to :mod:`mlaser.ops` (Wave 2, "business
logic out of the UI").  ``mlaser.ui.docops.X`` keeps working for the names that still exist;
new code imports :mod:`mlaser.ops`."""
from __future__ import annotations

from mlaser.ops.craft import (MicroJointParams, apply_kerf, apply_lead, auto_microjoints,  # noqa: F401
                              clear_joints, default_lead_params)
from mlaser.ops.document import (_bbox, _targets, align, delete_selected, demo_document, mirror,  # noqa: F401
                                 paste_parts, reverse, rotate, scale, scale_to, select_where,
                                 set_layer, targets, transform_parts, translate)
