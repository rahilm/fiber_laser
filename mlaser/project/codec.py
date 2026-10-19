"""JSON encoding of the document model for the ``.mlz`` project format (docs/dev/project-format.md).

Floats are written with ``repr`` (exact round trip, ``-0.0`` kept); NaN and infinities, tuples,
dicts with non-string keys, bytes and numpy arrays are tagged objects (``{"$tuple": [...]}``
...).  Segments are compact arrays: ``["L", x0, y0, x1, y1]`` and
``["A", x0, y0, x1, y1, cx, cy, ccw]``.  Model objects found inside ``extra`` (a contour kept
by the alpha-corner tool, fly-cut scan paths ...) and nest results are encoded as tagged
dataclasses; only the classes in :data:`DATACLASSES` are ever decoded (no pickle, nothing
imported by name from the file).

Opaque ``.chf`` round-trip records (``Part.extra['chf']``, ``['_chf_snapshot']``,
``Contour.extra['chf']``, ``['_chf_craft']``, ``Document.extra['chf']``) are not encoded here:
the project stores the ``.chf`` export of the document next to the JSON and grafts the records
back on load (:mod:`mlaser.project.format`).
"""
from __future__ import annotations

import base64
import dataclasses
import importlib
import io
import math
from typing import Any, Callable, Optional

import numpy as np

from ..model import (Arc, Contour, Craft, Document, Glyph, LayerParams, LeadParams, Line, MicroJoint,
                     Part, segment_key)

#: ``extra`` keys that the .chf graft restores (never written to the JSON)
CHF_PART_KEYS = ("chf", "_chf_snapshot")
CHF_CONTOUR_KEYS = ("chf", "_chf_craft")
CHF_DOC_KEYS = ("chf",)

#: tagged dataclasses that may be decoded: tag -> (module, class name)
DATACLASSES: dict[str, tuple[str, str]] = {
    "ScanPath": ("mlaser.cam.flycut", "ScanPath"),
    "NestJob": ("mlaser.nest.model", "NestJob"),
    "NestPart": ("mlaser.nest.model", "NestPart"),
    "NestSheet": ("mlaser.nest.model", "NestSheet"),
    "NestParams": ("mlaser.nest.model", "NestParams"),
    "NestPlacement": ("mlaser.nest.model", "Placement"),
    "SheetLayout": ("mlaser.nest.model", "SheetLayout"),
    "NestResult": ("mlaser.nest.model", "NestResult"),
}
_TAG_OF: dict[tuple[str, str], str] = {v: k for k, v in DATACLASSES.items()}

SMALL_BYTES = 4096   # bytes up to this size go inline (base64), larger ones into a blob


class Blobs:
    """Binary members of the archive (``blobs/<n>.<ext>``), written / read by the format."""

    def __init__(self, data: Optional[dict[str, bytes]] = None) -> None:
        self.data: dict[str, bytes] = dict(data or {})

    def put(self, payload: bytes, ext: str) -> str:
        name = f"blobs/{len(self.data):05d}.{ext}"
        self.data[name] = payload
        return name

    def get(self, name: str) -> bytes:
        try:
            return self.data[name]
        except KeyError:
            raise ValueError(f"missing blob {name}") from None


