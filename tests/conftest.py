"""Shared pytest setup: headless Qt, test markers by module, and an orderly Qt teardown.

Markers (registered in pyproject.toml) are assigned here by directory/module, so test files
need no edits; a test may carry several (e.g. ``ui`` and ``sim``):

* ``parity``  tests/parity/: compare with captures of the original under Wine (excluded from
  the default run, ``pytest -m parity`` runs them);
* ``golden``  tests/golden/: the frozen artefacts of tools/make_goldens.py;
* ``safety``  tests/safety/: safety-behaviour tests (simulator fault injection);
* ``ui``      modules that build Qt widgets (offscreen);
* ``sim``     modules that start the UDP card simulator or a device simulator;
* ``unit``    everything else (pure, no Qt widgets, no sockets).

PySide6 can segfault at interpreter exit when Python-owned widgets outlive the QApplication
(seen intermittently in tests that open dialogs).  Deleting every top-level widget and
flushing deferred deletes while the application still exists avoids that.
"""
import gc
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

if sys.version_info < (3, 12):
    # PySide6 6.12 releases one reference to None too many somewhere in the main-window paths
    # (test_ui.py::test_main_window_screenshot aborts with "Fatal Python error: none_dealloc").
    # Python 3.12 made None immortal, which hides it; on 3.11 the refcount reaches zero during a
    # garbage collection.  Give None a large reserve so the test run survives (tests only).
    import ctypes

    ctypes.c_ssize_t.from_address(id(None)).value += 1 << 28

# modules (file stems under tests/) that build Qt widgets
UI_MODULES = {
    "test_camx_ui", "test_card_ui", "test_craft_ui", "test_devices_ui", "test_diag_monitors",
    "test_diag_ui", "test_draw_geometry", "test_draw_tools", "test_engrave_dialog",
    "test_integration", "test_lead_hooks", "test_machine_ops", "test_nest_ui_project",
    "test_report_window", "test_ui", "test_ui_lead",
}
# modules that start the UDP card simulator or a device simulator (sockets, threads, timing)
SIM_MODULES = {
    "test_card_homing_alarms", "test_card_resume", "test_card_scaling", "test_card_stream",
    "test_card_ui", "test_controller", "test_controller_startup", "test_devices_af_ec",
    "test_devices_follower", "test_devices_laser", "test_devices_ui", "test_diag_monitors",
    "test_integration", "test_machine_edgeseek", "test_machine_manual", "test_machine_ops",
    "test_parity_capture", "test_process_compile", "test_ui",
}
_DIR_MARKS = {"parity": "parity", "golden": "golden", "safety": "safety"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = item.path
        parts = path.parts
        marks = set()
        for d, m in _DIR_MARKS.items():
            if d in parts[:-1] and "tests" in parts:
                marks.add(m)
        if path.stem in UI_MODULES:
            marks.add("ui")
        if path.stem in SIM_MODULES:
            marks.add("sim")
        if not marks and not any(item.get_closest_marker(n) for n in ("parity", "golden", "safety", "ui", "sim")):
            marks.add("unit")
        for m in sorted(marks):
            item.add_marker(getattr(pytest.mark, m))


def pytest_sessionfinish(session, exitstatus):
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    app.processEvents()
    gc.collect()
    app.processEvents()
