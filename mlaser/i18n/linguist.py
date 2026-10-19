"""Qt Linguist bridge: ``.ts`` files made from the JSON catalogues, and back (Qt-free).

``mlaser/i18n/en.json`` stays the source of truth.  For a translator the catalogue is exported
as a standard Qt Linguist file (TS 2.1), edited with Qt Linguist (``pyside6-linguist``) or any
TS tool, then either

* imported back into ``mlaser/i18n/<code>.json`` (:func:`ts_to_catalogue`, ``python -m
  mlaser.i18n ts-import``), which is what ships, or
* compiled to ``<code>.qm`` with ``lrelease`` (``pyside6-lrelease``) and dropped into
  ``mlaser/i18n/qm/`` or the user's language folder (``~/.mlaser/lang``), where
  :class:`mlaser.ui.i18n.Translator` loads it at run time (no rebuild needed).

Mapping of one catalogue entry to a TS ``<message>``:

==================  ===================================================================
TS element          content
==================  ===================================================================
``<context>``       the id's prefix (``cmd`` for ``cmd.machine.home``)
``id`` attribute    the readable id
``<source>``        the English text (named ``{placeholders}`` kept as they are)
``<comment>``       the readable id again: Qt's disambiguation, so two ids with the same
                    English text stay two messages; ``QTranslator.translate(context,
                    english, id)`` is the run-time lookup
``<extracomment>``  the placeholders a translation must keep and the original's key
``<translation>``   the text; ``type="unfinished"`` when missing or when the English
                    source changed since it was translated
==================  ===================================================================

Existing work is carried over when a ``.ts`` is regenerated: the shipped ``<code>.json``, an
earlier ``.ts`` (``merge``), and optionally the texts imported from an original install
(:mod:`mlaser.i18n.importer`; their printf placeholders are rewritten to the named ones and the
entries stay unfinished for review).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from . import BRACE_RE, FORMAT, PRINTF_RE, Catalogue, catalogue

TS_VERSION = "2.1"


@dataclass
class TsMessage:
    id: str
    context: str
    source: str
    translation: str = ""
    finished: bool = False
    comment: str = ""


@dataclass
class TsFile:
    language: str
    source_language: str = "en"
    messages: list[TsMessage] = field(default_factory=list)

    def by_id(self) -> dict[str, TsMessage]:
        return {m.id: m for m in self.messages}


def context_of(sid: str) -> str:
    return sid.split(".", 1)[0] if "." in sid else "main"


def placeholders(text: str) -> set[str]:
    """Named ``{x}`` placeholders of ``text``."""
    return set(BRACE_RE.findall(text or ""))


def printf_to_named(text: str, names: list[str]) -> Optional[str]:
    """An imported original text with ``%d``/``%s`` placeholders rewritten to the catalogue's
    named ones (in ``args`` order); None when the counts do not match."""
    parts = PRINTF_RE.findall(text)
    if not parts:
        return text
    if len(parts) != len(names):
        return None
    it = iter(names)
    return PRINTF_RE.sub(lambda _m: "{" + next(it) + "}", text)


def _imported_texts(code: str, en: Catalogue) -> dict[str, str]:
    from . import importer
    table = importer.imported().get(code, {}).get("legacy_strings", {})
    out = {}
    for key, text in table.items():
        sid = en.legacy.get(key)
        if sid and text.strip():
            t = printf_to_named(text, en.args.get(sid, []))
            if t is not None:
                out[sid] = t
    return out


def build_ts(code: str, translations: Optional[dict[str, str]] = None,
             merge: Optional[TsFile] = None, seed: Optional[dict[str, str]] = None,
             en: Optional[Catalogue] = None) -> TsFile:
    """The :class:`TsFile` of language ``code`` for every id of ``en``.  Translations, in order of
    preference: ``merge`` (an earlier ``.ts``: finished there and its source still the current
    English text), ``translations`` (default: the shipped ``<code>.json``), ``seed`` (unfinished
    suggestions, e.g. imported original texts)."""
    en = en or catalogue("en")
    if translations is None:
        translations = catalogue(code).strings if code != "en" else {}
    old = merge.by_id() if merge else {}
    seed = seed or {}
    rev = {sid: k for k, sid in en.legacy.items()}
    msgs = []
    for sid in sorted(en.strings):
        src = en.strings[sid]
        m = TsMessage(sid, context_of(sid), src)
        o = old.get(sid)
        if o is not None and o.translation and o.finished and o.source == src:
            m.translation, m.finished = o.translation, True
        elif translations.get(sid, "").strip():
            m.translation, m.finished = translations[sid], True
        elif o is not None and o.translation:
            m.translation = o.translation             # English changed: review
        elif seed.get(sid, "").strip():
            m.translation = seed[sid]
        notes = []
        if en.args.get(sid):
            notes.append("placeholders: " + ", ".join("{" + a + "}" for a in en.args[sid]))
        if sid in rev:
            notes.append(f"original key {rev[sid]}")
        m.comment = "; ".join(notes)
        msgs.append(m)
    return TsFile(code, "en", msgs)


def dumps_ts(ts: TsFile) -> str:
    root = ET.Element("TS", {"version": TS_VERSION, "language": ts.language,
                             "sourcelanguage": ts.source_language})
    ctxs: dict[str, ET.Element] = {}
    for m in sorted(ts.messages, key=lambda m: (m.context, m.id)):
        c = ctxs.get(m.context)
        if c is None:
            c = ctxs[m.context] = ET.SubElement(root, "context")
            ET.SubElement(c, "name").text = m.context
        e = ET.SubElement(c, "message", {"id": m.id})
        ET.SubElement(e, "source").text = m.source
        ET.SubElement(e, "comment").text = m.id
        if m.comment:
            ET.SubElement(e, "extracomment").text = m.comment
        t = ET.SubElement(e, "translation", {} if m.finished else {"type": "unfinished"})
        t.text = m.translation
    ET.indent(root, "    ")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n'
            + ET.tostring(root, encoding="unicode") + "\n")


def parse_ts(text: str) -> TsFile:
    """Read a TS file (ours, or one saved by Qt Linguist / lupdate)."""
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1))
    except ET.ParseError as e:
        raise ValueError(f"not a Qt Linguist .ts file: {e}") from None
    if root.tag != "TS":
        raise ValueError("not a Qt Linguist .ts file: root element is not <TS>")
    ts = TsFile(root.get("language", ""), root.get("sourcelanguage", "en") or "en")
    for c in root.iter("context"):
        cname = c.findtext("name", "")
        for e in c.iter("message"):
            sid = e.get("id") or e.findtext("comment") or ""
            t = e.find("translation")
            typ = t.get("type", "") if t is not None else "unfinished"
            ts.messages.append(TsMessage(
                sid, cname, e.findtext("source", ""), (t.text or "") if t is not None else "",
                finished=typ not in ("unfinished", "obsolete", "vanished"),
                comment=e.findtext("extracomment", "")))
    return ts


def ts_to_catalogue(ts: TsFile, name: str = "", include_unfinished: bool = False,
                    en: Optional[Catalogue] = None) -> dict:
    """A ``mlaser-i18n/1`` catalogue (``<code>.json``) of the translated messages whose id is in
    ``en`` (finished ones only unless ``include_unfinished``)."""
    en = en or catalogue("en")
    strings = {m.id: m.translation for m in ts.messages
               if m.id in en.strings and m.translation.strip() and (m.finished or include_unfinished)}
    return {"format": FORMAT, "language": ts.language, "name": name or ts.language,
            "strings": dict(sorted(strings.items()))}


def dumps_catalogue(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def find_lrelease() -> Optional[str]:
    """``pyside6-lrelease`` (PySide6's wheel ships it) or a Qt ``lrelease`` on PATH, or None."""
    import os
    import shutil
    import sys
    for name in ("pyside6-lrelease", "lrelease", "lrelease-qt6"):
        for cand in (os.path.join(os.path.dirname(sys.executable), name), shutil.which(name)):
            if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def compile_qm(ts_path: str, qm_path: Optional[str] = None) -> str:
    """Compile ``ts_path`` with lrelease; returns the .qm path (RuntimeError without lrelease)."""
    import os
    import subprocess
    exe = find_lrelease()
    if exe is None:
        raise RuntimeError("lrelease not found (install PySide6, which ships pyside6-lrelease)")
    qm_path = qm_path or os.path.splitext(ts_path)[0] + ".qm"
    r = subprocess.run([exe, "-silent", ts_path, "-qm", qm_path], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(qm_path):
        raise RuntimeError(f"lrelease failed: {(r.stderr or r.stdout).strip()}")
    return qm_path
