"""The alarm/fault remedy texts come from the catalogue, unchanged in English (F8, docs/SAFETY.md §10)."""
from __future__ import annotations

from types import SimpleNamespace

from mlaser import i18n
from mlaser.ui import safety_texts as S


def _alarm(code=None, kind="card"):
    return SimpleNamespace(code=code, kind=kind)


def test_every_remedy_id_is_in_the_catalogue():
    en = i18n.catalogue("en").strings
    ids = list(S.KIND_IDS.values()) + list(S.CODE_IDS.values()) + [sid for _, sid in S.FAULT_IDS] + [S.DEFAULT_ID]
    for sid in ids:
        assert en.get(sid, "").strip(), sid


def test_english_wording_unchanged():
    # verbatim English of the texts (the button is "Reset alarm" everywhere since UX audit W5)
    assert S.remedy(_alarm(kind="estop")) == (
        "Emergency stop pressed or the e-stop circuit is open. Make the machine safe, release "
        "the e-stop, then press Reset alarm and arm the laser again. Home the axes before cutting again.")
    assert S.remedy(_alarm(8025, "card")) == (
        "EtherCAT bus fault. Check the bus cables between card and drives and the drive power, "
        "then press Reset alarm (the card restarts).")
    assert S.remedy("link lost (heartbeat)") == (
        "The PC lost the connection to the motion card. Check the network cable, the "
        "switch and the card power. The card was halted as far as it could be reached. "
        "When the link is back press Reset alarm, home if needed, then Resume.")
    assert S.remedy("interlock door open") == "A door or guard is open. Close it, press Reset alarm, then Resume."
    assert S.remedy("something else") == (
        "Check the reason above, make the machine safe, then press Reset alarm. After a reset the "
        "machine is disarmed; continue with Resume from the breakpoint.")


def test_matching_order_and_fallbacks():
    # code beats kind; unknown code -> kind; unknown kind -> card
    assert S.remedy(_alarm(9007, "estop")) == S.BY_CODE[9007]
    assert S.remedy(_alarm(1234, "limit")) == S.BY_KIND["limit"]
    assert S.remedy(_alarm(1234, "nonsense")) == S.BY_KIND["card"]
    assert S.remedy(_alarm(None, "servo")) == S.BY_KIND["servo"]
    # fault prefixes: first containing prefix wins, then the first word
    assert S.remedy("Interlock gas low") == S.BY_FAULT[2][1]
    assert S.remedy("interlock something") == S.BY_FAULT[1][1]   # first "interlock ..." entry
    assert S.remedy("card alarm 8025") == S.BY_FAULT[5][1]


def test_with_the_ui_translator_english():
    from mlaser.ui.i18n import Translator
    tr = Translator()
    for kind in S.KIND_IDS:
        assert S.remedy(_alarm(kind=kind), tr) == S.remedy(_alarm(kind=kind))
    for code in S.CODE_IDS:
        assert S.remedy(_alarm(code), tr) == S.BY_CODE[code]
    for prefix, text in S.BY_FAULT:
        assert S.remedy(prefix, tr) == text


class _FakeTr:
    """A translator with a German catalogue for two ids."""
    DE = {"remedy.estop": "Not-Aus gedrückt.", "remedy.fault.interlock_door": "Tür offen."}

    def has(self, key):
        return key in i18n.catalogue("en").strings

    def tr(self, key, default=None):
        return self.DE.get(key) or i18n.catalogue("en").strings.get(key, default or key)


def test_a_translation_overrides_the_english():
    tr = _FakeTr()
    assert S.remedy(_alarm(kind="estop"), tr) == "Not-Aus gedrückt."
    assert S.remedy("interlock door", tr) == "Tür offen."
    assert S.remedy(_alarm(kind="limit"), tr) == S.BY_KIND["limit"]      # untranslated -> English
