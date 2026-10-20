"""Job intake: jobs submitted from outside the operator screen wait for the operator's approval.

The automation API (:mod:`mlaser.api`) and the command line (``mlaser queue add``) never put a
job into the queue directly.  They **submit** it here; the job waits as *pending approval* until
the operator approves it at the machine (Run screen > Queue > **Approve**), and only then becomes
an ordinary queue entry, which still runs only after the operator's "Sheet loaded: start" and the
normal Start path (arming question, pre-flight, the start gate; docs/SAFETY.md).  Nothing here can
start, stop, arm or move anything: the store has no reference to a controller.

Storage (``<data>/production``):

* ``inbox/<id>.json``: one file per submission, written atomically by any thread or process
  (the API server's threads, a CLI run while the program is closed);
* ``inbox/files/``: uploaded drawings (API uploads, ``.mlz`` converted by the CLI);
* ``pending.json``: the pending list the owner (the application's GUI thread) keeps.  It is a
  separate file on purpose: a pending job is never in ``queue.json``, so no version of the
  program that does not know about approvals can mistake it for an approved one.

:meth:`IntakeStore.scan` (the owner, every half second) moves new inbox files into the pending
list; a file that cannot be read or fails validation is renamed ``.bad`` and reported.

What a submission may carry is limited to the queue's descriptive fields: the drawing, a name,
a quantity, material, thickness, sheet and a note.  It can carry no layer parameters, no
technology and no ``extra`` data: the cutting parameters come from the drawing and the machine's
layer table, as for a job the operator adds.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("mlaser.intake")

FORMAT = "mlaser-intake/1"
INBOX = "inbox"
FILES = "files"
PENDING_FILE = "pending.json"
#: drawings the queue can run (the queue loads .chf / .dxf / .svg; convert a .mlz first)
QUEUE_SUFFIXES = (".chf", ".dxf", ".svg")
MAX_PENDING = 100                 # pending + inbox; more submissions are refused
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024     # one inbox file (the JSON, not the drawing)
MAX_QUANTITY = 9999
_LIMITS = {"name": 120, "material": 80, "sheet": 80, "note": 500, "client": 120, "reference": 120}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
SOURCES = ("api", "cli", "test")


class IntakeError(ValueError):
    """A submission was refused (the message says why; safe to show to the submitter)."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PendingJob:
    """One submitted job waiting for the operator."""

    path: str
    name: str = ""
    quantity: int = 1
    material: str = ""
    thickness_mm: float = 0.0
    sheet: str = ""
    note: str = ""                      # the submitter's note (shown to the operator)
    reference: str = ""                 # the submitter's own id (order number, ERP key)
    source: str = "api"                 # api | cli
    client: str = ""                    # API client address or CLI user
    submitted: float = 0.0
    id: str = ""
    upload: bool = False                # the drawing was uploaded into inbox/files

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "PendingJob":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def public(self) -> dict:
        """What the API shows (no local path)."""
        d = self.to_json()
        d["file"] = os.path.basename(self.path)
        d.pop("path", None)
        d["status"] = "pending_approval"
        return d


def _clean_text(v: Any, key: str) -> str:
    s = "" if v is None else str(v)
    s = "".join(ch for ch in s if ch >= " " or ch == "\t").strip()
    return s[:_LIMITS.get(key, 200)]


