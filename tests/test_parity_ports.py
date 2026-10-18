"""The parity fence (mlaser.parity_ports): @port markers, __port__ records, ParityProfile.

* every ``@port`` symbol is reachable through the default profile (so the pipeline can only
  use a port through a profile slot);
* every port module carries a ``__port__`` record;
* the old domain names of ported entry points dispatch through the active profile;
* a derived profile replaces a slot without touching the ports.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest

import mlaser.parity_ports as PP
from mlaser.parity_ports import ORIGINAL, Implementation, profile as prof

_INFRA = {"mlaser.parity_ports", "mlaser.parity_ports._registry", "mlaser.parity_ports.profile"}
_OPTIONAL = "mlaser.parity_ports.profiles."


def _port_modules():
    mods = []
    for m in pkgutil.walk_packages(PP.__path__, "mlaser.parity_ports."):
        mod = importlib.import_module(m.name)
        # parity_ports.profiles holds optional non-default profiles (new behaviour, not ports)
        if m.name not in _INFRA and not m.ispkg and not m.name.startswith(_OPTIONAL):
            mods.append(mod)
    return mods


def _resolve(rec):
    obj = importlib.import_module(rec.module)
    for part in rec.qualname.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_port_module_has_a_record():
    mods = _port_modules()
    assert mods
    for mod in mods:
        rec = getattr(mod, "__port__", None)
        assert isinstance(rec, dict), f"{mod.__name__} has no __port__ record"
        assert rec.get("source") and rec.get("functions"), mod.__name__


def test_every_port_symbol_is_reachable_through_the_default_profile():
    _port_modules()
    reachable = {}
    for slot, impl in ORIGINAL.slots().items():
        for name in impl.names():
            reachable[id(getattr(impl, name))] = f"{slot}.{name}"
    recs = PP.registered()
    assert recs
    missing = [r.name for r in recs if id(_resolve(r)) not in reachable]
    assert not missing, f"@port symbols not reachable through ORIGINAL: {missing}"


def test_every_profile_target_is_a_port():
    """The default profile points only at marked ports (not at domain code)."""
    for slot, impl in ORIGINAL.slots().items():
        for name in impl.names():
            obj = getattr(impl, name)
            assert hasattr(obj, "__port__"), f"{slot}.{name} is not marked with @port"
            assert obj.__module__.startswith("mlaser.parity_ports."), (slot, name)


def test_slots_satisfy_their_interfaces():
    for slot, impl in ORIGINAL.slots().items():
        for name in prof.interface_names(prof.INTERFACES[slot]):
            assert callable(getattr(impl, name)), (slot, name)


def test_profile_selection_and_derived_profiles():
    from mlaser.motion import interp

    calls = []
    fake = ORIGINAL.motion.with_(seg_interp=lambda *a, **k: calls.append(a) or [0.0, 1.0])
    p = ORIGINAL.derive("test-fake-motion", "test", motion=fake)
    assert p.smoother is ORIGINAL.smoother
    assert PP.active_profile() is ORIGINAL
    with PP.use_profile(p):
        assert interp.seg_interp(10.0, 100.0, 0.0, 0.0, 1000.0, 10000.0) == [0.0, 1.0]
    assert calls and PP.active_profile() is ORIGINAL
    assert interp.seg_interp(10.0, 100.0, 0.0, 0.0, 1000.0, 10000.0)[-1] == 1.0
    with pytest.raises(TypeError):
        Implementation("motion", plan_velocity=print)        # incomplete slot
    with pytest.raises(KeyError):
        PP.get_profile("no-such-profile")
    assert PP.get_profile(PP.DEFAULT) is ORIGINAL


def test_domain_names_dispatch_through_the_profile():
    from mlaser.motion import interp, planner, profile, smooth

    for fn in (interp.seg_interp, interp.seg_interp_time, interp.arc_interp, planner.plan_velocity,
               profile.make_profile, profile.scurve_profile, smooth.smooth_path_points):
        assert hasattr(fn, "__parity_slot__"), fn
    # types and helpers are the port's own objects
    from mlaser.parity_ports.cadmodule import planner as port_planner
    assert planner.PathPoint is port_planner.PathPoint
    assert planner._point_limits is port_planner._point_limits
