"""Import translations from the user's own Mlaser installation.

mlaser does not redistribute the original program's language files. A user who owns an
installation can point mlaser at it once; this module reads the ``Lang`` folder (``lang.txt``
holds Simplified Chinese and English, ``French.txt``, ``German.txt``... one language each, all
``key#text`` lines, see :mod:`mlaser.i18n.langfile`) and stores the texts in the user's data
directory, keyed by the original's keys. :class:`mlaser.ui.i18n.Translator` maps our readable ids
to those keys through the ``legacy`` table of ``en.json``.

The original's English column is not imported: mlaser's own English catalogue is used instead.

Usage::

    python -m mlaser.i18n import "C:/Program Files/Mlaser"      # install folder, Lang folder or one .txt
    python -m mlaser.i18n list
    python -m mlaser.i18n remove fr
    mlaser --import-lang "C:/Program Files/Mlaser" --lang de    # same, from the app's command line

or File > Import Language Files… in the application.

Storage: ``$MLASER_DATA_DIR/lang/<code>.json`` (default ``~/.mlaser/lang``); override with
``MLASER_LANG_DIR``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional, Union

from mlaser.i18n.langfile import _lines, _read_text

PathLike = Union[str, Path]

#: original language file stem -> (code, native name). ``lang`` = lang.txt (column 1 = zh).
LANGUAGE_FILES: dict[str, tuple[str, str]] = {
    "lang": ("zh", "简体中文"),
    "russian": ("ru", "Русский"), "german": ("de", "Deutsch"), "spanish": ("es", "Español"),
    "portuguese": ("pt", "Português"), "french": ("fr", "Français"), "italian": ("it", "Italiano"),
    "polish": ("pl", "Polski"), "vietnamese": ("vi", "Tiếng Việt"), "turkdili": ("tr", "Türkçe"),
    "turkish": ("tr", "Türkçe"),
}


def lang_store() -> Path:
    """Directory holding imported catalogues."""
    d = os.environ.get("MLASER_LANG_DIR")
    if d:
        return Path(d)
    base = os.environ.get("MLASER_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".mlaser")
    return Path(base) / "lang"


def find_lang_files(path: PathLike) -> list[Path]:
    """Language files under ``path``: an install folder (with ``Lang/``), a ``Lang`` folder, or
    a single ``.txt`` file."""
    p = Path(path)
    if p.is_file():
        return [p]
    for d in (p / "Lang", p / "lang", p):
        if d.is_dir():
            files = sorted(f for f in d.iterdir() if f.suffix.lower() == ".txt")
            if files:
                return files
    return []


def parse_file(path: PathLike) -> Optional[tuple[str, str, dict[str, str]]]:
    """``(code, name, {original key: text})`` for one original language file, or None if the
    file name is not a known language. ``lang.txt`` yields its Chinese column."""
    p = Path(path)
    stem = p.stem.lower()
    if stem not in LANGUAGE_FILES:
        return None
    code, name = LANGUAGE_FILES[stem]
    out: dict[str, str] = {}
    for ln in _lines(_read_text(p)):
        f = ln.split("#")
        if len(f) < (3 if stem == "lang" else 2) or not f[0]:
            continue
        txt = f[1].replace("\\r\\n", "\n").replace("\\n", "\n")
        out.setdefault(f[0], txt)
    return code, name, out


def import_path(path: PathLike, dest: Optional[PathLike] = None) -> list[str]:
    """Import every recognised language file under ``path``; returns the language codes stored."""
    files = find_lang_files(path)
    if not files:
        raise FileNotFoundError(f"no Mlaser language files found in {path}")
    store = Path(dest) if dest else lang_store()
    store.mkdir(parents=True, exist_ok=True)
    codes = []
    for f in files:
        parsed = parse_file(f)
        if parsed is None or not parsed[2]:
            continue
        code, name, texts = parsed
        data = {"format": "mlaser-i18n-import/1", "language": code, "name": name,
                "source": str(Path(f).resolve()), "imported": time.strftime("%Y-%m-%d %H:%M:%S"),
                "legacy_strings": texts}
        tmp = store / f"{code}.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, store / f"{code}.json")
        codes.append(code)
    if not codes:
        raise FileNotFoundError(f"no recognised Mlaser language files in {path}")
    return codes


def imported(dest: Optional[PathLike] = None) -> dict[str, dict]:
    """``{code: {"name":..., "source":..., "legacy_strings": {...}}}`` for every imported language."""
    store = Path(dest) if dest else lang_store()
    out: dict[str, dict] = {}
    if not store.is_dir():
        return out
    for f in sorted(store.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("format") == "mlaser-i18n-import/1" and data.get("language"):
            out[data["language"]] = data
    return out


def remove(code: str, dest: Optional[PathLike] = None) -> bool:
    f = (Path(dest) if dest else lang_store()) / f"{code}.json"
    if f.exists():
        f.unlink()
        return True
    return False
