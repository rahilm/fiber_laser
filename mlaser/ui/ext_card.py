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


def _tr(window, key: str, default: str, **values) -> str:
    tr = getattr(window, "tr_", None)
    s = None
    if tr is not None:
        try:
            s = tr.format(key, **values) if values and tr.has(key) else tr.tr(key)
        except Exception:  # noqa: BLE001
            s = None
    if not s or s == key:
        s = default.format(**values) if values else default
    return s




class AlarmListDialog(Dialog):
    """Active card alarms (code, text) with a Reset alarm button; ``fault``: the latched fault's
    raw reason (the engineering detail behind the banner's plain headline, UX audit W12)."""

    def __init__(self, window, alarms: list, fault: str = ""):
        if fault:
            explanation = _tr(window, "card.alarms.fault_detail", "Fault detail: {detail}", detail=fault)
        else:
            explanation = "" if alarms else _tr(window, "card.alarms.none", "No active alarms.")
        super().__init__(window, _tr(window, "mf123", "Alarm"), buttons=QDialogButtonBox.Close,
                         explanation=explanation)
        t = S.table([S.Col(_tr(window, "card.alarms.code", "Code"), priority=1), S.Col(_tr(window, "gp85", "Description"), stretch=True)])
        t.setRowCount(len(alarms))
        tr = getattr(window, "tr_", None)
        for i, a in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(a.code)))
            t.setItem(i, 1, QTableWidgetItem(a.text(tr)))
        self.table = t
        self.body.addWidget(t, 1)
        clr = self.buttons.addButton(_tr(window, "run.btn.reset", "Reset alarm"), QDialogButtonBox.ActionRole)
        S.set_enabled(clr, bool(alarms or fault), "" if alarms or fault else _tr(
            window, "card.alarms.nothing_to_clear", "No alarm to reset"))
        clr.clicked.connect(lambda: (self.accept(), self._reset(window)))

    @staticmethod
    def _reset(window) -> None:
        su = getattr(window, "safety_ui", None)
        if su is not None:
            su.reset_fault()
        else:
            window.ctl.clear_alarms()


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
        su = getattr(self.w, "safety_ui", None)
        if su is not None:                 # "Reset alarm": one path (UX audit C2/W5)
            su.reset_fault()
        else:
            self.w.ctl.clear_alarms()

    def break_resume(self) -> None:
        ctl = self.w.ctl
        if ctl.breakpoint is not None:
            from .resume import resume
            resume(ctl, "exact", self.w.document)
        else:
            from .qtbridge import when_done
            when_done(ctl.resume_saved(self.w.document),
                      lambda ok: None if ok else self.w.log_panel.log(_tr(
                          self.w, "card.log.no_breakpoint", "Resume from breakpoint: no breakpoint to continue from")))

    def params(self) -> None:
        sc = self.w.ctl.scaling
        m = getattr(self.w, "machine", None)
        from .units_qt import fmt
        txt = sc.summary() if sc is not None else _tr(self.w, "card.log.not_read",
                                                        "Card parameters not read (not connected)")
        if m is not None:
            txt += "\n" + _tr(self.w, "card.log.machine",
                              "Machine: {ppm_x} x {ppm_y} pulses/mm, period {period} ms, step speed cap {speed}",
                              ppm_x=f"{m.pulses_per_mm_x:g}", ppm_y=f"{m.pulses_per_mm_y:g}",
                              period=f"{m.period_ms:g}", speed=fmt(m.max_step_speed, "speed", decimals=0))
        self.w.log_panel.log(txt)

    def stream_process(self, on: bool) -> None:
        self.w.ctl.stream_process = bool(on)
        self.w.log_panel.log(_tr(self.w, "card.log.stream_on", "Streaming in a separate process: on") if on
                             else _tr(self.w, "card.log.stream_off", "Streaming in a separate process: off"))


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
