#!/usr/bin/env python3
"""CI check of the string catalogues: missing keys and placeholder mismatches.

Errors (exit 1):

* a key the code uses is not in ``mlaser/i18n/en.json``: the original's opaque keys found by
  ``tools/i18n_keys.py`` (``tr("mf15")``, key tables), and readable ids passed to the lookup
  calls (``tr("panel.x")``, ``_tr(w, "panel.x", ...)``, ``ctx.tr.format("panel.x", n=1)``,
  ``tr_.bind(obj, "panel.x", ...)``);
* a keyword passed to ``<translator>.format("id", name=...)`` that is not a placeholder of the id;
* ``en.json`` itself: a declared ``args`` list that differs from the text's ``{placeholders}``,
  a printf placeholder in an English text, a ``legacy`` entry that points to no id;
* another catalogue (``mlaser/i18n/<code>.json``) or a Qt Linguist file (``*.ts`` under
  ``mlaser/i18n`` and ``translations/``, or given on the command line): an id that is not in
  en.json (``.json`` only; stale .ts entries are warnings), or a translation whose
  ``{placeholders}`` differ from the English text's.

Warnings: untranslated ids of another catalogue (``--strict`` makes them errors), stale .ts
messages.

    python tools/i18n_check.py                  # the repository
    python tools/i18n_check.py --root DIR       # another tree with the same layout (tests)
    python tools/i18n_check.py extra/de.ts      # also check these .ts files
"""
from __future__ import annotations

import argparse
import ast
import glob
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
BRACE_RE = re.compile(r"\{([A-Za-z_]\w*)(?::[^{}]*)?\}")
PRINTF_RE = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?l*[dfsuxgcS]")
#: lookup callables and the position of the key argument
CALLS = {"tr": 0, "_tr": None, "tr_param": 0, "format": 0, "bind": 1, "trf": 0}


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _receiver_is_translator(node: ast.expr) -> bool:
    src = ast.unparse(node).lower()
    return "tr" in src.split(".")[-1] or "translator" in src or "i18n" in src or src.endswith("tr_")


