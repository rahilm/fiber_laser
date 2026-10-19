"""The prepared-contour cache of build_toolpath (cam/toolpath.prepare_cached) never changes
the toolpath: a second build, and builds after in-place edits, equal an uncached build."""
from __future__ import annotations

from pathlib import Path

import pytest

from mlaser.cam import toolpath as T
from mlaser.cam.toolpath import CamParams, build_toolpath
from mlaser.model import Arc, Contour, Craft, Document, LeadParams, Line, Part

DATA = Path(__file__).parent / "data"


def _uncached(doc, prm):
    old = T.PREPARE_CACHE
    T.PREPARE_CACHE = False
    try:
        return build_toolpath(doc, prm)
    finally:
        T.PREPARE_CACHE = old


def _rect(x, y, w, h):
    p = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(p[i], p[(i + 1) % 4]) for i in range(4)], True)


def _doc():
    parts = []
    for i in range(6):
        s = (10.0 * i + 7, 5.0)
        parts.append(Part([Contour([Arc(s, s, (10.0 * i + 5, 5.0), True)], True), _rect(10.0 * i, 0, 9, 9)],
                          Craft(lead_in=LeadParams(kind=1, length=1.0), micro_joints=[3.0])))
    return Document(parts=parts)


@pytest.mark.parametrize("mode", [0, 5, 1])
def test_cached_builds_equal_uncached(mode):
    doc, prm = _doc(), CamParams(sort_mode=mode)
    ref = _uncached(doc, prm)
    assert build_toolpath(doc, prm) == ref
    assert build_toolpath(doc, prm) == ref          # hits
    doc.parts[2].craft.lead_in.length = 2.5          # in-place craft edit
    doc.parts[3].contours[1].segments[0] = Line((30.0, 0.0), (39.5, 0.0))   # geometry edit
    doc.parts[3].contours[1].segments[1].start = (39.5, 0.0)                # in place
    doc.parts[4].craft.kerf_comp, doc.parts[4].craft.kerf_width = 1, 0.3
    assert build_toolpath(doc, prm) == _uncached(doc, prm)
    prm2 = CamParams(sort_mode=mode, start_mode=0)
    assert build_toolpath(doc, prm2) == _uncached(doc, prm2)


def test_cached_samples_equal_uncached():
    from mlaser.formats.chf import load_chf
    files = sorted((DATA / "chf").glob("*.chf"))[:4]
    if not files:
        pytest.skip("no sample .chf")
    for f in files:
        doc = load_chf(str(f))
        ref = _uncached(doc, CamParams())
        assert build_toolpath(doc, CamParams()) == ref
        assert build_toolpath(doc, CamParams()) == ref
