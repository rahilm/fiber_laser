"""The public surface a plugin may import (docs/dev/PLUGIN_STANDARD.md §3), and the project's
own plugins keep to it (review 2026-11-09, arch.md A13: the Onshape plugin reached into
``mlaser.ui.units_qt``, ``mlaser.ui.responsive`` and the Orders extension)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: the modules a plugin may import (PLUGIN_STANDARD.md §3); a prefix ending in "." means the package
PUBLIC = ("mlaser.model", "mlaser.services.orders", "mlaser.services.parts", "mlaser.services.nesting",
          "mlaser.services.config", "mlaser.services.units", "mlaser.ui.plugin_widgets", "mlaser.io.")


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, [a.name for a in node.names], node.lineno
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, [], node.lineno


def _public(mod: str) -> bool:
    return any(mod.startswith(p) if p.endswith(".") else mod == p for p in PUBLIC)


@pytest.mark.parametrize("pkg", sorted(p for p in (ROOT / "plugins").glob("*/mlaser_*") if p.is_dir()),
                         ids=lambda p: p.name)
def test_project_plugins_import_only_the_public_api(pkg):
    bad = []
    for f in sorted(pkg.rglob("*.py")):
        for mod, names, line in _imports(f):
            if mod.split(".")[0] != "mlaser":
                continue
            ok = _public(mod) or (names and all(_public(f"{mod}.{n}") for n in names))
            if not ok or any(n.startswith("_") for n in names):
                bad.append(f"{f.relative_to(ROOT)}:{line}: {mod} {names}")
    assert bad == []


def test_plugin_widgets_names():
    pytest.importorskip("PySide6")
    from mlaser.ui import plugin_widgets as W
    from mlaser.ui import responsive, units_qt
    assert sorted(W.__all__) == ["ColumnFitter", "FlowLayout", "QuantitySpinBox", "WrapCheckBox",
                                 "on_units_changed"]
    assert W.QuantitySpinBox is units_qt.QuantitySpinBox and W.on_units_changed is units_qt.on_units_changed
    assert W.WrapCheckBox is responsive.WrapCheckBox and W.FlowLayout is responsive.FlowLayout
    assert W.ColumnFitter is responsive.ColumnFitter


def test_onshape_never_uses_the_window():
    """Load into Nest goes through ctx.services.orders_load_group, not ctx.window.extension()."""
    src = "\n".join(p.read_text(encoding="utf-8")
                    for p in (ROOT / "plugins" / "mlaser-onshape" / "mlaser_onshape").glob("*.py"))
    assert "ctx.window" not in src and ".extension(" not in src and "orders_load_group" in src
