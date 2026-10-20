"""``--units`` of the command-line tools: the text output follows it (or the config's
``units.system``), the JSON output stays mm with ``"units": "mm"``, and lengths typed on the
command line may carry a unit (plain numbers stay mm)."""
from __future__ import annotations

import io
import json
import os
import re

import pytest

from mlaser import cli
from mlaser.cli import common as C
from mlaser.services.config import ConfigService

DATA = os.path.join(os.path.dirname(__file__), "data")
GEAR = os.path.join(DATA, "dxf", "gear.dxf")
TECH = "S235/3/O2"
METRIC_RE = re.compile(r"(?<![\w/])(?:mm|m|m³|kg)(?![\w²³/])")


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def _text(*argv: str) -> str:
    out = io.StringIO()
    code = cli.main(list(argv), out=out, err=io.StringIO())
    assert code in (0, 1), out.getvalue()
    return out.getvalue()


def _json(*argv: str) -> dict:
    out = io.StringIO()
    cli.main(list(argv) + ["--json"], out=out, err=io.StringIO())
    return json.loads(out.getvalue())


def test_text_output_imperial():
    met = _text("compile", GEAR, "--tech", TECH)
    imp = _text("compile", GEAR, "--tech", TECH, "--units", "imperial")
    assert " m," in met and " mm" in met
    assert " ft," in imp and " in" in imp and not METRIC_RE.search(imp.split("\n", 1)[1]), imp
    # --units before the command works too
    assert _text("--units", "imperial", "compile", GEAR, "--tech", TECH) == imp
    est = _text("estimate", GEAR, "--tech", TECH, "--units", "imperial")
    assert " ft" in est and not METRIC_RE.search(est.replace(GEAR, "")), est


def test_json_stays_mm_whatever_the_units():
    a = _json("compile", GEAR, "--tech", TECH)
    b = _json("compile", GEAR, "--tech", TECH, "--units", "imperial")
    assert a["units"] == b["units"] == "mm"
    assert a["job"]["cut_length_mm"] == b["job"]["cut_length_mm"] > 0
    assert a["job"]["bounds"] == b["job"]["bounds"]
    err = _json("compile", "missing.dxf", "--units", "imperial")
    assert err["units"] == "mm" and not err["ok"]


def test_default_is_the_configs_unit_system(dirs):
    cfg = ConfigService(dirs / "c.json", load=False)
    cfg.set("units.system", "imperial")
    cfg.save()
    imp = _text("compile", GEAR, "--tech", TECH, "--config", str(dirs / "c.json"))
    assert " ft," in imp
    assert " m," in _text("compile", GEAR, "--tech", TECH, "--config", str(dirs / "c.json"), "--units", "metric")
    assert " m," in _text("compile", GEAR, "--tech", TECH)        # the user's (fresh) config: metric


def test_lengths_on_the_command_line():
    assert C.parse_length("3") == 3.0                      # a plain number is mm, as always
    assert C.parse_length("0.125in") == pytest.approx(3.175)
    assert C.parse_length('1/8"') == pytest.approx(3.175)
    assert cli._size("3000x1500") == (3000.0, 1500.0)
    assert cli._size("120inx60in") == pytest.approx((3048.0, 1524.0))
    assert cli._size("3000 x 1500 mm".replace(" mm", "mm")) == (3000.0, 1500.0)
    assert cli._start("1in,2in") == pytest.approx((25.4, 50.8))
    with pytest.raises(C.CliError):
        cli._size("axb")
    with pytest.raises(C.CliError):
        C.parse_length("3 psi")
    res = _json("validate", GEAR, "--tech", TECH, "--sheet", "1inx1in", "--units", "imperial")
    bad = [i for i in res["issues"] if i["code"] == "exceeds_sheet"]
    assert bad and bad[0]["sheet"] == [25.4, 25.4] and "mm" in bad[0]["message"]
    txt = _text("validate", GEAR, "--tech", TECH, "--sheet", "1inx1in", "--units", "imperial")
    line = next(ln for ln in txt.splitlines() if "does not fit" in ln)
    assert "1 × 1 in" in line and " mm" not in line


def test_bad_units_is_a_usage_error():
    out = io.StringIO()
    assert cli.main(["compile", GEAR, "--units", "furlong", "--json"], out=out, err=io.StringIO()) == C.EXIT_USAGE


def test_validate_preflight_messages_in_output_units():
    """The pre-flight's own messages (out of travel) follow --units in the text output; the
    JSON keeps the mm text."""
    txt = _text("validate", GEAR, "--travel", "10x10", "--units", "imperial")
    assert "out of" in txt.lower() or "outside the travel" in txt
    line = [ln for ln in txt.splitlines() if "outside the travel" in ln][0]
    assert " in" in line and not METRIC_RE.search(line), line
    d = _json("validate", GEAR, "--travel", "10x10", "--units", "imperial")
    msg = [i["message"] for i in d["issues"] if i["code"] == "out_of_travel"][0]
    assert " mm" in msg and d["units"] == "mm"
    from mlaser.machine import preflight as PF
    assert PF.TEXT_FORMAT["range"] is PF._mm_range          # restored after the text output
