"""Card-control UI: alarms, Clear Alarm, breakpoint resume, card parameters, streaming mode.

``register(ctx)`` (a :class:`mlaser.services.plugins.PluginContext`) binds these commands and
subscribes to the controller's alarm events (``ctl.events``, :mod:`mlaser.machine.events`;
delivered on the GUI thread by ``ui.qtbridge``) again on every controller change
(``ctx.on_controller_changed``):

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

from PySide6.QtWidgets import (QDialogButtonBox, QTableWidgetItem)

from . import style as S
from .style import Dialog


def _tr(window, key: str, default: str) -> str:
    tr = getattr(window, "tr_", None)
    if tr is None:
        return default
    try:
        s = tr.tr(key)
    except Exception:  # noqa: BLE001
        return default
    return s if s and s != key else default




class AlarmListDialog(Dialog):
    """Active card alarms (code, text) with a Clear alarms button."""

    def __init__(self, window, alarms: list):
        super().__init__(window, _tr(window, "mf123", "Alarm"), buttons=QDialogButtonBox.Close,
                         explanation="" if alarms else "No active alarms.")
        t = S.table([S.Col("Code", priority=1), S.Col(_tr(window, "gp85", "Description"), stretch=True)])
        t.setRowCount(len(alarms))
        tr = getattr(window, "tr_", None)
        for i, a in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(a.code)))
            t.setItem(i, 1, QTableWidgetItem(a.text(tr)))
        self.table = t
        self.body.addWidget(t, 1)
        clr = self.buttons.addButton(_tr(window, "gp90", "Clear Alarm"), QDialogButtonBox.ActionRole)
        S.set_enabled(clr, bool(alarms), "" if alarms else "No alarm to clear")
        clr.clicked.connect(lambda: (window.ctl.clear_alarms(), self.accept()))


class CardExtension:
    def __init__(self, window):
        self.w = window
        self._connected_to = None
        self._unsub: list = []
        self._last: set = set()

    # ------------------------------------------------------------------ controller events
    def attach(self) -> None:
        from mlaser.machine import events as EV
        ctl = self.w.ctl
        if ctl is self._connected_to:
            return
        for un in self._unsub:
            un()
        self._connected_to = ctl
        self._unsub = [ctl.events.subscribe(EV.AlarmRaised, lambda e: self._alarms_changed(list(e.active))),
                       ctl.events.subscribe(EV.AlarmCleared, lambda e: self._alarms_changed(list(e.active)))]
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
        AlarmListDialog(self.w, list(self.w.ctl.active_alarms)).exec()

    def clear_alarms(self) -> None:
        self.w.ctl.clear_alarms()

    def break_resume(self) -> None:
        ctl = self.w.ctl
        if ctl.breakpoint is not None:
            from .resume import resume
            resume(ctl, "exact", self.w.document)
        elif not ctl.resume_saved(self.w.document):
            self.w.log_panel.log("mp64: no breakpoint to continue from")

    def params(self) -> None:
        sc = self.w.ctl.scaling
        m = getattr(self.w, "machine", None)
        from .units_qt import fmt
        txt = sc.summary() if sc is not None else "card parameters not read (not connected)"
        if m is not None:
            txt += (f"\nMachine: {m.pulses_per_mm_x:g} x {m.pulses_per_mm_y:g} pulses/mm, "
                    f"period {m.period_ms:g} ms, step speed cap {fmt(m.max_step_speed, 'speed', decimals=0)}")
        self.w.log_panel.log(txt)

    def stream_process(self, on: bool) -> None:
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
