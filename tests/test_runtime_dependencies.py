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
import atexit, faulthandler, json, os, sys
baseline = set(sys.modules)
# a start that does not end shows where every thread waits (the test's timeout is 200 s)
faulthandler.dump_traceback_later(120, repeat=True)
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
    if "multiprocessing" in sys.modules:
        print("probe: children at exit:", sys.modules["multiprocessing"].active_children(),
              file=sys.stderr, flush=True)
atexit.register(dump)
# start-up questions (startup homing...) answer "no" at once: a modal box would outlive the
# smoke test's timer
from PySide6.QtWidgets import QDialog, QMessageBox
QDialog.exec = lambda self, *a: 0
QMessageBox.exec = lambda self, *a: int(QMessageBox.No)
from mlaser.__main__ import main
rc = main(["--demo", "--smoke-test", "2", "--new-instance"])
print("probe: main returned", rc, file=sys.stderr, flush=True)
dump()
sys.exit(rc)
"""


def _probe(args: list[str], tmp_path: Path, env: dict, tag: str) -> tuple[int, str]:
    """Run the probe with its output in files (a child process that kept a pipe open would hold
    a pipe reader too); on a timeout, fail with what the start printed so far."""
    o, e = tmp_path / f"{tag}.out", tmp_path / f"{tag}.err"
    with open(o, "wb") as fo, open(e, "wb") as fe:
        p = subprocess.Popen([sys.executable, "-c", *args], cwd=str(tmp_path), env=env, stdout=fo, stderr=fe)
        try:
            rc = p.wait(timeout=200)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            pytest.fail(f"the start did not end in 200 s\n--- stdout:\n"
                        f"{o.read_text('utf-8', 'replace')[-3000:]}\n--- stderr:\n"
                        f"{e.read_text('utf-8', 'replace')[-12000:]}")
    return rc, e.read_text("utf-8", "replace")


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
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {_requirement_name(r) for r in project["dependencies"]}
    allowed = _closure(declared)
    # an optional extra (the nester) is imported behind a guard, and only where it is installed
    # (CI installs every extra): its packages are declared too, they are just not required
    optional = _closure({_requirement_name(r) for reqs in project.get("optional-dependencies", {}).values()
                         for r in reqs})
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    for k, sub in (("MLASER_CONFIG_DIR", "cfg"), ("MLASER_DATA_DIR", "data"), ("MLASER_LOG_DIR", "log"),
                   ("MLASER_APP_DIR", "app"), ("MLASER_RUNTIME_DIR", "run")):
        env[k] = str(tmp_path / sub)
    env.update(QT_QPA_PLATFORM="offscreen", MLASER_NO_FIRSTRUN="1", MLASER_SINGLE_INSTANCE="0",
               MLASER_CARD_LOCK="0", MLASER_NO_INHIBIT="1", PYTHONPATH=str(ROOT))
    out = tmp_path / "modules.json"
    rc, err = _probe([PROBE, str(out)], tmp_path, env, "start")
    assert rc == 0, err[-3000:]
    mods = json.loads(out.read_text())
    stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    dists = metadata.packages_distributions()
    used: dict[str, set[str]] = {}
    for m in mods:
        if m in stdlib or m == "mlaser" or m == "__main__":
            continue
        used[m] = {_norm(d) for d in dists.get(m, ())}
    assert "platformdirs" in used, sorted(used)          # the path that used to fail was taken
    # a module is covered when one of the distributions that provide it is declared (several can
    # provide the same top-level package: PySide6-Essentials and PySide6-Addons both do)
    missing = {m: sorted(ds) for m, ds in used.items()
               if ds and "mlaser" not in ds and not ds & allowed and not ds & optional}
    assert not missing, f"imported at start but not a declared dependency: {missing}"
    # and nothing the start REQUIRES may come from an extra only: checked where no extra is installed
    required_from_extra = {m: sorted(ds) for m, ds in used.items()
                           if ds and not ds & allowed and "mlaser" not in ds and ds & optional}
    for m in required_from_extra:
        grc, gerr = _probe([f"import sys; sys.modules[{m!r}] = None\n" + PROBE, str(out)], tmp_path, env,
                           f"guard-{m}")
        assert grc == 0, f"the start needs the optional {m}: {gerr[-2000:]}"
