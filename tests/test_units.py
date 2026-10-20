"""mlaser.services.units: conversion constants, kinds, formatting, parsing, round trips,
settings and change listeners (Qt-free)."""
import math
import random

import pytest

from mlaser.services import units as U
from mlaser.services.config import ConfigService
from mlaser.services.units import KINDS, Units

IMP, MET = Units("imperial"), Units("metric")


def test_exact_constants():
    assert U.MM_PER_IN == 25.4
    assert U.MM_PER_FT == pytest.approx(304.8, abs=0)
    assert U.KG_PER_LB == 0.45359237
    assert U.BAR_PER_PSI == pytest.approx(0.0689475729316836, rel=1e-15)
    assert U.L_PER_FT3 == pytest.approx(28.316846592, rel=1e-15)
    assert IMP.to_display(25.4, "length") == 1.0
    assert IMP.from_display(1.0, "length") == 25.4
    assert IMP.to_display(25.4 / 60, "speed") == pytest.approx(1.0)        # in/min
    assert IMP.to_display(1.0, "pressure") == pytest.approx(14.503773773, rel=1e-9)


def test_metric_is_identity_for_internal_kinds():
    for k in ("length", "small_length", "speed", "accel", "jerk", "area", "pressure", "mass", "flow",
              "density", "time", "angle"):
        for v in (0.0, 1.0, 3.2, -7.125, 12345.678):
            assert MET.to_display(v, k) == v
            assert MET.from_display(v, k) == v


def test_display_units_per_system():
    assert [IMP.unit(k) for k in ("length", "small_length", "long_length", "speed", "accel", "area",
                                  "large_area", "mass", "pressure", "flow", "time", "angle")] == \
        ["in", "in", "ft", "in/min", "in/s²", "in²", "ft²", "lb", "psi", "ft³/h", "s", "°"]
    assert [MET.unit(k) for k in ("length", "long_length", "speed", "accel", "large_area", "pressure")] == \
        ["mm", "m", "mm/s", "mm/s²", "m²", "bar"]


def test_imperial_decimals_and_steps():
    assert IMP.decimals("length") == 3 and IMP.resolution("length") == pytest.approx(0.0254)
    assert IMP.decimals("small_length") == 4 and IMP.step("small_length") == 0.001
    assert IMP.resolution("small_length") == pytest.approx(0.00254)
    assert MET.decimals("length") == 2 and MET.decimals("small_length") == 3


def test_fmt():
    assert IMP.fmt(3.175, "length") == "0.125 in"
    assert IMP.fmt(0.2, "small_length") == "0.0079 in"
    assert MET.fmt(3.25, "length") == "3.25 mm"
    assert IMP.fmt(None, "length") == "-"
    assert IMP.fmt(-0.0001, "length") == "0.000 in"           # no "-0.000"
    assert IMP.fmt(6.35, "length", trim=True) == "0.25 in"
    assert IMP.fmt(30.0, "angle") == "30.00°"
    assert IMP.fmt(10.0, "time") == "10.00 s"
    assert IMP.fmt_size(3048.0, 1524.0, decimals=1) == "120.0 × 60.0 in"
    assert IMP.header("Width", "length") == "Width (in)"
    assert IMP.fmt(1e6, "large_area") == "10.8 ft²"
    assert IMP.fmt(1000.0, "long_length") == "3.3 ft"
    assert "/ft²" in IMP.fmt(10.0, "per_large_area")


@pytest.mark.parametrize("text,mm", [
    ('1/8"', 3.175), ("3.2 mm", 3.2), ("0.125 in", 3.175), ("2'6\"", 762.0), ("2 ft 6 in", 762.0),
    ("1 1/2 in", 38.1), ("1-1/8\"", 28.575), ("-2'6\"", -762.0), ("2' 6 1/2\"", 774.7), ("10 cm", 100.0),
    ("0.5 inch", 12.7), ("1.5 inches", 38.1), ("3,2 mm", 3.2), ("1 ft", 304.8), ("2'", 609.6),
    ("2' 6", 762.0), ("-0.5 in", -12.7), ("1e1 mm", 10.0), ("1/8 in", 3.175), ("½ in", None),
])
def test_parse_length_with_units(text, mm):
    for u in (IMP, MET):
        if mm is None:
            with pytest.raises(ValueError):
                u.parse(text, "length")
        else:
            assert u.parse(text, "length") == pytest.approx(mm, abs=1e-9)


def test_parse_bare_number_is_display_unit():
    assert IMP.parse("1", "length") == 25.4
    assert MET.parse("1", "length") == 1.0
    assert IMP.parse(".5", "length") == 12.7
    assert IMP.parse("1/8", "length") == 3.175
    assert IMP.parse("100", "speed") == pytest.approx(100 * 25.4 / 60)


