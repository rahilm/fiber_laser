"""Core document model shared by every subsystem.

Units are millimetres, angles radians unless a field says otherwise.
Geometry is stored as contours of Line/Arc segments; arcs use start/end/centre
plus direction so they round-trip both the .chf format and G-code (G2/G3).
Subsystems may extend these classes (add fields with defaults) but must not
rename or remove existing fields without updating every user.

Typed fields and ``extra`` (Wave 3, docs/dev/project-format.md)
---------------------------------------------------------------
Data that carries meaning lives in typed, documented fields:

=========================  ==============================================================
``Contour.craft``          per-contour craft override of a multi-contour part (or None)
``Contour.glyphs``         source glyphs (:class:`Glyph`) the segments were made from;
                           :meth:`Contour.source_glyphs` returns them only while the
                           segments are unchanged (``glyph_key`` is their fingerprint)
``Part.raster``            the raster engraving of a raster part
                           (:class:`mlaser.cam.raster_engraving.RasterEngraving`)
``Craft.micro_joints``     list of :class:`MicroJoint` (position mm, width mm)
``Craft.joint_width``      default width of joints given without one (None: 0.5 mm)
``Craft.cool_points``      the one cooling-point list (mm of arc length; a legacy (x, y)
                           point is accepted and projected onto the contour)
``Craft.start_param``      start as a fraction 0..1 of the contour length (None: unset)
``Craft.overcut``          signed overcut (+) / gap (-) in mm (None: unset)
``Craft.lead_error``       the lead-check error mark
=========================  ==============================================================

``extra`` keeps opaque round-trip data (the ``.chf`` record tree, snapshots, importer notes,
see :data:`EXTRA_KEYS`).  The old ``extra`` keys of the typed fields (``Contour.extra['craft']``,
``['glyphs']``, ``['glyph_segments']``, ``Part.extra['raster']``, ``Craft.extra['cool_points']``,
``['micro_joint_widths']``, ``['micro_joint_len']``, ``['start_param']``, ``['overcut']``,
``['lead_error']``) still work: ``extra`` is a :class:`LegacyExtra` view that reads and writes
them through the typed fields and issues :class:`LegacyExtraWarning` (a DeprecationWarning;
not for the parity ports, whose frozen code keeps using the old keys).
"""
from __future__ import annotations

import copy
import itertools
import math
import sys
import warnings
import weakref
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterable, Iterator, NamedTuple, Optional, Union

Point = tuple[float, float]

# Stable identities (Wave 2 services, docs/dev/ARCHITECTURE.md "services"): every Part and Contour
# gets a process-unique ``uid`` when it is created.  ``copy.deepcopy`` keeps it (an undo snapshot
# is the same object in an earlier state); a copy that becomes a *second* object in a document
# (paste, array) gets a fresh one from :func:`ensure_unique_ids`.  ``uid`` takes no part in
# equality or repr, so comparisons and file formats are unchanged.
_ids = itertools.count(1)


def new_id() -> int:
    """A new process-unique object id (thread safe: ``next`` on a count is atomic)."""
    return next(_ids)


@dataclass
class Line:
    start: Point
    end: Point

    def length(self) -> float:
        return math.dist(self.start, self.end)

    def reversed(self) -> "Line":
        return Line(self.end, self.start)


@dataclass
class Arc:
    start: Point
    end: Point
    center: Point
    ccw: bool = True

    @property
    def radius(self) -> float:
        return math.dist(self.center, self.start)

    def sweep(self) -> float:
        """Signed sweep angle (positive = CCW). Full circle when start == end."""
        a0 = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        a1 = math.atan2(self.end[1] - self.center[1], self.end[0] - self.center[0])
        d = a1 - a0
        if self.ccw:
            while d <= 1e-12:
                d += 2 * math.pi
        else:
            while d >= -1e-12:
                d -= 2 * math.pi
        return d

    def length(self) -> float:
        return abs(self.sweep()) * self.radius

    def reversed(self) -> "Arc":
        return Arc(self.end, self.start, self.center, not self.ccw)

    def point_at(self, t: float) -> Point:
        a0 = math.atan2(self.start[1] - self.center[1], self.start[0] - self.center[0])
        a = a0 + self.sweep() * t
        r = self.radius
        return (self.center[0] + r * math.cos(a), self.center[1] + r * math.sin(a))


