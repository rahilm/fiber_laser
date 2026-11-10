"""Crash recovery (deep review 2026-11, UX finding 6): the first crash of a new profile is
detected, the prompt says exactly what Recover restores, and the resume point of a held job
comes back when it belongs to the recovered document (as a kept point: nothing moves; on a real
card only after homing; the resume goes through the normal flow)."""
from __future__ import annotations

import json
import os
import shutil
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from safety_helpers import rect_doc  # noqa: E402


def _until(app, pred, timeout=10.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_dialogs(monkeypatch, tmp_path):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    for n in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, n, staticmethod(lambda *a, **k: QMessageBox.No))


def _window(app, sim: bool = False):
    from wiring import real_window
    ctl = None
    if sim:
        from mlaser.controller.ui_adapter import UiController
        ctl = UiController(poll_s=0.02)
    w = real_window(ctl)
    w.resize(1280, 720)
    w.show()
    if sim:
        assert ctl.connect_simulator(time_scale=1.0)
        assert _until(app, lambda: ctl.state().connected)
    app.processEvents()
    return w


def _crash(w) -> None:
    """The process ends without the clean exit (kill -9, power loss): the window is closed and
    its clean-exit marker is put back to what the session left on disk (NormalExit=0)."""
    from wiring import dispose
    fdir = w.extension("ext_machine").ops.file_dir
    try:
        w.ctl.disconnect()
    except Exception:  # noqa: BLE001
        pass
    dispose(w)
    ini = fdir / "softPara.ini"
    if ini.exists():
        ini.write_text(ini.read_text(errors="replace").replace("NormalExit=1", "NormalExit=0"))


def _ini(app_dir) -> str:
    p = os.path.join(app_dir, "File", "softPara.ini")
    return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""


# --------------------------------------------------------------------------- detection
def test_first_crash_on_a_new_profile_is_detected(app):
    app_dir = os.environ["MLASER_APP_DIR"]
    assert not os.path.exists(os.path.join(app_dir, "File"))       # a brand-new profile
    w = _window(app)
    ext = w.extension("ext_machine")
    assert ext.normal_exit                                          # nothing to recover yet
    assert "NormalExit=0" in _ini(app_dir).replace(" ", "")         # the marker from the first session on
    ext.autosave_store().save(rect_doc(w=100, h=80))                # a job was prepared
    _crash(w)
    w2 = _window(app)
    try:
        ext2 = w2.extension("ext_machine")
        assert not ext2.normal_exit                                 # detected: the prompt is offered
        assert "ended abnormally" in w2.log_panel.text.toPlainText()
    finally:
        _crash(w2)


def test_autosave_without_any_marker_counts_as_interrupted(app):
    """A profile whose marker was never written (the gap above, from an older version)."""
    from mlaser.project import Autosave
    app_dir = os.environ["MLASER_APP_DIR"]
    Autosave(os.path.join(app_dir, "File")).save(rect_doc(w=100, h=80))
    w = _window(app)
    try:
        assert not w.extension("ext_machine").normal_exit
    finally:
        _crash(w)


def test_clean_exit_is_not_a_crash(app):
    from wiring import dispose
    w = _window(app)
    w.extension("ext_machine").autosave_store().save(rect_doc(w=100, h=80))
    dispose(w)                                                      # closeEvent: the clean exit
    w2 = _window(app)
    try:
        assert w2.extension("ext_machine").normal_exit
    finally:
        dispose(w2)


# --------------------------------------------------------------------------- the prompt
def test_prompt_after_a_crash_mid_cut_says_the_position_is_not_restored(app):
    """A running job keeps no resume point: the document only, said plainly."""
    w = _window(app)
    ext = w.extension("ext_machine")
    ext.autosave_store().save(rect_doc(w=100, h=80))
    _crash(w)
    w2 = _window(app)
    try:
        ext2 = w2.extension("ext_machine")
        text = ext2.recovery_detail()
        assert "1 part" in text and "not restored" in text and "already be cut" in text
        assert "no job was held" in text
        ext2.recovery(confirm=False)
        assert len(w2.document.parts) == 1
        assert w2.ctl.kept_breakpoint is None and w2.ctl.breakpoint is None
        assert "not restored" in w2.log_panel.text.toPlainText()
    finally:
        _crash(w2)


