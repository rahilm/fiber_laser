"""Unit level: the MachineAPI is complete on both controllers, the FSM, the command policy, the
pre-flight check, the transport ownership."""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from mlaser.machine import api
from mlaser.machine import preflight as PF
from mlaser.machine.safety import Cmd, SafetyFSM, SafetyState, classify_mailbox, classify_request, verdict
from mlaser.protocol import frames as F

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------- MachineAPI
@pytest.mark.parametrize("cls_path", ["mlaser.machine.demo:DemoController",
                                      "mlaser.controller.ui_adapter:UiController"])
def test_both_controllers_implement_the_whole_api(cls_path):
    import importlib
    mod, name = cls_path.split(":")
    cls = getattr(importlib.import_module(mod), name)
    ctl = cls()
    assert isinstance(ctl, api.MachineAPI)
    for m in api.api_members():
        assert hasattr(ctl, m), f"{name} lacks {m}"
        proto = getattr(api.MachineAPI, m, None)
        if callable(proto) and not isinstance(proto, type):
            want = [p for p in inspect.signature(proto).parameters if p != "self"]
            have = inspect.signature(getattr(ctl, m)).parameters
            for p in want:
                assert p in have, f"{name}.{m} lacks parameter {p}"


def test_ui_has_no_hasattr_probing_of_the_controller():
    """The UI talks to the MachineAPI directly (architecture review §1.4: 26 probing sites)."""
    pat = re.compile(r"(hasattr|getattr)\((self\.)?(w\.|window\.|self\.ext\.w\.)?ctl\b")
    hits = []
    for p in sorted((ROOT / "mlaser" / "ui").rglob("*.py")):
        for n, line in enumerate(p.read_text("utf-8").splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p.name}:{n}: {line.strip()}")
    assert hits == []


