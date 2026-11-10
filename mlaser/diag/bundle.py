"""The diagnostic bundle: one local zip with what a field bug report needs (review A8 / F11).

Help › Save diagnostic bundle…, the log viewer, the fault and crash-recovery dialogs and
``mlaser-cli diag-bundle OUT.zip`` all end here.  The caller collects :class:`Sources` (the
configuration through ``ConfigService``'s public API, the controller object, the alarm history,
a plan summary; :mod:`mlaser.services.diagnostics`), :func:`contents` lists what will be written
(the operator sees that list before saving) and :func:`write_bundle` writes the zip.

Rules:

* **Nothing is ever uploaded.**  The bundle is a file the operator chooses to send.  No network.
* **No card commands.**  Only state the program already holds is read (the controller's cached
  fields, the safety FSM's transitions, the supervisor's halt records, the frame rings of
  :meth:`mlaser.controller.supervisor.SafetySupervisor.recent_frames`); nothing here calls a
  method that sends a request.
* **Off the GUI thread.**  :func:`write_bundle` reads log files and compresses; callers run it
  in a worker (the GUI: ``BackgroundCall``).
* **Redaction** (:func:`redact_config`, :data:`SECRET_KEY_WORDS`, :data:`PERSONAL_KEYS`): secrets
  (tokens, passwords, notification credentials) and personal data (e-mail addresses, user
  names, the ntfy topic, webhook URLs) are replaced by ``<redacted>``; a path under a home
  directory becomes ``~/…/<file name>`` (in the logs the home directory becomes ``~``); a network
  address other than the card's is kept but marked ``[network host]`` with its path and query
  dropped.  The manifest lists every key that was changed and why.
* **Out of scope**: registers 59500 / 59503 and everything of the vendor's licence / activation
  path.  :data:`EXCLUDED_REGISTERS` drops any frame addressed to them from the frame list; the
  card identity is the firmware version and config word read at connect, nothing else.
* **The drawing** is included only when the operator ticks it (``include_drawing``); otherwise
  the plan summary carries counts and extents, no geometry.
* **Bounded**: logs newest first up to :data:`LOG_BUDGET` bytes in all, a file larger than
  :data:`LOG_FILE_CAP` contributes its newest part.
"""
from __future__ import annotations

import hashlib
import json
import locale
import os
import platform
import re
import sys
import time
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from mlaser._buildid import build_identity

#: OUT OF SCOPE for this project: the vendor's hardware challenge /
#: licence registers.  Never read for the bundle, and any frame addressed to them that sits in a
#: frame ring is dropped, not exported.  Nothing of the licence / activation path is collected.
EXCLUDED_REGISTERS = frozenset({59500, 59503})

#: a configuration key whose last part contains one of these words holds a secret: replaced
REDACTED = "<redacted>"
SECRET_KEY_WORDS = ("token", "password", "passwd", "passphrase", "secret", "credential", "apikey", "api_key",
                    "auth", "cookie", "private_key", "signing")
#: personal data and credentials of the notification channels (mlaser/ui/ext_notify.py): the
#: ntfy topic is the channel's only access control, a webhook URL usually embeds its key
PERSONAL_KEYS = frozenset({"smtp_user", "email_from", "email_to", "ntfy_topic", "webhook_url", "user", "username",
                           "user_name", "email", "e_mail", "mail", "phone", "operator_name", "full_name"})
#: a configuration key whose last part ends with one of these names a network host
HOST_KEY_SUFFIXES = ("host", "server", "hostname", "address", "ip")
NETWORK_MARK = "[network host] "

LOG_BUDGET = 24_000_000        # bytes of log text in one bundle
LOG_FILE_CAP = 6_000_000       # bytes taken from one file (its newest part)
EVENT_TEXT_CAP = 400           # characters of one event's text
FRAME_HEX_CAP = 512            # hex characters of one frame

_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
#: a home directory of any user and platform (a value copied from another machine)
_HOME_RE = re.compile(r"(?:/home/|/Users/|/var/home/|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/])[^\\/\s\"']+",
                      re.IGNORECASE)


