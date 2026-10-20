"""Reading and writing ``.mlz`` project files (docs/dev/project-format.md).

A project is a zip archive::

    mimetype                 "application/vnd.mlaser.project+zip" (stored, first member)
    manifest.json            format, version, app, times, sha256 + size of every member, notes
    document.json            the Document (mlaser.project.codec, schema in mlaser.project.schema)
    sections/<name>.json     technology assignments, the job queue, other tracks' data
    nest/job.json            the nest job with its last result (tagged dataclasses)
    blobs/<n>.npy|bin        raster engraving arrays and large binary values
    chf/records.chf          the .chf export of the document: carries the opaque .chf records so
                             that a .chf opened, saved as .mlz and exported again is byte-identical
    fonts/<file>             fonts of text outlines that are not bundled (when embedded)

Saving is atomic (a temporary file in the target folder, fsync, ``os.replace``).  Loading checks
the archive, the manifest, every checksum and the document schema before decoding, and migrates
older versions (:mod:`mlaser.project.migrations`); every failure is a :class:`ProjectError` whose
message names the file and what is wrong.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from ..io.limits import ImportCancelled, tick
from ..model import Document
from . import migrations as M
from .codec import CHF_CONTOUR_KEYS, CHF_DOC_KEYS, CHF_PART_KEYS, Blobs, Decoder, Encoder
from .schema import DOCUMENT_SCHEMA, MANIFEST_SCHEMA, SECTION_SCHEMAS, validate

FORMAT = "mlaser-project"
VERSION = M.CURRENT
MIMETYPE = b"application/vnd.mlaser.project+zip"
SUFFIX = ".mlz"
MAX_UNCOMPRESSED = 2**30               # kept for callers: the total limit (MAX_TOTAL below)
FONT_EMBED_LIMIT = 8 * 2**20           # "auto": embed non-bundled font files up to this size

PathLike = Union[str, os.PathLike]


class ProjectError(Exception):
    """A project file that cannot be read (damaged, not a project, too new)."""


@dataclass
class Project:
    """A document plus the data saved with it.

    ``technology``: technology assignments (e.g. layer -> material / thickness / gas / nozzle /
    technology id and version), free JSON owned by the technology database.
    ``queue``: job queue entries (JSON objects).  ``nest``: a ``mlaser.nest.model.NestJob``
    (parts, sheets, parameters, last result).  ``sections``: further named JSON sections (other
    tracks, plugins).  ``fonts``: embedded font files (file name -> bytes).
    ``meta``: the manifest of a loaded file; ``notes``: what could not be kept (save) or was
    migrated (load)."""

    document: Document = field(default_factory=Document)
    technology: dict = field(default_factory=dict)
    queue: list = field(default_factory=list)
    nest: Any = None
    sections: dict[str, Any] = field(default_factory=dict)
    fonts: dict[str, bytes] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _app() -> str:
    try:
        from importlib.metadata import version
        return f"mlaser {version('mlaser')}"
    except Exception:  # noqa: BLE001
        return "mlaser"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _dump(v: Any) -> bytes:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


# =========================================================================== writing
def _has_chf_records(doc: Document) -> bool:
    if isinstance(doc.extra, dict) and doc.extra.get("chf") is not None:
        return True
    return any(dict.get(p.extra, "chf") is not None for p in doc.parts)


def _fonts_of(doc: Document) -> set[str]:
    out = set()
    for p in doc.parts:
        for c in p.contours:
            f = dict.get(c.extra, "font")
            if isinstance(f, str) and f and dict.get(c.extra, "text") is not None:
                out.add(f)
    return out


def _embed_fonts(doc: Document, mode: Union[bool, str], notes: list[str]) -> dict[str, bytes]:
    if not mode:
        return {}
    from ..cad import fonts as F
    out: dict[str, bytes] = {}
    for fam in sorted(_fonts_of(doc)):
        if F.is_bundled_family(fam) or fam == "default":
            continue
        hit = F.find_font_file(fam)
        if hit is None:
            notes.append(f"font {fam!r} not found: not embedded")
            continue
        path = hit[0]
        try:
            size = os.path.getsize(path)
            if mode == "auto" and size > FONT_EMBED_LIMIT:
                notes.append(f"font {fam!r} ({size // 2**20} MB) not embedded (over the limit)")
                continue
            out[os.path.basename(path)] = Path(path).read_bytes()
        except OSError as e:
            notes.append(f"font {fam!r} not embedded: {e}")
    return out


def dumps_project(project: Union[Project, Document], embed_fonts: Union[bool, str] = "auto") -> bytes:
    """The ``.mlz`` bytes of ``project`` (a bare Document is wrapped)."""
    if isinstance(project, Document):
        project = Project(document=project)
    doc = project.document
    blobs = Blobs()
    enc = Encoder(blobs)
    notes: list[str] = []
    ddoc = enc.document(doc)
    members: dict[str, bytes] = {"mimetype": MIMETYPE}
    chf_info = None
    if _has_chf_records(doc):
        from ..formats.chf import dumps_chf
        try:
            members["chf/records.chf"] = dumps_chf(doc, version=doc.version)
            for i, (p, pd) in enumerate(zip(doc.parts, ddoc["parts"])):
                if dict.get(p.extra, "chf") is not None:
                    pd["chf_graph"] = i
            chf_info = {"member": "chf/records.chf", "version": int(doc.version)}
        except Exception as e:  # noqa: BLE001 - the export is rebuilt from the model then
            notes.append(f".chf records not kept ({type(e).__name__}: {e}); a .chf export is rebuilt")
    members["document.json"] = _dump(ddoc)
    sections = dict(project.sections)
    if project.technology:
        sections["technology"] = project.technology
    if project.queue:
        sections["queue"] = project.queue
    for name, v in sorted(sections.items()):
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"bad section name {name!r}")
        ev = enc.value(v, f"sections.{name}")
        members[f"sections/{name}.json"] = _dump(ev)
    if project.nest is not None:
        members["nest/job.json"] = _dump(enc.value(project.nest, "nest"))
    fonts = dict(project.fonts)
    fonts.update(_embed_fonts(doc, embed_fonts, notes))
    for fn, b in sorted(fonts.items()):
        members[f"fonts/{os.path.basename(fn)}"] = b
    members.update(blobs.data)
    notes += [f"not saved: {d}" for d in enc.dropped]
    man = M.manifest_for(members, VERSION, {
        "app": _app(), "created": str(project.meta.get("created") or _now()), "saved": _now(),
        "sections": sorted(sections), "chf": chf_info, "dropped": notes})
    members["manifest.json"] = _dump(man)
    project.notes = notes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)
        for name in ["manifest.json"] + sorted(k for k in members if k not in ("mimetype", "manifest.json")):
            data = members[name]
            ctype = zipfile.ZIP_STORED if name.endswith((".npy",)) and len(data) < 256 else zipfile.ZIP_DEFLATED
            z.writestr(_info(name), data, compress_type=ctype, compresslevel=6)
    return buf.getvalue()


def _info(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    zi.external_attr = 0o644 << 16
    return zi


def atomic_write(path: PathLike, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same folder and ``os.replace``
    (a crash leaves either the old file or the new one, never a half-written file)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if hasattr(os, "O_DIRECTORY"):
        try:
            dfd = os.open(str(p.parent), os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def save_project(path: PathLike, project: Union[Project, Document], embed_fonts: Union[bool, str] = "auto") -> None:
    """Save atomically as ``.mlz``."""
    atomic_write(path, dumps_project(project, embed_fonts=embed_fonts))


# =========================================================================== reading
# Untrusted input (docs/dev/project-format.md "Limits"): a project is a customer file like a
# drawing.  Only ``mimetype``, ``manifest.json`` (or a version-0 ``project.json``) and the
# members the manifest lists are read, each streamed in chunks with a running limit (the sizes
# in the zip headers are not trusted), so a zip bomb costs at most the limits below.
MAX_MEMBER = 256 * 2**20              # one member (raster blobs are the large ones)
MAX_TOTAL = 2**30                     # every member read, together
MAX_MEMBERS = 65536                   # entries in the archive
MAX_RATIO = 200                       # uncompressed : compressed, above RATIO_MIN
RATIO_MIN = 16 * 2**20                # small members may compress better (JSON of zeros)
MAX_JSON = 256 * 2**20                # one JSON member
MAX_JSON_DEPTH = 100                  # nesting of [ and { (a document needs about 12)
_CHUNK = 1 << 20
# raster arrays (blobs/*.npy) of a mostly empty bitmap compress up to ~1000:1 legitimately:
# only the member and total limits apply to them
_RATIO_EXEMPT = ("blobs/",)


def _mb(n: int) -> str:
    return f"{n / 2**20:,.0f} MB"


class _Reader:
    """Streamed, limited reads of zip members (see the limits above)."""

    def __init__(self, z: zipfile.ZipFile, name: str, progress=None):
        # default: the import context of this thread (mlaser.io.limits: progress dialog, Cancel)
        self.z, self.name, self.total = z, name, 0
        self.progress = progress if progress is not None else (lambda n: tick(n, "bytes"))

    def read(self, info: zipfile.ZipInfo, expect: Optional[int] = None, cap: int = MAX_MEMBER) -> bytes:
        """The member's bytes; ``expect``: its size in the manifest (more is damage), ``cap``:
        the limit for this member."""
        name, fn = self.name, info.filename
        if fn.startswith("/") or ".." in fn.split("/"):
            raise ProjectError(f"{name}: bad member name {fn!r}")
        limit = cap if expect is None else min(cap, int(expect))
        if expect is not None and expect > MAX_MEMBER:
            raise ProjectError(f"{name}: member {fn} is {_mb(int(expect))}, more than the limit of "
                               f"{_mb(MAX_MEMBER)} per member")
        ratio = not fn.startswith(_RATIO_EXEMPT)
        csize = max(1, int(info.compress_size))
        buf = bytearray()
        try:
            with self.z.open(info) as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    n = len(buf)
                    self.total += len(chunk)
                    if n > limit:
                        if expect is not None and n > expect:
                            raise ProjectError(f"{name}: {fn} is damaged (checksum mismatch)")   # larger than listed
                        raise ProjectError(f"{name}: member {fn} is larger than the limit of "
                                           f"{_mb(cap)} for it")
                    if self.total > MAX_TOTAL:
                        raise ProjectError(f"{name}: the project expands to more than {_mb(MAX_TOTAL)}; "
                                           "it is either damaged or built to exhaust the computer")
                    if ratio and n > RATIO_MIN and n > MAX_RATIO * csize:
                        raise ProjectError(f"{name}: member {fn} expands more than {MAX_RATIO}:1 "
                                           "(a zip bomb?); not opened")
                    self.progress(self.total)
        except (ProjectError, ImportCancelled):
            raise
        except (zipfile.BadZipFile, EOFError, OSError, ValueError) as e:     # bad CRC, truncated
            raise ProjectError(f"{name}: member {fn} is damaged ({e})") from e
        except MemoryError:
            raise ProjectError(f"{name}: not enough memory to read {fn}") from None
        except Exception as e:  # noqa: BLE001 - zlib.error and friends
            raise ProjectError(f"{name}: member {fn} is damaged ({type(e).__name__}: {e})") from e
        return bytes(buf)


def _json_depth_ok(b: bytes) -> bool:
    """True when the brackets of the JSON text ``b`` nest at most :data:`MAX_JSON_DEPTH` deep
    (strings removed first; numpy, so a large document costs milliseconds)."""
    import re

    import numpy as np
    if b.count(b"[") + b.count(b"{") <= MAX_JSON_DEPTH:
        return True
    t = re.sub(rb'"(?:[^"\\]|\\.)*"', b"", b)
    a = np.frombuffer(t, dtype=np.uint8)
    step = (a == 0x5B).astype(np.int32) + (a == 0x7B) - (a == 0x5D) - (a == 0x7D)
    return int(np.cumsum(step).max(initial=0)) <= MAX_JSON_DEPTH


def _members_listed(raw: bytes) -> Optional[list[str]]:
    """The member names a manifest lists (None when it cannot be read: the checks after the
    read report that)."""
    if len(raw) > MAX_JSON or not _json_depth_ok(raw):
        return None
    try:
        man = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    files = man.get("files") if isinstance(man, dict) else None
    if not isinstance(files, dict):
        return None
    return [k for k in files if isinstance(k, str)]


def _expected_sizes(raw: bytes) -> dict[str, int]:
    try:
        files = json.loads(raw.decode("utf-8")).get("files") or {}
        return {k: int(v["size"]) for k, v in files.items()
                if isinstance(v, dict) and isinstance(v.get("size"), int) and v["size"] >= 0}
    except Exception:  # noqa: BLE001 - validated later
        return {}


def _read_members(data, name: str, progress=None) -> dict[str, bytes]:
    """The members of the archive ``data`` (bytes or a binary file) that a project uses."""
    head = data[:4] if isinstance(data, (bytes, bytearray)) else None
    try:
        z = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    except zipfile.BadZipFile as e:
        if head is None:
            try:
                data.seek(0)
                head = data.read(4)
            except OSError:
                head = b""
        if head != b"PK\x03\x04":
            raise ProjectError(f"{name}: not an mlaser project (.mlz) file") from e
        raise ProjectError(f"{name}: the file is damaged (the zip structure is broken: {e})") from e
    except (OSError, ValueError) as e:
        raise ProjectError(f"{name}: the file is damaged ({e})") from e
    infos = z.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ProjectError(f"{name}: the archive has {len(infos):,} entries (more than {MAX_MEMBERS:,})")
    for i in infos:
        if i.filename.startswith("/") or ".." in i.filename.split("/"):
            raise ProjectError(f"{name}: bad member name {i.filename!r}")
    by_name = {i.filename: i for i in infos if not i.is_dir()}
    rd = _Reader(z, name, progress)
    out: dict[str, bytes] = {}
    for meta in ("mimetype", "manifest.json"):
        if meta in by_name:
            out[meta] = rd.read(by_name[meta], cap=1024 if meta == "mimetype" else MAX_JSON)
    if "manifest.json" not in out:
        if "project.json" in by_name:                   # format version 0: one JSON member
            out["project.json"] = rd.read(by_name["project.json"], cap=MAX_JSON)
        return out
    listed = _members_listed(out["manifest.json"])
    if listed is None:
        return out
    sizes = _expected_sizes(out["manifest.json"])
    claimed = sum(sizes.values())
    if claimed > MAX_TOTAL:
        raise ProjectError(f"{name}: the manifest lists {_mb(claimed)} of content, more than the limit "
                           f"of {_mb(MAX_TOTAL)}")
    for member in listed:
        info = by_name.get(member)
        if info is None or member in out:
            continue                                     # missing: reported by the checksum loop
        out[member] = rd.read(info, sizes.get(member))
    return out


def _load_json(members: dict[str, bytes], member: str, name: str) -> Any:
    try:
        raw = members[member]
    except KeyError:
        raise ProjectError(f"{name}: {member} is missing") from None
    if len(raw) > MAX_JSON:
        raise ProjectError(f"{name}: {member} is larger than {_mb(MAX_JSON)}")
    if not _json_depth_ok(raw):
        raise ProjectError(f"{name}: {member} is nested more than {MAX_JSON_DEPTH} levels deep")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProjectError(f"{name}: {member} is not valid JSON ({e})") from e
    except (ValueError, RecursionError) as e:       # Python's 4300-digit int limit, depth
        raise ProjectError(f"{name}: {member} cannot be read ({e})") from e


def loads_project(data: bytes, name: str = "<project>", progress=None) -> Project:
    """Read ``.mlz`` bytes or a binary file (see the module docstring for the checks, and the
    limits above).  ``progress(bytes_read)`` is called while the members are read; it may raise
    to cancel.  Every failure is a :class:`ProjectError` (also deep JSON, huge integers and
    memory exhaustion: a crafted file must not reach the excepthook)."""
    try:
        return _loads_project(data, name, progress)
    except ProjectError:
        raise
    except RecursionError:
        raise ProjectError(f"{name}: the project data is nested too deeply") from None
    except MemoryError:
        raise ProjectError(f"{name}: not enough memory to open the project") from None
    except ValueError as e:
        raise ProjectError(f"{name}: the project cannot be read ({e})") from e


def _loads_project(data, name: str, progress) -> Project:
    members = _read_members(data, name, progress)
    for meta in ("manifest.json", "project.json"):
        if meta in members:
            _load_json(members, meta, name)          # size and depth limits before parsing
    try:
        version = M.detect_version(members)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProjectError(f"{name}: {e}") from e
    if version > VERSION:
        raise ProjectError(f"{name}: made by a newer mlaser (project format {version}; this version "
                           f"reads formats up to {VERSION}). Update mlaser to open it.")
    migrated: list[int] = []
    if version < VERSION:
        try:
            members, migrated = M.migrate(members)
        except Exception as e:  # noqa: BLE001
            raise ProjectError(f"{name}: cannot migrate project format {version}: {e}") from e
    man = _load_json(members, "manifest.json", name)
    errs = validate(man, MANIFEST_SCHEMA, "manifest")
    if errs:
        raise ProjectError(f"{name}: manifest.json is invalid: " + "; ".join(errs[:5]))
    if "mimetype" in members and members["mimetype"] != MIMETYPE:
        raise ProjectError(f"{name}: unknown mimetype {members['mimetype'][:60]!r}")
    for member, info in man["files"].items():
        b = members.get(member)
        if b is None:
            raise ProjectError(f"{name}: {member} is missing (listed in the manifest)")
        if len(b) != info["size"] or hashlib.sha256(b).hexdigest() != info["sha256"]:
            raise ProjectError(f"{name}: {member} is damaged (checksum mismatch)")
    ddoc = _load_json(members, "document.json", name)
    errs = validate(ddoc, DOCUMENT_SCHEMA, "document")
    if errs:
        raise ProjectError(f"{name}: document.json does not match the schema: " + "; ".join(errs[:5]))
    dec = Decoder(Blobs({k: v for k, v in members.items() if k.startswith("blobs/")}))
    try:
        doc = dec.document(ddoc)
    except Exception as e:  # noqa: BLE001 - schema-valid but undecodable values
        raise ProjectError(f"{name}: document.json cannot be read ({type(e).__name__}: {e})") from e
    notes = [f"migrated from project format {v}" for v in migrated]
    chf = man.get("chf")
    if chf and chf.get("member") in members:
        _graft_chf(doc, ddoc, members[chf["member"]], name, notes)
    proj = Project(document=doc, meta=man, notes=notes)
    for member in sorted(members):
        if member.startswith("sections/") and member.endswith(".json"):
            sec = member[len("sections/"):-len(".json")]
            raw = _load_json(members, member, name)
            sch = SECTION_SCHEMAS.get(sec)
            if sch is not None:
                errs = validate(raw, sch, sec)
                if errs:
                    raise ProjectError(f"{name}: section {sec} is invalid: " + "; ".join(errs[:5]))
            try:
                v = dec.value(raw)
            except Exception as e:  # noqa: BLE001
                raise ProjectError(f"{name}: section {sec} cannot be read ({e})") from e
            if sec == "technology":
                proj.technology = v
            elif sec == "queue":
                proj.queue = v
            else:
                proj.sections[sec] = v
    if "nest/job.json" in members:
        try:
            proj.nest = dec.value(_load_json(members, "nest/job.json", name))
        except ProjectError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ProjectError(f"{name}: the nest job cannot be read ({e})") from e
    proj.fonts = {k[len("fonts/"):]: v for k, v in members.items() if k.startswith("fonts/")}
    return proj


def _graft_chf(doc: Document, ddoc: dict, data: bytes, name: str, notes: list[str]) -> None:
    """Put the opaque .chf records back onto the parts that had them when the file was saved."""
    from ..formats.chf import loads_chf
    try:
        cdoc = loads_chf(data)
    except Exception as e:  # noqa: BLE001 - the model is complete without them
        notes.append(f".chf records unreadable ({e}); a .chf export is rebuilt from the model")
        return
    for p, pd in zip(doc.parts, ddoc.get("parts", [])):
        g = pd.get("chf_graph")
        if g is None or g >= len(cdoc.parts):
            continue
        src = cdoc.parts[g]
        if len(src.contours) != len(p.contours):
            notes.append(f"part {g}: .chf record does not match the geometry; rebuilt on export")
            continue
        for k in CHF_PART_KEYS:
            v = dict.get(src.extra, k)
            if v is not None:
                dict.__setitem__(p.extra, k, v)
        for c, sc in zip(p.contours, src.contours):
            for k in CHF_CONTOUR_KEYS:
                v = dict.get(sc.extra, k)
                if v is not None:
                    dict.__setitem__(c.extra, k, v)
    for k in CHF_DOC_KEYS:
        if k in cdoc.extra:
            doc.extra[k] = cdoc.extra[k]


def load_project(path: PathLike, progress=None) -> Project:
    """Open a ``.mlz`` file (read from the file, not loaded into memory whole first)."""
    p = Path(path)
    try:
        f = open(p, "rb")
    except OSError as e:
        raise ProjectError(f"{p}: cannot read the file ({e.strerror or e})") from e
    with f:
        return loads_project(f, str(p), progress)  # type: ignore[arg-type]


# =========================================================================== conveniences
def is_project_file(path: PathLike) -> bool:
    return str(path).lower().endswith(SUFFIX)


def open_document(path: PathLike) -> Document:
    """A Document from a ``.mlz`` project or a ``.chf`` drawing."""
    if is_project_file(path):
        return load_project(path).document
    from ..formats.chf import load_chf
    return load_chf(str(path))


def export_chf(doc: Document, version: Optional[int] = None) -> bytes:
    """The ``.chf`` interchange bytes of ``doc`` (raster parts as their burn segments)."""
    from ..cam.raster_engraving import expand_document
    from ..formats.chf import dumps_chf
    d = expand_document(doc)
    return dumps_chf(d, version=d.version if version is None else version)
