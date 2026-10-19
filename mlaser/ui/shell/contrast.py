"""Colour contrast helpers for the themes (Qt-free, unit-tested).

* :func:`contrast` is the WCAG 2.x contrast ratio of two sRGB colours.
* :func:`adjust_for_background` keeps a colour's hue and saturation and moves only its
  lightness until it reaches ``min_ratio`` against the background (the smallest change that
  does).  The canvas uses it for the layer colours: the original's palette (bright green,
  cyan, yellow on black) stays as it is on the dark canvas, and is darkened just enough on the
  light one.
* :func:`distinct` checks that a palette stays distinguishable after the adjustment (the
  smallest pairwise CIE76 ΔE).

WCAG asks for 3:1 for graphical objects (1.4.11) and 4.5:1 for text (1.4.3).
"""
from __future__ import annotations

import colorsys
import math


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


__all__ = ["adjust_palette", "contrast", "luminance", "adjust_for_background", "delta_e", "distinct", "to_hex"]
