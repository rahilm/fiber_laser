"""Format migrations of ``.mlz`` archives (docs/dev/project-format.md §Versions).

Each step takes the archive members (name -> bytes) of format version ``n`` and returns the
members of version ``n + 1``; :func:`migrate` chains them up to :data:`CURRENT`.  A step never
reads the model classes: it rewrites JSON, so an old file keeps loading after the model moves on.
Add a step (and a fixture in tests/data/project/) whenever the layout of a member changes.

Version 0 (the Wave 3 draft): one ``project.json`` with ``{"format_version": 0, "document": ...,
"sections": ...}``; the document still used the pre-typed model (``Craft.extra['micro_joint_len']``,
``['overcut']``, ``['start_param']``, ``['cool_points']``, ``['lead_error']``, the single
``Craft.cool_point``, plain position lists in ``micro_joints``, ``Contour.extra['craft']``).
Version 1: ``mimetype``, ``manifest.json`` with checksums, ``document.json`` with the typed fields,
``sections/<name>.json``, ``blobs/``, ``chf/records.chf``, ``fonts/``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

CURRENT = 1

Members = dict[str, bytes]


def _json(b: bytes) -> Any:
    return json.loads(b.decode("utf-8"))


def _dump(v: Any) -> bytes:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def detect_version(members: Members) -> int:
    """Format version of an archive (raises ValueError when it is not a project)."""
    if "manifest.json" in members:
        man = _json(members["manifest.json"])
        if not isinstance(man, dict) or man.get("format") != "mlaser-project":
            raise ValueError("manifest.json does not describe an mlaser project")
        v = man.get("version")
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("manifest.json has no valid format version")
        return v
    if "project.json" in members:
        p = _json(members["project.json"])
        if isinstance(p, dict) and p.get("format_version") == 0:
            return 0
    raise ValueError("no manifest.json: not an mlaser project")


# --------------------------------------------------------------------------- 0 -> 1
_CRAFT_KEYS = {"micro_joint_len": "joint_width", "overcut": "overcut", "start_param": "start_param",
               "lead_error": "lead_error"}


def _craft_0_1(c: dict) -> dict:
    out = dict(c)
    ex = dict(out.pop("extra", {}) or {})
    for old, new in _CRAFT_KEYS.items():
        if old in ex:
            out[new] = ex.pop(old)
    out["lead_error"] = bool(out.get("lead_error", False))
    cps = list(ex.pop("cool_points", []) or [])
    cp = out.pop("cool_point", None)
    if cp is not None:
        cps.append({"$tuple": list(cp)})
    out["cool_points"] = cps
    widths = ex.pop("micro_joint_widths", None)
    js, auto = [], []
    for i, j in enumerate(out.get("micro_joints", [])):
        if isinstance(j, list):
            js.append([j[0], j[1]])
        else:
            w = widths[i] if widths and i < len(widths) and widths[i] is not None else out.get("joint_width")
            js.append([j, 0.5 if w is None else w])
            auto.append(i)
    out["micro_joints"] = js
    out["joints_auto"] = auto
    out["joint_widths_chf"] = widths
    out["extra"] = ex
    return out


def _contour_0_1(c: dict) -> dict:
    out = dict(c)
    ex = dict(out.pop("extra", {}) or {})
    if isinstance(ex.get("craft"), dict):
        out["craft"] = _craft_0_1(ex.pop("craft"))
    if "glyphs" in ex:
        out["glyphs"] = ex.pop("glyphs")
        out["glyphs_current"] = bool(ex.pop("glyphs_current", True))
    if ex:
        out["extra"] = ex
    return out


def _part_0_1(p: dict) -> dict:
    out = dict(p)
    out["craft"] = _craft_0_1(out.get("craft", {}))
    out["contours"] = [_contour_0_1(c) for c in out.get("contours", [])]
    return out


def migrate_0_to_1(members: Members) -> Members:
    p = _json(members["project.json"])
    doc = dict(p.get("document", {}))
    doc["parts"] = [_part_0_1(x) for x in doc.get("parts", [])]
    doc.setdefault("version", 9)
    doc.setdefault("layers", [])
    doc.setdefault("extra", {})
    out: Members = {"mimetype": b"application/vnd.mlaser.project+zip", "document.json": _dump(doc)}
    sections = p.get("sections", {}) or {}
    for name, v in sections.items():
        out[f"sections/{name}.json"] = _dump(v)
    for k, v in members.items():
        if k.startswith(("blobs/", "fonts/")):
            out[k] = v
    out["manifest.json"] = _dump(manifest_for(out, version=1, extra={
        "app": str(p.get("app", "mlaser (format 0)")), "created": str(p.get("created", "")),
        "sections": sorted(sections), "migrated_from": 0}))
    return out


def manifest_for(members: Members, version: int, extra: dict | None = None) -> dict:
    files = {k: {"sha256": hashlib.sha256(v).hexdigest(), "size": len(v)}
             for k, v in sorted(members.items()) if k not in ("manifest.json", "mimetype")}
    man = {"format": "mlaser-project", "version": version, "files": files}
    man.update(extra or {})
    return man


MIGRATIONS: dict[int, Callable[[Members], Members]] = {0: migrate_0_to_1}


def migrate(members: Members) -> tuple[Members, list[int]]:
    """Members of the current version and the versions migrated through."""
    v = detect_version(members)
    seen = []
    while v < CURRENT:
        step = MIGRATIONS.get(v)
        if step is None:
            raise ValueError(f"no migration from format version {v}")
        members = step(members)
        seen.append(v)
        v = detect_version(members)
    return members, seen
