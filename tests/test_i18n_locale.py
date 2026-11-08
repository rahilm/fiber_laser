"""i18n beyond the string catalogue (UI evaluation F8): plural forms, Qt's own translations,
locale-aware number display, and right-to-left layouts that keep machine axes and number + unit
texts the right way round."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QLocale, Qt
from PySide6.QtWidgets import QApplication, QDoubleSpinBox

from mlaser import i18n as cat
from mlaser.services import units as U


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------- plural rules
@pytest.mark.parametrize("lang,n,want", [
    ("en", 0, "other"), ("en", 1, "one"), ("en", 2, "other"), ("en", 1.5, "other"),
    ("de", 1, "one"), ("tr", 1, "one"), ("it", 5, "other"),
    ("fr", 0, "one"), ("fr", 1, "one"), ("fr", 2, "other"), ("fr", 1_000_000, "many"),
    ("pt", 0, "one"), ("es", 1_000_000, "many"),
    ("ru", 1, "one"), ("ru", 21, "one"), ("ru", 11, "many"), ("ru", 2, "few"), ("ru", 24, "few"),
    ("ru", 12, "many"), ("ru", 5, "many"), ("ru", 1.5, "other"),
    ("pl", 1, "one"), ("pl", 2, "few"), ("pl", 22, "few"), ("pl", 12, "many"), ("pl", 21, "many"),
    ("pl", 5, "many"), ("zh", 1, "other"), ("vi", 3, "other"), ("xx", 1, "one"),
])
def test_plural_category(lang, n, want):
    assert cat.plural_category(lang, n) == want


def _fake_catalogues(monkeypatch, extra_ru=None):
    en = cat.Catalogue("en", "English", {"t.parts.one": "{n} part", "t.parts.other": "{n} parts"},
                       {"t.parts.one": ["n"], "t.parts.other": ["n"]})
    ru = cat.Catalogue("ru", "Русский", dict(extra_ru if extra_ru is not None else {
        "t.parts.one": "{n} деталь", "t.parts.few": "{n} детали", "t.parts.many": "{n} деталей",
        "t.parts.other": "{n} детали"}))
    real = cat.catalogue
    monkeypatch.setattr(cat, "catalogue", lambda code="en": {"en": en, "ru": ru}.get(code) or real(code))
    return en, ru


def test_plural_lookup_english_and_russian(monkeypatch):
    _fake_catalogues(monkeypatch)
    from mlaser.ui.i18n import Translator
    t = Translator()
    t.en = cat.catalogue("en")
    assert [t.plural("t.parts", n) for n in (1, 2)] == ["1 part", "2 parts"]
    monkeypatch.setattr(t, "available", lambda: [])
    t.language = "ru"
    assert [t.plural("t.parts", n) for n in (1, 3, 5, 21)] == ["1 деталь", "3 детали", "5 деталей", "21 деталь"]


def test_plural_missing_category_falls_back_to_other_then_english(monkeypatch):
    _fake_catalogues(monkeypatch, {"t.parts.other": "{n} шт."})
    from mlaser.ui.i18n import Translator
    t = Translator()
    t.en = cat.catalogue("en")
    t.language = "ru"
    assert t.plural("t.parts", 5) == "5 шт."
    _fake_catalogues(monkeypatch, {})
    assert t.plural("t.parts", 5) == "5 parts"


def test_qt_free_plural_and_provider(monkeypatch):
    _fake_catalogues(monkeypatch)
    monkeypatch.setattr(cat, "_provider", None)
    assert cat.plural("t.parts", 1) == "1 part" and cat.plural("t.parts", 7) == "7 parts"

    class P:
        def plural(self, base, n, **kw):
            return f"<{base}:{n}>"

        def format(self, key, **kw):
            return f"<{key}>"
    cat.set_provider(P())
    try:
        assert cat.plural("t.parts", 2) == "<t.parts:2>" and cat.tr("x.y") == "<x.y>"
    finally:
        cat.set_provider(None)


# ---------------------------------------------------------------------------- Qt's own strings
def test_qt_translations_follow_a_non_english_language(app):
    from mlaser.ui.i18n import install_qt_translations
    try:
        assert install_qt_translations("de")
        assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Abbrechen"
    finally:
        install_qt_translations("en")
    assert QCoreApplication.translate("QPlatformTheme", "Cancel") == "Cancel"


def test_qt_translations_only_when_mlaser_has_the_language(app, monkeypatch):
    from mlaser.ui import i18n as ui_i18n
    t = ui_i18n.Translator()
    monkeypatch.setattr(ui_i18n, "_default", t)
    calls = []
    monkeypatch.setattr(ui_i18n, "install_qt_translations", calls.append)
    monkeypatch.setattr(t, "available", lambda: [ui_i18n.LanguageInfo(1, "en", "English")])
    try:
        t.set_language("de")             # no German texts: Qt's strings stay English too
        assert calls[-1] == "en"
        monkeypatch.setattr(t, "available", lambda: [ui_i18n.LanguageInfo(3, "de", "Deutsch")])
        t.set_language("de")
        assert calls[-1] == "de"
    finally:
        cat.set_provider(None)


# ---------------------------------------------------------------------------- numbers
def test_display_numbers_follow_the_locale(app):
    from mlaser.ui.units_qt import QuantitySpinBox, follow_locale
    assert follow_locale(QLocale(QLocale.German, QLocale.Germany)) == ","
    try:
        prev = QLocale()
        QLocale.setDefault(QLocale(QLocale.German, QLocale.Germany))
        q = QuantitySpinBox("length", None, minimum=0, maximum=1000, decimals=2)
        q.setValue(1.5)
        d = QDoubleSpinBox()
        d.setDecimals(2)
        d.setValue(1.5)
        assert q.text() == "1,50 mm" and d.text() == "1,50"          # one decimal separator
        assert U.fmt(1234.5) == "1234,50 mm"
        assert U.mm_text(0.25) == "0,25 mm" and U.mm_size(3000, 1500.5, ".1f") == "3000,0 × 1500,5 mm"
        # typed input: both separators; the value is internal mm, unchanged
        q.lineEdit().setText("2,25")
        q.interpretText()
        assert q.value() == 2.25
        assert U.parse("1.5", "length") == U.parse("1,5", "length") == 1.5
        d.deleteLater()
        q.deleteLater()
    finally:
        QLocale.setDefault(prev)
        U.set_display_locale(".", False)
    assert U.fmt(1234.5) == "1234.50 mm"                             # back: files/CLI style


def test_units_default_is_the_c_locale_for_qt_free_callers():
    """Without a UI (CLI, files) the texts stay as they were: "." and no marks."""
    assert U.decimal_point() == "." and U.fmt(0.1) == "0.10 mm" and U.mm_text(0.3) == "0.3 mm"


# ---------------------------------------------------------------------------- right to left
def test_rtl_keeps_axes_and_number_unit_texts(app):
    from PySide6.QtWidgets import QWidget

    from mlaser.ui.jogpad import JogPad
    from mlaser.ui.units_qt import follow_locale
    from types import SimpleNamespace
    host = QWidget()
    pad = JogPad(SimpleNamespace(tr_=None, config=None), lambda: None, z=True, parent=host)
    host.resize(400, 400)
    QApplication.setLayoutDirection(Qt.RightToLeft)
    try:
        follow_locale(QLocale.c())
        host.show()
        for _ in range(5):
            QApplication.processEvents()
        xp, xm = pad.buttons["X+"], pad.buttons["X-"]
        assert xp.mapTo(host, xp.rect().center()).x() > xm.mapTo(host, xm.rect().center()).x()
        assert pad.buttons["Z+"].mapTo(host, pad.buttons["Z+"].rect().center()).x() > \
            xp.mapTo(host, xp.rect().center()).x()                  # Z column right of the cross
        t = U.fmt(0.1, "length", trim=True)
        assert t == "‎0.1 mm"                                   # one left-to-right run
        assert U.parse(t, "length") == 0.1
    finally:
        QApplication.setLayoutDirection(Qt.LeftToRight)
        for _ in range(3):
            QApplication.processEvents()
        host.deleteLater()
    assert U.fmt(0.1, "length", trim=True) == "0.1 mm"              # follows the change back
