"""CI: a change of documentation only skips the test matrix's steps, not its checks, and still
runs the docs job (.github/workflows/ci.yml, job "changed files").

The job's script is run here with a stand-in ``gh`` that prints the changed files, the way the
compare API would.  A workflow-level ``paths-ignore`` is not used on purpose: a skipped workflow
leaves a required check pending, and a matrix job skipped by a job-level ``if`` reports its check
under the unexpanded name; so the matrix job always runs and only its steps are skipped.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_job_names_branch_protection_may_require_are_kept():
    jobs = _ci()["jobs"]
    assert jobs["test"]["name"] == "${{ matrix.os }} / Python ${{ matrix.python }}"
    assert jobs["architecture"]["name"] == "import contracts, types, i18n, docs"
    assert "lint" in jobs and "nightly-report" in jobs
    on = _ci().get("on", _ci().get(True))           # YAML 1.1 reads the key `on` as True
    for trigger in ("push", "pull_request"):
        assert not (on.get(trigger) or {}).get("paths-ignore") and not (on.get(trigger) or {}).get("paths")


def test_every_test_step_is_gated_and_the_docs_job_is_not():
    jobs = _ci()["jobs"]
    test = jobs["test"]
    assert test["needs"] == "changes" and "cancelled()" in test["if"]
    assert "needs.changes.outputs.code != 'false'" in test["env"]["RUN_TESTS"]
    steps = test["steps"]
    assert "RUN_TESTS != 'true'" in steps[0]["if"]                     # the notice
    for st in steps[1:]:
        assert "env.RUN_TESTS == 'true'" in st.get("if", ""), st.get("name", st.get("uses"))
    arch = jobs["architecture"]
    assert "needs" not in arch and "if" not in arch
    assert not any("RUN_TESTS" in str(st.get("if", "")) for st in arch["steps"])
    docs = [st for st in arch["steps"] if st.get("name") == "docs checks"]
    assert docs and "tests/test_docs_*.py" in docs[0]["run"] and "gen_keyboard_doc.py --check" in docs[0]["run"]


def _script() -> str:
    steps = _ci()["jobs"]["changes"]["steps"]
    return next(st for st in steps if st.get("id") == "classify")["run"]


@pytest.fixture
def run_classify(tmp_path):
    if sys.platform.startswith("win") or shutil.which("bash") is None:
        pytest.skip("needs a POSIX bash")

    def run(files, event="push", base="a" * 40, gh_fails=False, tests=None):
        work = tmp_path / f"w{len(list(tmp_path.iterdir()))}"
        (work / "bin").mkdir(parents=True)
        (work / "tests").mkdir()
        for name, text in (tests or {"test_x.py": "def test(): pass\n"}).items():
            (work / "tests" / name).write_text(text)
        gh = work / "bin" / "gh"
        listing = work / "files.txt"
        listing.write_text("".join(f + "\n" for f in files))
        gh.write_text("#!/bin/sh\nexit 1\n" if gh_fails else f"#!/bin/sh\ncat '{listing}'\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        out, summary = work / "out", work / "summary"
        env = dict(os.environ, PATH=f"{work / 'bin'}{os.pathsep}{os.environ['PATH']}", EVENT=event, BASE=base,
                   HEAD_SHA="b" * 40, GITHUB_REPOSITORY="o/r", GITHUB_OUTPUT=str(out),
                   GITHUB_STEP_SUMMARY=str(summary), GH_TOKEN="x")
        r = subprocess.run(["bash", "-c", _script()], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return out.read_text().strip()
    return run


def test_documentation_only_skips_the_tests(run_classify):
    assert run_classify(["docs/user/shell.md", "README.md", "docs/dev/RELEASE.md"]) == "code=false"
    assert run_classify(["docs/user/shell.md"], event="pull_request") == "code=false"


def test_anything_else_runs_them(run_classify):
    assert run_classify(["docs/user/shell.md", "mlaser/ui/mainwindow.py"]) == "code=true"
    assert run_classify([".github/workflows/ci.yml"]) == "code=true"
    assert run_classify(["docs/dev/plugin-repo-template/pyproject.toml"]) == "code=true"    # tested like code
    assert run_classify(["docs/dev/plugin-index.schema.json"]) == "code=true"


def test_a_document_a_test_reads_runs_them(run_classify):
    tests = {"test_doc.py": "# see docs/user/shell.md\nTEXT = (ROOT / 'docs/user/settings.md').read_text()\n"
                            'SEC = (ROOT / "SECURITY.md").read_text()\n'}
    assert run_classify(["docs/user/settings.md"], tests=tests) == "code=true"
    assert run_classify(["SECURITY.md"], tests=tests) == "code=true"
    assert run_classify(["docs/user/shell.md"], tests=tests) == "code=false"     # only named in a comment


def test_unknown_base_and_other_events_run_everything(run_classify):
    assert run_classify(["docs/user/shell.md"], base="0" * 40) == "code=true"       # a new branch
    assert run_classify(["docs/user/shell.md"], base="") == "code=true"
    assert run_classify(["docs/user/shell.md"], event="schedule") == "code=true"
    assert run_classify(["docs/user/shell.md"], event="workflow_dispatch") == "code=true"
    assert run_classify(["docs/user/shell.md"], gh_fails=True) == "code=true"       # the API failed
    assert run_classify([]) == "code=true"
    assert run_classify([f"docs/x{i}.md" for i in range(300)]) == "code=true"       # the API's cap
