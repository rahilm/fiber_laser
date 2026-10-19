import logging as _logging

# library logging: nothing is printed unless the application configures handlers
# (mlaser.machine.logsetup.setup_logging, called by the GUI entry point)
_logging.getLogger("mlaser").addHandler(_logging.NullHandler())