Segment = Union[Line, Arc]


class GlyphKind(IntEnum):
    """Source glyph kinds (IEditableGlyph type +8, CADModule glyph factory FUN_10062860;
    docs/re/cam.md §0).  The values are the .chf ``####Gly`` type numbers."""
    POINT = 1
    LINE = 2            # "segment"
    ARC = 3
    CIRCLE = 4
    ELLIPSE = 5         # ellipse arc
    LWPOLY = 6          # lwpolyline with bulges
    SPLINE = 7


GLYPH_POINT, GLYPH_LINE, GLYPH_ARC, GLYPH_CIRCLE, GLYPH_ELLIPSE, GLYPH_LWPOLY, GLYPH_SPLINE = GlyphKind


@dataclass
class Glyph:
    """One source primitive as the original's CAD kernel keeps it (a ``####Gly`` record in .chf).

    ``Contour.segments`` stays the flattened Line/Arc geometry every subsystem uses; a contour
    may additionally carry the glyphs it was made from (``Contour.glyphs``, see
    :meth:`Contour.set_source_glyphs`) so that writers can reproduce the original primitive kinds
    (one lwpolyline with bulges, a spline, a circle) instead of the flattened pieces.

    ``data`` uses the .chf field names (docs/re/formats.md §2.8): point ``p``; line ``start``/``end``;
    arc ``center``/``radius``/``a0``/``a1`` (radians, CCW from a0 to a1); circle ``center``/``radius``
    (starts at angle 0); ellipse ``center``/``axis``/``ratio``/``t0``/``t1``; lwpoly ``flag``
    (1 = closed) and ``verts`` = [((x, y), bulge)]; spline ``ctrl``/``knots`` (cubic,
    len(knots) == len(ctrl) + 4) and the two opaque ints ``u0``/``u1``.
    ``dir`` is -1 when the contour traverses the glyph backwards.
    """
    kind: int
    data: dict = field(default_factory=dict)
    dir: int = 1


@dataclass
class LeadParams:
    """Lead-in / lead-out ("GuideCurve Para" in .chf)."""
    kind: int = 0            # 0 none, 1 line, 2 arc, 3 line+arc (see docs/re/cam.md)
    angle_deg: float = 90.0
    length: float = 2.0
    radius: float = 1.0
    at_corner: bool = False


DEFAULT_JOINT_WIDTH = 0.5   # mm, the micro-joint length of the original's dialog (cam.microjoint)


class MicroJoint(NamedTuple):
    """One micro-joint (bridge): centre ``position`` in mm of arc length from the contour's first
    point (the stored start, not the cut start) and ``width`` in mm.  A tuple, so the old
    ``(pos, len)`` pairs and the code that indexes them keep working."""
    position: float
    width: float = DEFAULT_JOINT_WIDTH


CoolPoint = Union[float, Point]
"""A cooling point: mm of arc length from the contour's first point.  A legacy (x, y) point is
accepted and projected onto the contour (``cam.toolpath``)."""


class LegacyExtraWarning(DeprecationWarning):
    """An ``extra`` key that became a typed field was used (see the module docstring)."""


# --------------------------------------------------------------------------- legacy extra view
@dataclass(frozen=True)
class _LegacyKey:
    attr: str                                   # the typed field, for the warning text
    present: Callable[[Any], bool]
    get: Callable[[Any], Any]
    set: Callable[[Any, Any], None]
    delete: Callable[[Any], None]


_HERE = __name__


def _warn_legacy(owner: Any, key: str, spec: _LegacyKey) -> None:
    f = sys._getframe(2)
    while f is not None and f.f_globals.get("__name__") == _HERE:
        f = f.f_back                             # type: ignore[assignment]
    if f is None:
        return
    mod = f.f_globals.get("__name__", "") or ""
    if mod.startswith("mlaser.parity_ports"):
        return                                   # the frozen ports keep the old keys
    warnings.warn_explicit(
        f"{type(owner).__name__}.extra[{key!r}] is deprecated: use {type(owner).__name__}.{spec.attr}",
        LegacyExtraWarning, f.f_code.co_filename, f.f_lineno, module=mod,
        registry=f.f_globals.setdefault("__warningregistry__", {}))


