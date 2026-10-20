"""The last-used values of the one-shot CAM tools, kept in the ConfigService.

The geometry tools (array, fill, fillet, unload loop, α corner, cooling points, rectangle common
edge, fly-cut, bridge, OneKey planning, engraving scan) are operations, not settings: their
dialog is the editor.  What a dialog was last run with is remembered under
``cam.tool.<tool>`` (a JSON object of the parameters' fields) instead of window attributes, so a
restart keeps it; Settings › CAM tools shows these values read-only with a Reset
(docs/user/settings.md).

:func:`bind` restores an extension's parameter objects at start-up and again after a change of
the key (a Reset in the Settings window gives the tool its defaults back); :func:`remember`
stores an object after its dialog was confirmed.
"""
from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from typing import Any

from mlaser.services.config import Setting

PREFIX = "cam.tool."


def key_of(tool: str) -> str:
    return f"cam.tool.{tool}"


def _jsonable(v: Any) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False


def snapshot(obj: Any) -> dict:
    """The JSON-able fields of a parameter dataclass."""
    if not is_dataclass(obj):
        return {}
    out = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        if isinstance(v, tuple):
            v = list(v)
        if _jsonable(v):
            out[f.name] = v
    return out


def restore(obj: Any, data: Any) -> Any:
    """Apply stored fields onto ``obj`` (type-checked: a field whose stored value has another
    type, or a field the class no longer has, keeps its default)."""
    if not isinstance(data, dict) or not is_dataclass(obj):
        return obj
    for f in fields(obj):
        if f.name not in data:
            continue
        cur, v = getattr(obj, f.name), data[f.name]
        try:
            if isinstance(cur, bool):
                v = bool(v)
            elif isinstance(cur, int) and not isinstance(v, bool):
                v = int(v)
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, tuple):
                v = tuple(v)
            elif cur is not None and not isinstance(v, type(cur)):
                continue
            setattr(obj, f.name, v)
        except (TypeError, ValueError):
            continue
    return obj


def register(cfg: Any, tools: list[str]) -> None:
    new = [Setting(key_of(t), "json", {}, label=f"settings.tool.{t}", section="cam", source="app",
                   home="settings") for t in tools if cfg.setting(key_of(t)) is None]
    if new:
        cfg.register(new)


def remember(cfg: Any, tool: str, obj: Any) -> None:
    data = snapshot(obj)
    if data and cfg.get(key_of(tool), {}) != data:
        cfg.set(key_of(tool), data)


def bind(owner: Any, cfg: Any, tools: dict[str, str]) -> None:
    """``tools``: {tool id: attribute of ``owner`` holding its parameter object}."""
    register(cfg, list(tools))

    def load(tool: str, attr: str) -> None:
        obj = getattr(owner, attr)
        try:
            fresh = type(obj)()
        except TypeError:
            fresh = obj
        setattr(owner, attr, restore(fresh, cfg.get(key_of(tool), {})))

    for tool, attr in tools.items():
        load(tool, attr)

    def changed(key: str) -> None:
        for tool, attr in tools.items():
            if key in ("*", key_of(tool)):
                load(tool, attr)

    cfg.subscribe(changed)


def summary(data: Any, tr: Any = None) -> str:
    """A one-line read-only summary of a stored tool value."""
    if not isinstance(data, dict) or not data:
        return ""
    parts = []
    for k, v in data.items():
        if isinstance(v, float):
            v = f"{v:g}"
        elif isinstance(v, bool):
            v = "on" if v else "off"
        parts.append(f"{k.replace('_', ' ')} {v}")
    return " · ".join(parts)
