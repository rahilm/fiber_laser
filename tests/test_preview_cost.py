"""Preview timeline (mlaser.services.preview) and the time / cost card (mlaser.services.costing).

The scrubber plays the PlannedJob Start streams: its time axis is the wire (one servo period per
step item plus the process items' card time) and its head position the pulses streamed so far."""

from __future__ import annotations

import math
import random

import pytest

from mlaser.cam.toolpath import CamParams
from mlaser.model import LayerParams
from mlaser.nc.machine import Machine
from mlaser.ops import demo_document
from mlaser.protocol import frames as F
from mlaser.services import costing as C
from mlaser.services.jobs import InlineExecutor, JobRequest, JobService
from mlaser.services.preview import CUT, TRAVEL, TimeStats, build_timeline
from mlaser.services.technology import apply_to_layer
from mlaser.services.technology_examples import examples


def _plan(tech_index: int = 4, dry_run: bool = False):
    doc = demo_document()
    tech = examples()[tech_index]
    for li in {p.craft.layer for p in doc.parts}:
        doc.layers[li] = apply_to_layer(LayerParams(index=li), tech)
    js = JobService(executor=InlineExecutor())
    req = JobRequest("process", doc, Machine(), (0.0, 0.0), CamParams(), dry_run=dry_run)
    js.last_req = req
    return js, js.plan_sync(req)


@pytest.fixture(scope="module")
def planned():
    js, plan = _plan()
    return plan, build_timeline(plan)


def test_timeline_totals_match_the_job(planned):
    plan, tl = planned
    job = plan.job
    assert tl.total_s == pytest.approx(job.total_time_s, abs=1e-9)
    st = tl.stats()
    assert st.cut_length_mm == pytest.approx(job.laser_on_length_mm, rel=1e-9)
    assert st.cut_s + st.travel_s + st.dwell_s + st.process_s == pytest.approx(tl.total_s)
    assert st.process_s == pytest.approx(job.process_time_s, abs=1e-9)
    assert sum(st.laser_on_by_layer.values()) == pytest.approx(st.laser_on_s)
    end = tl.state_at(tl.total_s)
    m = job.machine
    assert (end.x, end.y) == pytest.approx((job.end_pulses[0] / m.pulses_per_mm_x, job.end_pulses[1] / m.pulses_per_mm_y))
    assert len(tl.markers) == len(job.markers)
    assert len(tl.blocks) == len(job.cut_blocks)
    assert any(tl.pierce)                     # staged pierce of the technology


def test_scrubber_position_maps_to_wire_samples(planned):
    """Slider time -> wire item -> head position equals the pulses of every step item streamed
    up to that item, and wire item -> time -> wire item is the identity for step items."""
    plan, tl = planned
    job = plan.job
    m = job.machine
    wire = list(job.wire)
    steps = [(i, F.decode_step(w[1:3])) for i, w in enumerate(wire) if (w[0] & 0xFFFF) == F.OP_STEP]
    before: dict[int, tuple[int, int]] = {}                  # pulses streamed before step i
    x, y = job.start_pulses
    for i, (dx, dy, _f, _d, _p) in steps:
        before[i] = (x, y)
        x += dx
        y += dy
    rnd = random.Random(7)
    for i, _ in rnd.sample(steps, 300):
        t = tl.time_of_wire(i)
        st = tl.state_at(t + tl.period_s * 0.5)              # while step i executes
        assert st.wire == i
        px, py = before[i]
        assert (st.x, st.y) == pytest.approx((px / m.pulses_per_mm_x, py / m.pulses_per_mm_y), abs=1e-9)
        dec = F.decode_step(wire[i][1:3])
        assert st.laser == (dec[3] != 0) or st.kind not in (CUT, TRAVEL)
    # the slider works in integer milliseconds: every ms position lands in the job
    for ms in range(0, int(tl.total_s * 1000), 997):
        st = tl.state_at(ms / 1000.0)
        assert 0 <= st.wire < len(wire)
    # jump to contour: each marker's time is where its wire index starts
    for t, w, p, c in tl.markers:
        assert tl.time_of_wire(w) == pytest.approx(t)
        assert tl.contour_at(t) is not None
    ts = sorted({mk[0] for mk in tl.markers})
    assert tl.next_contour_time(ts[0]) == ts[1]
    assert tl.next_contour_time(ts[1], -1) == ts[0]


def test_path_until_draws_the_streamed_path(planned):
    plan, tl = planned
    xs, ys, las = tl.path_until(tl.total_s)
    assert len(xs) == len(ys) == len(las)
    cut_len = sum(math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1]) for k in range(1, len(xs)) if las[k])
    assert cut_len == pytest.approx(plan.job.laser_on_length_mm, rel=1e-6)
    xs2, _, _ = tl.path_until(tl.total_s, max_points=500)
    assert len(xs2) < len(xs)


def test_preview_is_the_plan_start_streams():
    """The plan the scrubber shows is the object Start hands to the controller (cache hit)."""
    js0, plan = _plan()
    queue: list = []                          # the GUI thread's event queue
    js = JobService(executor=InlineExecutor(), post=queue.append)
    js.cache.update(js0.cache)                # the preview plan is in the cache
    started = []

    class Sub:
        def start_job(self, p):
            started.append(p)

    js.run(js0.last_req, Sub())
    while queue:
        queue.pop(0)()
    assert started == [plan] and started[0] is plan
    assert build_timeline(started[0]).key == plan.key


