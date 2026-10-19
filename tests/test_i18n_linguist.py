"""Qt Linguist bridge: en.json -> .ts -> parse / -> .json, .qm compiled with lrelease and loaded
by the Translator."""
import json
import os

import pytest

from mlaser.i18n import catalogue, linguist as L


def test_ts_round_trip_covers_every_id():
    en = catalogue("en")
    ts = L.build_ts("de", translations={})
    text = L.dumps_ts(ts)
    back = L.parse_ts(text)
    assert back.language == "de" and back.source_language == "en"
    got = back.by_id()
    assert set(got) == set(en.strings)
    for sid, src in en.strings.items():
        m = got[sid]
        assert m.source == src and m.context == L.context_of(sid)
        assert not m.finished and m.translation == ""
    # placeholders are documented for the translator
    sid = next(s for s in en.strings if en.args.get(s))
    assert "{" + en.args[sid][0] + "}" in got[sid].comment


def test_translations_carried_over_and_imported_back(tmp_path):
    en = catalogue("en")
    sid = "cmd.machine.home" if "cmd.machine.home" in en.strings else sorted(en.strings)[0]
    other = sorted(en.strings)[1]
    ts = L.build_ts("de", translations={sid: "Zum Ursprung"})
    m = ts.by_id()
    assert m[sid].finished and m[sid].translation == "Zum Ursprung"
    # an earlier .ts wins; a changed English source turns it unfinished (needs review)
    old = L.parse_ts(L.dumps_ts(ts))
    old.by_id()[other].translation, old.by_id()[other].finished = "Alt", True
    old.by_id()[other].source = "something else"
    ts2 = L.build_ts("de", translations={}, merge=old)
    m2 = ts2.by_id()
    assert m2[sid].translation == "Zum Ursprung" and m2[sid].finished
    assert m2[other].translation == "Alt" and not m2[other].finished
    cat = L.ts_to_catalogue(ts2, "Deutsch")
    assert cat["strings"] == {sid: "Zum Ursprung"} and cat["language"] == "de"
    assert L.ts_to_catalogue(ts2, include_unfinished=True)["strings"][other] == "Alt"
    json.loads(L.dumps_catalogue(cat))


def test_printf_seed_becomes_named():
    assert L.printf_to_named("%d von %d", ["a", "b"]) == "{a} von {b}"
    assert L.printf_to_named("%d", ["a", "b"]) is None
    assert L.printf_to_named("kein", []) == "kein"


def test_parse_errors():
    with pytest.raises(ValueError, match="not a Qt Linguist"):
        L.parse_ts("<html/>")
    with pytest.raises(ValueError, match="not a Qt Linguist"):
        L.parse_ts("garbage <<")


def test_cli_ts_and_import(tmp_path):
    from mlaser.i18n.__main__ import main
    out = tmp_path / "fr.ts"
    assert main(["ts", "fr", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8").replace(
        '<translation type="unfinished" />', "<translation>X</translation>", 1)
    out.write_text(text, encoding="utf-8")
    j = tmp_path / "fr.json"
    assert main(["ts-import", str(out), "-o", str(j), "--name", "Français"]) == 0
    data = json.loads(j.read_text(encoding="utf-8"))
    assert len(data["strings"]) == 1 and data["name"] == "Français"


@pytest.mark.skipif(L.find_lrelease() is None, reason="lrelease not available")
def test_qm_is_loaded_by_translator(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from mlaser.ui.i18n import Translator
    en = catalogue("en")
    sid = "cmd.machine.home" if "cmd.machine.home" in en.strings else sorted(en.strings)[0]
    legacy = next((k for k, v in en.legacy.items() if v == sid), None)
    ph = next(s for s in sorted(en.strings) if en.args.get(s))
    ph_text = " ".join("{" + a + "}" for a in en.args[ph]) + " (de)"
    ts = L.build_ts("de", translations={sid: "Zum Ursprung (qm)", ph: ph_text})
    tsf = tmp_path / "de.ts"
    tsf.write_text(L.dumps_ts(ts), encoding="utf-8")
    qm = L.compile_qm(str(tsf), str(tmp_path / "de.qm"))
    assert os.path.getsize(qm) > 30
    t = Translator(import_dir=str(tmp_path), language="de")
    assert "de" in [li.code for li in t.available()]
    assert t.tr(sid) == "Zum Ursprung (qm)"
    if legacy:
        assert t.tr(legacy) == "Zum Ursprung (qm)"
    vals = {a: f"<{a}>" for a in en.args[ph]}
    assert t.format(ph, **vals).startswith(" ".join(vals.values()))
    # untranslated ids fall back to English
    other = next(s for s in sorted(en.strings) if s not in (sid, ph))
    assert t.tr(other) == en.strings[other]
    t.set_language("en")
    assert t.tr(sid) == en.strings[sid]
