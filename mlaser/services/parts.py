"""Part recognition: group the contours of a drawing into real parts (Qt-free).

An imported DXF has one :class:`~mlaser.model.Part` per contour (the original's graphs), so a
gear and its bore are two objects and "nest the drawing" would nest the bore as a separate
disc.  :func:`recognize` groups them into :class:`PartGroup` s: an outer profile with its holes
and internal features (etch lines, text, slots), then merges identical parts into one group
with a quantity.  It is an explicit step after the import; the import itself (the
DxfParse-faithful port and its golden outputs) is not changed, and neither is the document:
a group only lists document part indices.

Rules (:class:`RecognitionRules`):

* ``mode``:
  - ``"nested"`` (default): containment.  A closed contour that is not inside another one is a
    part's outer profile; everything inside it (holes, islands, open contours, features) belongs
    to that part.  With ``islands="parts"`` an island inside a hole is a part of its own (part in
    part, even-odd rule) instead.
  - ``"layer"``: one part per DXF layer (customers who draw each part on its own layer).
  - ``"block"``: one part per INSERT (block reference) of the DXF file; contours outside every
    block fall back to containment.  Identical blocks are identical parts.
  - ``"file"``: the whole drawing is one part (the old *Import file* behaviour).
  - ``"none"``: every document part is a part.
* ``feature_layers``: contours on these layers (glob patterns, case-insensitive; ETCH, MARK,
  ENGRAVE ...) are never a part's outer profile; they stay with the part that contains them.
* ``ignore_layers``: contours on these layers (BEND, DIM, DEFPOINTS ...) are left out.
* ``merge_identical``: parts with the same geometry up to a rotation (not a mirror image: left
  and right brackets stay apart) become one group with a quantity.

Open contours that lie in no part are not parts: they are listed in :attr:`PartSet.loose`
(repair them first, see :mod:`mlaser.services.repair`).

:class:`PartSet` supports :meth:`~PartSet.merge` and :meth:`~PartSet.split` (the review list in
the UI) and :meth:`~PartSet.nest_parts` (one :class:`~mlaser.nest.model.NestPart` per group with
the geometry of its first copy).
"""
from __future__ import annotations

import copy
import fnmatch
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from mlaser.model import Contour, Document, Part

MODES = ("nested", "layer", "block", "file", "none")
FEATURE_LAYERS = ("ETCH*", "MARK*", "ENGRAV*", "SCRIBE*", "TEXT*")
IGNORE_LAYERS = ("BEND*", "DIM*", "DEFPOINTS", "CONSTRUCT*", "CENTER*", "HIDDEN*", "TITLE*",
                 "FRAME*", "BORDER*")

__all__ = ["RecognitionRules", "PartGroup", "PartSet", "recognize", "MODES", "FEATURE_LAYERS",
           "IGNORE_LAYERS", "group_signature", "same_shape"]


@dataclass
class RecognitionRules:
    mode: str = "nested"
    feature_layers: tuple[str, ...] = FEATURE_LAYERS
    ignore_layers: tuple[str, ...] = IGNORE_LAYERS
    merge_identical: bool = True
    islands: str = "keep"              # keep (stay in the part) | parts (part in part)
    tolerance: float = 0.05            # mm, for "identical"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown recognition mode {self.mode!r} (one of {', '.join(MODES)})")
        if self.islands not in ("keep", "parts"):
            raise ValueError("islands must be 'keep' or 'parts'")

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RecognitionRules":
        d = dict(d or {})
        kw = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for k in ("feature_layers", "ignore_layers"):
            if k in kw:
                v = kw[k]
                kw[k] = tuple(x.strip() for x in (v.split(",") if isinstance(v, str) else v) if str(x).strip())
        return cls(**kw)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "feature_layers": list(self.feature_layers),
                "ignore_layers": list(self.ignore_layers), "merge_identical": self.merge_identical,
                "islands": self.islands, "tolerance": self.tolerance}


def _match(layer: str, patterns: Iterable[str]) -> bool:
    name = (layer or "").upper()
    return bool(name) and any(fnmatch.fnmatchcase(name, p.upper()) for p in patterns)


