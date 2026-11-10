"""One damaged data file no longer removes a workspace (architecture review A9): the alarm
history and the technology database load what they can, keep the file as ``.bad``, set
``load_error``, and the workspace says so; a built-in plugin that fails anyway is reported in
the status bar, not only in one log line."""
from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from mlaser.services.alarm_history import AlarmHistory, AlarmRecord
from mlaser.services.technology import TechnologyStore

GOOD = json.dumps({"kind": "alarm", "text": "x", "raised": 1.0, "code": 1, "cleared": None,
                   "alarm_kind": ""}).encode()


def test_alarm_history_skips_damaged_lines(tmp_path):
    p = tmp_path / "alarm_history.jsonl"
    raw = GOOD + b"\n\xff\xfe\n[1,2]\n\"x\"\n" + GOOD.replace(b'"code": 1', b'"code": 2') + b"\n"
    p.write_bytes(raw)
    h = AlarmHistory(p)                       # a UnicodeDecodeError used to escape from here
    assert [r.code for r in h.entries(newest_first=False)] == [1, 2]
    assert "3" in h.load_error
    assert (tmp_path / "alarm_history.jsonl.bad").read_bytes() == raw
    h.add(AlarmRecord("alarm", "y", 2.0, 3))
    h.flush()
    assert len(p.read_text("utf-8").splitlines()) == 3


def test_alarm_history_clean_file_has_no_error(tmp_path):
    p = tmp_path / "alarm_history.jsonl"
    p.write_bytes(GOOD + b"\n")
    h = AlarmHistory(p)
    assert not h.load_error and not (tmp_path / "alarm_history.jsonl.bad").exists()


@pytest.mark.parametrize("data", [b"SQLite format 3\x00" + b"\x01" * 3000, b"not a database at all" * 100],
                         ids=["sqlite_header", "garbage"])
def test_technology_store_moves_a_damaged_database_aside(tmp_path, data):
    p = tmp_path / "default.sqlite3"
    p.write_bytes(data)
    s = TechnologyStore(p)                    # sqlite3.DatabaseError used to escape from here
    try:
        assert s.load_error and (tmp_path / "default.sqlite3.bad").read_bytes() == data
        assert s._meta("examples_seeded") is not None     # a working, fresh store
    finally:
        s.close()
    s2 = TechnologyStore(p)
    try:
        assert not s2.load_error
    finally:
        s2.close()


# ---------------------------------------------------------------- the window
@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app, monkeypatch, tmp_path):
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_APP_DIR", "app"), ("MLASER_DATA_DIR", "data")):
        (tmp_path / sub).mkdir()
        monkeypatch.setenv(k, str(tmp_path / sub))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))
    made = []
    yield tmp_path, made
    from wiring import dispose
    for w in made:
        dispose(w)


def test_window_keeps_machine_and_process_with_damaged_files(env, monkeypatch):
    tmp, made = env
    data = tmp / "data"
    (data / "alarm_history.jsonl").write_bytes(GOOD + b"\n\xff\xfe\n")
    (data / "technology").mkdir()
    (data / "technology" / "default.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\x01" * 3000)
    seen: list[str] = []
    from mlaser.ui import panels
    orig = panels.LogPanel.log
    monkeypatch.setattr(panels.LogPanel, "log", lambda self, msg, *a, **k: (seen.append(str(msg)),
                                                                            orig(self, msg, *a, **k))[1])
    from wiring import real_window
    w = real_window()
    made.append(w)
    names = {k.rsplit(".", 1)[-1] for k in w.extensions}
    assert {"ext_machine", "ext_process"} <= names and not w.extension_failures
    assert any("alarm_history.jsonl.bad" in s for s in seen)
    assert any("default.sqlite3.bad" in s for s in seen)


def test_failed_builtin_is_said_in_the_status_bar(env, monkeypatch):
    _tmp, made = env
    from mlaser.ui import ext_card

    def boom(ctx):
        raise RuntimeError("broken")
    monkeypatch.setattr(ext_card, "register", boom)
    from wiring import real_window
    w = real_window()
    made.append(w)
    assert "ext_card" in w.extension_failures
    assert "ext_card" in w.statusBar().currentMessage()
