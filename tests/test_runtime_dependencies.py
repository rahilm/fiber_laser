"""Every third-party package the application imports when it starts is a declared dependency
(fw review F1: ``platformdirs`` was imported at start but only installed through the dev
group, so ``pip install mlaser`` gave an app that died with ModuleNotFoundError).

The GUI is started the way ``python -m mlaser --demo --smoke-test`` starts it, in a separate
process without pytest (the configuration, data and log folders then take their real,
platformdirs-based paths), and the top-level modules it imported are mapped to their
distributions.  Each must be in ``[project] dependencies`` of pyproject.toml or required by one
of them; the dev and packaging groups do not count."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import atexit, json, os, sys
baseline = set(sys.modules)
out = sys.argv[1]
# the folders as a real start resolves them (no MLASER_* override, not under pytest): paths only
saved = {k: os.environ.pop(k) for k in ("MLASER_CONFIG_DIR", "MLASER_DATA_DIR", "MLASER_LOG_DIR")}
from mlaser.services.config import config_dir, data_dir
from mlaser.machine.logsetup import log_dir
config_dir(); data_dir(); log_dir()
os.environ.update(saved)                     # the start itself writes into the test's folders
def dump():
    new = sorted({m.split(".")[0] for m in set(sys.modules) - baseline})
    with open(out, "w") as f:
        json.dump(new, f)
atexit.register(dump)
# start-up questions (startup homing...) answer "no" at once: a modal box would outlive the
# smoke test's timer
from PySide6.QtWidgets import QDialog, QMessageBox
QDialog.exec = lambda self, *a: 0
QMessageBox.exec = lambda self, *a: int(QMessageBox.No)
from mlaser.__main__ import main
rc = main(["--demo", "--smoke-test", "2", "--new-instance"])
dump()
sys.exit(rc)
"""


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _requirement_name(req: str) -> str:
    import re
    return _norm(re.split(r"[\s<>=!~;\[(]", req.strip(), maxsplit=1)[0])


def _closure(names: set[str]) -> set[str]:
    """``names`` and everything they require (markers evaluated, extras not followed)."""
    from packaging.markers import Marker  # noqa: F401 - only to fail clearly when absent
    from packaging.requirements import Requirement
    seen: set[str] = set()
    todo = list(names)
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        seen.add(n)
        try:
            reqs = metadata.requires(n) or []
        except metadata.PackageNotFoundError:
            continue
        for r in reqs:
            req = Requirement(r)
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            todo.append(_norm(req.name))
    return seen


def test_start_imports_only_declared_dependencies(tmp_path):
    pytest.importorskip("packaging.requirements")
    declared = {_requirement_name(r) for r in tomllib.loads((ROOT / "pyproject.toml").read_text())
                ["project"]["dependencies"]}
    allowed = _closure(declared)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_DATA_DIR", "data"), ("MLASER_LOG_DIR", "log"),
                   ("MLASER_APP_DIR", "app"), ("MLASER_RUNTIME_DIR", "run")):
        env[k] = str(tmp_path / sub)
    env.update(QT_QPA_PLATFORM="offscreen", MLASER_NO_FIRSTRUN="1", MLASER_SINGLE_INSTANCE="0",
               MLASER_CARD_LOCK="0", MLASER_NO_INHIBIT="1", PYTHONPATH=str(ROOT))
    out = tmp_path / "modules.json"
    r = subprocess.run([sys.executable, "-c", PROBE, str(out)], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=200)
    assert r.returncode == 0, r.stderr[-3000:]
    mods = json.loads(out.read_text())
    stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    dists = metadata.packages_distributions()
    used: dict[str, set[str]] = {}
    for m in mods:
        if m in stdlib or m == "mlaser" or m == "__main__":
            continue
        for d in dists.get(m, ()):
            used.setdefault(_norm(d), set()).add(m)
    assert "platformdirs" in used, sorted(used)          # the path that used to fail was taken
    missing = {d: sorted(ms) for d, ms in used.items() if d not in allowed and d != "mlaser"}
    assert not missing, f"imported at start but not a declared dependency: {missing}"
