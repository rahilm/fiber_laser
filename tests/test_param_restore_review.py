"""The two "Restore parameters" commands show what changes before they apply (UX review
2026-11-09 finding 11, left over after the reviewing import).

"Restore parameters from the motion card…" and "… from a file…" open the parameter import's
review: every value now and restored, the values that change how the machine moves marked in
words and bold and listed first; Restore asks once more (Cancel the default, the motion values
under "Show details"), saves the current parameters first and stores exactly what was
reviewed.  Real MainWindow, offscreen, simulator."""
from __future__ import annotations

import pytest
from test_param_import_review import _press, app, service_win  # noqa: F401

from mlaser.formats import backup as B
from mlaser.formats.params import ParamStore, param_defs


def _soft_limit_def():
    return next(d for d in param_defs() if d.file == "HardPara.xml" and d.key.endswith("SoftLimitMaxLen"))


def _rows(d):
    return [d.tree.topLevelItem(i).child(j) for i in range(d.tree.topLevelItemCount())
            for j in range(d.tree.topLevelItem(i).childCount())]


def _travel_row(d):
    return next(r for r in _rows(d) if r.text(0).endswith("SoftLimitMaxLen"))


def test_restore_from_the_card_reviews_then_stores_what_was_reviewed(app, service_win, monkeypatch):  # noqa: F811
    w = service_win
    ext = w.extension("ext_machine")
    monkeypatch.setattr(ext, "_password", lambda: True)
    sl = _soft_limit_def()
    before = ext.params().hard.get(sl)
    card = ext._candidate()                       # the card holds a wider travel limit
    card.hard.set(sl, 3050.0)
    B.write_hard_to_card(ext.ops.mc, card.hard)
    seen = _press(monkeypatch, yes=False)
    d = ext.restore_hw()
    try:
        assert d is not None and "motion card" in d.windowTitle()
        assert ext.params().hard.get(sl) == pytest.approx(before), "applied without a review"
        row = _travel_row(d)
        assert row.text(2) != row.text(3) and "3050" in row.text(3).replace(",", "")
        assert "travel limit" in row.text(4) and row.font(0).bold()            # words and bold
        assert _rows(d)[0] is row                                                # motion values first
        assert not d.b_compare.isVisible() and not d.b_preview.isVisible()
        # Enter on the question: nothing restored
        assert d.apply() is None
        q = seen[-1]
        assert q["default"] == "Cancel" and "Restore parameters" in q["buttons"]
        assert "SoftLimitMaxLen" in q["detail"] and "params-before-restore-" in q["info"]
        assert ext.params().hard.get(sl) == pytest.approx(before)
        # the verb: the reviewed values are stored, the card is not read again
        reads = []
        real = B.read_hard_from_card
        monkeypatch.setattr(B, "read_hard_from_card", lambda *a, **k: reads.append(1) or real(*a, **k))
        _press(monkeypatch, yes=True)
        assert d.apply() is not None
        assert reads == []
        assert ext.params().hard.get(sl) == pytest.approx(3050.0)
        assert "params-before-restore-" in d.status.text()
    finally:
        d.close()


def test_restore_from_a_file_lists_the_differences(app, service_win, monkeypatch, tmp_path):  # noqa: F811
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    w = service_win
    ext = w.extension("ext_machine")
    sl = _soft_limit_def()
    src = tmp_path / "saved.xml"
    ext._backup().export_to_file(src)
    st = ParamStore.load(src)
    st.set(sl, 2222.0)
    st.save(src)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    _press(monkeypatch, yes=False)
    d = ext.restore_file()
    try:
        assert d is not None and "saved.xml" in d.layout().itemAt(0).widget().text()
        changed = [c.key for c in d.plan.changes]
        assert len(changed) == 1 and changed[0].endswith("SoftLimitMaxLen"), changed
        _press(monkeypatch, yes=True)
        assert d.apply() is not None
        assert ext.params().hard.get(sl) == pytest.approx(2222.0)
    finally:
        d.close()


def test_nothing_to_restore_says_so(app, service_win, monkeypatch, tmp_path):  # noqa: F811
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    ext = service_win.extension("ext_machine")
    src = tmp_path / "same.xml"
    ext._backup().export_to_file(src)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(src), "")))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    seen = _press(monkeypatch, yes=True)
    d = ext.restore_file()
    try:
        assert d.plan.changes == [] and not d.b_apply.isEnabled()
        assert d.status.text() == "No machine parameter differs."
        assert d.apply() is None and seen == []
    finally:
        d.close()
