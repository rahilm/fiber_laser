"""Pessimistic simulator, fault injection, audit log files, --real failing loudly."""
from __future__ import annotations

import json
import time

from mlaser.controller.client import MotionController, TransportConfig
from mlaser.controller.simulator import CardSimulator
from mlaser.protocol import frames as F

from safety_helpers import wait_until


def test_pessimistic_simulator_keeps_running_and_holds_the_beam():
    sim = CardSimulator(pessimistic=True, halt_lag_ms=50).start()
    try:
        with MotionController(config=TransportConfig(host="127.0.0.1", port=sim.port)) as mc:
            mc.fifo_clear()
            st = mc.read_status()
            frames = F.build_fifo_frames([F.encode_step(10, 0, 5000, 100, 5000) for _ in range(2000)],
                                         st.fifo_seq + 1)
            for fr in frames[:10]:
                mc.fifo_write_frame(fr)
            mc.fifo_start()
            time.sleep(0.2)
            mc.fifo_stop()
            x = sim.position(0)
            time.sleep(0.02)
            assert sim.position(0) > x                  # still executing buffered items
            assert wait_until(lambda: not sim.state.fifo_running, 1.0) is not None
            assert sim.beam_on                          # no implicit laser-off on a halt
            mc.command(F.mailbox_sys(F.SYS_PWM, 5000, 0, 0))
            assert not sim.beam_on
    finally:
        sim.stop()


def test_fault_injection_api():
    sim = CardSimulator().start()
    try:
        # a 200 ms reply timeout: the 20 ms latency injected below must not exceed it on a host
        # whose sleeps run long (macOS runner VMs: a 20 ms sleep takes 30-40 ms; with 30 ms the
        # delayed reply timed out, "no reply")
        cfg = TransportConfig(host="127.0.0.1", port=sim.port, timeout_ms=200, fifo_time_ms=400, max_recv=1)
        with MotionController(config=cfg) as mc:
            mc.read_status()
            sim.inject(link_down=True)
            try:
                mc.read_status()
                raise AssertionError("no error with the link down")
            except Exception as e:  # noqa: BLE001
                assert "no reply" in str(e)
            assert sim.faults.dropped >= 1
            sim.clear_faults()
            sim.inject(latency_ms=20)
            # perf_counter: time.monotonic() ticks in 15.6 ms steps on Windows before 3.13
            t0 = time.perf_counter()
            mc.read_reg(1001)
            assert time.perf_counter() - t0 >= 0.02
    finally:
        sim.stop()


def test_audit_log_records_commands_arming_and_faults(make_ctl, tmp_path):
    from mlaser.machine import logsetup
    d = logsetup.setup_logging(tmp_path)
    try:
        ctl = make_ctl(arm=False)
        assert ctl.arm()
        ctl.card().set_pwm(5000, 0)
        ctl.raise_fault("audit test")
        time.sleep(0.1)
    finally:
        logsetup.teardown_logging(tmp_path)
    recs = [json.loads(line) for line in (d / "audit.log").read_text("utf-8").splitlines()]
    events = {r["event"] for r in recs}
    assert {"connect", "arm", "command", "fault", "halt"} <= events
    assert (d / "mlaser.log").exists() and (d / "crash.log").exists()


def test_real_mode_fails_loudly(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from mlaser.ui import __main__ as M
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    ini = tmp_path / "ipAdd.ini"
    ini.write_text("CardIP=127.0.0.1\nCardPort=9\nMCTimeout=20\nMCFifoTime=40\nMCMaxRecvTime=1\n")
    from mlaser.machine import lifecycle
    try:
        assert M.main(["--real", "--ini", str(ini), "--log-dir", str(tmp_path / "log")]) == 2
    finally:
        lifecycle.uninstall()
