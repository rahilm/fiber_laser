"""The one jog pad (mlaser/ui/jogpad.py) and the one run-button set (style.run_button): the Run
Control dock, the Machine workspace and the wizards show the same widget, look and rule; the run
bar and the Run screen the same run controls (UI audit I-1, I-2, I-3; DESIGN.md §2, §7)."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QToolButton

from mlaser.ui import style
from mlaser.ui.jogpad import CELL, JogPad
from wiring import dispose, real_window


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    from PySide6.QtWidgets import QDialog, QMessageBox
    QApplication.instance() or QApplication([])
    mp = pytest.MonkeyPatch()
    mp.setenv("MLASER_DATA_DIR", str(tmp_path_factory.mktemp("jogpad") / "data"))
    mp.setattr(QDialog, "exec", lambda self, *a: 0)
    mp.setattr(QMessageBox, "exec", lambda self, *a: QMessageBox.Ok)
    w = real_window()
    w.resize(1512, 875)
    w.show()
    yield w
    dispose(w)
    mp.undo()


def _pump(n: int = 5) -> None:
    for _ in range(n):
        QApplication.processEvents()


def _cells(pad: JogPad) -> list[QToolButton]:
    return [b for b in pad.findChildren(QToolButton) if b.objectName().startswith("jog_")]


def test_every_jog_pad_is_the_one_widget(win):
    dock = win.control.jog
    machine = win.extension("ext_machine").workspace.jog
    assert type(dock) is JogPad and type(machine) is JogPad
    for pad in (dock, machine):
        assert set(pad.buttons) == {"X+", "X-", "Y+", "Y-", "Z+", "Z-"}
        for b in _cells(pad):
            # every cell the same square touch target, an icon with its label, a tooltip and an
            # accessible name
            assert b.minimumWidth() == b.minimumHeight() >= CELL
            assert not b.icon().isNull() and b.text() and b.toolTip() and b.accessibleName()
        assert [b.text() for b in pad.step_buttons[1:]] == [b.text() for b in machine.step_buttons[1:]]
        assert all(b.minimumHeight() >= style.TARGET_TOUCH for b in pad.step_buttons)


def test_home_is_homing_and_the_centre_is_go_to_origin(win):
    """Homing (the reference switches) is the Home row, never the pad's centre; the centre is
    Go to origin (the program zero), shown where the page has no other Go-to-origin control."""
    dock = win.control.jog
    assert dock.centre is not None and "origin" in dock.centre.toolTip().lower()
    assert "home" not in dock.centre.toolTip().lower()
    assert set(dock.home_buttons) == {"mp29_1", "mp30"}
    homed = []
    win.control.confirm_fn = lambda key: True
    orig = win.control.ctl.home
    win.control.ctl.home = lambda axes: homed.append(axes)
    try:
        dock.home_buttons["mp29_1"].click()
        dock.home_buttons["mp30"].click()
    finally:
        win.control.ctl.home = orig
    assert homed == ["XY", "Z"]
    machine = win.extension("ext_machine").workspace.jog
    assert machine.centre is None and machine.hint is not None and not machine.home_buttons


def test_one_step_choice_for_every_pad(win):
    dock = win.control.jog
    machine = win.extension("ext_machine").workspace.jog
    machine.step_buttons[3].click()
    _pump()
    assert dock.step_mm() == machine.step_mm() > 0
    dock.step_buttons[0].click()
    _pump()
    assert machine.step_mm() == 0.0


def test_run_bar_and_run_screen_use_one_factory(win):
    bar = win.safety_ui.run_bar.buttons
    win.shell.show_workspace("run")
    _pump()
    from PySide6.QtWidgets import QWidget
    screen = win.findChild(QWidget, "run_workspace").buttons
    for kind in style.RUN_KINDS:
        assert bar[kind].property("run_kind") == screen[kind].property("run_kind") == kind
        assert bar[kind].toolTip().split("\n")[0] == screen[kind].toolTip().split("\n")[0]
        assert not bar[kind].icon().isNull() and not screen[kind].icon().isNull()
    # the glossary's words: Arm laser, Hold (not Pause), Dry run (not Dry cut)
    assert bar["arm"].text() == screen["arm"].text()
    assert bar["dry_run"].text() == screen["dry_run"].text()
    win.shell.show_workspace("design")


def test_disabled_run_controls_say_why(win):
    """Offline: Start, Hold, Frame, Dry run and Arm are off and their tooltips say why; STOP is
    always on."""
    _pump()
    bar = win.safety_ui.run_bar.buttons
    assert bar["stop"].isEnabled()
    for k in ("start", "hold", "frame", "dry_run", "arm"):
        b = bar[k]
        if not b.isEnabled():
            assert b.property("disabled_reason") and b.property("disabled_reason") in b.toolTip()


def test_procedure_cards_open_their_wizard_without_the_jobs_word(win):
    ws = win.extension("ext_machine").workspace
    for cmd, b in ws.proc_buttons.items():
        assert b.icon().isNull()                      # the icon is the card title's
        assert b.text().strip().lower() != "start"
        if not b.isEnabled():
            assert b.toolTip()                        # says which user mode it needs
