"""Notifications plugin: sends "sheet done", "load the next sheet", alarms / faults, "job finished"
and "maintenance due" through ntfy, e-mail or a webhook (docs/user/notifications.md).

The logic is Qt-free in :mod:`mlaser.notify`; this module holds the settings
(``plugin.ext_notify.*`` in the ConfigService), the secret store (keyring, else
``secrets.json`` with permissions 0600 next to ``config.json``), the wiring to the controller's
events, the queue runner and the maintenance check, and the settings dialog with **Send test**.

Send-only: a notification never controls the machine.  Every send happens on the notifier's
worker thread; an event handler only queues (never blocks the GUI or the streamer thread).
Simulator, dry-run and practice jobs are skipped unless *Include simulator runs* is on.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from mlaser.notify import EVENTS, SECRETS, SETTING_DEFAULTS, EventWiring, Notifier, SecretStore
from mlaser.notify.core import SECURITY
from mlaser.services.config import Setting

from .workspaces import TOUCH_MIN, tr

#: setting -> (i18n id, English label); the form shows them in this order per group
LABELS = {
    "enabled": ("notify.set.enabled", "Send notifications"),
    "machine_name": ("notify.set.machine_name", "Machine name (title prefix)"),
    "include_simulator": ("notify.set.include_simulator", "Include simulator, dry-run and practice runs"),
    "ntfy_enabled": ("notify.set.ntfy_enabled", "ntfy"),
    "ntfy_server": ("notify.set.ntfy_server", "ntfy server"),
    "ntfy_topic": ("notify.set.ntfy_topic", "ntfy topic"),
    "email_enabled": ("notify.set.email_enabled", "E-mail"),
    "smtp_host": ("notify.set.smtp_host", "SMTP server"),
    "smtp_port": ("notify.set.smtp_port", "SMTP port"),
    "smtp_security": ("notify.set.smtp_security", "Connection security"),
    "smtp_user": ("notify.set.smtp_user", "SMTP user (empty: no login)"),
    "email_from": ("notify.set.email_from", "Sender address"),
    "email_to": ("notify.set.email_to", "Recipients (comma separated)"),
    "webhook_enabled": ("notify.set.webhook_enabled", "Webhook"),
    "webhook_url": ("notify.set.webhook_url", "Webhook URL"),
    "min_interval_s": ("notify.set.min_interval_s", "Same notification at most every"),
    "max_per_hour": ("notify.set.max_per_hour", "At most per hour (all notifications)"),
    "quiet_start": ("notify.set.quiet_start", "Quiet from (HH:MM, empty: off)"),
    "quiet_end": ("notify.set.quiet_end", "Quiet until (HH:MM)"),
    "quiet_alarms": ("notify.set.quiet_alarms", "Alarms also during quiet hours"),
}
EVENT_LABELS = {
    "sheet_done": ("notify.event.sheet_done", "Sheet done"),
    "queue_waiting": ("notify.event.queue_waiting", "Queue waits for the next sheet"),
    "alarm": ("notify.event.alarm", "Alarm or fault"),
    "job_finished": ("notify.event.job_finished", "Job finished or stopped"),
    "maintenance_due": ("notify.event.maintenance_due", "Maintenance due"),
}
SECRET_LABELS = {
    "ntfy": ("notify.secret.ntfy", "ntfy access token"),
    "email": ("notify.secret.email", "SMTP password"),
    "webhook": ("notify.secret.webhook", "Webhook signing secret"),
}
SECURITY_LABELS = (("notify.security.starttls", "STARTTLS"), ("notify.security.ssl", "SSL/TLS"),
                   ("notify.security.none", "None (local relay only)"))
GROUPS = (("notify.group.general", "General", ("enabled", "machine_name", "include_simulator")),
          ("notify.group.ntfy", "ntfy", ("ntfy_enabled", "ntfy_server", "ntfy_topic")),
          ("notify.group.email", "E-mail (SMTP)", ("email_enabled", "smtp_host", "smtp_port", "smtp_security",
                                                   "smtp_user", "email_from", "email_to")),
          ("notify.group.webhook", "Webhook", ("webhook_enabled", "webhook_url")),
          ("notify.group.limits", "Limits and quiet hours", ("min_interval_s", "max_per_hour", "quiet_start",
                                                             "quiet_end", "quiet_alarms")))
GROUP_SECRET = {"notify.group.ntfy": "ntfy", "notify.group.email": "email", "notify.group.webhook": "webhook"}


def settings_schema() -> list[Setting]:
    out = []
    for key, default in SETTING_DEFAULTS.items():
        label = LABELS.get(key, (f"notify.set.{key}", key))[0]
        if key == "smtp_security":
            out.append(Setting(key, "enum", default, options=SECURITY, label=label))
        elif key == "smtp_port":
            out.append(Setting(key, "int", default, min=1, max=65535, label=label))
        elif key == "min_interval_s":
            out.append(Setting(key, "float", default, unit="s", min=0.0, max=86400.0, label=label))
        elif key == "max_per_hour":
            out.append(Setting(key, "float", default, min=0.0, max=3600.0, label=label))
        elif isinstance(default, bool):
            out.append(Setting(key, "bool", default, label=label))
        else:
            out.append(Setting(key, "str", default, label=label))
    return out


class NotifyPlugin:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.window = w = ctx.window
        self.settings = ctx.settings_schema(settings_schema())
        from mlaser.services.config import config_dir
        self.secrets = SecretStore(config_dir() / "secrets.json")
        self.notifier = Notifier(self.settings, self.secrets)
        from mlaser.services.runsource import window_job_source
        self.wiring = EventWiring(self.notifier, source=lambda mode: window_job_source(w, mode),
                                  include_simulator=lambda: bool(self.settings["include_simulator"]),
                                  text=lambda key, default="", **kw: tr(w, key, default, **kw),
                                  machine=lambda: str(self.settings["machine_name"] or ""))
        self.dialog: Optional[NotifyDialog] = None
        self.attach()
        ctx.on_controller_changed(lambda _c: self.attach())
        ctx.on_close(self.close)
        diag = w.extension("ext_diag") if hasattr(w, "extension") else None
        svc = getattr(diag, "svc", None)
        if svc is not None and hasattr(svc, "maintenance_listeners"):
            svc.maintenance_listeners.append(self.wiring.maintenance)
        ctx.register_command("notify.settings", self.open_settings, label="notify.cmd.settings", min_mode="engineer")
        ctx.register_command("notify.test", self.send_test, label="notify.cmd.test", min_mode="engineer")

    def attach(self) -> None:
        ctl = getattr(self.window, "ctl", None)
        self.wiring.attach_bus(getattr(ctl, "events", None))
        hooks = getattr(self.window, "hooks", None)
        fn = hooks.provider("queue_runner") if hooks is not None else None
        try:
            runner = fn() if fn is not None else None
        except Exception:  # noqa: BLE001 - no run workspace: no queue notifications
            runner = None
        if runner is not None:
            self.wiring.attach_runner(runner)

    def send_test(self) -> bool:
        ok = self.wiring.test()
        if not ok:
            self.ctx.log(tr(self.window, "notify.msg.no_channel", "Notifications: no channel is enabled"))
        else:
            self.ctx.log(tr(self.window, "notify.msg.test_queued", "Notifications: test sent (see the result in "
                            "Notification settings)"))
        return ok

    def open_settings(self) -> "NotifyDialog":
        self.dialog = NotifyDialog(self)
        self.dialog.exec()
        return self.dialog

    def close(self) -> None:
        self.wiring.detach()
        self.notifier.close()


class NotifyDialog(QDialog):
    """Channel, event and limit settings; the secrets (never shown); **Send test**."""

    def __init__(self, plugin: NotifyPlugin) -> None:
        super().__init__(plugin.window)
        self.plugin = plugin
        w = plugin.window
        self.setObjectName("notify_dialog")
        self.setWindowTitle(tr(w, "notify.title", "Notifications"))
        outer = QVBoxLayout(self)
        intro = QLabel(tr(w, "notify.intro", "Messages about the machine to a phone or a mailbox. They only "
                          "inform: nothing can be controlled by a notification or a reply."))
        intro.setWordWrap(True)
        outer.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.fields: dict[str, QWidget] = {}
        self.secret_fields: dict[str, QLineEdit] = {}
        s = plugin.settings
        for gid, gtext, keys in GROUPS:
            box = QGroupBox(tr(w, gid, gtext))
            form = QFormLayout(box)
            for key in keys:
                lid, ltext = LABELS[key]
                form.addRow(tr(w, lid, ltext), self._field(key, s[key]))
            ch = GROUP_SECRET.get(gid)
            if ch is not None:
                e = QLineEdit()
                e.setObjectName(f"notify_secret_{ch}")
                e.setEchoMode(QLineEdit.Password)
                e.setPlaceholderText(tr(w, "notify.secret.stored", "(stored: leave empty to keep)")
                                     if plugin.secrets.has(SECRETS[ch]) else "")
                self.secret_fields[ch] = e
                sid, stext = SECRET_LABELS[ch]
                form.addRow(tr(w, sid, stext), e)
            bl.addWidget(box)
        ev = QGroupBox(tr(w, "notify.group.events", "Events (channels: all, or a list such as ntfy, email)"))
        ef = QFormLayout(ev)
        for kind in EVENTS:
            row = QHBoxLayout()
            cb = QCheckBox()
            cb.setObjectName(f"notify_event_{kind}")
            cb.setChecked(bool(s[f"event_{kind}"]))
            self.fields[f"event_{kind}"] = cb
            le = QLineEdit(str(s[f"channels_{kind}"] or "all"))
            le.setObjectName(f"notify_channels_{kind}")
            self.fields[f"channels_{kind}"] = le
            row.addWidget(cb)
            row.addWidget(le, 1)
            lid, ltext = EVENT_LABELS[kind]
            ef.addRow(tr(w, lid, ltext), row)
        bl.addWidget(ev)
        bl.addWidget(QLabel(tr(w, "notify.secret.where", "Secrets are kept in: {where}",
                               where=plugin.secrets.backend if plugin.secrets.backend == "keyring"
                               else str(plugin.secrets.path))))
        self.result = QLabel()
        self.result.setObjectName("notify_result")
        self.result.setWordWrap(True)
        outer.addWidget(self.result)
        row = QHBoxLayout()
        self.test_btn = QPushButton(tr(w, "notify.btn.test", "Send test"))
        self.test_btn.setObjectName("notify_test")
        self.test_btn.setMinimumHeight(TOUCH_MIN)
        self.test_btn.clicked.connect(self.send_test)
        row.addWidget(self.test_btn)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        bb.accepted.connect(self._save_close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        outer.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.show_results)
        self.timer.start()
        self.show_results()
        self.resize(640, 720)

    def _field(self, key: str, value: Any) -> QWidget:
        w = self.plugin.window
        f: QWidget
        if key == "smtp_security":
            cb = QComboBox()
            for tid, text in SECURITY_LABELS:
                cb.addItem(tr(w, tid, text))
            cb.setCurrentIndex(int(value or 0))
            f = cb
        elif key == "smtp_port":
            sp = QSpinBox()
            sp.setRange(1, 65535)
            sp.setValue(int(value))
            f = sp
        elif key in ("min_interval_s", "max_per_hour"):
            ds = QDoubleSpinBox()
            ds.setRange(0.0, 86400.0 if key == "min_interval_s" else 3600.0)
            ds.setDecimals(0)
            if key == "min_interval_s":
                ds.setSuffix(" s")
            ds.setValue(float(value))
            f = ds
        elif isinstance(SETTING_DEFAULTS[key], bool):
            c = QCheckBox()
            c.setChecked(bool(value))
            f = c
        else:
            f = QLineEdit(str(value or ""))
        f.setObjectName(f"notify_{key}")
        self.fields[key] = f
        return f

    def save(self) -> None:
        s = self.plugin.settings
        for key, f in self.fields.items():
            if isinstance(f, QCheckBox):
                s[key] = f.isChecked()
            elif isinstance(f, QComboBox):
                s[key] = f.currentIndex()
            elif isinstance(f, (QSpinBox, QDoubleSpinBox)):
                s[key] = f.value()
            elif isinstance(f, QLineEdit):
                s[key] = f.text().strip()
        for ch, e in self.secret_fields.items():
            if e.text():
                self.plugin.secrets.set(SECRETS[ch], e.text())
                e.clear()
        cfg = getattr(self.plugin.window, "config", None)
        if cfg is not None and hasattr(cfg, "flush"):
            cfg.flush()

    def _save_close(self) -> None:
        self.save()
        self.accept()

    def send_test(self) -> bool:
        self.save()
        return self.plugin.send_test()

    def show_results(self) -> None:
        w = self.plugin.window
        res = list(self.plugin.notifier.results)[-4:]
        if not res:
            self.result.setText(tr(w, "notify.result.none", "No notification sent yet."))
            return
        lines = []
        for r in reversed(res):
            ok = tr(w, "notify.result.ok", "sent") if r.ok else tr(w, "notify.result.failed", "FAILED")
            lines.append(f"{time.strftime('%H:%M:%S', time.localtime(r.t))}  {r.kind} → {r.channel}: {ok} ({r.detail})")
        self.result.setText("\n".join(lines))

    def done(self, r: int) -> None:
        self.timer.stop()
        super().done(r)


def register(ctx: Any) -> NotifyPlugin:
    return NotifyPlugin(ctx)


__all__ = ["register", "NotifyPlugin", "NotifyDialog", "settings_schema"]
