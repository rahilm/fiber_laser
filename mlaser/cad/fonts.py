"""Font files for text outlines, found without Qt (fontTools name tables).

Lookup order (:func:`locate`):

1. an explicit font file (``family`` is a path to a .ttf/.ttc/.otf file, or ``font_file=``);
2. an installed family, searched by its name table in the usual font folders of Windows, macOS
   and Linux (:func:`find_font_file`; well-known file names first, e.g. ``simsun.ttc`` for
   SimSun, as the DXF TEXT import did before);
3. a font bundled with mlaser (``mlaser/cad/fontdata``: Noto Sans SC subset for Latin, Greek,
   Cyrillic and GB 2312 Chinese, Noto Sans Arabic subset; SIL OFL 1.1, see
   ``tools/build_bundled_fonts.py``).

The bundled fonts are also the per-character fallback of the shaper
(:mod:`mlaser.cad.shaping`): a character missing in the chosen font is drawn from the first
bundled font that has it.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Optional

FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc")
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontdata")
#: (family, file) of the bundled fonts, in fallback order
BUNDLED_FONTS: tuple[tuple[str, str], ...] = (
    ("Noto Sans SC", "NotoSansSC-Regular-subset.otf"),
    ("Noto Sans Arabic", "NotoSansArabic-Regular-subset.ttf"),
)
BUNDLED_FAMILY = BUNDLED_FONTS[0][0]
#: installed families tried, in this order, when the requested one is missing (cad.text default
#: face gp79 宋体 / "Segoe UI", then common Linux/macOS faces)
FALLBACK_FAMILIES = ("SimSun", "Segoe UI", "DejaVu Sans", "Liberation Sans", "Arial")

_FONT_FILES: dict = {}
_FONT_HINTS = {"simsun": ("simsun.ttc", "simsun.ttf"), "宋体": ("simsun.ttc",), "nsimsun": ("simsun.ttc",),
               "wenquanyi micro hei": ("wqy-microhei.ttc",)}


@dataclass(frozen=True)
class FontRef:
    """One face of a font file: ``path``, collection ``index`` and the family it was found as."""
    path: str
    index: int = 0
    family: str = ""
    bundled: bool = False


def font_dirs() -> list[str]:
    """The font folders of this host that exist (Windows, macOS, Linux, user folders)."""
    home = os.path.expanduser("~")
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
            "/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts"),
            "/usr/share/fonts", "/usr/local/share/fonts", os.path.join(home, ".fonts"),
            os.path.join(home, ".local", "share", "fonts")]
    return [d for d in dirs if d and os.path.isdir(d)]


def font_names(path: str) -> list[tuple[int, set]]:
    """[(index in the collection, {family names, lower case})] of a font file (fontTools, lazily)."""
    from fontTools.ttLib import TTCollection, TTFont
    fonts = TTCollection(path, lazy=True).fonts if path.lower().endswith((".ttc", ".otc")) \
        else [TTFont(path, lazy=True)]
    out = []
    for i, f in enumerate(fonts):
        try:
            names = {str(n).strip().lower() for n in f["name"].names if n.nameID in (1, 16)}
        except Exception:   # noqa: BLE001
            names = set()
        out.append((i, names))
    return out


def _system_files() -> list[str]:
    files = []
    for d in font_dirs():
        for root, _ds, fs in os.walk(d):
            files += [os.path.join(root, f) for f in fs if f.lower().endswith((".ttf", ".ttc", ".otf"))]
    return files


def find_font_file(family: str) -> Optional[tuple[str, int]]:
    """(path, index) of the installed font file of ``family`` (searched in :func:`font_dirs`;
    well-known file names first, then by path), cached.  This is the search the DXF TEXT import
    (GGO_BEZIER records, parity D-16) has always used: keep it unchanged."""
    key = family.strip().lower()
    if key in _FONT_FILES:
        return _FONT_FILES[key]
    found = None
    try:
        files = _system_files()
        hints = _FONT_HINTS.get(key, ())
        files.sort(key=lambda f: (os.path.basename(f).lower() not in hints, f))
        for f in files:
            try:
                for i, names in font_names(f):
                    if key in names:
                        found = (f, i)
                        break
            except Exception:   # noqa: BLE001
                continue
            if found:
                break
    except Exception:   # noqa: BLE001
        found = None
    _FONT_FILES[key] = found
    return found


def bundled_fonts() -> list[FontRef]:
    """The bundled fonts that are present (a source checkout without them gives [])."""
    out = []
    for fam, fn in BUNDLED_FONTS:
        p = os.path.join(BUNDLED_DIR, fn)
        if os.path.isfile(p):
            out.append(FontRef(p, 0, fam, True))
    return out


def bundled_font(family: Optional[str] = None) -> Optional[FontRef]:
    """The bundled font of ``family`` (case-insensitive); without a family the default one."""
    fs = bundled_fonts()
    if family is None:
        return fs[0] if fs else None
    key = family.strip().lower()
    return next((f for f in fs if f.family.lower() == key), None)


def is_font_file(name: Optional[str]) -> bool:
    return bool(name) and str(name).lower().endswith(FONT_EXTS) and os.path.isfile(str(name))


@functools.lru_cache(maxsize=None)
def _faces() -> tuple[tuple[str, int, frozenset, str, str], ...]:
    """(path, index, {family names}, display family, subfamily) of every installed face."""
    from fontTools.ttLib import TTCollection, TTFont
    out = []
    for f in sorted(_system_files()):
        try:
            fonts = TTCollection(f, lazy=True).fonts if f.lower().endswith(".ttc") else [TTFont(f, lazy=True)]
            for i, ft in enumerate(fonts):
                nt = ft["name"]
                fams = frozenset(str(n).strip().lower() for n in nt.names if n.nameID in (1, 16))
                disp = (nt.getDebugName(16) or nt.getDebugName(1) or "").strip()
                sub = (nt.getDebugName(17) or nt.getDebugName(2) or "").strip().lower()
                if disp:
                    out.append((f, i, fams, disp, sub))
        except Exception:   # noqa: BLE001
            continue
    return tuple(out)


def find_face(family: str, bold: bool = False, italic: bool = False) -> Optional[FontRef]:
    """The installed face of ``family`` matching ``bold`` / ``italic`` best (subfamily name),
    else :func:`find_font_file`'s face; None when the family is not installed."""
    if bold or italic:
        key = family.strip().lower()
        best, score = None, -1
        for path, i, fams, _disp, sub in _faces():
            if key not in fams:
                continue
            is_b = any(w in sub for w in ("bold", "black", "heavy"))
            is_i = any(w in sub for w in ("italic", "oblique"))
            s = (is_b == bold) * 2 + (is_i == italic)
            if s > score:
                best, score = (path, i), s
        if best is not None and score == 3:
            return FontRef(best[0], best[1], family)
    ff = find_font_file(family)
    return FontRef(ff[0], ff[1], family) if ff else None


