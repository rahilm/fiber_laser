"""``ParityProfile``: which implementation of each fenced algorithm the pipeline uses.

The domain code (motion, nc, cam, io, formats) never calls a port directly where an algorithm is
chosen; it asks the active profile::

    from mlaser.parity_ports import active_profile
    plan = active_profile().motion.plan_velocity(points, params)

The default profile, ``"original"`` (:data:`ORIGINAL`), is the bit-exact behaviour of Mlaser
v0.0.1.51_SP1 that the goldens and the Wine parity run pin.  A modern algorithm (a faster
orderer, chord-error flattening, a jerk-limited planner, ...) is added as a *new* profile that
replaces one or more slots, without editing a port::

    FAST = ORIGINAL.derive("fast-order", "numpy ordering",
                           ordering=Implementation("ordering", import_sort="mypkg.order:import_sort", ...))
    register_profile(FAST)
    with use_profile("fast-order"):
        job = compile_job(doc)

Each slot is an :class:`Implementation`: a named set of callables (resolved lazily from
``"module:attribute"`` targets, so building a profile imports nothing) that satisfies the slot's
interface (the ``Protocol`` classes below list the names a slot must provide).

The active profile is process-wide (a plain module global, also seen by worker threads);
:func:`use_profile` switches it for a ``with`` block.
"""
from __future__ import annotations

import contextlib
import importlib
import logging
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Iterator, Optional, Protocol, Union


# --------------------------------------------------------------------------- interfaces
class MotionPlanner(Protocol):
    """Velocity planning and 1-D motion profiles (CVelocityPlanning, segInterp, arcInterp)."""
    plan_velocity: Callable[..., Any]       # (points, PlannerParams) -> Plan
    make_profile: Callable[..., Any]        # (L, vs, vmax, ve, acc, jerk, mode) -> Profile
    scurve_profile: Callable[..., Any]
    seg_interp: Callable[..., list]
    seg_interp_time: Callable[..., float]
    arc_interp: Callable[..., list]


class Smoother(Protocol):
    """Corner blending of a cut path before planning (CContoutSmooth) and its curve table."""
    smooth_path_points: Callable[..., Any]  # (moves, MotionConfig, lead_index=) -> (points, geom)
    ContourSmoother: type
    CurveTable: type


class SplineCurves(Protocol):
    """Exact spline glyphs carried through the cutting pipeline (parity JC-07)."""
    SplineCurve: type
    CurveRef: type


class Sampler(Protocol):
    """Pulse quantisation of the per-period samples (MainApp FUN_00462630 / FUN_00473bd0)."""
    Emitter: type
    emit_samples: Callable[..., None]
    gap_flags: Callable[..., list]
    sample_speeds: Callable[..., list]


class ProcessEncoder(Protocol):
    """ManuItem -> FIFO items (NCModule FUN_10043c20)."""
    ManuEncoder: type


class Orderer(Protocol):
    """Cutting order and start points (CADModule sort dispatcher 0x1003f010, FUN_10071f80)."""
    import_sort: Callable[..., Any]
    local_optimal_elems: Callable[..., Any]
    containers_last_faithful: Callable[..., Any]
    ring_sort: Callable[..., Any]
    choose_start: Callable[..., float]


class ArcFitter(Protocol):
    """Polyline -> lines and arcs (CircleFitDLL CCircleFit::mainFit)."""
    fit_arcs: Callable[..., Any]


class DxfRules(Protocol):
    """DXF entities -> glyphs -> contours as DxfParse + CADModule build them."""
    entity_glyphs: Callable[..., list]
    collect: Callable[..., None]
    chain_glyphs: Callable[..., list]
    contour_of_chain: Callable[..., Any]


class ChfCodec(Protocol):
    """The .chf document format (CSerialEng text, CGlyContour records), byte for byte."""
    loads_chf: Callable[..., Any]
    dumps_chf: Callable[..., bytes]
    load_chf: Callable[..., Any]
    save_chf: Callable[..., None]


