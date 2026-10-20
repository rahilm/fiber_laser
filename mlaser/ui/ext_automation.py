"""Automation API plugin: starts the opt-in local REST + WebSocket server (:mod:`mlaser.api`)
and feeds it the status, the events and the intake store (docs/user/automation.md).

What it gives the server, and nothing else:

* a status dict built on the GUI thread once a second from the Run workspace's status, the
  queue and the approval list (``publish_status``);
* events from the controller's EventBus, the queue, the queue runner and the intake store
  (``publish_event``: a lock and a deque append, cheap on any thread, never waiting for a client);
* ``submit`` / ``upload``: :meth:`mlaser.services.intake.IntakeStore.submit` /
  ``store_upload`` (files in the inbox; the operator approves at the machine).

The server never receives the controller, the window or the queue object: no request can move,
arm, start, stop or approve anything (tests/safety/test_automation_api.py).

Settings ``plugin.ext_automation.*``: ``enabled`` (off), ``host`` (``127.0.0.1``), ``port``
(8766), ``token`` (generated, 32 characters), ``queue_intake`` (off), ``uploads`` (on),
``job_folders`` (folders a submission may name by path, ``;``-separated), ``max_upload_mb``.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QLabel, QLineEdit,
                               QMessageBox, QSpinBox, QWidget)

from mlaser.machine import events as EV
from mlaser.services import runsource as RS
from mlaser.services.config import Setting

from . import style as S
from .style import Dialog
from .workspaces import tr

log = logging.getLogger("mlaser.ui.automation")
PLUGIN_API = 1

SETTINGS = [
    Setting("enabled", "bool", False, label="api.set.enabled"),
    Setting("host", "str", "127.0.0.1", label="api.set.host"),
    Setting("port", "int", 8766, min=1024, max=65535, label="api.set.port"),
    Setting("token", "str", "", label="api.set.token"),
    Setting("queue_intake", "bool", False, label="api.set.queue_intake"),
    Setting("uploads", "bool", True, label="api.set.uploads"),
    Setting("job_folders", "str", "", label="api.set.job_folders"),
    Setting("max_upload_mb", "int", 20, min=1, max=50, label="api.set.max_upload_mb"),
]


def new_token() -> str:
    return secrets.token_urlsafe(24)


def _alarm_list(window: Any) -> list[dict]:
    ctl = window.ctl
    out = []
    try:
        st = ctl.safety()
        if st.banner == "FAULT":
            out.append({"code": "FAULT", "text": st.fault, "kind": "fault"})
    except Exception:  # noqa: BLE001
        pass
    trn = getattr(window, "tr_", None)
    for a in list(ctl.active_alarms or []):
        out.append({"code": str(getattr(a, "code", "")), "text": a.text(trn) if hasattr(a, "text") else str(a),
                    "kind": str(getattr(a, "kind", "card"))})
    return out


def _entry(e: Any) -> dict:
    return {"id": e.id, "name": e.name, "quantity": e.quantity, "done": e.done, "status": e.status,
            "material": e.material, "thickness_mm": e.thickness_mm, "sheet": e.remnant_id or e.sheet,
            "est_s": round(float(e.est_s or 0.0), 1), "actual_s": round(float(e.actual_s or 0.0), 1),
            "source": (e.extra.get("intake") or {}).get("source", "operator") if isinstance(e.extra, dict) else "operator"}


class AutomationPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.window = ctx.window
        self.settings = ctx.settings_schema(SETTINGS)
        self.server: Any = None
        self._unsub: list = []
        self._bus: Any = None
        self._pending_ids: set[str] = set()
        self._runner_state = ""
        self._hooked_queue = False
        self.timer = QTimer(self.window)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.publish_status)
        ctx.on_controller_changed(lambda _c: self.attach())
        ctx.on_close(self.close)
        ctx.provide("automation.server", lambda: self.server)
        if bool(self.settings["enabled"]):
            QTimer.singleShot(0, lambda: self.set_enabled(True, confirm=False))

    # ------------------------------------------------------------------ services
    def _provider(self, name: str) -> Any:
        hooks = getattr(self.window, "hooks", None)
        fn = hooks.provider(name) if hooks is not None else None
        try:
            return fn() if fn is not None else None
        except Exception:  # noqa: BLE001
            return None

    @property
    def intake(self) -> Any:
        return self._provider("intake")

    @property
    def queue(self) -> Any:
        return self._provider("queue")

    @property
    def runner(self) -> Any:
        return self._provider("queue_runner")

    def allowed_roots(self) -> list[str]:
        roots = [r.strip() for r in str(self.settings["job_folders"] or "").split(";") if r.strip()]
        it = self.intake
        if it is not None:
            roots.append(str(it.files_dir))
        return roots

    # ------------------------------------------------------------------ server callables (server threads)
    def _submit(self, req: dict, client: str) -> dict:
        it = self.intake
        if it is None:
            raise ValueError("no intake store")
        return it.submit(req, "api", client, allowed_roots=self.allowed_roots()).public()

    def _upload(self, filename: str, content: bytes) -> str:
        it = self.intake
        if it is None:
            raise ValueError("no intake store")
        return it.store_upload(filename, content)

    # ------------------------------------------------------------------ on / off
    def set_enabled(self, on: bool, confirm: bool = True, port: Optional[int] = None) -> bool:
        from mlaser.api import AutomationServer
        w = self.window
        if not on:
            self.stop()
            self.settings["enabled"] = False
            return False
        if self.server is not None and self.server.running:
            return True
        token = str(self.settings["token"] or "")
        if len(token) < 24:
            token = new_token()
            self.settings["token"] = token
        host = str(self.settings["host"] or "127.0.0.1")
        port = int(self.settings["port"]) if port is None else int(port)
        intake_on = bool(self.settings["queue_intake"])
        if confirm and not getattr(w, "_test_no_dialogs", False):
            text = tr(w, "api.warn.text",
                      "The automation API serves the machine status and events on {host}:{port} to programs that "
                      "have its token{intake}. It cannot start, stop, hold, arm or move the machine. "
                      "Keep it on this PC (127.0.0.1) unless the shop network is trusted.\n\nEnable it?",
                      host=host, port=port,
                      intake=tr(w, "api.warn.intake", ", and accepts jobs for the queue, which wait for your "
                                "approval on the Run screen") if intake_on else "")
            if QMessageBox.warning(w, tr(w, "api.warn.title", "Enable the automation API"), text,
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return False
        try:
            self.server = AutomationServer(host, port, token, submit=self._submit if intake_on else None,
                                           upload=self._upload if intake_on and bool(self.settings["uploads"]) else None,
                                           allow_queue=intake_on,
                                           max_upload=int(self.settings["max_upload_mb"]) * 1024 * 1024).start()
        except (OSError, ValueError) as exc:
            self.server = None
            self.ctx.log(tr(w, "api.log.failed", "Automation API could not start on {host}:{port}: {err}",
                            host=host, port=port, err=exc))
            return False
        self.settings["enabled"] = True
        self.attach()
        self.publish_status()
        self.timer.start()
        self.ctx.log(tr(w, "api.log.started", "Automation API ON (read-only{intake}): {url}", url=self.server.url(),
                        intake=tr(w, "api.log.intake", ", queue intake with approval") if intake_on else ""))
        return True

    def stop(self) -> None:
        self.timer.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.ctx.log(tr(self.window, "api.log.stopped", "Automation API stopped"))

    def close(self) -> None:
        self.stop()
        for un in self._unsub:
            un()
        self._unsub = []

    # ------------------------------------------------------------------ events
    def _pub(self, kind: str, data: Optional[dict] = None) -> None:
        srv = self.server
        if srv is not None and srv.running:
            srv.publish_event(kind, data or {})

    def attach(self) -> None:
        bus = self.window.ctl.events
        if bus is not self._bus:
            for un in self._unsub:
                un()
            self._bus = bus
            self._unsub = [bus.subscribe(EV.JobStarted, self._on_started),
                           bus.subscribe(EV.JobFinished, self._on_finished),
                           bus.subscribe(EV.AlarmRaised, lambda e: self._pub("alarm", {"alarms": self._alarms(e.alarms)})),
                           bus.subscribe(EV.AlarmCleared, lambda e: self._pub("alarm_cleared",
                                                                              {"alarms": self._alarms(e.alarms)})),
                           bus.subscribe(EV.StateChanged, lambda e: self._pub("state", {"old": e.old, "new": e.new,
                                                                                        "reason": e.reason,
                                                                                        "fault": e.fault})),
                           bus.subscribe(EV.Armed, lambda e: self._pub("armed")),
                           bus.subscribe(EV.Disarmed, lambda e: self._pub("disarmed", {"reason": e.reason})),
                           bus.subscribe(EV.BreakpointChanged, lambda e: self._pub(
                               "breakpoint", {"active": e.breakpoint is not None}))]
        if not self._hooked_queue:
            q, r, it = self.queue, self.runner, self.intake
            if q is not None:
                q.subscribe(lambda: self._pub("queue_changed", {"entries": len(q), **q.totals()}))
            if r is not None:
                r.sheet_listeners.append(lambda e, ok: self._pub("sheet_done", {"entry": _entry(e), "ok": bool(ok)}))
                r.subscribe(self._runner_changed)
            if it is not None:
                self._pending_ids = {j.id for j in it.pending}
                it.subscribe(self._intake_changed)
            self._hooked_queue = q is not None or r is not None or it is not None

    @staticmethod
    def _alarms(alarms: Any) -> list[dict]:
        return [{"code": str(getattr(a, "code", "")), "text": a.text_en if hasattr(a, "text_en") else str(a)}
                for a in (alarms or ())]

    def _on_started(self, ev: Any) -> None:
        if ev.tracked:
            self._pub("job_started", {"mode": ev.mode, "resume": bool(ev.resume),
                                      "source": RS.window_job_source(self.window, ev.mode), "job": self._job_name()})

    def _on_finished(self, ev: Any) -> None:
        if ev.tracked:
            self._pub("job_finished", {"mode": ev.mode, "ok": bool(ev.ok), "reason": ev.reason,
                                       "duration_s": round(float(ev.duration_s or 0.0), 1),
                                       "source": RS.window_job_source(self.window, ev.mode), "job": self._job_name()})

    def _job_name(self) -> str:
        path = getattr(self.window, "path", None)
        return os.path.basename(path) if path else ""

    def _runner_changed(self) -> None:
        r = self.runner
        if r is None:
            return
        st = r.state
        if st == "confirm" and self._runner_state != "confirm" and r.current is not None:
            self._pub("queue_waiting", {"next": _entry(r.current), "message": r.message})
        self._runner_state = st

    def _intake_changed(self) -> None:
        it, q = self.intake, self.queue
        if it is None:
            return
        now = {j.id for j in it.pending}
        approved = set()
        if q is not None:
            approved = {(e.extra.get("intake") or {}).get("intake_id") for e in q if isinstance(e.extra, dict)}
        for jid in self._pending_ids - now:
            self._pub("intake_approved" if jid in approved else "intake_rejected", {"id": jid})
        self._pending_ids = now

    # ------------------------------------------------------------------ status (GUI thread)
    def status_dict(self) -> dict:
        w = self.window
        ctl = w.ctl
        run = w.extension("run") if hasattr(w, "extension") else None
        out: dict = {"remote_control": False, "queue_intake": bool(self.settings["queue_intake"])}
        try:
            s = run.status() if run is not None else None
        except Exception:  # noqa: BLE001
            s = None
        if s is not None:
            out.update({"state": s.kind, "reason": run.reason_text(s), "mode": s.mode, "armed": bool(s.armed),
                        "laser_enabled": not s.dry_run, "simulator": bool(s.simulator),
                        "progress": round(float(s.progress), 4), "elapsed_s": round(float(s.elapsed_s), 1),
                        "eta_s": round(float(s.eta_s), 1) if s.eta_s is not None else None,
                        "part": s.part, "parts": s.parts, "contour": s.contour, "contours": s.contours,
                        "position_mm": [round(s.x, 2), round(s.y, 2), round(s.z, 2)],
                        "speed_mm_s": round(float(s.speed), 1), "breakpoint": bool(s.breakpoint)})
        else:
            st = ctl.state()
            out.update({"state": st.mode, "armed": bool(st.armed)})
        out["source"] = RS.job_source(ctl, "process", RS.practice_active(w))
        out["job"] = self._job_name()
        out["alarms"] = _alarm_list(w)
        q, r, it = self.queue, self.runner, self.intake
        if q is not None:
            out["queue"] = [_entry(e) for e in q][:200]
            out["queue_totals"] = q.totals()
        if r is not None:
            out["queue_runner"] = {"state": r.state, "current": _entry(r.current) if r.current is not None else None}
        if it is not None:
            out["pending_approval"] = [j.public() for j in it.pending][:200]
        return out

    def publish_status(self) -> None:
        srv = self.server
        if srv is None or not srv.running:
            return
        try:
            srv.publish_status(self.status_dict())
        except Exception as exc:  # noqa: BLE001 - the API must never disturb the UI
            log.debug("automation status failed: %s", exc)

    # ------------------------------------------------------------------ settings dialog
    def settings_dialog(self) -> Optional[QDialog]:
        d = ApiSettingsDialog(self)
        if getattr(self.window, "_test_no_dialogs", False):
            return d
        if d.exec():
            d.apply()
        return d


class ApiSettingsDialog(Dialog):
    def __init__(self, plugin: AutomationPlugin) -> None:
        w = plugin.window
        super().__init__(w, tr(w, "api.dlg.title", "Automation API"),
                         explanation=tr(w, "api.dlg.note", "Read-only status and events for the shop's own tools, "
                                        "and optional queue intake. Jobs from the API wait for your approval on the "
                                        "Run screen. The API cannot start, stop, hold, arm or move the machine."))
        self.p = plugin
        s = plugin.settings
        self.setObjectName("api_settings")
        f = self.form()
        self.host = QLineEdit(str(s["host"]))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(s["port"]))
        self.intake = QCheckBox(tr(w, "api.set.queue_intake", "Accept jobs for the queue (operator approval)"))
        self.intake.setChecked(bool(s["queue_intake"]))
        self.uploads = QCheckBox(tr(w, "api.set.uploads", "Accept uploaded drawings"))
        self.uploads.setChecked(bool(s["uploads"]))
        self.folders = QLineEdit(str(s["job_folders"]))
        self.folders.setPlaceholderText(tr(w, "api.dlg.folders_hint", "folders a job may be named from, ; separated"))
        self.token = QLineEdit(str(s["token"]))
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.Password)
        tok = QWidget()
        trow = S.hbox(tok)
        trow.addWidget(self.token, 1)
        for key, text, fn in (("show", tr(w, "api.dlg.show", "Show"), self._show),
                              ("copy", tr(w, "api.dlg.copy", "Copy"), self._copy),
                              ("new", tr(w, "api.dlg.new_token", "New token"), self._new)):
            b = S.button(text, on_click=fn)
            b.setObjectName(f"api_token_{key}")
            trow.addWidget(b)
        S.form_row(f, tr(w, "api.set.host", "Listen on (address)"), self.host)
        S.form_row(f, tr(w, "api.set.port", "Port"), self.port)
        S.form_row(f, tr(w, "api.set.token", "Token"), tok)
        S.form_row(f, "", self.intake)
        S.form_row(f, "", self.uploads)
        S.form_row(f, tr(w, "api.set.job_folders", "Job folders"), self.folders)
        self.state = QLabel()
        self.state.setObjectName("api_state")
        self.state.setWordWrap(True)
        srv = plugin.server
        self.state.setText(tr(w, "api.dlg.on", "Running: {url}", url=srv.url()) if srv is not None and srv.running
                           else tr(w, "api.dlg.off", "Not running (command: Automation API on/off)"))
        self.body.addWidget(self.state)

    def _show(self) -> None:
        self.token.setEchoMode(QLineEdit.Normal if self.token.echoMode() == QLineEdit.Password else QLineEdit.Password)

    def _copy(self) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self.token.text())

    def _new(self) -> None:
        self.token.setText(new_token())

    def apply(self) -> None:
        """Store the values; a running server restarts with them."""
        s = self.p.settings
        s["host"] = self.host.text().strip() or "127.0.0.1"
        s["port"] = int(self.port.value())
        s["queue_intake"] = self.intake.isChecked()
        s["uploads"] = self.uploads.isChecked()
        s["job_folders"] = self.folders.text().strip()
        if self.token.text():
            s["token"] = self.token.text()
        if self.p.server is not None and self.p.server.running:
            self.p.stop()
            self.p.set_enabled(True, confirm=False)


def register(ctx: Any) -> AutomationPlugin:
    p = AutomationPlugin(ctx)
    ctx.register_command("api.toggle", lambda on: p.set_enabled(bool(on)), label="api.cmd.toggle", checkable=True,
                         min_mode="engineer")
    ctx.register_command("api.settings", p.settings_dialog, label="api.cmd.settings", min_mode="engineer")
    return p


__all__ = ["register", "AutomationPlugin", "ApiSettingsDialog", "SETTINGS", "new_token"]