def locate(family: Optional[str], bold: bool = False, italic: bool = False,
           font_file: Optional[str] = None) -> Optional[FontRef]:
    """Explicit file -> installed family -> bundled font of that name -> None (see the module
    docstring).  ``family`` may itself be a font file path."""
    for f in (font_file, family):
        if f and is_font_file(f):
            return FontRef(os.path.abspath(str(f)), 0, os.path.splitext(os.path.basename(str(f)))[0])
    if family:
        r = find_face(family, bold, italic)
        if r is not None:
            return r
        return bundled_font(family)
    return None


def resolve_family(family: Optional[str]) -> str:
    """``family`` when it can be found (file, installed or bundled), else the first installed
    family of :data:`FALLBACK_FAMILIES`, else the bundled :data:`BUNDLED_FAMILY`."""
    if family and locate(family) is not None:
        return family
    for f in FALLBACK_FAMILIES:
        if find_font_file(f) is not None:
            return f
    return BUNDLED_FAMILY


def default_ref() -> Optional[FontRef]:
    """The font text falls back to when nothing else is found: the bundled default."""
    return bundled_font()


def available_families() -> list[str]:
    """Installed scalable families (display names) plus the bundled ones, sorted."""
    fams = {disp for _p, _i, _f, disp, _s in _faces()}
    fams |= {f.family for f in bundled_fonts()}
    return sorted(fams, key=str.lower)


def find_by_filename(name: str) -> Optional[FontRef]:
    """An installed (or bundled) font file whose base name is ``name`` (case-insensitive), as a
    DXF text style stores it (``simsun.ttc``, ``arial.ttf``)."""
    base = os.path.basename(str(name).replace("\\", "/")).lower()
    if not base:
        return None
    for f in sorted(_system_files()):
        if os.path.basename(f).lower() == base:
            return FontRef(f, 0, os.path.splitext(os.path.basename(f))[0])
    for b in bundled_fonts():
        if os.path.basename(b.path).lower() == base:
            return b
    return None
