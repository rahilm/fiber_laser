"""One parameter import with a review (UX review 2026-11-09, finding 11).

"Import Mlaser parameters…" used to apply at once in Engineer mode (the travel limit went from
1000 to 3050 mm with one log line), Machine › Setup › Apply had no confirmation, no backup and no
reconnect prompt, and there was no compare.  Now every import shows the preview first (current
-> new, the values that make motion safe marked), asks with verb buttons (Cancel the default),
backs up the configuration with a timestamp, says that the card needs a reconnect (and offers
it), and a backup can be compared and restored.  Real MainWindow, offscreen, simulator."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/wiring.py


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _win(app, monkeypatch, tmp_path, mode):
    from PySide6.QtWidgets import QDialog
    from wiring import real_window

    from mlaser.controller.ui_adapter import UiController
    from mlaser.services.config import ConfigService
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    ctl = UiController(poll_s=0.02)
    assert ctl.connect_simulator(time_scale=1.0)
    cfg = ConfigService(tmp_path / "cfg" / "config.json", load=False)
    w = real_window(ctl, user_mode=mode, config=cfg)
    return w


@pytest.fixture
def win(app, monkeypatch, tmp_path):
    from wiring import dispose
    w = _win(app, monkeypatch, tmp_path, "engineer")
    yield w
    w.ctl.disconnect()
    dispose(w)


@pytest.fixture
def service_win(app, monkeypatch, tmp_path):
    from wiring import dispose
    w = _win(app, monkeypatch, tmp_path, "service")
    yield w
    w.ctl.disconnect()
    dispose(w)


def _dat(tmp_path, **values):
    from mlaser.services.config import ConfigService
    src = ConfigService(tmp_path / "src.json", load=False)
    for k, v in values.items():
        src.set(k.replace("__", "."), v)
    d = tmp_path / "dat"
    src.export_dat(d)
    return d


def _press(monkeypatch, yes):
    """QMessageBox.exec answers: Enter (the default button) or the verb."""
    from PySide6.QtWidgets import QMessageBox

    from mlaser.ui.verb_question import answer_with
    seen = []

    def ex(box, *a):
        seen.append({"title": box.windowTitle(), "default": box.defaultButton().text() if box.defaultButton() else None,
                     "buttons": [b.text() for b in box.buttons()], "detail": box.detailedText(),
                     "info": box.informativeText()})
        if yes:
            return answer_with(box, True)
        box.defaultButton().click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", ex)
    return seen


def test_engineer_import_previews_and_asks_before_the_travel_limit_changes(app, win, tmp_path, monkeypatch):
    w = win
    w.config.set("MAC.SoftLimitMaxLen", 1000.0)
    dat = _dat(tmp_path, MAC__SoftLimitMaxLen=3050.0)
    assert w.shell.allows("shell.import_params")             # Engineer: the same reviewing path
    d = w.shell.import_params(str(dat))
    try:
        assert w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(1000.0), "applied without a preview"
        ch = {c.key: c for c in d.plan.changes}
        assert "MAC.SoftLimitMaxLen" in ch and ch["MAC.SoftLimitMaxLen"].new == pytest.approx(3050.0)
        # the row is marked in words (and bold): never by colour alone
        rows = [d.tree.topLevelItem(i).child(j) for i in range(d.tree.topLevelItemCount())
                for j in range(d.tree.topLevelItem(i).childCount())]
        row = next(r for r in rows if r.text(0) == "MAC.SoftLimitMaxLen")
        assert "travel limit" in row.text(4) and row.font(0).bold()
        # Enter on the question: nothing changes
        seen = _press(monkeypatch, yes=False)
        assert d.apply() is None
        assert seen and seen[-1]["default"] == "Cancel" and "Apply changes" in seen[-1]["buttons"]
        assert "MAC.SoftLimitMaxLen" in seen[-1]["detail"] and "move" in seen[-1]["info"]
        assert w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(1000.0)
        # the verb: applied, backed up first, the reconnect offered (the card is connected)
        _press(monkeypatch, yes=True)
        res = d.apply()
        assert res is not None and w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(3050.0)
        assert res.config_backup is not None and res.config_backup.exists()
        assert d.b_reconnect.isVisibleTo(d) and "reconnect" in d.status.text().lower()
        # compare with that backup and restore it
        plan = d.compare(str(res.config_backup))
        bc = {c.key: c for c in plan.changes}
        assert plan.kind == "backup" and bc["MAC.SoftLimitMaxLen"].old == pytest.approx(3050.0)
        assert bc["MAC.SoftLimitMaxLen"].new == pytest.approx(1000.0)
        assert d.apply() is not None
        assert w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(1000.0)
    finally:
        d.close()


def test_setup_apply_asks_backs_up_and_offers_reconnect(app, service_win, monkeypatch):
    w = service_win
    from mlaser.services import settings_import as SI
    w.config.set("MAC.SoftLimitMaxLen", 1000.0)
    w.config.save()
    ws = w.extension("ext_machine").workspace
    sp = ws.settings_panel
    sp.set_editable(True)
    ed = sp.show_key("MAC.SoftLimitMaxLen")
    assert ed is not None
    ed.setValue(1500.0)
    assert "MAC.SoftLimitMaxLen" in sp.changed()
    n_bak = len(SI.list_backups(w.config))
    seen = _press(monkeypatch, yes=False)
    assert sp.apply() == 0
    assert seen[-1]["default"] == "Cancel" and "Maximum stroke" in seen[-1]["detail"]
    assert w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(1000.0)
    ed = sp.show_key("MAC.SoftLimitMaxLen")
    ed.setValue(1500.0)
    _press(monkeypatch, yes=True)
    assert sp.apply() == 1
    assert w.config.get("MAC.SoftLimitMaxLen") == pytest.approx(1500.0)
    assert len(SI.list_backups(w.config)) == n_bak + 1
    assert sp.b_reconnect.isVisibleTo(sp) and "reconnect" in sp.status.text().lower()


def test_safety_categories():
    from mlaser.services import settings_import as SI
    assert SI.safety_category("MAC.SoftLimitMaxLen") == "travel"
    assert SI.safety_category("MAC_1.WritePluse") == "scaling"
    assert SI.safety_category("AX.InterpolationCycle") == "scaling"
    assert SI.safety_category("MAC.AxisReverse") == "direction"
    assert SI.safety_category("MC.JogFastSpeed") == ""


def test_restore_from_file_asks_and_backs_up_first(app, service_win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from mlaser.formats import backup as B
    w = service_win
    from mlaser.formats.params import ParamStore
    ext = w.extension("ext_machine")
    src = tmp_path / "saved.xml"
    ext._backup().export_to_file(src)
    st = ParamStore.load(src)                     # one value differs: there is something to restore
    d_dbl = next(d for d in B.hard_table() if d.type == 2)
    st.set(d_dbl, (st.get(d_dbl) or 0.0) + 1.5)
    st.save(src)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    called = []
    monkeypatch.setattr(B.ParamBackup, "import_from_file", lambda self, path, layers=False: called.append(path) or {})
    seen = _press(monkeypatch, yes=False)
    d = ext.restore_file()                        # the review first (tests/test_param_restore_review.py)
    assert d.plan is not None and d.plan.changes
    assert d.apply() is None
    assert not called, "Enter restored the parameters"
    assert seen[-1]["default"] == "Cancel" and "Restore parameters" in seen[-1]["buttons"]
    assert "params-before-restore-" in seen[-1]["info"]
    _press(monkeypatch, yes=True)
    assert ext.restore_file().apply() is not None
    assert called == [str(src)]
    assert list(Path(ext.ops.file_dir).glob("params-before-restore-*.xml"))