# =========================================================================== sources
@dataclass
class Sources:
    """What a bundle is made of; collected by the caller (``mlaser.services.diagnostics``) from
    state it already holds.  Every field may be empty."""
    log_dirs: list[str] = field(default_factory=list)      # mlaser.log / audit.log / crash.log folders
    ui_log_dirs: list[str] = field(default_factory=list)   # <data>/Log (the operator's log viewer)
    config: Optional[dict] = None                          # {"profile": ..., "values": ...} (unredacted)
    config_error: str = ""                                 # the config service's load error, if any
    machine_params: Optional[dict] = None                  # {"hard": {key: text}, "manu": {...}}
    machine_param_defaults: Optional[dict] = None          # same shape: the registry defaults
    card_host: str = ""
    controller: Any = None                                 # a UiController (cached state read only)
    alarm_history: list = field(default_factory=list)      # dicts (services.alarm_history.AlarmRecord)
    alarm_source: Any = None                               # read into alarm_history in the worker
    plan: Optional[dict] = None                            # plan / job summary (no geometry)
    drawing: Optional[bytes] = None                        # the document (.mlz), only when ticked
    drawing_name: str = "drawing.mlz"
    screens: list = field(default_factory=list)            # [{name, geometry, dpr, ...}] (GUI)
    extra: dict = field(default_factory=dict)              # more sections {name: JSON-able}


# =========================================================================== system
#: distributions whose versions go into the bundle (required, optional, build)
DEPENDENCIES = ("PySide6", "shiboken6", "PySide6-Essentials", "numpy", "shapely", "ezdxf", "fontTools", "Pillow",
                "svgelements", "pyclipper", "uharfbuzz", "segno", "platformdirs", "keyring", "hidapi", "pyserial",
                "ortools", "spyrrow", "ruckig", "matplotlib")


def dependency_versions(names: Iterable[str] = DEPENDENCIES) -> dict:
    from importlib import metadata
    out: dict = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            out[n] = None
        except Exception as e:  # noqa: BLE001 - a broken metadata folder must not stop the bundle
            out[n] = f"error: {e}"
    return out


def _qt_info() -> dict:
    """Qt as loaded in this process.  The bundle module imports no Qt itself (the CLI and the
    services use it, and they import no Qt: the import contract): a process that has not loaded
    Qt (the CLI) reports the installed PySide6 version from its metadata only."""
    from importlib import metadata
    try:
        out: dict = {"pyside6_installed": metadata.version("PySide6")}
    except metadata.PackageNotFoundError:
        out = {"pyside6_installed": None}
    core = sys.modules.get("PySide6.QtCore")
    if core is not None:
        try:
            out["qt_runtime"] = core.qVersion()
            out["qt_compiled"] = getattr(core, "__version__", "")
        except Exception as e:  # noqa: BLE001 - informative only
            out["error"] = f"{type(e).__name__}: {e}"
    gui = sys.modules.get("PySide6.QtGui")
    app = getattr(gui, "QGuiApplication", None) if gui is not None else None
    try:
        if app is not None and app.instance() is not None:
            out["platform_plugin"] = app.platformName()
    except Exception:  # noqa: BLE001
        pass
    return out


def _kernel() -> dict:
    try:
        from mlaser.parity_ports import kernel_status
        return kernel_status().to_dict()
    except Exception as e:  # noqa: BLE001 - informative only
        return {"active": "error", "reason": f"{type(e).__name__}: {e}"}


def system_info(screens: Optional[list] = None) -> dict:
    """Version block: build identity, Python, Qt / PySide6, OS, CPU, locale, screens, kernel,
    dependency versions.  Reads no personal data (no user or host name)."""
    try:
        loc = list(locale.getlocale())
    except (ValueError, TypeError):
        loc = []
    return {
        "app": build_identity().as_dict(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "build": list(platform.python_build()), "compiler": platform.python_compiler(),
                   "executable": redact_path(sys.executable)},
        "qt": _qt_info(),
        "os": {"platform": platform.platform(), "system": platform.system(), "release": platform.release(),
               "version": platform.version()},
        "cpu": {"machine": platform.machine(), "processor": platform.processor(), "count": os.cpu_count(),
                "byteorder": sys.byteorder},
        "locale": {"locale": loc, "encoding": locale.getpreferredencoding(False),
                   "filesystem_encoding": sys.getfilesystemencoding()},
        "screens": list(screens or []),
        "kernel": _kernel(),
        "dependencies": dependency_versions(),
    }