class Encoder:
    """Model -> JSON-safe values.  ``dropped`` lists the paths of values that could not be
    encoded (they are left out and reported in the manifest)."""

    def __init__(self, blobs: Blobs) -> None:
        self.blobs = blobs
        self.dropped: list[str] = []

    # -- generic values -------------------------------------------------------
    def value(self, v: Any, path: str) -> Any:
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, int) and not isinstance(v, bool):
            return int(v)
        if isinstance(v, float):
            return v if math.isfinite(v) else {"$float": repr(v)}
        if isinstance(v, np.generic):
            return self.value(v.item(), path)
        if isinstance(v, tuple) and not hasattr(v, "_fields"):
            return {"$tuple": [self.value(x, f"{path}[{i}]") for i, x in enumerate(v)]}
        if isinstance(v, list):
            return [self.value(x, f"{path}[{i}]") for i, x in enumerate(v)]
        if isinstance(v, dict):
            return self.mapping(v, path)
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            if len(b) <= SMALL_BYTES:
                return {"$b64": base64.b64encode(b).decode("ascii")}
            return {"$blob": self.blobs.put(b, "bin")}
        if isinstance(v, np.ndarray):
            return {"$npy": self.array(v)}
        if isinstance(v, (Line, Arc)):
            return {"$seg": self.segment(v, path)}
        if isinstance(v, Contour):
            return {"$Contour": self.contour(v, path)}
        if isinstance(v, Part):
            return {"$Part": self.part(v, path)}
        if isinstance(v, Craft):
            return {"$Craft": self.craft(v, path)}
        if isinstance(v, LeadParams):
            return {"$Lead": self.lead(v)}
        if isinstance(v, Glyph):
            return {"$Glyph": self.glyph(v, path)}
        if isinstance(v, MicroJoint):
            return {"$tuple": [float(v.position), float(v.width)]}
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            tag = _TAG_OF.get((type(v).__module__, type(v).__qualname__))
            if tag is not None:
                return {"$dc": tag, "f": {f.name: self.value(getattr(v, f.name), f"{path}.{f.name}")
                                          for f in dataclasses.fields(v)}}
        self.dropped.append(f"{path} ({type(v).__name__})")
        return _DROP

    def mapping(self, d: dict, path: str, skip: tuple = ()) -> Any:
        items = list(dict.items(d)) if isinstance(d, dict) else list(d.items())
        if all(isinstance(k, str) and not k.startswith("$") for k, _v in items):
            out = {}
            for k, v in items:
                if k in skip:
                    continue
                e = self.value(v, f"{path}.{k}")
                if e is not _DROP:
                    out[k] = e
            return out
        pairs = []
        for k, v in items:
            if k in skip:
                continue
            ek, ev = self.value(k, f"{path}<key>"), self.value(v, f"{path}[{k!r}]")
            if ek is not _DROP and ev is not _DROP:
                pairs.append([ek, ev])
        return {"$dict": pairs}

    def array(self, a: np.ndarray) -> str:
        buf = io.BytesIO()
        np.save(buf, np.ascontiguousarray(a), allow_pickle=False)
        return self.blobs.put(buf.getvalue(), "npy")

    # -- model ----------------------------------------------------------------
    def segment(self, s, path: str) -> list:
        pts = (s.start, s.end) if type(s) is Line else (s.start, s.end, s.center)
        flat: list = []
        for p in pts:
            if type(p) is not tuple or len(p) != 2 or not all(_plain_num(x) for x in p):
                return ["G", self.value(type(s).__name__, path),
                        [self.value(q, path) for q in pts] + ([bool(s.ccw)] if type(s) is Arc else [])]
            flat += [_num(p[0]), _num(p[1])]
        if type(s) is Line:
            return ["L", *flat]
        if type(s) is Arc:
            return ["A", *flat, bool(s.ccw)]
        raise TypeError(type(s).__name__)

    def lead(self, l: LeadParams) -> dict:
        return {"kind": int(l.kind), "angle_deg": _num(l.angle_deg), "length": _num(l.length),
                "radius": _num(l.radius), "at_corner": bool(l.at_corner)}

    def glyph(self, g: Glyph, path: str) -> dict:
        return {"kind": int(g.kind), "data": self.value(g.data, f"{path}.data"), "dir": int(g.dir)}

    def craft(self, c: Craft, path: str) -> dict:
        d = c.__dict__
        return {
            "layer": int(c.layer), "kerf_comp": self.value(c.kerf_comp, path),
            "kerf_width": self.value(c.kerf_width, path),
            "lead_in": self.lead(c.lead_in), "lead_out": self.lead(c.lead_out),
            "micro_joints": [[_num(float(j[0])), _num(float(j[1]))] if isinstance(j, (tuple, list))
                             else _num(float(j)) for j in c.micro_joints],
            "joints_auto": [int(i) for i in d.get("_mj_auto", ())],
            "joint_widths_chf": self.value(d.get("_mj_widths"), f"{path}.joint_widths_chf"),
            "joint_width": self.value(c.joint_width, path),
            "cool_points": [self.value(p, f"{path}.cool_points") for p in c.cool_points],
            "start_param": self.value(c.start_param, path),
            "overcut": self.value(c.overcut, path),
            "lead_error": bool(c.lead_error),
            "extra": self.mapping(c.extra, f"{path}.extra"),
        }

    def contour(self, c: Contour, path: str) -> dict:
        out: dict[str, Any] = {
            "closed": bool(c.closed),
            "segments": [self.segment(s, f"{path}.segments[{i}]") for i, s in enumerate(c.segments)],
        }
        if c.craft is not None:
            out["craft"] = self.craft(c.craft, f"{path}.craft")
        if c.glyphs is not None:
            out["glyphs"] = [self.glyph(g, f"{path}.glyphs[{i}]") for i, g in enumerate(c.glyphs)]
            out["glyphs_current"] = c.glyphs_current()
        ex = self.mapping(c.extra, f"{path}.extra", skip=CHF_CONTOUR_KEYS)
        if ex:
            out["extra"] = ex
        return out

    def part(self, p: Part, path: str) -> dict:
        out: dict[str, Any] = {
            "name": str(p.name), "selected": bool(p.selected),
            "craft": self.craft(p.craft, f"{path}.craft"),
            "contours": [self.contour(c, f"{path}.contours[{i}]") for i, c in enumerate(p.contours)],
        }
        if p.raster is not None:
            out["raster"] = self.raster(p.raster, f"{path}.raster")
        ex = self.mapping(p.extra, f"{path}.extra", skip=CHF_PART_KEYS)
        if ex:
            out["extra"] = ex
        return out

    def raster(self, r, path: str) -> Any:
        try:
            return {"x0": self.array(r.x0), "x1": self.array(r.x1), "row": self.array(r.row),
                    "row_y": self.array(r.row_y), "power": self.array(r.power),
                    "rho": self.value(r.rho, path), "overscan": self.value(r.overscan, path),
                    "direction": str(r.direction), "params": self.value(r.params, f"{path}.params"),
                    "frame": [_num(float(x)) for x in r.frame]}
        except AttributeError:
            self.dropped.append(f"{path} ({type(r).__name__})")
            return None

    def layer(self, lp: LayerParams, path: str) -> dict:
        out = {}
        for f in dataclasses.fields(lp):
            e = self.value(getattr(lp, f.name), f"{path}.{f.name}")
            if e is not _DROP:
                out[f.name] = e
        return out

    def document(self, doc: Document) -> dict:
        return {
            "version": int(doc.version),
            "layers": [self.layer(lp, f"layers[{k}]") | {"key": self.value(k, "layers")}
                       for k, lp in doc.layers.items()],
            "parts": [self.part(p, f"parts[{i}]") for i, p in enumerate(doc.parts)],
            "extra": self.mapping(doc.extra, "extra", skip=CHF_DOC_KEYS),
        }


