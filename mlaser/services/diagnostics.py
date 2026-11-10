"""Collects the inputs of the diagnostic bundle (:mod:`mlaser.diag.bundle`) from the services.

Two steps, so the GUI never waits on a disk:

* :func:`snapshot` on the caller's thread (the GUI thread in the window): copies of what other
  code may change while the bundle is written (the configuration values through
  ``ConfigService``'s public API, the machine parameters, the plan summary).  Cheap: dict copies
  and the parameter view (about 2 ms).
* :func:`mlaser.diag.bundle.write_bundle` in a worker with the result: reads the log files and
  the alarm history from disk and the controller's cached state, compresses, writes.

Never sends a card command, never opens a network connection (docs/user/faq.md, "How do I report a problem?").
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from mlaser.diag import bundle as B


def config_snapshot(cfg: Any) -> tuple[Optional[dict], str]:
    """(``{"profile", "values", "file"}``, load error) of a ConfigService; (None, "") without one.
    Uses the public attributes only (``values``, ``profile``, ``path``, ``load_error``)."""
    if cfg is None:
        return None, ""
    prof = getattr(cfg, "profile", None)
    data = {"profile": {"base": getattr(prof, "base", None), "overrides": dict(getattr(prof, "overrides", {}) or {})},
            "values": dict(getattr(cfg, "values", {}) or {}),
            "file": B.redact_path(str(getattr(cfg, "path", "") or ""))}
    err = getattr(cfg, "load_error", "") or ""
    return data, str(err)


def machine_param_values(cfg: Any) -> tuple[Optional[dict], Optional[dict]]:
    """(values, registry defaults) of the machine parameter files, ``{"hard": .., "manu": ..}``."""
    if cfg is None or not hasattr(cfg, "machine_params"):
        return None, None
    from mlaser.formats.params import MachineParams
    mp = cfg.machine_params()
    d = MachineParams.defaults()
    return ({"HardPara.xml": dict(mp.hard.values), "ManuPara.xml": dict(mp.manu.values)},
            {"HardPara.xml": dict(d.hard.values), "ManuPara.xml": dict(d.manu.values)})


def alarm_records(history: Any = None) -> list[dict]:
    """The alarm history: ``history`` (an AlarmHistory), else the one on disk."""
    from dataclasses import asdict, is_dataclass

    from mlaser.services.alarm_history import AlarmHistory, default_history, fmt_time
    if history is None:
        history = default_history()
        history.load()
    out = []
    recs: list[Any] = (history.entries(newest_first=True) if isinstance(history, AlarmHistory)
                       else list(history or []))
    for r in recs:
        d: dict = asdict(r) if is_dataclass(r) and not isinstance(r, type) else dict(r)
        d["raised_at"] = fmt_time(d.get("raised"))
        d["cleared_at"] = fmt_time(d.get("cleared"))
        out.append(d)
    return out


def log_dirs() -> list[str]:
    from mlaser.machine.logsetup import log_dir
    return [str(log_dir())]


def ui_log_dirs(data_dir: Optional[str] = None) -> list[str]:
    from mlaser.services.config import data_dir as _dd
    return [os.path.join(data_dir or str(_dd()), "Log")]


def snapshot(cfg: Any = None, controller: Any = None, document: Any = None, *, screens: Optional[list] = None,
             drawing: Optional[bytes] = None, drawing_name: str = "", data_dir: Optional[str] = None,
             alarm_history: Any = None, extra: Optional[dict] = None) -> B.Sources:
    """The bundle's :class:`~mlaser.diag.bundle.Sources`.  The alarm history (``alarm_history``:
    an AlarmHistory or records; None: read from disk) and the logs are read by
    :func:`complete` in the worker."""
    conf, err = config_snapshot(cfg)
    params, defaults = machine_param_values(cfg)
    host = str(getattr(getattr(controller, "cfg", None), "host", "") or "")
    plan = {"document": B.document_summary(document),
            "job": B.job_summary(getattr(controller, "job", None)) if controller is not None else None}
    src = B.Sources(log_dirs=log_dirs(), ui_log_dirs=ui_log_dirs(data_dir), config=conf, config_error=err,
                    machine_params=params, machine_param_defaults=defaults, card_host=host, controller=controller,
                    plan=plan, drawing=drawing, drawing_name=drawing_name or "drawing.mlz",
                    screens=list(screens or []), extra=dict(extra or {}), alarm_source=alarm_history)
    return src


def complete(src: B.Sources) -> B.Sources:
    """The parts read from disk (worker thread): the alarm history."""
    src.alarm_history = alarm_records(src.alarm_source)
    return src


def write(path: str | os.PathLike, src: B.Sources, include_drawing: bool = False) -> dict:
    """:func:`complete` + :func:`mlaser.diag.bundle.write_bundle` (worker thread)."""
    return B.write_bundle(Path(path), complete(src), include_drawing)


__all__ = ["snapshot", "complete", "write", "config_snapshot", "machine_param_values", "alarm_records", "log_dirs",
           "ui_log_dirs"]
