"""The layer defaults reach the parity fence by injection (ConfigService.layer_defaults), not by
an import of formats.params from parity_ports (the last import-linter ignore is gone)."""
import ast
import inspect

import pytest

from mlaser.formats.params import layer_defs
from mlaser.parity_ports.mainapp import layer as L


def _registry():
    return {d.key: d.default_value for d in layer_defs() if "." in d.key}


def test_layer_module_imports_nothing_outside_the_fence():
    tree = ast.parse(inspect.getsource(L))
    mods = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if "formats" in m]


def test_nc_installs_the_registry_defaults():
    import mlaser.nc.process  # noqa: F401 - installs the fallback provider
    assert L.layer_defaults_installed()
    assert L._layer_defaults() == _registry()


def test_config_service_provides_identical_defaults(tmp_path):
    from mlaser.services.config import ConfigService
    old = L._DEFAULTS_PROVIDER
    try:
        svc = ConfigService(tmp_path / "c.json")
        assert L._DEFAULTS_PROVIDER == svc.layer_defaults
        assert L._layer_defaults() == _registry()
        v = L.LayerView(None)
        assert v["CutSpeed"] == _registry()["GP.CutSpeed"]
    finally:
        L.set_layer_defaults(old)


def test_missing_provider_is_a_clear_error():
    old = L._DEFAULTS_PROVIDER
    try:
        L.set_layer_defaults(None)
        with pytest.raises(RuntimeError, match="not injected"):
            L.LayerView(None)
    finally:
        L.set_layer_defaults(old)


def test_keep_existing_provider():
    old = L._DEFAULTS_PROVIDER
    try:
        L.set_layer_defaults(lambda: {"GP.X": 1})
        assert not L.set_layer_defaults(lambda: {"GP.X": 2}, replace_existing=False)
        assert L._layer_defaults() == {"GP.X": 1}
    finally:
        L.set_layer_defaults(old)
