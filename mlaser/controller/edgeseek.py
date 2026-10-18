"""Edge seek (寻边, mf19): find the sheet with the capacitive follower and compute its rotation
and origin, so the job is rotated and offset to match the sheet.

Recovered from MainApp.exe (base 0x400000) and NCModule.dll (0x10000000); tags **O** observed,
**I** inferred, **U** unknown.  docs/protocol.md §16 has the full write-up.

Commands (MainApp FUN_004b0760, ids 0x10cd..0x10d3, O):

=========  =====================  ==================================================
0x10cd     newLang27 disc search   CDiscSeek dialog (FUN_0051db70) -> step list FUN_005c2060
0x10ce     A250211_0 capacitive    EdgeSeekAuto dialog (FUN_005529e0) -> FUN_005c0ad0(3|4)
0x10cf     A250211_1 two-point     CEdgeSeekDlg (FUN_005e17e0): mark P1/P2 with the head
0x10d0     A250211_2 memorised     shows cfg+0x4c60 in degrees (FUN_004b0990)
0x10d1     A250211_3 clear angle   cfg+0x4bc4 := 0, CAD +0x1bc (FUN_004b0420)
0x10d2     A250211_4 recover       cfg+0x4bc4 := 1, CAD +0x1b8(angle, 1, anchor) (FUN_004b0300)
0x10d3     A250211_5 recover+zero  FUN_005b7f20 (go to the corner) then recover
=========  =====================  ==================================================

The sequences run as a list of 9-word steps ``{type, a..h}`` (FUN_005bfed0, list at panel
+0x1f38) executed by the panel thread FUN_005c35f0.  The capacitive list (FUN_005c0bd0, O):

* per point: step 0xb absolute move to the point (speed MC.XFastMoveSpeed·ppm, acc
  MC.XFastMoveAcc), step 6 sleep 100 ms, step 2/0xb wait until the card is idle;
* step 0x15 (FUN_005b6a70): NC +0xe8 = write(101, 101) (cancel), then NC +0x110
  (FUN_10031cc0) = mailbox ``[117, ftol(ZFFollowSpeed·10), FollowHeight·1000, UpHeight·1000,
  Sensitivity, SlowTolerance·1000, 0, ftol(EdgeSeekOutSpeed·10), 1]``;
* step 2/0xd (FUN_005b6680): wait until follower reg 3 == 117 and reg 2 low byte == 3;
* step 9 (FUN_005bea00 -> NC +0x50 FUN_10032910): relative move ``[5, mask, v, a, 10a, dx, dy,
  0, 0]`` of 5000 mm away from the sheet at EdgeSeekOutSpeed (first 2 points along Y, the
  others along X; 3 for the 7-point mode);
* step 2/9 (FUN_005b6b30): poll follower reg 2 == 0x80000006 ("edge found"), then stop the axes
  (NC +0x64 ``[1, 0x1f]``), read follower reg 14/15 and the axis latch (axis RO k7) and store
  the edge point at cfg+0x4bc8+16·i.  The coordinate along the move is replaced by
  ``start + sign·EdgeSeekXYSlowSpeed·0.00025·reg15`` (DAT_008c48c8 = 0.00025 s per sample);
* step 2/0xb wait idle.
* After the points: step 0x29 (FUN_005b7f20) corner = intersection / projection, move to
  corner + offset; step 6 200 ms; wait; step 0x2a (angle and rotation, case 0x2a of
  FUN_005c35f0 at 0x5c58ab).
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from . import frames as F

Point = tuple[float, float]

# follower (on-card FTC) registers, MC address 10000 + i (devices.md §3)
ZF_BASE = F.REG_ONBZF
ZF_RUN_STATUS = 2              # bit 31 homed, low byte state
ZF_RUN_COMMAND = 3
ZF_EDGE_SAMPLES = 14           # "edge-seek samples" (ZFReadOnly label, devices.md)
ZF_EDGE_POINT = 15             # samples from the start of the move to the edge (I, see module doc)
ZF_EDGE_FOUND = 0x80000006     # O: FUN_005b6b30 / FUN_005b7950 compare with -0x7ffffffa
ZF_EDGE_ARMED_STATE = 3        # O: FUN_005b6680 waits for (reg2 & 0xff) == 3 ...
CMD_EDGE_SEEK = 117            # ... and reg3 == 0x75; mailbox op 0x75 (FUN_10031cc0)
CMD_FOLLOW = 104               # 0x68: the same wait with a zero argument (step 2/6)
CMD_CANCEL = 101               # NC +0xe8: write(101, 101)
SAMPLE_S = 0.00025             # DAT_008c48c8
SEEK_TRAVEL_MM = 5000.0        # DAT_0088e520 (capacitive), relative move length
DISC_TRAVEL_MM = 2000.0        # DAT_0084cc70 (disc search)
POINT_DELAY_S = 0.1            # step 6 with 100 ms (local_ec = 100)
FINAL_DELAY_S = 0.2            # step 6 with 0xc8
SECOND_LEFT_FACTOR = 1.5       # DAT_008c48d8: 4th left-edge point of the 7-point mode
ANGLE_MIN_DELTA_MM = 0.1       # DAT_0083d520: |dx| (|dy|) below this -> angle 0 (90°)
PERP_TOL_DEG = 0.02            # DAT_00833f00: correction when the edges are not square
APPLY_MIN_ANGLE = 0.0015       # DAT_0084cc38: below this the rotation counts as "none"

MODE_TWO_POINT = 0             # CEdgeSeekDlg sets cfg+0x4c48 = 0
MODE_3 = 3                     # 2 points on the X edge + 1 on the Y edge (projection)
MODE_4 = 4                     # 2 + 2 (line intersection)
MODE_7 = 7                     # 3 + 4 (robust fit)


class EdgeSeekError(RuntimeError):
    pass


@dataclass
class EdgeSeekParams:
    """GRP.* parameters of the "Edge seek" pages (ManuPara.xml, offsets of the MainApp block)."""
    sensitivity: int = 20             # GRP.EdgeSeekSensitivity +0x4578
    follow_height: float = 2.0        # GRP.EdgeSeekFollowHeight +0x4580 (mm)
    up_height: float = 30.0           # GRP.EdgeSeekUpHeight +0x4588 (mm)
    step_height: float = 0.4          # GRP.SlowEdgeSeekMoveOutTolerance +0x4598 ("Step height", mm)
    slow_speed: float = 50.0          # GRP.EdgeSeekXYSlowSpeed +0x45a8 (used for the sample->mm term)
    out_speed: float = 50.0           # GRP.EdgeSeekOutSpeed +0x47a0 (move-out speed, mm/s)
    board_x: float = 5.0              # GRP.EdgeBoardSizeX +0x4c68 (sheet size used for spacing)
    board_y: float = 5.0              # GRP.EdgeBoardSizeY +0x4c70
    auto_board_size: bool = False     # GRP.EdgeAutoBoardSize +0x4774: board size = drawing bbox
    x_percent: float = 0.5            # GRP.EdgeSeekXPercent +0x4790
    y_percent: float = 0.5            # GRP.EdgeSeekYPercent +0x4798
    offset_x: float = 5.0             # GRP.EdgeOffsetX +0x4c50 (origin inside the corner)
    offset_y: float = 5.0             # GRP.EdgeOffsetY +0x4c58
    mode: int = 3                     # GRP.EdgeSeekMode +0x4778 (3 or 4 points)
    current_start: bool = False       # GRP.EdgeSeekIsCurrentStart +0x477c
    start: Point = (0.0, 0.0)         # GRP.EdgeSeekStartPointX/Y +0x4780/+0x4788
    dock_type: int = 7                # MSC.DockPtType +0x497c (corner: 1, 3, 7, 9)
    zf_follow_speed: float = 200.0    # ZF.ZFFollowSpeed +0x4a70
    move_speed: float = 100.0         # MC.XFastMoveSpeed +0x4858
    move_acc: float = 4000.0          # MC.XFastMoveAcc +0x4868
    timeout_s: float = 120.0          # rebuild: give up waiting for the follower

    KEYS = {"sensitivity": "GRP.EdgeSeekSensitivity", "follow_height": "GRP.EdgeSeekFollowHeight",
            "up_height": "GRP.EdgeSeekUpHeight", "step_height": "GRP.SlowEdgeSeekMoveOutTolerance",
            "slow_speed": "GRP.EdgeSeekXYSlowSpeed", "out_speed": "GRP.EdgeSeekOutSpeed",
            "board_x": "GRP.EdgeBoardSizeX", "board_y": "GRP.EdgeBoardSizeY",
            "auto_board_size": "GRP.EdgeAutoBoardSize", "x_percent": "GRP.EdgeSeekXPercent",
            "y_percent": "GRP.EdgeSeekYPercent", "offset_x": "GRP.EdgeOffsetX",
            "offset_y": "GRP.EdgeOffsetY", "mode": "GRP.EdgeSeekMode",
            "current_start": "GRP.EdgeSeekIsCurrentStart", "dock_type": "MSC.DockPtType",
            "zf_follow_speed": "ZF.ZFFollowSpeed", "move_speed": "MC.XFastMoveSpeed",
            "move_acc": "MC.XFastMoveAcc"}

    @classmethod
    def from_values(cls, vals: Optional[dict]) -> "EdgeSeekParams":
        """From ``Machine.params`` (``{"GRP.EdgeSeekOutSpeed": 50.0, ...}``)."""
        p = cls()
        vals = vals or {}
        for attr, key in cls.KEYS.items():
            if key in vals and vals[key] not in (None, ""):
                cur = getattr(p, attr)
                try:
                    setattr(p, attr, type(cur)(float(vals[key])) if not isinstance(cur, bool)
                            else bool(int(float(vals[key]))))
                except (TypeError, ValueError):
                    pass
        sx, sy = vals.get("GRP.EdgeSeekStartPointX"), vals.get("GRP.EdgeSeekStartPointY")
        if sx is not None and sy is not None:
            p.start = (float(sx), float(sy))
        return p

    def directions(self) -> tuple[int, int]:
        """FUN_005c0bd0 switch on MSC.DockPtType (O): the corner the sheet is seeked at.

        Returns (sx, sy) = ``(EdgeSeekXDirection != 0)·2 − 1`` etc.: points are spaced towards
        +s and the head moves out of the sheet towards −s.  7 (bottom-left, the default) →
        (+1, +1); 1 (top-left) → (+1, −1); 3 (top-right) → (−1, −1); 9 (bottom-right) → (−1, +1).
        Any other dock type is refused with a message box (DAT_008c3c8c)."""
        table = {1: (1, 0), 3: (0, 0), 7: (1, 1), 9: (0, 1)}
        if self.dock_type not in table:
            raise EdgeSeekError(f"edge seek needs a corner dock point (1, 3, 7, 9), not {self.dock_type}")
        dx, dy = table[self.dock_type]
        return (1 if dx else -1, 1 if dy else -1)


@dataclass
class SeekPoint:
    """One probe of the capacitive sequence: start position and the direction of the move."""
    start: Point
    axis: int          # 0 = move along X (finds an edge parallel to Y), 1 = along Y
    sign: int          # direction of the move-out (+1 / −1)


def plan_points(start: Point, n: int, p: EdgeSeekParams) -> list[SeekPoint]:
    """Probe points of FUN_005c0bd0 (O, asm 0x5c0e32..0x5c11d9 and the loop at 0x5c1427).

    ``dx = BoardSizeX·XPercent·sx``, ``dy = BoardSizeY·YPercent·sy``.  3/4 points:
    (x0, y0), (x0+dx, y0) moving along Y, then (x0, y0), (x0, y0+dy) moving along X.  7 points:
    (x0, y0), (x0+dx/2, y0), (x0+dx, y0) along Y, then (x0, y0), (x0, y0+dy/2), (x0, y0+dy),
    (x0, y0+1.5·dy) along X.  The move-out is ``−5000·s`` mm (5000 = DAT_0088e520).  The sign of
    the third 7-mode probe uses sx in the original although it moves along Y (kept)."""
    sx, sy = p.directions()
    x0, y0 = start
    dx = p.board_x * p.x_percent * sx
    dy = p.board_y * p.y_percent * sy
    if n == MODE_7:
        pts = [(x0, y0), (x0 + 0.5 * dx, y0), (x0 + dx, y0), (x0, y0), (x0, y0 + 0.5 * dy),
               (x0, y0 + dy), (x0, y0 + SECOND_LEFT_FACTOR * dy)]
    elif n in (MODE_3, MODE_4):
        pts = [(x0, y0), (x0 + dx, y0), (x0, y0), (x0, y0 + dy)][:n]
    else:
        raise EdgeSeekError(f"unsupported point count {n}")
    n_y = 3 if n == MODE_7 else 2
    out = []
    for i, q in enumerate(pts):
        sign = sy if i < 2 else sx
        out.append(SeekPoint(q, 1 if i < n_y else 0, -sign))
    return out


# --------------------------------------------------------------------------- geometry (O)
def line_intersection(p0: Point, p1: Point, p2: Point, p3: Point) -> Optional[Point]:
    """FUN_00475b60: intersection of line p0-p1 with line p2-p3 (None when parallel)."""
    a = p0[0] * p1[1] - p1[0] * p0[1]
    b = p2[0] * p3[1] - p3[0] * p2[1]
    d = (p1[1] - p0[1]) * (p2[0] - p3[0]) - (p3[1] - p2[1]) * (p0[0] - p1[0])
    if d == 0.0:
        return None
    return ((a * (p2[0] - p3[0]) - b * (p0[0] - p1[0])) / d,
            ((p1[1] - p0[1]) * b - (p3[1] - p2[1]) * a) / d)


def project_on_line(p0: Point, p1: Point, q: Point) -> Optional[Point]:
    """FUN_00475c10: foot of the perpendicular from q onto line p0-p1."""
    ux, uy = p1[0] - p0[0], p1[1] - p0[1]
    n = ux * ux + uy * uy
    if n == 0.0:
        return None
    t = (uy * (q[1] - p0[1]) + ux * (q[0] - p0[0])) / n
    return (t * ux + p0[0], t * uy + p0[1])


def fit_line(pts: Sequence[Point]) -> Optional[tuple[float, float]]:
    """FUN_00475800: least squares ``y = k·x + b``; None when |n·Σx² − (Σx)²| < 1e-5."""
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    d = n * sxx - sx * sx
    if abs(d) < 1e-5:
        return None
    return ((n * sxy - sx * sy) / d, (sxx * sy - sx * sxy) / d)


def _angle_to(v: Point) -> float:
    """FUN_0055a090((1, 0), v): signed angle from +X to v, (−π, π]."""
    if math.hypot(*v) < 1e-8:
        return 0.0
    return math.atan2(v[1], v[0])


def edge_angle(points: Sequence[Point], mode: int) -> float:
    """Sheet rotation (radians), case 0x2a of FUN_005c35f0 (O, asm 0x5c58dc..0x5c5b7e) and
    CEdgeSeekDlg FUN_005e30c0 (same code).

    ``v = p1 − p0`` (negated when v.x < 0); ``a = atan2(v)`` when |v.x| > 0.1, else 0.  In the
    4-point mode also ``w = p3 − p2`` (negated when w.y < 0), ``b = atan2(w)`` (π/2 when
    |w.y| ≤ 0.1); when the two edges are more than 0.02° from square, half the difference is
    added: ``a += ((b − a) − π/2)·0.5``."""
    v = (points[1][0] - points[0][0], points[1][1] - points[0][1])
    if v[0] < 0:
        v = (-v[0], -v[1])
    a = _angle_to(v) if abs(v[0]) > ANGLE_MIN_DELTA_MM else 0.0
    if mode == MODE_4 and len(points) >= 4:
        w = (points[3][0] - points[2][0], points[3][1] - points[2][1])
        if w[1] < 0:
            w = (-w[0], -w[1])
        b = _angle_to(w) if abs(w[1]) > ANGLE_MIN_DELTA_MM else math.pi / 2
        err = (b - a) - math.pi / 2
        if abs(err) * 180.0 / math.pi > PERP_TOL_DEG:
            a += err * 0.5
    return a


def corner_point(points: Sequence[Point], mode: int) -> Optional[Point]:
    """FUN_005b7f20 (O): mode 4 → intersection of p0-p1 with p2-p3 (FUN_00475b60); mode 3 →
    projection of p2 onto p0-p1 (FUN_00475c10); mode 7 → :func:`corner_point_7`."""
    if mode == MODE_4:
        return line_intersection(points[0], points[1], points[2], points[3])
    if mode == MODE_3:
        return project_on_line(points[0], points[1], points[2])
    if mode == MODE_7:
        return corner_point_7(points)
    return None


def corner_point_7(points: Sequence[Point]) -> Optional[Point]:
    """7-point mode (mode 7 branch of FUN_005b7f20, partly I).

    O: the four left-edge points (3..6) are rotated by −π/4 about point 3 (FUN_005d42b0), each
    leave-one-out triple is fitted with FUN_00475800 and the fit with the smallest residual sum
    (FUN_00475900 distances) is kept; the bottom edge is fitted through the two of the three
    bottom points that agree best.  I: the rebuild then intersects the two fitted lines (the
    original's final projection FUN_00475a90 does the same for exact data)."""
    left = list(points[3:7])
    bottom = list(points[0:3])

    def best_line(pts):
        best = None
        for skip in range(len(pts)):
            sub = [q for i, q in enumerate(pts) if i != skip]
            fit = _fit_any(sub)
            if fit is None:
                continue
            res = sum(_dist_line(fit, q) for q in sub)
            if best is None or res < best[0]:
                best = (res, fit)
        return best[1] if best else None

    lb, ll = best_line(bottom), best_line(left)
    if lb is None or ll is None:
        return None
    return line_intersection(lb[0], lb[1], ll[0], ll[1])


def _fit_any(pts: Sequence[Point]) -> Optional[tuple[Point, Point]]:
    """Least-squares line through pts as two points (works for vertical lines too)."""
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - cx) ** 2 for p in pts)
    syy = sum((p[1] - cy) ** 2 for p in pts)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in pts)
    if sxx + syy < 1e-12:
        return None
    th = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return ((cx, cy), (cx + math.cos(th), cy + math.sin(th)))


