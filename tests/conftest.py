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

Under pytest-xdist (CI: ``-n auto --dist loadgroup``) every ``safety`` and ``sim`` test is put
in one xdist group, ``timing``: they run one after another on a single worker, in the same
process, as in a serial run, so their real-time bounds keep their meaning (docs/dev/TESTING.md,
"Parallel runs").  Without xdist nothing changes.

PySide6 can segfault at interpreter exit when Python-owned widgets outlive the QApplication
(seen intermittently in tests that open dialogs).  Deleting every top-level widget and
flushing deferred deletes while the application still exists avoids that.
"""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if os.environ.get("MLASER_FAULT_LOG"):   # CI diagnostics: a native crash's Python stacks to a file
    import faulthandler
    # one file per xdist worker (fault.log.gw0, ...): the workers would overwrite each other
    _wid = os.environ.get("PYTEST_XDIST_WORKER")
    _FAULT_LOG = open(os.environ["MLASER_FAULT_LOG"] + (f".{_wid}" if _wid else ""), "w")  # noqa: SIM115 - open for the session
    faulthandler.enable(file=_FAULT_LOG, all_threads=True)

import pytest  # noqa: E402

# modules (file stems under tests/) that build Qt widgets
UI_MODULES = {
    "test_camx_ui", "test_card_ui", "test_craft_ui", "test_devices_ui", "test_diag_monitors",
    "test_diag_ui", "test_draw_geometry", "test_draw_tools", "test_engrave_dialog",
    "test_integration", "test_lead_hooks", "test_machine_ops", "test_nest_ui_project",
    "test_report_window", "test_ui", "test_ui_lead", "test_help_browser", "test_project_ui",
    "test_machine_workspace", "test_ws_run", "test_partlist_cache", "test_run_controls_visible",
    "test_import_worker_ui",
    # Review 2 shop floor (automation track)
    "test_notify_ui", "test_labels", "test_marking", "test_practice", "test_runsource",
}
# modules that start the UDP card simulator or a device simulator (sockets, threads, timing)
SIM_MODULES = {
    "test_card_homing_alarms", "test_card_resume", "test_card_scaling", "test_card_stream",
    "test_card_ui", "test_controller", "test_controller_startup", "test_devices_af_ec",
    "test_devices_follower", "test_devices_laser", "test_devices_ui", "test_diag_monitors",
    "test_integration", "test_machine_edgeseek", "test_machine_manual", "test_machine_ops",
    "test_parity_capture", "test_process_compile", "test_ui", "test_procedures",
    "test_machine_workspace", "test_ws_run",
    "test_practice",          # lessons on the card simulator (real-time waits)
}
_DIR_MARKS = {"parity": "parity", "golden": "golden", "safety": "safety"}
#: markers whose tests share one xdist worker (real-time assertions, simulators, sockets)
TIMING_MARKS = ("safety", "sim")
TIMING_GROUP = "timing"
PARITY_GROUP = "parity"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """Every test gets its own ConfigService directory (mlaser.services.config): values a test
    sets never leak into the next test's windows."""
    if "MLASER_CONFIG_DIR" not in os.environ or os.environ.get("_MLASER_CONFIG_AUTO"):
        monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path_factory.mktemp("config")))
        monkeypatch.setenv("_MLASER_CONFIG_AUTO", "1")
    # the machine plugin keeps softPara.ini (NormalExit) in the app directory: one per test, so a
    # window that was never closed cleanly does not raise the recovery dialog in the next one
    if "MLASER_APP_DIR" not in os.environ or os.environ.get("_MLASER_APP_AUTO"):
        monkeypatch.setenv("MLASER_APP_DIR", str(tmp_path_factory.mktemp("app")))
        monkeypatch.setenv("_MLASER_APP_AUTO", "1")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # tryfirst: the markers must be on the items before ``-m`` deselects (the mark plugin) and
    # before xdist's worker turns ``xdist_group`` into the "@timing" node id suffix that
    # --dist loadgroup schedules by (its hook is registered after this conftest, so it would
    # otherwise run first and see no group)
    group = config.pluginmanager.hasplugin("xdist")
    for item in items:
        path = item.path
        parts = path.parts
        marks = set()
        # directories below tests/ only (a checkout may itself live in a directory named "safety")
        below = parts[len(parts) - 1 - parts[::-1].index("tests") + 1:-1] if "tests" in parts else ()
        for d, m in _DIR_MARKS.items():
            if d in below:
                marks.add(m)
        if path.stem in UI_MODULES:
            marks.add("ui")
        if path.stem in SIM_MODULES:
            marks.add("sim")
        if not marks and not any(item.get_closest_marker(n) for n in ("parity", "golden", "safety", "ui", "sim")):
            marks.add("unit")
        for m in sorted(marks):
            item.add_marker(getattr(pytest.mark, m))
        if group and any(item.get_closest_marker(m) for m in TIMING_MARKS):
            item.add_marker(pytest.mark.xdist_group(TIMING_GROUP))
        elif group and item.get_closest_marker("parity"):
            # the parity areas share ../parity/out and depend on each other's output (jobcad ->
            # jobmainapp -> jobnc), so they run in order on one worker
            item.add_marker(pytest.mark.xdist_group(PARITY_GROUP))


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


_frozen_module = [None]


def pytest_runtest_setup(item):
    """At each new test module: collect what the previous modules left, then freeze the
    survivors (``gc.freeze``), so a full collection during a test scans only that test's objects.

    The suite's heap grows to ~2 million objects (windows, documents and caches of earlier
    tests); a full collection of it held the GIL for 0.4-0.6 s (1 s on the CI runners) in the
    middle of timing tests: the simulated card ran dry, links and heartbeats looked dead.  The
    collect at the boundary scans only the objects created since the last freeze."""
    mod = item.module.__name__ if item.module is not None else None
    if mod != _frozen_module[0]:
        _frozen_module[0] = mod
        gc.collect()
        gc.freeze()