# =========================================================================== redaction
def _home_dirs() -> list[str]:
    homes = []
    h = os.path.expanduser("~")
    if h and h not in ("~", "/", "\\"):
        homes.append(h)
        if os.sep == "\\":
            homes.append(h.replace("\\", "/"))
    return homes


def redact_path(s: str) -> str:
    """A path under a home directory -> ``~/…/<file name>`` (``~`` for the home itself); any
    other string is returned with the home directory replaced by ``~``."""
    if not isinstance(s, str) or not s:
        return s
    for h in _home_dirs():
        if _starts(s, h):
            rest = s[len(h):].lstrip("\\/")
            return "~" if not rest else "~/…/" + re.split(r"[\\/]", rest)[-1]
    if s.startswith("~"):
        rest = s[1:].lstrip("\\/")
        return "~" if not rest else "~/…/" + re.split(r"[\\/]", rest)[-1]
    m = _HOME_RE.match(s)
    if m:
        rest = s[m.end():].lstrip("\\/")
        return "~" if not rest else "~/…/" + re.split(r"[\\/]", rest)[-1]
    return redact_text(s)


def _starts(s: str, prefix: str) -> bool:
    if os.sep == "\\":
        return s.lower().startswith(prefix.lower()) and (len(s) == len(prefix) or s[len(prefix)] in "\\/")
    return s.startswith(prefix) and (len(s) == len(prefix) or s[len(prefix)] == "/")


def redact_text(text: str) -> str:
    """Free text (a log line, a message): every home directory path prefix becomes ``~``."""
    for h in _home_dirs():
        if h in text:
            text = text.replace(h, "~")
    return _HOME_RE.sub("~", text)


def _key_words(key: str) -> str:
    return re.split(r"[.\[]", key)[-1].lower() if key else ""


def secret_key(key: str) -> bool:
    last = _key_words(key)
    return any(w in last for w in SECRET_KEY_WORDS)


def personal_key(key: str) -> bool:
    return _key_words(key) in PERSONAL_KEYS


def host_key(key: str) -> bool:
    last = _key_words(key)
    return any(last == s or last.endswith("_" + s) for s in HOST_KEY_SUFFIXES)


def mark_url(s: str, card_host: str = "") -> str:
    """``https://user:pw@example.com:8443/a/b?x=1`` -> ``[network host] https://example.com:8443/…``;
    the card's own address is kept as it is."""
    try:
        u = urllib.parse.urlsplit(s)
        host = u.hostname or ""
        port = u.port
    except ValueError:
        return NETWORK_MARK + REDACTED
    if card_host and host == card_host:
        return s
    tail = "/…" if (u.path not in ("", "/") or u.query or u.fragment) else ""
    return f"{NETWORK_MARK}{u.scheme}://{host}{f':{port}' if port else ''}{tail}"


