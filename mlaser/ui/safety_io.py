"""Safety I/O in the application: the interlocks and the heartbeat reach the controller.

``install(window)`` (MainWindow, before the safety UI) creates a :class:`SafetyIOBinding`:

* applies the ``safety.*`` configuration (:mod:`mlaser.services.safety_io`) to the controller at
  start-up, after every change of a ``safety.*`` value and after the controller is replaced;
  while a job runs or the machine is armed a change stays **pending** and is applied as soon as
  the machine is idle and disarmed (the Run screen says so);
* gives the Run screen its notice ("door interlock not configured ...", :meth:`notice_text`) and
  the pre-flight dialog its warnings (:meth:`preflight_issues`, real machine only);
* the **Safety I/O** dialog (:class:`SafetyIODialog`, command ``machine.safety_io``, Engineer and
  Service mode; also from the commissioning checklist): input number, alarm level and "not
  fitted" per interlock, the follower interlock, the heartbeat output and frequency.
"""
from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QLabel, QSpinBox, QVBoxLayout)

from mlaser.services import safety_io as S
from mlaser.services.config import MAX_INPUT, MAX_OUTPUT

from .workspaces import tr

TITLE_KEYS = {"gas": "safety.io.gas", "door": "safety.io.door", "laser": "safety.io.laser",
              "heartbeat": "safety.io.heartbeat"}


