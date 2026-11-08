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

* plural forms: a ``<translator>.plural("id", n)`` / ``i18n.plural("id", n)`` whose ``id.other``
  is not in en.json; an en.json plural id (``<base>.one``...) without its ``<base>.other``; a
  ``(s)`` plural hack in an English text or ``"s" if n != 1`` in the code;
* user-visible text that bypasses the catalogue (:func:`literals`): a string literal given to a
  Qt text call (``setText``, ``setToolTip``, ``QLabel(...)``, ``QMessageBox.question(...)``,
  ``addItem``, ``setHorizontalHeaderLabels`` ...) in ``mlaser/``;
* sentence fragments joined by code (:func:`concatenations`): a translated text ``+`` or
  f-string-joined with literal words or another translated text. A whole sentence with
  ``{placeholders}`` is translatable; fragments are not (word order differs between languages).

A line that is deliberately not translated (a file name, a protocol word) carries the comment
``# i18n: ok`` and is skipped by the last three checks.

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
CALLS = {"tr": 0, "_tr": None, "tr_param": 0, "format": 0, "bind": 1, "trf": 0, "_trf": None,
         "plural": 0, "_trn": None, "trn": None}
#: plural lookups (the id names the ``<id>.<category>`` family)
PLURAL_CALLS = ("plural", "_trn", "trn")
#: the CLDR plural categories (mlaser.i18n.PLURAL_CATEGORIES)
PLURAL_CATS = ("zero", "one", "two", "few", "many", "other")
#: a line the author marked as deliberately not translated
OK_MARK = "i18n: ok"
WORD_RE = re.compile(r"[A-Za-z]{2,}")
#: Qt calls whose string arguments are shown to the user, and which arguments
TEXT_SETTERS = {
    "setText", "setToolTip", "setWindowTitle", "setPlaceholderText", "setStatusTip", "setWhatsThis",
    "setAccessibleName", "setAccessibleDescription", "setTitle", "setLabelText", "setTabText",
    "setTabToolTip", "addTab", "insertTab", "addItem", "insertItem", "addItems", "showMessage",
    "setHeaderLabels", "setHorizontalHeaderLabels", "setVerticalHeaderLabels", "setSuffix", "setPrefix",
    "setSpecialValueText", "setInformativeText", "setDetailedText", "addRow", "setCancelButtonText",
    "setItemText", "setHtml", "setPlainText", "addButton"}
TEXT_CTORS = {"QLabel": 1, "QPushButton": 1, "QCheckBox": 1, "QRadioButton": 1, "QGroupBox": 1,
              "QAction": 1, "QToolButton": 1, "QMenu": 1, "QTableWidgetItem": 1, "QListWidgetItem": 1,
              "QTreeWidgetItem": 1, "QCommandLinkButton": 1, "QDockWidget": 1, "QToolBar": 1,
              "QProgressDialog": 2, "QMessageBox": 3}
TEXT_DIALOGS = {"question", "warning", "information", "critical", "about", "getText", "getInt",
                "getDouble", "getItem", "getOpenFileName", "getSaveFileName", "getExistingDirectory",
                "getOpenFileNames"}
#: translated-text calls (for the concatenation check)
TR_CALLS = {"tr", "_tr", "format", "trf", "_trf", "tr_param", "plural", "_trn", "trn"}
#: Qt calls whose later arguments are data, not text (``addItem(text, userData)``)
FIRST_ARG_ONLY = {"addItem", "insertItem", "setItemText", "setTabText", "setTabToolTip", "addTab", "insertTab"}


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


