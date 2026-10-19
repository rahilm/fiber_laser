"""Help: packaged-docs lookup, the search index (Qt-free) and the help browser window."""
import os
from pathlib import Path

import pytest

from mlaser.diag import helpdocs as H

ROOT = Path(__file__).resolve().parents[1]


def _tree(tmp_path):
    d = tmp_path / "docs"
    (d / "user").mkdir(parents=True)
    (d / "re" / "history").mkdir(parents=True)
    (d / "review").mkdir()
    (d / "README.md").write_text("# Docs\n\nSee [start](user/getting-started.md#first-start).\n")
    (d / "user" / "README.md").write_text("# User guide\n\n[Safety](safety.md)\n")
    (d / "user" / "getting-started.md").write_text(
        "# Getting started\n\nInstall it.\n\n## First start\n\nRun the demo with the simulator.\n"
        "\n## Simulator\n\nThe simulator simulator simulator replays the FIFO.\n")
    (d / "user" / "safety.md").write_text("# Safety\n\nArm the laser. Back to [start](getting-started.md)."
                                          " External [site](https://example.org). Missing [x](../review/r.md)\n")
    (d / "re" / "protocol.md").write_text("# Card protocol\n\nFIFO frames and the simulator.\n")
    (d / "re" / "history" / "BRIEF.md").write_text("# Brief\n\nsimulator\n")
    (d / "review" / "r.md").write_text("# Review\n")
    (d / "SAFETY.md").write_text("# Safety architecture\n\nstate machine\n")
    return d


def test_docs_root_prefers_env_then_package_then_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_HELP_DIR", str(tmp_path))
    assert H.docs_root() == str(tmp_path)
    monkeypatch.delenv("MLASER_HELP_DIR")
    root = H.docs_root()
    assert root in (H.PKG_DOCS, H.REPO_DOCS) and os.path.isdir(root)


def test_topics_sections_and_exclusions(tmp_path):
    d = _tree(tmp_path)
    ts = H.topics(str(d))
    assert [t.rel for t in ts] == ["README.md", "user/README.md", "user/getting-started.md",
                                   "user/safety.md", "SAFETY.md", "re/protocol.md"]
    assert [t.section for t in ts] == ["", "user", "user", "user", "dev", "re"]
    assert ts[2].title == "Getting started"


def test_search_ranks_and_requires_every_word(tmp_path):
    d = _tree(tmp_path)
    idx = H.HelpIndex(H.topics(str(d)))
    hits = idx.search("simulator")
    assert hits[0].topic.rel == "user/getting-started.md" and hits[0].anchor == "simulator"
    assert {h.topic.rel for h in hits} == {"user/getting-started.md", "re/protocol.md"}   # not history
    both = idx.search("demo simulator")
    assert [(h.topic.rel, h.anchor) for h in both] == [("user/getting-started.md", "first-start")]
    assert both[0].line == 7 and "demo" in both[0].snippet
    assert idx.search("nothing-like-this") == [] and idx.search("  ") == []
    # the old tuple API still works on the real docs
    assert H.search("simulator") and all(len(r) == 3 for r in H.search("simulator"))


def test_real_docs_search_finds_the_user_guide():
    hits = H.index().search("arming")
    assert hits and any(h.topic.rel.startswith("user/") for h in hits[:5])


@pytest.fixture
def browser(qapp_or_none, tmp_path):
    from mlaser.ui.help_browser import HelpBrowser
    b = HelpBrowser(docs_dir=str(_tree(tmp_path)))
    yield b
    b.close()
    b.deleteLater()


@pytest.fixture
def qapp_or_none():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_browser_links_anchors_and_history(browser):
    b = browser
    assert b.current == "README.md" and "See start" in b.view.toPlainText()
    b._link(__import__("PySide6.QtCore", fromlist=["QUrl"]).QUrl("user/getting-started.md#first-start"))
    assert b.current == "user/getting-started.md" and "first-start" in b._anchors
    assert b.tree.currentItem().text(0) == "Getting started"
    b.open("user/safety.md")
    from PySide6.QtCore import QUrl
    b._link(QUrl("../review/r.md"))
    assert b.current == "user/safety.md" and "not part of the installed help" in b.status.text()
    b.back()
    assert b.current == "user/getting-started.md"
    b.back()
    assert b.current == "README.md" and not b.back_btn.isEnabled()
    b.forward()
    assert b.current == "user/getting-started.md" and b.fwd_btn.isEnabled()
    b.home()
    assert b.current == "README.md"


def test_browser_search_opens_hit_with_highlight(browser):
    b = browser
    b.search.setText("simulator")
    hits = b.do_search()
    assert b.side.currentIndex() == 1 and b.results.count() == len(hits) == 3
    assert "3 results" in b.results_label.text()
    b._open_hit(b.results.item(0))
    assert b.current == "user/getting-started.md"
    assert len(b.view.extraSelections()) >= 4
    assert b.view.textCursor().block().text().startswith("The simulator")
    b.search.setText("zzzz")
    assert b.do_search() == [] and "No results" in b.results_label.text()
    b.search.clear()
    assert b.side.currentIndex() == 0


def test_browser_sections_and_about(browser):
    b = browser
    tops = [b.tree.topLevelItem(i).text(0) for i in range(b.tree.topLevelItemCount())]
    assert tops == ["Docs", "User guide", "Developer", "Reverse engineering", "About"]
    b.tree.setCurrentItem(b.tree.topLevelItem(1))          # a section opens its index page
    assert b.current == "user/README.md" and b.tree.topLevelItem(1).childCount() == 2
    b.tree.setCurrentItem(b.tree.topLevelItem(4))
    assert "omitted" in b.view.toPlainText()
