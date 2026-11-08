"""String catalogues (Qt-free).

Format
------
``mlaser/i18n/<code>.json`` (only ``en.json`` ships)::

    {
      "format": "mlaser-i18n/1",
      "language": "en", "name": "English",
      "strings": {"cmd.machine.home": "Home X/Y", "panel.estimate": "... {minutes} min ...", ...},
      "args":    {"panel.estimate": ["minutes", "seconds", ...]},
      "legacy":  {"mf15": "cmd.machine.home", ...}
    }

* ``strings`` maps a **readable id** (``area.name``) to the text. Placeholders are named,
  Python ``str.format`` style (``{minutes}``).
* ``args`` gives the positional order of the placeholders. It matches the order of the
  printf placeholders in the original program's text, so translations imported from an
  original install (which use ``%d``/``%s``) format with the same call.
* ``legacy`` maps the original program's opaque keys (``mf15``, ``pd125``, ...) to ids. Every
  existing ``tr("mf15")`` call site keeps working, and an imported translation, which is keyed
  by those opaque keys, lines up with our ids.

Plural forms
------------
A text that depends on a count is a set of ids ``<base>.<category>`` with the CLDR cardinal
categories ``zero``, ``one``, ``two``, ``few``, ``many``, ``other`` (:func:`plural_category`):
English has ``<base>.one`` and ``<base>.other``; a translation adds the categories its language
uses (Russian ``one``/``few``/``many``/``other``, Chinese only ``other``). Look one up with
``plural("<base>", n, ...)`` here (Qt-free code) or ``Translator.plural`` in the UI; the count is
the ``{n}`` placeholder. A missing category falls back to ``other``, then to English.

A catalogue for another language needs only ``language``, ``name`` and ``strings`` (same ids).
Languages we do not ship come from the user's own Mlaser install through
:mod:`mlaser.i18n.importer`.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

HERE = os.path.dirname(__file__)
FORMAT = "mlaser-i18n/1"

#: printf conversions used by the original's strings (``%d``, ``%0.2f``, ``%s``, ``%.1f``...)
PRINTF_RE = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?l*[dfsuxgcS]")
BRACE_RE = re.compile(r"\{([A-Za-z_]\w*)(?::[^{}]*)?\}")


@dataclass
class Catalogue:
    code: str
    name: str
    strings: dict[str, str] = field(default_factory=dict)
    args: dict[str, list[str]] = field(default_factory=dict)
    legacy: dict[str, str] = field(default_factory=dict)

    def plural_id(self, base: str, n: float) -> Optional[str]:
        """The id of ``base``'s form for count ``n`` in this catalogue's language (or None)."""
        for cat_ in (plural_category(self.code or "en", n), "other"):
            sid = f"{base}.{cat_}"
            if self.strings.get(sid, "").strip():
                return sid
        return None

    def resolve(self, key: str) -> Optional[str]:
        """Readable id for ``key`` (an id or an original key), or None if unknown."""
        if key in self.strings:
            return key
        return self.legacy.get(key)

    def text(self, key: str) -> Optional[str]:
        sid = self.resolve(key)
        return self.strings.get(sid) if sid is not None else None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Catalogue":
        return cls(data.get("language", ""), data.get("name", ""), dict(data.get("strings", {})),
                   {k: list(v) for k, v in data.get("args", {}).items()}, dict(data.get("legacy", {})))


@lru_cache(maxsize=None)
def catalogue(code: str = "en") -> Catalogue:
    """The shipped catalogue for ``code`` (an empty one if we do not ship that language)."""
    path = os.path.join(HERE, f"{code}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return Catalogue.from_json(json.load(fh))
    except OSError:
        return Catalogue(code, code)


#: the CLDR cardinal plural categories, in CLDR order
PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")


def _operands(n: float) -> tuple[int, int]:
    """CLDR operands ``i`` (integer digits) and ``v`` (number of visible fraction digits)."""
    if isinstance(n, int):
        return abs(int(n)), 0
    txt = repr(abs(float(n)))
    if "e" in txt or "E" in txt:
        txt = f"{abs(float(n)):f}".rstrip("0")
    head, _, frac = txt.partition(".")
    frac = frac.rstrip("0") if frac != "0" else ""
    return int(head), len(frac)


def plural_category(lang: str, n: float) -> str:
    """The CLDR cardinal plural category of ``n`` in ``lang`` (``"one"``, ``"few"``, ``"other"``...).

    Covers the languages mlaser offers (``mlaser.ui.i18n.LANGUAGES``) and the common families;
    an unknown language uses the English rule. Integers are the usual input; a float such as
    ``1.5`` follows the rule's fraction operands (English: ``other``)."""
    code = (lang or "en").replace("-", "_").split("_")[0].lower()
    i, v = _operands(n)
    if code in ("zh", "ja", "ko", "vi", "th", "id", "ms", "lo", "my"):
        return "other"
    if code in ("ru", "uk", "be"):
        if v:
            return "other"
        if i % 10 == 1 and i % 100 != 11:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    if code == "pl":
        if v:
            return "other"
        if i == 1:
            return "one"
        if 2 <= i % 10 <= 4 and not 12 <= i % 100 <= 14:
            return "few"
        return "many"
    if code in ("cs", "sk"):
        if v:
            return "many"
        return "one" if i == 1 else "few" if 2 <= i <= 4 else "other"
    if code in ("fr", "pt"):
        # fr / pt (Brazil): 0 and 1 are "one"; whole millions are "many"
        if i in (0, 1):
            return "one"
        if not v and i and i % 1_000_000 == 0:
            return "many"
        return "other"
    if code in ("es", "it"):
        if i == 1 and not v:
            return "one"
        if not v and i and i % 1_000_000 == 0:
            return "many"
        return "other"
    if code == "ar":
        if v:
            return "other"
        if i == 0:
            return "zero"
        if i in (1, 2):
            return ("one", "two")[i - 1]
        if 3 <= i % 100 <= 10:
            return "few"
        if 11 <= i % 100 <= 99:
            return "many"
        return "other"
    if code == "he":
        if i == 1 and not v:
            return "one"
        if i == 2 and not v:
            return "two"
        return "other"
    # en, de, nl, sv, da, no, tr, el, hu, fi, et, ... : one for exactly 1
    return "one" if i == 1 and not v else "other"


