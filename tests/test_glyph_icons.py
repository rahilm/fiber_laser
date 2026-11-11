"""Icons are the SVG set, never font glyphs (UI review 2026-11-09 finding 12): "▶", "⚠", "✓",
"★" take the platform font's shape and weight (a colour emoji on Windows) beside the line icons.

The catalogue strings and the UI code that still use a glyph as an icon are listed here with
their counts, which may only go down; a new one fails.  The Nest, Orders and Report pages, the
wizard step list and the import review list use SVG icons."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GLYPHS = set("▶◀⚠✓✗✔✘★○⏸⏵")

#: catalogue strings still carrying a glyph (owner: the Run workspace's alarm block)
KNOWN_STRINGS = {"run.alarm.title.one", "run.alarm.title.other"}

#: lines of UI code (comments left out) with a glyph, per file
KNOWN_CODE = {
    "ext_practice.py": 1,
    "shell/__init__.py": 1,            # a layout diagram in the docstring, not an icon
}

#: the files moved to the SVG icons (style.IconLabel, icons.status_icon): no glyph may come back
CLEAN = ("workspaces/nest.py", "workspaces/orders.py", "workspaces/report.py", "icons.py", "style.py",
         "preview_scrubber.py", "wizards/settings_import.py", "wizards/base.py", "wizards/procedures.py",
         "wizards/commissioning_live.py", "workspaces/run.py", "workspaces/machine.py", "shell/dialogs.py",
         "shell/palette.py")


def test_no_new_glyph_in_the_catalogue():
    strings = json.loads((ROOT / "mlaser" / "i18n" / "en.json").read_text(encoding="utf-8"))
    strings = strings.get("strings", strings)
    found = {k for k, v in strings.items() if isinstance(v, str) and GLYPHS & set(v)}
    assert found <= KNOWN_STRINGS, sorted(found - KNOWN_STRINGS)
    for k in ("nestws.btn.nest", "nestws.btn.to_run", "nestws.btn.to_run_n", "orders.btn.nest"):
        assert not GLYPHS & set(strings[k]), k


def test_no_new_glyph_in_the_ui_code():
    ui = ROOT / "mlaser" / "ui"
    counts = {}
    for f in ui.rglob("*.py"):
        n = sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#") and GLYPHS & set(line.split(" # ")[0]))
        if n:
            counts[f.relative_to(ui).as_posix()] = n
    over = {f: n for f, n in counts.items() if n > KNOWN_CODE.get(f, 0)}
    assert not over, over
    for f in CLEAN:
        assert f not in counts, f