class LegacyExtra(dict):
    """``extra`` of :class:`Part`, :class:`Contour` and :class:`Craft`: a dict of opaque
    round-trip data that also answers the old keys of the typed fields (read, write, delete,
    ``in``, iteration and copies), so code written against the old keys keeps working.

    The dict itself stores only the opaque keys; the old keys live in the owner's typed fields.
    ``dict(extra)``, ``extra.items()`` and ``copy.deepcopy(extra)`` (alone) give both, like the
    old dict did; ``pickle`` stores a plain dict (both) and the owner re-wraps it."""

    __slots__ = ("_ref", "_virt")

    def __init__(self, owner: Any, data: Optional[dict] = None, route: bool = True) -> None:
        super().__init__()
        self._ref = weakref.ref(owner)
        self._virt: dict[str, _LegacyKey] = type(owner)._LEGACY_KEYS
        if data:
            for k, v in data.items():
                spec = self._virt.get(k) if route else None
                if spec is not None:
                    spec.set(owner, v)
                elif k not in self._virt:
                    dict.__setitem__(self, k, v)

    # -- the owner -----------------------------------------------------------
    def _owner(self) -> Any:
        o = self._ref()
        if o is None:
            raise ReferenceError("the owner of this extra dict no longer exists")
        return o

    def _present(self) -> list[tuple[str, _LegacyKey]]:
        o = self._ref()
        if o is None:
            return []
        return [(k, s) for k, s in self._virt.items() if s.present(o)]

    def materialize(self) -> dict:
        """A plain dict with the opaque keys and the present old keys (the pre-Wave-3 form)."""
        out = dict(dict.items(self))
        o = self._ref()
        if o is not None:
            for k, s in self._virt.items():
                if s.present(o):
                    out[k] = s.get(o)
        return out

    # -- mapping protocol ----------------------------------------------------
    def __getitem__(self, key):
        spec = self._virt.get(key)
        if spec is None:
            return dict.__getitem__(self, key)
        o = self._owner()
        _warn_legacy(o, key, spec)
        if not spec.present(o):
            raise KeyError(key)
        return spec.get(o)

    def get(self, key, default=None):
        spec = self._virt.get(key)
        if spec is None:
            return dict.get(self, key, default)
        o = self._ref()
        if o is None:
            return default
        _warn_legacy(o, key, spec)
        return spec.get(o) if spec.present(o) else default

    def __contains__(self, key) -> bool:
        spec = self._virt.get(key)
        if spec is None:
            return dict.__contains__(self, key)
        o = self._ref()
        if o is None:
            return False
        _warn_legacy(o, key, spec)
        return spec.present(o)

    def __setitem__(self, key, value) -> None:
        spec = self._virt.get(key)
        if spec is None:
            dict.__setitem__(self, key, value)
            return
        o = self._owner()
        _warn_legacy(o, key, spec)
        spec.set(o, value)

    def __delitem__(self, key) -> None:
        spec = self._virt.get(key)
        if spec is None:
            dict.__delitem__(self, key)
            return
        o = self._owner()
        _warn_legacy(o, key, spec)
        if not spec.present(o):
            raise KeyError(key)
        spec.delete(o)

    _MISSING = object()

    def pop(self, key, default=_MISSING):
        spec = self._virt.get(key)
        if spec is None:
            if default is LegacyExtra._MISSING:
                return dict.pop(self, key)
            return dict.pop(self, key, default)
        o = self._ref()
        if o is not None:
            _warn_legacy(o, key, spec)
        if o is None or not spec.present(o):
            if default is LegacyExtra._MISSING:
                raise KeyError(key)
            return default
        v = spec.get(o)
        spec.delete(o)
        return v

    def setdefault(self, key, default=None):
        spec = self._virt.get(key)
        if spec is None:
            return dict.setdefault(self, key, default)
        o = self._owner()
        _warn_legacy(o, key, spec)
        if not spec.present(o):
            spec.set(o, default)
        return spec.get(o)

    def update(self, other=(), **kw) -> None:  # type: ignore[override]
        items = other.items() if hasattr(other, "items") else other
        for k, v in itertools.chain(items, kw.items()):
            self[k] = v

    def clear(self) -> None:
        dict.clear(self)
        o = self._ref()
        if o is not None:
            for _k, s in self._present():
                s.delete(o)

    def popitem(self):
        if dict.__len__(self):
            return dict.popitem(self)
        for k, s in self._present():
            o = self._owner()
            v = s.get(o)
            s.delete(o)
            return k, v
        raise KeyError("popitem(): dictionary is empty")

    def keys(self):  # type: ignore[override]
        return self.materialize().keys()

    def items(self):  # type: ignore[override]
        return self.materialize().items()

    def values(self):  # type: ignore[override]
        return self.materialize().values()

    def __iter__(self):
        return iter(self.keys())

    def __len__(self) -> int:
        return dict.__len__(self) + len(self._present())

    def __bool__(self) -> bool:
        return len(self) > 0

    def __eq__(self, other) -> bool:
        if isinstance(other, LegacyExtra):
            other = other.materialize()
        if not isinstance(other, dict):
            return NotImplemented
        return self.materialize() == other

    def __ne__(self, other) -> bool:
        r = self.__eq__(other)
        return r if r is NotImplemented else not r

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return dict.__repr__(self)              # the opaque data; the typed fields show themselves

    def copy(self) -> dict:  # type: ignore[override]
        return self.materialize()

    def __copy__(self) -> dict:
        return self.materialize()

    def __deepcopy__(self, memo: dict):
        o = self._ref()
        new_owner = memo.get(id(o)) if o is not None else None
        if new_owner is not None:
            # copied as part of its owner: the typed fields are copied with the owner
            out = LegacyExtra(new_owner)
            memo[id(self)] = out
            for k, v in dict.items(self):
                dict.__setitem__(out, k, copy.deepcopy(v, memo))
            return out
        res = copy.deepcopy(self.materialize(), memo)
        memo[id(self)] = res
        return res

    def __reduce__(self):
        return (dict, (self.materialize(),))

    def __or__(self, other):
        d = self.materialize()
        d.update(other)
        return d

    def __ior__(self, other):
        self.update(other)
        return self


