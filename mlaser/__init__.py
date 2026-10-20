import logging as _logging

#: the application version (PEP 440); kept equal to ``[project] version`` in pyproject.toml
#: (tests/test_plugin_manager.py checks it).  Plugins declare a range against it (``mlaser = ...``
#: in mlaser-plugin.toml, docs/dev/PLUGIN_STANDARD.md).
__version__ = "0.1.0rc1"

# library logging: nothing is printed unless the application configures handlers
# (mlaser.machine.logsetup.setup_logging, called by the GUI entry point)
_logging.getLogger("mlaser").addHandler(_logging.NullHandler())
