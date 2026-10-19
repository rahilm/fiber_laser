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

from ..model import Document
from . import migrations as M
from .codec import CHF_CONTOUR_KEYS, CHF_DOC_KEYS, CHF_PART_KEYS, Blobs, Decoder, Encoder
from .schema import DOCUMENT_SCHEMA, MANIFEST_SCHEMA, SECTION_SCHEMAS, validate

FORMAT = "mlaser-project"
VERSION = M.CURRENT
MIMETYPE = b"application/vnd.mlaser.project+zip"
SUFFIX = ".mlz"
MAX_UNCOMPRESSED = 4 * 2**30          # refuse archives that claim more (zip bombs)
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
def _read_members(data: bytes, name: str) -> dict[str, bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        if data[:4] != b"PK\x03\x04":
            raise ProjectError(f"{name}: not an mlaser project (.mlz) file") from e
        raise ProjectError(f"{name}: the file is damaged (the zip structure is broken: {e})") from e
    infos = z.infolist()
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise ProjectError(f"{name}: the archive claims more than {MAX_UNCOMPRESSED >> 30} GB of content")
    out = {}
    for i in infos:
        if i.is_dir():
            continue
        if i.filename.startswith("/") or ".." in i.filename.split("/"):
            raise ProjectError(f"{name}: bad member name {i.filename!r}")
        try:
            out[i.filename] = z.read(i)
        except (zipfile.BadZipFile, EOFError, OSError, ValueError) as e:     # bad CRC, truncated
            raise ProjectError(f"{name}: member {i.filename} is damaged ({e})") from e
        except Exception as e:  # noqa: BLE001 - zlib.error and friends
            raise ProjectError(f"{name}: member {i.filename} is damaged ({type(e).__name__}: {e})") from e
    return out


def _load_json(members: dict[str, bytes], member: str, name: str) -> Any:
    try:
        return json.loads(members[member].decode("utf-8"))
    except KeyError:
        raise ProjectError(f"{name}: {member} is missing") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProjectError(f"{name}: {member} is not valid JSON ({e})") from e


def loads_project(data: bytes, name: str = "<project>") -> Project:
    """Read ``.mlz`` bytes (see the module docstring for the checks)."""
    members = _read_members(data, name)
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


def load_project(path: PathLike) -> Project:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise ProjectError(f"{p}: cannot read the file ({e.strerror or e})") from e
    return loads_project(data, str(p))


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