def part_layer(p: Part) -> str:
    """The DXF layer a document part came from ("" when unknown)."""
    v = p.extra.get("layer") if isinstance(p.extra, dict) or hasattr(p.extra, "get") else None
    return str(v) if v is not None else ""


# ============================================================================== groups
@dataclass
class PartGroup:
    """One recognised part: ``copies`` lists the document part indices of each copy found in
    the drawing (the first copy's geometry is the one nested)."""

    name: str
    copies: list[list[int]]
    kind: str = "part"                 # part | open (loose open geometry)
    block: str = ""                    # block name (mode "block")
    layer: str = ""
    area: float = 0.0                  # net area of one copy (mm²)
    perimeter: float = 0.0             # all contours of one copy (mm)
    holes: int = 0
    open_contours: int = 0
    size: tuple[float, float] = (0.0, 0.0)
    signature: tuple = ()

    @property
    def quantity(self) -> int:
        return len(self.copies)

    @property
    def members(self) -> list[int]:
        return list(self.copies[0]) if self.copies else []

    def all_members(self) -> list[int]:
        return sorted({i for c in self.copies for i in c})

    def label(self) -> str:
        w, h = self.size
        bits = [f"{w:.0f} × {h:.0f} mm"]
        if self.holes:
            bits.append(f"{self.holes} hole{'s' if self.holes != 1 else ''}")
        if self.open_contours:
            bits.append(f"{self.open_contours} open")
        return f"{self.name}  ×{self.quantity}  ({', '.join(bits)})"


@dataclass
class PartSet:
    """The result of :func:`recognize` for one document."""

    groups: list[PartGroup] = field(default_factory=list)
    loose: list[int] = field(default_factory=list)       # open geometry in no part (not nested)
    ignored: list[int] = field(default_factory=list)     # on an ignored layer
    base: str = ""                                        # name stem (the file name)
    rules: RecognitionRules = field(default_factory=RecognitionRules)
    _doc: Optional[Document] = field(default=None, repr=False, compare=False)

    @property
    def count(self) -> int:
        """Parts in the drawing (sum of the quantities)."""
        return sum(g.quantity for g in self.groups)

    def summary(self) -> str:
        n, k = self.count, len(self.groups)
        s = f"{n} part{'s' if n != 1 else ''}"
        if k != n:
            s += f" ({k} different)"
        if self.loose:
            s += f", {len(self.loose)} open contour(s) in no part"
        if self.ignored:
            s += f", {len(self.ignored)} ignored"
        return s

    # ------------------------------------------------------------------ edits
    def merge(self, indices: Sequence[int]) -> PartGroup:
        """Make the groups ``indices`` one part (one copy holding every member of every copy):
        "these contours belong together"."""
        idx = sorted({int(i) for i in indices if 0 <= int(i) < len(self.groups)})
        if not idx:
            raise ValueError("nothing to merge")
        sel = [self.groups[i] for i in idx]
        members = sorted({m for g in sel for m in g.all_members()})
        g = PartGroup(sel[0].name, [members], block=sel[0].block, layer=sel[0].layer)
        if self._doc is not None:
            _measure(self._doc, g, self.rules.tolerance)
        for i in reversed(idx):
            del self.groups[i]
        self.groups.insert(idx[0], g)
        self.loose = [i for i in self.loose if i not in members]
        return g

    def split(self, index: int) -> list[PartGroup]:
        """Undo a grouping: a group of identical copies becomes one group per copy; a single
        part becomes one group per document part."""
        g = self.groups[index]
        if g.quantity > 1:
            new = [PartGroup(f"{g.name}.{k + 1}", [list(c)], g.kind, g.block, g.layer)
                   for k, c in enumerate(g.copies)]
        elif len(g.members) > 1:
            new = [PartGroup(f"{g.name}.{k + 1}", [[m]], g.kind, g.block, g.layer)
                   for k, m in enumerate(g.members)]
        else:
            return [g]
        if self._doc is not None:
            for n in new:
                _measure(self._doc, n, self.rules.tolerance)
        self.groups[index:index + 1] = new
        return new

    def rename(self, index: int, name: str) -> None:
        self.groups[index].name = name.strip() or self.groups[index].name

    # ------------------------------------------------------------------ nesting
    def nest_parts(self, doc: Optional[Document] = None, multiplier: int = 1,
                   extra: Optional[dict] = None, include_loose: bool = False) -> list:
        """One NestPart per group: the first copy's document parts (deep copies, unselected),
        quantity = copies × ``multiplier``.  ``extra`` is copied into every NestPart's extra
        (order references)."""
        from mlaser.nest.model import NestPart
        doc = doc or self._doc
        if doc is None:
            raise ValueError("nest_parts needs the document")
        out = []
        groups = list(self.groups)
        if include_loose and self.loose:
            groups.append(PartGroup(f"{self.base or 'part'}-open", [list(self.loose)], "open"))
        for g in groups:
            geo = []
            for i in g.members:
                q = copy.deepcopy(doc.parts[i])
                q.selected = False
                geo.append(q)
            if not geo:
                continue
            ex = {"recognized": {"members": len(g.members), "copies": g.quantity, "kind": g.kind,
                                 "area_mm2": round(g.area, 3), "holes": g.holes}}
            if g.block:
                ex["recognized"]["block"] = g.block
            ex.update(copy.deepcopy(extra or {}))
            out.append(NestPart(geo, name=g.name, quantity=max(0, g.quantity * int(multiplier)), extra=ex))
        return out