def test_recovery_prompt_text_promises_no_breakpoint(app):
    from mlaser.ui import safety_ui
    seen = []

    class Box(QMessageBox):
        def exec(self):
            seen.append((self.text(), self.informativeText()))
            return 0
    w = _window(app)
    try:
        orig = safety_ui.QMessageBox
        safety_ui.QMessageBox = Box
        try:
            safety_ui.recovery_prompt(w, lambda: None, "DETAIL")
        finally:
            safety_ui.QMessageBox = orig
        assert seen and "breakpoint" not in seen[0][0] and seen[0][1] == "DETAIL"
    finally:
        _crash(w)


# --------------------------------------------------------------------------- the resume point
def _held_job(app, w):
    """A cut held at a breakpoint (simulator); returns the breakpoint."""
    ctl, su = w.ctl, w.safety_ui
    w.set_document(rect_doc(w=400, h=300))
    assert ctl.arm()
    su.start()
    assert _until(app, lambda: ctl.state().mode == "process", 20)
    t = time.monotonic() + 0.5
    while time.monotonic() < t:
        app.processEvents()
        time.sleep(0.01)
    su.hold()
    assert _until(app, lambda: ctl.breakpoint is not None, 10)
    return ctl.breakpoint


def test_held_job_resume_point_is_recovered_on_the_same_document(app, monkeypatch):
    from mlaser.machine import events as EV
    from mlaser.nc.resume import document_key
    app_dir = os.environ["MLASER_APP_DIR"]
    fdir = os.path.join(app_dir, "File")
    w = _window(app, sim=True)
    bp = _held_job(app, w)
    rp = os.path.join(fdir, "resume_point.json")
    assert _until(app, lambda: os.path.exists(rp), 5)               # written while held
    rec = json.load(open(rp))
    assert rec["doc_key"] == bp.doc_key and abs(rec["x"] - bp.x) < 1e-6
    keep = os.path.join(app_dir, "..", os.path.basename(app_dir) + "_keep")
    os.makedirs(keep, exist_ok=True)
    for n in ("resume_point.json", "autosave.mlz", "autosave.chf"):
        shutil.copy(os.path.join(fdir, n), keep)
    w.ctl.stop(wait=True)
    _crash(w)
    for n in ("resume_point.json", "autosave.mlz", "autosave.chf"):  # the files as the power cut left them
        shutil.copy(os.path.join(keep, n), fdir)
    with open(os.path.join(fdir, "softPara.ini"), "w") as f:
        f.write("[SC2000]\nNormalExit=0\n")

    w2 = _window(app, sim=True)
    try:
        ext2, ctl2, su2 = w2.extension("ext_machine"), w2.ctl, w2.safety_ui
        assert not ext2.normal_exit
        text = ext2.recovery_detail()
        assert f"X {bp.x:.1f}, Y {bp.y:.1f}" in text and "Nothing moves" in text and "home it first" in text
        ext2.recovery(confirm=False)
        kept = ctl2.kept_breakpoint
        assert kept is not None and ctl2.breakpoint is None          # offered, not held: nothing moves
        assert ctl2.state().mode == "ready"
        assert abs(kept.x - bp.x) < 1e-6 and abs(kept.y - bp.y) < 1e-6
        assert kept.doc_key == bp.doc_key == document_key(w2.document)
        su2.refresh()
        assert not su2.kept_bar.isHidden() and "Recovered resume point" in su2.kept.label.text()

        # a real card: not before homing in this session (the position may have been lost)
        monkeypatch.setattr(ctl2, "is_simulator", lambda: False)
        su2.refresh()
        assert "home the machine" in su2.kept.label.text()
        su2.resume_kept()
        assert ctl2.breakpoint is None and ctl2.kept_breakpoint is kept
        assert "home the machine first" in w2.statusBar().currentMessage()
        ctl2.events.publish(EV.HomingDone(False))                 # a failed homing does not count
        assert _until(app, lambda: True, 0.1) and kept.home_first
        su2.resume_kept()
        assert ctl2.breakpoint is None
        ctl2.events.publish(EV.HomingDone(True))
        assert _until(app, lambda: not kept.home_first, 2)
        su2.resume_kept()
        assert ctl2.breakpoint is kept and ctl2.kept_breakpoint is None

        # the resume goes through the normal flow: disarmed, the controller refuses it
        assert not ctl2.safety().armed
        monkeypatch.setattr(ctl2, "is_simulator", lambda: True)
        su2.start()                                               # Start = Continue
        _until(app, lambda: False, 0.5)
        assert ctl2.state().mode != "process" and ctl2.breakpoint is kept
        assert ctl2.last_preflight is not None and "not_armed" in ctl2.last_preflight.codes()
        # armed (the operator's explicit arm), it resumes from the recovered point and finishes
        assert ctl2.arm()
        su2.start()
        assert _until(app, lambda: ctl2.state().mode == "process", 20)
        assert ctl2.breakpoint is None
        assert _until(app, lambda: ctl2.state().mode == "ready", 60)
    finally:
        try:
            w2.ctl.stop(wait=True)
        except Exception:  # noqa: BLE001
            pass
        _crash(w2)