def redact_config(data: Any, card_host: str = "") -> tuple[Any, list[dict]]:
    """A copy of ``data`` (the configuration) with the rules of the module docstring applied, and
    the list of changes ``[{"key", "reason"}]`` (key names only, never a value)."""
    changes: list[dict] = []

    def walk(key: str, v: Any) -> Any:
        if key and secret_key(key):
            if v in ("", None, [], {}):
                return v
            changes.append({"key": key, "reason": "secret"})
            return REDACTED
        if key and personal_key(key):
            if v in ("", None, [], {}):
                return v
            changes.append({"key": key, "reason": "personal data"})
            return REDACTED
        if isinstance(v, dict):
            return {k: walk(f"{key}.{k}" if key else str(k), x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(f"{key}[{i}]", x) for i, x in enumerate(v)]
        if isinstance(v, str) and v:
            if _URL_RE.match(v):
                out = mark_url(v, card_host)
                if out != v:
                    changes.append({"key": key, "reason": "network host (path and query dropped)"})
                return out
            if host_key(key) and v != card_host and v not in ("localhost", "127.0.0.1", "::1"):
                changes.append({"key": key, "reason": "network host"})
                return NETWORK_MARK + v
            out = redact_path(v)
            if out != v:
                changes.append({"key": key, "reason": "path under the home directory"})
            return out
        return v

    return walk("", data), changes


# =========================================================================== machine parameters
def params_hash(params: dict) -> str:
    """SHA-256 of the machine parameter values (canonical JSON: sorted keys, no spaces)."""
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def params_section(params: Optional[dict], defaults: Optional[dict] = None, profile: Any = None) -> dict:
    """Hash, summary and the values themselves."""
    if not params:
        return {"available": False}
    out: dict = {"sha256": params_hash(params), "profile": profile}
    summary: dict = {"files": {k: len(v or {}) for k, v in params.items()}}
    if defaults:
        changed = sorted(f"{f}:{k}" for f, st in params.items() for k, v in (st or {}).items()
                         if (defaults.get(f) or {}).get(k) != v)
        summary["differs_from_defaults"] = len(changed)
        summary["changed_keys"] = changed
    out["summary"] = summary
    out["values"] = params
    return out


# =========================================================================== controller (cached state)
def _jsonable(v: Any, depth: int = 0) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, bytes):
        return v.hex()
    if depth > 4:
        return repr(v)[:EVENT_TEXT_CAP]
    if is_dataclass(v) and not isinstance(v, type):
        try:
            return _jsonable(asdict(v), depth + 1)
        except Exception:  # noqa: BLE001
            return repr(v)[:EVENT_TEXT_CAP]
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x, depth + 1) for x in v]
    if hasattr(v, "value") and type(v).__module__ != "builtins":     # enums
        return str(v)
    return repr(v)[:EVENT_TEXT_CAP]


def _wall(mono: float, now_wall: float, now_mono: float) -> float:
    return round(now_wall - (now_mono - mono), 3)


def _iso(t: Optional[float]) -> Optional[str]:
    if not t:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def frame_records(frames: Iterable[tuple]) -> list[dict]:
    """The supervisor's frame ring as records; frames to :data:`EXCLUDED_REGISTERS` are dropped
    (out of scope, see there)."""
    out = []
    for e in frames:
        try:
            ch, t, func, addr, tx, rx, outcome = e
        except (TypeError, ValueError):
            continue
        if addr in EXCLUDED_REGISTERS:          # out of scope: never exported
            continue
        out.append({"t": _iso(t), "channel": ch, "func": f"0x{int(func):02x}", "addr": int(addr),
                    "tx": (tx or b"").hex()[:FRAME_HEX_CAP], "rx": None if rx is None else rx.hex()[:FRAME_HEX_CAP],
                    "outcome": outcome})
    return out


