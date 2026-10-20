"""Under pytest-xdist (``-n N --dist loadgroup``, CI) every safety and sim test runs on ONE
worker (xdist group "timing", tests/conftest.py), so the real-time bounds keep their meaning;
the other tests spread over the workers (docs/dev/TESTING.md, "Parallel runs")."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytest.importorskip("xdist")

BODY = '''
import os, pathlib, pytest

@pytest.mark.parametrize("i", range({n}))
def test_x(i):
    out = pathlib.Path(os.environ["GROUP_OUT"])
    out.mkdir(exist_ok=True)
    (out / f"{tag}-{{i}}").write_text(os.environ.get("PYTEST_XDIST_WORKER", "none"))
'''


def test_safety_and_sim_tests_share_one_worker(tmp_path):
    tests = tmp_path / "tests"
    (tests / "safety").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "conftest.py", tests / "conftest.py")
    (tests / "safety" / "test_a_safety.py").write_text(BODY.format(n=6, tag="safety"))
    (tests / "test_card_scaling.py").write_text(BODY.format(n=6, tag="sim"))      # a SIM_MODULES name
    (tests / "test_plain_unit.py").write_text(BODY.format(n=24, tag="unit"))
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    env = dict(os.environ, GROUP_OUT=str(tmp_path / "out"))
    env.pop("PYTEST_XDIST_WORKER", None)
    env.pop("PYTEST_ADDOPTS", None)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "3",
                        "--dist", "loadgroup", "-W", "ignore::pytest.PytestUnknownMarkWarning"],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    got = {p.name: p.read_text() for p in (tmp_path / "out").iterdir()}
    timing = {w for k, w in got.items() if k.startswith(("safety-", "sim-"))}
    assert len([k for k in got if k.startswith(("safety-", "sim-"))]) == 12
    assert len(timing) == 1 and "none" not in timing, got
    # the rest is not pinned to that worker
    assert {w for k, w in got.items() if k.startswith("unit-")} - timing, got