def test_resume_point_of_another_document_is_not_restored(app):
    from mlaser.nc.resume import Breakpoint
    from mlaser.nc import compile_job
    from mlaser.services import resume_point as RP
    app_dir = os.environ["MLASER_APP_DIR"]
    fdir = os.path.join(app_dir, "File")
    w = _window(app)
    ext = w.extension("ext_machine")
    a, b = rect_doc(w=100, h=80), rect_doc(w=120, h=80)
    job = compile_job(a)
    from mlaser.nc.resume import document_key, position_mm
    x, y = position_mm(job, 5)
    RP.save(fdir, Breakpoint(job, 5, x, y, doc_key=document_key(a)))
    ext.autosave_store().save(b)
    _crash(w)
    w2 = _window(app)
    try:
        ext2 = w2.extension("ext_machine")
        text = ext2.recovery_detail()
        assert "not restored" in text and "another drawing" in text
        ext2.recovery(confirm=False)
        assert w2.ctl.kept_breakpoint is None and w2.ctl.breakpoint is None
    finally:
        _crash(w2)


def test_rebuild_refuses_a_point_that_lands_elsewhere():
    from mlaser.nc import compile_job
    from mlaser.nc.resume import Breakpoint, document_key, position_mm
    from mlaser.services import resume_point as RP
    doc = rect_doc(w=100, h=80)
    job = compile_job(doc)
    idx = len(job.wire) // 2
    x, y = position_mm(job, idx)
    rec = RP.record(Breakpoint(job, idx, x, y, doc_key=document_key(doc)))

    class Ctl:
        cam_params = None
        machine = job.machine
        layers = None
        laser_enabled = True

        @staticmethod
        def place(d):
            return d
    bp, why = RP.rebuild(rec, doc, Ctl())
    assert why == "" and bp.index == idx and bp.home_first and bp.recovered
    rec["x"] += 0.5
    assert RP.rebuild(rec, doc, Ctl()) == (None, RP.REBUILD)
    rec["x"] -= 0.5
    assert RP.rebuild(rec, rect_doc(w=101, h=80), Ctl()) == (None, RP.OTHER_DOCUMENT)
    del Ctl.place
    assert RP.rebuild(rec, doc, Ctl()) == (None, RP.CONTROLLER)
