"""Colour contrast helpers for the themes (Qt-free, unit-tested).

* :func:`contrast` is the WCAG 2.x contrast ratio of two sRGB colours.
* :func:`adjust_for_background` keeps a colour's hue and saturation and moves only its
  lightness until it reaches ``min_ratio`` against the background (the smallest change that
  does).  The canvas uses it for the layer colours: the original's palette (bright green,
  cyan, yellow on black) stays as it is on the dark canvas, and is darkened just enough on the
  light one.
* :func:`distinct` checks that a palette stays distinguishable after the adjustment (the
  smallest pairwise CIE76 ΔE).
* :func:`separate_for_cvd` moves the lightness of colours that only differ by hue for a
  red-green colour-blind user (:func:`cvd_delta_e`, Machado et al. 2009) until they differ by
  lightness too.

WCAG asks for 3:1 for graphical objects (1.4.11) and 4.5:1 for text (1.4.3).
"""
from __future__ import annotations

import colorsys
import math
from typing import Optional


def _hex(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0


def to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


def _lin(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def luminance(c: str) -> float:
    r, g, b = _hex(c)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def adjust_for_background(color: str, background: str, min_ratio: float = 3.0) -> str:
    """``color`` with only its lightness changed so that it has at least ``min_ratio`` contrast
    against ``background`` (unchanged when it already has).  Darkens on a light background,
    lightens on a dark one."""
    if contrast(color, background) >= min_ratio:
        return color
    r, g, b = _hex(color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    darker = luminance(background) > 0.18
    lo, hi = (0.0, l) if darker else (l, 1.0)
    best = to_hex(colorsys.hls_to_rgb(h, lo if darker else hi, s))
    for _ in range(30):                      # bisection on the lightness
        mid = (lo + hi) / 2
        cand = to_hex(colorsys.hls_to_rgb(h, mid, s))
        if contrast(cand, background) >= min_ratio:
            best = cand
            if darker:
                lo = mid
            else:
                hi = mid
        else:
            if darker:
                hi = mid
            else:
                lo = mid
    return best


def _lab(c: str) -> tuple[float, float, float]:
    r, g, b = (_lin(v) for v in _hex(c))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    la, lb = _lab(a), _lab(b)
    return math.dist(la, lb)


def distinct(colors: list[str]) -> float:
    """The smallest pairwise ΔE (CIE76) of ``colors`` (inf for fewer than two)."""
    best = math.inf
    for i in range(len(colors)):
        for j in range(i + 1, len(colors)):
            best = min(best, delta_e(colors[i], colors[j]))
    return best


def adjust_palette(colors: list[str], background: str, min_ratio: float = 3.0,
                   min_delta_e: float = 12.0) -> list[str]:
    """:func:`adjust_for_background` for every colour, then keep the palette distinguishable:
    a colour that lands within ``min_delta_e`` of an earlier one (two greys darkened to the
    same grey) is pushed further, in steps of more contrast, until it is distinct."""
    out: list[str] = []
    for c in colors:
        ratio = min_ratio
        a = adjust_for_background(c, background, ratio)
        while out and min(delta_e(a, o) for o in out) < min_delta_e and ratio < 15.0:
            ratio += 1.0
            a = adjust_for_background(c, background, ratio)
        out.append(a)
    return out


# ------------------------------------------------------------------ colour vision deficiency
#: Machado, Oliveira & Fernandes (2009), severity 1.0, on linear RGB: how a deuteranope and a
#: protanope (together about 1 in 12 men) see a colour
CVD_MATRICES: dict[str, tuple[tuple[float, float, float], ...]] = {
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
}
VISIONS = ("normal", "deutan", "protan")


def _lab_lin(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = rgb
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def seen_lab(c: str, vision: str = "normal") -> tuple[float, float, float]:
    """CIE Lab of ``c`` as seen with ``vision`` (``normal``, ``deutan``, ``protan``)."""
    lin = tuple(_lin(v) for v in _hex(c))
    m = CVD_MATRICES.get(vision)
    if m is not None:
        lin = tuple(max(0.0, min(1.0, sum(m[i][j] * lin[j] for j in range(3)))) for i in range(3))
    return _lab_lin(lin)  # type: ignore[arg-type]


def cvd_delta_e(a: str, b: str) -> float:
    """The smallest ΔE (CIE76) of ``a`` and ``b`` over normal, deuteranope and protanope
    vision: how far apart the two are for everyone."""
    return min(math.dist(seen_lab(a, v), seen_lab(b, v)) for v in VISIONS)


def separate_for_cvd(colors: list[str], background: str, min_ratio: float = 3.0,
                     min_delta_e: float = 10.0, keep_normal: float = 12.0, rounds: int = 40) -> list[str]:
    """``colors`` with the lightness of some moved (hue and saturation kept) until every pair is
    at least ``min_delta_e`` apart for normal, deuteranope and protanope vision
    (:func:`cvd_delta_e`): red and green layers that differ by hue only look alike to a
    red-green colour-blind user; a difference of lightness survives.  The pair furthest below is
    taken first, and of its two colours the one that needs the smaller change moves; a colour
    keeps ``min_ratio`` contrast against ``background`` and ``keep_normal`` ΔE to every other
    colour for normal vision, and no other pair gets worse than the worst there is.  Stops when
    no step helps (the result is then as far apart as these rules allow)."""
    cols = list(colors)
    n = len(cols)
    base_l = [colorsys.rgb_to_hls(*_hex(c))[1] for c in cols]
    for _ in range(rounds):
        worst = min(((cvd_delta_e(cols[i], cols[j]), i, j) for i in range(n) for j in range(i + 1, n)),
                    default=None)
        if worst is None or worst[0] >= min_delta_e:
            break
        d0, i, j = worst
        best: Optional[tuple[float, int, str]] = None
        for k, other in ((i, j), (j, i)):
            h, _l, s = colorsys.rgb_to_hls(*_hex(cols[k]))
            for step in sorted(range(8, 95, 2), key=lambda v: abs(v / 100 - base_l[k])):
                c = to_hex(colorsys.hls_to_rgb(h, step / 100, s))
                if contrast(c, background) < min_ratio or cvd_delta_e(c, cols[other]) < min_delta_e:
                    continue
                if any(delta_e(c, cols[m]) < keep_normal or cvd_delta_e(c, cols[m]) < min(d0, min_delta_e)
                       for m in range(n) if m not in (k, other)):
                    continue
                cost = abs(step / 100 - base_l[k])
                if best is None or cost < best[0]:
                    best = (cost, k, c)
                break
        if best is None:
            break
        cols[best[1]] = best[2]
    return cols


__all__ = ["adjust_palette", "contrast", "luminance", "adjust_for_background", "delta_e", "distinct", "to_hex",
           "cvd_delta_e", "seen_lab", "separate_for_cvd", "VISIONS"]