def test_only_the_supervisor_holds_a_transport():
    """No direct ``ctl.mc`` / raw MotionController outside the supervisor and the streamer."""
    raw = []
    for p in sorted((ROOT / "mlaser").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        txt = p.read_text("utf-8")
        if "MotionController(" in txt and rel not in (
                "mlaser/controller/supervisor.py", "mlaser/controller/stream.py",
                "mlaser/controller/client.py", "mlaser/parity/synth.py", "mlaser/parity/capture.py"):
            raw.append(rel)
    assert raw == []
    mc_uses = []
    for pkg in ("ui", "diag", "devices", "report", "engrave"):
        for p in sorted((ROOT / "mlaser" / pkg).rglob("*.py")):
            for n, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                if re.search(r"\bctl\.mc\b|getattr\([^)]*\"mc\"\)", line):
                    mc_uses.append(f"{p.name}:{n}")
    assert mc_uses == []


def test_card_handle_hides_the_socket():
    from mlaser.controller.supervisor import CardHandle, SafetySupervisor
    sup = SafetySupervisor(SafetyFSM())
    h = CardHandle(sup)
    for name in ("close", "open", "guard", "_sock", "connect", "transact_raw"):
        with pytest.raises(AttributeError):
            getattr(h, name)
    with pytest.raises(AttributeError):
        h.guard = None


# ----------------------------------------------------------------------------- FSM
def test_fsm_transitions():
    seen = []
    fsm = SafetyFSM(lambda o, n, st, r: seen.append((o.value, n.value, r)))
    assert fsm.arm() == "arming refused: not connected"
    fsm.connected()
    assert fsm.state == SafetyState.DISARMED and not fsm.armed
    assert fsm.job_started(emits=True).startswith("start refused")
    assert fsm.job_started(emits=False) is None and fsm.state == SafetyState.RUNNING
    fsm.job_ended()
    assert fsm.arm() is None and fsm.state == SafetyState.ARMED
    assert fsm.job_started(emits=True) is None
    fsm.job_paused()
    assert fsm.state == SafetyState.PAUSED
    fsm.job_resumed()
    fsm.raise_fault("link lost")
    assert fsm.state == SafetyState.FAULT and not fsm.armed and fsm.fault == "link lost"
    fsm.raise_fault("later reason")
    assert fsm.fault == "link lost"                       # first reason wins
    assert fsm.arm().startswith("arming refused: fault")
    assert fsm.job_started(emits=False).startswith("start refused: fault")
    fsm.offline()
    fsm.connected()
    assert fsm.state == SafetyState.FAULT                 # latched across a reconnect
    assert fsm.reset() is None and fsm.state == SafetyState.DISARMED and not fsm.armed
    fsm.dry_run = True
    assert "dry-run" in fsm.arm()


# ----------------------------------------------------------------------------- policy
def _req(addr, values, func=F.FUNC_WRITE):
    return F.Request(func, addr, len(values), list(values))


@pytest.mark.parametrize("words,cls", [
    ([9999, 3, 5000, 50, 1], Cmd.EMISSION),
    ([9999, 17, 5000, 50, 1], Cmd.EMISSION),
    ([9999, 3, 5000, 0, 0], Cmd.HALT),
    ([9999, 4, 0, 5000], Cmd.EMISSION),
    ([9999, 4, 0, 0], Cmd.HALT),
    ([9999, 2, 1, 1], Cmd.EMISSION),            # output 1 = gas (guarded below)
    ([9999, 2, 4, 4], Cmd.IO),                  # output 3 = red light
    ([9999, 2, 0xFFFF, 0], Cmd.HALT),
    ([9999, 18, 1000], Cmd.EMISSION),
    ([9999, 5, 0, 0], Cmd.HALT),
    ([1, 0x1F, 2, 4000, 200000], Cmd.HALT),
    ([118, 5, 0], Cmd.HALT),
    ([101], Cmd.HALT),
    ([3, 0, 1000, 400, 4000, 50000], Cmd.MOTION),
    ([5, 3, 1000, 400, 4000, 1, 1, 0, 0], Cmd.MOTION),
    ([2, 3, 0], Cmd.MOTION),
])
def test_mailbox_classes(words, cls):
    assert classify_mailbox(words, guarded_outputs=0b11) == cls


def test_fifo_frame_classes():
    on = F.encode_step(1, 0, 5000, 50, 100)
    off = F.encode_step(1, 0, 5000, 0, 100)
    assert classify_request(_req(F.REG_FIFO_DATA, [1] + off + off)) == Cmd.MOTION
    assert classify_request(_req(F.REG_FIFO_DATA, [1] + off + on)) & Cmd.EMISSION
    assert classify_request(_req(F.REG_FIFO_DATA, [1] + off + F.encode_laser(5000, 40))) & Cmd.EMISSION
    assert classify_request(_req(F.REG_FIFO_CTRL, [F.FIFO_START])) == Cmd.MOTION
    assert classify_request(_req(F.REG_FIFO_CTRL, [F.FIFO_STOP])) == Cmd.HALT
    assert classify_request(_req(F.REG_STATUS_RO, [], F.FUNC_READ)) == Cmd.READ
    assert classify_request(_req(50000, [1])) == Cmd.CONFIG


def test_verdicts():
    S = SafetyState
    assert verdict(Cmd.EMISSION, S.DISARMED, False)
    assert verdict(Cmd.EMISSION, S.ARMED, True) is None
    assert verdict(Cmd.EMISSION, S.RUNNING, False)          # a laser-off job stays laser-off
    assert verdict(Cmd.MOTION, S.FAULT, False)
    assert verdict(Cmd.HALT, S.FAULT, False) is None
    assert verdict(Cmd.READ, S.OFFLINE, False) is None
    assert verdict(Cmd.CONFIG, S.RUNNING, True)


# ----------------------------------------------------------------------------- pre-flight
def test_preflight_report():
    from mlaser.model import Arc, Contour, Craft, Document, LayerParams, Line, Part
    doc = Document(parts=[Part([Contour([Line((0, 0), (10, 0))], False)], Craft(layer=1)),
                   Part([Contour([Arc((5, 5), (5, 5), (float("nan"), 5), True)], True)], Craft(layer=2))],
                   layers={1: LayerParams(1), 2: LayerParams(2, extra={"placeholder": True})})
    travel = PF.TravelLimits((0, 100), (0, 100))
    rep = PF.check("start", document=doc, extent=(-5, 0, 50, 50), travel=travel, homed=False,
                   emits=True, armed=False)
    assert {"not_homed", "not_armed", "layer_undefined", "bad_geometry", "open_contour",
            "out_of_travel"} <= rep.codes()
    assert not rep.ok
    rep = PF.check("frame", document=doc, extent=(0, 0, 50, 50), travel=travel, homed=True,
                   emits=False)
    # laser-off run: an undefined layer is a warning, not an error
    assert [i.severity for i in rep.issues if i.code == "layer_undefined"] == ["warning"]
    assert PF.check("move", target=(150, 0), travel=travel, homed=True).codes() == {"out_of_travel"}
