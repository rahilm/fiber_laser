"""Controller-side plugin boundary (Safety-core track; UI-side trust model: test_plugin_trust.py).
Review 2 P1-8: a plugin cannot reach emission while disarmed through a public route.

An "evil" plugin is registered through :func:`mlaser.services.plugins.register_plugin` against
a stub host with a real UiController on the simulator (the probe of the review,
``plugin_probe_ctl.py``).  Every public (non-underscore) route it used is closed now:

* ``ctx.controller`` is a read-only view (controller.plugin_view): no ``sup``, ``fsm``, ``mc``;
* the card handle returns plain functions (``card().transact.__self__`` gone) and is read-only;
* the real controller (``ctx.window.ctl``) has no ``sup`` / ``mc``; ``laser_enabled`` is
  read-only, ``fsm.dry_run`` cannot be lifted, the channel's guard is sealed;
* ``arm()`` from a plugin needs the plugin policy (refused and audited without one).

Python cannot sandbox in-process code (private attributes, ``gc``, an own socket to the card):
that boundary is documented (docs/SAFETY.md §3a), not tested here.
"""
from __future__ import annotations

import sys
import types

import pytest

from mlaser.controller.supervisor import CommandRefused
from mlaser.services.commands import CommandRegistry
from mlaser.services.plugins import Hooks, PluginInfo, register_plugin


class StubHost:
    def __init__(self, ctl, docs=None):
        self.commands = CommandRegistry()
        self.hooks = Hooks()
        self.docs = docs
        self.config = None
        self.jobs = None
        self.ctl = ctl
        self.tr_ = None
        self.logs: list = []

    def log(self, m):
        self.logs.append(m)


def _sim_emits(ctl) -> bool:
    st = ctl.sim.state
    return bool(st.pwm_on or st.laser or any(st.da) or st.outputs >> 1 & 1)


@pytest.fixture
def setup():
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(laser_enabled=False, poll_s=0.05,
                       output_map={"gas": 1, "shutter": 2, "red_light": 3})
    assert ctl.connect_simulator(require_homed=False)
    yield ctl
    ctl.disconnect()


def _register(host, fn, name="evil"):
    mod = types.ModuleType(f"{name}_plugin")
    mod.register = fn
    sys.modules[f"{name}_plugin"] = mod
    try:
        return register_plugin(host, PluginInfo(name, f"{name}_plugin", "entry-point"))
    finally:
        sys.modules.pop(f"{name}_plugin", None)


def _attempts(ctx):
    """Every public route the review's probe used; returns those that were NOT closed."""
    reached = []
    view = ctx.controller
    real = ctx.window.ctl

    def tryit(name, fn):
        try:
            fn()
            reached.append(name)
        except (AttributeError, CommandRefused, PermissionError, TypeError):
            pass

    card = view.card()
    tryit("card.set_pwm", lambda: card.set_pwm(5000, 50))
    tryit("card.set_output(shutter)", lambda: card.set_output(2, True))
    tryit("card.transact.__self__", lambda: card.transact.__self__)
    tryit("card.guard", lambda: card.guard)
    tryit("card setattr", lambda: setattr(card, "x", 1))
    for obj, label in ((view, "view"), (real, "ctl")):
        tryit(f"{label}.sup", lambda o=obj: o.sup)
        tryit(f"{label}.mc", lambda o=obj: o.mc.transact.__self__)
        tryit(f"{label}.laser_enabled=True", lambda o=obj: setattr(o, "laser_enabled", True))
        tryit(f"{label}.require_homed=False", lambda o=obj: setattr(o, "require_homed", False))
        tryit(f"{label}.max_buffer_ms", lambda o=obj: setattr(o, "max_buffer_ms", 60000.0))
        tryit(f"{label}.interlocks.cfg", lambda o=obj: setattr(o.interlocks, "cfg", None))
        tryit(f"{label}.interlocks.cfg.follower", lambda o=obj: setattr(o.interlocks.cfg, "follower", False))
    tryit("view.fsm", lambda: view.fsm)
    tryit("ctl.fsm.dry_run=False", lambda: setattr(real.fsm, "dry_run", False))
    tryit("ctl.card().transact.__self__", lambda: real.card().transact.__self__)
    mc = real._mc                                       # private: by design reachable, but sealed
    tryit("mc.guard=None", lambda: setattr(mc, "guard", None))
    tryit("view.set_plugin_policy", lambda: view.set_plugin_policy(lambda *a: None))
    if view.arm("plugin wants to cut"):
        reached.append("view.arm")
    tryit("card.set_pwm after arm", lambda: card.set_pwm(5000, 50))
    tryit("card.command PWM", lambda: card.command([9999, 3, 5000, 80, 1]))
    tryit("card.fifo emission", lambda: card.set_da(1, 5000))
    return reached


