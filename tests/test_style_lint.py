"""The design system's lint (mlaser/ui/style.py, docs/dev/STYLE.md).

In ``mlaser/ui`` and the bundled plugins' UI code:

* **colour**: no colour literal (``"#c62828"``, ``QColor(21, 101, 192)``, ``Qt.red``, a named
  colour in a style sheet) outside :mod:`mlaser.ui.shell.theme`, the safety colour table and the
  canvas's data tables (the original's layer colours and drawing-area defaults): the lines
  between ``# style-lint: colour table`` and ``# style-lint: end``; code asks
  :func:`mlaser.ui.style.color` / ``css`` for a role;
* **font**: no absolute font size (``setPointSizeF(13)``, ``font-size: 18pt``,
  ``QFont("Sans", 8)``, ``pointSizeF() + 2``); sizes are type-scale roles (:func:`style.font`);
* **margin**: no literal layout margins or spacings other than 0 (``setContentsMargins(8, ...)``,
  ``setSpacing(6)``, ``padding: 4px`` in a style sheet); the tokens SP0..SP5 / :func:`style.margins`.

Files not yet migrated are in :data:`ALLOWLIST` with their current counts: a count may only go
down (the test says when to lower it), a file that is migrated leaves the list, and no file may
join it.  Comments and docstrings are not code and are not checked.
"""
from __future__ import annotations

import io
import os
import re
import tokenize

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCAN = ["mlaser/ui", "plugins/mlaser-onshape/mlaser_onshape"]
EXEMPT = {"mlaser/ui/shell/theme.py", "mlaser/ui/style.py"}

#: files still to migrate: {path: {rule: count}}.  Only ever lower these numbers.
ALLOWLIST: dict[str, dict[str, int]] = {
    # the classic ribbon keeps the original's tight look on purpose (UI audit §9); everything
    # else is migrated
    "mlaser/ui/ribbon.py": {"margin": 5},
}

HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?\b")
NAMED_CSS = re.compile(r"(?:^|[;{\s])(?:color|background(?:-color)?|border(?:-color)?)\s*:\s*(?:[^;}]*\s)?"
                       r"(white|black|red|green|blue|orange|yellow|grey|gray|darkred)\b(?!\()", re.I)
CODE_COLOUR = [re.compile(r"\bQColor\(\s*\d"), re.compile(r"\bQt\.(?:GlobalColor\.)?(?:red|green|blue|yellow|black|"
                                                           r"white|darkRed|darkGreen|gray|darkGray|lightGray|cyan|"
                                                           r"magenta)\b")]
FONT = [re.compile(r"\.setPointSizeF?\(\s*[\d.]+\s*\)"), re.compile(r"\.setPixelSize\(\s*\d+\s*\)"),
        re.compile(r"\bQFont\(\s*[^,()]+,\s*\d+"), re.compile(r"pointSizeF?\(\)\s*[-+]\s*[\d.]"),
        re.compile(r"max\([^()]*pointSizeF?\(\)\s*,\s*[\d.]+\)")]
FONT_CSS = re.compile(r"font-size\s*:\s*\d+(?:\.\d+)?\s*(?:pt|px)", re.I)
MARGIN = [re.compile(r"\.setContentsMargins\(\s*[\d\s,]*[1-9][\d\s,]*\)"),
          re.compile(r"\.set(?:Horizontal|Vertical)?Spacing\(\s*[1-9]\d*\s*\)"),
          re.compile(r"(?:margins|spacing)\s*=\s*[1-9]\d*\b")]
MARGIN_CSS = re.compile(r"(?:padding|margin)(?:-\w+)?\s*:\s*[1-9]\d*\s*px", re.I)
TABLE_START, TABLE_END = "# style-lint: colour table", "# style-lint: end"


def _files() -> list[str]:
    out = []
    for base in SCAN:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for f in files:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dirpath, f), ROOT).replace(os.sep, "/")
                    if rel not in EXEMPT:
                        out.append(rel)
    return sorted(out)


