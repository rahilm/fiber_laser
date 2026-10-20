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
