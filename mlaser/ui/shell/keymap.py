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

Per platform (:data:`PLATFORM_KEYS`, :func:`window_keys`): the tables are written with
``Ctrl`` (Qt maps it to ⌘ on macOS); each platform overrides what its conventions say:
redo is Ctrl+Y on Windows, Ctrl+Shift+Z on Linux and ⇧⌘Z on macOS; Delete is ⌫ on a Mac
keyboard; full screen is F11 on Windows/Linux and ⌃⌘F on macOS; and every F-key action gets a ⌘
alternative on macOS, where the F-keys are media keys by default.  Shortcut text shown to the
user goes through :func:`native_text` (⌘K on macOS, never a literal "Ctrl+K").
"""
from __future__ import annotations

import re
import sys
from typing import Callable, Iterable, Optional

WINDOW_KEYS: dict[str, str] = {
    # file
    "file.new": "Ctrl+N", "file.open": "Ctrl+O", "file.save": "Ctrl+S", "file.save_as": "Ctrl+Shift+S",
    "file.import": "Ctrl+I", "app.settings": "Ctrl+,", "app.exit": "Ctrl+Q",
    # edit
    "edit.undo": "Ctrl+Z|Alt+Backspace", "edit.redo": "Ctrl+Y|Ctrl+Shift+Z", "edit.copy": "Ctrl+C", "edit.paste": "Ctrl+V",
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
    # window
    "window.fullscreen": "F11",
}

#: platform overrides of :data:`WINDOW_KEYS` ("" removes a key on that platform)
PLATFORM_KEYS: dict[str, dict[str, str]] = {
    "macos": {
        "edit.redo": "Ctrl+Shift+Z", "edit.delete": "Backspace|Del",
        "window.fullscreen": "Meta+Ctrl+F", "window.minimize": "Ctrl+M",
        "app.help": "Ctrl+?|F1", "shell.shortcuts": "Ctrl+/|Ctrl+F1",
        # F-keys are media keys on a Mac keyboard: a ⌘ alternative for each (⌘. is the Mac's
        # "cancel", so it stops the job; Esc stops too)
        "run.start": "Ctrl+R|F5", "run.frame": "Ctrl+Shift+F|F6", "run.dry_run": "Ctrl+Shift+R|Shift+F6",
        "run.stop": "Ctrl+.|F12", "shell.reset_layout": "Ctrl+Alt+0",
    },
    "windows": {"app.exit": ""},                       # Alt+F4 (the system's), File > Exit
    "linux": {"edit.redo": "Ctrl+Shift+Z|Ctrl+Y"},
}


def platform_id(plat: Optional[str] = None) -> str:
    plat = plat or sys.platform
    return "macos" if plat in ("darwin", "macos") else "windows" if plat.startswith("win") else "linux"


def window_keys(plat: Optional[str] = None) -> dict[str, str]:
    """:data:`WINDOW_KEYS` with the platform's overrides."""
    out = dict(WINDOW_KEYS)
    for cmd, key in PLATFORM_KEYS.get(platform_id(plat), {}).items():
        if key:
            out[cmd] = key
        else:
            out.pop(cmd, None)
    return out


_MAC_MODS = (("Meta+", "⌃"), ("Alt+", "⌥"), ("Shift+", "⇧"), ("Ctrl+", "⌘"))
_MAC_KEYS = {"Backspace": "⌫", "Del": "⌦", "Delete": "⌦", "Esc": "⎋", "Return": "↩", "Enter": "⌤",
             "Tab": "⇥", "Space": "Space", "Left": "←", "Right": "→", "Up": "↑", "Down": "↓"}
_SEQ_RE = re.compile(r"\b((?:(?:Ctrl|Shift|Alt|Meta)\+)+)(F\d{1,2}|Backspace|Delete|Del|Esc|Return|Enter|Tab|"
                     r"Space|Left|Right|Up|Down|[A-Za-z0-9]\b|[,.=\-?/\[\]])")


def _mac_seq(mods: str, key: str) -> str:
    out = "".join(sym for name, sym in _MAC_MODS if name in mods)
    return out + _MAC_KEYS.get(key, key.upper() if len(key) == 1 else key)


def seq_text(seq: str, plat: Optional[str] = None) -> str:
    """One key sequence as the platform writes it ("Backspace" -> "⌫", "Ctrl+M" -> "⌘M")."""
    if platform_id(plat) != "macos":
        return seq
    parts = seq.split("+")
    if len(parts) > 1 and parts[-1] == "":           # "Ctrl++"
        parts = parts[:-2] + ["+"]
    mods, key = "".join(p + "+" for p in parts[:-1]), parts[-1]
    if mods and not all(m in ("Ctrl", "Shift", "Alt", "Meta") for m in parts[:-1]):
        return seq
    return _mac_seq(mods, key)