def test_parse_other_families():
    assert IMP.parse("300 mm/min", "speed") == pytest.approx(5.0)
    assert MET.parse("100 ipm", "speed") == pytest.approx(100 * 25.4 / 60)
    assert MET.parse("2 m/min", "speed") == pytest.approx(2000 / 60)
    assert IMP.parse("40 psi", "pressure") == pytest.approx(40 * U.BAR_PER_PSI)
    assert IMP.parse("1.5 bar", "pressure") == 1.5
    assert IMP.parse("1 g", "accel") == pytest.approx(9806.65)
    assert IMP.parse("2 lb", "mass") == pytest.approx(2 * 0.45359237)
    assert IMP.parse("1 m²", "area") == pytest.approx(1e6)


@pytest.mark.parametrize("bad", ["", "abc", "3 psi", "1/0", "3 mm 4", "3 in in", "--3", "mm"])
def test_parse_errors(bad):
    with pytest.raises(ValueError):
        IMP.parse(bad, "length")


def test_unknown_kind():
    with pytest.raises(ValueError):
        IMP.fmt(1.0, "lenght")


def test_round_trip_within_half_a_step():
    """mm -> display text -> mm stays within half a display step of the original."""
    rnd = random.Random(7)
    for u in (IMP, MET, Units("imperial", {"speed": "in/s"}), Units("metric", {"speed": "m/min"})):
        for kind in KINDS:
            half = u.resolution(kind) / 2 * (1 + 1e-9)
            for _ in range(200):
                v = rnd.uniform(-5000, 5000) if kind not in ("area", "large_area") else rnd.uniform(0, 5e6)
                back = u.parse(u.fmt(v, kind), kind)
                assert abs(back - v) <= half + 1e-12 * abs(v), (kind, u, v, back)
                assert u.from_display(u.to_display(v, kind), kind) == pytest.approx(v, rel=1e-12, abs=1e-12)


def test_overrides_and_decimals():
    u = Units("metric", {"speed": "m/min", "length": "in", "pressure": "psi", "bogus": "x", "area": "nope"})
    assert u.unit("speed") == "m/min" and u.unit("length") == "in" and u.unit("pressure") == "psi"
    assert u.unit("area") == "mm²"                       # invalid override ignored
    assert u.unit("long_length") == "m"                  # not overridable
    v = Units("imperial", {"speed": "ipm"})
    assert v.unit("speed") == "in/min"
    d = Units("imperial", decimals={"length": 2})
    assert d.decimals("length") == 2 and d.decimals("small_length") >= 3


def test_settings_registered_and_follow_config(tmp_path):
    cfg = ConfigService(tmp_path / "c.json", load=False)
    for k in ("units.system", "units.length", "units.speed", "units.accel", "units.area", "units.mass",
              "units.pressure", "units.decimals_length"):
        assert cfg.setting(k) is not None and cfg.setting(k).section == "units"
    assert cfg.get("units.system") == "metric"
    seen = []
    U.bind(cfg)
    unsub = U.subscribe(seen.append)
    try:
        assert U.current() == MET
        cfg.set("units.system", "imperial")
        assert U.current().imperial and seen[-1].imperial
        cfg.set("units.speed", "ipm")
        assert U.current().unit("speed") == "in/min"
        cfg.set("units.decimals_length", "2")
        assert U.current().decimals("length") == 2
        n = len(seen)
        cfg.set("bed.slat_pitch", 120.0)                 # other keys do not fire
        assert len(seen) == n
        cfg.reset("units.speed")
        cfg.reset("units.decimals_length")
        U.set_system("metric")
        assert cfg.get("units.system") == "metric" and not U.current().imperial
    finally:
        unsub()
        U.reset()


def test_switching_never_touches_stored_values(tmp_path):
    cfg = ConfigService(tmp_path / "c.json", load=False)
    cfg.set("bed.slat_pitch", 101.6)
    before = dict(cfg.values)
    U.bind(cfg)
    try:
        cfg.set("units.system", "imperial")
        after = {k: v for k, v in cfg.values.items() if not k.startswith("units.")}
        assert after == before and cfg.get("bed.slat_pitch") == 101.6
        assert U.current().fmt(cfg.get("bed.slat_pitch"), "length") == "4.000 in"
    finally:
        U.reset()


def test_listener_errors_do_not_stop_others():
    got = []
    u1 = U.subscribe(lambda u: 1 / 0)
    u2 = U.subscribe(got.append)
    try:
        U.set_current(IMP)
        assert got and got[-1] == IMP
    finally:
        u1(), u2()
        U.reset()


def test_kind_for_unit():
    assert U.kind_for_unit("mm") == "length" and U.kind_for_unit("mm / s") == "speed"
    assert U.kind_for_unit("mm/s2") == "accel" and U.kind_for_unit("bar") == "pressure"
    assert U.kind_for_unit("/m") == "per_long_length" and U.kind_for_unit("/m²") == "per_large_area"
    assert IMP.to_display(10.0, U.kind_for_unit("/m²")) == pytest.approx(10.0 * 0.09290304)
    assert U.kind_for_unit("%") is None and U.kind_for_unit("p/mm") is None and U.kind_for_unit("") is None


