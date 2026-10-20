"""UI string lookup with live language switching.

Texts come from:

1. mlaser's own catalogues, ``mlaser/i18n/<code>.json`` (English ships; see
   :mod:`mlaser.i18n` for the format: readable ids, named placeholders, and a ``legacy`` map
   from the original program's opaque keys such as ``mf15``/``pd125`` to those ids);
2. compiled Qt Linguist translations, ``<code>.qm`` in ``mlaser/i18n/qm/`` (shipped) or in the
   user's language folder (``~/.mlaser/lang``, which wins), made from the ``.ts`` files of
   :mod:`mlaser.i18n.linguist` with ``lrelease``; looked up with ``QTranslator.translate(context,
   English text, id)``;
3. translations imported from the user's own Mlaser install (:mod:`mlaser.i18n.importer`,
   stored under ``~/.mlaser/lang``), or read directly from an install folder passed as
   ``Translator(lang_dir=...)``. Those are keyed by the original's keys.

``tr(key)`` accepts either a readable id (``"cmd.machine.home"``) or an original key
(``"mf15"``). Lookup order: the current language (our JSON catalogue, then a .qm, then the
imported original texts), then English, then ``default``, then the key itself.  The JSON is the
reviewed source of truth (a finished .ts is imported into it); a .qm lets a translator try a
translation in the running program without that step; the original's texts come last because
they are machine-imported, keyed by opaque ids and use printf placeholders. ``format(key, ...)`` fills placeholders, both our
named ``{minutes}`` ones and the printf ones of imported texts.
"""
from __future__ import annotations

import os
import re
import sys
import weakref
from dataclasses import dataclass
from typing import Callable, Optional

from mlaser import i18n as cat
from mlaser.i18n import importer

_MAC = sys.platform == "darwin"

#: a log line that starts with an opaque key of the original: ``mp29: ...``, ``mf625 upgrade: ...``
_LOG_KEY = re.compile(r"^([a-z]{1,3}\d+(?:_\d+)?)\b((?:\s+[A-Za-z][\w-]*){0,3})?\s*(?::\s*(.*))?$", re.S)
#: card command words in a log line: ``([9999, 5, 0, 0])``, ``(mode 0, [2, 0x3, 0])``, ``, INFERRED``
_LOG_CMD = re.compile(r"\s*\((?:[^()]*\[[\d, x.:+-]*\][^()]*|INFERRED)\)|,\s*INFERRED\b|\s*\[[\d, x]+\]")
#: an opaque key inside a log line, in parentheses: ``(pd2415)``, ``(mp22 offline)``
_LOG_INLINE = re.compile(r"\s*\(([a-z]{1,3}\d+(?:_\d+)?)(?:\s+[a-z]+)?\)")


def _native_keys(text: str) -> str:
    from .shell.keymap import native_text
    return native_text(text, "darwin")


#: shipped compiled Linguist translations (``<code>.qm``)
QM_DIR = os.path.join(os.path.dirname(cat.__file__), "qm")


def qm_paths(code: str, import_dir: Optional[str] = None) -> list[str]:
    """Existing ``<code>.qm`` files, highest priority first (user folder, then shipped)."""
    dirs = [str(import_dir) if import_dir else str(importer.lang_store()), QM_DIR]
    return [p for p in (os.path.join(d, f"{code}.qm") for d in dirs) if os.path.isfile(p)]


@dataclass(frozen=True)
class LanguageInfo:
    index: int          # the original's lang.ini ``Lang=`` index
    code: str
    name: str           # native display name


LANGUAGES: list[LanguageInfo] = [
    LanguageInfo(1, "en", "English"),
    LanguageInfo(0, "zh", "简体中文"),
    LanguageInfo(2, "ru", "Русский"),
    LanguageInfo(3, "de", "Deutsch"),
    LanguageInfo(4, "es", "Español"),
    LanguageInfo(5, "pt", "Português"),
    LanguageInfo(6, "fr", "Français"),
    LanguageInfo(7, "it", "Italiano"),
    LanguageInfo(8, "pl", "Polski"),
    LanguageInfo(9, "vi", "Tiếng Việt"),
    LanguageInfo(10, "tr", "Türkçe"),
]


