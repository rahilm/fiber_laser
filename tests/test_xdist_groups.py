"""Under pytest-xdist (``-n N --dist loadgroup``, CI) the safety and sim tests run in timing
groups (tests/conftest.py): all of a group on ONE worker, one after another, so the real-time
bounds keep their meaning; ``timing`` (tests/safety/), ``timing-sim``, ``timing-gui``.  The
modules in ``UNTIMED_MODULES`` and the other tests spread over the workers (docs/dev/TESTING.md,
"Parallel runs")."""
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


TIMED = """
import os, pathlib, pytest

@pytest.mark.timing
@pytest.mark.parametrize("i", range({n}))
def test_t(i):
    out = pathlib.Path(os.environ["GROUP_OUT"])
    out.mkdir(exist_ok=True)
    (out / f"{tag}-{{i}}").write_text(os.environ.get("PYTEST_XDIST_WORKER", "none"))
"""


def test_each_timing_group_shares_one_worker(tmp_path):
    tests = tmp_path / "tests"
    (tests / "safety").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "conftest.py", tests / "conftest.py")
    (tests / "safety" / "test_a_safety.py").write_text(BODY.format(n=6, tag="safety"))
    (tests / "test_card_scaling.py").write_text(BODY.format(n=6, tag="sim"))      # a SIM_MODULES name, timing-sim
    (tests / "test_large_drawing.py").write_text(BODY.format(n=6, tag="gui"))     # timing-gui
    (tests / "test_close_save.py").write_text(TIMED.format(n=3, tag="gui-marked"))  # ``timing`` marker, timing-gui
    (tests / "safety" / "test_policy.py").write_text(BODY.format(n=24, tag="untimed"))   # UNTIMED_MODULES
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
    assert len(got) == 6 + 6 + 6 + 3 + 24 + 24, got

    def workers(*tags):
        return {w for k, w in got.items() if k.rsplit("-", 1)[0] in tags}
    for group in (("safety",), ("sim",), ("gui", "gui-marked")):
        assert len(workers(*group)) == 1 and "none" not in workers(*group), (group, got)
    # the rest (an untimed module of tests/safety/ too) is not pinned to one worker
    assert len(workers("untimed")) > 1 and len(workers("unit")) > 1, got
