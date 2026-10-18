"""Mlaser UI language files (``Lang/lang.txt``, ``Lang/<Language>.txt``, ``Lang/lang.ini``).

Reconstructed from LangModule.dll:

* enumerator ``FUN_10006850``: language list = ``["简体中文", "English"]``
  followed by the stem of every ``Lang\\*.txt`` file except ``lang`` (CFileFind order, i.e.
  alphabetical on NTFS).
* ``FUN_10009590`` loader: ``Lang\\lang.txt`` (UTF-16LE with BOM) is split on ``#``; lines with
  at least 3 fields fill map 0 (key -> Chinese) and map 1 (key -> English).  Every other
  language file is split the same way and lines with >= 2 fields fill that language's map
  (key -> field[1]).  Lines with fewer fields are ignored.  Lines are read up to 1000 wchar.
* Current language: ``Lang\\lang.ini`` ``[Global] Lang=<name>`` (ANSI/GBK, GetPrivateProfileString);
  if the name is not in the list, ``GetPrivateProfileInt`` is tried as an index.  Writing uses
  ``WritePrivateProfileStringW(Global, Lang, name)`` (``FUN_100021a0``).
* Lookup ``FUN_10005710``: current language map -> English map (index 1) -> the key itself.
  (std::map insert keeps the first occurrence of duplicated keys.)
"""
from __future__ import annotations

import configparser
import io
from pathlib import Path
from typing import Optional, Union

ZH = "简体中文"
EN = "English"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gbk", "replace")


def _lines(text: str):
    for ln in text.splitlines():
        yield ln[:1000]


class Translator:
    """``tr(key, lang)`` over the Mlaser Lang directory.

    ``lang`` may be a language name (``"English"``, ``"简体中文"``, ``"French"`` ...), an index into
    :attr:`languages`, or ``None`` for the current language (from lang.ini).
    """

    def __init__(self, lang_dir: Union[str, Path, None] = None):
        self.lang_dir = Path(lang_dir) if lang_dir else None
        self.languages: list[str] = [ZH, EN]
        self.maps: list[dict[str, str]] = [{}, {}]
        self.current = 0
        if self.lang_dir is not None:
            self.load(self.lang_dir)

    # ------------------------------------------------------------------ loading
    def load(self, lang_dir: Union[str, Path]) -> None:
        d = Path(lang_dir)
        self.lang_dir = d
        self.languages = [ZH, EN]
        self.maps = [{}, {}]
        main = d / "lang.txt"
        if main.exists():
            for ln in _lines(_read_text(main)):
                f = ln.split("#")
                if len(f) < 3:
                    continue
                self.maps[0].setdefault(f[0], f[1])
                self.maps[1].setdefault(f[0], f[2])
        for p in sorted(d.glob("*.txt"), key=lambda p: p.name.lower()):
            if p.stem.lower() == "lang":
                continue
            m: dict[str, str] = {}
            for ln in _lines(_read_text(p)):
                f = ln.split("#")
                if len(f) >= 2:
                    m.setdefault(f[0], f[1])
            self.languages.append(p.stem)
            self.maps.append(m)
        self.current = self._read_ini(d / "lang.ini")

    def _read_ini(self, ini: Path) -> int:
        if not ini.exists():
            return 0
        raw = ini.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", "replace")
        # the hint line spans several physical lines; only "Lang=" matters
        val: Optional[str] = None
        section = None
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
            elif section == "Global" and s.split("=", 1)[0].strip() == "Lang" and "=" in s:
                val = s.split("=", 1)[1].strip()
        if val is None:
            return 0
        # the value may be GBK while the rest of the file is UTF-8: re-decode that line
        for ln in raw.splitlines():
            if ln.strip().startswith(b"Lang="):
                b = ln.strip()[5:]
                for enc in ("utf-8", "gbk"):
                    try:
                        val = b.decode(enc).strip()
                        break
                    except UnicodeDecodeError:
                        pass
        if val in self.languages:
            return self.languages.index(val)
        try:
            i = int(val)
            return i if 0 <= i < len(self.languages) else 0
        except ValueError:
            return 0

    def save_ini(self, ini: Union[str, Path, None] = None, encoding: str = "gbk") -> None:
        """Set ``[Global] Lang=<current name>`` keeping the rest of lang.ini."""
        ini = Path(ini) if ini else (self.lang_dir / "lang.ini")
        name = self.languages[self.current]
        lines = ini.read_bytes().decode(encoding, "replace").splitlines() if ini.exists() else ["[Global]"]
        out, done = [], False
        for ln in lines:
            if ln.strip().startswith("Lang="):
                out.append("Lang=" + name); done = True
            else:
                out.append(ln)
        if not done:
            out.append("Lang=" + name)
        ini.write_bytes(("\n".join(out) + "\n").encode(encoding, "replace"))

    # ------------------------------------------------------------------ lookup
    def index_of(self, lang: Union[str, int, None]) -> int:
        if lang is None:
            return self.current
        if isinstance(lang, int):
            return lang if 0 <= lang < len(self.languages) else 1
        if lang in self.languages:
            return self.languages.index(lang)
        low = {n.lower(): i for i, n in enumerate(self.languages)}
        aliases = {"zh": 0, "cn": 0, "chinese": 0, "en": 1, "english": 1}
        return low.get(lang.lower(), aliases.get(lang.lower(), 1))

    def set_language(self, lang: Union[str, int]) -> None:
        self.current = self.index_of(lang)

    def tr(self, key: str, lang: Union[str, int, None] = None) -> str:
        m = self.maps[self.index_of(lang)]
        if key in m:
            return m[key]
        if key in self.maps[1]:
            return self.maps[1][key]
        return key

    __call__ = tr

    def keys(self) -> set[str]:
        return set(self.maps[0]) | set(self.maps[1])