class Translator:
    """Key -> text lookup with live language switching.

    Widgets register with :meth:`bind`; :meth:`set_language` re-applies every binding.

    ``lang_dir``: optional original Mlaser install (or its ``Lang`` folder) to read translations
    from directly, without importing them. ``import_dir``: where imported translations live
    (default :func:`mlaser.i18n.importer.lang_store`).
    """

    def __init__(self, lang_dir: Optional[str] = None, language: str = "en",
                 import_dir: Optional[str] = None):
        self.lang_dir = lang_dir
        self.import_dir = import_dir
        self.en = cat.catalogue("en")
        self._imported: dict[str, dict[str, str]] = {}
        self._qm: dict[str, list] = {}
        self._bindings: list[tuple[weakref.ref, str, Callable, tuple]] = []
        self.language = "en"
        self.reload()
        self.set_language(language)

    # -- loading -----------------------------------------------------------
    def reload(self) -> None:
        """Re-read imported translations (and the direct ``lang_dir`` if given)."""
        tables: dict[str, dict[str, str]] = {}
        for code, data in importer.imported(self.import_dir).items():
            tables[code] = dict(data.get("legacy_strings", {}))
        if self.lang_dir:
            for f in importer.find_lang_files(self.lang_dir):
                parsed = importer.parse_file(f)
                if parsed is not None:
                    tables.setdefault(parsed[0], {}).update(parsed[2])
        self._imported = tables
        self._qm = {}

    def import_from(self, path: str) -> list[str]:
        """Import an original install's language files (see :mod:`mlaser.i18n.importer`)."""
        codes = importer.import_path(path, self.import_dir)
        self.reload()
        return codes

    @staticmethod
    def info(code: str) -> LanguageInfo:
        for li in LANGUAGES:
            if li.code == code:
                return li
        raise KeyError(code)

    def available(self) -> list[LanguageInfo]:
        """Languages with texts: English, shipped catalogues and imported translations."""
        have = {"en"} | set(cat.shipped_languages()) | {c for c, t in self._imported.items() if t}
        have |= {li.code for li in LANGUAGES if qm_paths(li.code, self.import_dir)}
        return [li for li in LANGUAGES if li.code in have]

    # -- lookup ------------------------------------------------------------
    def _lookup(self, key: str, code: str) -> Optional[str]:
        sid = self.en.resolve(key)
        if code != "en":
            if sid is not None:
                t = cat.catalogue(code).strings.get(sid)
                if t and t.strip():
                    return t
                t = self._qm_text(code, sid)
                if t:
                    return t
            table = self._imported.get(code)
            if table:
                legacy = cat.legacy_key(key) or key
                t = table.get(legacy)
                if t and t.strip():
                    return t
        if sid is not None:
            return self.en.strings.get(sid)
        return None

    def _qm_translators(self, code: str) -> list:
        qs = self._qm.get(code)
        if qs is None:
            qs = []
            paths = qm_paths(code, self.import_dir)
            if paths:
                from PySide6.QtCore import QTranslator
                for p in paths:
                    q = QTranslator()
                    if q.load(p):
                        qs.append(q)
            self._qm[code] = qs
        return qs

    def _qm_text(self, code: str, sid: str) -> Optional[str]:
        qs = self._qm_translators(code)
        if not qs:
            return None
        src = self.en.strings.get(sid, "")
        ctx = sid.split(".", 1)[0] if "." in sid else "main"
        for q in qs:
            t = q.translate(ctx, src, sid)
            if t and t.strip():
                return t
        return None

    def has(self, key: str) -> bool:
        return self.en.resolve(key) is not None or key in self._imported.get(self.language, {})

    def tr(self, key: str, default: Optional[str] = None) -> str:
        t = self._lookup(key, self.language)
        if t is None:
            t = default if default is not None else key
        # shortcut text as the platform writes it: "(Ctrl+K)" is "(⌘K)" on macOS (Native track)
        return _native_keys(t) if _MAC and "+" in t else t

    def format(self, key: str, *args, default: Optional[str] = None, **kwargs) -> str:
        """Translated text with its placeholders filled (positional values follow the catalogue's
        ``args`` order, keyword values fill named placeholders)."""
        sid = self.en.resolve(key)
        names = self.en.args.get(sid, []) if sid else []
        return cat.format_text(self.tr(key, default), names, args, kwargs)

    def log_text(self, msg: str) -> str:
        """A log line as the user reads it: never an opaque key of the original program.

        Emitters write ``"<key>: <detail>"`` (``"mp29: move to (0.000, 0.000)"``), sometimes
        ``"<key> <word>: <detail>"``; the key becomes its text (``"Go to origin: move to ..."``).
        A key without a text, or whose text needs placeholders the line does not carry, is
        dropped and the detail stays.  Opaque keys inside the detail (``(mp22 offline)``,
        ``(pd2415)``) and the card command words (``([9999, 5, 0, 0])``, ``INFERRED``) are
        removed: they are in the file log, not for the operator (UI audit T-5)."""
        m = _LOG_KEY.match(msg)
        if m is not None:
            key, words, detail = m.group(1), (m.group(2) or "").strip(), (m.group(3) or "").strip()
            text = self.tr(key) if self.has(key) else ""
            if "{" in text or "%" in text:
                text = ""
            if words and all(w.lower() in text.lower() for w in words.split()):
                words = ""                       # "mf625 upgrade" -> "Extended card upgrade"
            head = " ".join(x for x in (text.rstrip(".:"), words) if x) if text else ""
            if head and detail:
                msg = f"{head}: {detail}"
            elif head:
                msg = head
            else:
                msg = " ".join(x for x in (words, detail) if x) or text
        msg = _LOG_CMD.sub("", msg)
        msg = _LOG_INLINE.sub(lambda mm: "" if not self.has(mm.group(1)) else mm.group(0), msg)
        msg = _LOG_INLINE.sub("", msg)
        msg = " ".join(msg.split())
        return msg[:1].upper() + msg[1:] if msg else msg

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
            setter(self.format(key, *fmt_args, default=default) if fmt_args else self.tr(key, default))
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