def test_dry_run_has_no_laser_time():
    _, plan = _plan(dry_run=True)
    tl = build_timeline(plan)
    assert tl.stats().laser_on_s == 0.0
    assert tl.total_s == pytest.approx(plan.job.total_time_s)


# --------------------------------------------------------------------------- cost
def _stats(**kw) -> TimeStats:
    base = dict(total_s=3600.0, cut_s=1800.0, travel_s=600.0, dwell_s=200.0, process_s=1000.0,
                laser_on_s=2000.0, cut_length_mm=50000.0, travel_length_mm=20000.0, gas_on_s=2400.0,
                gas_by_layer={1: 2400.0}, laser_on_by_layer={1: 2000.0})
    base.update(kw)
    return TimeStats(**base)


def test_cost_arithmetic():
    info = {1: C.LayerInfo(1, power_pct=80.0, gas="N2", pressure_bar=12.0, flow_l_min=250.0, nozzle="1.5 single",
                           power_w=1500.0, density_g_cm3=7.93, thickness_mm=2.0, material="SS")}
    rates = C.Rates(machine_per_h=60.0, power_per_kwh=0.30, gas_per_m3={"N2": 1.2, "O2": 2.0, "Air": 0.0},
                    material_per_kg=4.0, wall_plug_eff=0.4, machine_kw=5.0)
    card = C.cost_card(_stats(), info, rates, pierces=12, cut_bbox=(0.0, 0.0, 1000.0, 500.0))
    v = card.value
    assert v("time.total") == 3600.0 and v("count.pierces") == 12.0 and v("length.cut") == 50.0
    gas = 250.0 * 2400.0 / 60.0 / 1000.0                       # l/min x s -> m³
    assert v("gas.volume") == pytest.approx(gas)
    laser = 1500.0 * 0.8 / 0.4 * 2000.0 / 3.6e6               # kWh
    machine = 5.0 * 1.0
    assert v("energy.laser") == pytest.approx(laser)
    assert v("energy.machine") == pytest.approx(machine)
    assert v("power.peak") == pytest.approx(1500 * 0.8 / 0.4 / 1000 + 5.0)
    mass = 1000 * 500 * 2.0 * 7.93 / 1e6                      # mm³ x g/cm³ -> kg
    assert v("material.mass") == pytest.approx(mass)
    assert v("cost.machine") == pytest.approx(60.0)
    assert v("cost.gas") == pytest.approx(gas * 1.2)
    assert v("cost.energy") == pytest.approx((laser + machine) * 0.30)
    assert v("cost.material") == pytest.approx(mass * 4.0)
    assert v("cost.total") == pytest.approx(60.0 + gas * 1.2 + (laser + machine) * 0.30 + mass * 4.0)
    assert not card.missing
    assert card.get("cost.total").kind == "estimate"


def test_cost_missing_inputs_are_asked_for_not_zero():
    info = {1: C.LayerInfo(1, power_pct=100.0, gas="O2", pressure_bar=0.8, nozzle="")}
    card = C.cost_card(_stats(), info, C.Rates(), pierces=1)
    miss = {ln.key for ln in card.missing}
    assert {"gas.volume", "energy.laser", "material.mass", "cost.machine", "cost.total"} <= miss
    assert card.get("gas.volume").note == "cost.note.gas_missing"
    assert card.get("cost.machine").value is None              # a rate of 0 is "not set", not 0 €
    # the user's flow answers the gas question
    card2 = C.cost_card(_stats(), info, C.Rates(gas_flow_l_min=20.0))
    assert card2.value("gas.volume") == pytest.approx(20.0 * 2400 / 60 / 1000)
    assert card2.get("gas.volume").kind == "estimate"


def test_orifice_flow():
    # choked: m = Cd A p0 sqrt(g/(R T)) (2/(g+1))^((g+1)/(2(g-1)))
    g, R, rho = C.GAS_PROPS["N2"]
    p0 = (12.0 + C.P_ATM_BAR) * 1e5
    A = math.pi * 1.5e-3 ** 2 / 4
    m = 0.8 * A * p0 * math.sqrt(g / (R * C.T_GAS_K)) * (2 / (g + 1)) ** ((g + 1) / (2 * (g - 1)))
    assert C.orifice_flow_l_min(12.0, 1.5, "N2", 0.8) == pytest.approx(m / rho * 60000)
    assert 150 < C.orifice_flow_l_min(12.0, 1.5, "N2") < 300           # the published range
    low = C.orifice_flow_l_min(0.5, 1.0, "O2")                           # subsonic branch
    assert 0 < low < C.orifice_flow_l_min(1.0, 1.0, "O2")
    assert C.nozzle_diameter("1.5 single") == 1.5 and C.nozzle_diameter("double") is None
    # a nozzle with a diameter and a pressure gives an estimate instead of a question
    info = {1: C.LayerInfo(1, 100.0, "N2", 12.0, nozzle="1.5 single", power_w=1500)}
    card = C.cost_card(_stats(), info, C.Rates())
    assert card.get("gas.volume").kind == "estimate" and card.get("gas.volume").note == "cost.note.gas_orifice"


def test_card_for_plan(planned):
    plan, tl = planned
    card = C.card_for_plan(plan, tl, C.Rates(machine_per_h=50.0))
    assert card.value("time.total") == pytest.approx(plan.job.total_time_s)
    assert card.value("cost.machine") == pytest.approx(plan.job.total_time_s / 3600 * 50.0)
    assert card.value("count.pierces") == plan.job.pierces
    assert card.get("material.mass").value and card.get("material.mass").value > 0
