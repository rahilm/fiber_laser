"""Start interlocks of optional built-in plugins fail closed (review A12), and the two safety-UI
handlers that defaulted to the permissive answer now take the restrictive one (review A14).

* The planned-quantity guard (``ext_diag``) and the material guard (``orders``) are installed by
  plugins.  When such a plugin fails to register, a cut is refused with a clear reason and the
  operator is told at start-up (log line and a warning), instead of starting unchecked.
* ``safety_ui.active_alarms``: an alarm list that cannot be read counts as an alarm (Start
  disabled), not as "no alarm".
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _window(tmp_path, monkeypatch, broken=()):
    from mlaser.services.config import ConfigService
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a) or QMessageBox.Ok))
    for mod in broken:
        def boom(ctx):
            raise RuntimeError("simulated register failure")
        monkeypatch.setattr(mod, "register", boom)
    cfg = ConfigService()
    cfg.set("app_dir", str(tmp_path))
    w = MainWindow(DemoController(), extensions=EXTENSIONS, config=cfg)
    return w, warned


def _close(qapp, w):
    w._test_no_dialogs = True
    w.close()
    w.deleteLater()
    qapp.processEvents()


def test_normal_start_has_both_guards_and_no_block(qapp, tmp_path, monkeypatch):
    w, warned = _window(tmp_path, monkeypatch)
    try:
        qapp.processEvents()
        assert w.start_guard_problems == []
        assert w.failed_extensions == {}
        assert w.jobs.check_start("process") == (True, "")
        assert not warned
    finally:
        _close(qapp, w)


def test_failed_guard_plugins_block_cutting_and_tell_the_operator(qapp, tmp_path, monkeypatch):
    import mlaser.ui.ext_diag as Dg
    import mlaser.ui.workspaces.orders as O
    w, warned = _window(tmp_path, monkeypatch, broken=(Dg, O))
    try:
        assert {"ext_diag", "orders"} <= set(w.failed_extensions)
        assert len(w.start_guard_problems) == 2
        ok, why = w.jobs.check_start("process")
        assert not ok and "Start is blocked" in why and "check" in why
        # laser-off runs are not what these guards check: frame / dry run stay possible
        assert w.jobs.check_start("frame") == (True, "")
        assert w.jobs.check_start("dry_run") == (True, "")
        qapp.processEvents()
        assert warned, "the operator was not told at start-up"
        assert any("Start is blocked" in str(a) for a in warned[0])
    finally:
        _close(qapp, w)


def test_one_failed_guard_plugin_blocks_too(qapp, tmp_path, monkeypatch):
    import mlaser.ui.workspaces.orders as O
    w, _warned = _window(tmp_path, monkeypatch, broken=(O,))
    try:
        assert len(w.start_guard_problems) == 1
        assert "material check" in w.start_guard_problems[0]
        assert w.jobs.check_start("process")[0] is False
    finally:
        _close(qapp, w)


def test_unreadable_alarm_list_counts_as_an_alarm(caplog):
    from mlaser.ui import safety_ui

    class Ctl:
        @property
        def active_alarms(self):
            raise RuntimeError("controller half torn down")

    class Win:
        ctl = Ctl()

    with caplog.at_level("WARNING", logger="mlaser.ui.safety"):
        alarms = safety_ui.active_alarms(Win())
    assert len(alarms) == 1
    assert alarms[0].text().startswith("Alarm state unknown")
    assert any("alarm list cannot be read" in r.getMessage() for r in caplog.records)
    # the run controls treat it as an alarm: never READY, Start not enabled
    assert safety_ui.run_state(None, None, alarms) == "alarm"


def test_emission_guard_install_failure_is_logged(qapp, tmp_path, monkeypatch, caplog):
    from mlaser.devices import laser as L
    w, _warned = _window(tmp_path, monkeypatch)
    try:
        def broken(fn):
            raise RuntimeError("registry broken")
        monkeypatch.setattr(L, "set_emission_guard", broken)
        from mlaser.ui.facade import DemoController
        with caplog.at_level("ERROR", logger="mlaser.ui.safety"):
            w.safety_ui.attach(DemoController())
        assert any("emission guard could not be installed" in r.getMessage() for r in caplog.records)
    finally:
        monkeypatch.undo()
        _close(qapp, w)
