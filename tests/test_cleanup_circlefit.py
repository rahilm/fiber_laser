"""fit_arcs vs CircleFitDLL mainFit segmentation (goldens from the parity harness run of the
original CircleFitDLL.dll under Wine, parity/out/report.md, D-06)."""
import math

from mlaser.cam.cleanup import FitParams, fit_arcs
from mlaser.model import Arc, Line

ANG = math.pi / 10


def _counts(segs):
    c = {"L": 0, "A": 0, "I": 0}
    for s in segs:
        c["L" if isinstance(s, Line) else ("I" if s.start == s.end else "A")] += 1
    return c


def _sine():
    return [(i / 120 * 2 * math.pi * 10, 8 * math.sin(i / 120 * 2 * math.pi)) for i in range(121)]


def test_sine_matches_dll():
    assert _counts(fit_arcs(_sine(), FitParams(err=0.02, ang=ANG))) == {"L": 2, "A": 16, "I": 0}
    assert _counts(fit_arcs(_sine(), FitParams(err=0.1, ang=ANG))) == {"L": 0, "A": 12, "I": 0}


def test_ellipse_matches_dll():
    e = [(30 * math.cos(2 * math.pi * i / 120), 15 * math.sin(2 * math.pi * i / 120)) for i in range(121)]
    segs = fit_arcs(e, FitParams(err=0.02, ang=ANG))
    assert _counts(segs) == {"L": 0, "A": 28, "I": 0}
    # first DLL primitive: ('A', (22.45687640263356, -0.00320451028708813), 7.543124278045045, ...)
    a = segs[0]
    assert isinstance(a, Arc)
    assert math.dist(a.center, (22.45687640263356, -0.00320451028708813)) < 1e-9
    assert abs(a.radius - 7.543124278045045) < 1e-9
    for s, t in zip(segs, segs[1:]):
        assert math.dist(s.end, t.start) < 1e-9
    assert math.dist(segs[-1].end, e[-1]) < 1e-9


def test_sine_first_line_matches_dll():
    segs = fit_arcs(_sine(), FitParams(err=0.02, ang=ANG))
    # DLL: ('L', (0, 0), (3.6651914291880923, 2.866943596362402))
    assert isinstance(segs[0], Line)
    assert math.dist(segs[0].end, (3.6651914291880923, 2.866943596362402)) < 1e-9
