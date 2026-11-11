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

from typing import Callable, Optional

from PySide6.QtWidgets import (QDialogButtonBox, QTableWidgetItem)

from . import style as S
from .extension_context import ExtensionContext
from .i18n import translator
from .style import Dialog


def _tr(key: str, default: str, **values) -> str:
    tr = translator()
    try:
        s = tr.format(key, **values) if values and tr.has(key) else tr.tr(key)
    except Exception:  # noqa: BLE001
        s = None
    if not s or s == key:
        s = default.format(**values) if values else default
    return s




class AlarmListDialog(Dialog):
    """Active card alarms (code, text) with a Reset alarm button; ``fault``: the latched fault's
    raw reason (the engineering detail behind the banner's plain headline, UX audit W12).
    ``window``: the main window (the parent, and what the diagnostic bundle reads); ``reset``:
    what "Reset alarm" does (the safety UI's fault reset; None: no reset button)."""

    def __init__(self, window, alarms: list, fault: str = "", reset: Optional[Callable[[], None]] = None):
        if fault:
            explanation = _tr("card.alarms.fault_detail", "Fault detail: {detail}", detail=fault)
        else:
            explanation = "" if alarms else _tr("card.alarms.none", "No active alarms.")
        super().__init__(window, _tr("mf123", "Alarm"), buttons=QDialogButtonBox.Close,
                         explanation=explanation)
        t = S.table([S.Col(_tr("card.alarms.code", "Code"), priority=1), S.Col(_tr("gp85", "Description"), stretch=True)])
        t.setRowCount(len(alarms))
        tr = translator()
        for i, a in enumerate(alarms):
            t.setItem(i, 0, QTableWidgetItem(str(a.code)))
            t.setItem(i, 1, QTableWidgetItem(a.text(tr)))
        self.table = t
        self.body.addWidget(t, 1)
        if reset is not None:
            clr = self.buttons.addButton(_tr("run.btn.reset", "Reset alarm"), QDialogButtonBox.ActionRole)
            S.set_enabled(clr, bool(alarms or fault), "" if alarms or fault else _tr(
                "card.alarms.nothing_to_clear", "No alarm to reset"))
            clr.clicked.connect(lambda: (self.accept(), reset()))
        if fault or alarms:              # the evidence for a report, offered where the fault is seen
            from .diag_bundle import bundle_button
            bundle_button(window, self.buttons, self)



class CardExtension:
    def __init__(self, ctx: ExtensionContext):
        self.ctx = ctx
        self._connected_to = None
        self._unsub: list = []
        self._last: set = set()

    # ------------------------------------------------------------------ controller events
    def attach(self) -> None:
        from mlaser.machine import events as EV
        ctl = self.ctx.controller
        if ctl is self._connected_to:
            return
        for un in self._unsub:
            un()
        self._connected_to = ctl
        self._unsub = [ctl.events.subscribe(EV.AlarmRaised, lambda e: self._alarms_changed(list(e.active))),
                       ctl.events.subscribe(EV.AlarmCleared, lambda e: self._alarms_changed(list(e.active)))]
        self._last = set()

    def _alarms_changed(self, alarms: list) -> None:
        tr = self.ctx.tr
        codes = {a.code for a in alarms}
        panel = self.ctx.log_panel
        for a in alarms:
            if a.code not in self._last:
                panel.add_alarm(a.text(tr))
        self._last = codes

    # ------------------------------------------------------------------ commands
    def alarms(self) -> None:
        self.attach()
        AlarmListDialog(self.ctx.parent, list(self.ctx.controller.active_alarms),
                        reset=self.clear_alarms).exec()

    def clear_alarms(self) -> None:
        self.ctx.safety_ui.reset_fault()     # "Reset alarm": one path (UX audit C2/W5)

    def break_resume(self) -> None:
        ctl = self.ctx.controller
        if ctl.breakpoint is not None:
            from .resume import resume
            resume(ctl, "exact", self.ctx.document)
        else:
            from .qtbridge import when_done
            when_done(ctl.resume_saved(self.ctx.document),
                      lambda ok: None if ok else self.ctx.log(_tr(
                          "card.log.no_breakpoint", "Resume from breakpoint: no breakpoint to continue from")))

    def params(self) -> None:
        sc = self.ctx.controller.scaling
        m = self.ctx.machine
        from .units_qt import fmt
        txt = sc.summary() if sc is not None else _tr("card.log.not_read",
                                                      "Card parameters not read (not connected)")
        if m is not None:
            txt += "\n" + _tr("card.log.machine",
                              "Machine: {ppm_x} x {ppm_y} pulses/mm, period {period} ms, step speed cap {speed}",
                              ppm_x=f"{m.pulses_per_mm_x:g}", ppm_y=f"{m.pulses_per_mm_y:g}",
                              period=f"{m.period_ms:g}", speed=fmt(m.max_step_speed, "speed", decimals=0))
        self.ctx.log(txt)

    def stream_process(self, on: bool) -> None:
        self.ctx.controller.stream_process = bool(on)
        self.ctx.log(_tr("card.log.stream_on", "Streaming in a separate process: on") if on
                     else _tr("card.log.stream_off", "Streaming in a separate process: off"))


def register(ctx: ExtensionContext) -> CardExtension:
    """Bind the card-control commands (``ctx``: the built-in :class:`ExtensionContext`)."""
    ext = CardExtension(ctx)
    ctx.register_command("card.alarms", ext.alarms, label="mf123")
    ctx.register_command("card.clear_alarms", ext.clear_alarms, label="gp90")
    ctx.register_command("card.break_resume", ext.break_resume, label="mp64")
    ctx.register_command("card.params", ext.params, label="gp98")
    ctx.register_command("card.stream_process", ext.stream_process, label="Stream in separate process",
                         checkable=True)
    ctx.on_controller_changed(lambda _ctl: ext.attach())
    ext.attach()
    return ext
