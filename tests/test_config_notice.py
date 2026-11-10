"""The start-up notice for a config.json that could not be loaded (architecture review A3,
``mlaser/ui/config_notice.py``): arming is refused until it is answered, its default is the safe
choice, its buttons are verbs, and it fits a half-screen window."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from mlaser.services.config import ConfigService
from mlaser.ui import config_notice as N


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _damage(cfg_dir, backup: bool = True) -> None:
    p = cfg_dir / "config.json"
    c = ConfigService(p)
    c.set("MAC.WritePluse", 20000)
    c.set("safety.door_input", 5)
    c.set_profile("params")
    c.save()
    if backup:
        ConfigService(p)                     # a clean start keeps config.json.bak
    good = p.read_bytes()
    p.write_bytes(good[: len(good) // 2])    # a power cut during a hand edit


@pytest.fixture
def make_win(app, monkeypatch, tmp_path):
    made = []

    def make(backup: bool = True):
        from wiring import real_window
        cfg_dir = tmp_path / ("deep/" * 12) / "cfg"      # a long path: the dialog must still fit
        cfg_dir.mkdir(parents=True)
        for k, sub in (("MLASER_CONFIG_DIR", None), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
            monkeypatch.setenv(k, str(cfg_dir if sub is None else tmp_path / sub))
        monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
        for n in ("information", "warning", "critical", "question"):
            monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
        _damage(cfg_dir, backup)
        w = real_window(config=ConfigService())
        w.ctl.connect()
        made.append(w)
        return w
    yield make
    from wiring import dispose
    for w in made:
        dispose(w)


def test_arming_refused_until_answered_then_restore(make_win):
    w = make_win()
    assert w.config.load_blocked and not w.config.is_set("safety.door_input")
    assert "config" in w.ctl.arm_holds()
    assert not w.ctl.arm()
    # a swapped controller gets the hold too
    from mlaser.ui.facade import DemoController
    w.set_controller(DemoController())
    w.ctl.connect()
    assert not w.ctl.arm()
    assert N.show_if_needed(w, run=lambda d: N.RESTORE)
    assert not w.config.load_blocked and w.ctl.arm_holds() == {}
    assert w.config.get("safety.door_input") == 5
    assert w.machine.pulses_per_mm_x == 200.0            # the window's machine followed ("*")
    assert w.ctl.arm()
    w.ctl.disarm()


def test_continue_with_defaults_and_quit(make_win):
    w = make_win()
    assert not N.show_if_needed(w, run=lambda d: N.QUIT)
    assert w.config.load_blocked and not w.ctl.arm()
    assert N.show_if_needed(w, run=lambda d: N.DEFAULTS)
    assert not w.config.load_blocked and w.ctl.arm_holds() == {}
    assert w.config.get("safety.door_input") == 0
    w.config.set("shell.welcome_done", True)
    w.config.save()                                       # writes again


def test_dialog_buttons_default_and_size(make_win):
    w = make_win(backup=True)
    d = N.ConfigNoticeDialog(w)
    try:
        roles = {d.buttons.buttonRole(b) for b in d.buttons.buttons()}
        assert QDialogButtonBox.RejectRole in roles and QDialogButtonBox.AcceptRole in roles
        texts = [b.text() for b in d.buttons.buttons()]
        assert len(texts) == 3 and all(t and t != "OK" for t in texts)
        assert d.b_restore is not None and d.b_restore.isDefault() and not d.b_defaults.isDefault()
        d.reject()                                        # Escape / the close button
        assert d.choice == N.QUIT
        assert d.minimumSizeHint().width() <= 683         # half-screen floor
        assert w.config.load_error_evidence.name in d.details.text().replace("​", "")
    finally:
        d.deleteLater()


def test_dialog_without_backup_defaults_to_quit(make_win):
    w = make_win(backup=False)
    d = N.ConfigNoticeDialog(w)
    try:
        assert d.b_restore is None and d.b_quit.isDefault() and not d.b_defaults.isDefault()
        assert d.minimumSizeHint().width() <= 683
    finally:
        d.deleteLater()
