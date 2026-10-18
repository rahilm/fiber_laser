"""Card-control UI: alarms, Clear Alarm, breakpoint resume, card parameters, streaming mode.

``register(window)`` adds these commands to the MainWindow (``window.ribbon.action`` +
``window.handlers``) and subscribes to the controller's alarm events (``ctl.events``,
:mod:`mlaser.machine.events`; delivered on the GUI thread by ``ui.qtbridge``):

* ``card.alarms``      (mf123 "Alarm")          dialog of the active card alarms
* ``card.clear_alarms`` (gp90 "Clear Alarm")    ``[9999, 5, 0, 0]``
* ``card.break_resume`` (mp64 "BreakCon")       continue from the newest AutosaveParam record
* ``card.params``      (gp98 "Settings")        scaling/period read from the card
* ``card.stream_process`` (checkable)           stream in a separate process (GIL-proof)
* ``machine.home*``    Go Origin X / Y / XY / XYZ / ZF-then-XY (mf15..mf18, mf222, mf482)

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


def _bind(window, cmd: str, key: str, fn, checkable: bool = False):
    act = window.ribbon.action(cmd, key, checkable=checkable)
    if not checkable:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            try:
                act.triggered.disconnect()
            except (RuntimeError, TypeError):
                pass
        act.triggered.connect(lambda _=False: fn())
    if isinstance(getattr(window, "handlers", None), dict):
        window.handlers[cmd] = fn
    return act


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
            ctl.resume()
        elif not ctl.resume_saved(self.w.document):
            self.w.log_panel.log("mp64: no breakpoint to continue from")

    def params(self) -> None:
        sc = self.w.ctl.scaling
        m = getattr(self.w, "machine", None)
        txt = sc.summary() if sc is not None else "card parameters not read (not connected)"
        if m is not None:
            txt += (f"\nMachine: {m.pulses_per_mm_x:g} x {m.pulses_per_mm_y:g} pulses/mm, "
                    f"period {m.period_ms:g} ms, step speed cap {m.max_step_speed:.0f} mm/s")
        self.w.log_panel.log(txt)

    def stream_process(self, on: bool) -> None:
        self.w.ctl.stream_process = bool(on)
        self.w.log_panel.log(f"streaming in a separate process: {'on' if on else 'off'}")


def register(window) -> CardExtension:
    """Add the card-control commands to ``window`` (MainWindow)."""
    ext = CardExtension(window)
    window.card_ext = ext
    _bind(window, "card.alarms", "mf123", ext.alarms)
    _bind(window, "card.clear_alarms", "gp90", ext.clear_alarms)
    _bind(window, "card.break_resume", "mp64", ext.break_resume)
    _bind(window, "card.params", "gp98", ext.params)
    act = _bind(window, "card.stream_process", "Stream in separate process", lambda: None, checkable=True)
    act.toggled.connect(ext.stream_process)
    homes = {"machine.home": ("mf15", "XY"), "machine.home_x": ("mf16", "X"),
             "machine.home_y": ("mf17", "Y"), "machine.home_xyz": ("mf222", "XYZ")}
    for cmd, (key, axes) in homes.items():
        _bind(window, cmd, key, lambda a=axes: window.ctl.home(a))
    _bind(window, "machine.home_zf_then_xy", "mf482",
          lambda: (window.ctl.home("Z"), window.ctl.home("XY")))
    # follow controller swaps (connect_simulator / set_controller)
    orig = getattr(window, "set_controller", None)
    if callable(orig):
        def set_controller(ctl, _orig=orig):
            _orig(ctl)
            ext.attach()
        window.set_controller = set_controller
    ext.attach()
    return ext