def _py_files(root: str):
    for path in sorted(glob.glob(os.path.join(root, "mlaser", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if "/parity_ports/" in rel or rel.startswith("mlaser/i18n/"):
            continue
        try:
            src = open(path, encoding="utf-8").read()
            yield rel, src.splitlines(), ast.parse(src)
        except (OSError, SyntaxError):
            continue


def _call_name(n: ast.Call) -> str:
    f = n.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _text_const(c: ast.AST) -> bool:
    """A string constant with words that is not an id or an original key."""
    return (isinstance(c, ast.Constant) and isinstance(c.value, str) and bool(WORD_RE.search(c.value))
            and not ID_RE.match(c.value) and not re.match(r"^[a-z]{1,3}\d+$", c.value))


def _visible_strings(node: ast.AST) -> list[str]:
    """Literal texts of an argument expression that reach the user as they are: constants,
    f-string parts and concatenations, not arguments of other calls (lookups, unit kinds)."""
    out: list[str] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.Call):
            f = n.func
            if not (isinstance(f, ast.Attribute) and f.attr == "format" and isinstance(f.value, ast.Constant)):
                return
        if isinstance(n, ast.Constant):
            if _text_const(n):
                out.append(n.value)
            return
        if isinstance(n, (ast.Dict, ast.Subscript, ast.Lambda, ast.Compare)):
            return
        for c in ast.iter_child_nodes(n):
            walk(c)
    walk(node)
    return out


def _has_tr(n: ast.AST) -> bool:
    return any(isinstance(c, ast.Call) and _call_name(c) in TR_CALLS for c in ast.walk(n))


def literals(root: str) -> list[tuple[str, int, str, str]]:
    """(file, line, call, text): literal user-visible texts given to Qt text calls."""
    out = []
    for rel, lines, tree in _py_files(root):
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = _call_name(n)
            if name in TEXT_SETTERS and isinstance(n.func, ast.Attribute):
                args = n.args
                if name in FIRST_ARG_ONLY:
                    # the text: addItem(text, data) / insertItem(i, text, data) / setTabText(i, text)
                    idx = 1 if name in ("insertItem", "setItemText", "setTabText", "setTabToolTip", "insertTab") else 0
                    if name == "addTab":
                        idx = 1
                    args = [a for a in n.args[idx:idx + 1] if not isinstance(a, ast.Call) or _call_name(a) != "QIcon"]
            elif name in TEXT_CTORS:
                args = n.args[:TEXT_CTORS[name]]
            elif name in TEXT_DIALOGS and isinstance(n.func, ast.Attribute):
                args = n.args[1:3]
            else:
                continue
            end = getattr(n, "end_lineno", n.lineno) or n.lineno
            if any(OK_MARK in lines[i - 1] for i in range(n.lineno, end + 1)):
                continue
            for a in args:
                for t in _visible_strings(a):
                    out.append((rel, n.lineno, name, t))
    return out


def concatenations(root: str) -> list[tuple[str, int, str]]:
    """(file, line, code): translated texts joined to literal words or other translated texts."""
    found: dict[tuple[str, int], str] = {}
    for rel, lines, tree in _py_files(root):
        for n in ast.walk(tree):
            hit = False
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                l, r = n.left, n.right
                if isinstance(l, (ast.List, ast.Tuple)) or isinstance(r, (ast.List, ast.Tuple)):
                    hit = False
                elif _has_tr(l) and (_has_tr(r) or _visible_strings(r) or isinstance(r, ast.JoinedStr)):
                    hit = True
                elif _has_tr(r) and (_visible_strings(l) or isinstance(l, ast.JoinedStr)):
                    hit = True
            elif isinstance(n, ast.JoinedStr):
                vals = [v for v in n.values if isinstance(v, ast.FormattedValue)]
                text = "".join(v.value for v in n.values if isinstance(v, ast.Constant))
                if any(_has_tr(v.value) for v in vals) and (len(vals) > 1 or WORD_RE.search(text)):
                    hit = True
            if not hit:
                continue
            end = getattr(n, "end_lineno", n.lineno) or n.lineno
            if any(OK_MARK in lines[i - 1] for i in range(n.lineno, end + 1)):
                continue
            found.setdefault((rel, n.lineno), ast.unparse(n)[:100])
    return [(f, ln, c) for (f, ln), c in sorted(found.items())]


#: ``"s" if n != 1`` / ``"s" if n > 1``: English-only plurals in the code
_PLURAL_HACK = re.compile(r"""['"]s['"]\s+if\s+[^\n]*?(!=|>|==)\s*1\b""")


def plural_hacks(root: str) -> list[tuple[str, int, str]]:
    out = []
    for rel, lines, _tree in _py_files(root):
        if rel == "mlaser/ui/responsive.py":            # a style-sheet state name, not a text
            continue
        for i, line in enumerate(lines, 1):
            if _PLURAL_HACK.search(line) and OK_MARK not in line:
                out.append((rel, i, line.strip()[:100]))
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


def check(root: str, ts_files: list[str], strict: bool = False, legacy_scan: bool = True,
          code_scan: bool = True) -> tuple[list[str], list[str]]:
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
        if re.search(r"\w\(s\)", text):                 # "job(s)"; "Time (s)" is seconds
            errors.append(f"en.json: {sid}: '(s)' plural: use <id>.one / <id>.other and plural()")
    for sid in strings:
        base, _, last = sid.rpartition(".")
        if last in PLURAL_CATS and last != "other" and base and f"{base}.other" not in strings:
            errors.append(f"en.json: plural {sid} without {base}.other")
    for sid in args:
        if sid not in strings:
            errors.append(f"en.json: args for unknown id {sid}")
    for k, sid in legacy.items():
        if sid not in strings:
            errors.append(f"en.json: legacy key {k} -> unknown id {sid}")
    # --- keys used by the code
    for sid, path, line, call, kws in code_ids(root):
        if call in PLURAL_CALLS:
            if f"{sid}.other" not in strings:
                errors.append(f"{path}:{line}: {call}({sid!r}): {sid}.other missing from en.json")
                continue
            bad = [k for k in kws if k not in BRACE_RE.findall(strings[f"{sid}.other"]) and k != "n"]
            if bad:
                errors.append(f"{path}:{line}: plural({sid!r}): {bad} not placeholders of the id")
            continue
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
            ref = sid
            if sid not in strings:
                base, _, last = sid.rpartition(".")
                if last in PLURAL_CATS and f"{base}.other" in strings:
                    ref = f"{base}.other"               # a plural category English does not use
                else:
                    errors.append(f"{name}: {sid}: id not in en.json")
                    continue
            if text.strip() and set(BRACE_RE.findall(text)) != set(BRACE_RE.findall(strings[ref])):
                errors.append(f"{name}: {sid}: placeholders {sorted(set(BRACE_RE.findall(text)))} != "
                              f"English {sorted(set(BRACE_RE.findall(strings[ref])))}")
        missing = [s for s in strings if not tr.get(s, "").strip()]
        if missing:
            (errors if strict else warnings).append(
                f"{name}: {len(missing)} of {len(strings)} ids untranslated (e.g. {missing[0]})")
    # --- texts that bypass the catalogue, sentence fragments, English-only plurals
    if code_scan:
        for path, line, call, text in literals(root):
            errors.append(f"{path}:{line}: {call}(): literal text {text!r} (use the catalogue, or mark "
                          f"the line '# {OK_MARK}')")
        for path, line, code in concatenations(root):
            errors.append(f"{path}:{line}: translated fragments joined by code: {code} (one id with "
                          "{placeholders})")
        for path, line, code in plural_hacks(root):
            errors.append(f"{path}:{line}: English-only plural: {code} (use plural())")
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
    ap.add_argument("--no-code-scan", action="store_true",
                    help="skip the literal-text / fragment / plural scan of the code")
    ap.add_argument("--report", action="store_true",
                    help="only count literals, fragments and plural hacks (no other checks)")
    a = ap.parse_args(argv)
    if a.report:
        root = os.path.abspath(a.root)
        lit, cc, ph = literals(root), concatenations(root), plural_hacks(root)
        for r in lit:
            print("literal", *r)
        for r in cc:
            print("fragments", *r)
        for r in ph:
            print("plural", *r)
        print(f"{len(lit)} literal(s), {len(cc)} joined fragment(s), {len(ph)} English-only plural(s)")
        return 0
    errors, warnings = check(os.path.abspath(a.root), a.ts, a.strict, not a.no_legacy_scan,
                             not a.no_code_scan)
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("error:", e)
    print(f"i18n check: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
