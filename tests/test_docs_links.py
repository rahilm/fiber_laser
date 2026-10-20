"""Documentation links: every relative Markdown link (file and #anchor) in docs/, README.md and
the top-level SECURITY / CONTRIBUTING / CHANGELOG resolves, every ``docs/...md`` path named in the code exists, and the packaged help ships the
files :mod:`mlaser.diag.helpdocs` lists (docs/dev/TESTING.md).

Run alone with ``pytest tests/test_docs_links.py -q``; the failure message lists every broken
link as ``file:line: target``."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mlaser.diag import helpdocs as H  # noqa: E402

DOCS = ROOT / "docs"

#: documents moved in Wave 3 (docs restructure): old path -> new path
MOVED = {f"docs/{n}.md": f"docs/re/{n}.md" for n in (
    "cam", "craft", "devices", "diag", "draw", "engrave", "formats", "motion", "nc", "nest", "parity",
    "protocol", "ui")}
MOVED["docs/BRIEF.md"] = "docs/re/history/BRIEF.md"

#: files that may still name a moved document by its old path (its MOVED entry is the new one):
#: the parity fence is changed only together with a Wine parity re-run, and the other files
#: belong to tracks working in parallel; each line is removed when the file is updated
OLD_PATH_ALLOWED = (
    "mlaser/parity_ports/",                                  # frozen ports: changed only with a parity re-run
)
#: docs/review/ux.md shows the review screenshots, which live outside the repository
#: (work/review/shots, docs/review/REVIEW.md)
EXTERNAL_IMAGES = ("docs/review/",)


def _md_files():
    files = [ROOT / "README.md", ROOT / "packaging" / "README.md"]
    files += [ROOT / n for n in ("SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md") if (ROOT / n).exists()]
    files += sorted(DOCS.rglob("*.md"))
    return files


def _anchors(path: Path, cache={}):
    if path not in cache:
        cache[path] = H.anchors(path.read_text(encoding="utf-8"))
    return cache[path]


def test_markdown_links_resolve():
    bad = []
    for f in _md_files():
        md = f.read_text(encoding="utf-8")
        for line, target in H.links(md):
            if H.is_external(target):
                continue
            path, _, anchor = target.partition("#")
            dest = (f.parent / path).resolve() if path else f
            where = f"{f.relative_to(ROOT)}:{line}: {target}"
            if not dest.exists():
                if f.relative_to(ROOT).as_posix().startswith(EXTERNAL_IMAGES) and path.startswith("shots/"):
                    continue
                bad.append(where + " (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                bad.append(where + " (no such heading)")
    assert not bad, "broken links:\n" + "\n".join(bad)


_DOC_PATH = re.compile(r"docs/[A-Za-z0-9_./-]+?\.md")


def test_doc_paths_named_in_code_exist():
    bad = []
    for base in ("mlaser", "tools", "tests", "packaging"):
        for p in sorted((ROOT / base).rglob("*")):
            if p.suffix not in (".py", ".json", ".spec", ".toml", ".md", ".sh", ".ps1") or not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel == "tests/test_docs_links.py" or "/data/" in rel:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for m in _DOC_PATH.finditer(text):
                name = m.group(0)
                if (ROOT / name).exists():
                    continue
                if name in MOVED and rel.startswith(OLD_PATH_ALLOWED):
                    continue
                line = text.count("\n", 0, m.start()) + 1
                bad.append(f"{rel}:{line}: {name}" + (f" (now {MOVED[name]})" if name in MOVED else ""))
    for f in (ROOT / "pyproject.toml", ROOT / ".github" / "workflows" / "ci.yml"):
        for m in _DOC_PATH.finditer(f.read_text(encoding="utf-8")):
            if not (ROOT / m.group(0)).exists():
                bad.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, "doc paths that do not exist:\n" + "\n".join(bad)


def test_moved_documents_are_gone_from_the_old_place():
    for old, new in MOVED.items():
        assert not (ROOT / old).exists() and (ROOT / new).exists(), (old, new)


def test_help_ships_the_listed_docs_and_the_hook_agrees():
    sys.path.insert(0, str(ROOT / "tools"))
    import help_build
    assert tuple(help_build.SHIPPED) == H.SHIPPED and tuple(help_build.EXCLUDED) == H.EXCLUDED
    files = H.shipped_files(str(DOCS))
    assert "user/getting-started.md" in files and "re/protocol.md" in files and "SAFETY.md" in files
    assert not any(f.startswith(("review/", "re/history/")) for f in files)


def test_slugs_match_github():
    assert H.slugify("8. Verifying against your machine") == "8-verifying-against-your-machine"
    assert H.slugify("`Craft.extra` & friends (P2-4)") == "craftextra--friends-p2-4"
    assert H.slugify("Nesting (排样)") == "nesting-排样"
    md = "# A\n## B\n## B\n```\n# not a heading\n```\n"
    assert [h[3] for h in H.headings(md)] == ["a", "b", "b-1"]


def test_links_skip_code():
    md = "see [x](a.md#b) and `[y](nope.md)`\n```\n[z](nope2.md)\n```\n![img](p.png \"t\")\n"
    assert H.links(md) == [(1, "a.md#b"), (5, "p.png")]
    assert H.resolve_link("re/cam.md", "../SAFETY.md#arming") == ("SAFETY.md", "arming")
    assert H.resolve_link("re/cam.md", "#top") == ("re/cam.md", "top")
    assert H.resolve_link("user/a.md", "./b.md") == ("user/b.md", "")
    assert H.resolve_link("user/a.md", "../re/nc.md") == ("re/nc.md", "")