_NO_EXTRA = type("_NoExtra", (), {"__repr__": lambda self: "{}"})()


class _ExtraField:
    """Data descriptor for ``extra``: the instance keeps its :class:`LegacyExtra` in
    ``__dict__['extra']``; a plain dict assigned (or restored by pickle / ``copy.copy``) is
    wrapped, its old keys routed into the typed fields."""

    def __set_name__(self, owner, name) -> None:
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return _NO_EXTRA                     # the dataclass default (a fresh dict per object)
        d = obj.__dict__
        v = d.get("extra")
        if type(v) is LegacyExtra and v._ref() is obj:
            return v
        return self._adopt(obj, v)

    def __set__(self, obj, value) -> None:
        if value is _NO_EXTRA or value is None:
            value = {}
        if obj.__dict__.get("_initialised") is not True:
            # inside __init__: the typed fields after ``extra`` are not set yet; route in
            # __post_init__ so an old key is not overwritten by the field default
            obj.__dict__["extra"] = LegacyExtra(obj, {k: v for k, v in _items(value)
                                                      if k not in type(obj)._LEGACY_KEYS}, route=False)
            pending = {k: v for k, v in _items(value) if k in type(obj)._LEGACY_KEYS}
            if pending:
                obj.__dict__["_pending_extra"] = pending
            return
        obj.__dict__["extra"] = LegacyExtra(obj, dict(_items(value)))

    def _adopt(self, obj, v) -> LegacyExtra:
        if v is None:
            v = {}
        ex = LegacyExtra(obj, dict(_items(v)))
        obj.__dict__["extra"] = ex
        return ex