def _dist_line(line: tuple[Point, Point], q: Point) -> float:
    (x0, y0), (x1, y1) = line
    return abs((x1 - x0) * (y0 - q[1]) - (x0 - q[0]) * (y1 - y0)) / math.hypot(x1 - x0, y1 - y0)


def circle_centre(p1: Point, p2: Point, p3: Point) -> Optional[Point]:
    """Disc search centre (FUN_005b8ef0, O: circumcentre of the three edge points; the
    original solves it in 3D with the follower Z, the rebuild uses the XY projection)."""
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    return ((a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d,
            (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d)


def two_point_angle(p1: Point, p2: Point) -> float:
    """CEdgeSeekDlg (FUN_005e2d20 / FUN_005e2ef0 store the head position as P1 / P2,
    FUN_005e30c0 computes the angle with mode 0 = :func:`edge_angle` on two points)."""
    return edge_angle([p1, p2], MODE_TWO_POINT)


def origin_from_corner(corner: Point, p: EdgeSeekParams) -> Point:
    """Target of the move after step 0x29 (O, asm 0x5b8914..0x5b8a55):
    ``(sx'·EdgeOffsetX + corner.x, sy'·EdgeOffsetY + corner.y)`` with
    ``sx' = (EdgeSeekXDirection == 1)·2 − 1`` (the same signs as :meth:`directions`)."""
    sx, sy = p.directions()
    return (corner[0] + sx * p.offset_x, corner[1] + sy * p.offset_y)


# --------------------------------------------------------------------------- result
@dataclass
class EdgeSeekResult:
    mode: int
    points: list[Point] = field(default_factory=list)   # cfg+0x4bc8 + 16·i
    corner: Optional[Point] = None                      # cfg+0x4c38 ("cross point", mp88_4)
    origin: Optional[Point] = None                      # corner + offsets (head goes there)
    angle: float = 0.0                                  # cfg+0x4c60, radians
    centre: Optional[Point] = None                      # disc search: cfg+0x4cc0

    @property
    def angle_deg(self) -> float:
        return math.degrees(self.angle)

    def summary(self) -> str:
        s = f"edge seek mode {self.mode}: {len(self.points)} points"
        if self.corner:
            s += f", cross point ({self.corner[0]:.3f}, {self.corner[1]:.3f})"
        if self.centre:
            s += f", centre ({self.centre[0]:.3f}, {self.centre[1]:.3f})"
        return s + f", angle {self.angle_deg:.3f}°"


def apply_to_document(doc, angle: float, centre: Point, enabled: bool = True,
                      offset: Point = (0.0, 0.0)) -> None:
    """Store the sheet rotation in the document's whole-drawing rotation state (the .chf
    trailer ``rotated / angle / centre / offset``, docs/formats.md §2.1; CADModule this+0x9d00
    / +0x9d10).  MainApp FUN_004b0300 calls CAD +0x1b8(cfg+0x4c60, cfg+0x4bc4, cfg+0x4980) with
    the anchor point +0x4980 as the centre (O).  ``mlaser.nc.compiler.compile_job`` applies it."""
    doc.extra["rotated"] = bool(enabled)
    doc.extra["rotate_angle"] = float(angle)
    doc.extra["rotate_center"] = (float(centre[0]), float(centre[1]))
    doc.extra["rotate_offset"] = (float(offset[0]), float(offset[1]))


def clear_document(doc) -> None:
    """A250211_3 "Clear the seeking angle" (FUN_004b0420): cfg+0x4bc4 := 0 (the angle value is
    kept, so A250211_4 can recover it)."""
    doc.extra["rotated"] = False


# --------------------------------------------------------------------------- the sequence
class EdgeSeeker:
    """Runs the capacitive / disc sequences on a :class:`~mlaser.controller.client.MotionController`.

    Positions come from axis RO k2 / position ppm (NC +0x1ac FUN_1002b900), moves are the
    relative mailbox move ``[5, mask, v, a, 10a, dx, dy, 0, 0]`` (FUN_10032910) and waits poll
    the card like the panel thread (30 ms sleeps, O)."""

    def __init__(self, mc, params: Optional[EdgeSeekParams] = None, ppm: Optional[float] = None,
                 x_axis: int = 0, y_axis: int = 1, z_axis: int = 2, poll_s: float = 0.03,
                 log: Optional[Callable[[str], None]] = None, cancel: Optional[threading.Event] = None):
        self.mc = mc
        self.p = params or EdgeSeekParams()
        self.ppm = float(ppm or mc.pulses_per_mm)
        self.ax, self.ay, self.az = x_axis, y_axis, z_axis
        self.poll_s = poll_s
        self.log = log or (lambda s: None)
        self.cancel = cancel or threading.Event()

    # ---- card access
    def position(self) -> Point:
        st = self.mc.read_status()
        return (st.axes[self.ax].position / self.ppm, st.axes[self.ay].position / self.ppm)

    def zf(self) -> list[int]:
        return self.mc.read_regs(ZF_BASE, F.N_ONBZF)

    def _wait(self, cond: Callable[[], bool], what: str, timeout: Optional[float] = None) -> None:
        t_end = time.monotonic() + (timeout or self.p.timeout_s)
        while not cond():
            if self.cancel.is_set():
                raise EdgeSeekError("edge seek cancelled")
            if time.monotonic() > t_end:
                raise EdgeSeekError(f"edge seek: timeout waiting for {what}")
            time.sleep(self.poll_s)

    def wait_idle(self) -> None:
        """Step 2/0xb: NC +0x280(3) then +0x230 == 0 (no axis busy)."""
        self._wait(lambda: not any(a.busy for a in self.mc.read_status().axes), "idle")

    def move_rel(self, dx: float, dy: float, speed: float) -> None:
        """Step 9 / FUN_005bea00: relative multi-axis move (counts, counts/s).  The acceleration
        word is ftol(MC.XFastMoveAcc), not scaled by ppm, as in every panel step-9 move (the
        machine-level capture: 4000; protocol.md §11a)."""
        p = self.ppm
        self.mc.move((1 << self.ax) | (1 << self.ay), F.ftol(speed * p), F.ftol(self.p.move_acc),
                     [F.ftol(dx * p), F.ftol(dy * p)])

    def move_to(self, x: float, y: float) -> None:
        """Step 0xb: absolute move = relative move by target − current (case 0xb of FUN_005c35f0)."""
        cx, cy = self.position()
        self.move_rel(x - cx, y - cy, self.p.move_speed)

    def arm(self) -> None:
        """Step 0x15 (FUN_005b6a70 → NC +0xe8, NC +0x110 FUN_10031cc0)."""
        p = self.p
        self.mc.write_reg(F.REG_MAILBOX, CMD_CANCEL)
        self.mc.command(edge_seek_command(p))

    def wait_armed(self) -> None:
        """Step 2/0xd (FUN_005b6680 with a non-zero argument → 0x75)."""
        def ok():
            z = self.zf()
            return (z[ZF_RUN_STATUS] & 0xFF) == ZF_EDGE_ARMED_STATE and z[ZF_RUN_COMMAND] == CMD_EDGE_SEEK
        self._wait(ok, "the follower to enter edge-seek mode", 10.0)

    def capture(self, start: Point, sp: SeekPoint) -> Point:
        """Step 2/9 (FUN_005b6b30, O): wait for 0x80000006, stop, latch, corrected point."""
        self._wait(lambda: self.zf()[ZF_RUN_STATUS] == ZF_EDGE_FOUND, "the edge")
        self.mc.stop_axes(0x1F)                                  # NC +0x64 [1, 0x1f]
        z = self.zf()
        lx = F.s32(self.mc.read_reg(F.REG_AXIS_RO + self.ax * 10 + F.AX_EDGE_LATCH)) / self.ppm
        ly = F.s32(self.mc.read_reg(F.REG_AXIS_RO + self.ay * 10 + F.AX_EDGE_LATCH)) / self.ppm
        travel = self.p.slow_speed * SAMPLE_S * z[ZF_EDGE_POINT]
        dvx, dvy = lx - start[0], ly - start[1]
        if abs(dvy) <= abs(dvx):          # moved along X
            sgn = 1 if dvx > 1e-9 else (-1 if dvx < -1e-9 else 0)
            return (start[0] + sgn * travel, ly)
        sgn = 1 if dvy > 1e-9 else (-1 if dvy < -1e-9 else 0)
        return (lx, start[1] + sgn * travel)

    # ---- sequences
    def run_capacitive(self, n: Optional[int] = None, start: Optional[Point] = None,
                       go_to_origin: bool = True) -> EdgeSeekResult:
        """FUN_005c0ad0(n) → FUN_005c0bd0 (n = 3 / 4 / 7 points)."""
        n = n or self.p.mode
        if start is None:
            start = self.position() if self.p.current_start else self.p.start
        plan = plan_points(start, n, self.p)
        res = EdgeSeekResult(mode=n)
        for i, sp in enumerate(plan):
            if i > 0 or not self.p.current_start:
                self.move_to(*sp.start)
                time.sleep(POINT_DELAY_S)
                self.wait_idle()
            self.arm()
            self.wait_armed()
            pos0 = self.position()                      # case 2/6: cfg+0xb4b8 = NC +0x1ac
            d = SEEK_TRAVEL_MM * sp.sign
            self.move_rel(d if sp.axis == 0 else 0.0, d if sp.axis == 1 else 0.0, self.p.out_speed)
            q = self.capture(pos0, sp)
            res.points.append(q)
            self.log(f"mp88_3: Edge Seek Step {i + 1} Pos, ({q[0]:.3f}, {q[1]:.3f})")
            self.wait_idle()
        self.mc.write_reg(F.REG_MAILBOX, CMD_CANCEL)
        res.corner = corner_point(res.points, n)
        if res.corner is None:
            raise EdgeSeekError("edge seek: the edges are parallel, no corner")
        res.origin = origin_from_corner(res.corner, self.p)
        res.angle = edge_angle(res.points, n)
        self.log(f"mp88_4: Edge Seek Cross Point Pos, ({res.corner[0]:.3f}, {res.corner[1]:.3f})")
        if go_to_origin:
            self.move_to(*res.origin)
            time.sleep(FINAL_DELAY_S)
            self.wait_idle()
        return res

    def run_disc(self, centre_guess: Optional[Point] = None, go_to_centre: bool = True) -> EdgeSeekResult:
        """Disc search FUN_005c2060 (O): from the guess move out −X, +X, −Y by 2000 mm, capture
        with FUN_005b7950 (latched XYZ, no correction), centre = circumcentre (FUN_005b8ef0),
        then move there (step 0x43) and apply (step 0x44)."""
        c = centre_guess or self.position()
        res = EdgeSeekResult(mode=-1)
        for i, (dx, dy) in enumerate(((-DISC_TRAVEL_MM, 0.0), (DISC_TRAVEL_MM, 0.0), (0.0, -DISC_TRAVEL_MM))):
            self.move_to(*c)
            time.sleep(POINT_DELAY_S)
            self.wait_idle()
            self.arm()
            self.wait_armed()
            self.move_rel(dx, dy, self.p.out_speed)
            self._wait(lambda: self.zf()[ZF_RUN_STATUS] == ZF_EDGE_FOUND, "the disc edge")
            self.mc.stop_axes(0x1F)
            lx = F.s32(self.mc.read_reg(F.REG_AXIS_RO + self.ax * 10 + F.AX_EDGE_LATCH)) / self.ppm
            ly = F.s32(self.mc.read_reg(F.REG_AXIS_RO + self.ay * 10 + F.AX_EDGE_LATCH)) / self.ppm
            res.points.append((lx, ly))
            self.log(f"newLang34: Edge search coordinates {i + 1} are ({lx:.3f}, {ly:.3f})")
            self.wait_idle()
        self.mc.write_reg(F.REG_MAILBOX, CMD_CANCEL)
        res.centre = circle_centre(*res.points)
        if res.centre is None:
            raise EdgeSeekError("disc search: the three points are collinear")
        res.corner = res.origin = res.centre
        self.log(f"mp88_4: Edge Seek Cross Point Pos, ({res.centre[0]:.3f}, {res.centre[1]:.3f})")
        if go_to_centre:
            self.move_to(*res.centre)
            time.sleep(FINAL_DELAY_S)
            self.wait_idle()
        return res


def edge_seek_command(p: EdgeSeekParams) -> list[int]:
    """Mailbox of NC +0x110 (FUN_10031cc0, O): ``[117, ftol(v·10), b, c, d, e, f, ftol(g·10), h]``
    with the step 0x15 fields of FUN_005c0bd0: v = ftol(ZFFollowSpeed), b = FollowHeight·1000,
    c = UpHeight·1000, d = Sensitivity, e = StepHeight·1000, f = 0, g = EdgeSeekOutSpeed
    (cfg+0x47a0, read in FUN_005b6a70), h = 1."""
    return [CMD_EDGE_SEEK, F.ftol(F.ftol(p.zf_follow_speed) * 10.0), F.ftol(p.follow_height * 1000),
            F.ftol(p.up_height * 1000), int(p.sensitivity), F.ftol(p.step_height * 1000), 0,
            F.ftol(p.out_speed * 10.0), 1]
