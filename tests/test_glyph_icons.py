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

#: catalogue strings still carrying a glyph (owners: the preview scrubber, the Run workspace,
#: the settings import wizard, Machine)
KNOWN_STRINGS = {"preview.lazy", "run.alarm.title.one", "run.alarm.title.other",
                 "wizard.import.safety_count.one", "wizard.import.safety_count.other",
                 "ws.machine.hw_applied_errors", "shell.palette.hint"}

#: lines of UI code (comments left out) with a glyph, per file
KNOWN_CODE = {
    "preview_scrubber.py": 3, "ext_practice.py": 1, "wizards/settings_import.py": 6,
    "shell/__init__.py": 1,            # a layout diagram in the docstring, not an icon
    "workspaces/machine.py": 1, "shell/dialogs.py": 1, "wizards/base.py": 2,
    "wizards/procedures.py": 4, "wizards/commissioning_live.py": 2, "workspaces/run.py": 1,
    "shell/palette.py": 1,            # the command palette's "recently used" mark
}


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
    for f in ("workspaces/nest.py", "workspaces/orders.py", "workspaces/report.py", "icons.py"):
        assert f not in counts, f
