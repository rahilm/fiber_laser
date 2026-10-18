"""Rebuild the fonts bundled in ``mlaser/cad/fontdata/`` from their upstream releases.

The bundled fonts are the last step of the text font lookup (explicit file -> installed family
-> bundled; :mod:`mlaser.cad.fonts`), so that Draw -> Text and DXF TEXT give the same outlines
on every machine, Chinese included, without Qt.

=====================================  =========================================================
NotoSansSC-Regular-subset.otf          Noto Sans CJK SC 2.004 (``SubsetOTF/SC``, CFF outlines),
                                       subset to GB 2312 (6763 hanzi + symbols), kana, Latin
                                       (Basic, Latin-1, Extended-A/B), Greek, Cyrillic,
                                       punctuation, CJK symbols and full-width forms; all
                                       OpenType layout features kept (kern, liga, vert, locl...)
NotoSansArabic-Regular-subset.ttf      Noto Sans Arabic 2.013 (unhinted TTF), subset to the
                                       Arabic blocks and presentation forms, features kept
OFL-NotoSansCJK.txt,                   SIL Open Font License 1.1 with each font's copyright
OFL-NotoSansArabic.txt                 notice (upstream LICENSE / OFL.txt, unchanged)
=====================================  =========================================================

Both fonts are under the SIL OFL 1.1 without Reserved Font Names, so subsets may keep their
names.  Downloads are verified against the SHA-256 below; the outputs are deterministic for a
given fontTools version (the ``head`` timestamps are kept from the source), and their hashes are
printed so that docs/ASSETS.md can be updated.

Usage::

    python tools/build_bundled_fonts.py            # download, verify, subset, write fontdata/
    python tools/build_bundled_fonts.py --full-cjk # all of CJK Unified Ideographs (about 5.2 MB)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import pathlib
import sys
import tempfile
import urllib.request
import zipfile

OUT = pathlib.Path(__file__).resolve().parent.parent / "mlaser" / "cad" / "fontdata"

CJK_URL = "https://github.com/notofonts/noto-cjk/raw/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"
CJK_SHA = "faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"
CJK_LICENSE_URL = "https://github.com/notofonts/noto-cjk/raw/Sans2.004/LICENSE"
CJK_LICENSE_SHA = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
AR_URL = ("https://github.com/notofonts/arabic/releases/download/NotoSansArabic-v2.013/"
          "NotoSansArabic-v2.013.zip")
AR_SHA = "1301aceaea84c501cf2e6dcfb3182e2328c8eae5725817fcb239672bda7154f1"
AR_MEMBER = "NotoSansArabic/unhinted/ttf/NotoSansArabic-Regular.ttf"
AR_MEMBER_SHA = "bd86ca02f087d7f3c3788ba458fb6b73744c7639ed276b8d870dba6def6c40d0"

CJK_OUT = "NotoSansSC-Regular-subset.otf"
AR_OUT = "NotoSansArabic-Regular-subset.ttf"

COMMON_RANGES = [
    (0x0020, 0x007E), (0x00A0, 0x024F),      # Basic Latin, Latin-1, Latin Extended-A/B
    (0x0370, 0x03FF), (0x0400, 0x04FF),      # Greek, Cyrillic
    (0x2000, 0x206F), (0x20A0, 0x20CF),      # General Punctuation, currency
    (0x2100, 0x21FF), (0x2200, 0x22FF),      # letterlike, number forms, arrows, math operators
    (0x2460, 0x24FF), (0x2500, 0x257F),      # enclosed alphanumerics, box drawing
    (0x3000, 0x30FF),                        # CJK symbols and punctuation, hiragana, katakana
    (0xFF00, 0xFFEF),                        # half-/full-width forms
]
ARABIC_RANGES = [(0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF),
                 (0xFE70, 0xFEFF), (0x0020, 0x0040), (0x00A0, 0x00A0), (0x200C, 0x200F),
                 (0x25CC, 0x25CC)]


def gb2312() -> set[int]:
    """Every code point of GB 2312 (rows 0xA1..0xF7, cells 0xA1..0xFE)."""
    out = set()
    for b1 in range(0xA1, 0xF8):
        for b2 in range(0xA1, 0xFF):
            try:
                out.add(ord(bytes([b1, b2]).decode("gb2312")))
            except UnicodeDecodeError:
                pass
    return out


def _ranges(rs) -> set[int]:
    return {c for a, b in rs for c in range(a, b + 1)}


def fetch(url: str, sha: str, dest: pathlib.Path) -> bytes:
    print(f"download {url}")
    with urllib.request.urlopen(url, timeout=120) as r:      # noqa: S310 - fixed https URLs
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        sys.exit(f"SHA-256 mismatch for {url}: {got} (expected {sha})")
    dest.write_bytes(data)
    return data


def subset(data: bytes, unicodes: set[int], out: pathlib.Path) -> None:
    from fontTools import subset as fts
    from fontTools.ttLib import TTFont

    opt = fts.Options()
    opt.layout_features = ["*"]
    opt.name_IDs = ["*"]
    opt.name_languages = ["*"]
    opt.notdef_outline = True
    opt.hinting = False
    opt.glyph_names = False
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    s = fts.Subsetter(opt)
    s.populate(unicodes=unicodes)
    s.subset(font)
    font.save(str(out))
    print(f"wrote {out.name}: {out.stat().st_size} bytes, {len(font.getGlyphOrder())} glyphs, "
          f"sha256 {hashlib.sha256(out.read_bytes()).hexdigest()}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--full-cjk", action="store_true",
                    help="keep all of CJK Unified Ideographs U+4E00..U+9FFF (about 5.2 MB)")
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mlaser-fonts-") as tmp:
        t = pathlib.Path(tmp)
        cjk = fetch(CJK_URL, CJK_SHA, t / "cjk.otf")
        lic = fetch(CJK_LICENSE_URL, CJK_LICENSE_SHA, t / "LICENSE")
        arz = fetch(AR_URL, AR_SHA, t / "arabic.zip")
        z = zipfile.ZipFile(io.BytesIO(arz))
        ar = z.read(AR_MEMBER)
        ar_lic = z.read("OFL.txt")
        if hashlib.sha256(ar).hexdigest() != AR_MEMBER_SHA:
            sys.exit(f"SHA-256 mismatch for {AR_MEMBER}")
        u = gb2312() | _ranges(COMMON_RANGES)
        if a.full_cjk:
            u |= _ranges([(0x4E00, 0x9FFF)])
        subset(cjk, u, a.out / CJK_OUT)
        subset(ar, _ranges(ARABIC_RANGES), a.out / AR_OUT)
        for name, data in (("OFL-NotoSansCJK.txt", lic), ("OFL-NotoSansArabic.txt", ar_lic)):
            (a.out / name).write_bytes(data)
            print(f"wrote {name}, sha256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