class SafetyIOBinding:
    """See the module docstring."""

    def __init__(self, window: Any) -> None:
        self.w = window
        self.pending = False
        self.last = ""                       # result of the last apply
        self._unsub = window.config.subscribe(self._config_changed)
        hooks = getattr(window, "hooks", None)
        if hooks is not None:
            hooks.add("controller_changed", lambda _ctl: self.sync(), "safety_io")
            hooks.add("close", self._close, "safety_io")
        t = getattr(window, "_timer", None)
        if t is not None:
            t.timeout.connect(self._tick)
        self.sync()

    # ------------------------------------------------------------------ state
    def config(self) -> S.SafetyIO:
        return S.from_config(self.w.config)

    def supported(self) -> bool:
        return S.controller_supports(getattr(self.w, "ctl", None))

    def sync(self) -> str:
        """Apply the configuration to the controller now (or mark it pending)."""
        ctl = getattr(self.w, "ctl", None)
        res = S.apply(ctl, self.config()) if ctl is not None else "unsupported"
        self.pending = res == "pending"
        if res != self.last and res in ("applied", "pending"):
            log = getattr(self.w, "log", None)
            if callable(log):
                log("safety I/O applied to the controller" if res == "applied" else
                    "safety I/O change pending: it applies when the machine is disarmed and idle")
        self.last = res
        return res

    def _config_changed(self, key: str) -> None:
        if key == "*" or key.startswith("safety."):
            self.sync()

    def _tick(self) -> None:
        if self.pending:
            self.sync()

    def _close(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    # ------------------------------------------------------------------ notices
    def notices(self) -> list[S.Notice]:
        return S.notices(self.config()) if self.supported() else []

    def notice_text(self) -> str:
        """One line for the Run screen ("" when every interlock and the heartbeat are set)."""
        parts = []
        for n in self.notices():
            name = tr(self.w, TITLE_KEYS[n.source], S.NAMES[n.source])
            parts.append(tr(self.w, n.key, "{name}: not configured", name=name))
        if self.pending:
            parts.append(tr(self.w, "safety.notice.pending", "safety I/O change pending (applies when disarmed)"))
        if not parts:
            return ""
        return tr(self.w, "safety.notice.prefix", "Safety I/O:") + " " + " · ".join(parts)

    def preflight_issues(self, action: str, emits: bool) -> list:
        """Warnings for the pre-flight dialog of a laser job on a real machine."""
        ctl = getattr(self.w, "ctl", None)
        if not self.supported() or ctl is None:
            return []
        try:
            if ctl.is_simulator():
                return []
        except Exception:  # noqa: BLE001
            return []
        return S.preflight_issues(self.config(), action, emits)


def augment_preflight(window: Any, rep: Any, action: str, emits: Optional[bool]) -> Any:
    """``rep`` plus the "interlock not configured" warnings (a new report; the controller's own
    ``last_preflight`` is not changed)."""
    b = getattr(window, "safety_io", None)
    if b is None or rep is None:
        return rep
    fires = bool(emits) if emits is not None else action in ("start", "resume")
    extra = b.preflight_issues(action, fires)
    if not extra:
        return rep
    from dataclasses import replace
    return replace(rep, issues=list(rep.issues) + extra)


def install(window: Any) -> SafetyIOBinding:
    b = SafetyIOBinding(window)
    window.safety_io = b
    return b


# =========================================================================== dialog
class SafetyIODialog(QDialog):
    """Edit the ``safety.*`` values (see the module docstring)."""

    def __init__(self, window: Any, parent: Any = None) -> None:
        super().__init__(parent or window)
        self.w = window
        self.cfg = window.config
        self.errors: list[str] = []
        self.setObjectName("safety_io_dialog")
        self.setWindowTitle(tr(window, "safety.io.title", "Safety I/O"))
        lay = QVBoxLayout(self)
        intro = QLabel(tr(window, "safety.io.intro",
                          "Card inputs of the interlocks and the output of the PC-alive watchdog relay "
                          "(docs/SAFETY.md §8, §11). 0 = not wired. Check every input with the "
                          "commissioning checklist after a change."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.inputs: dict[str, QSpinBox] = {}
        self.levels: dict[str, QComboBox] = {}
        self.not_fitted: dict[str, QCheckBox] = {}
        self.states: dict[str, QLabel] = {}
        for src, (inp, act) in S.INTERLOCKS.items():
            box = QGroupBox(tr(window, TITLE_KEYS[src], S.NAMES[src]))
            box.setObjectName(f"safety_io_{src}")
            f = QFormLayout(box)
            sp = QSpinBox()
            sp.setObjectName(f"safety_io_{src}_input")
            sp.setRange(0, MAX_INPUT)
            sp.setSpecialValueText(tr(window, "safety.io.unwired", "not wired"))
            sp.setValue(int(self.cfg.get(f"safety.{inp}")))
            f.addRow(tr(window, "safety.io.input", "Card input"), sp)
            lv = QComboBox()
            lv.setObjectName(f"safety_io_{src}_level")
            lv.addItem(tr(window, "safety.io.level_on", "input ON (normally open contact)"), True)
            lv.addItem(tr(window, "safety.io.level_off", "input OFF (normally closed contact, wire break trips)"),
                       False)
            lv.setCurrentIndex(0 if bool(self.cfg.get(f"safety.{act}")) else 1)
            f.addRow(tr(window, "safety.io.active", "Alarm when"), lv)
            nf = QCheckBox(tr(window, "safety.io.not_fitted", "This machine has no such sensor"))
            nf.setObjectName(f"safety_io_{src}_not_fitted")
            nf.setChecked(bool(self.cfg.get(f"safety.{src}_not_fitted")))
            f.addRow("", nf)
            st = QLabel()
            st.setObjectName(f"safety_io_{src}_state")
            f.addRow(tr(window, "safety.io.now", "Now"), st)
            sp.valueChanged.connect(lambda v, c=nf: _wired(c, v))
            _wired(nf, sp.value())
            self.inputs[src], self.levels[src], self.not_fitted[src], self.states[src] = sp, lv, nf, st
            lay.addWidget(box)
        self.follower = QCheckBox(tr(window, "safety.io.follower", "Follower alarm interlock (on-board follower)"))
        self.follower.setObjectName("safety_io_follower")
        self.follower.setChecked(bool(self.cfg.get("safety.follower")))
        lay.addWidget(self.follower)
        hb = QGroupBox(tr(window, "safety.io.heartbeat", S.NAMES["heartbeat"]))
        hb.setObjectName("safety_io_heartbeat")
        f = QFormLayout(hb)
        self.hb_output = QSpinBox()
        self.hb_output.setObjectName("safety_io_heartbeat_output")
        self.hb_output.setRange(0, MAX_OUTPUT)
        self.hb_output.setSpecialValueText(tr(window, "safety.io.off", "off"))
        self.hb_output.setValue(int(self.cfg.get("safety.heartbeat_output")))
        f.addRow(tr(window, "safety.io.heartbeat_output", "Card output"), self.hb_output)
        self.hb_hz = QDoubleSpinBox()
        self.hb_hz.setObjectName("safety_io_heartbeat_hz")
        self.hb_hz.setRange(0.5, 50.0)
        self.hb_hz.setDecimals(1)
        self.hb_hz.setSuffix(" Hz")
        self.hb_hz.setValue(float(self.cfg.get("safety.heartbeat_hz")))
        f.addRow(tr(window, "safety.io.heartbeat_hz", "Frequency"), self.hb_hz)
        lay.addWidget(hb)
        self.message = QLabel()
        self.message.setObjectName("safety_io_message")
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#b71c1c;")
        lay.addWidget(self.message)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh_states()

    def refresh_states(self) -> None:
        """The live state of each wired input (the controller's interlock monitor)."""
        ctl = getattr(self.w, "ctl", None)
        active = S.active(ctl)
        cur = S.current(ctl) if ctl is not None else None
        for src, lbl in self.states.items():
            if cur is None:
                lbl.setText(tr(self.w, "safety.io.now_unknown", "no machine controller"))
            elif not cur.configured(src):
                lbl.setText(tr(self.w, "safety.io.now_unwired", "not monitored"))
            elif src in active:
                lbl.setText(tr(self.w, "safety.io.now_alarm", "ALARM: {text}", text=active[src]))
            else:
                lbl.setText(tr(self.w, "safety.io.now_clear", "clear"))

    def values(self) -> dict:
        out: dict = {}
        for src, (inp, act) in S.INTERLOCKS.items():
            out[f"safety.{inp}"] = self.inputs[src].value()
            out[f"safety.{act}"] = bool(self.levels[src].currentData())
            out[f"safety.{src}_not_fitted"] = bool(self.not_fitted[src].isChecked()) and not self.inputs[src].value()
        out["safety.follower"] = self.follower.isChecked()
        out["safety.heartbeat_output"] = self.hb_output.value()
        out["safety.heartbeat_hz"] = round(self.hb_hz.value(), 3)
        return out

    def save(self) -> bool:
        """Validate and store the values (persisted at once); False with :attr:`errors`."""
        vals = self.values()
        cand = S.from_config(_Overlay(self.cfg, vals))
        ctl = getattr(self.w, "ctl", None)
        self.errors = S.problems(cand, S.output_map(ctl))
        if self.errors:
            self.message.setText(tr(self.w, "safety.io.problems", "Not saved: {problems}",
                                    problems="; ".join(self.errors)))
            return False
        for k, v in vals.items():
            self.cfg.set(k, v)
        try:
            self.cfg.save()
        except OSError as exc:
            self.errors = [str(exc)]
            self.message.setText(tr(self.w, "safety.io.problems", "Not saved: {problems}", problems=str(exc)))
            return False
        return True

    def _ok(self) -> None:
        if self.save():
            self.accept()


def _wired(not_fitted: QCheckBox, value: int) -> None:
    """A wired input cannot also be "not fitted"."""
    not_fitted.setEnabled(value == 0)
    if value:
        not_fitted.setChecked(False)


class _Overlay:
    """A ConfigService view with some values replaced (validation before saving)."""

    def __init__(self, cfg: Any, vals: dict) -> None:
        self._cfg, self._vals = cfg, vals

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._vals:
            return self._vals[key]
        return self._cfg.get(key)


def open_dialog(window: Any, parent: Any = None) -> Optional[SafetyIODialog]:
    """The Safety I/O dialog (modal); returns it after it closed (None when there is no config)."""
    if getattr(window, "config", None) is None:
        return None
    d = SafetyIODialog(window, parent)
    d.exec()
    return d


__all__ = ["SafetyIOBinding", "SafetyIODialog", "install", "augment_preflight", "open_dialog"]
