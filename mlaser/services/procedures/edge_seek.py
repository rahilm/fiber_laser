"""Guided edge seek: capacitive 3 / 4 points, disc centre, manual two points.

The measuring sequences are the original's (:class:`mlaser.controller.edgeseek.EdgeSeeker`,
docs/re/protocol.md §16); this module wraps them in a step machine with a check, a review of the
measured sheet (outline and angle, :func:`sheet_outline`) and an explicit, undoable apply
(:func:`apply_to_docs`, one ``DocumentService.edit``).
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable, Optional

from mlaser.controller import edgeseek as E

from .engine import Procedure, ProcedureAborted, Step, StepFailed

Point = tuple[float, float]

MODES = {"cap3": 3, "cap4": 4, "disc": -1, "two_point": 0}
TITLES = {"cap3": "wizard.edge.title_cap3", "cap4": "wizard.edge.title_cap4",
          "disc": "wizard.edge.title_disc", "two_point": "wizard.edge.title_two_point"}


def sheet_outline(res: E.EdgeSeekResult, p: Optional[E.EdgeSeekParams] = None,
                  mode: str = "cap4") -> dict:
    """What to draw for a measured sheet (machine coordinates, mm):

    ``outline`` (list of polylines), ``points`` (the measured points), ``corner``, ``origin``,
    ``angle_deg``.  Capacitive: the two measured edges from the corner, drawn as a rectangle of
    the board size (or 1.5x the probe spread when the board size is not set); disc: the circle
    through the three points; two points: the measured edge."""
    p = p or E.EdgeSeekParams()
    out: dict = {"outline": [], "points": list(res.points), "corner": res.corner,
                 "origin": res.origin, "angle_deg": res.angle_deg, "centre": res.centre}
    if mode == "disc" and res.centre is not None and res.points:
        cx, cy = res.centre
        r = sum(math.hypot(x - cx, y - cy) for x, y in res.points) / len(res.points)
        out["radius"] = r
        out["outline"] = [[(cx + r * math.cos(2 * math.pi * i / 96), cy + r * math.sin(2 * math.pi * i / 96))
                           for i in range(97)]]
        return out
    if mode == "two_point":
        if len(res.points) >= 2:
            (x1, y1), (x2, y2) = res.points[:2]
            out["outline"] = [[(x1, y1), (x2, y2)]]
        return out
    c = res.corner
    if c is None:
        return out
    try:
        sx, sy = p.directions()
    except E.EdgeSeekError:
        sx, sy = 1, 1
    a = res.angle
    u = (math.cos(a) * sx, math.sin(a) * sx)            # along the X edge, into the sheet
    v = (-math.sin(a) * sy, math.cos(a) * sy)           # along the Y edge, into the sheet

    def spread(axis_vec: Point) -> float:
        d = [abs((x - c[0]) * axis_vec[0] + (y - c[1]) * axis_vec[1]) for x, y in res.points]
        return max(d) if d else 0.0
    lx = p.board_x if p.board_x > 10.0 else max(50.0, 1.5 * spread(u))
    ly = p.board_y if p.board_y > 10.0 else max(50.0, 1.5 * spread(v))
    pts = [c, (c[0] + u[0] * lx, c[1] + u[1] * lx),
           (c[0] + u[0] * lx + v[0] * ly, c[1] + u[1] * lx + v[1] * ly),
           (c[0] + v[0] * ly, c[1] + v[1] * ly), c]
    out["outline"] = [pts]
    out["size"] = (lx, ly)
    return out


def apply_to_docs(docs: Any, res: E.EdgeSeekResult, label: str = "edge seek") -> None:
    """Rotate the drawing by the measured angle about the job origin: one undoable edit of the
    document's rotation state (FUN_00557d90 / FUN_004b0300; ``nc.compiler`` applies it)."""
    centre = res.origin or res.corner or (0.0, 0.0)
    docs.edit(lambda d: E.apply_to_document(d, res.angle, centre), label, parts=())


