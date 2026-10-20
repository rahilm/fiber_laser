"""The packaging contract, checked without mlaser installed."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_matches_pyproject():
    man = tomllib.loads((ROOT / "mlaser_example" / "mlaser-plugin.toml").read_text(encoding="utf-8"))
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (name, value), = py["project"]["entry-points"]["mlaser.plugins"].items()
    assert man["plugin"]["id"] == name and man["plugin"]["entry"] == value
    assert man["plugin"]["version"] == py["project"]["version"]
    assert "mlaser-plugin.toml" in py["tool"]["setuptools"]["package-data"]["mlaser_example"]