def controller_snapshot(ctl: Any) -> dict:
    """What the controller already holds, as JSON-able data.  Reads attributes and in-memory
    lists only; calls nothing that talks to the card.  ``ctl``: a UiController (None: empty)."""
    if ctl is None:
        return {"available": False}
    now_wall, now_mono = time.time(), time.monotonic()
    out: dict = {"available": True}
    g = lambda name, default=None: getattr(ctl, name, default)    # noqa: E731
    cfg = g("cfg")
    ver = int(g("version", 0) or 0)
    sc = g("scaling")
    st = g("_s")                                 # MachineState (cached; read only)
    out["card"] = {
        "host": getattr(cfg, "host", None), "port": getattr(cfg, "port", None),
        "transport": getattr(cfg, "transport", None),
        "connected": bool(getattr(st, "connected", False)), "simulator": g("sim") is not None,
        # identity as read at connect: firmware version and config word (RO[0..1]); nothing else
        "firmware_version": ver or None, "firmware": f"V{ver // 100}.{ver % 100:02d}" if ver else None,
        "config_word": f"0x{int(g('config_word', 0) or 0):08x}",
        "scaling": _safe(lambda: sc.summary()) if sc is not None else None,
        "laser_enabled": bool(g("laser_enabled", True)),
    }
    out["state"] = _jsonable(st) if st is not None else None
    fsm = g("fsm")
    if fsm is not None:
        out["safety"] = {"status": _safe(lambda: _jsonable(fsm.status())),
                         "transitions": [{"t": _iso(t), "from": a, "to": b, "reason": r}
                                         for t, a, b, r in list(getattr(fsm, "transitions", []))]}
    sup = g("_sup")                              # the supervisor: lists and counters read only
    if sup is not None:
        halts = []
        for h in list(getattr(sup, "halts", [])):
            halts.append({"t": _iso(_wall(h.t0, now_wall, now_mono)) if h.t0 else None, "reason": h.reason,
                          "channel": h.channel, "ok": h.ok, "ms": round(h.duration_s * 1000, 1),
                          "steps": [{"name": n, "ok": ok, "error": err} for n, ok, err in h.steps]})
        out["supervisor"] = {
            "halts": halts, "halt_count": getattr(sup, "halt_count", None),
            "latched": _safe(lambda: bool(sup.halted)), "link_ok": getattr(sup, "link_ok", None),
            "streaming": getattr(sup, "streaming", None),
            "heartbeat_errors": getattr(sup, "heartbeat_errors", None),
            "ui_thread_requests": getattr(sup, "ui_thread_requests", None),
            "fifo_frames": getattr(sup, "fifo_frames", None),
            "refused": [{"t": _iso(t), "cls": c, "reason": r} for t, c, r in list(getattr(sup, "refused", []))],
        }
        rf = getattr(sup, "recent_frames", None)
        out["frames"] = frame_records(rf()) if callable(rf) else []
    alarms = g("active_alarms") or []
    out["active_alarms"] = [{"code": getattr(a, "code", None), "kind": getattr(a, "kind", None),
                             "text": _safe(lambda a=a: a.text(None)) if callable(getattr(a, "text", None))
                             else str(a)} for a in list(alarms)]
    ev = g("events")
    out["events"] = [{"type": type(e).__name__, "data": redact_text(repr(e))[:EVENT_TEXT_CAP]}
                     for e in list(getattr(ev, "history", []) or [])]
    out["job"] = job_summary(g("job"))
    return out


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a broken field must not stop the bundle
        return f"error: {type(e).__name__}: {e}"


def job_summary(job: Any) -> Optional[dict]:
    """The compiled job's numbers (no geometry)."""
    if job is None:
        return None
    keys = ("steps", "duration_s", "overspeed_periods", "laser_on_length_mm", "rapid_length_mm", "pierces",
            "dry_run", "process_items", "process_time_s", "start_mm", "end_mm")
    out = {k: _jsonable(getattr(job, k, None)) for k in keys}
    try:
        out["wire_items"] = len(job.wire)
    except Exception:  # noqa: BLE001
        out["wire_items"] = None
    out["cut_blocks"] = len(getattr(job, "cut_blocks", []) or [])
    out["layers"] = sorted({int(b[2]) for b in getattr(job, "cut_blocks", []) or [] if len(b) > 2
                            and isinstance(b[2], int)})
    return out


def document_summary(doc: Any) -> Optional[dict]:
    """Counts, extent and the used layers' process values of a document; no geometry."""
    if doc is None:
        return None
    parts = list(getattr(doc, "parts", []) or [])
    contours = [c for p in parts for c in getattr(p, "contours", [])]
    used = sorted({getattr(c, "layer", None) for c in contours if getattr(c, "layer", None) is not None},
                  key=str)
    layers = {}
    for i, lp in (getattr(doc, "layers", {}) or {}).items():
        if i in used:
            layers[str(i)] = {k: getattr(lp, k, None) for k in ("speed", "power_pct", "frequency_hz", "duty_pct",
                                                                 "gas", "gas_pressure", "cut_height")}
    return {"parts": len(parts), "contours": len(contours),
            "open_contours": sum(1 for c in contours if not getattr(c, "closed", True)),
            "bbox_mm": _safe(lambda: [round(float(v), 3) for v in doc.bbox()]),
            "layers_used": [str(u) for u in used], "layer_params": layers,
            "extra_keys": sorted(str(k) for k in (getattr(doc, "extra", {}) or {}))}


# =========================================================================== logs
LOG_PATTERNS = (re.compile(r"^mlaser\.log(\.\d+)?$"), re.compile(r"^audit\.log(\.\d+)?$"),
                re.compile(r"^crash\.log$"))


