"""The CLI commands.  Each returns ``(exit code, result dict, text formatter)``; see
:mod:`mlaser.cli` for the output envelope and docs/user/automation.md for the fields."""
from __future__ import annotations

import csv
import getpass
import json
import os
from typing import Any, Callable, Optional

from . import common as C
from .common import EXIT_CHECK, EXIT_INPUT, EXIT_OK, EXIT_OUTPUT, EXIT_REFUSED, EXIT_USAGE, CliError

Result = tuple[int, dict, Callable[[dict], str]]


def _fmt_s(s: Any) -> str:
    try:
        s = float(s)
    except (TypeError, ValueError):
        return "-"
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _load(args: Any, path: str) -> tuple[Any, Optional[dict]]:
    """The document of ``path`` with ``--tech`` applied; returns (document, technology info)."""
    doc = C.load_document(path)
    info = None
    if getattr(args, "tech", None):
        store = C.open_store(getattr(args, "tech_db", None))
        try:
            tech = C.find_technology(store, args.tech)
            layers = C.apply_technology(doc, tech)
        finally:
            store.close()
        info = {"key": tech.key.label(), "id": tech.id, "version": tech.version, "example": tech.is_example,
                "verified": tech.verified, "layers": layers}
    return doc, info


def _machine(args: Any) -> Any:
    return C.config_service(getattr(args, "config", None)).machine()


# ------------------------------------------------------------------------------ compile
def cmd_compile(args: Any) -> Result:
    doc, tech = _load(args, args.file)
    from mlaser.cli.common import plan, plan_summary
    p = plan(doc, _machine(args), args.mode, _start_of(args), bool(args.dry_run))
    res: dict = {"file": args.file, "document": C.document_summary(doc), "technology": tech,
                 "layers": C.technology_ref(doc), "job": plan_summary(p)}
    if args.frames:
        res["frames_file"] = write_frames(p, args.frames)
    warnings = _layer_warnings(doc, emits=not p.dry_run)
    if warnings:
        res["warnings"] = warnings

    def text(d: dict) -> str:
        j = d["job"]
        lines = [f"{d['file']}: {j['mode']}{' (laser off)' if j['dry_run'] else ''}, "
                 f"{d['document']['parts']} parts, {j['pierces']} pierces",
                 f"  time {_fmt_s(j['time_s'])} (planner estimate {_fmt_s(j['estimate_s'])}), "
                 f"cut {j['cut_length_mm'] / 1000:.2f} m, rapid {j['rapid_length_mm'] / 1000:.2f} m",
                 f"  {j['wire_items']} wire items in {j['frames']} frames, extent {j['bounds']}"]
        if d.get("frames_file"):
            lines.append(f"  frames written to {d['frames_file']}")
        lines += [f"  WARNING: {w['message']}" for w in d.get("warnings", [])]
        return "\n".join(lines)
    return EXIT_OK, res, text


def _start_of(args: Any) -> tuple[float, float]:
    from mlaser.cli import _start
    return _start(getattr(args, "start", None))


def _layer_warnings(doc: Any, emits: bool) -> list[dict]:
    from mlaser.machine import preflight as PF
    rep = PF.check("start", document=doc, require_homed=False, armed=True, emits=emits)
    return [{"code": i.code, "severity": i.severity, "message": i.message}
            for i in rep.issues if i.code == "layer_undefined"]


