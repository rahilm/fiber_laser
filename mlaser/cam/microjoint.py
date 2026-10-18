"""Micro-joints (bridges, lang "微连"), port of CADModule 0x1006c930 / 0x10071180 / 0x100673f0.

Storage on the contour: mode (0x114, 1 = active), centres c_i as normalised
path parameters (0x118) and joint lengths L_i in mm (0x128).  In the .chf file
the block is (oddly) named "<PWM Control>".

* Interval of a joint: [c - L/(2*length), c + L/(2*length)], clamped to [0,1];
  overlapping neighbours are merged (0x1006c930).
* Auto placement (0x10071180): skip if max(bbox) < max(2, 5L, min_size);
  n = count or floor(R/spacing) - 1; if n*L > R/2 then L = R/(2n);
  closed contours: c_k = k/(n+1), k = 1..n; open with "joint at head": c_k = k/n.
* At output (0x100673f0) intervals are rebased by the start parameter and
  mapped onto the whole toolpath (lead-in + contour + overcut) as
  (u*length + lead_in_len)/total; inside a joint the per-sample laser duty is 0
  (the head keeps moving at cut speed with the beam off).

Here positions are expressed in **mm of arc length from the contour start**
(model.Craft.micro_joints), lengths in ``Craft.extra['micro_joint_len']``
(default 0.5 mm) or per joint as (pos, len) tuples.
"""
from __future__ import annotations

import math
from typing import Iterable

from ..model import Contour, Segment
from ..geom.contour import contour_bbox, sub_path

DEFAULT_JOINT_LEN = 0.5


def joint_intervals(length: float, joints: Iterable, default_len: float = DEFAULT_JOINT_LEN,
                    closed: bool = True) -> list[tuple[float, float]]:
    """Merged [a, b] arc-length intervals (mm) of the joints."""
    iv = []
    for j in joints:
        if isinstance(j, (tuple, list)):
            c, L = float(j[0]), float(j[1])
        else:
            c, L = float(j), default_len
        if L <= 0 or length <= 0:
            continue
        if closed:
            c %= length
            a, b = c - L / 2, c + L / 2
            if a < 0:                       # straddles the start: wrap
                iv.append((length + a, length))
            if b > length:
                iv.append((0.0, b - length))
            a, b = max(a, 0.0), min(b, length)
        else:
            a, b = max(c - L / 2, 0.0), min(c + L / 2, length)
        if b > a:
            iv.append((a, b))
    iv.sort()
    merged: list[tuple[float, float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            pa, pb = merged[-1]
            merged[-1] = (pa, max(pb, b))
        else:
            merged.append((a, b))
    return merged


def auto_joints(c: Contour, count: int = 0, spacing: float = 0.0, joint_len: float = DEFAULT_JOINT_LEN,
                min_size: float = 0.0, joint_at_head: bool = False) -> list[tuple[float, float]]:
    """Evenly spaced joints along the path (count, or spacing in mm). Returns (pos_mm, len)."""
    x0, y0, x1, y1 = contour_bbox(c)
    if max(x1 - x0, y1 - y0) < max(2.0, 5 * joint_len, min_size):
        return []
    R = c.length()
    n = count if count > 0 else (int(math.floor(R / spacing)) - 1 if spacing > 0 else 0)
    if n <= 0:
        return []
    L = joint_len
    if n * L > R / 2:
        L = R / (2 * n)
    if c.closed:
        return [(R * k / (n + 1), L) for k in range(1, n + 1)]
    if joint_at_head:
        return [(R * k / n, L) for k in range(1, n + 1)]
    return [(R * k / (n + 1), L) for k in range(1, n + 1)]


def split_by_joints(c: Contour, intervals: list[tuple[float, float]], d0: float = 0.0,
                    d1: float | None = None) -> list[tuple[Segment, bool]]:
    """Path between arc lengths d0..d1 (wrap allowed for closed contours) as
    (segment, laser_on) pieces.  Joint intervals are in [0, length) and repeat
    every lap on closed contours."""
    L = c.length()
    if d1 is None:
        d1 = L
    cuts = [d0, d1]
    laps = int(math.ceil(d1 / L)) + 1 if (c.closed and L > 0) else 1
    spans = []
    for lap in range(laps):
        for a, b in intervals:
            spans.append((a + lap * L, b + lap * L))
            cuts += [a + lap * L, b + lap * L]
    cuts = sorted({x for x in cuts if d0 <= x <= d1})
    out: list[tuple[Segment, bool]] = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 1e-9:
            continue
        mid = (a + b) / 2
        on = not any(sa <= mid <= sb for sa, sb in spans)
        for s in sub_path(c, a, b):
            out.append((s, on))
    return out
