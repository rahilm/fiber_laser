"""mlaser.nc: sampling, pulse quantisation and wire encoding."""
from __future__ import annotations

import math

import pytest

from mlaser.toolpath import items as I
from mlaser.protocol import frames as F
from mlaser.model import Arc, Contour, Craft, Document, LayerParams, LeadParams, Line, Part
from mlaser.nc import Machine, compile_items, compile_job, frame_items


def _rect(x, y, w, h):
    pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return Contour([Line(pts[i], pts[(i + 1) % 4]) for i in range(4)], True)


def _doc():
    s = (60.0, 20.0)
    parts = [Part([_rect(0, 0, 80, 40)], Craft(layer=1, lead_in=LeadParams(kind=1, length=2))),
             Part([_rect(10, 10, 20, 20)], Craft(layer=1)),
             Part([Contour([Arc(s, s, (50.0, 20.0), True)], True)], Craft(layer=2))]
    return Document(parts, {1: LayerParams(1, speed=80, power_pct=60, pierce={"time_s": 0.05}),
                            2: LayerParams(2, speed=40, power_pct=30)})


def test_sum_of_deltas_matches_final_position():
    m = Machine(pulses_per_mm_x=1000.0, pulses_per_mm_y=800.0)
    job = compile_job(_doc(), {"return_to_origin": True, "origin": (5.0, -3.0)}, m)
    d = job.step_deltas()
    sx, sy = sum(a[0] for a in d), sum(a[1] for a in d)
    assert (job.start_pulses[0] + sx, job.start_pulses[1] + sy) == job.end_pulses
    # FUN_00473bd0 truncates with the remainder carried (parity JB-01): the card ends within one
    # pulse of the nominal end (here 4999 = one pulse short), the remainder is Job.pulse_carry.
    # end + carry is the nominal end up to the sub-tick gaps after each run's last sample
    # (no closing step), far below 0.01 pulse
    ex, ey = 5.0 * 1000, -3.0 * 800
    assert abs(job.end_pulses[0] - ex) <= 1 and abs(job.end_pulses[1] - ey) <= 1
    assert job.end_pulses[0] + job.pulse_carry[0] == pytest.approx(ex, abs=0.01)
    assert job.end_pulses[1] + job.pulse_carry[1] == pytest.approx(ey, abs=0.01)
    assert all(-127 <= a[0] <= 127 and -127 <= a[1] <= 127 for a in d)
    # wire items decode back through the FIFO item parser
    words = [w for it in job.wire for w in it]
    assert len(F.parse_fifo_items(words)) == len(job.wire)


def test_laser_length_power_and_time():
    doc = _doc()
    job = compile_job(doc, None, Machine())
    expect = 2 * (80 + 40) + 4 * 20 + 2 * math.pi * 10 + 2.0      # contours + lead-in
    assert job.laser_on_length_mm == pytest.approx(expect, rel=0.01)
    powers = {a[4] for a in job.step_deltas() if a[3]}
    assert powers == {0xFFFF}            # ManuItem +8 = 0xFFFF for every sample (parity JB-02)
    assert job.pierces == 3
    assert job.duration_s == pytest.approx(job.estimate["total"], rel=0.05)
    assert len(job.markers) == 3
    # frames hold about 1200 bytes and carry consecutive sequence numbers
    frames = job.frames(7)
    assert [f[0] for f in frames] == list(range(7, 7 + len(frames)))
    assert max(4 * (len(f) + 3) for f in frames) <= 1300


def test_dry_run_has_no_laser():
    job = compile_job(_doc(), None, Machine(), dry_run=True)
    assert job.laser_on_length_mm == 0
    assert not any(a[3] for a in job.step_deltas())


def test_split_large_deltas_and_dwell():
    m = Machine(pulses_per_mm_x=1000.0, pulses_per_mm_y=1000.0, rapid_vel=1000.0, rapid_acc=20000.0,
                jump_add_time_ms=0)
    items = [I.Rapid((0, 0), (100, 0)), I.LaserOn(1), I.Delay(0.01), I.LaserOff()]
    job = compile_items(items, m)
    d = job.step_deltas()
    assert max(abs(a[0]) for a in d) <= 127
    assert sum(a[0] for a in d) == 100_000
    # a 1000 mm/s rapid needs 1000 pulses/ms > int8: every step is one period, so the
    # excess is spread over extra periods and reported
    assert m.max_step_speed == pytest.approx(127.0)
    assert job.overspeed_periods > 0
    assert len(d) == round(job.duration_s / (m.period_ms * 1e-3))
    dwell = [a for a in d if a[0] == 0 and a[1] == 0 and a[3]]
    assert len(dwell) == 10 and all(a[2] == 5000 for a in dwell)


def test_frame_items_laser_off():
    job = compile_items(frame_items((0, 0, 10, 5), 50.0), Machine(), (2.0, 2.0))
    assert job.laser_on_length_mm == 0
    # truncation + carry (JB-01): within one pulse of the corner, the rest is in pulse_carry
    assert all(abs(p) <= 1 for p in job.end_pulses)
    assert all(abs(p + c) < 0.01 for p, c in zip(job.end_pulses, job.pulse_carry))


def test_layer_frequency_and_duty_in_step_items():
    # ManuItem +0x14 is the PWM frequency and +0x12 the duty byte (MainApp FUN_00462630)
    doc = _doc()
    doc.layers[1].frequency_hz, doc.layers[1].duty_pct = 2000, 70
    doc.layers[2].frequency_hz, doc.layers[2].duty_pct = 800, 40
    d = compile_job(doc, None, Machine()).step_deltas()
    assert {(a[2], a[3]) for a in d if a[3]} == {(2000, 70), (800, 40)}
    assert all(a[4] == 0 for a in d if not a[3])


def test_default_step_scale_is_ppr_over_lead():
    # MainApp FUN_00458b60: step pulses/mm = WritePluse / SpeedRatio = 10000 / 100 (docs/protocol.md §11)
    m = Machine()
    assert (m.pulses_per_mm_x, m.ppr, m.lead_mm) == (100.0, 10000, 100.0)
    assert m.max_step_speed == pytest.approx(1270.0)
    job = compile_items([I.Rapid((0, 0), (100, 0))], Machine(rapid_vel=1000.0, rapid_acc=20000.0, jump_add_time_ms=0))
    assert job.overspeed_periods == 0 and sum(a[0] for a in job.step_deltas()) == 10_000
