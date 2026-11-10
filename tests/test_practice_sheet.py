"""The practice sheet itself (mlaser.services.practice.practice_document), without the simulator.

tests/test_practice.py runs the lessons that cut the sheet in real time on a copy scaled by
``SHEET_SCALE`` (the lesson is under test there, not the sheet's size): the real sheet still
compiles to a job, and the scaled copy is the same sheet (parts, contours, layers, the practice
mark) with every length scaled."""
from __future__ import annotations

import pytest

from mlaser.nc.compiler import compile_job
from mlaser.services import practice as PR

from test_practice import SHEET_SCALE, small_practice_document


def _shape(doc):
    return ([(p.name, p.craft.layer, [(len(c.segments), c.closed) for c in p.contours]) for p in doc.parts],
            sorted(doc.layers), doc.layers[1].speed, doc.layers[1].power_pct)


def test_the_real_practice_sheet_compiles_to_a_job():
    doc = PR.practice_document()
    assert PR.is_practice_document(doc)
    job = compile_job(doc)
    assert job.steps > 0 and job.laser_on_length_mm > 0
    assert doc.layers[1].speed == PR.PRACTICE_SPEED


def test_the_lessons_test_sheet_is_the_practice_sheet_scaled():
    real, small = PR.practice_document(), small_practice_document()
    assert PR.is_practice_document(small) and _shape(small) == _shape(real)
    a, b = compile_job(real), compile_job(small)
    assert b.laser_on_length_mm == pytest.approx(a.laser_on_length_mm * SHEET_SCALE, rel=0.02)
    bx = [p.bbox() for p in real.parts]
    sx = [p.bbox() for p in small.parts]
    for r, s in zip(bx, sx):
        assert s == pytest.approx(tuple(v * SHEET_SCALE for v in r), abs=1e-3)   # arcs: bbox of sampled points
