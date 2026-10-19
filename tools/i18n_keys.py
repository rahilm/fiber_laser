"""Find every string-catalogue key the code uses and check it against ``mlaser/i18n/en.json``.

Sources of keys:

* string literals anywhere in ``mlaser/**/*.py`` (``tr("mf15")``, ``_tr(w, "mp54", ...)``,
  key tables such as ``ribbon_spec`` and ``panels.ControlPanel.STEP_KEYS``);
* module-level containers after import, for keys built in comprehensions
  (``[f"RORegName_{i}" for i in ...]``, ``{n: f"gp{n}" ...}``);
* the parameter descriptors in ``mlaser/formats/data/mlaser_params.json`` (label and option
  keys).

A literal counts as a key when it is in the catalogue's legacy map, or (with ``--vendor``) in
the original program's ``lang.txt``, or when it has the shape of an original key.

    .venv/bin/python tools/i18n_keys.py              # exit 1 if a used key is missing
    .venv/bin/python tools/i18n_keys.py --list       # print the used keys
    .venv/bin/python tools/i18n_keys.py --vendor <Mlaser>/Lang/lang.txt --list
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import pkgutil
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

#: shapes of the original's opaque keys (docs/re/ui.md "Key-family cheat sheet")
KEY_RE = re.compile(
    r"^(?:(?:mf|mp|pd|gp|hp|mv|lp|slp|ss|zf|af|ec|cp|de|db|da|ap|np|SBT|SLED|newLang|eNewLang)\d[\w-]*"
    r"|A2\d{5}_\d+|\w*RegName_\d+|EtherCAT\w*_\d+(?:_\d+)?|(?:np|nset|pd_Nest|mf_S)_\w+)$")


#: literals that look like keys but are not (device names, ...)
NOT_KEYS = {"ec3710"}


def _vendor_keys(path: str) -> set[str]:
    from mlaser.i18n.langfile import Translator
    t = Translator()
    t.load(os.path.dirname(path)) if os.path.isdir(os.path.dirname(path)) else None
    return t.keys()


def _walk_values(obj, out: set, depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(obj, str):
        out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_values(k, out, depth + 1)
            _walk_values(v, out, depth + 1)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            _walk_values(v, out, depth + 1)


def literals() -> set[str]:
    out: set[str] = set()
    for base, _dirs, files in os.walk(os.path.join(ROOT, "mlaser")):
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(base, f), encoding="utf-8").read()
                for n in ast.walk(ast.parse(src)):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        out.add(n.value)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import mlaser
    for mod in pkgutil.walk_packages(mlaser.__path__, "mlaser."):
        if mod.name == "mlaser.parity" or mod.name.startswith("mlaser.parity.") or mod.name.endswith("__main__"):
            continue
        try:
            m = importlib.import_module(mod.name)
        except Exception:  # noqa: BLE001 - optional dependencies
            continue
        for v in vars(m).values():
            if isinstance(v, (list, tuple, dict, set, frozenset)):
                _walk_values(v, out)
            elif isinstance(v, type) and v.__module__ == m.__name__:
                for ck, cv in vars(v).items():
                    # dunders are not key tables (3.13 adds __static_attributes__, the names of
                    # the attributes the methods assign: self.da1 is not the catalogue key da1)
                    if ck.startswith("__"):
                        continue
                    if isinstance(cv, (list, tuple, dict, set, frozenset)):
                        _walk_values(cv, out)
    for r in json.load(open(os.path.join(ROOT, "mlaser/formats/data/mlaser_params.json"), encoding="utf-8")):
        out.add(r.get("lang") or "")
        for o in r.get("options") or []:
            out.add(o.get("lang") or "")
    out.discard("")
    return out


def used_keys(vendor: set[str] | None = None) -> set[str]:
    from mlaser.i18n import catalogue
    known = set(catalogue("en").legacy)
    lits = literals()
    if vendor is not None:
        return {s for s in lits if s in vendor or s in known}
    return {s for s in lits if s in known or (KEY_RE.match(s) and s not in NOT_KEYS)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", help="the original's Lang/lang.txt (only used to recognise keys)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    vendor = _vendor_keys(a.vendor) if a.vendor else None
    keys = used_keys(vendor)
    if a.list:
        print("\n".join(sorted(keys)))
        return 0
    from mlaser.i18n import catalogue
    missing = sorted(k for k in keys if catalogue("en").resolve(k) is None)
    for k in missing:
        print("missing:", k)
    print(f"{len(keys)} keys used, {len(missing)} missing from mlaser/i18n/en.json")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