def write_frames(p: Any, path: str) -> str:
    """The card frames Start streams (``PlannedJob.frames()``), one JSON array per line after a
    header line ``{"format": "mlaser-frames/1", ...}``."""
    try:
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            frames = p.frames()
            head = {"format": "mlaser-frames/1", "frames": len(frames), "mode": p.mode, "key": p.key,
                    "start_mm": list(p.start), "dry_run": bool(p.dry_run)}
            f.write(json.dumps(head) + "\n")
            for fr in frames:
                f.write(json.dumps([int(w) for w in fr], separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise CliError(f"{path}: cannot write ({exc.strerror or exc})", EXIT_OUTPUT, "write_failed") from exc
    return path


# ------------------------------------------------------------------------------ estimate
def _rates(args: Any) -> Any:
    """Cost rates: ``--rates`` JSON (Rates field names) or the Process workspace's settings
    (``plugin.ext_process.*`` in the config)."""
    from mlaser.services.costing import Rates
    if args.rates:
        try:
            data = json.load(open(args.rates, encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError(f"--rates {args.rates}: {exc}", EXIT_INPUT, "unreadable") from exc
        fields = Rates.__dataclass_fields__
        unknown = sorted(set(data) - set(fields))
        if unknown:
            raise CliError(f"--rates: unknown field(s) {', '.join(unknown)}", EXIT_INPUT, "bad_rates")
        return Rates(**data)
    cfg = C.config_service(getattr(args, "config", None))

    def g(key: str, default: Any) -> Any:
        v = cfg.get("plugin.ext_process." + key, default)
        return default if v in (None, "") else v
    return Rates(currency=str(g("currency", "€")), machine_per_h=float(g("machine_per_h", 0.0)),
                 power_per_kwh=float(g("power_per_kwh", 0.0)),
                 gas_per_m3={"O2": float(g("gas_o2_per_m3", 0.0)), "N2": float(g("gas_n2_per_m3", 0.0)),
                             "Air": float(g("gas_air_per_m3", 0.0))},
                 material_per_kg=float(g("material_per_kg", 0.0)), laser_rated_w=float(g("laser_rated_w", 0.0)),
                 wall_plug_eff=float(g("wall_plug_pct", 35.0)) / 100.0, machine_kw=float(g("machine_kw", 0.0)),
                 nozzle_cd=float(g("nozzle_cd", 0.8)), gas_flow_l_min=float(g("gas_flow_l_min", 0.0)) or None,
                 sheet_w_mm=float(g("sheet_w_mm", 0.0)), sheet_h_mm=float(g("sheet_h_mm", 0.0)))


def cmd_estimate(args: Any) -> Result:
    from mlaser.services.costing import card_for_plan
    from mlaser.services.preview import build_timeline
    doc, tech = _load(args, args.file)
    p = C.plan(doc, _machine(args), "process", _start_of(args))
    tl = build_timeline(p)
    st = tl.stats()
    rates = _rates(args)
    card = card_for_plan(p, tl, rates)
    lines = [{"key": ln.key, "value": C.r(ln.value, 4) if ln.value is not None else None, "unit": ln.unit,
              "kind": ln.kind, "note": ln.note} for ln in card.lines]
    missing = [ln.key for ln in card.missing]
    total = None if "cost.total" in missing else card.value("cost.total")
    res = {"file": args.file, "technology": tech, "layers": C.technology_ref(doc),
           "time": C.r({"total_s": st.total_s, "cut_s": st.cut_s, "travel_s": st.travel_s, "dwell_s": st.dwell_s,
                        "process_s": st.process_s, "laser_on_s": st.laser_on_s, "gas_on_s": st.gas_on_s}),
           "length_mm": C.r({"cut": st.cut_length_mm, "travel": st.travel_length_mm}),
           "pierces": int(p.job.pierces), "cost": {"currency": card.currency, "total": C.r(total, 4)
                                                   if isinstance(total, float) else total, "lines": lines,
                                                   "missing": missing}}
    warnings = _layer_warnings(doc, emits=True)
    if warnings:
        res["warnings"] = warnings

    def text(d: dict) -> str:
        t = d["time"]
        out = [f"{d['file']}: {_fmt_s(t['total_s'])} total (cut {_fmt_s(t['cut_s'])}, travel "
               f"{_fmt_s(t['travel_s'])}, pierce/process {_fmt_s(t['dwell_s'] + t['process_s'])}), "
               f"{d['pierces']} pierces, cut {d['length_mm']['cut'] / 1000:.2f} m"]
        c = d["cost"]
        for ln in c["lines"]:
            v = ("input needed" if ln["value"] is None or ln["key"] in c["missing"]
                 else f"{ln['value']:g} {ln['unit']}")
            out.append(f"  {ln['key']:<16} {v}")
        if c["missing"]:
            out.append("  (rates not set: Process > Rates and assumptions, or --rates FILE)")
        return "\n".join(out)
    return EXIT_OK, res, text


# ------------------------------------------------------------------------------ nest
def _parts_list(items: list[str]) -> list[dict]:
    """``FILE[:QTY]`` arguments and parts-list CSVs -> [{file, quantity, name, ...}]."""
    out: list[dict] = []
    for it in items:
        if it.lower().endswith(".csv") and os.path.isfile(it):
            out += _read_csv(it)
            continue
        path, qty = it, 1
        head, sep, tail = it.rpartition(":")
        if sep and tail.isdigit() and head and not os.path.exists(it):
            path, qty = head, int(tail)
        out.append({"file": path, "quantity": qty})
    if not out:
        raise CliError("no parts given", EXIT_USAGE, "usage")
    return out


def _read_csv(path: str) -> list[dict]:
    """Columns (header row, any order, case-insensitive): ``file`` (or ``path``), ``quantity``
    (or ``qty``), optional ``name``, ``priority``, ``rotation`` (allowed step in degrees),
    ``mirror`` (yes/no).  Relative files are relative to the CSV."""
    base = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(f, dialect=dialect))
    except OSError as exc:
        raise CliError(f"{path}: {exc.strerror or exc}", EXIT_INPUT, "unreadable") from exc
    out = []
    for n, row in enumerate(rows, 2):
        row = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        f_ = row.get("file") or row.get("path") or row.get("drawing")
        if not f_:
            raise CliError(f"{path} line {n}: no file", EXIT_INPUT, "bad_csv")
        try:
            qty = int(float(row.get("quantity") or row.get("qty") or 1))
            prio = int(float(row.get("priority") or 0))
            rot = float(row["rotation"]) if row.get("rotation") else None
        except ValueError as exc:
            raise CliError(f"{path} line {n}: {exc}", EXIT_INPUT, "bad_csv") from exc
        out.append({"file": f_ if os.path.isabs(f_) else os.path.join(base, f_), "quantity": max(1, qty),
                    "name": row.get("name", ""), "priority": prio, "rotation": rot,
                    "mirror": row.get("mirror", "").lower() in ("1", "y", "yes", "true", "x")})
    return out


def cmd_nest(args: Any) -> Result:
    from mlaser.nest import NestJob, NestParams, NestPart, NestSheet
    from mlaser.services.nesting import NestService, sheet_documents
    size = _size_of(args.sheet)
    assert size is not None
    if args.sheets < 1:
        raise CliError("--sheets must be at least 1", EXIT_USAGE, "usage")
    plist = _parts_list(args.parts)
    params = NestParams(spacing=args.spacing, margin=args.margin, rotation_step=args.rotation or 0.0,
                        time_limit_s=args.time_limit)
    if args.engine:
        params.engine = args.engine
    job = NestJob(params=params, name=args.name)
    tech_info = None
    layers: dict = {}
    for item in plist:
        doc, tech = _load(args, item["file"])
        tech_info = tech_info or tech
        for k, lp in doc.layers.items():
            layers.setdefault(k, lp)
        parts = [p for p in doc.parts if p.contours]
        if not parts:
            raise CliError(f"{item['file']}: no geometry", EXIT_INPUT, "empty")
        rot = item.get("rotation")
        angles = None
        if rot is not None:
            angles = [0.0] if rot <= 0 else [a * rot for a in range(int(round(360.0 / rot)))]
        job.add_part(NestPart(parts, name=item.get("name") or os.path.splitext(os.path.basename(item["file"]))[0],
                              quantity=int(item["quantity"]), priority=int(item.get("priority", 0) or 0),
                              angles=angles, mirror=bool(item.get("mirror", False))))
    job.add_sheet(NestSheet(width=size[0], height=size[1], quantity=args.sheets))
    svc = NestService(threaded=False)
    errors: list[BaseException] = []
    svc.start(job, on_failed=errors.append)
    if errors or not svc.runs:
        e = errors[0] if errors else RuntimeError("no result")
        raise CliError(f"nesting failed: {type(e).__name__}: {e}", C.EXIT_FAILED, "nest_failed")
    run = svc.runs[-1]
    names = {p.id: p.name for p in run.job.parts}
    sheets = []
    for k, doc, info in sheet_documents(run.job, run.result, layers):
        row = {"index": k + 1, "parts": info["parts"], "utilisation": C.r(info["utilisation"], 4),
               "size": info["size"]}
        if args.out:
            fname = f"{args.name}_sheet{k + 1}.{args.format}"
            row["file"] = C.save_document(doc, os.path.join(args.out, fname))
        sheets.append(row)
    res = {"engine": run.engine, "sheets": sheets, "sheets_used": run.sheets,
           "utilisation": C.r(run.utilisation, 4), "placed": run.placed, "unplaced": run.unplaced,
           "placed_by_part": {names.get(k, str(k)): v for k, v in run.result.placed.items()},
           "unplaced_by_part": {names.get(k, str(k)): v for k, v in run.result.unplaced.items() if v},
           "elapsed_s": C.r(run.elapsed_s), "notes": list(run.result.notes), "technology": tech_info,
           "material": args.material, "thickness_mm": args.thickness}
    if args.out and args.format != "mlz":
        res["note"] = (f".{args.format} sheet files carry no layer parameters: the machine's layer table "
                       "supplies them (use --format mlz to keep the technology)")
    code = EXIT_CHECK if run.unplaced else EXIT_OK
    if args.out:
        summary = os.path.join(args.out, f"{args.name}.json")
        try:
            with open(summary, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=1, ensure_ascii=False, default=str)
        except OSError as exc:
            raise CliError(f"{summary}: cannot write ({exc.strerror or exc})", EXIT_OUTPUT, "write_failed") from exc
        res["summary_file"] = summary

    def text(d: dict) -> str:
        out = [f"nest: {d['placed']} placed, {d['unplaced']} unplaced on {d['sheets_used']} sheet(s), "
               f"utilisation {100 * (d['utilisation'] or 0):.1f} % ({d['engine']}, {d['elapsed_s']} s)"]
        for s in d["sheets"]:
            out.append(f"  sheet {s['index']}: {s['parts']} parts, {100 * (s['utilisation'] or 0):.1f} %"
                       + (f" -> {s['file']}" if s.get("file") else ""))
        for name, n in d["unplaced_by_part"].items():
            out.append(f"  UNPLACED: {name} x {n}")
        return "\n".join(out)
    return code, res, text


def _size_of(text: Optional[str], what: str = "--sheet") -> Optional[tuple[float, float]]:
    from mlaser.cli import _size
    return _size(text, what)


# ------------------------------------------------------------------------------ validate
def cmd_validate(args: Any) -> Result:
    from mlaser.machine import preflight as PF
    doc, tech = _load(args, args.file)
    machine = _machine(args)
    issues: list[dict] = []
    p = None
    try:
        p = C.plan(doc, machine, "process", _start_of(args), bool(args.dry_run))
    except CliError as exc:
        issues.append({"code": "compile_failed", "severity": "error", "message": str(exc)})
    travel = None
    tsize = _size_of(args.travel, "--travel")
    if tsize is not None:
        travel = PF.TravelLimits((0.0, tsize[0]), (0.0, tsize[1]), "--travel")
    else:
        travel = PF.travel_from_machine(machine)
    emits = not args.dry_run
    rep = PF.check("start", document=doc, job=p.job if p is not None else None, travel=travel,
                   require_homed=False, armed=True, emits=emits)
    issues += [{"code": i.code, "severity": i.severity, "message": i.message,
                "where": list(i.where) if i.where is not None else None} for i in rep.issues]
    sheet = _size_of(args.sheet)
    if sheet is not None and p is not None:
        x0, y0, x1, y1 = p.cut_bounds
        if x0 < -1e-6 or y0 < -1e-6 or x1 > sheet[0] + 1e-6 or y1 > sheet[1] + 1e-6:
            issues.append({"code": "exceeds_sheet", "severity": "error",
                           "message": f"the cut ({x0:.1f}, {y0:.1f}) .. ({x1:.1f}, {y1:.1f}) mm does not fit on "
                                      f"a {sheet[0]:g} x {sheet[1]:g} mm sheet at the origin"})
    errors = [i for i in issues if i["severity"] == "error"]
    res = {"file": args.file, "valid": not errors, "errors": len(errors),
           "warnings": len(issues) - len(errors), "issues": issues, "technology": tech,
           "layers": C.technology_ref(doc), "document": C.document_summary(doc),
           "travel": travel.describe() if travel is not None else None,
           "extent": C.r(list(rep.extent)) if rep.extent else None,
           "not_checked": ["homing", "arming", "card alarms", "interlocks", "head position (start gate)"]}

    def text(d: dict) -> str:
        head = f"{d['file']}: {'OK' if d['valid'] else 'NOT VALID'} ({d['errors']} error(s), {d['warnings']} warning(s))"
        return "\n".join([head] + [f"  {i['severity'].upper()}: {i['message']}" for i in d["issues"]])
    return (EXIT_OK if not errors else EXIT_CHECK), res, text


# ------------------------------------------------------------------------------ convert
def cmd_convert(args: Any) -> Result:
    if os.path.abspath(args.input) == os.path.abspath(args.output):
        raise CliError("the output is the input file", EXIT_USAGE, "usage")
    doc, tech = _load(args, args.input)
    C.save_document(doc, args.output)
    res = {"input": args.input, "output": args.output, "document": C.document_summary(doc), "technology": tech}
    ext = os.path.splitext(args.output)[1].lower()
    if ext in (".dxf", ".svg"):
        res["note"] = "geometry only: leads, kerf, joints and layer parameters are not written to " + ext

    def text(d: dict) -> str:
        return f"{d['input']} -> {d['output']} ({d['document']['parts']} parts)" + \
            (f"\n  note: {d['note']}" if d.get("note") else "")
    return EXIT_OK, res, text


# ------------------------------------------------------------------------------ queue
def _production_dir(args: Any) -> str:
    if getattr(args, "data_dir", None):
        root = args.data_dir
    else:
        from mlaser.services.config import data_dir
        root = str(data_dir())
    return os.path.join(root, "production")


def cmd_queue_add(args: Any) -> Result:
    """Submit for the operator's approval (never queued directly, never started)."""
    from mlaser.services.intake import IntakeError, store_upload, submit_offline
    if not os.path.isfile(args.file):
        raise CliError(f"{args.file}: file not found", EXIT_INPUT, "not_found")
    prod = _production_dir(args)
    path = os.path.abspath(args.file)
    converted = None
    if path.lower().endswith(".mlz"):
        # the queue runs .chf / .dxf / .svg: the project's drawing goes in as a .chf
        from mlaser.project import export_chf
        doc = C.load_document(path)
        try:
            converted = store_upload(prod, os.path.splitext(os.path.basename(path))[0] + ".chf", export_chf(doc))
        except (IntakeError, OSError) as exc:
            raise CliError(f"cannot convert {args.file}: {exc}", EXIT_OUTPUT, "write_failed") from exc
        path = converted
    name = args.name or os.path.splitext(os.path.basename(args.file))[0]
    req = {"path": path, "name": name, "quantity": args.quantity, "material": args.material,
           "thickness_mm": args.thickness, "sheet": args.sheet, "note": args.note, "reference": args.reference}
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (service account)
        user = ""
    try:
        job = submit_offline(prod, req, source="cli", client=user)
    except IntakeError as exc:
        if converted:
            try:
                os.unlink(converted)
            except OSError:
                pass
        raise CliError(f"queue add refused: {exc}", EXIT_USAGE if exc.code in ("invalid", "unknown_field")
                       else EXIT_REFUSED, exc.code) from exc
    except OSError as exc:
        raise CliError(f"cannot write the submission ({exc.strerror or exc})", EXIT_OUTPUT, "write_failed") from exc
    res = {"id": job.id, "status": "pending_approval", "file": job.path, "name": job.name,
           "quantity": job.quantity, "inbox": os.path.join(prod, "inbox"),
           "next": "the operator approves it at the machine (Run > Queue > Approve); it never starts by itself"}

    def text(d: dict) -> str:
        return (f"submitted {d['name']} x {d['quantity']} (id {d['id']}): waiting for the operator's approval at "
                "the machine (Run > Queue)")
    return EXIT_OK, res, text


def cmd_queue_list(args: Any) -> Result:
    prod = _production_dir(args)
    entries, pending = [], []
    try:
        data = json.load(open(os.path.join(prod, "queue.json"), encoding="utf-8"))
        entries = [{k: e.get(k) for k in ("id", "name", "quantity", "done", "status", "material", "thickness_mm",
                                          "sheet", "est_s")} for e in data.get("entries", []) if isinstance(e, dict)]
    except (OSError, ValueError, AttributeError):
        pass
    from mlaser.services.intake import PendingJob
    try:
        pd = json.load(open(os.path.join(prod, "pending.json"), encoding="utf-8"))
        pending = [PendingJob.from_json(d).public() for d in pd.get("pending", []) if isinstance(d, dict)]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        inbox = sum(1 for n in os.listdir(os.path.join(prod, "inbox")) if n.endswith(".json"))
    except OSError:
        inbox = 0
    res = {"queue": entries, "pending_approval": pending, "inbox": inbox}

    def text(d: dict) -> str:
        out = [f"queue: {len(d['queue'])} job(s); waiting for approval: {len(d['pending_approval'])}"
               + (f" (+{d['inbox']} not picked up yet)" if d["inbox"] else "")]
        out += [f"  {e['status']:<12} {e['name']}  {e['done']}/{e['quantity']}" for e in d["queue"]]
        out += [f"  {'approval':<12} {p['name']}  x{p['quantity']}  ({p['source']} {p['client']})"
                for p in d["pending_approval"]]
        return "\n".join(out)
    return EXIT_OK, res, text


__all__ = ["cmd_compile", "cmd_estimate", "cmd_nest", "cmd_validate", "cmd_convert", "cmd_queue_add",
           "cmd_queue_list", "write_frames"]