_DROP = object()


def _plain_num(x) -> bool:
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, bool)


def _num(x):
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return {"$float": repr(x)}
    return x


# =========================================================================== decoding
class Decoder:
    def __init__(self, blobs: Blobs) -> None:
        self.blobs = blobs

    def value(self, v: Any) -> Any:
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if not isinstance(v, dict):
            return v
        if len(v) >= 1 and next(iter(v)).startswith("$"):
            return self.tagged(v)
        return {k: self.value(x) for k, x in v.items()}

    def tagged(self, v: dict) -> Any:
        if "$float" in v:
            return float(v["$float"])
        if "$tuple" in v:
            return tuple(self.value(x) for x in v["$tuple"])
        if "$dict" in v:
            return {_hashable(self.value(k)): self.value(x) for k, x in v["$dict"]}
        if "$b64" in v:
            return base64.b64decode(v["$b64"])
        if "$blob" in v:
            return self.blobs.get(v["$blob"])
        if "$npy" in v:
            return self.array(v["$npy"])
        if "$seg" in v:
            return self.segment(v["$seg"])
        if "$Contour" in v:
            return self.contour(v["$Contour"])
        if "$Part" in v:
            return self.part(v["$Part"])
        if "$Craft" in v:
            return self.craft(v["$Craft"])
        if "$Lead" in v:
            return self.lead(v["$Lead"])
        if "$Glyph" in v:
            return self.glyph(v["$Glyph"])
        if "$dc" in v:
            return self.dataclass(v["$dc"], v.get("f", {}))
        raise ValueError(f"unknown tag {next(iter(v))!r}")

    def array(self, name: str) -> np.ndarray:
        return np.load(io.BytesIO(self.blobs.get(name)), allow_pickle=False)

    def dataclass(self, tag: str, fields: dict) -> Any:
        if tag not in DATACLASSES:
            raise ValueError(f"unknown object type {tag!r}")
        mod, name = DATACLASSES[tag]
        cls = getattr(importlib.import_module(mod), name)
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: self.value(x) for k, x in fields.items() if k in names})

    # -- model ----------------------------------------------------------------
    def segment(self, s: list):
        t = s[0]
        if t == "L":
            return Line((_f(s[1]), _f(s[2])), (_f(s[3]), _f(s[4])))
        if t == "A":
            return Arc((_f(s[1]), _f(s[2])), (_f(s[3]), _f(s[4])), (_f(s[5]), _f(s[6])), bool(s[7]))
        if t == "G":
            kind, pts = self.value(s[1]), [self.value(p) for p in s[2]]
            if kind == "Line":
                return Line(pts[0], pts[1])
            if kind == "Arc":
                return Arc(pts[0], pts[1], pts[2], bool(pts[3]))
        raise ValueError(f"unknown segment {t!r}")

    def lead(self, d: dict) -> LeadParams:
        return LeadParams(kind=d["kind"], angle_deg=_f(d["angle_deg"]), length=_f(d["length"]),
                          radius=_f(d["radius"]), at_corner=bool(d["at_corner"]))

    def glyph(self, d: dict) -> Glyph:
        return Glyph(int(d["kind"]), self.value(d.get("data", {})), int(d.get("dir", 1)))

    def craft(self, d: dict) -> Craft:
        c = Craft(layer=d["layer"], kerf_comp=self.value(d["kerf_comp"]),
                  kerf_width=self.value(d["kerf_width"]),
                  lead_in=self.lead(d["lead_in"]), lead_out=self.lead(d["lead_out"]))
        c.extra = self.value(d.get("extra", {}))
        js = []
        for j in d.get("micro_joints", []):
            js.append(MicroJoint(_f(j[0]), _f(j[1])) if isinstance(j, list) else MicroJoint(_f(j)))
        dd = c.__dict__
        dd["micro_joints"] = js
        dd["_mj_auto"] = tuple(int(i) for i in d.get("joints_auto", []))
        raw = self.value(d.get("joint_widths_chf"))
        dd["_mj_widths"] = None if raw is None else list(raw)
        dd["joint_width"] = self.value(d.get("joint_width"))
        c.cool_points = [self.value(p) for p in d.get("cool_points", [])]
        c.start_param = self.value(d.get("start_param"))
        c.overcut = self.value(d.get("overcut"))
        c.lead_error = bool(d.get("lead_error", False))
        return c

    def contour(self, d: dict) -> Contour:
        c = Contour([self.segment(s) for s in d["segments"]], bool(d["closed"]))
        if d.get("extra"):
            c.extra = self.value(d["extra"])
        if d.get("craft") is not None:
            c.craft = self.craft(d["craft"])
        if d.get("glyphs") is not None:
            c.glyphs = [self.glyph(g) for g in d["glyphs"]]
            c.glyph_key = segment_key(c.segments) if d.get("glyphs_current") else ("stale",)
        return c

    def part(self, d: dict) -> Part:
        p = Part([self.contour(c) for c in d["contours"]], self.craft(d["craft"]),
                 name=d.get("name", ""), selected=bool(d.get("selected", False)))
        if d.get("extra"):
            p.extra = self.value(d["extra"])
        if d.get("raster") is not None:
            p.raster = self.raster(d["raster"])
        return p

    def raster(self, d: dict):
        from ..cam.raster_engraving import RasterEngraving
        return RasterEngraving(self.array(d["x0"]), self.array(d["x1"]), self.array(d["row"]),
                               self.array(d["row_y"]), self.array(d["power"]), rho=self.value(d.get("rho")),
                               overscan=self.value(d.get("overscan", 5.0)),
                               direction=d.get("direction", "serpentine"),
                               params=self.value(d.get("params", {})),
                               frame=tuple(_f(x) for x in d["frame"]))

    def layer(self, d: dict) -> LayerParams:
        names = {f.name for f in dataclasses.fields(LayerParams)}
        return LayerParams(**{k: self.value(v) for k, v in d.items() if k in names})

    def document(self, d: dict) -> Document:
        layers = {}
        for ld in d.get("layers", []):
            layers[_hashable(self.value(ld.get("key", ld.get("index"))))] = self.layer(ld)
        return Document(parts=[self.part(p) for p in d.get("parts", [])], layers=layers,
                        version=int(d.get("version", 9)), extra=self.value(d.get("extra", {})))


def _f(x):
    if isinstance(x, dict) and "$float" in x:
        return float(x["$float"])
    return x


def _hashable(k):
    return tuple(_hashable(x) for x in k) if isinstance(k, list) else k


Hook = Callable[[Any], Any]
