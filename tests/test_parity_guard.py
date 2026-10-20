"""tools/parity_guard.py: a PR changing tests/golden/ or mlaser/parity_ports/ needs the
``parity-change`` label or a ``Parity-Change:`` trailer (CONTRIBUTING.md)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import parity_guard as G  # noqa: E402

SIGN = "\nCo-Authored-By: Someone <a@b.c>"      # same trailer block


def test_protected_paths():
    assert G.protected_changes(["README.md", "mlaser/nc/compiler.py"]) == []
    assert G.protected_changes(["tests/golden/jobs.json", "mlaser\\parity_ports\\mainapp\\x.py", "a"]) == [
        "mlaser/parity_ports/mainapp/x.py", "tests/golden/jobs.json"]
    # a prefix match on the directory, not on a similarly named file
    assert G.protected_changes(["tests/golden_extra.py", "mlaser/parity_ports_old.py"]) == []


def test_trailers_parse_like_git():
    msg = "Subject\n\nBody text.\n\nParity-Change: the kerf fix changes 4 jobs\nCo-Authored-By: X <x@y>"
    t = G.trailers(msg)
    assert t["parity-change"] == ["the kerf fix changes 4 jobs"]
    assert "co-authored-by" in t
    # in the body, not in the trailer block: not a trailer
    assert G.trailers("Subject\n\nParity-Change: x\nmore prose here, not a trailer") == {}
    assert G.trailers("Parity-Change: only a subject") == {}
    # continuation line
    t = G.trailers("S\n\nParity-Change: first part\n  second part")
    assert t["parity-change"] == ["first part second part"]


def test_decide():
    golden = ["tests/golden/jobs_random.json", "README.md"]
    assert G.decide(["README.md"], [], [])[0]
    ok, why = G.decide(golden, [], ["Fix a thing\n" + SIGN])
    assert not ok and "tests/golden/jobs_random.json" in why and "parity-change" in why
    assert G.decide(golden, ["bug", "parity-change"], [])[0]
    assert G.decide(golden, [], ["Fix\n\nParity-Change: reviewed in #12" + SIGN])[0]
    # an empty trailer value does not count
    assert not G.decide(golden, [], ["Fix\n\nParity-Change:" + SIGN])[0]
    # the word in prose, or a trailer-like line in the body, does not count
    assert not G.decide(golden, [], ["Fix\n\nThis is not a Parity-Change: x, really.\n" + SIGN])[0]
    assert not G.decide(golden, [], ["Fix\n\nParity-Change: body\n\nCo-Authored-By: X <x@y>"])[0]


def test_labels():
    assert G.parse_labels('["parity-change", "docs"]') == ["parity-change", "docs"]
    assert G.parse_labels("a, parity-change") == ["a", "parity-change"]
    assert G.parse_labels("") == [] and G.parse_labels(None) == [] and G.parse_labels("[]") == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_end_to_end_git(tmp_path, monkeypatch):
    def git(*a):
        return subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "README.md").write_text("x\n")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "tests" / "golden").mkdir(parents=True)
    (tmp_path / "tests" / "golden" / "a.json").write_text("{}\n")
    git("add", ".")
    git("commit", "-q", "-m", "change the goldens")
    head = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PARITY_GUARD_LABELS", raising=False)
    assert G.main(["--base", base, "--head", head]) == 1
    assert G.main(["--base", base, "--head", head, "--labels", '["parity-change"]']) == 0
    git("commit", "-q", "--allow-empty", "-m", "declare\n\nParity-Change: regenerated after review")
    head2 = git("rev-parse", "HEAD")
    assert G.main(["--base", base, "--head", head2]) == 0
    assert G.main(["--base", "nonexistent", "--head", head2]) == 2