# ============================================================================== geometry
def _contours(doc: Document, members: Iterable[int]) -> list[Contour]:
    return [c for i in members for c in doc.parts[i].contours if c.segments]


def _measure(doc: Document, g: PartGroup, tol: float) -> None:
    from mlaser.nest.shapes import parts_geometry
    parts = [doc.parts[i] for i in g.members]
    cs = _contours(doc, g.members)
    g.open_contours = sum(1 for c in cs if not c.closed)
    g.perimeter = sum(c.length() for c in cs)
    try:
        geo = parts_geometry([Part([c]) for p in parts for c in p.contours if c.closed and c.segments])
        g.area = float(geo.area)
        x0, y0, x1, y1 = geo.bounds if not geo.is_empty else (0, 0, 0, 0)
        if geo.is_empty:
            raise ValueError
        g.size = (x1 - x0, y1 - y0)
        g.holes = sum(len(p.interiors) for p in getattr(geo, "geoms", [geo]))
    except Exception:  # noqa: BLE001 - open geometry only
        bbs = [p.bbox() for p in parts if p.contours]
        if bbs:
            g.size = (max(b[2] for b in bbs) - min(b[0] for b in bbs), max(b[3] for b in bbs) - min(b[1] for b in bbs))
        g.area, g.holes = 0.0, 0
    g.signature = group_signature(doc, g, tol)


def _shape(doc: Document, members: Sequence[int]):
    """The closed contours of a copy as one shapely geometry (even-odd), plus the open ones as
    thin strips (so two parts that differ only in an etch line differ)."""
    from mlaser.nest.shapes import parts_geometry
    return parts_geometry([doc.parts[i] for i in members])


def _moments(geo) -> tuple[float, float, float, float, float]:
    """Centroid (x, y), principal second moments (i1 >= i2) and the principal axis angle (rad)
    of a polygon set (holes subtract)."""
    from shapely.geometry import Polygon
    from shapely.geometry.polygon import orient
    A = float(geo.area)
    if A <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    c = geo.centroid
    cx, cy = float(c.x), float(c.y)
    ixx = iyy = ixy = 0.0
    for p in getattr(geo, "geoms", [geo]):
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        p = orient(p, 1.0)                 # exterior CCW, holes CW: signed sums subtract holes
        for ring in [p.exterior, *p.interiors]:
            pts = list(ring.coords)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                x0 -= cx
                x1 -= cx
                y0 -= cy
                y1 -= cy
                cr = x0 * y1 - x1 * y0
                ixx += (y0 * y0 + y0 * y1 + y1 * y1) * cr / 12.0
                iyy += (x0 * x0 + x0 * x1 + x1 * x1) * cr / 12.0
                ixy += (x0 * y1 + 2 * x0 * y0 + 2 * x1 * y1 + x1 * y0) * cr / 24.0
    t = (ixx + iyy) / 2
    d = math.sqrt(max(0.0, ((ixx - iyy) / 2) ** 2 + ixy * ixy))
    ang = 0.5 * math.atan2(2 * ixy, ixx - iyy) if d > 1e-12 * max(t, 1e-12) else 0.0
    return cx, cy, t + d, t - d, ang


