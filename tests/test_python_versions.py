"""The Python versions the package says it supports are the ones CI tests and wheels are built
for (review 2026-11-09, fw.md F9: CI runs 3.14 and cibuildwheel builds cp314, but the trove
classifiers stopped at 3.13)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _ci_pythons() -> set[str]:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    test = ci["jobs"]["test"]["strategy"]["matrix"]
    found = set(re.findall(r"3\.\d+", str(test.get("python", ""))))
    for inc in test.get("include", []):
        if "python" in inc:
            found.add(str(inc["python"]))
    return found


def test_classifiers_name_every_tested_python():
    meta = _pyproject()["project"]
    classified = {c.rsplit("::", 1)[1].strip() for c in meta["classifiers"]
                  if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)}
    wheels = {f"3.{m}" for m in re.findall(r"cp3(\d+)-\*", " ".join(_pyproject()["tool"]["cibuildwheel"]["build"]))}
    tested = _ci_pythons()
    assert {"3.12", "3.13", "3.14"} <= tested
    assert classified == tested == wheels
    floor = meta["requires-python"].removeprefix(">=")
    assert floor == min(tested, key=lambda v: tuple(map(int, v.split("."))))
