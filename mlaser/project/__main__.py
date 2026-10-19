"""``python -m mlaser.project schema | info FILE | convert IN OUT``.

* ``schema``: the JSON schemas of manifest.json, document.json and the well-known sections;
* ``info FILE``: the manifest and a summary of a ``.mlz`` (exit 1 with the error when damaged);
* ``convert IN OUT``: ``.chf`` -> ``.mlz`` or ``.mlz`` -> ``.chf`` (by the suffix of OUT).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ProjectError, export_chf, load_project, open_document, save_project
from .format import atomic_write
from .schema import DOCUMENT_SCHEMA, MANIFEST_SCHEMA, SECTION_SCHEMAS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mlaser.project", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema")
    p = sub.add_parser("info")
    p.add_argument("file")
    p = sub.add_parser("convert")
    p.add_argument("src")
    p.add_argument("dst")
    a = ap.parse_args(argv)
    if a.cmd == "schema":
        print(json.dumps({"manifest": MANIFEST_SCHEMA, "document": DOCUMENT_SCHEMA, "sections": SECTION_SCHEMAS},
                         indent=1))
        return 0
    try:
        if a.cmd == "info":
            pr = load_project(a.file)
            d = pr.document
            print(json.dumps(pr.meta, indent=1))
            print(f"parts {len(d.parts)}, contours {sum(len(p.contours) for p in d.parts)}, "
                  f"layers {sorted(d.layers)}, raster parts {sum(p.raster is not None for p in d.parts)}")
            print(f"sections {sorted(pr.sections) + [s for s in ('technology', 'queue') if getattr(pr, s)]}"
                  f", nest {'yes' if pr.nest is not None else 'no'}, fonts {sorted(pr.fonts)}")
            for n in pr.notes:
                print("note:", n)
            return 0
        doc = open_document(a.src)
        if Path(a.dst).suffix.lower() == ".chf":
            atomic_write(a.dst, export_chf(doc))
        else:
            save_project(a.dst, doc)
        return 0
    except ProjectError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
