"""The notifications plugin in the real application wiring: commands, settings, the dialog's
test send to a local webhook, and the subscriptions (controller events, queue runner,
maintenance check)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from test_notify import FakeHTTP  # noqa: E402


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(QDialog, "exec", lambda self, *a: 0)
    from wiring import real_window
    w = real_window()
    yield w
    w.close()


def test_plugin_wiring_and_test_send(win, tmp_path):
    from mlaser.machine import events as EV
    w = win
    p = w.extension("ext_notify")
    assert p is not None
    assert "notify.settings" in w.commands and "notify.test" in w.commands
    # subscriptions: the controller's bus, the queue runner, the maintenance check
    assert p.wiring._bus is w.ctl.events
    runner = w.hooks.provider("queue_runner")()
    assert p.wiring._sheet_done in runner.sheet_listeners
    assert p.wiring.maintenance in w.extension("ext_diag").svc.maintenance_listeners
    srv = FakeHTTP()
    try:
        d = p.open_settings()                       # exec() stubbed: the dialog stays for the test
        d.fields["webhook_enabled"].setChecked(True)
        d.fields["webhook_url"].setText(srv.url + "/hook")
        d.secret_fields["webhook"].setText("sig-secret")
        assert d.send_test()                        # saves, then queues the test
        assert p.notifier.flush(60)
        assert srv.got.wait(10)
        d.show_results()
        assert "webhook" in d.result.text()
    finally:
        srv.close()
    body = json.loads(srv.requests[0]["body"])
    assert body["event"] == "test"
    # the secret is in the secret store, not in the configuration
    assert p.secrets.get("notify.webhook_secret") == "sig-secret"
    assert "sig-secret" not in json.dumps(w.config.values, default=str)
    assert w.config.get("plugin.ext_notify.webhook_url") == srv.url + "/hook"
    # the demo controller is a simulator: its jobs are not notified by default
    p.settings["enabled"] = True
    n0 = len(p.notifier.results) + p.notifier.counts["suppressed"] + p.notifier.counts["queued"]
    w.ctl.events.publish(EV.JobStarted("process", "x"))
    w.ctl.events.publish(EV.JobFinished("process", True, "done"))
    QApplication.processEvents()
    assert len(p.notifier.results) + p.notifier.counts["suppressed"] + p.notifier.counts["queued"] == n0
    w.run("notify.test")