#: slot name -> interface
INTERFACES: dict[str, type] = {
    "motion": MotionPlanner,
    "smoother": Smoother,
    "spline": SplineCurves,
    "sampling": Sampler,
    "process": ProcessEncoder,
    "ordering": Orderer,
    "arcfit": ArcFitter,
    "dxf": DxfRules,
    "chf": ChfCodec,
}


def interface_names(iface: type) -> tuple[str, ...]:
    return tuple(getattr(iface, "__annotations__", {}))


# --------------------------------------------------------------------------- implementations
class Implementation:
    """A named set of callables filling one slot; targets are ``"module:attribute"`` strings
    (resolved and cached on first use) or the objects themselves."""

    def __init__(self, slot: str, **targets: Union[str, Any]):
        if slot not in INTERFACES:
            raise ValueError(f"unknown slot {slot!r} (one of {sorted(INTERFACES)})")
        missing = set(interface_names(INTERFACES[slot])) - set(targets)
        if missing:
            raise TypeError(f"{slot} implementation lacks {sorted(missing)}")
        self.slot = slot
        self._targets = dict(targets)
        self._cache: dict[str, Any] = {}

    def names(self) -> tuple[str, ...]:
        return tuple(self._targets)

    def target(self, name: str) -> Union[str, Any]:
        return self._targets[name]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._cache[name]
        except KeyError:
            pass
        try:
            t = self._targets[name]
        except KeyError:
            raise AttributeError(f"{self.slot} implementation has no {name!r}") from None
        if isinstance(t, str):
            mod, _, attr = t.partition(":")
            t = getattr(importlib.import_module(mod), attr)
        self._cache[name] = t
        return t

    def with_(self, **targets: Union[str, Any]) -> "Implementation":
        """A copy with some targets replaced."""
        return Implementation(self.slot, **{**self._targets, **targets})

    def __repr__(self) -> str:
        return f"Implementation({self.slot!r}, {', '.join(self._targets)})"


@dataclass(frozen=True)
class ParityProfile:
    """One choice of implementation per slot (see the module docstring)."""
    name: str
    description: str
    motion: Implementation
    smoother: Implementation
    spline: Implementation
    sampling: Implementation
    process: Implementation
    ordering: Implementation
    arcfit: Implementation
    dxf: Implementation
    chf: Implementation
    #: logged as a warning whenever the profile is made active (e.g. "not validated on hardware")
    warning: str = ""
    #: options of rebuild extensions that are not parity slots (no bit-exact original exists),
    #: e.g. ``{"shortest_path": "ortools"}`` for sort mode 20 (``mlaser.cam.ordering``)
    options: tuple = ()

    def option(self, key: str, default: Any = None) -> Any:
        return dict(self.options).get(key, default)

    def slots(self) -> dict[str, Implementation]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name in INTERFACES}

    def derive(self, name: str, description: str, *, warning: str = "",
               options: Optional[dict] = None, **slots: Implementation) -> "ParityProfile":
        """A new profile that replaces the given slots (and extension options) and keeps the
        others."""
        for k, impl in slots.items():
            if k not in INTERFACES or impl.slot != k:
                raise ValueError(f"slot {k!r} needs an Implementation of that slot")
        opts = tuple(sorted({**dict(self.options), **(options or {})}.items()))
        return replace(self, name=name, description=description, warning=warning, options=opts, **slots)


_PP = "mlaser.parity_ports"