def _code(src: str) -> tuple[list[str], list[str]]:
    """(code lines without comments and docstrings, string literals) of ``src``; the safety
    colour table's lines are blanked."""
    lines = src.splitlines()
    skip = False
    for i, ln in enumerate(lines):
        if TABLE_START in ln:
            skip = True
        if skip:
            lines[i] = ""
        if TABLE_END in ln:
            skip = False
    src = "\n".join(lines) + "\n"
    out = [list(ln) for ln in src.splitlines()]
    strings = []
    prev = None
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    for i, tok in enumerate(toks):
        kind = tok.type
        blank = False
        if kind == tokenize.COMMENT:
            blank = True
        elif kind == tokenize.STRING:
            # a docstring: a string statement on its own (after INDENT / NEWLINE / start)
            is_doc = prev in (None, tokenize.INDENT, tokenize.NEWLINE, tokenize.DEDENT, tokenize.NL) and \
                (i + 1 < len(toks) and toks[i + 1].type in (tokenize.NEWLINE, tokenize.ENDMARKER))
            if is_doc:
                blank = True
            else:
                strings.append(tok.string)
        if blank:
            (sr, sc), (er, ec) = tok.start, tok.end
            for r in range(sr, er + 1):
                row = out[r - 1]
                a = sc if r == sr else 0
                b = ec if r == er else len(row)
                for c in range(a, min(b, len(row))):
                    row[c] = " "
        if kind not in (tokenize.COMMENT, tokenize.NL):
            prev = kind
    return ["".join(r) for r in out], strings


def findings(path: str) -> dict[str, list[str]]:
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    lines, _strings = _code(src)
    found: dict[str, list[str]] = {"colour": [], "font": [], "margin": []}
    for n, ln in enumerate(lines, 1):
        if not ln.strip():
            continue
        where = f"{path}:{n}: {ln.strip()[:100]}"
        if HEX.search(ln) or NAMED_CSS.search(ln) or any(p.search(ln) for p in CODE_COLOUR):
            found["colour"].append(where)
        if any(p.search(ln) for p in FONT) or FONT_CSS.search(ln):
            found["font"].append(where)
        if any(p.search(ln) for p in MARGIN) or MARGIN_CSS.search(ln):
            found["margin"].append(where)
    return found


def test_lint_patterns_work():
    """The patterns catch what they should and leave tokens and comments alone."""
    src = ("a = '#c62828'  # '#ffffff' in a comment\n"
           "b.setStyleSheet('QPushButton{color:white;padding:4px}')\n"
           "f.setPointSizeF(13)\n"
           "f.setPointSizeF(f.pointSizeF() * 1.2)\n"
           "lay.setContentsMargins(8, 2, 8, 2)\n"
           "lay.setContentsMargins(0, 0, 0, 0)\n"
           "lay.setContentsMargins(SP2, 0, 0, 0)\n"
           "lay.setSpacing(6)\n"
           "lay.setSpacing(SP2)\n"
           "c = QColor(21, 101, 192)\n"
           "# style-lint: colour table\n"
           "T = {'X': '#7f0000'}\n"
           "# style-lint: end\n")
    lines, _ = _code(src)
    flagged = {r: [i + 1 for i, ln in enumerate(lines) if ln.strip() and check(ln)] for r, check in (
        ("colour", lambda ln: HEX.search(ln) or NAMED_CSS.search(ln) or any(p.search(ln) for p in CODE_COLOUR)),
        ("font", lambda ln: any(p.search(ln) for p in FONT) or FONT_CSS.search(ln)),
        ("margin", lambda ln: any(p.search(ln) for p in MARGIN) or MARGIN_CSS.search(ln)))}
    assert flagged == {"colour": [1, 2, 10], "font": [3], "margin": [2, 5, 8]}


@pytest.mark.parametrize("rule", ["colour", "font", "margin"])
def test_no_literals(rule):
    """No colour literal / absolute font size / literal margin outside the allowlist, and the
    allowlist only shrinks."""
    over, lower = [], []
    seen = set()
    for path in _files():
        hits = findings(path)[rule]
        allowed = ALLOWLIST.get(path, {}).get(rule, 0)
        if hits:
            seen.add(path)
        if len(hits) > allowed:
            over.append(f"{path}: {len(hits)} > {allowed}\n    " + "\n    ".join(hits))
        elif len(hits) < allowed:
            lower.append(f"{path}: {rule} {allowed} -> {len(hits)}")
    assert not over, f"{rule} literals (use mlaser.ui.style):\n" + "\n".join(over)
    assert not lower, "the allowlist can shrink, lower it:\n" + "\n".join(lower)


def test_allowlist_names_files_that_exist():
    for path in ALLOWLIST:
        assert os.path.exists(os.path.join(ROOT, path)), path