def is_plural_id(sid: str) -> bool:
    """True for ``<base>.<category>`` ids."""
    return sid.rsplit(".", 1)[-1] in PLURAL_CATEGORIES and "." in sid


def shipped_languages() -> list[str]:
    return sorted(f[:-5] for f in os.listdir(HERE) if f.endswith(".json"))


def legacy_key(key: str) -> Optional[str]:
    """The original program's key for a readable id (or ``key`` itself if it is one)."""
    en = catalogue("en")
    if key in en.legacy:
        return key
    return _reverse_legacy().get(key)


@lru_cache(maxsize=1)
def _reverse_legacy() -> dict[str, str]:
    return {sid: k for k, sid in catalogue("en").legacy.items()}


def format_text(text: str, names: list[str], args: tuple = (), kwargs: Optional[dict] = None) -> str:
    """Fill ``text``'s placeholders.

    Named ``{x}`` placeholders take ``kwargs`` (or ``args`` in ``names`` order); printf
    placeholders from an imported original translation take the values in ``names`` order.
    Text that cannot be formatted is returned unchanged.
    """
    kw = dict(kwargs or {})
    if args:
        kw.update({n: v for n, v in zip(names, args) if n not in kw})
    try:
        if BRACE_RE.search(text):
            return text.format(**kw) if kw or not args else text.format(*args)
        if PRINTF_RE.search(text):
            vals = tuple(kw[n] for n in names) if names and all(n in kw for n in names) else tuple(args)
            return text % vals
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return text


# -------------------------------------------------------------------------- Qt-free lookups
#: The UI registers its translator here (``mlaser.ui.i18n``) so Qt-free code (services,
#: reports) speaks the current UI language. Without one, the English catalogue is used.
_provider: Optional[Any] = None


def set_provider(provider: Optional[Any]) -> None:
    """Route :func:`tr` / :func:`plural` through ``provider`` (an object with ``format(key,
    **kw)`` and ``plural(base, n, **kw)``, the UI's Translator), or back to English (None)."""
    global _provider
    _provider = provider


def tr(key: str, **kwargs: Any) -> str:
    """Text of ``key`` in the current language with its ``{placeholders}`` filled."""
    if _provider is not None:
        return str(_provider.format(key, **kwargs))
    en = catalogue("en")
    sid = en.resolve(key)
    text = en.strings.get(sid, key) if sid else key
    return format_text(text, en.args.get(sid or "", []), (), kwargs)


def plural(base: str, n: float, **kwargs: Any) -> str:
    """The form of ``base`` for the count ``n`` in the current language; ``{n}`` is the count."""
    if _provider is not None:
        return str(_provider.plural(base, n, **kwargs))
    en = catalogue("en")
    sid = en.plural_id(base, n) or base
    kwargs.setdefault("n", n)
    return format_text(en.strings.get(sid, base), en.args.get(sid, []), (), kwargs)