def _items(v) -> list:
    if isinstance(v, LegacyExtra):
        return list(v.materialize().items())
    return list(dict.items(v)) if isinstance(v, dict) else list(dict(v).items())


def _finish_init(obj) -> None:
    d = obj.__dict__
    d["_initialised"] = True
    pending = d.pop("_pending_extra", None)
    if pending:
        for k, v in pending.items():
            type(obj)._LEGACY_KEYS[k].set(obj, v)


def _key(attr: str, default: Any = None, present: Optional[Callable[[Any], bool]] = None) -> _LegacyKey:
    """An old key mapped 1:1 onto attribute ``attr`` (absent when the value is ``default``)."""
    return _LegacyKey(
        attr,
        present or (lambda o: getattr(o, attr) is not default),
        lambda o: getattr(o, attr),
        lambda o, v: setattr(o, attr, v),
        lambda o: setattr(o, attr, copy.copy(default) if isinstance(default, list) else default),
    )


# --------------------------------------------------------------------------- craft
class _JointsField:
    """``Craft.micro_joints``: every assignment is normalised to :class:`MicroJoint` entries.

    Accepted entries: a position (float; width from the .chf per-joint widths, else
    ``joint_width``, else 0.5 mm) or a (position, width) pair."""

    def __set_name__(self, owner, name) -> None:
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return _NO_EXTRA
        return obj.__dict__.setdefault("micro_joints", [])

    def __set__(self, obj, value) -> None:
        if value is _NO_EXTRA or value is None:
            value = []
        out: list[MicroJoint] = []
        auto: list[int] = []
        for i, j in enumerate(value):
            if isinstance(j, (tuple, list)):
                out.append(MicroJoint(float(j[0]), float(j[1])))
            else:
                out.append(MicroJoint(float(j), DEFAULT_JOINT_WIDTH))
                auto.append(i)
        d = obj.__dict__
        d["micro_joints"] = out
        d["_mj_auto"] = tuple(auto)
        if auto:
            _resolve_joint_widths(obj)


def _resolve_joint_widths(cr) -> None:
    """Width of the joints given without one: the .chf per-joint width (``_mj_widths``, the +0x128
    list of the record) at the same index, else ``joint_width``, else 0.5 mm (the order
    ``cam.toolpath`` used before Wave 3)."""
    d = cr.__dict__
    js = d.get("micro_joints")
    if not js:
        return
    raw = d.get("_mj_widths") or ()
    jw = d.get("joint_width")
    dflt = DEFAULT_JOINT_WIDTH if jw is None else float(jw)
    for i in d.get("_mj_auto", ()):
        if i >= len(js):
            continue
        w = raw[i] if i < len(raw) and raw[i] is not None else dflt
        if js[i].width != float(w) or type(js[i].width) is not float:
            js[i] = MicroJoint(js[i].position, float(w))


def _set_mj_widths(cr, v) -> None:
    cr.__dict__["_mj_widths"] = None if v is None else list(v)
    _resolve_joint_widths(cr)


def _set_joint_width(cr, v) -> None:
    cr.__dict__["joint_width"] = v
    _resolve_joint_widths(cr)


class _JointWidthField:
    def __set_name__(self, owner, name) -> None:
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return None
        return obj.__dict__.get("joint_width")

    def __set__(self, obj, value) -> None:
        _set_joint_width(obj, value)


