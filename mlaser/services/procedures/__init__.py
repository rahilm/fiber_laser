"""Guided procedures (wizards): Qt-free step machines run against the MachineAPI.

=====================  ===========================================  ============
id                     class                                        lowest mode
=====================  ===========================================  ============
``edge_seek``          :class:`.edge_seek.EdgeSeekProcedure`         operator
``follower_calibration`` :class:`.follower_cal.FollowerCalibrationProcedure` engineer
``focus_home``         :class:`.focus_home.FocusHomingProcedure`     operator
``laser_test``         :class:`.laser_test.LaserTestProcedure`       engineer
``commissioning``      :class:`.commissioning.CommissioningProcedure`  service
=====================  ===========================================  ============

See :mod:`.engine` for the step kinds and the safety rules (abort = ``safe_halt``), and
:mod:`.simrig` for the dry-run simulators.  The UI is ``mlaser.ui.wizards``.
"""
from .engine import (ABORTED, DONE, FAILED, MODES, READY, RUNNING, WAITING, Procedure,  # noqa: F401
                     ProcedureAborted, Step, StepFailed, mode_allows)

MIN_MODES = {"edge_seek": "operator", "follower_calibration": "engineer", "focus_home": "operator",
             "laser_test": "engineer", "commissioning": "service", "settings_import": "service"}
