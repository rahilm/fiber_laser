"""``python -m mlaser.i18n import PATH | list | remove CODE | ts CODE | ts-import FILE | qm FILE``.

Imported translations (``import`` / ``list`` / ``remove``, :mod:`mlaser.i18n.importer`) and the
Qt Linguist workflow (``ts`` / ``ts-import`` / ``qm``, :mod:`mlaser.i18n.linguist`)."""
from __future__ import annotations

import argparse
import sys

from . import importer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mlaser.i18n", description=importer.__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="import the language files of an Mlaser install (folder or .txt file)")
    p.add_argument("path")
    sub.add_parser("list", help="list imported languages")
    r = sub.add_parser("remove", help="remove an imported language")
    r.add_argument("code")
    t = sub.add_parser("ts", help="write a Qt Linguist .ts file of language CODE from en.json")
    t.add_argument("code")
    t.add_argument("-o", "--output", help="output file (default <code>.ts)")
    t.add_argument("--merge", help="an earlier .ts whose translations are kept")
    t.add_argument("--seed-imported", action="store_true",
                   help="suggest the texts imported from an original install (unfinished)")
    ti = sub.add_parser("ts-import", help="turn a translated .ts back into a <code>.json catalogue")
    ti.add_argument("file")
    ti.add_argument("-o", "--output", help="output .json (default mlaser/i18n/<code>.json)")
    ti.add_argument("--name", default="", help="native language name")
    ti.add_argument("--unfinished", action="store_true", help="also take unfinished translations")
    q = sub.add_parser("qm", help="compile a .ts to .qm with lrelease")
    q.add_argument("file")
    q.add_argument("-o", "--output")
    a = ap.parse_args(argv)
    if a.cmd in ("ts", "ts-import", "qm"):
        return _linguist(a)
    if a.cmd == "import":
        try:
            codes = importer.import_path(a.path)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"imported {', '.join(codes)} into {importer.lang_store()}")
    elif a.cmd == "list":
        for code, data in importer.imported().items():
            print(f"{code}\t{data.get('name', '')}\t{len(data.get('legacy_strings', {}))} strings\t{data.get('source', '')}")
    elif a.cmd == "remove":
        return 0 if importer.remove(a.code) else 1
    return 0


def _linguist(a) -> int:
    import os

    from . import HERE, linguist as L
    if a.cmd == "ts":
        merge = None
        if a.merge:
            with open(a.merge, encoding="utf-8") as fh:
                merge = L.parse_ts(fh.read())
        seed = L._imported_texts(a.code, L.catalogue("en")) if a.seed_imported else None
        ts = L.build_ts(a.code, merge=merge, seed=seed)
        out = a.output or f"{a.code}.ts"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(L.dumps_ts(ts))
        done = sum(m.finished for m in ts.messages)
        print(f"{out}: {len(ts.messages)} messages, {done} translated")
    elif a.cmd == "ts-import":
        try:
            with open(a.file, encoding="utf-8") as fh:
                ts = L.parse_ts(fh.read())
        except (OSError, ValueError) as exc:
            print(exc, file=sys.stderr)
            return 1
        if not ts.language or ts.language == "en":
            print("the .ts file names no target language (or English)", file=sys.stderr)
            return 1
        data = L.ts_to_catalogue(ts, a.name, a.unfinished)
        out = a.output or os.path.join(HERE, f"{ts.language}.json")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(L.dumps_catalogue(data))
        print(f"{out}: {len(data['strings'])} strings")
    else:
        try:
            print(L.compile_qm(a.file, a.output))
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
