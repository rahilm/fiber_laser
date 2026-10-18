"""mlaser.ui.ext_card: commands and alarm forwarding."""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest  # noqa: E402

pytest.importorskip("PySide6")


def test_ext_card_register_and_alarm_flow():
    from PySide6.QtWidgets import QApplication
    from wiring import real_window
    app = QApplication.instance() or QApplication([])
    w = real_window()
    try:
        ext = w.extension("ext_card")
        for cmd in ("card.alarms", "card.clear_alarms", "card.break_resume", "card.params",
                    "card.stream_process", "machine.home"):
            assert cmd in w.ribbon.actions
        assert w.connect_simulator(time_scale=4.0)
        w.run("card.params")
        assert "pulses/mm" in w.log_panel.text.toPlainText()
        w.ctl.sim.set_estop(True)
        end = time.monotonic() + 3
        while time.monotonic() < end and 8030 not in ext._last:
            app.processEvents()
            time.sleep(0.02)
        assert 8030 in ext._last and w.log_panel.alarms.rowCount() >= 1
        w.ctl.sim.set_estop(False)
        w.run("card.clear_alarms")
        assert w.ctl.sim.state.commands[-1] == [9999, 5, 0, 0]
        w.ribbon.actions["card.stream_process"].setChecked(True)
        assert w.ctl.stream_process is True
    finally:
        w.close()
