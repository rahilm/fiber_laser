"""Undo step names: what Edit > Undo / Redo say ("Undo Move", "Redo Rotate 90° clockwise").

An undo command records a *label* (``DocumentService.edit(fn, label)``).  Labels come from many
places: an internal word (``"nudge"``, ``"rotate"``), a plugin's generic default (``"draw"``,
``"craft"``, ``"onshape: edit"``), or an already translated text.  Two steps make them names:

* :func:`record_label` (``DocumentService.label_for``): while a command runs (menu, ribbon,
  shortcut, palette), a generic or catalogue word is replaced by that **command id**, so the
  step is named after the command (``xform.rot_cw90``); a draw tool that edits after its
  command returned (clicks on the canvas) is named after the last command of its kind.
* :func:`display_name`: a command id shows as the command's label (translated, without "…"),
  an internal word through the catalogue ``undo.<word>`` (``undo.nudge`` = "Move"), a readable
  id as its text; anything else as it is.

Arrow-key nudges are one step (``coalesce`` in :meth:`DocumentService.edit`).
"""
from __future__ import annotations

import re
from typing import Any

#: plugin / helper defaults that say nothing about the edit
GENERIC = frozenset({"", "edit", "draw", "craft", "cam", "replace", "batch"})
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


def slug(label: str) -> str:
    """Catalogue suffix of an internal label: ``"clear micro-joints"`` -> ``clear_micro_joints``."""
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _generic(label: str) -> bool:
    low = label.strip().lower()
    return low in GENERIC or low.endswith((": edit", ": replace"))


def record_label(window: Any, label: str) -> str:
    """The label recorded for an edit made now (see the module docstring)."""
    tr = getattr(window, "tr_", None)
    running = getattr(window, "_running_cmd", "") or ""
    catalogued = bool(tr is not None and label and tr.has(f"undo.{slug(label)}"))
    if running and (_generic(label) or catalogued):
        return running
    last = (getattr(window, "_last_cmds", None) or {}).get(label.strip().lower(), "")
    if not running and last and _generic(label):
        return last
    return label


def display_name(window: Any, label: str) -> str:
    """The name of an undo step as the user reads it."""
    if not label:
        return ""
    tr = getattr(window, "tr_", None)
    commands = getattr(window, "commands", None)
    if commands is not None and label in commands:
        shell = getattr(window, "shell", None)
        act = getattr(getattr(window, "ribbon", None), "actions", {}).get(label)
        text = shell.label(label) if shell is not None else (act.text() if act is not None else label)
        text = text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip()
        for tail in ("…", "..."):
            if text.endswith(tail):
                text = text[: -len(tail)].rstrip()
        if text and text != label:
            return text
    if tr is not None:
        key = f"undo.{slug(label)}"
        if slug(label) and tr.has(key):
            return tr.tr(key)
        if _ID_RE.match(label) and tr.has(label):
            return tr.tr(label)
        low = label.strip().lower()
        if low.endswith((": edit", ": replace")) or low in GENERIC:
            return tr.tr("undo.edit")
    return label[:1].upper() + label[1:]


def menu_texts(window: Any) -> tuple[str, str]:
    """(Undo text, Redo text) for the Edit menu: "Undo Move" / "Redo Move", plain "Undo" /
    "Redo" when there is nothing to undo / redo."""
    tr = window.tr_
    docs = window.docs
    st = docs.stack
    undo = (tr.format("shell.undo.named", action=display_name(window, st.undo_text)) if docs.can_undo
            else tr.tr("cmd.edit.undo"))
    redo = (tr.format("shell.redo.named", action=display_name(window, st.redo_text)) if docs.can_redo
            else tr.tr("cmd.edit.redo"))
    return undo, redo


__all__ = ["record_label", "display_name", "menu_texts", "slug", "GENERIC"]
