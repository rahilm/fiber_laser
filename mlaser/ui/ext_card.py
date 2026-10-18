"""Card-control UI: alarms, Clear Alarm, breakpoint resume, card parameters, streaming mode.

``register(ctx)`` (a :class:`mlaser.services.plugins.PluginContext`) binds these commands and
re-attaches to the controller's signals on every controller change (``ctx.on_controller_changed``)
when it is a :class:`mlaser.controller.ui_adapter.UiController`:

* ``card.alarms``      (mf123 "Alarm")          dialog of the active card alarms
* ``card.clear_alarms`` (gp90 "Clear Alarm")    ``[9999, 5, 0, 0]``
* ``card.break_resume`` (mp64 "BreakCon")       continue from the newest AutosaveParam record
* ``card.params``      (gp98 "Settings")        scaling/period read from the card
* ``card.stream_process`` (checkable)           stream in a separate process (GIL-proof)

The ``machine.home*`` commands are the main window's (this module used to bind them a second
time with the same behaviour).

New alarms are appended to the log dock's alarm tab (``LogPanel.add_alarm``).
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)


def _tr(window, key: str, default: str) -> str:
    tr = getattr(window, "tr_", None)
    if tr is None:
        return default
    try:
        s = tr.tr(key)
    except Exception:  # noqa: BLE001
        return default
    return s if s and s != key else default


def _is_card(ctl) -> bool:
    return hasattr(ctl, "active_alarms") and hasattr(ctl, "breakpoint")


class AlarmListDialog(QDialog):
    """Active card alarms (code, text) with a Clear Alarm button."""

    def __init__(self, window, alarms: list):
        super().__init__(window)
        self.setWindowTitle(_tr(window, "mf123", "Alarm"))
        lay = QVBoxLayout(self)
        t = QTableWidget(len(alarms), 2)
        t.setHorizontalHeaderLabels(["code", _tr(window, "gp85", "Description")])
        tr = getattr(window, "tr_", None)
        for i, a in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(a.code)))
            t.setItem(i, 1, QTableWidgetItem(a.text(tr)))
        t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(t)
        if not alarms:
            lay.addWidget(QLabel("no active alarms"))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        clr = bb.addButton(_tr(window, "gp90", "Clear Alarm"), QDialogButtonBox.ActionRole)
        clr.clicked.connect(lambda: (window.ctl.clear_alarms(), self.accept()))
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class CardExtension:
    def __init__(self, window):
        self.w = window
        self._connected_to = None

    # ------------------------------------------------------------------ controller signals
    def attach(self) -> None:
        ctl = self.w.ctl
        if ctl is self._connected_to or not _is_card(ctl):
            return
        self._connected_to = ctl
        sig = getattr(ctl, "signals", None)
        if sig is not None:
            sig.alarmsChanged.connect(self._alarms_changed)
        self._last = set()

    def _alarms_changed(self, alarms: list) -> None:
        tr = getattr(self.w, "tr_", None)
        codes = {a.code for a in alarms}
        panel = getattr(self.w, "log_panel", None)
        for a in alarms:
            if a.code not in self._last and panel is not None and hasattr(panel, "add_alarm"):
                panel.add_alarm(a.text(tr))
        self._last = codes

    # ------------------------------------------------------------------ commands
    def alarms(self) -> None:
        self.attach()
        AlarmListDialog(self.w, list(getattr(self.w.ctl, "active_alarms", []))).exec()

    def clear_alarms(self) -> None:
        self.w.ctl.clear_alarms()

    def break_resume(self) -> None:
        ctl = self.w.ctl
        if getattr(ctl, "breakpoint", None) is not None:
            ctl.resume()
        elif hasattr(ctl, "resume_saved"):
            if not ctl.resume_saved(self.w.document):
                self.w.log_panel.log("mp64: no breakpoint to continue from")
        else:
            self.w.log_panel.log("mp64: breakpoint resume needs the card controller")

    def params(self) -> None:
        sc = getattr(self.w.ctl, "scaling", None)
        m = getattr(self.w, "machine", None)
        txt = sc.summary() if sc is not None else "card parameters not read (not connected)"
        if m is not None:
            txt += (f"\nMachine: {m.pulses_per_mm_x:g} x {m.pulses_per_mm_y:g} pulses/mm, "
                    f"period {m.period_ms:g} ms, step speed cap {m.max_step_speed:.0f} mm/s")
        self.w.log_panel.log(txt)

    def stream_process(self, on: bool) -> None:
        if hasattr(self.w.ctl, "stream_process"):
            self.w.ctl.stream_process = bool(on)
            self.w.log_panel.log(f"streaming in a separate process: {'on' if on else 'off'}")


def register(ctx) -> CardExtension:
    """Bind the card-control commands (``ctx``: PluginContext)."""
    ext = CardExtension(ctx.window)
    ctx.register_command("card.alarms", ext.alarms, label="mf123")
    ctx.register_command("card.clear_alarms", ext.clear_alarms, label="gp90")
    ctx.register_command("card.break_resume", ext.break_resume, label="mp64")
    ctx.register_command("card.params", ext.params, label="gp98")
    ctx.register_command("card.stream_process", ext.stream_process, label="Stream in separate process",
                         checkable=True)
    ctx.on_controller_changed(lambda _ctl: ext.attach())
    ext.attach()
    return ext
