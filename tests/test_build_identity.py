"""Build identity: version from the metadata, the commit from git / the generated _build_info.py
(review F11 item 2, A8 item 1)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from mlaser import _buildid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import help_build  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_identity():
    _buildid.build_identity.cache_clear()
    yield
    _buildid.build_identity.cache_clear()


def test_the_hook_and_the_runtime_ask_git_the_same_question():
    assert tuple(help_build.GIT_DESCRIBE) == tuple(_buildid.GIT_DESCRIBE)


def test_parse_describe():
    assert _buildid.parse_describe("1a2b3c4d5e6f\n") == ("1a2b3c4d5e6f", False)
    assert _buildid.parse_describe("1a2b3c4d5e6f-dirty") == ("1a2b3c4d5e6f", True)


def test_checkout_reports_its_commit():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode != 0:
        pytest.skip("git unavailable")
    ident = _buildid.build_identity()
    assert ident.source == "git"
    assert head.stdout.strip().startswith(ident.commit) and len(ident.commit) >= 7
    assert ident.version and ident.commit in ident.text()


def test_a_folder_that_is_not_a_repository_root_has_no_commit(tmp_path):
    # e.g. an unpacked sdist: it must never take the commit of a repository around it
    assert _buildid.git_identity(str(tmp_path)) is None
    assert help_build.git_identity(str(tmp_path)) is None


def test_generated_file_is_read_when_there_is_no_checkout(monkeypatch):
    monkeypatch.setattr(_buildid, "git_identity", lambda root, timeout=3.0: None)
    mod = types.ModuleType("mlaser._build_info")
    mod.COMMIT, mod.DIRTY = "0123456789ab", True
    monkeypatch.setitem(sys.modules, "mlaser._build_info", mod)
    ident = _buildid.build_identity()
    assert (ident.commit, ident.dirty, ident.source) == ("0123456789ab", True, "build")
    assert ident.text().endswith("(commit 0123456789ab, modified)")


def test_unknown_without_checkout_or_generated_file(monkeypatch):
    monkeypatch.setattr(_buildid, "git_identity", lambda root, timeout=3.0: None)
    monkeypatch.setitem(sys.modules, "mlaser._build_info", None)       # import fails
    ident = _buildid.build_identity()
    assert (ident.commit, ident.source) == ("unknown", "unknown")
    assert ident.text().endswith("(commit unknown)")


def test_version_comes_from_the_package_metadata(monkeypatch):
    from importlib import metadata
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    assert _buildid.build_identity().version == "9.9.9"


def test_write_build_info_without_git(tmp_path):
    out = tmp_path / "pkg" / "_build_info.py"
    assert help_build.write_build_info(str(out), str(tmp_path)) == "unknown"
    ns: dict = {}
    exec(out.read_text("utf-8"), ns)
    assert ns["COMMIT"] == "unknown" and ns["DIRTY"] is False
    # an sdist's copy is kept when git is not available (the wheel built from the sdist)
    out.write_text(help_build.build_info_text("feedc0ffee12", False), "utf-8")
    assert help_build.write_build_info(str(out), str(tmp_path), keep_existing=True) is None
    assert "feedc0ffee12" in out.read_text("utf-8")


def test_write_build_info_replaces_a_hard_link(tmp_path):
    # sdist's release tree hard-links the source files: writing through the link would change
    # the checkout's own file
    src = tmp_path / "source.py"
    src.write_text("COMMIT = 'old'\n", "utf-8")
    link = tmp_path / "tree" / "_build_info.py"
    link.parent.mkdir()
    try:
        os.link(src, link)
    except OSError:
        pytest.skip("no hard links here")
    help_build.write_build_info(str(link), str(tmp_path / "tree"))
    assert src.read_text("utf-8") == "COMMIT = 'old'\n"
    assert "unknown" in link.read_text("utf-8")


def test_generated_file_is_git_ignored_and_not_committed():
    assert "mlaser/_build_info.py" in (ROOT / ".gitignore").read_text("utf-8").splitlines()
    if (ROOT / ".git").exists():
        r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "mlaser/_build_info.py"], capture_output=True,
                           text=True)
        assert r.returncode != 0 or r.stdout.strip() == ""


def test_pyproject_wires_both_hooks():
    import tomllib
    cmd = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["tool"]["setuptools"]["cmdclass"]
    assert cmd["build_py"] == "tools.help_build.BuildPy" and cmd["sdist"] == "tools.help_build.Sdist"


def test_log_header_names_version_and_commit(tmp_path, monkeypatch):
    from mlaser.machine import logsetup
    monkeypatch.setattr(_buildid, "git_identity", lambda root, timeout=3.0: ("abcdef123456", False))
    d = tmp_path / "log"
    try:
        logsetup.setup_logging(d)
        for h in logging.getLogger("mlaser").handlers:
            h.flush()
        main = (d / "mlaser.log").read_text("utf-8")
        audit = [json.loads(ln) for ln in (d / "audit.log").read_text("utf-8").splitlines() if ln.strip()]
    finally:
        logsetup.teardown_logging(d)
    ident = _buildid.build_identity()
    assert f"mlaser {ident.version} (commit abcdef123456)" in main
    sess = [a for a in audit if a.get("event") == "session"]
    assert sess and sess[0]["commit"] == "abcdef123456" and sess[0]["version"] == ident.version
    assert sess[0]["python"] and sess[0]["os"]