def group_signature(doc: Document, g: PartGroup, tol: float = 0.05) -> tuple:
    """Discrete fingerprint of one copy (closed and open contour counts) plus the continuous
    values compared with a tolerance by :func:`_similar`: net area, total contour length and
    the principal moments."""
    cs = _contours(doc, g.members)
    n_closed = sum(1 for c in cs if c.closed)
    try:
        geo = _shape(doc, g.members)
        _cx, _cy, i1, i2, _a = _moments(geo)
        area = float(geo.area)
    except Exception:  # noqa: BLE001
        i1 = i2 = area = 0.0
    return (n_closed, len(cs) - n_closed, area, sum(c.length() for c in cs), i1, i2)


def _similar(a: tuple, b: tuple, tol: float) -> bool:
    if len(a) != 6 or len(b) != 6 or a[:2] != b[:2]:
        return False
    per = max(a[3], b[3], 1e-9)
    if abs(a[2] - b[2]) > tol * per + 1e-9:
        return False
    if abs(a[3] - b[3]) > max(0.01 * per, 20 * tol):
        return False
    for i in (4, 5):
        if abs(a[i] - b[i]) > 0.02 * max(abs(a[i]), abs(b[i]), 1e-9) + 1e-9:
            return False
    return True


def _far_angles(ga, ca: tuple, gb, tol: float, limit: int = 96) -> list[float]:
    def verts(g) -> list[tuple[float, float]]:
        out = []
        for p in getattr(g, "geoms", [g]):
            ext = getattr(p, "exterior", None)
            if ext is not None:
                out.extend(ext.coords)
        return out
    va, vb = verts(ga), verts(gb)
    if not va or not vb:
        return [0.0]
    da = [math.dist(v, ca) for v in va]
    db = [math.dist(v, ca) for v in vb]
    ra, rb = max(da), max(db)
    fb = vb[db.index(rb)]
    angb = math.degrees(math.atan2(fb[1] - ca[1], fb[0] - ca[0]))
    out: list[float] = []
    for v, d in zip(va, da):
        if d >= ra - max(2 * tol, 1e-6):
            a = (math.degrees(math.atan2(v[1] - ca[1], v[0] - ca[0])) - angb) % 360.0
            if all(abs(a - o) > 0.05 for o in out):
                out.append(a)
            if len(out) >= limit:
                break
    return out or [0.0]


def same_shape(doc: Document, a: Sequence[int], b: Sequence[int], tol: float = 0.05) -> bool:
    """True when copy ``b`` is copy ``a`` moved and rotated (not mirrored), within ``tol`` mm:
    the symmetric difference after aligning centroids and principal axes is below
    ``tol × perimeter``."""
    from shapely import affinity
    ga, gb = _shape(doc, a), _shape(doc, b)
    if ga.is_empty or gb.is_empty:
        return False
    per = max(ga.length, 1e-9)
    limit = tol * per + 1e-9
    if abs(ga.area - gb.area) > limit:
        return False
    ax, ay, a1, a2, aa = _moments(ga)
    bx, by, b1, b2, ba = _moments(gb)
    gb0 = affinity.translate(gb, ax - bx, ay - by)
    if abs(a1 - a2) <= 1e-3 * max(a1, 1e-9):
        # nearly isotropic (squares, gears, circles): align B's farthest vertex with each of
        # A's farthest vertices (one of them matches when the shapes are the same)
        cands = _far_angles(ga, (ax, ay), gb0, tol)
    else:
        cands = []
        for base in (math.degrees(aa - ba), math.degrees(ba - aa)):   # either axis convention
            cands += [base + k * 90.0 + d for k in range(4) for d in (0.0, -0.5, 0.5)]
    for deg in cands:
        gr = affinity.rotate(gb0, deg, origin=(ax, ay))
        try:
            if ga.symmetric_difference(gr).area <= limit:
                return True
        except Exception:  # noqa: BLE001 - GEOS topology error: not the same
            continue
    return False