def log_files(log_dirs: Iterable[str], ui_log_dirs: Iterable[str] = ()) -> list[tuple[str, Path]]:
    """``[(name in the zip, path)]``, newest first: the rotating logs and ``audit.log`` (with
    their rotated copies), ``crash.log``, and the operator's daily UI logs (``*.log``)."""
    found: list[tuple[float, str, Path]] = []
    for label, dirs, match in (("app", log_dirs, lambda n: any(p.match(n) for p in LOG_PATTERNS)),
                               ("ui", ui_log_dirs, lambda n: n.lower().endswith((".log", ".txt")))):
        for d in dirs:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_file() and match(e.name):
                        found.append((e.stat().st_mtime, f"logs/{label}/{e.name}", Path(e.path)))
                except OSError:
                    continue
    found.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _t, name, p in found:
        if name not in seen:
            seen.add(name)
            out.append((name, p))
    return out


def read_tail(p: Path, cap: int) -> tuple[bytes, bool]:
    """The file, or its newest ``cap`` bytes (starting at a line) when larger; (data, truncated)."""
    with open(p, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size <= cap:
            fh.seek(0)
            return fh.read(), False
        fh.seek(size - cap)
        data = fh.read()
    nl = data.find(b"\n")
    return (data[nl + 1:] if 0 <= nl < 4096 else data), True


# =========================================================================== contents and writing
@dataclass
class Item:
    name: str          # path in the zip
    what: str          # what it is, for people (English, the bundle's own README)
    key: str           # i18n id of the same description (the save dialog's list)


def contents(src: Sources, include_drawing: bool = False) -> list[Item]:
    """What :func:`write_bundle` will write, in order (shown to the operator before saving)."""
    items = [
        Item("README.txt", "What this bundle is and what was left out", "diag.bundle.item.readme"),
        Item("system.json", "Version, commit, Python, Qt, OS, CPU, locale, screens, kernel, dependencies",
             "diag.bundle.item.system"),
        Item("config.json", "Settings (secrets, personal data and home paths redacted)", "diag.bundle.item.config"),
        Item("machine_parameters.json", "Machine parameters: SHA-256, summary and values",
             "diag.bundle.item.params"),
        Item("controller.json", "Card identity as read at connect, machine state, safety transitions, halts, "
             "recent card frames, events", "diag.bundle.item.controller"),
        Item("alarm_history.json", "Alarm and fault history", "diag.bundle.item.alarms"),
        Item("plan.json", "Plan / job summary of the current document (no geometry)", "diag.bundle.item.plan"),
        Item("logs/", "mlaser.log, audit.log, crash.log and the daily logs, newest first, size-limited",
             "diag.bundle.item.logs"),
    ]
    if include_drawing and src.drawing:
        items.append(Item(f"drawing/{_safe_name(src.drawing_name)}", "The current drawing", "diag.bundle.item.drawing"))
    return items


def _safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1] or "drawing.mlz"
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:100]