def test_malicious_plugin_cannot_reach_emission_while_disarmed(setup):
    ctl = setup
    host = StubHost(ctl)
    host.ctl = ctl
    out: dict = {}

    def evil(ctx):
        ctx.host.ctl = ctl                              # ctx.window is the host
        host.__dict__["ctl"] = ctl
        out["reached"] = _attempts(ctx)
        return "ok"
    ctx, res = _register(host, evil)
    assert res == "ok", host.logs
    assert out["reached"] == []
    assert not ctl.fsm.armed and ctl.fsm.dry_run
    assert not _sim_emits(ctl)


def test_plugin_arm_needs_the_policy_and_is_audited(caplog):
    from mlaser.controller.ui_adapter import UiController
    ctl = UiController(poll_s=0.05)
    assert ctl.connect_simulator(require_homed=False)
    try:
        host = StubHost(ctl)
        got: dict = {}

        def plug(ctx):
            got["ctx"] = ctx
        _register(host, plug, "helper")
        view = got["ctx"].controller
        caplog.set_level("INFO", logger="mlaser.audit")
        assert not view.arm("test") and not ctl.fsm.armed          # no policy: refused
        assert any("plugin_refused" in r.getMessage() for r in caplog.records)
        assert not view.reset_fault() or not ctl.fsm.fault
        asked = []
        ctl.set_plugin_policy(lambda src, act, why: asked.append((src, act)) or
                              (None if act == "arm" else "no"))
        assert view.arm("allowed by the operator") and ctl.fsm.armed
        assert ("plugin:helper", "arm") in asked
        assert any("arm" in r.getMessage() and "plugin:helper" in r.getMessage() for r in caplog.records)
        ctl.disarm("test")
        # the operator path is not a plugin: the policy is not asked
        n = len(asked)
        assert ctl.arm(reason="operator dialog") and len(asked) == n
        ctl.set_plugin_policy(lambda *a: 1 / 0)                     # a broken policy refuses
        ctl.disarm("test")
        assert not view.arm()
    finally:
        ctl.disconnect()


def test_plugin_cannot_replace_the_document_during_a_job():
    from mlaser.machine.demo import DemoController
    from mlaser.model import Document
    from mlaser.services.document import DocumentService
    d = DemoController()
    d.connect()
    docs = DocumentService(Document())
    host = StubHost(d, docs)
    got: dict = {}
    _register(host, lambda ctx: got.setdefault("ctx", ctx), "docs")
    ctx = got["ctx"]
    d.fsm.job_started(emits=False)                     # a job is active
    with pytest.raises(RuntimeError):
        ctx.replace_document(Document())
    d.fsm.job_ended()
    ctx.replace_document(Document())


def test_card_handle_hides_the_channel_object():
    from mlaser.controller.supervisor import CardHandle, SafetySupervisor
    from mlaser.machine.safety import SafetyFSM
    import gc
    sup = SafetySupervisor(SafetyFSM())
    h = CardHandle(sup)
    with pytest.raises(AttributeError):
        h._sup                                         # no instance dict entry any more
    assert not any(r is sup for r in gc.get_referents(h))


def test_guard_of_a_channel_is_sealed():
    from mlaser.controller.client import MotionController
    mc = MotionController(guard=lambda req: None)
    with pytest.raises(AttributeError):
        mc.guard = None
    fresh = MotionController()
    fresh.guard = lambda req: None                     # the first assignment seals it
    with pytest.raises(AttributeError):
        fresh.guard = lambda req: None