# ============================================================================== recognise
def _base_name(doc: Document, base: str) -> str:
    if base:
        return base
    src = doc.extra.get("source") if isinstance(doc.extra, dict) else None
    if src:
        return os.path.splitext(os.path.basename(str(src)))[0]
    return "part"


def recognize(doc: Document, rules: Optional[RecognitionRules] = None, base: str = "",
              indices: Optional[Iterable[int]] = None) -> PartSet:
    """Group the document parts ``indices`` (default: all with geometry) into parts (see the
    module docstring)."""
    rules = rules or RecognitionRules()
    base = _base_name(doc, base)
    sel = sorted(set(indices)) if indices is not None else list(range(len(doc.parts)))
    units = [i for i in sel if 0 <= i < len(doc.parts) and any(c.segments for c in doc.parts[i].contours)]
    ps = PartSet(base=base, rules=rules, _doc=doc)
    ps.ignored = [i for i in units if _match(part_layer(doc.parts[i]), rules.ignore_layers)]
    units = [i for i in units if i not in set(ps.ignored)]
    feature = {i for i in units if _match(part_layer(doc.parts[i]), rules.feature_layers)}
    raw: list[PartGroup] = []
    if rules.mode == "file":
        if units:
            raw.append(PartGroup(base, [list(units)]))
    elif rules.mode == "none":
        raw = [PartGroup(doc.parts[i].name or f"{base}-{k + 1}", [[i]]) for k, i in enumerate(units)]
    elif rules.mode == "layer":
        by: dict[str, list[int]] = {}
        for i in units:
            by.setdefault(part_layer(doc.parts[i]), []).append(i)
        heads = {lay: [i for i in idx if i not in feature] for lay, idx in by.items()}
        loose_f = []
        for lay, idx in by.items():
            if heads[lay]:
                raw.append(PartGroup(lay or base, [idx], layer=lay))
            else:
                loose_f.extend(idx)
        if loose_f:                    # a pure feature layer: its contours go with their part
            sub, lo = _nested(doc, [i for g in raw for i in g.members] + loose_f, feature, rules)
            owner = {m: k for k, g in enumerate(sub) for m in g.members}
            for f in loose_f:
                tgt = owner.get(f)
                hit = None
                if tgt is not None:
                    head = sub[tgt].members
                    hit = next((g for g in raw if set(head) & set(g.members) - {f}), None)
                if hit is not None:
                    hit.copies[0].append(f)
                else:
                    ps.loose.append(f)
    elif rules.mode == "block":
        raw, rest = _blocks(doc, units)
        sub, lo = _nested(doc, rest, feature, rules)
        raw += sub
        ps.loose.extend(lo)
    else:
        raw, lo = _nested(doc, units, feature, rules)
        ps.loose.extend(lo)
    # loose groups made of open contours only are not parts
    groups = []
    for g in raw:
        cs = _contours(doc, g.members)
        if cs and not any(c.closed for c in cs):
            ps.loose.extend(g.members)
            continue
        g.copies[0] = sorted(g.copies[0])
        _measure(doc, g, rules.tolerance)
        groups.append(g)
    ps.loose = sorted(set(ps.loose))
    if rules.merge_identical:
        groups = _merge_identical(doc, groups, rules.tolerance)
    # names: <file>-<n> (the block name in block mode), a single part takes the file name
    if rules.mode == "nested":
        if len(groups) == 1:
            groups[0].name = base
        else:
            for k, g in enumerate(groups):
                g.name = f"{base}-{k + 1}"
    elif rules.mode == "block":
        n = 0
        for g in groups:
            if not g.block:
                n += 1
                g.name = f"{base}-{n}"
    ps.groups = groups
    return ps


def _k(n: Any) -> int:
    return int(n.key)


