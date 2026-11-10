"""The timing groups of tests/conftest.py stay what docs/dev/TESTING.md says they are: every module
they name exists (a renamed file would silently fall back to the ``timing`` group), no module is
in two of them, and a module run outside every timing group (``UNTIMED_MODULES``) shows none of
the signs of a real-time test: no clock read, no sleep, no ``flaky`` rerun, no card simulator
link.  A timing test added to such a module fails here until it is marked ``timing`` (or the
module leaves ``UNTIMED_MODULES``)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

#: the signs of a test that depends on real time
REAL_TIME = re.compile(r"\bsleep\(|\bmonotonic\(|\bperf_counter\(|\btime\.time\(|\bflaky\b|connect_simulator|"
                       r"CardSimulator|simulator_controller|\bmake_ctl\b|\bsafety_helpers\b|\bwait_until\(")


@pytest.fixture(scope="module")
def C(request):
    """tests/conftest.py as pytest loaded it (``import conftest`` may find tests/safety/'s)."""
    for plugin in request.config.pluginmanager.get_plugins():
        f = getattr(plugin, "__file__", None)
        if f and Path(f).resolve() == TESTS / "conftest.py":
            return plugin
    raise AssertionError("tests/conftest.py is not loaded")


def _module(stem: str) -> Path:
    found = sorted(TESTS.rglob(f"{stem}.py"))
    assert len(found) == 1, (stem, found)
    return found[0]


def test_every_named_module_exists_once(C):
    for stem in set().union(C.UNTIMED_MODULES, *C.TIMING_GROUPS.values()):
        _module(stem)


def test_the_groups_do_not_overlap(C):
    seen: dict[str, str] = {}
    for name, mods in C.TIMING_GROUPS.items():
        assert name != C.TIMING_GROUP
        for m in mods:
            assert m not in seen, (m, name, seen[m])
            seen[m] = name
    assert not set(seen) & C.UNTIMED_MODULES


def _untimed_sources(path: Path) -> list[tuple[str, str]]:
    """(name, source) of every function and class of ``path`` that is not marked ``timing``."""
    src = path.read_text(encoding="utf-8")
    out = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            marks = {ast.unparse(d) for d in node.decorator_list}
            if "pytest.mark.timing" in marks:
                continue
            out.append((node.name, ast.get_source_segment(src, node) or ""))
        elif not isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(("<module>", ast.get_source_segment(src, node) or ""))
    return out


def test_untimed_modules_show_no_real_time_dependence(C):
    hits = [f"{stem}.{name}: {m.group(0)}" for stem in sorted(C.UNTIMED_MODULES)
            for name, src in _untimed_sources(_module(stem)) for m in REAL_TIME.finditer(src)]
    assert not hits, ("a module in UNTIMED_MODULES (tests/conftest.py) looks like a real-time test; mark the "
                      f"test ``timing`` or take the module out of UNTIMED_MODULES: {hits}")


def test_the_guard_sees_a_real_time_test(tmp_path):
    """The check above is not vacuous: a sleep, a clock bound or the simulator is found."""
    p = tmp_path / "test_fake.py"
    p.write_text("import time\n\ndef test_a():\n    time.sleep(0.1)\n\n"
                 "def test_b(make_ctl):\n    pass\n\n"
                 "@pytest.mark.timing\ndef test_c():\n    t0 = time.perf_counter()\n")
    names = {name for name, src in _untimed_sources(p) if REAL_TIME.search(src)}
    assert names == {"test_a", "test_b"}