@dataclass(eq=False)
class Craft:
    """Per-part cutting attributes ("<Crafts>" block in .chf)."""
    layer: int = 1                       # process layer index (colour / parameter set)
    kerf_comp: int = -1                  # -1 none, 0 inner, 1 outer (see docs/re/formats.md)
    kerf_width: float = 0.0
    lead_in: LeadParams = field(default_factory=LeadParams)
    lead_out: LeadParams = field(default_factory=lambda: LeadParams(kind=0))
    #: micro-joints as (position mm, width mm); see :class:`MicroJoint` and :class:`_JointsField`
    micro_joints: list[MicroJoint] = _JointsField()  # type: ignore[assignment]
    #: opaque round-trip data (the .chf record's ``pwm_mode``, ``cool_positions`` ...)
    extra: dict = _ExtraField()  # type: ignore[assignment]
    #: cooling points, mm of arc length from the contour's first point (original +0x15c)
    cool_points: list[CoolPoint] = field(default_factory=list)
    #: start as a fraction 0..1 of the contour length (setter 0x10071f30, +0x180); None = unset
    start_param: Optional[float] = None
    #: signed overcut (+) / gap (-) in mm (+0x198); None = unset (0)
    overcut: Optional[float] = None
    #: default width (mm) of joints added without one (the micro-joint dialog's length)
    joint_width: Optional[float] = _JointWidthField()  # type: ignore[assignment]
    #: lead-check error mark (+0x1f9, mf91 clears it)
    lead_error: bool = False

    _LEGACY_KEYS = {}  # filled below

    def __post_init__(self) -> None:
        _finish_init(self)

    def __eq__(self, other) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.layer == other.layer and self.kerf_comp == other.kerf_comp
                and self.kerf_width == other.kerf_width and self.lead_in == other.lead_in
                and self.lead_out == other.lead_out and self.micro_joints == other.micro_joints
                and self.cool_points == other.cool_points and self.start_param == other.start_param
                and self.overcut == other.overcut and self.joint_width == other.joint_width
                and self.lead_error == other.lead_error
                and self.__dict__.get("_mj_widths") == other.__dict__.get("_mj_widths")
                and dict.__eq__(self.extra, other.extra))

    __hash__ = None  # type: ignore[assignment]

    # -- typed helpers ---------------------------------------------------------
    def joint_list(self) -> list[tuple[float, float]]:
        """(position mm, width mm) of every joint (also of entries appended in place)."""
        dflt = DEFAULT_JOINT_WIDTH if self.joint_width is None else float(self.joint_width)
        out = []
        for j in self.micro_joints:
            if isinstance(j, (tuple, list)):
                out.append((float(j[0]), float(j[1])))
            else:
                out.append((float(j), dflt))
        return out

    @property
    def cool_point(self) -> Optional[Point]:
        """Deprecated single cooling point: the first (x, y) entry of :attr:`cool_points`."""
        for p in self.cool_points:
            if isinstance(p, (tuple, list)):
                return tuple(p)  # type: ignore[return-value]
        return None

    @cool_point.setter
    def cool_point(self, value: Optional[Point]) -> None:
        pts = [p for p in self.cool_points if not isinstance(p, (tuple, list))]
        if value is not None:
            pts.append((float(value[0]), float(value[1])))
        self.cool_points = pts


Craft._LEGACY_KEYS = {
    "micro_joint_widths": _LegacyKey(
        "micro_joints", lambda o: o.__dict__.get("_mj_widths") is not None,
        lambda o: o.__dict__.get("_mj_widths"), _set_mj_widths, lambda o: _set_mj_widths(o, None)),
    "micro_joint_len": _key("joint_width"),
    "cool_points": _LegacyKey(
        "cool_points", lambda o: bool(o.cool_points), lambda o: o.cool_points,
        lambda o, v: setattr(o, "cool_points", list(v) if v is not None else []),
        lambda o: setattr(o, "cool_points", [])),
    "start_param": _key("start_param"),
    "overcut": _key("overcut"),
    "lead_error": _LegacyKey("lead_error", lambda o: bool(o.lead_error), lambda o: o.lead_error,
                             lambda o, v: setattr(o, "lead_error", bool(v)),
                             lambda o: setattr(o, "lead_error", False)),
}


# --------------------------------------------------------------------------- contour
def segment_key(segs) -> tuple:
    """A value fingerprint of ``segs`` (equal iff the segments compare equal): what
    :meth:`Contour.source_glyphs` compares instead of keeping a deep copy of the segments."""
    out: list = []
    for s in segs:
        t = type(s)
        if t is Line:
            out.append((_pt(s.start), _pt(s.end)))
        elif t is Arc:
            out.append((_pt(s.start), _pt(s.end), _pt(s.center), s.ccw))
        else:
            out.append((t, copy.deepcopy(s)))
    return tuple(out)