def _nested(doc: Document, units: Sequence[int], feature: set, rules: RecognitionRules
            ) -> tuple[list[PartGroup], list[int]]:
    """Containment grouping of ``units`` (document part indices)."""
    from mlaser.cam.ordering import build_tree
    items: list[tuple[int, Contour]] = [(i, c) for i in units for c in doc.parts[i].contours if c.segments]
    if not items:
        return [], []
    nodes = build_tree([c for _i, c in items], list(range(len(items))))
    owner = [i for i, _c in items]
    # anchor of a unit: its closed contour with the largest bbox (else its first contour)
    anchor: dict[int, Any] = {}
    for n in nodes:
        u = owner[_k(n)]
        cur = anchor.get(u)
        if cur is None or (n.contour.closed, n.area) > (cur.contour.closed, cur.area):
            anchor[u] = n

    def chain(n) -> list:
        """Closed non-feature ancestors of node n owned by another unit, nearest first."""
        out = []
        p = n.parent
        u = owner[_k(n)]
        while p is not None:
            pu = owner[_k(p)]
            if p.contour.closed and pu not in feature and pu != u:
                out.append(p)
            p = p.parent
        return out

    target: dict[int, Optional[int]] = {}
    for u in units:
        a = anchor.get(u)
        if a is None:
            target[u] = None
            continue
        ch = chain(a)
        head_ok = u not in feature and a.contour.closed
        if not ch:
            target[u] = u if head_ok else None                  # None: loose
        elif rules.islands == "parts" and head_ok and len(ch) % 2 == 0:
            target[u] = u                                       # island in a hole: own part
        elif rules.islands == "parts":
            target[u] = owner[_k(ch[0])]
        else:
            target[u] = owner[_k(ch[-1])]

    def resolve(u: int, seen: set) -> Optional[int]:
        t = target.get(u)
        if t is None or t == u:
            return t
        if t in seen:
            return u
        seen.add(t)
        return resolve(t, seen)

    groups: dict[int, list[int]] = {}
    loose: list[int] = []
    for u in units:
        if u not in anchor:
            continue
        h = resolve(u, {u})
        if h is None:
            loose.append(u)
        else:
            groups.setdefault(h, []).append(u)
    out = [PartGroup(doc.parts[h].name, [sorted(m)], layer=part_layer(doc.parts[h]))
           for h, m in sorted(groups.items())]
    return out, loose


def _blocks(doc: Document, units: Sequence[int]) -> tuple[list[PartGroup], list[int]]:
    """Group units by the INSERT whose box contains them (smallest box first)."""
    src = doc.extra.get("source") if isinstance(doc.extra, dict) else None
    if not src or not str(src).lower().endswith(".dxf") or not os.path.exists(str(src)):
        return [], list(units)
    from mlaser.io.dxf_blocks import insert_boxes
    off = doc.extra.get("placement_offset") or (0.0, 0.0)
    try:
        boxes = insert_boxes(str(src), (float(off[0]), float(off[1])))
    except Exception:  # noqa: BLE001 - an unreadable file: containment only
        return [], list(units)
    boxes.sort(key=lambda b: (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]))
    g = 1e-6
    by: dict[int, list[int]] = {}
    rest = []
    for u in units:
        bb = doc.parts[u].bbox()
        hit = None
        for k, b in enumerate(boxes):
            x0, y0, x1, y1 = b.bbox
            tol = g + 1e-6 * max(abs(x0), abs(x1), abs(y0), abs(y1), 1.0)
            if bb[0] >= x0 - tol and bb[1] >= y0 - tol and bb[2] <= x1 + tol and bb[3] <= y1 + tol:
                hit = k
                break
        if hit is None:
            rest.append(u)
        else:
            by.setdefault(hit, []).append(u)
    out = [PartGroup(boxes[k].block, [sorted(m)], block=boxes[k].block, layer=boxes[k].layer)
           for k, m in sorted(by.items())]
    return out, rest


def _merge_identical(doc: Document, groups: list[PartGroup], tol: float) -> list[PartGroup]:
    out: list[PartGroup] = []
    for g in groups:
        hit = None
        for h in out:
            if h.kind != g.kind or not _similar(h.signature, g.signature, tol):
                continue
            if g.block and h.block and g.block == h.block:
                hit = h
                break
            if same_shape(doc, h.members, g.members, tol):
                hit = h
                break
        if hit is None:
            out.append(g)
        else:
            hit.copies.extend(g.copies)
    return out
