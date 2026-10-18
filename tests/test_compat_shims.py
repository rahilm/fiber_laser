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
}


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
            elif isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            bad += [f"{rel}:{n.lineno} {m}" for m in names if m in MOVED]
    assert not bad, bad