def code_ids(root: str) -> list[tuple[str, str, int, str, list[str]]]:
    """(id, file, line, call, keyword names) for readable ids passed to the lookup calls."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "mlaser", "**", "*.py"), recursive=True)):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name) and f.id in CALLS:
                name = f.id
            elif isinstance(f, ast.Attribute) and f.attr in CALLS:
                name = f.attr
                if isinstance(f.value, ast.Constant):
                    continue                                    # "...".format(...)
                if name in ("format", "bind") and not _receiver_is_translator(f.value):
                    continue
            else:
                continue
            pos = CALLS[name]
            cands = n.args[:2] if pos is None else n.args[pos:pos + 1]
            for a in cands:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and ID_RE.match(a.value):
                    kws = [k.arg for k in n.keywords if k.arg and k.arg != "default"]
                    out.append((a.value, os.path.relpath(path, root), n.lineno, name, kws))
                    break
    return out


def _used_legacy_keys(root: str) -> set[str]:
    spec = importlib.util.spec_from_file_location("i18n_keys", os.path.join(root, "tools", "i18n_keys.py"))
    if spec is None or spec.loader is None or not os.path.isfile(spec.origin or ""):
        return set()
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.used_keys())


def _parse_ts(path: str):
    sys.path.insert(0, ROOT)
    from mlaser.i18n.linguist import parse_ts
    with open(path, encoding="utf-8") as fh:
        return parse_ts(fh.read())


def check(root: str, ts_files: list[str], strict: bool = False, legacy_scan: bool = True
          ) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    cat_dir = os.path.join(root, "mlaser", "i18n")
    en = _load_json(os.path.join(cat_dir, "en.json"))
    strings: dict[str, str] = en.get("strings", {})
    args: dict[str, list] = en.get("args", {})
    legacy: dict[str, str] = en.get("legacy", {})
    # --- en.json itself
    for sid, text in strings.items():
        if PRINTF_RE.search(text):
            errors.append(f"en.json: {sid}: printf placeholder in the English text")
        if set(BRACE_RE.findall(text)) != set(args.get(sid, [])):
            errors.append(f"en.json: {sid}: args {args.get(sid, [])} != placeholders "
                          f"{sorted(set(BRACE_RE.findall(text)))}")
    for sid in args:
        if sid not in strings:
            errors.append(f"en.json: args for unknown id {sid}")
    for k, sid in legacy.items():
        if sid not in strings:
            errors.append(f"en.json: legacy key {k} -> unknown id {sid}")
    # --- keys used by the code
    for sid, path, line, call, kws in code_ids(root):
        if sid not in strings:
            errors.append(f"{path}:{line}: {call}({sid!r}): id missing from en.json")
            continue
        bad = [k for k in kws if call == "format" and k not in BRACE_RE.findall(strings[sid])]
        if bad:
            errors.append(f"{path}:{line}: format({sid!r}): {bad} not placeholders of the id")
    if legacy_scan:
        for k in sorted(_used_legacy_keys(root)):
            if k not in strings and k not in legacy:
                errors.append(f"code: key {k} missing from en.json")
    # --- other catalogues
    for path in sorted(glob.glob(os.path.join(cat_dir, "*.json"))):
        if os.path.basename(path) == "en.json":
            continue
        name = os.path.relpath(path, root)
        try:
            data = _load_json(path)
        except (OSError, ValueError) as e:
            errors.append(f"{name}: unreadable: {e}")
            continue
        tr = data.get("strings", {})
        for sid, text in tr.items():
            if sid not in strings:
                errors.append(f"{name}: {sid}: id not in en.json")
            elif text.strip() and set(BRACE_RE.findall(text)) != set(BRACE_RE.findall(strings[sid])):
                errors.append(f"{name}: {sid}: placeholders {sorted(set(BRACE_RE.findall(text)))} != "
                              f"English {sorted(set(BRACE_RE.findall(strings[sid])))}")
        missing = [s for s in strings if not tr.get(s, "").strip()]
        if missing:
            (errors if strict else warnings).append(
                f"{name}: {len(missing)} of {len(strings)} ids untranslated (e.g. {missing[0]})")
    # --- Qt Linguist files
    found = glob.glob(os.path.join(cat_dir, "**", "*.ts"), recursive=True) + \
        glob.glob(os.path.join(root, "translations", "**", "*.ts"), recursive=True)
    for path in sorted(set(found)) + list(ts_files):
        name = os.path.relpath(path, root) if path.startswith(root) else path
        try:
            ts = _parse_ts(path)
        except (OSError, ValueError) as e:
            errors.append(f"{name}: {e}")
            continue
        stale = 0
        for m in ts.messages:
            if m.id not in strings:
                stale += 1
                continue
            if m.translation.strip() and set(BRACE_RE.findall(m.translation)) != set(
                    BRACE_RE.findall(strings[m.id])):
                errors.append(f"{name}: {m.id}: placeholders {sorted(set(BRACE_RE.findall(m.translation)))}"
                              f" != English {sorted(set(BRACE_RE.findall(strings[m.id])))}")
        if stale:
            warnings.append(f"{name}: {stale} message(s) for ids no longer in en.json")
    return errors, warnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ts", nargs="*", help="extra .ts files to check")
    ap.add_argument("--root", default=ROOT, help="tree to check (default: this repository)")
    ap.add_argument("--strict", action="store_true", help="untranslated ids are errors")
    ap.add_argument("--no-legacy-scan", action="store_true",
                    help="skip the import-based scan of the original's keys (tools/i18n_keys.py)")
    a = ap.parse_args(argv)
    errors, warnings = check(os.path.abspath(a.root), a.ts, a.strict, not a.no_legacy_scan)
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("error:", e)
    print(f"i18n check: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