def test_convert():
    assert U.convert(1, "in", "mm") == 25.4
    assert U.convert(60, "in/min", "in/s") == pytest.approx(1.0)
    with pytest.raises(ValueError):
        U.convert(1, "in", "psi")
    assert math.isclose(U.convert(1, "ft²", "in²"), 144.0)


def test_compiled_jobs_identical_in_imperial(tmp_path):
    """The planner / wire never see the display units: the golden job records of a few seeded
    random jobs are identical with the system bound to imperial (tools/make_goldens.py --check
    proves the same for every golden)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "make_goldens", Path(__file__).resolve().parents[1] / "tools" / "make_goldens.py")
    mg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mg)
    seeds = (0, 1, 5, 17)
    metric = [mg.random_case(s)[0] for s in seeds]
    assert all("wire" in r for r in metric), metric
    cfg = ConfigService(tmp_path / "c.json", load=False)
    U.bind(cfg)
    try:
        cfg.set("units.system", "imperial")
        cfg.set("units.speed", "ipm")
        imperial = [mg.random_case(s)[0] for s in seeds]
    finally:
        U.reset()
    assert imperial == metric


def test_preflight_messages_follow_units():
    from mlaser.machine.preflight import TravelLimits, check

    t = TravelLimits((0.0, 1270.0), (0.0, 635.0))
    rep = check("move", target=(2540.0, 0.0), travel=t)
    assert any("(2540.0, 0.0) mm" in i.message for i in rep.issues)
    U.set_current(IMP)
    try:
        rep = check("move", target=(2540.0, 0.0), travel=t)
        msg = [i.message for i in rep.issues if i.code == "out_of_travel"][0]
        assert "(100.000, 0.000) in" in msg and "X 0.000..50.000 in" in msg and "mm" not in msg
    finally:
        U.reset()
    assert "mm" in TravelLimits((0, 1), (0, 1)).describe()


def test_service_messages_follow_units_and_stay_exact_in_metric():
    """Messages built below the UI (remnants, parts, material check, tipping): exactly the old
    metric text in metric, display units in imperial."""
    from mlaser.cam.tipping import TipPiece
    from mlaser.services.material_check import SheetSpec
    from mlaser.services.remnants import Remnant

    rem = Remnant([[(0.0, 0.0), (1219.2, 0.0), (1219.2, 609.6), (0.0, 609.6)]], "S235", 3.0, id="R1")
    tip = TipPiece((0, 1), False, "tips", size=(50.8, 25.4))
    sheet = SheetSpec("S235", 3.175)
    assert rem.label() == "R1 1219x610 mm S235 3 mm"
    assert tip.message.startswith("part part 1 contour 2 (51 x 25 mm)")
    assert sheet.label() == "S235 3.175 mm"
    assert U.mm_text(0.2, ".3f", "small_length") == "0.200 mm"
    U.set_current(IMP)
    try:
        assert rem.label() == "R1 48.00x24.00 in S235 0.118 in"
        assert "(2.00 x 1.00 in)" in tip.message
        assert sheet.label() == "S235 0.125 in"
        assert U.mm_text(0.2, ".3f", "small_length") == "0.0079 in"
        with U.using(MET):
            assert sheet.label() == "S235 3.175 mm"
        assert U.current() is IMP
    finally:
        U.reset()
    assert tip.message.startswith("part part 1 contour 2 (51 x 25 mm)")


def test_original_unit_rows_map_onto_units_settings(tmp_path):
    """MC.UnitType / UN.SpeedUnit / UN.AccUnit / UN.GasPressureUnit (the original's rows) are
    copied onto units.* once, when set and the target is not: one units setting per quantity."""
    cfg = ConfigService(tmp_path / "c.json", load=False)
    cfg.set("UN.SpeedUnit", 3)                 # Inch/min
    cfg.set("UN.GasPressureUnit", 1)           # MPa
    cfg.set("units.accel", "g")
    cfg.set("UN.AccUnit", 2)                   # target already set: kept
    U.bind(cfg)
    try:
        assert cfg.get("units.speed") == "in/min" and cfg.get("units.pressure") == "MPa"
        assert cfg.get("units.accel") == "g" and not cfg.is_set("units.system")
        assert U.current().unit("speed") == "in/min" and U.current().unit("pressure") == "MPa"
        assert cfg.get("UN.GasPressureUnit") == 1  # its storage meaning stays
        cfg.set("MC.UnitType", 1)                  # e.g. an import later
        assert cfg.get("units.system") == "imperial" and U.current().imperial
        cfg.set("units.pressure", "psi")           # the units.* setting is the editor from now on
        cfg.set("UN.GasPressureUnit", 0)
        assert cfg.get("units.pressure") == "psi"
    finally:
        U.reset()
