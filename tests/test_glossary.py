"""The English catalogue follows the glossary and the case rules (UI audit DESIGN.md section 6,
AUDIT T-2, T-4, T-5, D-1).

* one word per concept: **Home** is machine homing (the reference switches), **Go to origin**
  moves to the program zero, **Set origin here** sets it; **Hold** / **Continue** pause and go on
  with a job; **Dry run**; **Rapid**; **Arm laser** / **Disarm laser**; **Continuous** jog;
  **Practice**;
* plain English labels: no ``Go Origin``, ``Dry Cut``, ``Empty Move``, ``OneKey``, no CamelCase
  words (other than product names), no ``<State>`` angle brackets;
* sentence case: no Title Case labels, capitals only for the state pill words and STOP;
* the log never shows an opaque key of the original program (``mp18:``, ``gp0:``).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAT = json.loads((ROOT / "mlaser" / "i18n" / "en.json").read_text(encoding="utf-8"))
S: dict[str, str] = CAT["strings"]
LEGACY: dict[str, str] = CAT["legacy"]


def text(key: str) -> str:
    return S[LEGACY.get(key, key)]


# --------------------------------------------------------------------------- one word per concept
BANNED = [r"\bGo Origin\b", r"\bGo Origion\b", r"\bDry Cut\b", r"\bEmpty Move\b", r"\bPractise\b",
          r"\bOneKey\b", r"\bGo to Manual Origin\b", r"\bGo to zero\b", r"\bMove Path\b", r"\bTwinkle\b"]


@pytest.mark.parametrize("pattern", BANNED)
def test_no_banned_term(pattern):
    hits = [f"{k}: {v}" for k, v in S.items() if re.search(pattern, v, re.I)]
    assert not hits, hits


#: machine homing: the reference switches
HOMING = ["mf15", "mf16", "mf17", "mf222", "mf482", "mp29_1", "mp30", "af13", "ec14",
          "cmd.machine.home_af", "ws.machine.home_x", "ws.machine.home_y", "ws.machine.home_xy",
          "ws.machine.home_xyz", "wizard.comm.homing_button", "wizard.focus.home_button"]
#: the program zero / work origin
GO_TO_ORIGIN = ["mp29", "mp164", "mp130", "ws.machine.return_origin"]
SET_ORIGIN = ["mf514", "mp129", "ws.machine.set_origin"]


@pytest.mark.parametrize("key", HOMING)
def test_homing_says_home(key):
    assert text(key).startswith("Home"), (key, text(key))


@pytest.mark.parametrize("key", GO_TO_ORIGIN)
def test_work_origin_says_go_to_origin(key):
    assert text(key) == "Go to origin", (key, text(key))


@pytest.mark.parametrize("key", SET_ORIGIN)
def test_set_origin_here(key):
    assert text(key) == "Set origin here", (key, text(key))


def test_homing_prompt_is_about_the_home_switches():
    # mf156 asks before homing (FUN_005b5de0); mp79 before the move to (0, 0)
    assert "home switches" in text("mf156")
    assert "origin" in text("mp79") and "home" not in text("mp79").lower()


def test_hold_continue_dry_run_rapid_arm_continuous():
    assert text("mp55") == "Hold"                    # pause the running job
    assert text("run.btn.hold") == "Hold" and text("run.btn.continue") == "Continue"
    assert text("mp57") == "Dry run" and text("pd311") == "Dry run"
    assert text("run.btn.arm") == "Arm laser" and text("run.btn.disarm") == "Disarm laser"
    assert text("ws.machine.step_cont") == "Continuous"
    assert "rapid" in text("cmd.show.move_path").lower()
    assert "rapid" in text("pd575").lower()          # Auto focus axis "empty move" speed
    assert text("mp64") == "Resume from breakpoint"  # the one place "resume" stays
    job_ids = [k for k, v in S.items() if re.fullmatch(r"Pause|Resume", v)]
    # a media "pause" of the preview player is not the job's hold
    assert set(job_ids) <= {"preview.pause"}, job_ids


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_jog_pad_keys_say_the_right_thing():
    """The Run Control jog pad: the centre cell (mp29) moves to the origin, mp29_1 homes X/Y,
    mp30 homes Z; the coordinate section's mp164 goes to the origin and mf514 sets it."""
    src = _src("mlaser/ui/panels.py")
    assert '_pad_button("mp29", "jog_zero"' in src
    assert '_text_button("mp29_1", self._sys_home)' in src
    assert '_pad_button("mp30", "jog_home_z"' in src
    assert '_text_button("mp164", self.ctl.go_origin_point)' in src
    assert '_text_button("mf514", self.ctl.set_origin)' in src
    assert text("mp29") == "Go to origin"
    assert text("mp29_1") == "Home X/Y" and text("mp30") == "Home Z"