def _pt(p):
    return p if type(p) is tuple else ("_", tuple(p) if p is not None else None)


def _set_glyph_segments(c, v) -> None:
    c.__dict__["glyph_key"] = None if v is None else segment_key(v)


@dataclass(eq=False)
class Contour:
    segments: list[Segment] = field(default_factory=list)
    closed: bool = True
    #: opaque round-trip data (the .chf record, importer notes)
    extra: dict = _ExtraField()  # type: ignore[assignment]
    uid: int = field(default_factory=new_id, compare=False, repr=False)
    #: per-contour craft override (multi-contour parts; ``cam.manual.contour_craft``)
    craft: Optional[Craft] = None
    #: source glyphs as attached (:meth:`set_source_glyphs`); see :meth:`source_glyphs`
    glyphs: Optional[list[Glyph]] = None
    #: :func:`segment_key` of the segments the glyphs describe
    glyph_key: Optional[tuple] = field(default=None, repr=False)

    _LEGACY_KEYS = {}  # filled below

    def __post_init__(self) -> None:
        _finish_init(self)

    def __eq__(self, other) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.segments == other.segments and self.closed == other.closed
                and self.craft == other.craft and self.glyphs == other.glyphs
                and self.glyph_key == other.glyph_key and dict.__eq__(self.extra, other.extra))

    __hash__ = None  # type: ignore[assignment]

    def points(self, tol: float = 0.05) -> Iterator[Point]:
        """Flatten to a polyline (for display / geometry ops)."""
        if not self.segments:
            return
        yield self.segments[0].start
        for s in self.segments:
            if isinstance(s, Line):
                yield s.end
            else:
                n = max(2, int(math.ceil(s.length() / max(tol, 1e-6) ** 0.5 / 2)))
                n = min(n, 720)
                for i in range(1, n + 1):
                    yield s.point_at(i / n)

    def length(self) -> float:
        return sum(s.length() for s in self.segments)

    def start_point(self) -> Point:
        return self.segments[0].start

    def set_source_glyphs(self, glyphs: list) -> None:
        """Attach the source glyphs of the current ``segments`` (see :class:`Glyph`)."""
        self.glyphs = list(glyphs)
        self.glyph_key = segment_key(self.segments)

    def source_glyphs(self) -> Optional[list]:
        """The attached source glyphs, or None when there are none or ``segments`` has been
        changed since they were attached (the glyphs no longer describe the geometry)."""
        g = self.glyphs
        if g and self.glyph_key is not None and self.glyph_key == segment_key(self.segments):
            return g
        return None


Contour._LEGACY_KEYS = {
    "craft": _key("craft"),
    "glyphs": _key("glyphs"),
    # the old deep copy of the segments: now their fingerprint; reads give the segments while the
    # glyphs are current (what the copy compared equal to), else None
    "glyph_segments": _LegacyKey(
        "glyph_key", lambda o: o.glyph_key is not None,
        lambda o: list(o.segments) if o.glyph_key == segment_key(o.segments) else None,
        _set_glyph_segments, lambda o: _set_glyph_segments(o, None)),
}


# --------------------------------------------------------------------------- part
@dataclass(eq=False)
class Part:
    """One drawable object ("graph" / "Group elem" in .chf)."""
    contours: list[Contour] = field(default_factory=list)
    craft: Craft = field(default_factory=Craft)
    name: str = ""
    selected: bool = False
    #: opaque round-trip data (``chf`` record, ``_chf_snapshot``) and tool notes (EXTRA_KEYS)
    extra: dict = _ExtraField()  # type: ignore[assignment]
    uid: int = field(default_factory=new_id, compare=False, repr=False)
    #: the raster engraving of a raster part (``mlaser.cam.raster_engraving.RasterEngraving``);
    #: the part's single contour is its frame
    raster: Optional[Any] = None

    _LEGACY_KEYS = {}  # filled below

    def __post_init__(self) -> None:
        _finish_init(self)

    def __eq__(self, other) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.contours == other.contours and self.craft == other.craft and self.name == other.name
                and self.selected == other.selected and self.raster is other.raster
                and dict.__eq__(self.extra, other.extra))

    __hash__ = None  # type: ignore[assignment]

    def bbox(self) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for c in self.contours:
            for x, y in c.points():
                xs.append(x); ys.append(y)
        if not xs:
            return (0, 0, 0, 0)
        return (min(xs), min(ys), max(xs), max(ys))


