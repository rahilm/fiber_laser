"""UI string catalogue backed by the original Mlaser ``Lang/*.txt`` files.

Evidence
--------
* ``$ORIG/Lang/lang.txt`` (UTF-16LE with BOM): one entry per line, ``key#中文#English``.
  Keys are what MainApp.exe passes to its language module (``语言包`` + key literal pairs
  in MainApp .rdata, e.g. ``mf6`` "Start", ``mf15`` "Go Origin", ``mp54`` "Start").
* ``$ORIG/Lang/<Language>.txt`` (UTF-16): ``key#text`` (one translation only).
* ``$ORIG/Lang/lang.ini`` ``[Global] hintStr`` lists the language indices:
  0 简体中文, 1 English, 2 Russian, 3 German, 4 Spanish, 5 Portuguese, 6 French,
  7 Italian, 8 Polish, 9 Vietnamese, 10 Turkish (``Lang=`` holds the active index).

Values may contain literal ``\\r\\n`` escapes and printf placeholders (``%s``, ``%0.2f``);
they are returned verbatim apart from turning ``\\r\\n``/``\\n`` into newlines.
Missing keys fall back to English, then to the key itself.
"""
from __future__ import annotations

import os
import weakref
from dataclasses import dataclass
from typing import Callable, Optional

RES_DIR = os.path.join(os.path.dirname(__file__), "resources")
LANG_DIR = os.path.join(RES_DIR, "lang")


@dataclass(frozen=True)
class LanguageInfo:
    index: int          # value of lang.ini ``Lang=``
    code: str
    name: str           # native display name
    file: str           # file inside resources/lang
    column: int         # column of the text after the key (1 = zh, 2 = en in lang.txt)


LANGUAGES: list[LanguageInfo] = [
    LanguageInfo(0, "zh", "简体中文", "lang.txt", 1),
    LanguageInfo(1, "en", "English", "lang.txt", 2),
    LanguageInfo(2, "ru", "Русский", "Russian.txt", 1),
    LanguageInfo(3, "de", "Deutsch", "German.txt", 1),
    LanguageInfo(4, "es", "Español", "Spanish.txt", 1),
    LanguageInfo(5, "pt", "Português", "Portuguese.txt", 1),
    LanguageInfo(6, "fr", "Français", "French.txt", 1),
    LanguageInfo(7, "it", "Italiano", "Italian.txt", 1),
    LanguageInfo(8, "pl", "Polski", "Polish.txt", 1),
    LanguageInfo(9, "vi", "Tiếng Việt", "Vietnamese.txt", 1),
    LanguageInfo(10, "tr", "Türkçe", "Turkdili.txt", 1),
]


def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def parse_lang_text(text: str, column: int) -> dict[str, str]:
    """Parse ``key#col1[#col2]`` lines into ``{key: text}`` taking ``column``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "#" not in line:
            continue
        parts = line.split("#")
        key = parts[0].strip()
        if not key:
            continue
        if column < len(parts):
            val = "#".join(parts[column:column + 1])
        else:
            val = parts[-1]
        out[key] = val.replace("\\r\\n", "\n").replace("\\n", "\n")
    return out


class Translator:
    """Key -> text lookup with live language switching.

    Widgets register with :meth:`bind`; :meth:`set_language` re-applies every binding.
    """

    def __init__(self, lang_dir: str = LANG_DIR, language: str = "en"):
        self.lang_dir = lang_dir
        self._tables: dict[str, dict[str, str]] = {}
        self._bindings: list[tuple[weakref.ref, str, Callable, tuple]] = []
        self.language = "en"
        self.set_language(language)

    # -- loading -----------------------------------------------------------
    def _table(self, code: str) -> dict[str, str]:
        if code not in self._tables:
            info = self.info(code)
            path = os.path.join(self.lang_dir, info.file)
            self._tables[code] = parse_lang_text(_read_text(path), info.column) if os.path.exists(path) else {}
        return self._tables[code]

    @staticmethod
    def info(code: str) -> LanguageInfo:
        for li in LANGUAGES:
            if li.code == code:
                return li
        raise KeyError(code)

    def available(self) -> list[LanguageInfo]:
        return [li for li in LANGUAGES if os.path.exists(os.path.join(self.lang_dir, li.file))]

    # -- lookup ------------------------------------------------------------
    def has(self, key: str) -> bool:
        return key in self._table("en") or key in self._table(self.language)

    def tr(self, key: str, default: Optional[str] = None) -> str:
        t = self._table(self.language).get(key)
        if t is None or (t.strip() == "" and key in self._table("en")):
            t = self._table("en").get(key)
        if t is None:
            return default if default is not None else key
        return t

    def tr_param(self, key: str, default: Optional[str] = None) -> tuple[str, str]:
        """Split a parameter label ``Group.Name`` into (group, name)."""
        t = self.tr(key, default)
        if "." in t:
            g, n = t.split(".", 1)
            return g.strip(), n.strip()
        return "", t.strip()

    # -- live switching ----------------------------------------------------
    def bind(self, obj, key: str, setter: Callable, *fmt_args, default: Optional[str] = None) -> None:
        """Call ``setter(text)`` now and on every language change while ``obj`` lives."""
        def apply():
            txt = self.tr(key, default)
            if fmt_args:
                try:
                    txt = txt % fmt_args
                except (TypeError, ValueError):
                    pass
            setter(txt)
        apply()
        try:
            ref = weakref.ref(obj)
        except TypeError:
            ref = lambda: obj  # noqa: E731
        self._bindings.append((ref, key, apply, fmt_args))

    def set_language(self, code: str) -> None:
        self.info(code)
        self.language = code
        alive = []
        for ref, key, apply, args in self._bindings:
            if ref() is None:
                continue
            try:
                apply()
                alive.append((ref, key, apply, args))
            except RuntimeError:  # wrapped C++ object already deleted
                pass
        self._bindings = alive


_default: Optional[Translator] = None


def translator() -> Translator:
    global _default
    if _default is None:
        _default = Translator()
    return _default


def tr(key: str, default: Optional[str] = None) -> str:
    return translator().tr(key, default)
