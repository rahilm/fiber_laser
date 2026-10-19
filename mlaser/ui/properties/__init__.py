"""The properties panel (Wave 3 Process track): edits the selection in place, with units and
live canvas previews, every edit an undo step (``PluginContext.edit``).  See
:mod:`mlaser.ui.properties.panel`."""

from .lead_diagram import LeadDiagram
from .panel import PropertiesPanel

__all__ = ["LeadDiagram", "PropertiesPanel"]