Part._LEGACY_KEYS = {"raster": _key("raster")}


@dataclass
class LayerParams:
    """Cutting process parameters for one layer (speed, power, gas, height...)."""
    index: int = 1
    color: str = "#ffffff"
    speed: float = 100.0          # mm/s
    power_pct: float = 100.0
    frequency_hz: float = 5000.0
    duty_pct: float = 100.0
    gas: int = 0
    gas_pressure: float = 0.0
    cut_height: float = 1.0
    pierce: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class Document:
    parts: list[Part] = field(default_factory=list)
    layers: dict[int, LayerParams] = field(default_factory=dict)
    version: int = 9
    extra: dict = field(default_factory=dict)

    def bbox(self):
        bs = [p.bbox() for p in self.parts if p.contours]
        if not bs:
            return (0, 0, 0, 0)
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


# --------------------------------------------------------------------------- extra key registry
EXTRA_KEYS: dict[str, tuple[str, str]] = {
    # key: (owner, meaning).  "opaque": round-trip data only the writer of that format reads.
    "Document.chf": ("opaque", ".chf trailer: vision graphs, rotation block, skipped lines"),
    "Document.order": ("cache", "cutting order (part, contour) set by the CAM sorts"),
    "Document.job_offset": ("transient", "placement offset of a job copy (controller.coords)"),
    "Document.rotated / rotate_angle / rotate_center / rotate_offset": ("state", "edge-seek rotation"),
    "Document.texts / points / regions / faces / source / placement_offset": ("import", "DXF import notes"),
    "Document.dock_point": ("state", "dock point (ext_craft)"),
    "Part.chf / _chf_snapshot / chf_type": ("opaque", ".chf graph record and the snapshot edits are diffed against"),
    "Part.scan_paths / scan_kind": ("derived", "fly-cut scan paths (cam.flycut)"),
    "Part.nest / nest_sheet / _origin": ("state", "nest placement (nest.apply)"),
    "Part.group / members": ("state", "a CGlyGroup part (cad.arrange)"),
    "Part.array_cell / array_index": ("state", "array copy index (cam.array)"),
    "Part.dxf_type": ("import", "DXF entity type"),
    "Contour.chf / _chf_craft": ("opaque", ".chf contour record and its craft snapshot"),
    "Contour.close_tol / depth": ("import", "closing tolerance and nesting depth for the .chf writer"),
    "Contour.text / font": ("import", "the TEXT source of text outlines"),
    "Contour.sort_layer / point / ellipse / spline_fit / raster_frame / alpha_source": (
        "state", "tool notes (cam.ordering, cad.primitives, raster frame, alpha corner source)"),
    "Craft.pwm_mode / cool_positions": ("opaque", ".chf <PWM Control> mode and <coolPos> fractions"),
    "LayerParams.placeholder / gp / name": ("state", "layer table notes (ui)"),
}


def ensure_unique_ids(doc: "Document", keep: Iterable = ()) -> int:
    """Give every Part and Contour of ``doc`` a uid no other object in it has.

    Objects in ``keep`` (the objects that held their uid before an edit) keep it; any other
    object whose uid is already taken (a deep copy of a part that is still in the document)
    gets a new one.  Returns the number of ids reassigned."""
    kept = {id(o) for o in keep}
    n = 0
    for attr in ("parts", "contours"):
        objs = list(doc.parts) if attr == "parts" else [c for p in doc.parts for c in p.contours]
        seen: set[int] = set()
        for o in objs:                       # the kept objects claim their uid first
            if id(o) in kept:
                seen.add(o.uid)
        for o in objs:
            if id(o) in kept:
                continue
            if o.uid in seen:
                o.uid = new_id()
                n += 1
            seen.add(o.uid)
    return n
