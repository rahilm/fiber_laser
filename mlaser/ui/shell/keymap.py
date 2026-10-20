"""Keyboard shortcuts: one table for the whole application (docs/user/keyboard.md).

* :data:`WINDOW_KEYS`: command id -> key sequence (alternatives separated by ``|``), active
  anywhere in the main window.  They are
  set on the commands' QActions (so menus and tooltips show them, and a command that the user
  mode does not allow is disabled together with its key).
* :data:`CANVAS_KEYS`: one-key tools, active while the canvas has the keyboard focus (so typing
  in a field or a table never triggers them).
* :data:`FIXED_KEYS`: keys handled elsewhere (the safety layer's global Hold/Stop while a job
  runs, the canvas' arrows and Esc); listed in the sheet, not rebindable here.

The sheet (Help > Keyboard shortcuts) and docs/user/keyboard.md are generated from these tables
and the command registry (:func:`rows`, :func:`markdown`).  Qt-free.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

WINDOW_KEYS: dict[str, str] = {
    # file
    "file.new": "Ctrl+N", "file.open": "Ctrl+O", "file.save": "Ctrl+S", "file.save_as": "Ctrl+Shift+S",
    "file.import": "Ctrl+I", "app.settings": "Ctrl+,", "app.exit": "Ctrl+Q",
    # edit
    "edit.undo": "Ctrl+Z|Alt+Backspace", "edit.redo": "Ctrl+Y", "edit.copy": "Ctrl+C", "edit.paste": "Ctrl+V",
    "edit.delete": "Del", "select.all": "Ctrl+A", "select.invert": "Ctrl+Shift+I",
    # view
    "view.zoom_fit": "F", "view.zoom_in": "Ctrl+=", "view.zoom_out": "Ctrl+-",
    # run (pre-flight first; Stop always passes)
    "run.start": "F5", "run.frame": "F6", "run.dry_run": "Shift+F6", "run.stop": "F12",
    # shell
    "shell.palette": "Ctrl+K", "app.help": "F1", "shell.shortcuts": "Ctrl+F1",
    "shell.reset_layout": "Ctrl+Shift+F12", "shell.user_mode": "Ctrl+Shift+M",
    "workspace.design": "Ctrl+1", "workspace.process": "Ctrl+2", "workspace.nest": "Ctrl+3",
    "workspace.run": "Ctrl+4", "workspace.report": "Ctrl+5", "workspace.machine": "Ctrl+6",
}

CANVAS_KEYS: dict[str, str] = {
    "view.select_mode": "V", "view.pan_mode": "H", "view.node_edit": "N",
    "craft.lead": "L", "craft.kerf": "K", "craft.microjoint": "J", "sort.run": "S",
    "run.simulate": "P", "shell.shortcuts": "?",
}

#: (keys, lang id of the description, context lang id)
FIXED_KEYS: list[tuple[str, str, str]] = [
    ("Space", "shell.key.hold", "shell.key.ctx_job"),
    ("Esc", "shell.key.stop", "shell.key.ctx_job"),
    ("F12", "shell.key.stop", "shell.key.ctx_any"),
    ("Esc", "shell.key.deselect", "shell.key.ctx_idle"),
    ("← ↑ → ↓", "shell.key.nudge", "shell.key.ctx_canvas"),
    ("Shift / Ctrl + click", "shell.key.multiselect", "shell.key.ctx_canvas"),
    ("Wheel", "shell.key.zoom", "shell.key.ctx_canvas"),
    ("Middle button drag", "shell.key.pan", "shell.key.ctx_canvas"),
    ("Tab / Shift+Tab", "shell.key.focus", "shell.key.ctx_any"),
]


def key_for(cmd_id: str) -> str:
    """The shortcut shown for a command ("" for none); window keys first."""
    return " / ".join(sequences(WINDOW_KEYS.get(cmd_id) or CANVAS_KEYS.get(cmd_id, "")))


def sequences(key: str) -> list[str]:
    """The alternatives of a table entry (``"Ctrl+K|F1"`` -> ``["Ctrl+K", "F1"]``)."""
    return [k.strip() for k in key.split("|") if k.strip()]


def conflicts() -> list[tuple[str, str, str]]:
    """Key sequences bound twice in the same context: (key, first id, second id)."""
    out = []
    for table in (WINDOW_KEYS, CANVAS_KEYS):
        seen: dict[str, str] = {}
        for cmd, key in table.items():
            for one in sequences(key):
                k = one.lower()
                if k in seen:
                    out.append((one, seen[k], cmd))
                seen[k] = cmd
    return out


def rows(label: Callable[[str], str], workspace: Callable[[str], str], tr: Callable[[str], str],
         known: Optional[Iterable[str]] = None) -> list[tuple[str, str, str, str]]:
    """(workspace title, command label, key, context) rows for the sheet, sorted by workspace
    and label; ``known`` limits them to registered commands."""
    ids = set(known) if known is not None else None
    out = []
    for table, ctx in ((WINDOW_KEYS, tr("shell.key.ctx_window")), (CANVAS_KEYS, tr("shell.key.ctx_canvas"))):
        for cmd, key in table.items():
            if ids is not None and cmd not in ids:
                continue
            out.append((workspace(cmd), label(cmd), " / ".join(sequences(key)), ctx))
    for key, desc, ctx in FIXED_KEYS:
        out.append(("", tr(desc), key, tr(ctx)))
    out.sort(key=lambda r: (r[0] == "", r[0], r[1].lower()))
    return out


def markdown(rows_: list[tuple[str, str, str, str]], title: str = "Keyboard shortcuts") -> str:
    lines = [f"# {title}", "",
             "Generated from the command registry by `tools/gen_keyboard_doc.py`; the same table is",
             "shown in the application under Help > Keyboard shortcuts (Ctrl+F1, or ? on the canvas).",
             "", "| Workspace | Command | Key | Where |", "|---|---|---|---|"]
    for ws, lab, key, ctx in rows_:
        lines.append(f"| {ws or '-'} | {lab} | `{key}` | {ctx} |")
    return "\n".join(lines) + "\n"


__all__ = ["sequences", "WINDOW_KEYS", "CANVAS_KEYS", "FIXED_KEYS", "key_for", "conflicts", "rows", "markdown"]