def _scrub(v: Any) -> Any:
    """Every string with its home directory prefix replaced by ``~`` (a module path, a message)."""
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        return {k: _scrub(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    return v


def _dump(v: Any) -> bytes:
    return json.dumps(_scrub(v), indent=1, ensure_ascii=False, default=str).encode("utf-8")


def build(src: Sources, include_drawing: bool = False, now: Optional[float] = None) -> dict[str, bytes]:
    """The bundle's files ``{name: bytes}`` (not yet zipped); see :func:`write_bundle`."""
    now = time.time() if now is None else now
    files: dict[str, bytes] = {}
    files["system.json"] = _dump(system_info(src.screens))
    cfg, changes = redact_config(src.config or {}, src.card_host)
    files["config.json"] = _dump({"config": cfg, "load_error": redact_text(src.config_error or "") or None,
                                  "redactions": changes})
    prof = (src.config or {}).get("profile") if isinstance(src.config, dict) else None
    files["machine_parameters.json"] = _dump(params_section(src.machine_params, src.machine_param_defaults, prof))
    files["controller.json"] = _dump(_safe(lambda: controller_snapshot(src.controller)))
    files["alarm_history.json"] = _dump([_jsonable(a) for a in src.alarm_history])
    files["plan.json"] = _dump(src.plan or {})
    for k, v in (src.extra or {}).items():
        files[f"extra/{_safe_name(k)}.json"] = _dump(v)
    budget = LOG_BUDGET
    logs: list[dict] = []
    for name, p in log_files(src.log_dirs, src.ui_log_dirs):
        if budget <= 0:
            logs.append({"name": name, "included": False, "reason": "size budget reached"})
            continue
        take = min(LOG_FILE_CAP, budget)
        try:
            data, cut = read_tail(p, take)
        except OSError as e:
            logs.append({"name": name, "included": False, "reason": str(e)})
            continue
        text = redact_text(data.decode("utf-8", "replace"))
        files[name] = text.encode("utf-8")
        budget -= take if cut else len(data)      # a cut file used up its whole share
        logs.append({"name": name, "included": True, "bytes": len(data), "truncated_to_newest": cut})
    drawing = None
    if include_drawing and src.drawing:
        drawing = f"drawing/{_safe_name(src.drawing_name)}"
        files[drawing] = bytes(src.drawing)
    manifest = {"created": _iso(now), "app": build_identity().as_dict(),
                "files": sorted([*files, "manifest.json", "README.txt"]),
                "logs": logs, "drawing_included": drawing is not None, "redactions": len(changes),
                "excluded_registers": sorted(EXCLUDED_REGISTERS)}
    files["manifest.json"] = _dump(manifest)
    files["README.txt"] = _readme(manifest).encode("utf-8")
    return files


def _readme(manifest: dict) -> str:
    # the bundle's own text is English: it is read by the maintainer, not shown in the UI
    return "\n".join([
        "mlaser diagnostic bundle",
        f"created {manifest['created']}, mlaser {manifest['app']['version']} "
        f"(commit {manifest['app']['commit']}{', modified' if manifest['app']['dirty'] else ''})",
        "",
        "This file was written on the operator's computer and was not sent anywhere.",
        "Contents: system.json, config.json (redacted), machine_parameters.json, controller.json,",
        "alarm_history.json, plan.json, logs/ (newest first, size-limited), manifest.json"
        + (", drawing/" if manifest["drawing_included"] else "") + ".",
        "",
        "Left out: tokens, passwords and notification credentials, e-mail addresses and user names",
        "(replaced by <redacted>); paths under the home directory are shortened to ~/…/<name>;",
        "network hosts other than the card's are marked [network host] with path and query removed;",
        "the drawing's geometry unless the operator ticked it; anything of the vendor's licence /",
        "activation path (registers 59500 / 59503 are never read or exported).",
        "No card command was sent to make this bundle: it holds what the program had already read.",
        "",
    ])


def write_bundle(path: str | os.PathLike, src: Sources, include_drawing: bool = False) -> dict:
    """Write the zip to ``path`` (a temporary file renamed into place, so a failure leaves no
    half bundle); returns the manifest.  Runs off the GUI thread; no network, no card I/O."""
    files = build(src, include_drawing)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    buf_order = ["README.txt", "manifest.json"] + sorted(n for n in files if n not in ("README.txt", "manifest.json"))
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for n in buf_order:
                zf.writestr(n, files[n])
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return json.loads(files["manifest.json"])


def default_name(now: Optional[float] = None) -> str:
    """``mlaser-diagnostics-20261110-153012.zip``"""
    return time.strftime("mlaser-diagnostics-%Y%m%d-%H%M%S.zip", time.localtime(time.time() if now is None else now))


def read_bundle(path: str | os.PathLike) -> dict[str, bytes]:
    """The files of a bundle (tests, tools)."""
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


__all__ = ["Sources", "Item", "contents", "build", "write_bundle", "read_bundle", "default_name", "system_info",
           "redact_config", "redact_path", "redact_text", "params_section", "params_hash", "controller_snapshot",
           "frame_records", "document_summary", "job_summary", "log_files", "EXCLUDED_REGISTERS", "SECRET_KEY_WORDS",
           "PERSONAL_KEYS", "REDACTED", "NETWORK_MARK", "LOG_BUDGET", "LOG_FILE_CAP"]
