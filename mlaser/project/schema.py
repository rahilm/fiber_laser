"""JSON schema of the ``.mlz`` members (format version 1) and a small validator.

The schemas use a subset of JSON Schema (draft 2020-12): ``type``, ``properties``, ``required``,
``items``, ``prefixItems``, ``enum``, ``minimum``, ``anyOf``, ``additionalProperties`` and
``$ref`` to ``#/$defs/...``.  :func:`validate` implements exactly that subset, so loading needs no
extra dependency; ``python -m mlaser.project schema`` prints the full schema for other tools.
Tagged values (``{"$tuple": ...}``, :mod:`mlaser.project.codec`) are allowed wherever ``extra``
data appears.
"""
from __future__ import annotations

from typing import Any

NUM = {"anyOf": [{"type": "number"}, {"type": "object", "required": ["$float"]}]}
NUM_OR_NULL = {"anyOf": [NUM, {"type": "null"}]}
POINT_SEG = {"type": "array", "minItems": 3}

DEFS: dict[str, Any] = {
    "num": NUM,
    "lead": {
        "type": "object",
        "required": ["kind", "angle_deg", "length", "radius", "at_corner"],
        "properties": {"kind": {"type": "integer"}, "angle_deg": NUM, "length": NUM, "radius": NUM,
                       "at_corner": {"type": "boolean"}},
    },
    "craft": {
        "type": "object",
        "required": ["layer", "lead_in", "lead_out"],
        "properties": {
            "layer": {"type": "integer"},
            "kerf_comp": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "kerf_width": NUM_OR_NULL,
            "lead_in": {"$ref": "#/$defs/lead"},
            "lead_out": {"$ref": "#/$defs/lead"},
            "micro_joints": {"type": "array", "items": {"anyOf": [
                NUM, {"type": "array", "prefixItems": [NUM, NUM], "minItems": 2}]}},
            "joints_auto": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "joint_widths_chf": {"anyOf": [{"type": "array"}, {"type": "null"}]},
            "joint_width": NUM_OR_NULL,
            "cool_points": {"type": "array"},
            "start_param": NUM_OR_NULL,
            "overcut": NUM_OR_NULL,
            "lead_error": {"type": "boolean"},
            "extra": {"type": "object"},
        },
    },
    "segment": {
        "type": "array", "minItems": 3,
        "prefixItems": [{"enum": ["L", "A", "G"]}],
    },
    "glyph": {
        "type": "object", "required": ["kind"],
        "properties": {"kind": {"type": "integer", "minimum": 1}, "data": {"type": "object"},
                       "dir": {"type": "integer"}},
    },
    "contour": {
        "type": "object",
        "required": ["closed", "segments"],
        "properties": {
            "closed": {"type": "boolean"},
            "segments": {"type": "array", "items": {"$ref": "#/$defs/segment"}},
            "craft": {"$ref": "#/$defs/craft"},
            "glyphs": {"type": "array", "items": {"$ref": "#/$defs/glyph"}},
            "glyphs_current": {"type": "boolean"},
            "extra": {"type": "object"},
        },
    },
    "raster": {
        "type": "object",
        "required": ["x0", "x1", "row", "row_y", "power", "frame"],
        "properties": {k: {"type": "string"} for k in ("x0", "x1", "row", "row_y", "power")}
        | {"frame": {"type": "array", "minItems": 4, "maxItems": 4, "items": NUM}},
    },
    "part": {
        "type": "object",
        "required": ["craft", "contours"],
        "properties": {
            "name": {"type": "string"},
            "selected": {"type": "boolean"},
            "craft": {"$ref": "#/$defs/craft"},
            "contours": {"type": "array", "items": {"$ref": "#/$defs/contour"}},
            "raster": {"$ref": "#/$defs/raster"},
            "extra": {"type": "object"},
            "chf_graph": {"type": "integer", "minimum": 0},
        },
    },
    "layer": {
        "type": "object", "required": ["index"],
        "properties": {"index": {"type": "integer"}, "color": {"type": "string"}, "speed": NUM,
                       "power_pct": NUM, "frequency_hz": NUM, "duty_pct": NUM, "gas": {"type": "integer"},
                       "gas_pressure": NUM, "cut_height": NUM, "pierce": {"type": "object"},
                       "extra": {"type": "object"}},
    },
}

DOCUMENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://mlaser.invalid/schema/project/document-1.json",
    "title": "mlaser project document (document.json, format version 1)",
    "type": "object",
    "required": ["version", "layers", "parts"],
    "properties": {
        "version": {"type": "integer", "minimum": 1},
        "layers": {"type": "array", "items": {"$ref": "#/$defs/layer"}},
        "parts": {"type": "array", "items": {"$ref": "#/$defs/part"}},
        "extra": {"type": "object"},
    },
    "$defs": DEFS,
}

MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://mlaser.invalid/schema/project/manifest.json",
    "title": "mlaser project manifest (manifest.json)",
    "type": "object",
    "required": ["format", "version", "files"],
    "properties": {
        "format": {"enum": ["mlaser-project"]},
        "version": {"type": "integer", "minimum": 0},
        "app": {"type": "string"},
        "created": {"type": "string"},
        "saved": {"type": "string"},
        "files": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["sha256", "size"],
            "properties": {"sha256": {"type": "string"}, "size": {"type": "integer", "minimum": 0}}}},
        "sections": {"type": "array", "items": {"type": "string"}},
        "chf": {"anyOf": [{"type": "null"}, {"type": "object"}]},
        "dropped": {"type": "array", "items": {"type": "string"}},
    },
}

SECTION_SCHEMAS: dict[str, dict] = {
    # well-known sections; any other name holds free JSON (other tracks, plugins)
    "technology": {"type": "object"},
    "queue": {"type": "array", "items": {"type": "object"}},
}

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is(v: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(inst: Any, schema: dict, path: str = "", root: dict | None = None, limit: int = 20) -> list[str]:
    """Errors of ``inst`` against ``schema`` as ``"path: message"`` strings (at most ``limit``)."""
    root = root or schema
    errs: list[str] = []
    _check(inst, schema, path or "$", root, errs, limit)
    return errs


def _check(v: Any, s: dict, path: str, root: dict, errs: list[str], limit: int) -> None:
    if len(errs) >= limit:
        return
    if "$ref" in s:
        ref = s["$ref"]
        node: Any = root
        for k in ref.lstrip("#/").split("/"):
            node = node[k]
        _check(v, node, path, root, errs, limit)
        return
    if "anyOf" in s:
        for sub in s["anyOf"]:
            tmp: list[str] = []
            _check(v, sub, path, root, tmp, limit)
            if not tmp:
                break
        else:
            errs.append(f"{path}: unexpected value {_short(v)}")
            return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {_short(v)} is not one of {s['enum']}")
        return
    t = s.get("type")
    if t is not None and not _is(v, t):
        errs.append(f"{path}: expected {t}, found {_short(v)}")
        return
    if "minimum" in s and _is(v, "number") and v < s["minimum"]:
        errs.append(f"{path}: {v} is below {s['minimum']}")
    if isinstance(v, dict):
        for k in s.get("required", ()):
            if k not in v:
                errs.append(f"{path}: missing {k!r}")
        props = s.get("properties", {})
        for k, x in v.items():
            if k in props:
                _check(x, props[k], f"{path}.{k}", root, errs, limit)
            elif isinstance(s.get("additionalProperties"), dict):
                _check(x, s["additionalProperties"], f"{path}.{k}", root, errs, limit)
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            errs.append(f"{path}: needs at least {s['minItems']} items, has {len(v)}")
        if "maxItems" in s and len(v) > s["maxItems"]:
            errs.append(f"{path}: has more than {s['maxItems']} items")
        pre = s.get("prefixItems", [])
        for i, x in enumerate(v):
            sub = pre[i] if i < len(pre) else s.get("items")
            if sub is not None:
                _check(x, sub, f"{path}[{i}]", root, errs, limit)
                if len(errs) >= limit:
                    return


def _short(v: Any) -> str:
    r = repr(v)
    return r if len(r) <= 40 else r[:37] + "..."