def test_machine_homing_section_uses_home():
    """Machine > Homing and jog: every homing button says Home, the origin buttons say Go to /
    Set origin."""
    src = _src("mlaser/ui/workspaces/machine.py")
    for key in re.findall(r'\("(ws\.machine\.home_\w+)", "machine\.home', src):
        assert text(key).startswith("Home"), key
    assert text("ws.machine.homing_box") == "Homing"


# --------------------------------------------------------------------------- plain English
#: product, file and protocol names written as their makers write them
CAMEL_OK = {"EtherCAT", "MaxPhotonics", "HardPara", "ManuPara", "LayerPara", "NexCut", "GitHub", "kWh",
            "JumpAddTime", "ipAdd", "macOS", "iOS"}


def test_no_camelcase_words():
    bad = []
    for k, v in S.items():
        for tok in re.findall(r"(?<![\w.%\\/$-])[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*(?![\w.(/\\-])", v):
            if tok not in CAMEL_OK:
                bad.append(f"{k}: {tok}")
    assert not bad, bad


def test_no_angle_bracket_states():
    bad = [f"{k}: {v}" for k, v in S.items() if re.search(r"<[A-Z][A-Za-z ]*>", v)]
    assert not bad, bad


# --------------------------------------------------------------------------- sentence case
WORD = re.compile(r"\{[^{}]*\}|<[^<>]*>|[A-Za-z][A-Za-z0-9']*")
#: names that keep their capital inside a sentence-case label: workspaces, user modes, products
PROPER = {"Mlaser", "Windows", "Linux", "Onshape", "EtherCAT", "MaxPhotonics", "Raycus", "IPG", "NexCut",
          "Machine", "Process", "Nest", "Design", "Run", "Report", "Setup", "Layers", "Library",
          "Properties", "Job", "Queue", "Operator", "Engineer", "Service", "Settings", "Esc", "Ctrl",
          "Cmd", "Space", "Start", "Stop", "Hold", "Continue", "Frame", "Arm", "Disarm", "Resume", "Gaussian",
          "Home", "Abort", "None", "True", "False",
          "LayerPara", "HardPara", "ManuPara", "Lang", "PINs", "IDs", "Celsius", "Fahrenheit"}


def _keep(tok: str) -> bool:
    ups = sum(1 for c in tok if c.isupper())
    return (tok in PROPER or ups >= 2 or (len(tok) == 1 and tok.isupper())
            or (ups and any(c.isdigit() for c in tok)))


def _title_words(part: str) -> list[str]:
    words = [w for w in WORD.findall(part) if w[0] not in "{<"]
    return [w for w in words[1:] if w[0].isupper() and not _keep(w)]


def _is_label(v: str) -> bool:
    return len(v) <= 70 and not re.search(r"[.?!;:]\s|\n|\(\*", v.rstrip(".?!:"))


def _parts(v: str) -> list[str]:
    if re.match(r"^[A-Z0-9][\w /()&+,'-]*\.[A-Z0-9]", v):     # Group.Name parameter labels
        return v.split(".", 1)
    return [v]


def test_labels_are_sentence_case():
    bad = []
    for k, v in S.items():
        if "›" in v or not _is_label(v):
            continue
        for part in _parts(v):
            tw = _title_words(part)
            if tw:
                bad.append(f"{k}: {v!r} ({', '.join(tw)})")
    assert not bad, f"{len(bad)} Title Case label(s):\n" + "\n".join(bad[:60])


#: words in capitals: acronyms, units, protocols
ACRONYMS = {"FTC", "API", "LED", "PWM", "LAN", "PIN", "PLC", "VFD", "PDF", "SMTP", "URL", "FIFO", "DXF",
            "MCC", "COM", "FPGA", "NEG", "POS", "SSL", "STARTTLS", "CSV", "ETA", "TCP", "CAD", "CCW", "TLS",
            "IPG", "XYZ", "ISO", "EUR", "USD", "CHF", "CLI", "SVG", "HTTP", "MES", "POST", "JSON", "HATCH",
            "CAM", "MAC", "LOCALAPPDATA", "OFF", "DIN", "RGB", "USB", "BMP", "PNG", "JPG", "GIF", "TXT", "INI",
            "XML", "DAT", "NC", "AF", "ZF", "EC", "DI", "DO", "DA", "AD", "IO", "PC", "ID", "UTC", "GPS",
            "RAM", "CPU", "GPU", "LCR", "IP", "PPM", "RPM", "UID", "CNC", "OK", "MDI", "DWG", "SVG", "STEP",
            "STL", "PTFE", "HMI", "ASCII", "UTF", "HSV", "WCAG", "CMYK", "GUI", "UI", "IPS", "QR", "SQL",
            "NFC", "HTML", "CSS", "WAN", "DNS", "DHCP", "MQTT", "OPC", "UA", "RTU", "UDP", "IPV", "MTU"}