def keys_text(key: str, plat: Optional[str] = None) -> str:
    """A table entry ("Ctrl+Y|Ctrl+Shift+Z") as shown: alternatives joined by " / "."""
    return " / ".join(seq_text(k, plat) for k in sequences(key))


def native_text(text: str, plat: Optional[str] = None) -> str:
    """Shortcut text as the platform writes it: on macOS "Ctrl+Shift+Z" -> "⇧⌘Z" and
    "Search (Ctrl+K)" -> "Search (⌘K)"; unchanged elsewhere.  Works on whole sentences."""
    if platform_id(plat) != "macos" or "+" not in text:
        return text
    return _SEQ_RE.sub(lambda m: _mac_seq(m.group(1), m.group(2)), text)

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


def key_for(cmd_id: str, plat: Optional[str] = None) -> str:
    """The shortcut shown for a command ("" for none); window keys first."""
    return keys_text(window_keys(plat).get(cmd_id) or CANVAS_KEYS.get(cmd_id, ""), plat)


def sequences(key: str) -> list[str]:
    """The alternatives of a table entry (``"Ctrl+K|F1"`` -> ``["Ctrl+K", "F1"]``)."""
    return [k.strip() for k in key.split("|") if k.strip()]


def conflicts(plat: Optional[str] = None) -> list[tuple[str, str, str]]:
    """Key sequences bound twice in the same context: (key, first id, second id)."""
    out = []
    for table in (window_keys(plat), CANVAS_KEYS):
        seen: dict[str, str] = {}
        for cmd, key in table.items():
            for one in sequences(key):
                k = one.lower()
                if k in seen:
                    out.append((one, seen[k], cmd))
                seen[k] = cmd
    return out


def rows(label: Callable[[str], str], workspace: Callable[[str], str], tr: Callable[[str], str],
         known: Optional[Iterable[str]] = None, plat: Optional[str] = None) -> list[tuple[str, str, str, str]]:
    """(workspace title, command label, key, context) rows for the sheet, sorted by workspace
    and label; ``known`` limits them to registered commands.  Keys as ``plat`` writes them."""
    ids = set(known) if known is not None else None
    out = []
    for table, ctx in ((window_keys(plat), tr("shell.key.ctx_window")), (CANVAS_KEYS, tr("shell.key.ctx_canvas"))):
        for cmd, key in table.items():
            if ids is not None and cmd not in ids:
                continue
            out.append((workspace(cmd), label(cmd), keys_text(key, plat), ctx))
    for key, desc, ctx in FIXED_KEYS:
        if platform_id(plat) == "macos":
            key = key.replace("Ctrl + click", "⌘ + click")
        out.append(("", tr(desc), native_text(key, plat), tr(ctx)))
    out.sort(key=lambda r: (r[0] == "", r[0], r[1].lower()))
    return out


def markdown(rows_: list[tuple[str, str, str, str]], title: str = "Keyboard shortcuts",
             mac_rows: Optional[list[tuple[str, str, str, str]]] = None) -> str:
    """The keyboard reference; with ``mac_rows`` (the same rows for macOS) a macOS column."""
    head = "| Workspace | Command | Key | Where |"
    rule = "|---|---|---|---|"
    if mac_rows is not None:
        head, rule = "| Workspace | Command | Windows / Linux | macOS | Where |", "|---|---|---|---|---|"
    lines = [f"# {title}", "",
             "Generated from the command registry by `tools/gen_keyboard_doc.py`; the same table is",
             "shown in the application under Help > Keyboard shortcuts (Ctrl+F1 / ⌘/, or ? on the canvas).",
             "Windows and Linux differ only in redo (Ctrl+Y on Windows, Ctrl+Shift+Z first on Linux) and",
             "Exit (no shortcut on Windows: Alt+F4).", "", head, rule]
    mac = {(r[0], r[1], r[3]): r[2] for r in (mac_rows or [])}
    seen = set()
    for ws, lab, key, ctx in rows_:
        seen.add((ws, lab, ctx))
        if mac_rows is not None:
            m = mac.get((ws, lab, ctx), "")
            lines.append(f"| {ws or '-'} | {lab} | `{key}` | {f'`{m}`' if m else '-'} | {ctx} |")
        else:
            lines.append(f"| {ws or '-'} | {lab} | `{key}` | {ctx} |")
    for ws, lab, key, ctx in (mac_rows or []):
        if (ws, lab, ctx) not in seen:
            lines.append(f"| {ws or '-'} | {lab} | - | `{key}` | {ctx} |")
    return "\n".join(lines) + "\n"


__all__ = ["sequences", "WINDOW_KEYS", "PLATFORM_KEYS", "window_keys", "native_text", "platform_id", "seq_text", "keys_text", "CANVAS_KEYS", "FIXED_KEYS", "key_for", "conflicts", "rows", "markdown"]
