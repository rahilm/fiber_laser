"""tools/i18n_check.py passes on the repository and fails on a broken tree."""
import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))


def _mod():
    spec = importlib.util.spec_from_file_location("i18n_check", os.path.join(ROOT, "tools", "i18n_check.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_repository_passes():
    errors, _warnings = _mod().check(ROOT, [])
    assert errors == []


def _broken_tree(tmp_path):
    cat = tmp_path / "mlaser" / "i18n"
    cat.mkdir(parents=True)
    en = {"format": "mlaser-i18n/1", "language": "en", "name": "English",
          "strings": {"panel.count": "{n} parts", "panel.ok": "OK", "panel.bad": "%d left"},
          "args": {"panel.count": ["n"], "panel.ok": ["x"]},
          "legacy": {"mf1": "panel.ok", "mf2": "panel.gone"}}
    (cat / "en.json").write_text(json.dumps(en), encoding="utf-8")
    de = {"language": "de", "name": "Deutsch",
          "strings": {"panel.count": "{anzahl} Teile", "panel.stale": "x"}}
    (cat / "de.json").write_text(json.dumps(de), encoding="utf-8")
    (cat / "fr.ts").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="fr">'
        '<context><name>panel</name><message id="panel.count"><source>{n} parts</source>'
        '<translation>{m} pièces</translation></message></context></TS>', encoding="utf-8")
    (tmp_path / "mlaser" / "mod.py").write_text(
        "def f(w):\n"
        "    w.tr_.tr('panel.missing')\n"
        "    w.tr_.format('panel.count', count=3)\n"
        "    '{x}'.format('panel.not_a_lookup')\n", encoding="utf-8")
    return tmp_path


def test_broken_tree_fails(tmp_path):
    root = _broken_tree(tmp_path)
    errors, warnings = _mod().check(str(root), [], legacy_scan=False)
    text = "\n".join(errors)
    assert "panel.missing" in text and "id missing" in text            # unknown id in code
    assert "['count'] not placeholders" in text                         # wrong keyword
    assert "panel.bad: printf" in text                                  # printf in English
    assert "panel.ok: args" in text                                     # args vs. text
    assert "legacy key mf2" in text
    assert "de.json: panel.count: placeholders" in text
    assert "de.json: panel.stale: id not in en.json" in text
    assert "fr.ts: panel.count: placeholders" in text
    assert "not_a_lookup" not in text
    assert any("untranslated" in w for w in warnings)
    # the command line exits 1
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "i18n_check.py"), "--root", str(root),
                        "--no-legacy-scan"], capture_output=True, text=True)
    assert r.returncode == 1 and "error(s)" in r.stdout


def _code_tree(tmp_path, code: str, strings: dict, other: dict | None = None):
    cat = tmp_path / "mlaser" / "i18n"
    cat.mkdir(parents=True)
    import re
    args = {k: list(dict.fromkeys(re.findall(r"\{(\w+)\}", v))) for k, v in strings.items()}
    en = {"format": "mlaser-i18n/1", "language": "en", "name": "English", "strings": strings,
          "args": {k: v for k, v in args.items() if v}, "legacy": {}}
    (cat / "en.json").write_text(json.dumps(en), encoding="utf-8")
    if other is not None:
        (cat / "ru.json").write_text(json.dumps({"language": "ru", "name": "ru", "strings": other}),
                                     encoding="utf-8")
    (tmp_path / "mlaser" / "mod.py").write_text(code, encoding="utf-8")
    return str(tmp_path)


def test_literals_fragments_and_plurals_are_caught(tmp_path):
    """UI evaluation F8: text that bypasses the catalogue, sentences glued from fragments and
    English-only plurals fail the check; '# i18n: ok' exempts a line."""
    root = _code_tree(tmp_path, (
        "def f(w, tr, n, QMessageBox, QLabel, i18n):\n"
        "    QMessageBox.question(w, 'Job running', tr('a.ok'))\n"
        "    w.setText('Error: ' + str(n))\n"
        "    QLabel('LayerPara.xml')  # i18n: ok (a file name)\n"
        "    w.setText(tr('a.ok') + ' more')\n"
        "    w.setText(f\"{tr('a.ok')} {n} left\")\n"
        "    w.setText(f\"{n} part{'s' if n != 1 else ''}\")\n"
        "    w.setText(tr.plural('a.parts', n))\n"
        "    w.setText(i18n.plural('a.gone', n))\n"
        "    w.setText(tr.format('a.parts.other', n=n))\n"
        "    w.setText(u.fmt(n, 'length'))\n"), {
            "a.ok": "OK", "a.parts.one": "{n} part", "a.parts.other": "{n} parts", "a.lost.one": "x",
            "a.hack": "{n} file(s)"},
        {"a.parts.few": "{n} детали", "a.parts.many": "{m} деталей"})
    errors, _ = _mod().check(root, [], legacy_scan=False)
    text = "\n".join(errors)
    assert "question(): literal text 'Job running'" in text
    assert "setText(): literal text 'Error: '" in text
    assert "LayerPara" not in text                               # marked
    assert text.count("translated fragments joined by code") == 2
    assert "English-only plural" in text
    assert "a.gone.other missing" in text and "a.parts.other missing" not in text
    assert "plural a.lost.one without a.lost.other" in text
    assert "a.hack: '(s)' plural" in text
    assert "ru.json: a.parts.few" not in text                    # a category English does not use
    assert "ru.json: a.parts.many: placeholders" in text
    assert "'length'" not in text                                # a unit kind is not a text
    _mod().main(["--root", root, "--report"])
