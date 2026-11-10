"""Help › Save diagnostic bundle…, Check for updates, the About version line, and the bundle
offered from the log viewer and the fault dialog (review A8, F11, UX 14)."""
from __future__ import annotations

import time
import zipfile

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from mlaser.ui.shell import menu_spec as M

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(app, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("MLASER_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    mp.setenv("MLASER_LOG_DIR", str(tmp_path_factory.mktemp("log")))
    mp.setattr(QDialog, "exec", lambda self, *a: 0)
    from wiring import dispose, real_window
    w = real_window()
    yield w
    dispose(w)
    mp.undo()


def _ext(w, name):
    e = w.extension(name)
    return e[0] if isinstance(e, tuple) else e


@pytest.mark.parametrize("plat", [M.MACOS, M.WINDOWS, M.LINUX])
def test_help_menu_has_bundle_and_update_check(win, plat):
    menus = M.layout(list(win.commands.handlers()), plat)
    where = M.placement(menus)
    assert where["diag.bundle"] == "help" and where["app.check_updates"] == "help"


def test_commands_are_open_to_every_user_mode(win):
    from mlaser.services.usermodes import OPERATOR, default_min_mode
    assert default_min_mode("diag.bundle") == OPERATOR and default_min_mode("app.check_updates") == OPERATOR
    assert "diag.bundle" in win.commands and "app.check_updates" in win.commands


def test_dialog_lists_contents_and_drawing_is_off_by_default(win):
    from mlaser.ui.diag_bundle import BundleDialog
    d = BundleDialog(win)
    try:
        assert not d.include_drawing.isChecked()
        text = d.list.text()
        assert "Settings" in text and "Machine parameters" in text and "No drawing geometry" in text
        n = len(d.items())
        if d.include_drawing.isEnabled():
            d.include_drawing.setChecked(True)
            assert len(d.items()) == n + 1 and "The current drawing" in d.list.text()
    finally:
        d.deleteLater()


def test_save_runs_off_the_gui_thread_and_writes_the_zip(win, tmp_path):
    from mlaser.ui.diag_bundle import BundleDialog
    d = BundleDialog(win)
    out = tmp_path / "bundle.zip"
    got = []
    t0 = time.perf_counter()
    assert d.save_to(str(out), on_done=got.append)
    started = time.perf_counter() - t0
    assert d._call.busy or got                     # the write happens in the worker
    assert d.wait(30)
    try:
        assert got == [str(out)] and out.is_file()
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            system = zf.read("system.json").decode()
        assert "controller.json" in names and not any(n.startswith("drawing/") for n in names)
        assert '"screens"' in system and '"dpr"' in system
        assert started < 2.0
    finally:
        d.deleteLater()


def test_drawing_goes_in_only_when_ticked(win, tmp_path):
    from mlaser.cad.primitives import rect
    from mlaser.model import Document, Part
    from mlaser.ui.diag_bundle import BundleDialog
    win.docs.adopt(Document(parts=[Part(contours=[rect((0, 0), (10, 10))])]), None)
    d = BundleDialog(win)
    try:
        assert d.include_drawing.isEnabled() and not d.include_drawing.isChecked()
        d.include_drawing.setChecked(True)
        assert d.save_to(str(tmp_path / "with.zip"), on_done=lambda p: None)
        assert d.wait(30)
        with zipfile.ZipFile(tmp_path / "with.zip") as zf:
            assert [n for n in zf.namelist() if n.startswith("drawing/")]
    finally:
        d.deleteLater()


def test_about_shows_version_and_commit(win):
    from mlaser._buildid import build_identity
    from mlaser.ui import dialogs
    a = dialogs.AboutDialog(win.tr_, win)
    try:
        from PySide6.QtWidgets import QLabel
        lab = a.findChild(QLabel, "about_version")
        ident = build_identity()
        assert lab is not None and ident.version in lab.text()
        assert (ident.commit in lab.text()) or ident.commit == "unknown"
    finally:
        a.deleteLater()


def test_log_viewer_and_fault_dialog_offer_the_bundle(win):
    from mlaser.ui import ext_card, ext_diag
    lv = ext_diag.LogViewer(_ext(win, "ext_diag"))
    fault = ext_card.AlarmListDialog(win, [], fault="link lost")
    plain = ext_card.AlarmListDialog(win, [])
    try:
        assert lv.findChild(QPushButton, "diag_bundle_button") is not None
        assert fault.findChild(QPushButton, "diag_bundle_button") is not None
        assert plain.findChild(QPushButton, "diag_bundle_button") is None
    finally:
        for x in (lv, fault, plain):
            x.deleteLater()


def test_crash_recovery_prompt_offers_the_bundle(win, monkeypatch):
    from mlaser.ui import diag_bundle, safety_ui
    seen, opened = [], []

    def fake_exec(box):
        b = [x for x in box.buttons() if x.text() == win.tr_.tr("cmd.diag.bundle")]
        seen.append(bool(b))
        if len(seen) == 1:              # first: the operator saves a bundle; then: Not now
            b[0].click()
        else:
            [x for x in box.buttons() if box.buttonRole(x) == QMessageBox.RejectRole][0].click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(diag_bundle, "open_bundle_dialog", lambda w, parent=None: opened.append(w))
    called = []
    safety_ui.recovery_prompt(win, lambda: called.append(1))
    assert seen == [True, True] and opened == [win] and called == []


def test_update_check_is_off_by_default(win):
    assert win.config.get("plugin.ext_diag.update_check") is False


def _boxes():
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.objectName() == "update_notice"]


def test_update_notice_only_for_a_newer_release(win, app, monkeypatch):
    from mlaser.services import update_check as U
    from mlaser.ui.diag_bundle import check_updates
    for b in _boxes():
        b.close()
    rel = lambda tag: [{"tag_name": tag, "html_url": f"{U.RELEASES_PAGE}/tag/{tag}"}]   # noqa: E731
    monkeypatch.setattr("mlaser._buildid.build_identity", lambda: type("I", (), {"version": "0.1.0"})())
    got = []
    check_updates(win, quiet=True, fetch=lambda: rel("v0.1.0"), on_result=got.append).wait(10)
    app.processEvents()
    assert got and not got[0].newer and not _boxes()
    got.clear()
    call = check_updates(win, quiet=True, fetch=lambda: rel("v0.9.0"), on_result=got.append)
    call.wait(10)
    app.processEvents()
    assert got[0].newer and got[0].latest == "0.9.0"
    boxes = _boxes()
    assert boxes and "0.9.0" in boxes[0].text()
    for b in boxes:
        b.close()
    # the check at start never puts a dialog over a running job
    monkeypatch.setattr(win.ctl, "job_active", lambda: True, raising=False)
    check_updates(win, quiet=True, fetch=lambda: rel("v0.9.0")).wait(10)
    app.processEvents()
    assert not _boxes()
