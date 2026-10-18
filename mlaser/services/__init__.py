"""Application services (Wave 2, docs/dev/ARCHITECTURE.md "services layer").

The layer between the Qt application (``mlaser.ui``) and the domain (model, ops, cam, nc,
controller).  Nothing here imports Qt, so every service is unit-testable without a display:

* :mod:`.document` - :class:`DocumentService`: the document, edit transactions, undo.
* :mod:`.undo` - command history, stable ids, the round-trip side table.
* :mod:`.commands` - :class:`CommandRegistry`: command id -> handler, duplicates refused.
* :mod:`.plugins` - :class:`PluginContext`, the plugin API, entry-point discovery.
* :mod:`.config` - :class:`ConfigService`: the typed parameter store and machine profile.
* :mod:`.jobs` - :class:`PlannedJob`, :class:`JobService`, the :class:`JobSubmitter` protocol.
* :mod:`.production` - production counters, planned-quantity interlock, maintenance.
"""