class EdgeSeekProcedure(Procedure):
    id = "edge_seek"
    min_mode = "operator"

    def __init__(self, machine: Any, mode: str = "cap4", params: Optional[E.EdgeSeekParams] = None,
                 values: Optional[dict] = None, apply: Optional[Callable[[E.EdgeSeekResult], None]] = None,
                 set_origin: Optional[Callable[[], Any]] = None, poll_s: float = 0.01, **kw: Any) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown edge-seek mode {mode!r}")
        self.mode = mode
        self.values = values if values is not None else {}
        self.params = copy.copy(params) if params is not None else E.EdgeSeekParams.from_values(self.values)
        if mode in ("cap3", "cap4"):
            self.params.mode = MODES[mode]
        self._apply = apply
        self._set_origin = set_origin
        self.set_origin_after = mode == "disc"
        self.disc_guess: Optional[Point] = None     # None: the head position (jogged into the disc)
        self.poll_s = poll_s
        self.result: Optional[E.EdgeSeekResult] = None
        self.points: dict[int, Point] = {}
        self.applied = False
        super().__init__(machine, **kw)
        self.title = TITLES[mode]

    # ------------------------------------------------------------------ steps
    def build_steps(self) -> list[Step]:
        cap = self.mode in ("cap3", "cap4")
        two = self.mode == "two_point"
        notes = ("wizard.safety.clear_area", "wizard.safety.abort", "wizard.edge.safety_sheet")
        return [
            Step("intro", "wizard.edge.intro_title", f"wizard.edge.intro_{self.mode}", "info",
                 safety=notes if not two else ("wizard.safety.clear_area", "wizard.safety.abort")),
            Step("params", "wizard.edge.params_title", "wizard.edge.params_text", "input",
                 skip=lambda p: two),
            Step("check", "wizard.check_title", "wizard.edge.check_text", "action", run=_check, auto=True),
            Step("measure", "wizard.edge.measure_title",
                 "wizard.edge.measure_cap" if cap else "wizard.edge.measure_disc", "action",
                 safety=("wizard.safety.motion",), run=_measure, motion=True,
                 action_label="wizard.edge.measure_button", skip=lambda p: two),
            Step("p1", "wizard.edge.p1_title", "wizard.edge.p1_text", "action", run=lambda p: p.record(1),
                 jog=True, action_label="wizard.edge.record_button", skip=lambda p: not two),
            Step("p2", "wizard.edge.p2_title", "wizard.edge.p2_text", "action", run=lambda p: p.record(2),
                 jog=True, action_label="wizard.edge.record_button", skip=lambda p: not two),
            Step("compute", "wizard.edge.compute_title", "", "action", run=_two_point, auto=True,
                 skip=lambda p: not two),
            Step("review", "wizard.edge.review_title", "wizard.edge.review_text", "confirm"),
            Step("apply", "wizard.edge.apply_title", "wizard.edge.apply_text", "action", run=_apply,
                 action_label="wizard.edge.apply_button", skip=lambda p: p.dry_run),
        ]

    # ------------------------------------------------------------------ helpers
    def record(self, n: int) -> Point:
        st = self.machine.state()
        if not st.connected:
            raise StepFailed("wizard.error.not_connected")
        pt = (float(st.x), float(st.y))
        self.points[n] = pt
        self.log(f"P{n} = ({pt[0]:.3f}, {pt[1]:.3f}) mm")
        return pt

    def outline(self) -> dict:
        if self.result is None:
            return {"outline": [], "points": []}
        return sheet_outline(self.result, self.params, self.mode)

    def on_abort(self) -> None:
        card = self.machine.card()
        if card is not None and self.mode != "two_point":
            try:                         # leave the follower's edge-seek mode (101 := 101)
                card.write_reg(E.F.REG_MAILBOX, E.CMD_CANCEL)
            except Exception:  # noqa: BLE001 - best effort, the halt already stopped the axes
                pass


def _check(p: EdgeSeekProcedure) -> dict:
    p.require_connected()
    warn = []
    if p.mode != "two_point":
        try:
            p.params.directions()
        except E.EdgeSeekError:
            raise StepFailed("wizard.edge.error.dock", dock=p.params.dock_type) from None
        if not p.dry_run and not int(float(p.values.get("ZF.ZFType", 1) or 0)):
            raise StepFailed("wizard.edge.error.no_follower")
        if p.machine.card() is None:
            raise StepFailed("wizard.error.not_connected")
    if not p.machine.state().homed:
        warn.append("wizard.warn.not_homed")
    return {"warnings": warn}


def _measure(p: EdgeSeekProcedure) -> E.EdgeSeekResult:
    p.operator_action("measure")
    card = p.machine.card()
    if card is None:
        raise StepFailed("wizard.error.not_connected")
    seeker = E.EdgeSeeker(card, p.params, log=p.log, cancel=p.cancel, poll_s=p.poll_s)
    try:
        res = seeker.run_disc(p.disc_guess, True) if p.mode == "disc" else seeker.run_capacitive(p.params.mode, None, True)
    except E.EdgeSeekError as e:
        if p.cancel.is_set():
            raise ProcedureAborted(p.id) from None
        raise StepFailed("wizard.edge.error.seek", detail=str(e)) from None
    p.check_cancel()
    p.result = res
    p.log(res.summary())
    return res


def _two_point(p: EdgeSeekProcedure) -> E.EdgeSeekResult:
    a, b = p.points.get(1), p.points.get(2)
    if a is None or b is None:
        raise StepFailed("wizard.edge.error.points")
    if math.hypot(b[0] - a[0], b[1] - a[1]) < 1.0:
        raise StepFailed("wizard.edge.error.too_close")
    ang = E.two_point_angle(a, b)
    p.result = E.EdgeSeekResult(mode=0, points=[a, b], corner=a, origin=a, angle=ang)
    return p.result


def _apply(p: EdgeSeekProcedure) -> bool:
    if p.result is None:
        raise StepFailed("wizard.edge.error.no_result")
    if p._apply is None:
        raise StepFailed("wizard.edge.error.no_document")
    p._apply(p.result)
    p.applied = True
    if p.set_origin_after and p._set_origin is not None:
        p._set_origin()
    return True

__all__ = ["EdgeSeekProcedure", "sheet_outline", "apply_to_docs", "MODES"]
