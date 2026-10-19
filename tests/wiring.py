"""Test helper: build the main window with the application's real wiring."""

from __future__ import annotations


def real_window(controller=None, exec_dialogs: bool = False, prompts: bool = False, **kw):
    """The application's real wiring: MainWindow with every built-in plugin (EXTENSIONS).
    ``exec_dialogs=False`` makes the plugins apply their stored dialog parameters; without
    ``prompts`` the start-up questions (homing, recovery, job running) answer "no"."""
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    w = MainWindow(controller if controller is not None else DemoController(), extensions=EXTENSIONS, **kw)
    for ext in w.extensions.values():
        for e in ext if isinstance(ext, tuple) else (ext,):
            if hasattr(e, "exec_dialogs"):
                e.exec_dialogs = exec_dialogs
    if not prompts:
        w._test_no_dialogs = True
        w.control.confirm_fn = lambda key: False
    return w
