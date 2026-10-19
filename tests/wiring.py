"""Test helper: build the main window with the application's real wiring."""

from __future__ import annotations


def real_window(controller=None, exec_dialogs: bool = False, prompts: bool = False,
                user_mode: str = "service", **kw):
    """The application's real wiring: MainWindow with every built-in plugin (EXTENSIONS).
    ``exec_dialogs=False`` makes the plugins apply their stored dialog parameters; without
    ``prompts`` the start-up questions (homing, recovery, job running) answer "no".
    ``user_mode``: the shell's user mode (mlaser.services.usermodes); Service by default so a
    test can trigger every command (the application itself starts in Engineer mode)."""
    from mlaser.ui.facade import DemoController
    from mlaser.ui.mainwindow import EXTENSIONS, MainWindow

    w = MainWindow(controller if controller is not None else DemoController(), extensions=EXTENSIONS, **kw)
    for ext in w.extensions.values():
        for e in ext if isinstance(ext, tuple) else (ext,):
            if hasattr(e, "exec_dialogs"):
                e.exec_dialogs = exec_dialogs
    if user_mode and w.user_mode != user_mode:
        assert w.set_user_mode(user_mode), f"user mode {user_mode} refused"
    if not prompts:
        w._test_no_dialogs = True
        w.control.confirm_fn = lambda key: False
    return w
