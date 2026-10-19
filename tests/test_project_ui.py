"""ext_project: .mlz open / save / export, sections, autosave and System Recovery (offscreen)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mlaser.formats.chf import load_chf  # noqa: E402
from mlaser.model import Contour, Craft, Document, Line, Part  # noqa: E402
from mlaser.project import load_project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _square(x=0.0):
    pts = [(x, 0.0), (x + 10, 0.0), (x + 10, 10.0), (x, 10.0)]
    return Part([Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)],
                Craft(layer=1, cool_points=[2.5], overcut=0.4))


def _window(tmp_path):
    from mlaser.services.config import ConfigService
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    cfg = ConfigService()
    cfg.set("app_dir", str(tmp_path))
    w = MainWindow(DemoController(), extensions=EXTENSIONS, config=cfg)
    w._test_no_dialogs = True
    return w


def test_save_open_and_export(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    w = _window(tmp_path)
    ext = w.extension("ext_project")
    assert all(c in w.commands for c in ("file.open_project", "file.save_project", "file.export_chf"))
    tech = {}
    ext.register_section("technology", lambda: {"1": {"material": "S235"}}, tech.update)
    w.set_document(Document([_square(), _square(20)]))
    path = tmp_path / "job.mlz"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "job"), "")))
    w.commands.run("file.save_project")
    assert path.exists() and w.docs.path == str(path)
    pr = load_project(path)
    assert pr.technology == {"1": {"material": "S235"}} and pr.document.parts[0].craft.cool_points == [2.5]
    w.set_document(Document())
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
    w.commands.run("file.open_project")
    assert len(w.document.parts) == 2 and w.document.parts[1].craft.overcut == 0.4
    assert tech == {"1": {"material": "S235"}}
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(tmp_path / "x.chf"), "")))
    w.commands.run("file.export_chf")
    assert len(load_chf(str(tmp_path / "x.chf")).parts) == 2
    # a damaged project is reported, the document stays
    (tmp_path / "bad.mlz").write_bytes(b"PK\3\4broken")
    msgs = []
    w.log_panel.log = msgs.append
    before = w.document
    assert ext.open(str(tmp_path / "bad.mlz")) is False
    assert w.document is before and any("damaged" in m for m in msgs)
    w.close()


def test_autosave_and_system_recovery_use_the_project(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    w = _window(tmp_path)
    ext = w.extension("ext_project")
    mach = w.extension("ext_machine")
    queue = []
    ext.register_section("queue", lambda: [{"name": "plate", "count": 2}], queue.extend)
    w.set_document(Document([_square()]))
    ext._dirty = True
    out = ext.autosave_now()
    assert [p.name for p in out] == ["autosave.mlz", "autosave.chf"]
    assert ext.autosave_now() is None                      # nothing changed since
    # the job-start autosave of the original writes both files too
    mach.autosave(Document([_square(), _square(30)]))
    assert len(load_project(tmp_path / "File" / "autosave.mlz").document.parts) == 2
    assert len(load_chf(str(tmp_path / "File" / "autosave.chf")).parts) == 2
    w.set_document(Document())
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.commands.run("app.recovery")
    qapp.processEvents()
    assert len(w.document.parts) == 2 and w.document.parts[0].craft.cool_points == [2.5]
    assert queue == [{"name": "plate", "count": 2}]
    w.close()
