"""The command-line tools (mlaser.cli): JSON output, exit codes, the same services as the GUI,
no card connection, and ``queue add`` that only submits for the operator's approval."""
from __future__ import annotations

import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest

from mlaser import cli
from mlaser.cli import common as C

DATA = os.path.join(os.path.dirname(__file__), "data")
GEAR = os.path.join(DATA, "dxf", "gear.dxf")
INSERTS = os.path.join(DATA, "dxf", "inserts2.dxf")
TECH = "S235/3/O2"


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("MLASER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MLASER_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture(autouse=True)
def no_card(monkeypatch):
    """The CLI never talks to a card: any socket connect or datagram send fails the test."""
    def refuse(*a, **k):
        raise AssertionError("the CLI opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "sendto", refuse)


def run(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    code = cli.main(list(argv) + ["--json"], out=out, err=io.StringIO())
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1, out.getvalue()
    res = json.loads(lines[0])
    assert res["exit_code"] == code and res["ok"] == (code == 0)
    return code, res


def test_compile_is_the_gui_plan_and_writes_the_frames(dirs):
    frames = str(dirs / "out" / "gear.frames.jsonl")
    code, res = run("compile", GEAR, "--tech", TECH, "--frames", frames, "--start", "5,5")
    assert code == 0 and res["command"] == "compile"
    assert res["technology"]["key"].startswith("Mild steel S235 3 mm") and res["technology"]["example"] is True
    assert res["layers"]["0"]["key"]["material"] == "Mild steel S235"
    j = res["job"]
    assert j["mode"] == "process" and not j["dry_run"] and j["pierces"] == 2 and j["frames"] > 0
    assert j["start"] == [5.0, 5.0] and j["time_s"] > 0 and "warnings" not in res
    # the same PlannedJob the GUI's JobService makes for this document (frames identical)
    from mlaser.services.jobs import InlineExecutor, JobService
    doc = C.load_document(GEAR)
    store = C.open_store()
    C.apply_technology(doc, C.find_technology(store, TECH))
    store.close()
    machine = C.config_service().machine()
    plan = JobService(executor=InlineExecutor()).plan_sync(C.make_request(doc, machine, start=(5.0, 5.0)))
    with open(frames, encoding="utf-8") as f:
        head = json.loads(f.readline())
        body = [json.loads(ln) for ln in f]
    assert head["format"] == "mlaser-frames/1" and head["frames"] == len(body) == j["frames"]
    assert head["key"] == j["key"]
    assert body == plan.frames()


def test_compile_without_technology_warns_about_the_undefined_layer():
    code, res = run("compile", GEAR)
    assert code == 0
    assert [w["code"] for w in res["warnings"]] == ["layer_undefined"]


def test_compile_dry_run_and_frame():
    code, res = run("compile", GEAR, "--tech", TECH, "--dry-run")
    assert code == 0 and res["job"]["dry_run"] is True and "warnings" not in res
    code, res = run("compile", GEAR, "--mode", "frame")
    assert code == 0 and res["job"]["mode"] == "frame" and res["job"]["pierces"] == 0


def test_estimate_time_and_cost(dirs):
    rates = dirs / "rates.json"
    rates.write_text(json.dumps({"currency": "EUR", "machine_per_h": 60.0, "power_per_kwh": 0.3,
                                 "gas_per_m3": {"O2": 1.0, "N2": 1.0, "Air": 0.1}, "material_per_kg": 1.5,
                                 "machine_kw": 5.0}))
    code, res = run("estimate", GEAR, "--tech", TECH, "--rates", str(rates))
    assert code == 0
    t = res["time"]
    assert t["total_s"] == pytest.approx(t["cut_s"] + t["travel_s"] + t["dwell_s"] + t["process_s"], abs=0.01)
    assert res["pierces"] == 2 and res["length_mm"]["cut"] > 0
    c = res["cost"]
    assert c["currency"] == "EUR" and c["missing"] == [] and c["total"] > 0
    keys = {ln["key"] for ln in c["lines"]}
    assert {"time.total", "cost.machine", "cost.total"} <= keys
    # without rates the cost lines say "input needed", the total is not a number
    code, res = run("estimate", GEAR, "--tech", TECH)
    assert code == 0 and res["cost"]["total"] is None and "cost.total" in res["cost"]["missing"]


def test_estimate_rates_from_the_config(dirs):
    from mlaser.services.config import ConfigService
    cfg = ConfigService()
    cfg.set("plugin.ext_process.machine_per_h", 90.0)
    cfg.set("plugin.ext_process.currency", "CHF")
    cfg.save()
    code, res = run("estimate", GEAR, "--tech", TECH)
    assert code == 0 and res["cost"]["currency"] == "CHF"
    line = next(ln for ln in res["cost"]["lines"] if ln["key"] == "cost.machine")
    assert line["value"] == pytest.approx(90.0 * res["time"]["total_s"] / 3600.0, rel=1e-3)


def _drawing_next_to(d) -> str:
    """A copy of the gear drawing under ``d``; its path relative to ``d`` (a CSV in ``d`` names
    it so).  Not ``relpath(GEAR, tmp)``: on Windows the checkout and the temp folder can be on
    different drives (CI: D: and C:), where no relative path exists."""
    sub = d / "drawings"
    sub.mkdir(exist_ok=True)
    shutil.copy(GEAR, sub / "gear.dxf")
    return os.path.join("drawings", "gear.dxf")


def test_nest_parts_list_and_csv(dirs):
    out = dirs / "nest"
    code, res = run("nest", f"{GEAR}:6", f"{INSERTS}:10", "--sheet", "1000x500", "--tech", TECH,
                    "--out", str(out), "--time-limit", "5")
    assert code == 0 and res["unplaced"] == 0 and res["placed"] == 16
    assert res["placed_by_part"] == {"gear": 6, "inserts2": 10} and res["sheets_used"] == 1
    sheet = res["sheets"][0]
    assert os.path.isfile(sheet["file"]) and sheet["file"].endswith(".mlz") and 0 < sheet["utilisation"] < 1
    summary = json.loads(open(res["summary_file"], encoding="utf-8").read())
    assert summary["placed"] == 16
    # the sheet file is a cuttable job: it compiles with the layer parameters of the technology
    code, comp = run("compile", sheet["file"])
    assert code == 0 and "warnings" not in comp and comp["job"]["pierces"] >= 16
    # a CSV parts list (relative paths, ; separated), more than fits: exit 1 with the unplaced parts
    csvp = dirs / "parts.csv"
    rel = _drawing_next_to(dirs)
    csvp.write_text(f"file;quantity;name;rotation\n{rel};40;big gear;90\n", encoding="utf-8")
    code, res = run("nest", str(csvp), "--sheet", "300x200", "--time-limit", "5")
    assert code == C.EXIT_CHECK
    assert res["unplaced"] > 0 and res["unplaced_by_part"]["big gear"] == res["unplaced"]


def test_nest_csv_is_the_nest_workspace_format(dirs):
    """The CLI reads parts lists with the Nest workspace's parser (services.orders): Windows-1252,
    ``;``, a decimal comma and loose header names; a refused row is exit 3 with its line."""
    from mlaser.cli.commands import _read_csv
    rel = _drawing_next_to(dirs)
    p = dirs / "w.csv"
    p.write_bytes(f"Datei;Menge;Material;Stärke;Kunde;rotation\n{rel};3;Edelstahl;1,5;Müller;90\n".encode("cp1252"))
    (row,) = _read_csv(str(p))
    assert row["quantity"] == 3 and row["thickness_mm"] == 1.5 and row["rotation"] == 90.0
    assert row["order"]["customer"] == "Müller" and os.path.samefile(row["file"], dirs / rel)
    bad = dirs / "bad.csv"
    bad.write_text(f"file,quantity\n{rel},2\n{rel},zwei\n", encoding="utf-8")
    code, res = run("nest", str(bad), "--sheet", "300x200")
    assert code == C.EXIT_INPUT and "line 3" in json.dumps(res)


@pytest.mark.parametrize("fmt", ["chf", "dxf", "svg"])
def test_nest_sheet_formats(dirs, fmt):
    code, res = run("nest", f"{GEAR}:2", "--sheet", "500x500", "--out", str(dirs / fmt), "--format", fmt,
                    "--time-limit", "5", "--tech", TECH)
    assert code == 0 and res["sheets"][0]["file"].endswith("." + fmt) and "layer table" in res["note"]
    assert C.load_document(res["sheets"][0]["file"]).parts


def test_validate_reports_like_the_preflight(dirs):
    code, res = run("validate", GEAR)
    assert code == 1 and not res["valid"]
    assert {i["code"] for i in res["issues"]} >= {"layer_undefined"}
    assert "interlocks" in res["not_checked"]
    # gear.dxf's lead-in reaches below Y 0 (out of the travel); inserts2 fits, with warnings
    code, res = run("validate", GEAR, "--tech", TECH, "--travel", "3000x1500")
    assert code == 1 and "out_of_travel" in {i["code"] for i in res["issues"]}
    code, res = run("validate", INSERTS, "--tech", TECH, "--start", "50,50", "--travel", "3000x1500")
    assert code == 0 and res["valid"] and res["errors"] == 0 and res["warnings"] == 1   # one collapsed open-contour line
    assert {i["code"] for i in res["issues"]} == {"open_contour"}
    code, res = run("validate", INSERTS, "--tech", TECH, "--travel", "3000x1500", "--sheet", "50x50")
    assert code == 1 and [i["code"] for i in res["issues"] if i["severity"] == "error"] == ["exceeds_sheet"]
    code, res = run("validate", INSERTS, "--tech", TECH, "--travel", "50x50")
    assert code == 1 and "out_of_travel" in {i["code"] for i in res["issues"]}


def test_convert_round_trips(dirs):
    ref = C.load_document(GEAR)
    n = len(ref.parts)
    paths = [dirs / "g.svg", dirs / "g.dxf", dirs / "g.mlz", dirs / "g.chf"]
    src = GEAR
    for p in paths:
        code, res = run("convert", src, str(p))
        assert code == 0 and res["document"]["parts"] == n, p
        src = str(p)
    back = C.load_document(str(paths[-1]))
    assert len(back.parts) == n
    a, b = ref.bbox(), back.bbox()
    assert a == pytest.approx(b, abs=0.05)
    length = sum(c.length() for p in ref.parts for c in p.contours)
    assert sum(c.length() for p in back.parts for c in p.contours) == pytest.approx(length, rel=2e-3)
    # the CAM settings survive .mlz / .chf
    code, res = run("convert", GEAR, str(dirs / "t.mlz"), "--tech", TECH)
    assert code == 0 and res["technology"]
    code, res = run("compile", str(dirs / "t.mlz"))
    assert code == 0 and "warnings" not in res


def test_exit_codes_and_errors(dirs):
    code, res = run("compile", str(dirs / "missing.dxf"))
    assert code == C.EXIT_INPUT and res["error"]["code"] == "not_found"
    bad = dirs / "x.txt"
    bad.write_text("hello")
    code, res = run("compile", str(bad))
    assert code == C.EXIT_INPUT and res["error"]["code"] == "unsupported"
    junk = dirs / "junk.dxf"
    junk.write_text("this is not a dxf")
    code, res = run("compile", str(junk))
    assert code == C.EXIT_INPUT and res["error"]["code"] == "unreadable"
    code, res = run("compile", GEAR, "--tech", "Unobtainium/3")
    assert code == C.EXIT_FAILED and res["error"]["code"] == "unknown_tech"
    code, res = run("compile", GEAR, "--tech", "steel/3")         # S235 and 1.4301 both match
    assert code == C.EXIT_FAILED and res["error"]["code"] == "ambiguous_tech" and len(res["error"]["candidates"]) == 2
    code, res = run("compile", GEAR, "--tech", "S235")
    assert code == C.EXIT_USAGE
    code, res = run("nest", GEAR, "--sheet", "axb")
    assert code == C.EXIT_USAGE
    code, res = run("frobnicate")
    assert code == C.EXIT_USAGE and res["error"]["code"] == "usage"
    code, res = run("convert", GEAR, str(dirs / "out.pdf"))
    assert code == C.EXIT_USAGE and res["error"]["code"] == "unsupported_output"
    blocked = dirs / "blocked"
    blocked.write_text("a file where a directory is needed")
    code, res = run("convert", GEAR, str(blocked / "x.svg"))
    assert code == C.EXIT_OUTPUT
    # text mode: the message on stderr, nothing on stdout
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["compile", str(dirs / "missing.dxf")], out=out, err=err) == C.EXIT_INPUT
    assert out.getvalue() == "" and "file not found" in err.getvalue()


def test_queue_add_only_submits_for_approval(dirs):
    data = dirs / "data"
    code, res = run("queue", "add", GEAR, "-n", "3", "--material", "S235", "--thickness", "3", "--note", "rush",
                    "--reference", "PO-17")
    assert code == 0 and res["status"] == "pending_approval" and res["quantity"] == 3
    prod = data / "production"
    assert not (prod / "queue.json").exists()                     # never queued directly
    inbox = list((prod / "inbox").glob("*.json"))
    assert len(inbox) == 1
    # the application picks it up as pending (it is still not in the queue)
    from mlaser.services.intake import IntakeStore
    from mlaser.services.queue import JobQueue
    store = IntakeStore(prod)
    [job] = store.scan()
    assert job.id == res["id"] and job.source == "cli" and job.note == "rush" and job.reference == "PO-17"
    q = JobQueue(prod / "queue.json")
    assert len(q) == 0 and q.next_entry() is None
    code, lst = run("queue", "list")
    assert code == 0 and lst["queue"] == [] and [p["id"] for p in lst["pending_approval"]] == [job.id]
    assert "path" not in lst["pending_approval"][0]
    # a project is converted to a .chf the queue can run
    from mlaser.project import save_project
    save_project(dirs / "p.mlz", C.load_document(GEAR))
    code, res = run("queue", "add", str(dirs / "p.mlz"))
    assert code == 0 and res["file"].endswith("_p.chf") and res["name"] == "p"
    # refusals
    code, res = run("queue", "add", GEAR, "-n", "0")
    assert code == C.EXIT_USAGE
    code, res = run("queue", "add", str(dirs / "nope.dxf"))
    assert code == C.EXIT_INPUT


def test_queue_add_refused_when_the_approval_list_is_full(dirs, monkeypatch):
    from mlaser.services import intake
    monkeypatch.setattr(intake, "MAX_PENDING", 2)
    assert run("queue", "add", GEAR)[0] == 0
    assert run("queue", "add", GEAR)[0] == 0
    code, res = run("queue", "add", GEAR)
    assert code == C.EXIT_REFUSED and res["error"]["code"] == "full"


def test_text_output_is_short(dirs):
    out = io.StringIO()
    assert cli.main(["compile", GEAR, "--tech", TECH], out=out) == 0
    text = out.getvalue()
    assert text.startswith(GEAR) and "pierces" in text and not text.lstrip().startswith("{")


def test_entry_points_dispatch_without_gui_or_controller(dirs):
    """``python -m mlaser compile ...`` runs the CLI: no Qt, no controller module imported."""
    code = ("import sys, runpy; sys.argv = ['mlaser', 'compile', %r, '--json']\n"
            "try:\n    runpy.run_module('mlaser', run_name='__main__')\nexcept SystemExit as e:\n    rc = e.code\n"
            "bad = [m for m in sys.modules if m.startswith(('PySide6', 'mlaser.controller', 'mlaser.devices'))]\n"
            "print('IMPORTED', bad, rc)\n") % GEAR
    env = dict(os.environ, MLASER_DATA_DIR=str(dirs / "data"), MLASER_CONFIG_DIR=str(dirs / "config"),
               MLASER_NO_FIRSTRUN="1")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300, env=env)
    lines = p.stdout.strip().splitlines()
    assert json.loads(lines[0])["ok"] is True, p.stderr
    assert lines[-1] == "IMPORTED [] 0", p.stderr
    # the GUI launcher (the installed ``mlaser`` script) hands the words over too
    from mlaser.ui import __main__ as ui_main
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        assert ui_main.main(["convert", GEAR, str(dirs / "g.svg")]) == 0
    finally:
        sys.stdout = old
    assert os.path.isfile(dirs / "g.svg")
    assert cli.is_subcommand(["compile", "x"]) and not cli.is_subcommand(["drawing.dxf"])
    assert not cli.is_subcommand([])


def test_the_document_is_not_changed_by_planning():
    doc = C.load_document(GEAR)
    before = copy.deepcopy(doc)
    C.plan(doc, C.config_service().machine())
    assert doc == before
