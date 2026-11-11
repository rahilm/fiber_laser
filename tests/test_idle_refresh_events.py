"""A periodic refresh that changes nothing sends no tooltip events.

The Machine page's tick and the machine panel's safety refresh run on every poll (20 per
second with the simulator).  They set the same tooltips again each time, and every
``setToolTip`` sends a ToolTipChange event: an idle Machine page held the GUI thread at about
29 % on Windows CI (tests/test_responsive_layout.py::test_idle_gui_thread_stays_idle).  Only a
change may set them."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")


def test_repeated_refresh_sends_no_tooltip_events():
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QApplication
    from wiring import real_window

    from mlaser.ui.workspaces.machine import MachineWorkspace
    app = QApplication.instance() or QApplication([])
    w = real_window()
    try:
        w.show()
        assert w.show_workspace("machine")
        page = w.findChild(MachineWorkspace)
        assert page is not None and page.isVisible()
        ctl = w.ctl
        st = ctl.state()
        ms = ctl.machine_status() if hasattr(ctl, "machine_status") else None
        page.tick()                               # the first refresh may set them
        w.control.safety_refresh(st, ms)
        app.processEvents()

        class Count(QObject):
            def __init__(self):
                super().__init__()
                self.hits: list[str] = []

            def eventFilter(self, o, e):  # noqa: N802 - Qt API
                if e.type() in (QEvent.ToolTipChange, QEvent.EnabledChange):
                    self.hits.append(f"{type(o).__name__}:{o.objectName() or getattr(o, 'text', str)()}")
                return False
        c = Count()
        app.installEventFilter(c)
        try:
            for _ in range(5):
                page.tick()
                w.control.safety_refresh(st, ms)
        finally:
            app.removeEventFilter(c)
        assert not c.hits, c.hits
    finally:
        w.close()
