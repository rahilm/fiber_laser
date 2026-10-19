"""Local help ("帮助" mf23): the packaged Markdown documentation, its topic tree and search.

Observed: the Help dialog (FUN_00560e90, 400 x 450) shows the picture ``res\\help.bmp`` and,
when it exists, ``File\\help.bmp`` (vendor contact / QR picture, not shipped with the rebuild:
branding).  ``File\\sources.txt`` holds ``https://app.gweikecloud.com/mlaser/PC``, read by the
online update check FUN_004e93a0 (A250708_0..3 "Update" / "Get updated information..."),
which is a phone-home and is intentionally omitted.

The rebuild's help is the Markdown in ``docs/`` (offline, no network access).  Where it is read
from (:func:`docs_root`):

1. ``$MLASER_HELP_DIR`` (tests, translators previewing a docs tree);
2. the packaged copy ``mlaser/help/docs``: a wheel gets it from the ``build_py`` hook in
   ``tools/help_build.py`` (pyproject ``[tool.setuptools.cmdclass]``), the PyInstaller bundle
   from ``packaging/mlaser.spec`` (``bundle_config.help_datas``);
3. the ``docs/`` folder of a source checkout (editable installs, running from the repository).

Only :data:`SHIPPED` goes into the packages: the user guide, the developer docs, the
reverse-engineering notes and the top-level SAFETY / ASSETS / ROADMAP files; ``docs/review``
(screenshots) and ``docs/re/history`` stay in the repository.

Everything here is Qt-free; :mod:`mlaser.ui.help_browser` is the window.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

PKG_DOCS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "help", "docs"))
REPO_DOCS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "docs"))
#: kept for older callers: the folder :func:`docs_root` found at import time
DOCS_DIR = PKG_DOCS if os.path.isdir(PKG_DOCS) else REPO_DOCS

#: what the packages ship, relative to ``docs/`` (files and folders); keep in sync with
#: ``tools/help_build.py`` (tests/test_docs_links.py checks it)
SHIPPED = ("README.md", "SAFETY.md", "ASSETS.md", "ROADMAP.md", "user", "dev", "re")
#: never shipped or listed, even inside a shipped folder
EXCLUDED = ("re/history",)

#: section id -> (i18n id, English title); the order of the tree
SECTIONS = (("user", "help.section.user", "User guide"),
            ("dev", "help.section.dev", "Developer"),
            ("re", "help.section.re", "Reverse engineering"))

#: topic order inside each section (file names without folder); others follow by name
ORDER = {
    "": ("README.md",),
    "user": ("README.md", "getting-started.md", "shell.md", "keyboard.md", "safety.md", "importing-mlaser-setup.md", "design.md",
             "process.md", "nest.md", "run.md", "report.md", "machine.md", "faq.md"),
    "dev": ("README.md", "ARCHITECTURE.md", "PLUGINS.md", "PERFORMANCE.md", "NATIVE_KERNEL.md",
            "TESTING.md", "PARITY.md", "project-format.md", "i18n.md", "RELEASE.md", "SAFETY.md", "ASSETS.md",
            "ROADMAP.md"),
    "re": ("README.md", "protocol.md", "formats.md", "cam.md", "motion.md", "nc.md", "devices.md",
           "diag.md", "ui.md", "draw.md", "craft.md", "engrave.md", "nest.md", "parity.md"),
}
#: top-level documents listed under a section (they stay at docs/ because code refers to them)
TOP_LEVEL_SECTION = {"SAFETY.md": "dev", "ASSETS.md": "dev", "ROADMAP.md": "dev"}


def docs_root() -> Optional[str]:
    """The documentation folder to read, or None when there is none (see the module doc)."""
    for d in (os.environ.get("MLASER_HELP_DIR"), PKG_DOCS, REPO_DOCS):
        if d and os.path.isdir(d):
            return d
    return None


@dataclass
class HelpTopic:
    path: str
    title: str
    section: str = ""       # "user" / "dev" / "re", "" for the overview
    rel: str = ""           # path relative to the docs root, "/" separated


def _excluded(rel: str) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in EXCLUDED)


def shipped_files(docs_dir: str) -> list[str]:
    """Relative paths ("/" separated) of every file the packages ship from ``docs_dir``."""
    out = []
    for item in SHIPPED:
        p = os.path.join(docs_dir, item)
        if os.path.isfile(p):
            out.append(item)
        elif os.path.isdir(p):
            for base, dirs, files in os.walk(p):
                dirs.sort()
                for n in sorted(files):
                    rel = os.path.relpath(os.path.join(base, n), docs_dir).replace(os.sep, "/")
                    if not _excluded(rel) and not n.startswith("."):
                        out.append(rel)
    return out


def _order_key(section: str, name: str) -> tuple:
    order = ORDER.get(section, ())
    return (order.index(name) if name in order else len(order), name.lower())


def topics(docs_dir: Optional[str] = None) -> list[HelpTopic]:
    """Every Markdown topic, the overview first, then the sections in :data:`SECTIONS` order."""
    d = docs_dir or docs_root()
    if not d or not os.path.isdir(d):
        return []
    by_section: dict[str, list[HelpTopic]] = {"": []}
    for rel in shipped_files(d):
        if not rel.endswith(".md"):
            continue
        head, _, name = rel.rpartition("/")
        section = head.split("/")[0] if head else TOP_LEVEL_SECTION.get(name, "")
        p = os.path.join(d, *rel.split("/"))
        by_section.setdefault(section, []).append(HelpTopic(p, title_of(p) or name, section, rel))
    out: list[HelpTopic] = []
    for sec in [""] + [s[0] for s in SECTIONS] + sorted(set(by_section) - {""} - {s[0] for s in SECTIONS}):
        out += sorted(by_section.get(sec, []),
                      key=lambda t: (_order_key(t.section, t.rel.rpartition("/")[2]), t.rel))
    return out


def title_of(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"^#\s+(.*)", ln)
                if m:
                    return _plain(m.group(1).strip())
    except OSError:
        pass
    return ""


def read_topic(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return f"# {os.path.basename(path)}\n\n{e}"


# --------------------------------------------------------------------------- Markdown helpers
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:.+?)\1")
_LINK = re.compile(r"!?\[(?P<text>(?:[^\[\]]|\[[^\]]*\])*)\]\((?P<target><[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def strip_code(md: str) -> list[str]:
    """The lines of ``md`` with fenced code blocks blanked and inline code spans removed (line
    numbers are kept)."""
    out, fence = [], ""
    for ln in md.splitlines():
        m = _FENCE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = ""
            out.append("")
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        if ln.startswith("    ") and (not out or not out[-1].strip()):
            out.append("")           # indented code block
            continue
        out.append(_CODE_SPAN.sub(" ", ln))
    return out


def _plain(text: str) -> str:
    """Heading text as rendered: links become their text, code/emphasis markers go."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text.replace("`", "").replace("**", "").replace("__", "").strip()


def slugify(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to '-'."""
    t = _plain(text).lower()
    t = "".join(ch for ch in t if ch in " -_" or unicodedata.category(ch)[0] in "LNM")
    return t.replace(" ", "-")


def headings(md: str) -> list[tuple[int, int, str, str]]:
    """(line number, level, text, anchor) of every ATX heading; duplicate anchors get -1, -2..."""
    seen: dict[str, int] = {}
    out = []
    fence = ""
    for i, ln in enumerate(md.splitlines(), 1):
        m = _FENCE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = ""
            continue
        if m:
            fence = m.group(1)
            continue
        h = _HEADING.match(ln)
        if h:
            text = _plain(h.group(2))
            base = slugify(h.group(2))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.append((i, len(h.group(1)), text, base if n == 0 else f"{base}-{n}"))
    return out


def anchors(md: str) -> set[str]:
    a = {h[3] for h in headings(md)}
    a |= set(re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", md))
    return a


def links(md: str) -> list[tuple[int, str]]:
    """(line number, target) of every inline link or image outside code."""
    out = []
    for i, ln in enumerate(strip_code(md), 1):
        for m in _LINK.finditer(ln):
            t = m.group("target").strip("<>")
            out.append((i, t))
    return out


def is_external(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) or target.startswith("//")


def resolve_link(current_rel: str, target: str) -> tuple[str, str]:
    """(relative path, anchor) a link in ``current_rel`` points to (path "" = the same topic)."""
    path, _, anchor = target.partition("#")
    if not path:
        return current_rel, anchor
    base = current_rel.rpartition("/")[0]
    parts: list[str] = base.split("/") if base else []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                parts.append("..")
        else:
            parts.append(seg)
    return "/".join(parts), anchor


# --------------------------------------------------------------------------- search
@dataclass
class SearchHit:
    topic: HelpTopic
    line: int               # 1-based line of the first match in the section
    anchor: str             # heading anchor of the section ("" = top of the topic)
    heading: str
    snippet: str
    score: float


@dataclass
class _Section:
    topic: HelpTopic
    line: int
    anchor: str
    heading: str
    level: int
    lines: list[tuple[int, str]] = field(default_factory=list)


def _words(q: str) -> list[str]:
    return [w for w in re.split(r"\s+", q.lower().strip()) if w]


class HelpIndex:
    """Sections of every topic (split at headings) for ranked full-text search.

    A query is a list of words; a section matches when it contains all of them.  Score: the
    topic title (x8), the section heading (x4) and the body (x1 per occurrence, capped)."""

    def __init__(self, ts: list[HelpTopic]):
        self.topics = ts
        self.sections: list[_Section] = []
        for t in ts:
            md = read_topic(t.path)
            hs = {h[0]: h for h in headings(md)}
            cur = _Section(t, 1, "", t.title, 0)
            self.sections.append(cur)
            for i, ln in enumerate(md.splitlines(), 1):
                if i in hs:
                    _, lvl, text, a = hs[i]
                    cur = _Section(t, i, a if lvl > 1 else "", text, lvl)
                    self.sections.append(cur)
                    continue
                if ln.strip():
                    cur.lines.append((i, ln))

    def search(self, query: str, limit: int = 100) -> list[SearchHit]:
        ws = _words(query)
        if not ws:
            return []
        hits = []
        for s in self.sections:
            title, head = s.topic.title.lower(), s.heading.lower()
            body = [(i, ln, ln.lower()) for i, ln in s.lines]
            score = 0.0
            first: Optional[tuple[int, str]] = None
            ok = True
            for w in ws:
                n = sum(lo.count(w) for _i, _l, lo in body)
                if not (n or w in head or w in title):
                    ok = False
                    break
                score += 8 * (w in title) + 4 * (w in head) + min(n, 10)
                if first is None and n:
                    first = next((i, ln) for i, ln, lo in body if w in lo)
            if not ok:
                continue
            if s.level == 0 and not s.lines:
                continue
            line, text = first if first is not None else (s.line, s.heading)
            hits.append(SearchHit(s.topic, line, s.anchor, s.heading, _snippet(text, ws[0]), score))
        hits.sort(key=lambda h: (-h.score, h.topic.rel, h.line))
        return hits[:limit]


def _snippet(line: str, word: str, width: int = 140) -> str:
    t = _plain(line.strip())
    t = re.sub(r"^(?:[-*+>]|\d+\.)\s+", "", t).replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip()
    k = t.lower().find(word)
    if len(t) <= width or k < 0:
        return t[:width]
    a = max(0, k - width // 3)
    return ("..." if a else "") + t[a:a + width] + ("..." if a + width < len(t) else "")


@lru_cache(maxsize=4)
def _index_for(root: str, stamp: float) -> HelpIndex:
    return HelpIndex(topics(root))


def index(docs_dir: Optional[str] = None) -> HelpIndex:
    """A (cached) :class:`HelpIndex` of ``docs_dir`` (default :func:`docs_root`)."""
    d = docs_dir or docs_root() or ""
    try:
        stamp = max((os.path.getmtime(t.path) for t in topics(d)), default=0.0)
    except OSError:
        stamp = 0.0
    return _index_for(d, stamp)


def search(query: str, docs_dir: Optional[str] = None, limit: int = 50) -> list[tuple[HelpTopic, int, str]]:
    """(topic, line number, line) of the best-ranked sections containing every word of ``query``
    (case-insensitive); see :class:`HelpIndex`."""
    return [(h.topic, h.line, h.snippet) for h in index(docs_dir).search(query, limit)]


def about_text() -> str:
    import platform
    try:
        import PySide6
        qt = PySide6.__version__
    except Exception:  # noqa: BLE001
        qt = "?"
    return ("Mlaser rebuild - cross-platform reimplementation of Gweike Mlaser v0.0.1.51_SP1\n"
            f"Python {platform.python_version()}, PySide6 {qt}, {platform.system()}\n"
            "Licensing, dongle checks, online update and cloud monitoring are intentionally omitted.")
