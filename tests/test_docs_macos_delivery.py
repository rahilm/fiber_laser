"""macOS delivery of the unsigned app (review 2026-11-09, fw.md F8): the network self-test names
the Local Network permission on macOS, and the user-facing texts describe the Gatekeeper path
of macOS 15 (Open Anyway) instead of the right-click > Open that no longer bypasses it."""
from __future__ import annotations

from pathlib import Path

import pytest

from mlaser import netcheck as N

ROOT = Path(__file__).resolve().parent.parent


def test_macos_hint_names_the_local_network_permission():
    c = N.firewall_hints(platform="darwin", exe="/x/mlaser", frozen=True, which=lambda t: None)
    assert "Local Network" in c.hint and "Privacy & Security" in c.hint
    assert "application firewall" in c.hint          # the firewall hint stays


@pytest.mark.parametrize("plat", ["win32", "linux"])
def test_other_platforms_have_no_local_network_hint(plat):
    c = N.firewall_hints(platform=plat, exe="/x/mlaser", frozen=True, which=lambda t: None)
    assert "Local Network" not in c.hint


@pytest.mark.parametrize("doc", ["README.md", "docs/dev/RELEASE.md"])
def test_gatekeeper_instructions_match_macos_15(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "Open Anyway" in text and "Local Network" in text
    assert "right-click > Open" not in text