ORIGINAL = ParityProfile(
    name="original",
    description="bit-exact Mlaser v0.0.1.51_SP1 behaviour (goldens + Wine parity run)",
    motion=Implementation(
        "motion",
        plan_velocity=f"{_PP}.cadmodule.planner:plan_velocity",
        make_profile=f"{_PP}.cadmodule.profile:make_profile",
        scurve_profile=f"{_PP}.cadmodule.profile:scurve_profile",
        seg_interp=f"{_PP}.cadmodule.interp:seg_interp",
        seg_interp_time=f"{_PP}.cadmodule.interp:seg_interp_time",
        arc_interp=f"{_PP}.cadmodule.interp:arc_interp",
    ),
    # CContoutSmooth: the curve tables, crtmath and the per-sample lookup on the C kernel when
    # the default selection chose it (profiles/native_smooth.py), else the Python port;
    # bit-identical either way ("original-python" below always runs the port)
    smoother=Implementation(
        "smoother",
        smooth_path_points=f"{_PP}.profiles.native_smooth:smooth_path_points",
        ContourSmoother=f"{_PP}.cadmodule.smooth:ContourSmoother",
        CurveTable=f"{_PP}.cadmodule.curvetable:CurveTable",
    ),
    spline=Implementation(
        "spline",
        SplineCurve=f"{_PP}.cadmodule.spline:SplineCurve",
        CurveRef=f"{_PP}.cadmodule.spline:CurveRef",
    ),
    # the emitter loop on the C kernel when it is built and passes its self-check at first use,
    # else the Python port (MLASER_NATIVE=0/1 forces one; profiles/native.py, NATIVE_KERNEL.md).
    # Bit-identical either way; "original-python" below always runs the port.
    sampling=Implementation(
        "sampling",
        Emitter=f"{_PP}.profiles.native:DefaultEmitter",
        emit_samples=f"{_PP}.profiles.native:emit_samples",
        gap_flags=f"{_PP}.mainapp.sampling:gap_flags",
        sample_speeds=f"{_PP}.mainapp.sampling:sample_speeds",
    ),
    process=Implementation(
        "process",
        ManuEncoder=f"{_PP}.ncmodule.manu:ManuEncoder",
    ),
    ordering=Implementation(
        "ordering",
        # numpy / grid drop-ins with the reference's results bit for bit (the statement-level
        # reference functions in cadmodule.ordering: profile "original-reference")
        import_sort=f"{_PP}.cadmodule.ordering_fast:import_sort",
        local_optimal_elems=f"{_PP}.cadmodule.ordering_fast:local_optimal_elems",
        containers_last_faithful=f"{_PP}.cadmodule.ordering_fast:containers_last_faithful",
        # the same scan on the C kernel when the default selection chose it (ordering_fast)
        ring_sort=f"{_PP}.cadmodule.ordering_fast:ring_sort",
        choose_start=f"{_PP}.cadmodule.ordering:choose_start",
    ),
    arcfit=Implementation(
        "arcfit",
        fit_arcs=f"{_PP}.circlefit.fit:fit_arcs",
    ),
    dxf=Implementation(
        "dxf",
        entity_glyphs=f"{_PP}.dxfparse.rules:entity_glyphs",
        collect=f"{_PP}.dxfparse.rules:collect",
        chain_glyphs=f"{_PP}.dxfparse.rules:chain_glyphs",
        contour_of_chain=f"{_PP}.dxfparse.rules:contour_of_chain",
    ),
    chf=Implementation(
        "chf",
        loads_chf=f"{_PP}.cadmodule.chf:loads_chf",
        dumps_chf=f"{_PP}.cadmodule.chf:dumps_chf",
        load_chf=f"{_PP}.cadmodule.chf:load_chf",
        save_chf=f"{_PP}.cadmodule.chf:save_chf",
    ),
)

#: the Python port of the emitter loop (MainApp FUN_00462630 / FUN_00473bd0): the reference the
#: C kernel is checked against
PYTHON_SAMPLING = Implementation(
    "sampling",
    Emitter=f"{_PP}.mainapp.sampling:Emitter",
    emit_samples=f"{_PP}.mainapp.sampling:emit_samples",
    gap_flags=f"{_PP}.mainapp.sampling:gap_flags",
    sample_speeds=f"{_PP}.mainapp.sampling:sample_speeds",
)

