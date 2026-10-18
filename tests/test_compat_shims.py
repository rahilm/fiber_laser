"""Old import paths left by the Wave 1 structure moves (mlaser/_compat.py).

Every old path must still import, be the very same module object as the new one (so private
names and monkeypatching keep working), and warn once with a DeprecationWarning naming the new
path.
"""
from __future__ import annotations

import importlib
import sys
import warnings

import pytest

# old path -> new path (docs/dev/ARCHITECTURE.md, "Moved modules")
MOVED = {
    "mlaser.controller.frames": "mlaser.protocol.frames",
    "mlaser.io.geom": "mlaser.geom.curves",
    "mlaser.controller.process": "mlaser.nc.process",
    "mlaser.nc.stream": "mlaser.controller.stream",
    "mlaser.formats.lang": "mlaser.i18n.langfile",
    "mlaser.cam.items": "mlaser.toolpath.items",
}
# modules that stayed but lost names: module -> the names that moved away
PARTIAL = {
    "mlaser.cam.geometry": {n for n in dir(importlib.import_module("mlaser.geom.contour"))
                            if not n.startswith("__")} - {"SplineCurve", "CurveRef"},
    "mlaser.devices.follower": {"make_follower_card_simulator"},
    "mlaser.ui.facade": {"OUTPUTS", "MachineState", "ControllerFacade"},
}


def test_cam_geometry_forwards_the_contour_helpers():
    """cam.geometry keeps SplineCurve/CurveRef; the contour helpers moved to geom.contour and
    are forwarded with a warning."""
    from mlaser.cam import geometry
    from mlaser.geom import contour

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert geometry.sub_path is contour.sub_path
        assert geometry.signed_area is contour.signed_area
    assert any("mlaser.geom.contour.sub_path" in str(w.message) for w in seen)
    with pytest.raises(AttributeError):
        geometry.no_such_name  # noqa: B018


@pytest.mark.parametrize("old,new", sorted(MOVED.items()))
def test_old_path_is_an_alias(old, new):
    new_mod = importlib.import_module(new)
    sys.modules.pop(old, None)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        old_mod = importlib.import_module(old)
    assert old_mod is new_mod
    assert sys.modules[old] is new_mod
    parent, _, leaf = old.rpartition(".")
    assert getattr(importlib.import_module(parent), leaf) is new_mod
    msgs = [str(w.message) for w in seen if issubclass(w.category, DeprecationWarning)]
    assert any(old in m and new in m for m in msgs), msgs


def test_no_internal_module_uses_an_old_path():
    """The package itself imports only new paths (the shims are for external scripts)."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "mlaser"
    shims = {str(root / (o.removeprefix("mlaser.").replace(".", "/") + ".py")) for o in MOVED}
    bad = []
    for f in root.rglob("*.py"):
        if str(f) in shims:
            continue
        rel = f.relative_to(root.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
            pkg = parts
        else:
            pkg = parts[:-1]
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = []
            if isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    up = pkg[: len(pkg) - (n.level - 1)]
                    base = ".".join(up + ([n.module] if n.module else []))
                names = [base] + [f"{base}.{a.name}" for a in n.names]
                if base in PARTIAL:
                    bad += [f"{rel}:{n.lineno} {base}.{a.name}" for a in n.names
                            if a.name in PARTIAL[base]]
            elif isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            bad += [f"{rel}:{n.lineno} {m}" for m in names if m in MOVED]
    assert not bad, bad


def test_follower_card_simulator_forwarder():
    from mlaser.controller.follower_sim import make_follower_card_simulator
    from mlaser.devices import follower

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert follower.make_follower_card_simulator is make_follower_card_simulator
    assert any("mlaser.controller.follower_sim" in str(w.message) for w in seen)


def test_ui_facade_forwards_the_machine_api():
    from mlaser.machine import api
    from mlaser.ui import facade

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert facade.ControllerFacade is api.ControllerFacade
        assert facade.MachineState is api.MachineState
        assert facade.OUTPUTS is api.OUTPUTS
    assert sum("mlaser.machine.api" in str(w.message) for w in seen) == 3
    assert issubclass(facade.DemoController, api.ControllerFacade)
