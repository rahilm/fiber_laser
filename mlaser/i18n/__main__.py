"""``python -m mlaser.i18n import PATH | list | remove CODE``: manage imported translations."""
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
    a = ap.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