#: the Python port of CContoutSmooth (curve tables, crtmath, sampling): the reference of the
#: C pieces of profiles/native_smooth.py
PYTHON_SMOOTHER = Implementation(
    "smoother",
    smooth_path_points=f"{_PP}.cadmodule.smooth:smooth_path_points",
    ContourSmoother=f"{_PP}.cadmodule.smooth:ContourSmoother",
    CurveTable=f"{_PP}.cadmodule.curvetable:CurveTable",
)

#: ORIGINAL with the emitter loop and the smoother always on the Python port (whatever
#: MLASER_NATIVE says)
ORIGINAL_PYTHON = ORIGINAL.derive(
    "original-python", "original, with the emitter loop and the smoother on the Python port (the reference)",
    sampling=PYTHON_SAMPLING, smoother=PYTHON_SMOOTHER)

PROFILES: dict[str, ParityProfile] = {ORIGINAL.name: ORIGINAL, ORIGINAL_PYTHON.name: ORIGINAL_PYTHON}
DEFAULT = ORIGINAL.name
_active: ParityProfile = ORIGINAL


def register_profile(profile: ParityProfile) -> ParityProfile:
    if profile.name in PROFILES and PROFILES[profile.name] is not profile:
        raise ValueError(f"a profile named {profile.name!r} is already registered")
    PROFILES[profile.name] = profile
    return profile


def get_profile(name: str | None = None) -> ParityProfile:
    """The registered profile ``name``; without a name, the active one.  An unknown name first
    imports the optional profiles (:mod:`mlaser.parity_ports.profiles`), which register
    themselves."""
    if name is None:
        return _active
    if name not in PROFILES:
        importlib.import_module(f"{_PP}.profiles")
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"no parity profile {name!r} (registered: {sorted(PROFILES)})") from None


def active_profile() -> ParityProfile:
    return _active


def set_profile(profile: Union[str, ParityProfile]) -> ParityProfile:
    """Make ``profile`` (or the registered profile of that name) active; returns the previous."""
    global _active
    prev = _active
    _active = get_profile(profile) if isinstance(profile, str) else profile
    if _active.warning and _active is not prev:
        logging.getLogger(__name__).warning("parity profile %r selected: %s", _active.name,
                                            _active.warning)
    return prev


@contextlib.contextmanager
def use_profile(profile: Union[str, ParityProfile]) -> Iterator[ParityProfile]:
    prev = set_profile(profile)
    try:
        yield _active
    finally:
        set_profile(prev)


def dispatch(slot: str, name: str) -> Callable[..., Any]:
    """A function that calls ``active_profile().<slot>.<name>``: what the domain modules export
    under the old name of a ported entry point, so every existing caller goes through the
    profile.  It carries the default implementation's name and docstring."""
    import functools

    target = getattr(getattr(ORIGINAL, slot), name)

    @functools.wraps(target)
    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(getattr(_active, slot), name)(*args, **kwargs)

    call.__parity_slot__ = (slot, name)  # type: ignore[attr-defined]
    return call


def export_ports(namespace: dict, module: str, names: Union[str, tuple, list] = "*",
                 **dispatched: tuple[str, str]) -> list[str]:
    """Re-export a port module from a domain module (the old home of the code).

    ``names="*"`` copies every top-level name of ``module`` that the port defines or imports
    (private ones too, so ``old_module._helper`` keeps resolving; dunders excluded); otherwise only
    the listed names.  ``dispatched`` maps an exported name to its ``(slot, name)`` in the profile:
    that name becomes a :func:`dispatch` function, so callers of the old name go through the
    active profile.  Returns the exported names.
    """
    mod = importlib.import_module(module)
    if names == "*":
        names = [n for n in vars(mod) if not (n.startswith("__") and n.endswith("__"))]
    out = []
    for n in names:
        if n in dispatched:
            namespace[n] = dispatch(*dispatched[n])
        else:
            namespace[n] = getattr(mod, n)
        out.append(n)
    for n, sn in dispatched.items():
        if n not in namespace:
            namespace[n] = dispatch(*sn)
            out.append(n)
    return out