#: the state pill / chip words and STOP: capitals by design
STATE_WORDS = {"READY", "ARMED", "DISARMED", "FAULT", "ALARM", "HOLD", "OFFLINE", "SIMULATOR", "DRY", "RUN",
               "RUNNING", "STOP"}
#: safety emphasis kept in capitals (a warning badge, an inverted-logic note)
CAPS_OK_IDS = {"tech.example_badge", "labels.msg.fallback", "wizard.comm.hb_relay_fail", "safety.io.level_off",
               "safety.io.level_on", "safety.io.now_alarm", "notify.msg.fault.title"}


def test_capitals_only_for_state_words_and_stop():
    bad = []
    for k, v in S.items():
        if k in CAPS_OK_IDS:
            continue
        plain = re.sub(r"<[^>]*>|\{[^}]*\}|docs/\S+", "", v)
        for tok in re.findall(r"\b[A-Z]{3,}\b", plain):
            if tok in ACRONYMS:
                continue
            if tok in STATE_WORDS and (k.startswith(("run.state.", "run.alarm.", "shell.chip.", "ws.machine.a_",
                                                      "wizard.chip", "run.btn.stop", "practice.", "run.reason.",
                                                      "shell.welcome."))):
                continue
            bad.append(f"{k}: {tok} in {v[:80]!r}")
    assert not bad, bad


# --------------------------------------------------------------------------- one label per setting
def test_visible_settings_have_distinct_labels():
    """No two visible settings of one section show the same label (the original reused lang
    keys: Match code 1 three times, the fiber and CO2 laser ports...).  Same-name rows of the
    multi-stage layer records (bolt pierce 1..5) are the technology form's (stage columns)."""
    from mlaser.services.config import LABEL_FIXES, registry_settings
    for key, lid in LABEL_FIXES.items():
        assert lid in S, (key, lid)
    seen: dict[tuple[str, str], str] = {}
    dup = []
    for k, s in registry_settings().items():
        if s.home in ("hidden", "process") or not s.label or ":" in k:
            continue
        t = S.get(LEGACY.get(s.label, s.label), s.label)
        prev = seen.setdefault((s.section, t), k)
        if prev != k:
            dup.append(f"{s.section}: {t!r} = {prev} and {k}")
    assert not dup, dup


# --------------------------------------------------------------------------- the log
RAW_KEY = re.compile(r"(?<![\w.])[a-z]{1,3}\d+(?:_\d+)?(?=[:\s)])")


@pytest.mark.parametrize("line, want", [
    ("mp18: the last run ended abnormally", "The last run ended abnormally"),
    ("gp0: hardware is not connected", "Hardware is not connected"),
    ("mf625 upgrade: needs a connected motion card (mp22 offline)", "Extended card upgrade: needs a connected motion card"),
    ("mp29: move to (0.000, 0.000)", "Go to origin: move to (0.000, 0.000)"),
    ("mf21: alarms cleared ([9999, 5, 0, 0])", "Clear alarm: alarms cleared"),
    ("mf705: batch cut is not enabled (pd2415)", "Batch cut: batch cut is not enabled"),
    ("mf91: 0 mark(s) cleared", "Clear error marks: 0 mark(s) cleared"),
    ("plain text stays", "Plain text stays"),
])
def test_log_text_never_shows_a_raw_key(line, want):
    from mlaser.ui.i18n import Translator
    got = Translator().log_text(line)
    assert got == want
    assert not RAW_KEY.search(got), got


def test_log_emitters_have_readable_texts():
    """Every ``log("<key>: ...")`` call in the code turns into a line without a raw key."""
    from mlaser.ui.i18n import Translator
    tr = Translator()
    pat = re.compile(r"(?:log|_log|emit)\(\s*f?([\"'])([a-z]{1,3}\d+(?:_\d+)?\b[^\"']*)\1")
    bad = []
    for path in sorted((ROOT / "mlaser").rglob("*.py")):
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            line = re.sub(r"\{[^{}]*\}", "1", m.group(2))
            out = tr.log_text(line)
            if RAW_KEY.search(out):
                bad.append(f"{path.relative_to(ROOT)}: {m.group(2)!r} -> {out!r}")
    assert not bad, bad


def test_log_panel_shows_readable_text():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from mlaser.ui.i18n import Translator
    from mlaser.ui.panels import LogPanel
    p = LogPanel(Translator())
    try:
        for line in ("mp18: the last run ended abnormally", "gp0: hardware is not connected", "mf15",
                     "mf625 upgrade: needs a connected motion card (mp22 offline)"):
            p.log(line)
        shown = p.text.toPlainText()
        assert "Hardware is not connected" in shown and "Home X/Y" in shown
        assert not RAW_KEY.search(re.sub(r"\d\d:\d\d:\d\d", "", shown)), shown
    finally:
        p.deleteLater()


def test_ast_parses():          # the patterns above read real source: keep them honest
    ast.parse(_src("mlaser/ui/panels.py"))
