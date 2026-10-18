"""Local help ("帮助" mf23).

Observed: the Help dialog (FUN_00560e90, 400 x 450) shows the picture ``res\\help.bmp`` and,
when it exists, ``File\\help.bmp`` (vendor contact / QR picture, not shipped with the rebuild:
branding).  ``File\\sources.txt`` holds ``https://app.gweikecloud.com/mlaser/PC``, read by the
online update check FUN_004e93a0 (A250708_0..3 "Update" / "Get updated information..."),
which is a phone-home and is intentionally omitted.

The rebuild's help is built from the Markdown documents in ``docs/`` (offline).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

DOCS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "docs"))
ORDER = ("README.md", "ui.md", "diag.md", "protocol.md", "devices.md", "motion.md", "cam.md", "nest.md",
         "nc.md", "formats.md")
SKIP = ("BRIEF.md",)


@dataclass
class HelpTopic:
    path: str
    title: str


def topics(docs_dir: Optional[str] = None) -> list[HelpTopic]:
    d = docs_dir or DOCS_DIR
    if not os.path.isdir(d):
        return []
    names = [n for n in os.listdir(d) if n.endswith(".md") and n not in SKIP]
    names.sort(key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
    out = []
    for n in names:
        p = os.path.join(d, n)
        out.append(HelpTopic(p, title_of(p) or n))
    readme = os.path.normpath(os.path.join(d, "..", "README.md"))
    if os.path.exists(readme) and not any(t.path == readme for t in out):
        out.insert(0, HelpTopic(readme, title_of(readme) or "README"))
    return out


def title_of(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"^#\s+(.*)", ln)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return ""


def read_topic(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return f"# {os.path.basename(path)}\n\n{e}"


def search(query: str, docs_dir: Optional[str] = None, limit: int = 50) -> list[tuple[HelpTopic, int, str]]:
    """(topic, line number, line) of lines containing ``query`` (case-insensitive)."""
    q = query.lower().strip()
    out = []
    if not q:
        return out
    for t in topics(docs_dir):
        for i, ln in enumerate(read_topic(t.path).splitlines(), 1):
            if q in ln.lower():
                out.append((t, i, ln.strip()))
                if len(out) >= limit:
                    return out
    return out


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