def validate(request: dict, source: str, client: str = "", allowed_roots: Optional[Iterable[str]] = None,
             clock: Callable[[], float] = time.time) -> PendingJob:
    """A :class:`PendingJob` from a submission dict (``path``, ``name``, ``quantity``,
    ``material``, ``thickness_mm``, ``sheet``, ``note``, ``reference``); IntakeError when it is
    not acceptable.  ``allowed_roots``: the path must lie inside one of them (the API: its upload
    directory and the configured job folders); None = any readable path (the local CLI)."""
    if not isinstance(request, dict):
        raise IntakeError("the request must be a JSON object")
    unknown = sorted(set(request) - {"path", "name", "quantity", "material", "thickness_mm", "sheet",
                                      "note", "reference"})
    if unknown:
        raise IntakeError(f"unknown field(s): {', '.join(unknown)} (a submission carries no cutting "
                          "parameters)", "unknown_field")
    raw = request.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise IntakeError("path: the drawing file is required")
    path = os.path.realpath(os.path.abspath(raw))
    if os.path.splitext(path)[1].lower() not in QUEUE_SUFFIXES:
        raise IntakeError(f"path: only {', '.join(QUEUE_SUFFIXES)} drawings can be queued", "unsupported")
    if allowed_roots is not None:
        roots = [os.path.realpath(os.path.abspath(r)) for r in allowed_roots if r]
        if not any(os.path.commonpath([path, r]) == r for r in roots):
            raise IntakeError("path: outside the folders this API may queue from", "forbidden_path")
    if not os.path.isfile(path):
        raise IntakeError("path: file not found", "not_found")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise IntakeError(f"path: {exc.strerror or exc}", "not_found") from exc
    if size > MAX_FILE_BYTES:
        raise IntakeError(f"path: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB", "too_large")
    try:
        qty = int(request.get("quantity", 1))
    except (TypeError, ValueError) as exc:
        raise IntakeError("quantity: a whole number 1..9999") from exc
    if isinstance(request.get("quantity"), bool) or not 1 <= qty <= MAX_QUANTITY:
        raise IntakeError("quantity: a whole number 1..9999")
    try:
        thick = float(request.get("thickness_mm", 0.0) or 0.0)
    except (TypeError, ValueError) as exc:
        raise IntakeError("thickness_mm: a number of millimetres") from exc
    if not 0.0 <= thick <= 200.0 or thick != thick:
        raise IntakeError("thickness_mm: 0..200 mm")
    if source not in SOURCES:
        raise IntakeError(f"source: {source!r}")
    name = _clean_text(request.get("name"), "name") or Path(path).stem[:_LIMITS["name"]]
    return PendingJob(path=path, name=name, quantity=qty,
                      material=_clean_text(request.get("material"), "material"), thickness_mm=thick,
                      sheet=_clean_text(request.get("sheet"), "sheet"), note=_clean_text(request.get("note"), "note"),
                      reference=_clean_text(request.get("reference"), "reference"), source=source,
                      client=_clean_text(client, "client"), submitted=float(clock()), id=uuid.uuid4().hex[:12])


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class IntakeStore:
    """The inbox and the pending list (see the module docstring).  ``directory`` is the
    production data directory (``<data>/production``)."""

    def __init__(self, directory: os.PathLike | str, clock: Callable[[], float] = time.time,
                 max_pending: Optional[int] = None, audit: Optional[Callable[..., None]] = None,
                 owner: bool = True) -> None:
        """``owner``: this process keeps the pending list (the application).  A submitter that is
        not the owner (the CLI) only reads ``pending.json`` (for the cap) and never rewrites it."""
        self.dir = Path(directory)
        self.clock = clock
        self.max_pending = int(MAX_PENDING if max_pending is None else max_pending)
        self.pending: list[PendingJob] = []
        self.listeners: list[Callable[[], None]] = []
        self.errors: list[str] = []           # inbox files that were refused (newest last)
        self._lock = threading.Lock()
        if audit is None:
            from mlaser.machine.audit import audit as _audit
            audit = _audit
        self._audit = audit
        self.owner = bool(owner)
        self.load()

    # ------------------------------------------------------------------ paths
    @property
    def inbox(self) -> Path:
        return self.dir / INBOX

    @property
    def files_dir(self) -> Path:
        return self.dir / INBOX / FILES

    @property
    def pending_path(self) -> Path:
        return self.dir / PENDING_FILE

    # ------------------------------------------------------------------ persistence (owner)
    def load(self) -> None:
        try:
            data = json.loads(self.pending_path.read_text("utf-8"))
            items = data.get("pending", []) if isinstance(data, dict) else []
            self.pending = [PendingJob.from_json(d) for d in items if isinstance(d, dict) and d.get("path")]
        except FileNotFoundError:
            self.pending = []
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.pending = []
            if not self.owner:
                return
            log.warning("intake: %s unreadable (%s); kept as .bad", self.pending_path, exc)
            try:
                os.replace(self.pending_path, str(self.pending_path) + ".bad")
            except OSError:
                pass

    def save(self) -> None:
        data = {"format": FORMAT, "saved": self.clock(), "pending": [p.to_json() for p in self.pending]}
        _atomic_write(self.pending_path, json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8"))

    def subscribe(self, fn: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def _changed(self) -> None:
        self.save()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener must not break the store
                log.exception("intake listener failed")

    # ------------------------------------------------------------------ submit (any thread)
    def _inbox_count(self) -> int:
        try:
            return sum(1 for n in os.listdir(self.inbox) if n.endswith(".json"))
        except OSError:
            return 0

    def count(self) -> int:
        """Pending jobs plus submissions not yet picked up."""
        with self._lock:
            return len(self.pending) + self._inbox_count()

    def submit(self, request: dict, source: str, client: str = "",
               allowed_roots: Optional[Iterable[str]] = None) -> PendingJob:
        """Validate and drop a submission into the inbox; it appears in :attr:`pending` at the
        owner's next :meth:`scan`.  Thread-safe and process-safe (one new file per call)."""
        job = validate(request, source, client, allowed_roots, self.clock)
        with self._lock:
            if len(self.pending) + self._inbox_count() >= self.max_pending:
                raise IntakeError(f"{self.max_pending} jobs are already waiting for approval", "full")
            data = json.dumps({"format": FORMAT, "job": job.to_json()}, ensure_ascii=False).encode("utf-8")
            _atomic_write(self.inbox / f"{job.id}.json", data)
        self._audit("intake_submitted", id=job.id, source=job.source, client=job.client,
                    file=os.path.basename(job.path), quantity=job.quantity)
        return job

    def store_upload(self, filename: str, content: bytes) -> str:
        """Save an uploaded drawing under ``inbox/files``; see :func:`store_upload`."""
        return store_upload(self.dir, filename, content)

    # ------------------------------------------------------------------ owner
    def scan(self) -> list[PendingJob]:
        """Move new inbox files into the pending list (owner thread).  Returns the new jobs."""
        try:
            names = sorted(n for n in os.listdir(self.inbox) if n.endswith(".json"))
        except OSError:
            return []
        new: list[PendingJob] = []
        for n in names:
            p = self.inbox / n
            try:
                if p.stat().st_size > MAX_REQUEST_BYTES:
                    raise ValueError("submission file too large")
                data = json.loads(p.read_text("utf-8"))
                if not isinstance(data, dict) or data.get("format") != FORMAT:
                    raise ValueError("not an mlaser intake file")
                job = PendingJob.from_json(data["job"])
                if not job.id or not job.path:
                    raise ValueError("incomplete submission")
                if any(j.id == job.id for j in self.pending):
                    raise ValueError(f"duplicate id {job.id}")
            except FileNotFoundError:
                continue                       # picked up by another scan
            except (OSError, ValueError, TypeError, KeyError) as exc:
                msg = f"{n}: {exc}"
                self.errors.append(msg)
                del self.errors[:-20]
                log.warning("intake: refused %s", msg)
                try:
                    os.replace(p, str(p) + ".bad")
                except OSError:
                    pass
                continue
            with self._lock:
                self.pending.append(job)
                try:
                    os.unlink(p)
                except OSError:
                    pass
            new.append(job)
        if new:
            self._changed()
        return new

    def get(self, job_id: str) -> PendingJob:
        for j in self.pending:
            if j.id == job_id:
                return j
        raise KeyError(job_id)

    def approve(self, job_id: str, queue: Any, operator: str = "operator", position: Optional[int] = None) -> Any:
        """The operator approved the job at the machine: it becomes a queue entry (status
        ``waiting``; it still runs only after "Sheet loaded: start").  Returns the entry."""
        job = self.get(job_id)
        if not os.path.isfile(job.path):
            raise IntakeError("the drawing no longer exists", "not_found")
        info = {"source": job.source, "client": job.client, "submitted": job.submitted,
                "reference": job.reference, "note": job.note, "approved_by": operator,
                "approved_at": self.clock(), "intake_id": job.id}
        entry = queue.add(job.path, job.quantity, name=job.name, position=position, material=job.material,
                          thickness_mm=job.thickness_mm, sheet=job.sheet, extra={"intake": info})
        with self._lock:
            self.pending = [j for j in self.pending if j.id != job_id]
        self._audit("intake_approved", id=job.id, entry=getattr(entry, "id", ""), source=job.source,
                    client=job.client, by=operator)
        self._changed()
        return entry

    def reject(self, job_id: str, operator: str = "operator", reason: str = "") -> PendingJob:
        job = self.get(job_id)
        with self._lock:
            self.pending = [j for j in self.pending if j.id != job_id]
        if job.upload or self._is_upload(job.path):
            try:
                os.unlink(job.path)
            except OSError:
                pass
        self._audit("intake_rejected", id=job.id, source=job.source, client=job.client, by=operator,
                    reason=reason)
        self._changed()
        return job

    def _is_upload(self, path: str) -> bool:
        try:
            return os.path.commonpath([os.path.realpath(path), os.path.realpath(self.files_dir)]) == \
                os.path.realpath(self.files_dir)
        except ValueError:
            return False


def store_upload(directory: os.PathLike | str, filename: str, content: bytes) -> str:
    """Save a drawing under ``<directory>/inbox/files`` with a fresh name (the extension must be a
    queue drawing; the name is reduced to safe characters).  Returns the stored path."""
    base = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    if ext.lower() not in QUEUE_SUFFIXES:
        raise IntakeError(f"filename: only {', '.join(QUEUE_SUFFIXES)} drawings can be queued", "unsupported")
    if len(content) > MAX_FILE_BYTES:
        raise IntakeError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB", "too_large")
    if not content:
        raise IntakeError("the file is empty")
    stem = (_SAFE_NAME.sub("_", stem)[:60] or "job").strip(" .") or "job"
    path = Path(directory) / INBOX / FILES / f"{uuid.uuid4().hex[:8]}_{stem}{ext.lower()}"
    _atomic_write(path, content)
    return str(path)


def submit_offline(directory: os.PathLike | str, request: dict, source: str = "cli", client: str = "",
                   allowed_roots: Optional[Iterable[str]] = None) -> PendingJob:
    """Submit without being the owner (the CLI): validates and writes the inbox file only; the
    running application (or its next start) picks it up."""
    return IntakeStore(directory, owner=False).submit(request, source, client, allowed_roots)


__all__ = ["IntakeStore", "PendingJob", "IntakeError", "validate", "submit_offline", "store_upload", "QUEUE_SUFFIXES",
           "MAX_PENDING", "MAX_FILE_BYTES", "FORMAT"]
